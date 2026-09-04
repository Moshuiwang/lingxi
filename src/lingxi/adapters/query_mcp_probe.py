"""问数 MCP 的就绪探针：以目标用户身份执行一次 ``list_metrics``。

实现 :class:`lingxi.core.permission.mcp_readiness.McpProbe`。判定（就绪/
同步中/技术失败怎么分）在 :mod:`lingxi.core.permission.mcp_readiness`，
本模块只负责协议细节与错误翻译。真实 MCP 协议面大半未实测，
``list_metrics`` 返回形状已实测到字段级（详见
:func:`content_text_metrics_reader`）；默认 reader 保持不变、只认历史
形状，已验证 reader 由装配层注入替换；"权限还没同步"目前只确认会以
HTTP 401 出现，分类只按状态码走。所有未知形态一律落"技术失败"，绝不落
"就绪"——本模块唯一不肯让步的地方，
同一条纪律的三个落点：响应必须自证是这次请求的答复（校验 jsonrpc 版本与
id）；传输层不跟随重定向；永远不数 content 的块数。令牌只走请求头、一次
都不进 core：明文由 ``token_provider`` 现取现用，用完即随栈帧消失；
``token_provider`` 自己失败时翻译成技术失败，原始异常不进 traceback 链。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from lingxi.core.ids import new_ulid
from lingxi.core.permission.mcp_readiness import McpProbeError

logger = logging.getLogger(__name__)

#: 就绪探针调用的工具名。合同登记的能力（接口设计 5.2「``list_metrics``：原系统已具备」）。
DEFAULT_TOOL_NAME = "list_metrics"

#: 单次探针的传输超时。远小于轮询间隔（180 秒）：一次挂死的调用不该吃掉整个预算，
#: 它应当尽快落成一次技术失败，让下一轮按节奏继续。
REQUEST_TIMEOUT_SECONDS = 20

#: "明确拒绝"的 HTTP 状态：鉴权与授权。它们表示 MCP **看见了我们的请求并拒绝**，
#: 在就绪语境里等于"它还没拉到我们发布的那一行"——是同步中，不是技术失败。
DEFAULT_DENIED_STATUS_CODES: tuple[int, ...] = (401, 403)

#: "明确拒绝"的 JSON-RPC 错误码。**默认为空**：真实服务端用哪些码表示鉴权失败未经实测，
#: 而猜错的方向不能是"把技术失败当成同步中"。留空时这类错误落技术失败——一样不会被读成
#: 就绪，只是运维看到的分类更保守。实测之后由装配层注入具体取值。
DEFAULT_DENIED_ERROR_CODES: tuple[int, ...] = ()

#: JSON-RPC 版本。响应必须逐字带回它，否则我们读的可能根本不是一个 JSON-RPC 响应。
JSONRPC_VERSION = "2.0"

#: 默认 reader **唯一**认得的结果形状：``result.structuredContent.metrics`` 是一个列表。
#: 收得这么窄是刻意的，见 :func:`default_metrics_reader`。
STRUCTURED_CONTENT_KEY = "structuredContent"
METRIC_LIST_KEY = "metrics"

#: :func:`content_text_metrics_reader` 认得的结果形状里，文本块所在的键与块类型。
#: 真实响应实测样本见模块文档「诚实边界」一节与
#: ``docs/参考证据/问数MCP-list_metrics真实响应形状.md``。
CONTENT_KEY = "content"
CONTENT_TEXT_TYPE = "text"

#: 每条指标记录必须**恰好**具备的字段集合，全部是字符串（实测钉死到这个细度）。
#: 缺字段或类型不对 → ``unrecognized_result_shape``。
METRIC_RECORD_FIELDS: tuple[str, ...] = ("metric_id", "name", "name_en")


class McpHttpResponse(NamedTuple):
    """一次 MCP 调用的原始响应：**状态码与解析后的载荷都要**。

    只返回载荷是不够的——"权限还没同步"很可能表现为 HTTP 401/403，而那时响应体未必是
    合法 JSON。丢掉状态码等于把一次可分辨的拒绝压成"响应形状不对"。
    """

    status: int
    payload: Any


def _require_https(endpoint: object) -> str:
    """出站必须 HTTPS：误配 ``http://`` 会把 Bearer 令牌明文上路。不回显收到的值。"""
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError("问数 MCP 端点必须以 https:// 开头（不回显收到的值）")
    return endpoint.rstrip("/")


def _no_redirect_opener() -> Any:
    """构造一个**不跟随重定向**的 opener。

    这条不是洁癖：``urllib`` 默认会自动跟随 3xx，并且**把 ``Authorization`` 请求头一起
    转发到新地址**。于是一个被劫持或误配的 ``302`` 就能把用户的 Bearer 令牌送到另一个
    主机、甚至从 https 降级到 http 明文——而调用方什么都看不到，只会看到一次"成功"。
    ``_require_https`` 只管得住我们**主动**填的那个地址，管不住服务端让我们再去哪里。

    做法是让 ``redirect_request`` 返回 ``None``：``urllib`` 于是把 3xx 当成
    :class:`HTTPError` 抛出，我们在下面按普通非 200 状态处理，落技术失败。
    """
    from urllib.request import HTTPRedirectHandler, build_opener

    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # 见外层文档
            return None

    return build_opener(_NoRedirect())


def urllib_mcp_transport(
    method: str,
    url: str,
    *,
    body: Mapping[str, Any] | None = None,
    token: str | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> McpHttpResponse:
    """默认传输：只发 HTTPS、**不跟随重定向**、不重试。令牌只进 ``Authorization`` 头。

    HTTP 错误**不抛异常**：状态码本身是分类依据（401/403 = 明确拒绝），把它折成一个
    通用的传输异常会让"被拒绝"和"网断了"变成同一件事。响应体解析失败时载荷给 ``None``，
    交由上层按状态码决定它是明确拒绝还是形状错误。3xx 因此也以状态码的形式回到上层，
    落进"既不是 200 也不在拒绝白名单里"那一路——技术失败。
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request

    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with _no_redirect_opener().open(request, timeout=timeout) as response:  # 地址来自受控配置
            return McpHttpResponse(int(response.status), _load_json(response.read()))
    except HTTPError as error:
        return McpHttpResponse(int(error.code), _load_json(_read_quietly(error)))
    except (URLError, OSError, TimeoutError):
        # 不接 traceback 链：底层异常的 ``str`` 里可能带着完整 URL 与请求头摘要。
        raise McpProbeError("transport_error", denied=False) from None


def _read_quietly(error: Any) -> bytes:
    try:
        return error.read()
    except Exception:  # 读不到就当空响应，状态码已经够分类了
        return b""


def _load_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _reject_non_finite_constant(constant: str) -> Any:
    """M3：``json.loads`` 默认把 ``NaN``/``Infinity``/``-Infinity`` 当合法值——那是 JSON 标准之外的 Python 扩展，真实合同里没有这种取值的位置。拒绝比容忍更安全。"""
    raise ValueError(f"json 载荷含非标准常量: {constant}")


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """M3：重复键在 JSON 标准下未定义行为，``json`` 模块默认静默取最后一个值——等于让"后一个同名键覆盖前一个"无声通过。逐字失败更安全。"""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"json 对象含重复键: {key!r}")
        seen.add(key)
    return dict(pairs)


def _parse_json_strictly(text: str) -> Any:
    """三个隐藏口子统一在这里收口：``RecursionError``（极深嵌套触发，不是 ``ValueError`` 子类，若只捕获后者会穿透逃到轮询循环变成未归类异常）、``NaN``/``Infinity``/``-Infinity`` 等非标准常量、重复键。

    三者统一折成 :class:`McpProbeError` 的 ``unrecognized_result_shape``，与本模块其余
    未知形状同一个错误码、同一个方向：宁可说"读不懂"，不能说"这个人可以
    用了"。不设响应尺寸上限：真实问题是读不懂就说读不懂，不是太大就拒绝。
    """
    try:
        return json.loads(
            text,
            parse_constant=_reject_non_finite_constant,
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except (ValueError, RecursionError):
        raise McpProbeError("unrecognized_result_shape", denied=False) from None


def default_metrics_reader(result: Mapping[str, Any]) -> int:
    """默认的指标计数：只认一种形状——``result.structuredContent.metrics`` 是一个列表，其余一切（缺键、类型不对、列表挂在别的键上）一律抛 ``unrecognized_result_shape``。

    窄到这个程度是刻意的：假就绪是唯一不可
    犯的方向，依次尝试多个候选键会让一份与指标毫无关系的响应也数出条数、
    判成就绪；真实形状未实测时，猜多种形状不比猜一种更接近真相，只是把
    猜错的后果从响亮失败换成静默假成功。永远不数 ``content`` 的块数：那
    通常是一个文本块，数块数会让"没有任何指标"的空回答也被判成就绪。
    """
    structured = result.get(STRUCTURED_CONTENT_KEY)
    if isinstance(structured, Mapping):
        metrics = structured.get(METRIC_LIST_KEY)
        if isinstance(metrics, list):
            return len(metrics)
    raise McpProbeError("unrecognized_result_shape", denied=False)


def _require_valid_metric_record(record: Any) -> None:
    """按实测样本校验一条指标记录——``metric_id``/``name``/``name_en``（:data:`METRIC_RECORD_FIELDS`）三个字段都必须存在且是字符串。缺字段或类型不对，一律 ``unrecognized_result_shape``：服务端契约若变了应当被响亮地发现，而不是被静默容忍。"""
    if not isinstance(record, Mapping):
        raise McpProbeError("unrecognized_result_shape", denied=False)
    for field in METRIC_RECORD_FIELDS:
        if not isinstance(record.get(field), str):
            raise McpProbeError("unrecognized_result_shape", denied=False)


def content_text_metrics_reader(result: Mapping[str, Any]) -> int:
    """已验证的指标计数：读 ``result.content`` 中唯一一个块，要求 ``type == "text"``，把 ``text`` 解析成 JSON，要求顶层恰好是 ``{"metrics": [...]}``，对列表每条记录按实测字段集合校验后返回条数。

    真实响应样例见 ``docs/参考证据/问数MCP-list_metrics真实响应形状.md``；
    ``default_metrics_reader`` 在真实 MCP 上读不懂该形状，本函数由装配层
    显式注入替换默认值。假定调用方已经过 ``isError`` 门，单独复用时须
    自己先判。只认 ``metrics`` 键、不数块数、要求恰好一个块、内层字段
    逐字匹配，任何偏离一律 ``unrecognized_result_shape``。
    """
    content = result.get(CONTENT_KEY)
    if not isinstance(content, list) or len(content) != 1:
        raise McpProbeError("unrecognized_result_shape", denied=False)
    block = content[0]
    if not isinstance(block, Mapping) or block.get("type") != CONTENT_TEXT_TYPE:
        raise McpProbeError("unrecognized_result_shape", denied=False)
    text = block.get("text")
    if not isinstance(text, str):
        raise McpProbeError("unrecognized_result_shape", denied=False)
    parsed = _parse_json_strictly(text)
    if not isinstance(parsed, Mapping) or set(parsed.keys()) != {METRIC_LIST_KEY}:
        raise McpProbeError("unrecognized_result_shape", denied=False)
    metrics = parsed[METRIC_LIST_KEY]
    if not isinstance(metrics, list):
        raise McpProbeError("unrecognized_result_shape", denied=False)
    for record in metrics:
        _require_valid_metric_record(record)
    return len(metrics)


def content_text_metric_ids_reader(result: Mapping[str, Any]) -> frozenset[str]:
    """``list_metrics`` 的 ``metric_id`` 集合读取（每日「MCP 指标目录 vs 映射表覆盖面」日检用）。

    与 :func:`content_text_metrics_reader` 出自同一份已实测响应形状，校验
    规则逐条相同，唯一差异是返回值：那个函数只数条数，这个函数要拿到
    逐个 ``metric_id``。刻意不改造那个函数去复用——它在就绪确认的生产
    关键路径上，改动都要重过一遍验收面；但底层严格 JSON 解析与单条记录
    校验两个小工具照旧复用。未知形状一律抛错，绝不返回可能不完整的集合
    ——半份目录会让日检漏报真实存在的未覆盖指标。
    """
    content = result.get(CONTENT_KEY)
    if not isinstance(content, list) or len(content) != 1:
        raise McpProbeError("unrecognized_result_shape", denied=False)
    block = content[0]
    if not isinstance(block, Mapping) or block.get("type") != CONTENT_TEXT_TYPE:
        raise McpProbeError("unrecognized_result_shape", denied=False)
    text = block.get("text")
    if not isinstance(text, str):
        raise McpProbeError("unrecognized_result_shape", denied=False)
    parsed = _parse_json_strictly(text)
    if not isinstance(parsed, Mapping) or set(parsed.keys()) != {METRIC_LIST_KEY}:
        raise McpProbeError("unrecognized_result_shape", denied=False)
    metrics = parsed[METRIC_LIST_KEY]
    if not isinstance(metrics, list):
        raise McpProbeError("unrecognized_result_shape", denied=False)
    metric_ids: list[str] = []
    for record in metrics:
        _require_valid_metric_record(record)
        metric_ids.append(record["metric_id"])
    return frozenset(metric_ids)


def fetch_metric_catalog(
    *,
    endpoint: str,
    token: str,
    transport: Callable[..., McpHttpResponse] | None = None,
    tool_name: str = DEFAULT_TOOL_NAME,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
) -> frozenset[str]:
    """执行一次 ``list_metrics``，返回响应里出现的全部 ``metric_id`` 集合。

    与 :class:`QueryMcpProbe` 独立：那个类回答权限是否同步好了（生产
    关键路径），本函数只回答"MCP 现在报告哪些指标 ID"（日检可见性）；
    两者复用同一套传输层，但刻意不合并成一个类，避免日检失败影响就绪
    判定。两类失败分两种异常：传参不合法用 ``ValueError`` 快速失败，
    响应读不懂用 :class:`McpProbeError`。不把任何失败当零个指标的安全
    默认值——那是比响亮失败更危险的漏报。令牌只进请求头、只在调用栈内
    存活。
    """
    if not isinstance(token, str) or not token:
        raise ValueError("查询指标目录必须提供令牌")
    request_id = new_ulid()
    request = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {}},
    }
    active_transport = transport or urllib_mcp_transport
    response = active_transport(
        "POST", _require_https(endpoint), body=request, token=token, timeout=timeout_seconds
    )
    del token  # 明文不再需要，尽早从本帧移除。
    if not isinstance(response, McpHttpResponse):
        raise McpProbeError("invalid_transport_result", denied=False)
    if response.status != 200:
        # 与就绪探针不同，本函数不区分「明确拒绝」与「技术失败」（模块文档「两类
        # 失败分两种异常类型」）：日检只需要知道"这一轮读不到"，不需要区分原因。
        raise McpProbeError(f"http_{response.status}", denied=False)
    payload = response.payload
    if not isinstance(payload, Mapping):
        raise McpProbeError("invalid_response_shape", denied=False)
    if payload.get("jsonrpc") != JSONRPC_VERSION or payload.get("id") != request_id:
        raise McpProbeError("invalid_jsonrpc_response", denied=False)
    if payload.get("error") is not None:
        raise McpProbeError("jsonrpc_error", denied=False)
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise McpProbeError("invalid_result_shape", denied=False)
    if result.get("isError") is True:
        raise McpProbeError("tool_error", denied=False)
    return content_text_metric_ids_reader(result)


class QueryMcpProbe:
    """问数 MCP 的就绪探针。构造只存参数，不做任何 I/O、不读凭据。"""

    def __init__(
        self,
        *,
        endpoint: str,
        token_provider: Callable[[str], str | None],
        transport: Callable[..., McpHttpResponse] | None = None,
        tool_name: str = DEFAULT_TOOL_NAME,
        metrics_reader: Callable[[Mapping[str, Any]], int] | None = None,
        denied_status_codes: tuple[int, ...] = DEFAULT_DENIED_STATUS_CODES,
        denied_error_codes: tuple[int, ...] = DEFAULT_DENIED_ERROR_CODES,
        tool_error_is_denied: bool = False,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """校验并接入探针端点、令牌供给、传输层与拒绝判定规则。"""
        self._endpoint = _require_https(endpoint)
        if not callable(token_provider):
            raise ValueError("token_provider 必须是按 user_id 返回明文令牌的可调用对象")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("探针工具名不得为空")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 1
        ):
            raise ValueError("探针传输超时必须是正整数秒")
        self._token_provider = token_provider
        self._transport: Callable[..., McpHttpResponse] = transport or urllib_mcp_transport
        self._tool_name = tool_name.strip()
        self._metrics_reader = metrics_reader or default_metrics_reader
        self._denied_status_codes = tuple(denied_status_codes)
        self._denied_error_codes = tuple(denied_error_codes)
        self._tool_error_is_denied = bool(tool_error_is_denied)
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> int:
        """单次传输超时。装配层必须让它与 :attr:`lingxi.core.permission.mcp_readiness.ReadinessSchedule.probe_timeout_seconds` 一致——那边算出来的"结论最晚什么时候落地"就是拿这个数算的。"""
        return self._timeout_seconds

    @property
    def metrics_reader(self) -> Callable[[Mapping[str, Any]], int]:
        """当前生效的指标计数函数。装配层与测试用它自证接线：没有它，"装配层是不是真的注入了已验证的 reader"就只能靠读代码相信，读不出来。"""
        return self._metrics_reader

    def list_metrics(self, *, user_id: str) -> int:
        """以该用户身份执行一次 ``list_metrics``，返回可见指标条数。

        失败一律抛 :class:`~lingxi.core.permission.mcp_readiness.McpProbeError`，
        ``denied`` 标明是"MCP 明确拒绝"（同步中）还是"结果不明"（技术失败）。
        """
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("就绪探针必须指明用户")
        token = self._token(user_id)
        # 请求标识用内部 ULID：不含用户资料，也不需要与任何外部标识对齐；每次现生成，
        # 因此可以用它证明"这份响应是这次请求的答复"。
        request_id = new_ulid()
        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": "tools/call",
            "params": {"name": self._tool_name, "arguments": {}},
        }
        response = self._transport(
            "POST", self._endpoint, body=request, token=token, timeout=self._timeout_seconds
        )
        del token  # 明文不再需要，尽早从本帧移除。
        return self._read(response, request_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _token(self, user_id: str) -> str:
        try:
            token = self._token_provider(user_id)
        except Exception as error:  # 失败形态由注入的 provider 决定
            # 只记类型，不记消息也不接 traceback 链：provider 的异常可能带着连接串或
            # 响应片段。这一路落技术失败——可分辨、被独立记录，且绝不会被读成就绪。
            logger.warning("就绪探针取令牌失败 user=%s error=%s", user_id, type(error).__name__)
            raise McpProbeError("token_unavailable", denied=False) from None
        if not isinstance(token, str) or not token:
            # 这个人还没有签发过令牌。不是"没权限"（那是数据库侧的判定），
            # 而是我们这一侧还没准备好——技术失败。
            raise McpProbeError("token_missing", denied=False)
        return token

    def _read(self, response: object, request_id: str) -> int:
        if not isinstance(response, McpHttpResponse):
            raise McpProbeError("invalid_transport_result", denied=False)
        status, payload = response.status, response.payload
        if status in self._denied_status_codes:
            # MCP 看见了请求并明确拒绝：它还没拉到我们发布的那一行 → 同步中。
            raise McpProbeError(f"http_{status}", denied=True)
        if status != 200:
            # 3xx 也走这里：传输层不跟随重定向，一次重定向就是一次技术失败
            # （跟随会把 Bearer 令牌转发到新地址，见 :func:`_no_redirect_opener`）。
            raise McpProbeError(f"http_{status}", denied=False)
        if not isinstance(payload, Mapping):
            raise McpProbeError("invalid_response_shape", denied=False)
        # **先证明这是"这次请求的 JSON-RPC 答复"，再谈内容。** 少了这两道，任何一份恰好
        # 长得像结果的 JSON——上一次请求的迟到答复、代理返回的缓存页、网关的健康检查
        # 响应——都能被读成一次成功的探针，而假就绪是五路里唯一不可犯的方向。
        if payload.get("jsonrpc") != JSONRPC_VERSION:
            raise McpProbeError("invalid_jsonrpc_version", denied=False)
        if payload.get("id") != request_id:
            raise McpProbeError("response_id_mismatch", denied=False)
        error = payload.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, Mapping) else None
            if isinstance(code, bool) or not isinstance(code, int):
                # **错误码必须是 JSON-RPC 规定的整数**，否则我们连"这是什么错"都不知道。
                # 不把它拼进自己的错误码：那等于让一个**外部可控的字符串**一路进到
                # ``mcp_sync_check.error_code``（落库、进审计），而且超过 200 字符时还会
                # 撞上那一列的 CHECK，把一次本该记下来的失败变成整轮确认中断。
                raise McpProbeError("invalid_result_shape", denied=False)
            denied = code in self._denied_error_codes
            # **错误消息一个字都不进我们的错误码**：它是服务端文本，可能回显请求内容。
            raise McpProbeError(f"jsonrpc_{code}", denied=denied)
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise McpProbeError("invalid_result_shape", denied=False)
        # **按键是否存在判断**，不按取值是不是 ``None``：显式的 ``"isError": null`` 与
        # "根本没有这个键"在 ``result.get()`` 下无法区分，于是一份自称出错、却又带着
        # 非空 metrics 的响应会被一路读成就绪。键在就必须是 bool。
        if "isError" in result:
            flag = result["isError"]
            if not isinstance(flag, bool):
                # **类型不对就当读不懂**，而不是"不是 True 那就当没错"。``1`` / ``"true"``
                # / ``null`` 这类取值在 ``is True`` 下都判否——正好违反"未知形状一律
                # 技术失败"。
                raise McpProbeError("invalid_result_shape", denied=False)
        else:
            flag = False
        if flag is True:
            # **默认按技术失败**（``tool_error_is_denied=False``）。``isError`` 只说明
            # "这次工具调用失败了"，它同时覆盖鉴权拒绝和工具自己崩溃、上游数据源不可用
            # 一类的故障。默认判成"同步中"会让一次工具崩溃安静地等满十五分钟再转运维，
            # 运维拿到的分类是"权限还没同步"——指向完全错误的方向。真实语义 L4a 实测
            # 之后，由装配层显式打开这个开关（白名单方式放宽，而不是默认放宽）。
            raise McpProbeError("tool_error", denied=self._tool_error_is_denied)
        count = self._metrics_reader(result)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise McpProbeError("invalid_metric_count", denied=False)
        return count


__all__ = [
    "CONTENT_KEY",
    "CONTENT_TEXT_TYPE",
    "DEFAULT_DENIED_ERROR_CODES",
    "DEFAULT_DENIED_STATUS_CODES",
    "DEFAULT_TOOL_NAME",
    "JSONRPC_VERSION",
    "METRIC_LIST_KEY",
    "METRIC_RECORD_FIELDS",
    "McpHttpResponse",
    "QueryMcpProbe",
    "REQUEST_TIMEOUT_SECONDS",
    "STRUCTURED_CONTENT_KEY",
    "content_text_metric_ids_reader",
    "content_text_metrics_reader",
    "default_metrics_reader",
    "fetch_metric_catalog",
    "urllib_mcp_transport",
]
