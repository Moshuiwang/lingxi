"""问数 MCP 的就绪探针：以目标用户身份执行一次 ``list_metrics``（Issue #156 / S-C-02）。

实现 :class:`lingxi.core.permission.mcp_readiness.McpProbe`。判定（就绪 / 同步中 /
技术失败怎么分、等多久、记不记账）都不在这里，在
:mod:`lingxi.core.permission.mcp_readiness`；本模块只负责把协议细节做对，并在边界上把
传输与协议的失败翻译成本仓库的语义。

## 诚实边界：**真实 MCP 协议面未实测（证据等级 L1）**

与 ``adapters/feishu_permission_bitable.py`` 同一姿态，但**更弱**：那个模块至少有三条
读/建/读回路径经过 G-BIT 回源实测，而问数 MCP 这一侧，本 Story **一次真实调用都没有
发生过**。下面全部断言跑在注入的假传输层上。因此三件事按未验证登记，等 Epic C 冻结后的
受控窗口做 L4a：

1. **端点形态与协议版本**：这里按 MCP Streamable HTTP + JSON-RPC 2.0 的 ``tools/call``
   编写（接口设计[「五、问数 MCP」](../../../docs/技术设计/接口设计.md#五问数-mcp消费方)），
   真实服务端是否就是这个形态未经证实；
2. **``list_metrics`` 的返回形状**：``structuredContent`` 里指标列表挂在哪个键、还是
   只有 ``content`` 里的一段 JSON 文本，未经证实。因此计数走**可注入**的
   ``metrics_reader``，默认实现**读不懂就失败**（见 :func:`default_metrics_reader`）；
3. **"权限还没同步"到底以什么错误形态出现**：HTTP 401/403、JSON-RPC error、还是
   ``result.isError``，未经证实。三种都覆盖到了，且**分类可注入**
   （``denied_status_codes`` / ``denied_error_codes``）。

**所有未知形态一律落"技术失败"，绝不落"就绪"。** 这是本模块唯一不肯让步的地方：一次
读不懂的响应被凑成就绪，用户侧表现为"开通成功了但一问就没有权限"，而我们的记录会说
一切正常。

## 令牌只走请求头，且一次都不进 ``core``

明文令牌由注入的 ``token_provider`` 按 ``user_id`` 现取现用：只放进 ``Authorization``
请求头（由传输层负责），**不进 URL、不进日志、不进异常消息**，用完即随栈帧消失。
:class:`lingxi.core.permission.mcp_readiness.McpProbe` 的签名里没有令牌参数，因此明文
在类型上就到不了判定层。

``token_provider`` 自己失败（解密失败、数据库不可达、这个人还没签发令牌）时，本模块
**翻译成 ``token_unavailable`` 的技术失败**，而不是像
``feishu_permission_bitable._token`` 那样原样上抛。差别是刻意的：在那条链路上"拿不到
凭据"不属于任何一种已登记的发布结果，只能上抛；而在这里它**正好就是**五路分流里的
「技术失败（网络、协议、数据库异常）」——落进去之后它会被独立记录、可分辨、且绝不会被
读成就绪或无权限。原始异常**不进消息也不进 traceback 链**：它可能带着连接串或响应片段。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Callable, NamedTuple

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

#: 从结构化结果里找指标列表时依次尝试的键。顺序即优先级。
METRIC_LIST_KEYS: tuple[str, ...] = ("metrics", "items", "data", "result", "list")


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


def urllib_mcp_transport(
    method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None
) -> McpHttpResponse:
    """默认传输：只发 HTTPS，不重试。令牌只进 ``Authorization`` 头。

    HTTP 错误**不抛异常**：状态码本身是分类依据（401/403 = 明确拒绝），把它折成一个
    通用的传输异常会让"被拒绝"和"网断了"变成同一件事。响应体解析失败时载荷给 ``None``，
    交由上层按状态码决定它是明确拒绝还是形状错误。
    """

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - 地址来自受控配置
            return McpHttpResponse(int(response.status), _load_json(response.read()))
    except HTTPError as error:
        return McpHttpResponse(int(error.code), _load_json(_read_quietly(error)))
    except (URLError, OSError, TimeoutError):
        # 不接 traceback 链：底层异常的 ``str`` 里可能带着完整 URL 与请求头摘要。
        raise McpProbeError("transport_error", denied=False) from None


def _read_quietly(error: Any) -> bytes:
    try:
        return error.read()
    except Exception:  # noqa: BLE001 - 读不到就当空响应，状态码已经够分类了
        return b""


def _load_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _count_from_document(document: Any) -> int | None:
    """从一个"可能是指标列表"的文档里数出条数；读不懂返回 ``None``。"""

    if isinstance(document, list):
        return len(document)
    if isinstance(document, Mapping):
        for key in METRIC_LIST_KEYS:
            value = document.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def default_metrics_reader(result: Mapping[str, Any]) -> int:
    """默认的指标计数：先看 ``structuredContent``，再看 ``content`` 里的 JSON 文本。

    **不数 ``content`` 的块数**。MCP 的 ``content`` 通常是一个文本块，里面装着整份 JSON；
    数块数会得到 1，于是一次"你没有任何指标"的空回答也会被判成就绪——**恰好是**
    "明确空结果不算就绪"要挡的那种假成功。因此这里只认真正的列表：要么在
    ``structuredContent`` 里，要么在文本块解析出来的 JSON 里。

    两条路都读不出列表就抛技术失败。默认实现宁可说"我读不懂"，也不猜一个数字出来——
    真实形状留 L4a 确认，确认后可由装配层注入更精确的 reader。
    """

    count = _count_from_document(result.get("structuredContent"))
    if count is not None:
        return count
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping):
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                document = json.loads(text)
            except ValueError:
                continue
            count = _count_from_document(document)
            if count is not None:
                return count
    raise McpProbeError("unrecognized_result_shape", denied=False)


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
    ) -> None:
        self._endpoint = _require_https(endpoint)
        if not callable(token_provider):
            raise ValueError("token_provider 必须是按 user_id 返回明文令牌的可调用对象")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("探针工具名不得为空")
        self._token_provider = token_provider
        self._transport: Callable[..., McpHttpResponse] = transport or urllib_mcp_transport
        self._tool_name = tool_name.strip()
        self._metrics_reader = metrics_reader or default_metrics_reader
        self._denied_status_codes = tuple(denied_status_codes)
        self._denied_error_codes = tuple(denied_error_codes)

    def list_metrics(self, *, user_id: str) -> int:
        """以该用户身份执行一次 ``list_metrics``，返回可见指标条数。

        失败一律抛 :class:`~lingxi.core.permission.mcp_readiness.McpProbeError`，
        ``denied`` 标明是"MCP 明确拒绝"（同步中）还是"结果不明"（技术失败）。
        """

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("就绪探针必须指明用户")
        token = self._token(user_id)
        request = {
            "jsonrpc": "2.0",
            # 请求标识用内部 ULID：不含用户资料，也不需要与任何外部标识对齐。
            "id": new_ulid(),
            "method": "tools/call",
            "params": {"name": self._tool_name, "arguments": {}},
        }
        response = self._transport("POST", self._endpoint, body=request, token=token)
        del token  # 明文不再需要，尽早从本帧移除。
        return self._read(response)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _token(self, user_id: str) -> str:
        try:
            token = self._token_provider(user_id)
        except Exception as error:  # noqa: BLE001 - 失败形态由注入的 provider 决定
            # 只记类型，不记消息也不接 traceback 链：provider 的异常可能带着连接串或
            # 响应片段。这一路落技术失败——可分辨、被独立记录，且绝不会被读成就绪。
            logger.warning("就绪探针取令牌失败 user=%s error=%s", user_id, type(error).__name__)
            raise McpProbeError("token_unavailable", denied=False) from None
        if not isinstance(token, str) or not token:
            # 这个人还没有签发过令牌。不是"没权限"（那是数据库侧的判定），
            # 而是我们这一侧还没准备好——技术失败。
            raise McpProbeError("token_missing", denied=False)
        return token

    def _read(self, response: object) -> int:
        if not isinstance(response, McpHttpResponse):
            raise McpProbeError("invalid_transport_result", denied=False)
        status, payload = response.status, response.payload
        if status in self._denied_status_codes:
            # MCP 看见了请求并明确拒绝：它还没拉到我们发布的那一行 → 同步中。
            raise McpProbeError(f"http_{status}", denied=True)
        if status != 200:
            raise McpProbeError(f"http_{status}", denied=False)
        if not isinstance(payload, Mapping):
            raise McpProbeError("invalid_response_shape", denied=False)
        error = payload.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, Mapping) else None
            denied = isinstance(code, int) and code in self._denied_error_codes
            # **错误消息一个字都不进我们的错误码**：它是服务端文本，可能回显请求内容。
            raise McpProbeError(f"jsonrpc_{code}", denied=denied)
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise McpProbeError("invalid_result_shape", denied=False)
        if result.get("isError") is True:
            # 工具跑了但拒绝了我们（MCP 的工具级错误约定）：同样按同步中处理。
            raise McpProbeError("tool_error", denied=True)
        count = self._metrics_reader(result)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise McpProbeError("invalid_metric_count", denied=False)
        return count


__all__ = [
    "DEFAULT_DENIED_ERROR_CODES",
    "DEFAULT_DENIED_STATUS_CODES",
    "DEFAULT_TOOL_NAME",
    "METRIC_LIST_KEYS",
    "McpHttpResponse",
    "QueryMcpProbe",
    "REQUEST_TIMEOUT_SECONDS",
    "default_metrics_reader",
    "urllib_mcp_transport",
]
