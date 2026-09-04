"""飞书开放平台 REST 调用的传输层、分页遍历与错误映射。

从 :mod:`lingxi.adapters.feishu_directory` 纯移动而来：这里只管"怎么把一个
HTTP 请求安全、节流、按需重试地发出去，并把飞书的分页/错误响应形状收敛成
干净的 Python 结构"，不涉及任何具体业务端点。``feishu_directory`` 的
``FeishuDirectoryClient`` 与 ``FeishuAuthorizationClient`` 都在此基础上组装
各自的业务方法。

凭据边界：``app_secret`` 与 ``refresh_token`` 只出现在请求体里，不进 URL、
不进日志、不进异常消息（``FeishuDirectoryError.code`` 只允许安全字符集）。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# 单页上限与翻页安全上界。上界存在的理由是 page_token 异常时不能无限翻页；
# 撞到上界是错误，不是"读完了"。
PAGE_SIZE = 100
MAX_PAGES = 200
REQUEST_TIMEOUT_SECONDS = 20

# 真实请求前的固定节流：组织快照整轮遍历要发几百次突发请求，回源实测证明
# 不节流会打穿飞书的累计频率限制。数值沿用已受控验收的历史脚本配方
# （约 8 req/s），见 _PagedClient._throttle 的文档字符串。
REQUEST_PAUSE_SECONDS = 0.12

# 节流抖动上限：多条工作线程可能共享同一个 client 实例，固定停顿量本身会让
# 它们"一起睡、一起醒"，在飞书那侧看到周期性尖峰。抖动量取节流步长的一半，
# 只做相位错开，不是令牌桶/漏桶那类全局限速器，见 _throttle 文档字符串。
REQUEST_PAUSE_JITTER_SECONDS = REQUEST_PAUSE_SECONDS / 2

# 频率限制的窄而有界重试：只对这一个错误码重试，且次数与退避都固定，见
# _PagedClient._request 的文档字符串。
FEISHU_RATE_LIMIT_ERROR_CODE = "feishu_code_99991400"
RATE_LIMIT_RETRY_BACKOFFS_SECONDS: tuple[float, ...] = (2.0, 5.0, 10.0)


class FeishuDirectoryError(RuntimeError):
    """飞书调用失败。``code`` 供程序判断，消息里不含任何凭据。

    ``definite`` 表示"飞书明确拒绝"（收到了业务错误码）而非"结果不明确"
    （传输层异常、超时等）。这是协议细节的唯一出口：apps 层只读该属性，
    不解析 ``code`` 的字符串形状。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        """记录分类标签 ``code``，``definite`` 缺省时按 ``feishu_code_`` 前缀推断。"""
        super().__init__(f"飞书目录接口调用失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS：``http://`` 会把 Bearer token、App Secret 与一次性 refresh token 明文上路。"""
    return _require_https_uri(base_url, "飞书 base_url").rstrip("/")


def _require_https_uri(value: object, label: str) -> str:
    """校验外部 OAuth URL，不把配置原值带进错误消息。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    text = value.strip()
    parsed = urlparse(text)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label}必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError(f"{label}不得包含 URL fragment")
    return text


class Transport(Protocol):
    """一次 HTTP 调用的最小形状：方法、URL、可选请求体与令牌。"""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, Any] | None = ...,
        token: str | None = ...,
    ) -> Any:
        """发起一次请求并返回已解析的响应（或按传输层约定抛出）。"""
        ...


def urllib_transport(
    method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None
) -> Any:
    """默认传输层：只发 HTTPS，本身不重试。

    有副作用的请求（``POST``）从不重试；只读的 ``GET`` 分页请求在更上层
    （``_PagedClient._request``）对频率限制错误码单独重试，这层本身始终
    只是"发一次、按原样返回或抛出"，不知道上面有没有重试。
    """
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # 地址来自受控配置
            return json.loads(response.read())
    except HTTPError as error:
        try:
            return json.loads(error.read())
        except Exception as parse_error:
            raise FeishuDirectoryError(f"http_{error.code}") from parse_error
    except (URLError, OSError, TimeoutError) as error:
        raise FeishuDirectoryError("transport_error") from error
    except ValueError as error:
        raise FeishuDirectoryError("invalid_json") from error


def _safe_feishu_code(value: object) -> str:
    """把飞书业务错误码渲染成审计安全的分类标签。

    ``code`` 来自不可信的外部响应：只在它是货真价实的 ``int``（排除
    ``bool``，因为 ``bool`` 是 ``int`` 的子类）时才插值，否则退化成固定
    标签 ``feishu_code_invalid``——避免把响应内容原样拼进空格分隔的
    ``k=v`` 审计行，被用来注入伪造的审计记录。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return f"feishu_code_{value}"
    return "feishu_code_invalid"


_UNSAFE_CODE_FRAGMENT_CHAR = re.compile(r"[^A-Za-z0-9_.:-]")


def _sanitize_code_fragment(value: str, *, max_length: int = 40) -> str:
    """把可能来自不可信响应的文本收窄成能安全拼进 ``FeishuDirectoryError.code`` 的片段。

    截断长度，并把不在安全字符集内的字符替换成 ``_``——同 ``_safe_feishu_code``
    一样防止响应内容（这里是一个陌生的分页列表键名）注入或撑爆空格分隔的审计行。
    """
    return _UNSAFE_CODE_FRAGMENT_CHAR.sub("_", value[:max_length])


def _payload(response: Any) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise FeishuDirectoryError("invalid_response_shape")
    code = response.get("code")
    if code not in (None, 0, "0"):
        raise FeishuDirectoryError(_safe_feishu_code(code))
    data = response.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def _first_matching_list(data: Mapping[str, Any], keys: tuple[str, ...]) -> list[Any] | None:
    """按候选键顺序找第一个类型为列表的字段值。"""
    for candidate in keys:
        value = data.get(candidate)
        if isinstance(value, list):
            return value
    return None


def _first_stray_list_key(data: Mapping[str, Any]) -> str | None:
    """在一个"候选键都没命中"的响应里，找出候选表之外的非空列表字段。

    命中即说明真实字段名很可能已经改名，候选表需要更新；调用方据此抛出
    ``unexpected_list_key_<字段名>`` 而不是把它当成合法空结果静默放过。
    """
    return next((key for key, value in data.items() if isinstance(value, list) and value), None)


class _PagedClient:
    """飞书分页 GET 请求的节流、重试与错误映射。

    供 :class:`~lingxi.adapters.feishu_directory.FeishuDirectoryClient` 与
    :class:`~lingxi.adapters.feishu_directory.FeishuAuthorizationClient` 复用。
    节流步长、抖动上限、重试退避序列、随机源与 sleep 都是可注入的构造
    参数，默认值本身就是生效状态（非零）——测试通过覆盖这些参数换取确定性
    与速度，生产路径不需要额外接线。
    """

    def __init__(
        self,
        *,
        base_url: str,
        transport: Callable[..., Any] | None = None,
        request_pause_seconds: float = REQUEST_PAUSE_SECONDS,
        request_pause_jitter_seconds: float = REQUEST_PAUSE_JITTER_SECONDS,
        sleep: Callable[[float], None] | None = None,
        random_source: Callable[[], float] = random.random,
        rate_limit_retry_backoffs: tuple[float, ...] = RATE_LIMIT_RETRY_BACKOFFS_SECONDS,
        round_deadline_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        self._base_url = _require_https(base_url)
        self._transport: Callable[..., Any] = transport or urllib_transport
        self._request_pause_seconds = request_pause_seconds
        self._request_pause_jitter_seconds = request_pause_jitter_seconds
        self._random_source: Callable[[], float] = random_source
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._rate_limit_retry_backoffs = rate_limit_retry_backoffs
        # 只在 round_budget() 的 with 块内非 None；默认关闭对未调用过它的
        # 调用方（如开通链）零影响。
        self._round_deadline: float | None = None
        self._round_deadline_clock: Callable[[], float] = round_deadline_clock

    @contextmanager
    def round_budget(self, *, seconds: float) -> Iterator[None]:
        """限定这个 client 实例内新请求的准入线。

        进入这个 ``with`` 块起，``_request`` 在每次真正发起请求前检查是否已过
        ``seconds`` 秒截止，撞线立即抛 ``FeishuDirectoryError("round_budget_exceeded")``
        而不再发出该请求。这不是"整个 ``with`` 块最多用 ``seconds`` 秒"的硬保证——判定点只在
        发起请求前，已经在途的单次操作（节流等待 + 一次 HTTP 请求，必要时
        含一次限频重试退避）仍会跑完；真实上界是「预算 + 单次操作最大
        时长」。默认关闭（``_round_deadline`` 为 ``None`` 时检查恒不成立），
        嵌套调用用内层截止覆盖外层，退出后恢复外层原值。
        """
        previous = self._round_deadline
        self._round_deadline = self._round_deadline_clock() + seconds
        try:
            yield
        finally:
            self._round_deadline = previous

    def _throttle(self) -> None:
        """真实请求前固定停顿一段时间，外加一点随机抖动。

        组织快照同步是递归遍历、单轮上千次请求，不节流会打穿飞书的累计
        频率限制；停顿量取自已验收的历史脚本配方（约 8 req/s）。抖动打散
        多线程共享同一个 client 实例时"一起睡、一起醒"造成的尖峰，不是
        令牌桶/漏桶那类维护跨调用状态的重型节流器。两个参数为假值
        （``0``）时分别完全不调用 ``self._sleep``/``self._random_source``，
        不是调用 ``sleep(0)``——测试据此验证"可以关掉"。
        """
        if self._request_pause_seconds:
            jitter = (
                self._request_pause_jitter_seconds * self._random_source()
                if self._request_pause_jitter_seconds
                else 0.0
            )
            self._sleep(self._request_pause_seconds + jitter)

    def _request(self, url: str, *, token: str) -> dict[str, Any]:
        """节流后发一次 ``GET`` 请求并解码；只对飞书频率限制错误码做窄而有界的重试。

        重试只在 ``FeishuDirectoryError.code`` 精确等于
        ``FEISHU_RATE_LIMIT_ERROR_CODE`` 时触发，次数有界（默认 3 次，退避
        递增 2/5/10 秒），每次重试前仍照常节流；其他业务错误码、协议层
        形状错误、传输层异常一律原样抛出、不重试——那些是真失败，重试只会
        掩盖或对已过载的序列继续加压。次数耗尽后原样重新抛出最后一次的
        错误，不覆盖基线。整轮预算检查（见 :meth:`round_budget`）在节流
        之前：撞线不占用节流、不计入重试次数。
        """
        attempt = 0
        while True:
            if (
                self._round_deadline is not None
                and self._round_deadline_clock() >= self._round_deadline
            ):
                raise FeishuDirectoryError("round_budget_exceeded")
            self._throttle()
            try:
                return _payload(self._transport("GET", url, body=None, token=token))
            except FeishuDirectoryError as error:
                if error.code != FEISHU_RATE_LIMIT_ERROR_CODE or attempt >= len(
                    self._rate_limit_retry_backoffs
                ):
                    raise
                self._sleep(self._rate_limit_retry_backoffs[attempt])
                attempt += 1

    def _next_page_url(self, path: str, *, query: Mapping[str, Any], page_token: str | None) -> str:
        parameters = {**query, "page_size": PAGE_SIZE}
        if page_token:
            parameters["page_token"] = page_token
        return f"{self._base_url}{path}?{urlencode(parameters)}"

    def _pages(
        self, path: str, *, token: str, query: Mapping[str, Any], keys: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """按候选键名读一份分页列表；空 ⇒ 空，畸形 ⇒ 抛错。

        只有全部满足才判定为合法空结果：第一页（无游标、未收集到任何数据）、
        ``keys`` 里的候选字段全部不在响应里、且 ``has_more`` 严格为
        ``False``。半页、``has_more=True`` 却没有列表、候选键存在但类型不对，
        都视为响应形状错误并抛错。空响应里若存在候选表之外的非空列表字段，
        抛 ``unexpected_list_key_<字段名>``——通常意味着字段名已改名。
        ``has_more`` 非法类型时只记 warning、按"读完了"处理，不硬抛。
        """
        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            data = self._request(
                self._next_page_url(path, query=query, page_token=page_token), token=token
            )
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                logger.warning(
                    "组织快照分页响应的 has_more 不是合法 bool，按既有宽容语义"
                    "处理（不阻塞本轮）path=%s type=%s",
                    path,
                    type(has_more).__name__,
                )
            items = _first_matching_list(data, keys)
            if items is None:
                if (
                    page_token is None
                    and not collected
                    and has_more is False
                    and not any(candidate in data for candidate in keys)
                ):
                    stray = _first_stray_list_key(data)
                    if stray is not None:
                        raise FeishuDirectoryError(
                            f"unexpected_list_key_{_sanitize_code_fragment(stray)}"
                        )
                    return collected
                raise FeishuDirectoryError(f"missing_{keys[0]}")
            for item in items:
                if not isinstance(item, Mapping):
                    # 静默丢弃会让对应租户躲过完整性校验、让半轮快照被标记为完成。
                    raise FeishuDirectoryError("invalid_page_item")
                collected.append(item)
            next_token = data.get("page_token")
            if has_more is not True:
                return collected
            # has_more=true 但游标缺失或停滞：服务端明确说还有数据，把已收集
            # 的半截结果当成功返回会让调用方用它替换掉旧快照。
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                raise FeishuDirectoryError("pagination_stalled")
            page_token = next_token
        raise FeishuDirectoryError("pagination_limit")

    def _pages_multi_empty_result(
        self,
        data: Mapping[str, Any],
        *,
        page_token: str | None,
        collected: dict[str, list[dict[str, Any]]],
        list_keys: tuple[str, ...],
    ) -> dict[str, list[dict[str, Any]]]:
        """候选键全部缺失时，判定这一页是否为合法空结果；不是则抛错。

        每条分支都以返回合法空结果或抛错收尾，不会走到"继续翻页"。

        判据用 ``is`` 身份比较（``x is False or x is None``），不能写成
        ``x in (False, None)``：``0 == False`` 为真而 ``in`` 用的是 ``==``，
        会让一个 ``has_more: 0`` 误判成合法空结果。
        """
        empty_page_has_more = data.get("has_more")
        is_first_untouched_page = page_token is None and not any(
            collected[key] for key in list_keys
        )
        if not (
            is_first_untouched_page
            and (empty_page_has_more is False or empty_page_has_more is None)
        ):
            raise FeishuDirectoryError(f"missing_{list_keys[0]}")
        stray = _first_stray_list_key(data)
        if stray is not None:
            raise FeishuDirectoryError(f"unexpected_list_key_{_sanitize_code_fragment(stray)}")
        return collected

    def _pages_multi(
        self, path: str, *, token: str, query: Mapping[str, Any], list_keys: tuple[str, ...]
    ) -> dict[str, list[dict[str, Any]]]:
        """同一分页游标下同时收集多个并列列表（如 ``share_departments``/``share_users``）。

        与 :meth:`_pages` 的空结果判据同构，但改成"至少一个候选键命中即正常
        收集"：飞书对这类端点只返回非空的那个列表，另一侧为空时该键整个不
        出现。全部候选键都缺失时的合法空结果判定见
        :meth:`_pages_multi_empty_result`；候选键存在但类型不对时维持硬抛。
        翻页阶段 ``has_more`` 必须是货真价实的 ``bool``，非法类型硬抛——与
        :meth:`_pages` 降级为 warning 刻意不同。
        """
        collected: dict[str, list[dict[str, Any]]] = {key: [] for key in list_keys}
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            data = self._request(
                self._next_page_url(path, query=query, page_token=page_token), token=token
            )
            hit_keys: list[str] = []
            for key in list_keys:
                if key not in data:
                    continue
                value = data[key]
                if not isinstance(value, list):
                    raise FeishuDirectoryError(f"missing_{key}")
                hit_keys.append(key)
            if not hit_keys:
                return self._pages_multi_empty_result(
                    data, page_token=page_token, collected=collected, list_keys=list_keys
                )
            for key in hit_keys:
                for item in data[key]:
                    if not isinstance(item, Mapping):
                        raise FeishuDirectoryError("invalid_page_item")
                    collected[key].append(item)
            next_token = data.get("page_token")
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                raise FeishuDirectoryError("has_more_invalid")
            if has_more is False:
                return collected
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                raise FeishuDirectoryError("pagination_stalled")
            page_token = next_token
        raise FeishuDirectoryError("pagination_limit")
