"""飞书开放平台的目录读取与专用授权续期客户端。

这是 `adapters/` 层：协议细节、分页、错误码映射都止步于此，飞书的返回结构
不得泄漏进 `core/` 的函数签名。规则在
:mod:`lingxi.core.identity.org_snapshot` 与 :mod:`lingxi.core.identity.credentials`。

**本模块的真实调用不在本次测试范围内。** 分页与错误映射的形状由注入的假传输层
锁住（tests/test_feishu_directory_adapter.py），真实链路属 L4a，留给
`biai-stage` + `Bot-Test`。请求路径与两条身份路径的用法吸收自已受控验证过的
`scripts/sync_feishu_org_snapshot.py`。

凭据边界：`app_secret` 与 `refresh_token` 只出现在**请求体**里，不进 URL、
不进日志、不进异常消息。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from lingxi.core.identity.credentials import AuthorizationGrant, DerivedAccessToken, SecretToken

logger = logging.getLogger(__name__)

# 单页上限与翻页安全上界。上界存在的理由是 page_token 异常时不能无限翻页；
# 撞到上界是错误，不是"读完了"。
PAGE_SIZE = 100
MAX_PAGES = 200
REQUEST_TIMEOUT_SECONDS = 20

# 真实请求前的固定节流（Issue #271）：一整轮组织快照遍历要发 550+ 次突发
# 请求，回源实测证明这会打穿飞书的累计频率限制（feishu_code_99991400）。
# 0.12 秒（约 8 req/s）是已受控验收、真跑通过 8 租户 710 人的历史脚本
# scripts/sync_feishu_org_snapshot.py 用过的配方，这里照抄同一个数值，
# 不新造节流算法。详见 _PagedClient._throttle 的文档字符串。
REQUEST_PAUSE_SECONDS = 0.12

# 频率限制的窄而有界重试（Issue #271 编排者 2026-08-20 补充压测证据）：
# 0.12 秒节流在 2 倍真实规模（564 次调用）下实测未撞限频，但对照的无节流
# 基线在约 1.9 req/s 撞墙、节流后的实测速率约 1.58 req/s 未撞——只差约 17%，
# 且飞书这个配额的确切机制（滚动窗口 / 每分钟额度 / 其他）未回源确认，
# 0.12 秒是"刚好越过线"而不是"留厚余量"的经验值。只对这一个错误码重试，
# 不对其他业务错误码、形状错误、传输错误扩大——那些是真失败，重试只会掩盖
# （历史脚本同样只对传输层错误重试，不对业务错误码重试）。三次、递增退避，
# 详见 _PagedClient._request 的文档字符串。
FEISHU_RATE_LIMIT_ERROR_CODE = "feishu_code_99991400"
RATE_LIMIT_RETRY_BACKOFFS_SECONDS: tuple[float, ...] = (2.0, 5.0, 10.0)
DEPARTMENT_ID_TYPES = frozenset({"open_department_id", "department_id"})


def department_identifier(entity: Mapping[str, Any]) -> tuple[str, str] | None:
    """从部门实体取出不可拆散的 ``(ID 值, ID 类型)``。

    飞书同一实体可能同时返回两种 ID。沿用已受控验证的选择顺序，优先使用
    ``open_department_id``；关键是把字段名作为类型一起交给下一次下钻调用，
    不能只留下裸值让服务端按默认类型猜测（Issue #16 B3）。
    """

    for id_type in ("open_department_id", "department_id"):
        value = entity.get(id_type)
        if isinstance(value, str) and value:
            return value, id_type
    return None


class FeishuDirectoryError(RuntimeError):
    """飞书调用失败。``code`` 供程序判断，消息里不含任何凭据。

    ``definite`` 表示"飞书明确拒绝"（收到了业务错误码）而非"结果不明确"
    （传输层异常、超时等）。这是协议细节的唯一出口：apps 层只读该属性，
    不解析 ``code`` 的字符串形状（代码框架第二节）。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"飞书目录接口调用失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


@dataclass(frozen=True)
class AuthorizationExchange:
    """授权码换回的最小正式结果。

    短期 ``access_token`` 只用于本次读取 ``user_info``，刻意不放进返回对象；
    调用方因此不能把它顺手交给凭据 vault 或日志。长期只保留用来轮换的
    ``AuthorizationGrant``。
    """

    # 正式重授权只需要把飞书回读的主体与长期凭据交给上层；完整的
    # ``IdentityProfile`` 属于 Bot-Test 开通资产，不能成为正式换码的传递依赖。
    subject_open_id: str
    grant: AuthorizationGrant


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS（接口设计二）：误配 http:// 会把 Bearer token、
    App Secret 与一次性 refresh token 明文上路（Codex 复查发现）。"""

    return _require_https_uri(base_url, "飞书 base_url").rstrip("/")


def _require_https_uri(value: object, label: str) -> str:
    """校验外部 OAuth URL，不把配置原值带进错误消息。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label}必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError(f"{label}不得包含 URL fragment")
    return text


class Transport(Protocol):
    def __call__(self, method: str, url: str, *, body: Mapping[str, Any] | None = ..., token: str | None = ...) -> Any: ...


def urllib_transport(method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None) -> Any:
    """默认传输层：只发 HTTPS，本身不重试。

    有副作用的请求（``POST``）到这一层从未重试过，这条不变。**只读的 ``GET``
    分页请求**上，Issue #271 在更高层的 ``_PagedClient._request`` 加了一道窄而
    有界的重试——只对飞书业务层面的频率限制错误码生效，见该方法文档字符串；
    这层传输函数本身仍然只是"发一次、按原样返回或抛出"，不知道、也不需要知道
    上面有没有重试。"""

    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - 地址来自受控配置
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
    """把飞书业务错误码渲染成审计安全的分类标签（独立审查 2026-08-20 必修 C）。

    ``FeishuDirectoryError.code`` 里，``feishu_code_*`` 这一类是**唯一由外部响应
    数据直接拼出来的字段**：飞书的业务错误码理应是整数，但响应本身不可信——把
    任意 JSON 值原样塞进 ``feishu_code_{value}`` 会让审计（空格分隔的 ``k=v``
    结构化行）被响应内容注入。审查实测：响应给 ``{"code": "0 action=
    org_snapshot_sync.committed run_id=forged tenants=8"}`` 这类值时，旧写法能在
    审计行里拼出一条看起来合法的伪造 ``committed`` 记录。只在 ``value`` 是货真
    价实的 ``int``（排除 ``bool``，因为 ``bool`` 是 ``int`` 子类）时插值，否则
    退化成一个固定标签——"响应的错误码不是预期的整数"本身就是可判断的信息，
    不需要连带把原值一起交出来。
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return f"feishu_code_{value}"
    return "feishu_code_invalid"


_UNSAFE_CODE_FRAGMENT_CHAR = re.compile(r"[^A-Za-z0-9_.:-]")


def _sanitize_code_fragment(value: str, *, max_length: int = 40) -> str:
    """把一段可能来自不可信响应的文本，收窄成能安全拼进 ``FeishuDirectoryError.code``
    的片段——截断长度、并把不在安全字符集里的字符替换成 ``_``。

    这是必修 C（``_safe_feishu_code``）同一个风险的延伸：``unexpected_list_key_*``
    这条错误码（``_pages`` 的空结果收紧，独立审查 2026-08-20 必修 A）把响应里一个
    "不在候选表里"的 JSON 键名原样交出来，方便诊断；但 JSON 键名同样是外部数据、
    同样可能含空格或其他会撑坏空格分隔 ``k=v`` 审计行的字符。只截断长度（`[:40]`）
    只挡住"行被撑得很长"，挡不住"行被注入假字段"——字符集必须一起收窄。
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


class _PagedClient:
    def __init__(
        self,
        *,
        base_url: str,
        transport: Callable[..., Any] | None = None,
        request_pause_seconds: float = REQUEST_PAUSE_SECONDS,
        sleep: Callable[[float], None] | None = None,
        rate_limit_retry_backoffs: tuple[float, ...] = RATE_LIMIT_RETRY_BACKOFFS_SECONDS,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        self._base_url = _require_https(base_url)
        self._transport: Callable[..., Any] = transport or urllib_transport
        # 节流参数与真实 sleep 都可注入（Issue #271）：默认值本身就生效
        # （REQUEST_PAUSE_SECONDS 非零），不靠调用方记得手动打开；测试注入
        # 假 sleeper 或把 request_pause_seconds 设成 0 来跳过真实等待。
        self._request_pause_seconds = request_pause_seconds
        self._sleep: Callable[[float], None] = sleep or time.sleep
        # 频率限制的窄而有界重试也可注入（Issue #271 编排者 2026-08-20 补充压测
        # 证据后追加，见 `_request` 文档字符串）：默认值本身就是生效的三次递增
        # 退避，测试注入更短的序列来避免真实等待，不代表生产默认是关闭的。
        self._rate_limit_retry_backoffs = rate_limit_retry_backoffs

    def _throttle(self) -> None:
        """真实请求前的固定节流（Issue #271）。

        组织快照同步是**递归遍历**：两条身份路径合计要发 550+ 次突发请求。
        回源实测证明这会打穿飞书的频率限制——把撞过限频的那个租户单独拿出来
        跑（不被前面 250 次调用预热），无论有无间隔，40 次调用全部成功；而
        被前面 250 次调用预热过的同一份调用序列在第 251 次收到
        ``feishu_code_99991400``。这坐实了 ``99991400`` 是**累计频率限制**，
        不是权限或数据问题，只能靠放慢请求节奏来大幅降低撞上它的概率。

        已受控验收、真跑通过 8 租户 710 人的历史脚本
        ``scripts/sync_feishu_org_snapshot.py`` 对此的配方是每次请求前固定停
        ``REQUEST_PAUSE_SECONDS``（0.12 秒，约 8 req/s）——这里照抄同一个已经
        验收过的数值，不新造节流算法。编排者 2026-08-20 按 2 倍真实规模
        （564 次调用）实测这个步长本身足以避免撞限：无节流基线约 1.9 req/s
        撞墙，节流后实测约 1.58 req/s 未撞。**但余量并不厚**（仅差约 17%），
        且飞书这个配额的确切机制未回源确认——残余的这一点不确定性不再指望
        单靠节流兜底，见 :meth:`_request` 对频率限制错误码的窄而有界重试
        （节流仍是主手段，重试是节流之外的第二道防线，不是替代）。

        停顿量与真实 sleep 都通过构造参数注入：``request_pause_seconds`` 为
        假值（如 ``0``）时**完全不调用** ``self._sleep``，不是调用
        ``sleep(0)``——单元测试据此既能验证"确实节流"，也能验证"可以关掉"，
        且默认构造出来的客户端就是生效状态，不需要调用方额外接线。
        """

        if self._request_pause_seconds:
            self._sleep(self._request_pause_seconds)

    def _request(self, url: str, *, token: str) -> dict[str, Any]:
        """节流后发起一次 ``GET`` 请求并解码，只对飞书的频率限制业务错误码做
        **窄而有界**的重试（Issue #271 编排者 2026-08-20 补充的真实规模压测
        证据）。

        **为什么加这一道、而不是只把节流步长调大**：0.12 秒节流在 2 倍真实
        规模下已经验证有效（见 :meth:`_throttle`），但余量薄、配额机制未知——
        单纯调大步长只是把同一个"猜"往后挪一个数量级，付出的代价是**每一次
        请求**都变慢，不管当天到底会不会撞限（本职责一整轮就有 550+ 次调用，
        这个代价摊在整条链路上）。而这里的失败代价严重不对称：一次
        ``feishu_code_99991400`` 若原样冒泡，会让 ``read_org_snapshot`` 整轮
        作废、``commit_batch`` 不被调用——已经成功读到的几百次调用全部作废，
        整轮要从头重来（而调用方 ``apps/scheduler/org_snapshot_sync.py`` 现在
        对失败施加 5 分钟起步、封顶 1 小时的退避，因此"重来"还要再等一段）。
        对比之下，一次有界重试最多多花 ``sum(RATE_LIMIT_RETRY_BACKOFFS_SECONDS)``
        （默认 17 秒）。**只对已经过载的这一次请求本身多等几秒，换掉"整轮
        重跑"，代价明显更低**，因此选择重试而不是继续调大节流步长。

        **本段原先写的"专用授权令牌每 UTC 日只能换一次……要等下一次 UTC 日界"
        已经作废**：产品负责人 2026-08-21 就 Issue #276 裁定把那道上界改成
        **最小间隔 5 分钟 + 每 UTC 日 100 次**两道（判据仍然只有凭据文件里的
        ``refresh_consumed_at``/``refresh_consumed_count`` 一份）。令牌寿命约
        2 小时这一条不变。上面的结论方向没变，只是"要等一天"这个后果不再成立。

        **范围收得很窄，不是"失败就重试"**：只有 ``FeishuDirectoryError.code``
        精确等于 ``FEISHU_RATE_LIMIT_ERROR_CODE``（``feishu_code_99991400``）
        才重试；其他业务错误码（权限、参数错误……）、``_pages``/``_pages_multi``
        自己抛出的形状错误（``missing_*``、``invalid_page_item``、
        ``pagination_stalled`` 等，这些发生在本方法返回之后，根本不在这个
        ``try`` 的覆盖范围内）、以及传输层异常（``transport_error`` 等）都
        原样抛出、不重试——那些是真失败，重试只会掩盖或者对一个已经过载的
        序列继续加压（历史脚本同样只对传输层错误重试，不对业务错误码重试）。
        重试次数**有界**（默认 3 次，``RATE_LIMIT_RETRY_BACKOFFS_SECONDS`` 的
        长度），退避递增（默认 2 / 5 / 10 秒）；每次重试前也照常先过一次
        :meth:`_throttle`，不绕开基础节流。次数耗尽仍然失败时，原样重新抛出
        最后一次收到的 :class:`FeishuDirectoryError`——「失败就保留上一份、
        不覆盖基线」的语义一个字不变，本方法只决定"要不要再试一次"，不决定
        "试过之后失败了怎么办"，那仍然是调用方（最终是
        ``apps/scheduler/org_snapshot_sync.py``）的既有职责。

        退避的等待量同样通过 ``self._sleep`` 完成，与 :meth:`_throttle` 共用
        同一个可注入的 sleeper——测试只需要一份假 sleeper 就能同时验证两者，
        不需要真的等待。
        """

        attempt = 0
        while True:
            self._throttle()
            try:
                return _payload(self._transport("GET", url, body=None, token=token))
            except FeishuDirectoryError as error:
                if error.code != FEISHU_RATE_LIMIT_ERROR_CODE or attempt >= len(self._rate_limit_retry_backoffs):
                    raise
                self._sleep(self._rate_limit_retry_backoffs[attempt])
                attempt += 1

    def _pages(self, path: str, *, token: str, query: Mapping[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
        """按候选键名读一份分页列表；**空 ⇒ 空，畸形 ⇒ 抛错**（Issue #268 F2，
        独立审查 2026-08-20 缩窄）。

        真实响应在结果为空时只有 ``has_more``，四个候选键一个都不出现（编排者
        2026-08-19 用应用身份实测过同一端点的这个形状）——此前把这种完全正常的
        空结果当成"响应形状错误"抛出，是本次 stage 首触冒烟每 30 秒失败一轮的
        直接原因之一。但空结果的判定收紧到**唯一场景**：这是**第一页**（还没有
        任何游标、也还没收集到任何数据）、候选键一个都不存在（哪怕值是 ``None``
        也算"存在"）、且 ``has_more`` 是货真价实的 ``False``。

        **独立审查 2026-08-20 推翻了本函数此前更宽松的一版**：那一版只要候选键
        不存在就放行空结果，不看是第一页还是第几页、也不看响应里是否其实有一个
        "长得不像候选键"的列表。审查用同一份假响应实测：真实列表键名一旦不在
        候选表里（例如飞书真用的是 ``associated_tenants`` 而不是这里列的任何一个），
        旧版**静默返回 ``[]``**，故障要等到约 700 次分页请求之后才会在
        ``verify_batch`` 的 ``path_sets_differ`` 里冒出来——诊断成本从 1 次请求
        变成约 700 次，与编排者两次基于间接证据误判根因是**同一个形状**，而
        #268 的第一优先恰恰是消灭这个形状。收紧后的判据：

        - **只在第一页**才可能判定为空——**中途某一页**丢了候选列表键，是"半页"
          不是"空"，必须抛错（下方两条合起来正是 #250 立下、本函数注释曾经点名
          过的"半页不得当成功"纪律，此前的宽松版本反而绕开了它）；
        - 第一页确实没有任何候选键、且 ``has_more`` 严格为 ``False`` 时，若响应里
          存在**另一个**非空列表字段（真实列表键不在候选表里的直接信号），**必须
          抛错并把那个陌生字段名交出来**（``unexpected_list_key_<字段名，经
          :func:`_sanitize_code_fragment` 截断并收窄字符集>``）——这条错误码
          本身就是诊断信息，不是"又一个不透明的 missing_"；确认响应里真的什么
          列表都没有，才是合法空结果；
        - ``has_more`` 为 ``True`` 却没有任何候选列表键：服务端明确说还有数据，
          没有列表放不进"这批是空的"这个解释，仍然抛错；
        - 候选键**存在**但类型不是列表（字符串、``null``、字典……）：飞书返回了
          这个字段就说明它有意义，类型不对是响应形状变了，不是"这批恰好没有"，
          仍然抛错；
        - 列表里出现非对象项、游标停滞：与此前一致，不受本次改动影响。

        **``has_more`` 不是合法 ``bool`` 时刻意不再抛错（独立审查「应修 E」）**：
        本仓库两处证据互相矛盾——已受控验收、真跑通过 8 租户 710 人的历史脚本
        ``scripts/sync_feishu_org_snapshot.py`` 与本函数改动前的既有实现都用
        "不是严格 ``True`` 就当作读完了"（对缺失/非法类型宽容）；而 F2 引用的
        "结果为空时字段缺失"这条说法若成立，`has_more: false` 本身就可能不出现，
        对非法类型一律硬抛会把 stage 的"每 30 秒 ``missing_X``"换成"每 30 秒
        ``has_more_invalid``"——看起来像修了，其实只是换了个错。证据不足，不
        收严：非严格 ``bool`` 只记一条 warning 日志（拿到真实响应形状证据前留痕，
        不失败关闭），分页是否继续仍按"严格等于 ``True`` 才继续"判定，与改动前
        完全一致。

        不采用历史脚本 ``scripts/sync_feishu_org_snapshot.py`` 的
        ``walk_dicts(response.get("data"))`` 递归遍历方案：那样做会把响应里任意
        位置的、恰好长得像目标对象的字典也一并捞进来，換来的宽容是以"来源不可控"
        为代价；这里要的只是别把"没有"误判成"读错了"，以及别把"读错了"误判成
        "没有"。
        """

        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            parameters = {**query, "page_size": PAGE_SIZE}
            if page_token:
                parameters["page_token"] = page_token
            url = f"{self._base_url}{path}?{urlencode(parameters)}"
            data = self._request(url, token=token)
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                # 降级为 warning，不再硬抛（应修 E）：证据不足以确定"非法类型"
                # 与"读完了"哪个解释更符合真实响应，见上方文档字符串。
                logger.warning(
                    "组织快照分页响应的 has_more 不是合法 bool，按既有宽容语义"
                    "处理（不阻塞本轮，Issue #268 应修 E）path=%s type=%s",
                    path,
                    type(has_more).__name__,
                )
            items = None
            for candidate in keys:
                value = data.get(candidate)
                if isinstance(value, list):
                    items = value
                    break
            if items is None:
                if page_token is None and not collected and has_more is False and not any(
                    candidate in data for candidate in keys
                ):
                    # 只在第一页、还没收集到任何数据时，才可能是"空结果"——见上方
                    # 文档字符串「独立审查 2026-08-20 推翻了…」一节。响应里若还有
                    # 一个不在候选表里的非空列表字段，那是真实字段名不在候选表里
                    # 的直接信号，必须抛错并把字段名交出来，不能静默吞掉。
                    stray = next(
                        (key for key, value in data.items() if isinstance(value, list) and value),
                        None,
                    )
                    if stray is not None:
                        # 顶层 JSON 键名不是凭据，但同样不可信：`_sanitize_code_
                        # fragment` 截断长度**并**收窄字符集（必修 C 的延伸，见
                        # 该函数文档字符串），不只是防止响应塞一个超长字符串把
                        # 审计行撑爆，也防止键名本身带空格 / `=` 之类字符注入
                        # 空格分隔的 `k=v` 审计行。
                        raise FeishuDirectoryError(f"unexpected_list_key_{_sanitize_code_fragment(stray)}")
                    return collected
                raise FeishuDirectoryError(f"missing_{keys[0]}")
            for item in items:
                if not isinstance(item, Mapping):
                    # 静默丢弃会让被丢的租户躲过完整性校验、半轮快照被标完成
                    # （终轮 Codex）：任何非对象项都是响应形状错误。
                    raise FeishuDirectoryError("invalid_page_item")
                collected.append(item)
            next_token = data.get("page_token")
            if has_more is not True:
                # 与改动前一致：只有严格的 `True` 才继续翻页；缺失、`False`、
                # 非法类型都当作"读完了"（应修 E，不再对非法类型硬抛）。
                return collected
            # has_more=true 但游标缺失或停滞：服务端明确说还有数据，把已收集的
            # 半截结果当成功返回会让调用方用它替换旧快照（Codex 复查发现）。
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                raise FeishuDirectoryError("pagination_stalled")
            page_token = next_token
        raise FeishuDirectoryError("pagination_limit")

    def _pages_multi(
        self, path: str, *, token: str, query: Mapping[str, Any], list_keys: tuple[str, ...]
    ) -> dict[str, list[dict[str, Any]]]:
        """同一分页游标下同时收集**多个**并列列表（Issue #250：``share_entities``
        单页同时返回 ``share_departments`` 与 ``share_users``，不是 :meth:`_pages`
        假设的单一命名列表）。

        **Issue #270 回源实测推翻了本函数此前的判据**：此前要求每一页 ``list_keys``
        全部存在且是列表，缺一个就硬抛——那句判据（"飞书没有理由只返回其中一个"）
        只是猜测，未经实测。真实响应（应用身份 ``tenant_access_token``，四个租户，
        含根部门与叶子部门）证明猜测是错的：``share_entities`` **只返回非空的那个
        列表**，另一侧为空时该键整个不出现，不是返回 ``[]``。旧判据下
        ``read_org_snapshot`` 遍历到第一个租户、第一次调用就抛 ``missing_
        share_users``，整轮中断，四张 ``feishu_org_*`` 表恒为空——这正是首次开通链
        身份定位 100% 失败的直接原因。新判据（与 :meth:`_pages` 在 Issue #268 F2
        定下的空结果收紧对齐，同一类风险、同一种收紧方式）：

        - **至少有一个** ``list_keys`` 命中（键存在且是列表，哪怕是空列表）⇒
          缺失的其余键按 ``[]`` 处理，正常收集并继续既有分页逻辑——这是实测常态；
        - **全部** ``list_keys`` 都缺失（键完全不在响应里，不是"存在但类型不对"，
          那种情况见下一条）、且这是**第一页**（尚无游标）、且此前尚未收集到任何
          数据、且 ``has_more`` **是 ``False`` 或整个不出现** ⇒ 合法空结果，正常
          返回——与 :meth:`_pages` 同一条"半页不得当成功"纪律：中途某一页全部键
          缺失、或 ``has_more`` 明确是别的值（``True``、字符串、数字……）、或已经
          收集过数据，都不满足这个窄条件，仍然抛错（``missing_{list_keys[0]}``），
          不能把"服务端说还有数据"或"半截数据"误判成"读完了的空结果"。

          **"或整个不出现"是冻结候选审查 2026-08-21 的 F3**：Issue #268 应修 E 记下的
          实测事实是「结果为空时字段整个不出现」——那条事实同时适用于 ``has_more``
          自己。此前这一支硬要求 ``has_more is False``，于是一个"空且连 ``has_more``
          都没有"的 ``share_entities`` 页会抛 ``missing_share_departments``、整轮
          作废，把 #270 好不容易修掉的形状原样退回来。放宽只发生在这个已经被四个
          条件夹住的窄口里：第一页、一无所获、没有任何候选键、且（下一条）响应里
          没有任何陌生的非空列表——此时响应里确实什么都没有，"读完了的空结果"是
          唯一说得通的解释。判据必须用 ``is`` 身份比较（``x is False or x is None``），
          **不能写成** ``x in (False, None)``：``0 == False`` 为真而 ``in`` 用的是
          ``==``，那样一个 ``has_more: 0`` 会误命中。
        - 同样情形下，若响应里存在**不在 ``list_keys`` 内**的非空列表字段 ⇒ 抛
          ``unexpected_list_key_{净化后的键名}``（同 :meth:`_pages`，经
          :func:`_sanitize_code_fragment` 截断并收窄字符集——JSON 键名是外部数据，
          未净化会撑坏空格分隔的审计行，见该函数文档字符串）；确认响应里真的
          什么列表都没有，才是合法空结果。走到这里时 ``list_keys`` 里没有任何
          一个键存在于响应中（上一条已经把"存在"的情形分流出去），因此任何
          命中的列表字段自动就是"不在候选表里"的陌生键，不需要额外排除；
        - 候选键**存在**但类型不是列表（字符串、``null``、字典……）⇒ **维持硬抛**
          ``missing_{key}``，不受"至少一个命中"影响：飞书返回了这个字段就说明它
          有意义，类型不对是响应形状变了，不是"这批恰好没有"；
        - 列表里出现非对象项、游标停滞、分页上界：与此前一致，不受本次改动影响。

        **正常翻页路径上 ``has_more`` 的处理不变**：与 :meth:`_pages` 刻意不同——那里
        对非法类型的 ``has_more`` 降级为 warning（证据不足以确定"非法类型"与"读完
        了"哪个解释更符合真实响应），而这里（下方 ``has_more_invalid``）对非 ``bool``
        的 ``has_more`` 仍然硬抛（Issue #250 F4）。Issue #270 的实测证明**非空**的
        ``share_entities`` 页上 ``has_more`` 是货真价实的 ``bool``，没有证据要求放宽
        那条判据，不顺手统一。上面 F3 放宽的是**另一条路径**：那一支只在"整页什么
        列表都没有"时才会被摸到，而那正是实测说"字段会整个不出现"的那个场景，两者
        不冲突。

        **与 :meth:`_pages` 空结果判据的剩余差异是刻意保留的**：那边仍然硬要求
        ``has_more is False``。它是独立审查 2026-08-20 显式收紧过、并写下理由的判据
        （四个候选键指向**同一个**列表，缺键的解释空间比这里大），改它属于另一条待
        登记的条目，不在本次修复包范围内。
        """

        collected: dict[str, list[dict[str, Any]]] = {key: [] for key in list_keys}
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            parameters = {**query, "page_size": PAGE_SIZE}
            if page_token:
                parameters["page_token"] = page_token
            url = f"{self._base_url}{path}?{urlencode(parameters)}"
            data = self._request(url, token=token)
            hit_keys: list[str] = []
            for key in list_keys:
                if key not in data:
                    continue
                value = data[key]
                if not isinstance(value, list):
                    # 存在但类型不对：飞书返回了这个字段就说明它有意义，不是
                    # "这批恰好没有"，即使别的候选键命中也不能放行（见上方
                    # 文档字符串）。
                    raise FeishuDirectoryError(f"missing_{key}")
                hit_keys.append(key)
            if not hit_keys:
                # 全部候选键都完全不在响应里（上面已经把"存在但类型不对"分流
                # 出去了）。只有第一页、此前一无所获、且 has_more 是 False **或整个
                # 不出现**时，才可能是 Issue #270 实测的合法空结果。
                #
                # **`is False or is None`，不是 `in (False, None)`**（冻结候选审查
                # 2026-08-21 F3）：`0 == False` 在 Python 里为真，`in` 用的是 `==`，
                # 于是一个 `has_more: 0` 会误命中"合法空结果"。身份比较不会。
                empty_page_has_more = data.get("has_more")
                if (
                    page_token is None
                    and not any(collected[key] for key in list_keys)
                    and (empty_page_has_more is False or empty_page_has_more is None)
                ):
                    stray = next(
                        (key for key, value in data.items() if isinstance(value, list) and value),
                        None,
                    )
                    if stray is not None:
                        raise FeishuDirectoryError(f"unexpected_list_key_{_sanitize_code_fragment(stray)}")
                    return collected
                raise FeishuDirectoryError(f"missing_{list_keys[0]}")
            for key in hit_keys:
                for item in data[key]:
                    if not isinstance(item, Mapping):
                        # 静默丢弃会让被丢的租户躲过完整性校验、半轮快照被标完成
                        # （同 :meth:`_pages` 对非对象项的态度）。
                        raise FeishuDirectoryError("invalid_page_item")
                    collected[key].append(item)
            next_token = data.get("page_token")
            has_more = data.get("has_more")
            # `has_more` 必须是货真价实的 bool——缺失、字符串 `"true"`、`1` 等任何
            # 非 bool 形态都不得被当成"读完了"。此前 `is not True` 会把这些异常类型
            # 全部归到"最后一页"分支，让半截数据当成功收场（Issue #250 编排者复查
            # F4）。类型错时立即抛，不静默按空列表或"已完成"处理。
            if not isinstance(has_more, bool):
                raise FeishuDirectoryError("has_more_invalid")
            if has_more is False:
                return collected
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                raise FeishuDirectoryError("pagination_stalled")
            page_token = next_token
        raise FeishuDirectoryError("pagination_limit")


class FeishuDirectoryClient(_PagedClient):
    """读取关联组织的租户、部门与成员，覆盖两条互相独立的身份路径。

    - **专用授权用户身份**（``trust_party/v1/*``）：:meth:`list_collaboration_tenants`
      / :meth:`list_visible_organization` / :meth:`get_member_detail`，Issue #16 起的
      既有实现，是首次开通链在职状态实时回读的唯一来源。
    - **应用身份**（``directory/v1/*``）：:meth:`list_collaboration_tenants_as_app` /
      :meth:`list_share_entities`，Issue #250 新增——组织快照批次完整性校验
      （``core/identity/org_snapshot.verify_batch``）要求同一租户被两条路径分别看到的
      成员集合彼此相等，缺其中一条就没有交叉校验的另一半输入，2026-07-28 的实况问题
      （8 个关联租户里 2 个返回 0 条成员，Issue #16）正是只有一条路径时看不出来的那种
      错误。两条路径的请求形状取自编排者 2026-08-19 用应用身份完成的真实探测（`directory/
      v1/collaboration_tenants`、`directory/v1/share_entities` 各自的参数与响应字段）。

    刻意**没有**目标租户构造参数：标识跨租户唯一，租户归属是查出来的结果
    （Issue #16 硬约束 1）。租户键一律作为调用参数传入。
    """

    def list_collaboration_tenants(self, *, token: str) -> list[dict[str, Any]]:
        # 字段链取自 2026-07-29 真实探针（scripts/verify_feishu_association.sh）：
        # 实测主字段是 target_tenant_list；此前写的 collaboration_tenant_list 在真实
        # 响应里不存在，会让每次成功响应都被判失败（Codex 复查发现）。
        #
        # 这条用户身份路径**没有**目标租户可传，返回"这个用户身份当前看不到任何
        # 关联组织"完全合法（`_pages` 已按 Issue #268 F2 把这种空结果与真正的响应
        # 形状错误分开）；调用方不得据此断言"该主体一定看不到关联组织"——那需要
        # 独立证据，不是本方法的隐含语义（Issue #268 更正评论撤回的正是这条跳跃）。
        return self._pages(
            "/trust_party/v1/collaboration_tenants",
            token=token,
            query={},
            keys=("target_tenant_list", "items", "collaboration_tenants", "tenants"),
        )

    def list_visible_organization(
        self,
        *,
        token: str,
        tenant_key: str,
        department_id: str | None = None,
        department_id_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回 (部门实体, 成员实体)。飞书把两者混在同一个列表里，这里拆开。"""

        query: dict[str, Any] = {}
        if department_id is not None or department_id_type is not None:
            if (
                not isinstance(department_id, str)
                or not department_id
                or department_id_type not in DEPARTMENT_ID_TYPES
            ):
                # 不回显 ID：调用方误把令牌接到这里时，配置错误也不能泄露原值。
                raise ValueError("部门 ID 值与类型必须成对，类型只能是 open_department_id 或 department_id")
            query["target_department_id"] = department_id
            query["department_id_type"] = department_id_type
        entities = self._pages(
            f"/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}/visible_organization",
            token=token,
            query=query,
            keys=("collaboration_entity_list", "entities", "items"),
        )
        departments = [item for item in entities if item.get("open_department_id") or item.get("department_id")]
        members = [item for item in entities if not (item.get("open_department_id") or item.get("department_id"))]
        return departments, members

    def list_collaboration_tenants_as_app(self, *, token: str) -> list[dict[str, Any]]:
        """以应用身份读取关联租户列表（``directory/v1/collaboration_tenants``）。

        Issue #250 编排者 2026-08-19 用应用身份实测：``page_size=50`` 返回 ``code=0``、
        8 个关联租户。响应顶层列表字段名未在探测记录里逐字确认，这里沿用
        :meth:`list_collaboration_tenants` 已验证过的多字段回退顺序并补上
        ``tenant_list``——两个端点同属"关联租户列表"这一类响应，命名规律最可能相同；
        真正的字段名由 L4a 受控真实同步核实，回退列表里任何一个命中都按同一套逻辑处理，
        不影响本侧的分页与校验语义。
        """

        return self._pages(
            "/directory/v1/collaboration_tenants",
            token=token,
            query={},
            keys=("tenant_list", "items", "collaboration_tenants", "tenants", "target_tenant_list"),
        )

    def list_share_entities(
        self, *, token: str, tenant_key: str, department_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """以应用身份读取一层共享范围（``directory/v1/share_entities``）。

        返回 ``(部门实体, 成员实体)``，对应响应里各自独立的 ``share_departments`` /
        ``share_users`` 列表——与 :meth:`list_visible_organization` 混在一个列表里不同，
        这个端点原生就是分开的两个列表（Issue #250 编排者 2026-08-19 实测参数与响应
        字段：``target_tenant_key``、``is_select_subject=false``、``target_department_id``；
        根部门传 ``"0"``）。

        **递归遍历（BFS 逐级下钻子部门）与伪根记录过滤不在这里做**：这里只负责单层
        分页，与 :meth:`list_visible_organization` 单层职责对称；递归属于"怎么把多次
        单层调用串成一整棵树"，是编排逻辑而不是协议细节，交给
        :mod:`lingxi.adapters.feishu_org_snapshot_reader`。
        """

        if not isinstance(department_id, str) or not department_id:
            raise ValueError("target_department_id 不能为空；根部门请传 '0'")
        query: dict[str, Any] = {
            "target_tenant_key": tenant_key,
            "is_select_subject": "false",
            "target_department_id": department_id,
        }
        collected = self._pages_multi(
            "/directory/v1/share_entities",
            token=token,
            query=query,
            list_keys=("share_departments", "share_users"),
        )
        return collected["share_departments"], collected["share_users"]

    def get_member_detail(self, *, token: str, tenant_key: str, member_id: str, id_type: str = "open_id") -> dict[str, Any]:
        """读取成员详情。**在职状态只能从这里实时取，不从快照取。**"""

        url = (
            f"{self._base_url}/trust_party/v1/collaboration_tenants/{quote(tenant_key, safe='')}"
            f"/collaboration_users/{quote(member_id, safe='')}?{urlencode({'target_user_id_type': id_type})}"
        )
        data = self._request(url, token=token)
        detail = data.get("target_user")
        if not isinstance(detail, Mapping):
            raise FeishuDirectoryError("member_detail_missing")
        return dict(detail)


class FeishuAuthorizationClient:
    """专用授权凭据的换码与续期调用。

    两条调用都在本适配器内完成协议细节；上层只得到正式领域对象，不会接触
    飞书响应字典或短期 ``access_token``。
    """

    def __init__(self, *, base_url: str, app_id: str, app_secret: str, transport: Callable[..., Any] | None = None) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url 必须由配置注入，不得写死在代码里")
        if not app_id or not app_secret:
            raise ValueError("缺少专用授权应用凭据")
        self._base_url = _require_https(base_url)
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport: Callable[..., Any] = transport or urllib_transport

    def exchange_authorization_code(
        self,
        code: str,
        *,
        redirect_uri: str,
        required_scope: str,
    ) -> AuthorizationExchange:
        """用一次性授权码取得身份和可轮换凭据。

        ``code`` 与短期访问令牌只在本次方法调用的内存里存在；只返回身份资料
        和 ``AuthorizationGrant``。没有明确的 ``offline_access`` 或完整的
        refresh 凭据时失败关闭，调用方不能把一次性的半成品交给 vault。
        """

        if not isinstance(code, str) or not code.strip():
            raise ValueError("授权码不能为空")
        redirect_uri = _require_https_uri(redirect_uri, "授权回跳地址")
        response = self._transport(
            "POST",
            f"{self._base_url}/authen/v2/oauth/token",
            body={
                "grant_type": "authorization_code",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            token=None,
        )
        data = response if isinstance(response, Mapping) else {}
        if data.get("code") not in (None, 0, "0"):
            raise FeishuDirectoryError(_safe_feishu_code(data.get("code")))
        access_token_value = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("refresh_token_expires_in")
        scope = data.get("scope")
        if not isinstance(access_token_value, str) or not access_token_value:
            raise FeishuDirectoryError("access_token_missing")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise FeishuDirectoryError("refresh_token_missing")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise FeishuDirectoryError("refresh_token_lifetime_missing")
        if not isinstance(scope, str) or not scope_covers(required_scope, scope):
            raise FeishuDirectoryError("scope_incomplete")

        access_token = SecretToken(access_token_value)
        info = self._transport(
            "GET",
            f"{self._base_url}/authen/v1/user_info",
            body=None,
            token=access_token.reveal(),
        )
        if not isinstance(info, Mapping) or info.get("code") not in (None, 0, "0"):
            code_value = info.get("code") if isinstance(info, Mapping) else "invalid_response"
            raise FeishuDirectoryError(_safe_feishu_code(code_value))
        raw_profile = info.get("data", info)
        if not isinstance(raw_profile, Mapping):
            raise FeishuDirectoryError("identity_profile_invalid")

        subject_open_id = _text_value(raw_profile.get("open_id"))
        if not subject_open_id:
            raise FeishuDirectoryError("identity_open_id_missing")
        return AuthorizationExchange(
            subject_open_id=subject_open_id,
            grant=AuthorizationGrant(SecretToken(refresh_token), expires_in, scope),
        )

    def refresh(self, current: AuthorizationGrant) -> tuple[AuthorizationGrant, DerivedAccessToken]:
        """用当前 ``refresh_token`` 换一组新凭据。

        返回 ``(新的长期凭据, 派生短期令牌)``。**长期凭据**的任何字段缺失、类型不符或
        有效期非正都抛 :class:`FeishuDirectoryError`——调用方据此按
        :func:`lingxi.core.identity.credentials.decide_after_refresh` 撤销，
        **绝不重放旧凭据**。

        短期令牌的寿命（``expires_in``）自 Issue #215 起被记下来交给调用方：日报改成
        按日取一次令牌之后，"缓存的这份还能不能用"没有别的判据。但它**缺失时不判整轮
        续期失败**，只是让派生令牌的寿命成为"未知"：续期成功这件事已经发生，新的一次性
        ``refresh_token`` 必须照常落盘，为一个附带字段把凭据判死会让"需人工重新授权"
        因为一个非凭据原因而发生，方向是反的。寿命未知的后果留在令牌供给那一侧
        （持有者拒绝缓存 → 供给按分类失败关闭），不会伪装成别的东西。
        """

        response = self._transport(
            "POST",
            f"{self._base_url}/authen/v2/oauth/token",
            body={
                "grant_type": "refresh_token",
                "client_id": self._app_id,
                "client_secret": self._app_secret,
                "refresh_token": current.refresh_token.reveal(),
            },
            token=None,
        )
        data = response if isinstance(response, Mapping) else {}
        code = data.get("code")
        if code not in (None, 0, "0"):
            raise FeishuDirectoryError(_safe_feishu_code(code))
        access_token = data.get("access_token")
        next_token = data.get("refresh_token")
        expires_in = data.get("refresh_token_expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuDirectoryError("access_token_missing")
        if not isinstance(next_token, str) or not next_token:
            raise FeishuDirectoryError("refresh_token_missing")
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise FeishuDirectoryError("refresh_token_lifetime_missing")
        scope = data.get("scope")
        access_token_lifetime = data.get("expires_in")
        if (
            not isinstance(access_token_lifetime, int)
            or isinstance(access_token_lifetime, bool)
            or access_token_lifetime <= 0
        ):
            access_token_lifetime = None
        logger.info(
            "专用授权续期成功 lifetime_seconds=%s access_token_lifetime_seconds=%s",
            expires_in,
            access_token_lifetime,
        )
        return (
            AuthorizationGrant(SecretToken(next_token), expires_in, scope if isinstance(scope, str) else ""),
            DerivedAccessToken(SecretToken(access_token), access_token_lifetime),
        )


class FeishuEmploymentReader:
    """首次开通链上的**实时**在职状态读取（Epic D / S-D-02）。

    组织快照刻意不保存在职状态（会立刻产生"库里在职、飞书已冻结"的陈旧窗口，而这个字段
    的唯一用途恰恰是拦截这种情况，见 ``core/identity/first_contact`` 与 `V-开通-07`），
    因此每次开通判定都要现读一次成员详情。

    **令牌供给是注入的可调用对象，不是构造参数**：这条读取用的是专用授权主体派生出来的
    短期令牌，而那条 ``refresh_token`` 是**一次性**的、全系统只允许一个消费者
    （2026-08-08 授权码被烧的事故形状）。把"怎么拿到令牌"留成注入口，是为了让"谁是那个
    唯一消费者"始终是一次显式的装配决定，而不是某个类的默认行为。

    读不到、读不懂都**不返回 ``False``**：``EmploymentStatus.from_feishu`` 对缺字段返回
    ``None``（判定层按"不可判定"拒绝开通），传输失败原样上抛（编排层归成本侧故障）。
    默认成在职是这条链上最危险的错误——它会给一个已冻结的账号开通并发布权限。
    """

    def __init__(
        self,
        *,
        client: "FeishuDirectoryClient",
        access_token: Callable[[], str],
    ) -> None:
        if not callable(access_token):
            raise TypeError("在职状态读取必须注入令牌供给")
        self._client = client
        self._access_token = access_token

    def status(self, *, tenant_key: str, open_id: str) -> Any:
        from lingxi.core.identity.first_contact import EmploymentStatus

        detail = self._client.get_member_detail(
            token=self._access_token(), tenant_key=tenant_key, member_id=open_id
        )
        return EmploymentStatus.from_feishu(detail.get("status"))


def _text_value(value: object) -> str:
    """只接受飞书身份字段的字符串形态，不把对象错误转成可识别文本。"""

    return value.strip() if isinstance(value, str) else ""


def scope_covers(requested_scope: str, returned_scope: str) -> bool:
    """判断飞书返回的 scope 是否覆盖本次配置请求的全部 scope。"""

    if not isinstance(requested_scope, str) or not isinstance(returned_scope, str):
        return False
    requested = frozenset(requested_scope.split())
    returned = frozenset(returned_scope.split())
    return bool(requested) and requested.issubset(returned)
