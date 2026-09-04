"""飞书 docx 文档交付适配器：生产适配器，只做 API 面，不接线。

建文档、写正文、对个人 ``open_id`` 授予文档级 ``full_access``、协作者读回；
交付形态：随对话交付给个人 + 文档级 ``full_access`` + 所有权留机器人。
本模块只做协议细节；接线属于 ``core.execution``/``apps.gateway``。**姿态
选择：裸 HTTP**：标准库 ``urllib``、不建 client、不发请求；令牌供给是
``Callable[[], str]``。**失败语义：不静默**：业务错误码非 0 抛
:class:`FeishuDocxDeliveryError`；成功但缺标识抛 ``LookupError``；HTTP 5xx
判「结果不明」、不解析响应体（见 :func:`urllib_transport`）。

正文交付默认走服务端一次建档（见 :meth:`LarkDocxDelivery.
create_document_with_markdown`），``markdown_convert_enabled`` 是退回两步
段落路径的止损闸；``read_body_children`` 供检查点判断是否已写过正文；
``document_url`` 需要单独注入 ``tenant_domain``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

#: 出站超时。与其它裸 HTTP 飞书适配器同量级（``feishu_tenant_token``/
#: ``feishu_directory``）：一次调用挂死不该无界占住调用方。
REQUEST_TIMEOUT_SECONDS = 20

#: 权限接口的 ``type`` 查询参数：docx 文档（区别于 folder/sheet 等其它对象类型）。
DOCX_PERMISSION_TYPE = "docx"

#: 唯一的授予档位：文档级「可管理」。
FULL_ACCESS_PERM = "full_access"

#: 授权的成员标识类型：飞书用户 ``open_id``（区别于 ``email``/``unionid`` 等）。
OPENID_MEMBER_TYPE = "openid"

#: 飞书用户 ``open_id`` 前缀，用于入口形状校验（同
#: :data:`lingxi.adapters.feishu_user_message.USER_OPEN_ID_PREFIX` 的理由：
#: 把群/租户标识误传成用户 open_id，要在**发出去之前**失败）。
USER_OPEN_ID_PREFIX = "ou_"

#: docx 正文段落 block 的 ``block_type``：S0 探针实测的纯文本段落类型。
_TEXT_PARAGRAPH_BLOCK_TYPE = 2

_DOCX_DOCUMENTS_PATH = "/docx/v1/documents"

#: 服务端一次建档写全文端点。**不带 ``/docx`` 前缀**——它是另一个接口族
#: （``docs_ai``），不是 docx 块 API 的子路径。
_DOCS_AI_DOCUMENTS_PATH = "/docs_ai/v1/documents"

#: 一次建档的正文格式。另一个合法取值是 ``xml``（带块 id 的结构化形式），本模块
#: 只发 markdown——模型产出的就是 markdown，转成 xml 等于把刚交回服务端的排版
#: 责任又拿回来一次。
_MARKDOWN_FORMAT = "markdown"

#: 标题在一次建档里的承载方式：拼在正文最前面的一个标签，不是独立字段。
_TITLE_OPEN_TAG = "<title>"
_TITLE_CLOSE_TAG = "</title>"

#: 服务端对这次建档的自评（``data.result``）。只有 ``success`` 与"该键不存在"
#: 算作"没有降级"，其余一切取值都倒向降级——见 :func:`_degraded_reason`。
_RESULT_SUCCESS = "success"
_RESULT_FAILED = "failed"

#: ``result="failed"`` 的原因码：服务端明确说这次建档失败。判 ``definite``——
#: 这是服务端给出的结论，不是传输层的猜测。
DOCS_AI_RESULT_FAILED = "docs_ai_result_failed"

#: 正文长度**前置守卫**阈值（字符数）。超过它就不去调一次建档端点，改走两步的
#: 段落路径并明示降级。该接口长度上限与限流官方无契约，20 000 是实测安全带：
#: 明显大于真实正文规模、明显小于实测撞 504 超时的长度，独立于登记侧的产品
#: 长度上限（两者取值互不依赖）。
MAX_MARKDOWN_CHARS = 20_000

#: 明示降级的原因码，供调用方把 ``body_degraded_reason`` 落库并改用「格式已
#: 简化」的用户文案。前两者在发出请求之前判定，是调用方唯一允许捕获并改走
#: 段落路径的两个码（尚未发生外部副作用，改路安全）；第三者是一次建档已经
#: 成功、但服务端自陈排版有降级，不改路不重试，只是如实告知。
BODY_TOO_LONG = "body_too_long"
TITLE_NOT_EMBEDDABLE = "title_not_embeddable"
SERVER_SIMPLIFIED_BODY = "server_simplified_body"

#: :meth:`LarkDocxDelivery.create_document_with_markdown` 在发出请求之前抛出
#: 的原因码集合；同时是抛出点与唯一捕获点的判据，两处必须逐字一致。
PRE_FLIGHT_DEGRADE_REASONS = frozenset({BODY_TOO_LONG, TITLE_NOT_EMBEDDABLE})


@dataclass(frozen=True)
class CreatedDocument:
    """:meth:`LarkDocxDelivery.create_document_with_markdown` 的返回值：这次一次建档到底建出了什么。

    ``document_id``：新文档的标识，正文**已经**随这次调用写完（不存在"建了
    档、正文还没写"的中间态）。``degraded_reason``：``None`` 表示服务端未
    自陈任何降级；非 ``None`` 表示服务端说这次排版有简化，用户拿到的排版
    与他本该拿到的不同——必须有这个返回值而不是让适配器自己把降级咽下去，
    静默降级会制造"用户以为拿到了带格式的文档、实际收到另一种内容"的假象。
    """

    document_id: str
    degraded_reason: str | None = None

    @property
    def degraded(self) -> bool:
        """是否有服务端自陈的排版降级。"""
        return self.degraded_reason is not None


class FeishuDocxDeliveryError(RuntimeError):
    """飞书 docx 交付失败。``code`` 供程序判断，消息里不含凭据、正文或标识符。

    ``definite``：``True`` 表示飞书明确拒绝（收到业务错误码），``False`` 表示
    结果不明（传输层异常、超时、响应形状不对）。判别口径同
    :class:`lingxi.adapters.feishu_directory.FeishuDirectoryError`。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        """记录安全分类字符串；``definite`` 缺省时按 feishu_code_ 前缀推断。"""
        super().__init__(f"飞书 docx 交付失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS：误配 ``http://`` 会把 Bearer token 明文上路。"""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url 必须由配置注入，不得写死在代码里")
    text = base_url.strip()
    parsed = urlparse(text)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("飞书 base_url 必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError("飞书 base_url 不得包含 URL fragment")
    return text.rstrip("/")


def _require_tenant_domain(value: str) -> str:
    """校验用于拼文档链接的裸域名（不是 API base_url）。

    不含协议、路径或空白，避免把一段可注入的值悄悄拼进对外链接。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tenant_domain 必须由配置注入，不得写死在代码里")
    text = value.strip()
    if "://" in text or "/" in text or any(character.isspace() for character in text):
        raise ValueError(
            "tenant_domain 必须是裸域名（不含协议、路径或空白），例如 example.feishu.cn"
        )
    return text


def _require_document_id(document_id: str) -> str:
    text = (document_id or "").strip()
    if not text:
        raise ValueError("document_id 不能为空")
    if any(character.isspace() for character in text):
        raise ValueError("document_id 不得包含空白字符，不回显收到的值")
    return text


def _require_user_open_id(open_id: str) -> str:
    """校验用户 ``open_id`` 形状；不合法就快速失败，且不回显取到的值。

    把群/租户标识误传成用户 open_id，要在**发出去之前**失败，而不是把
    「可管理」权限授予一个错误的收件人。
    """
    text = (open_id or "").strip()
    if not text.startswith(USER_OPEN_ID_PREFIX) or len(text) <= len(USER_OPEN_ID_PREFIX):
        raise ValueError(
            f"open_id 必须是飞书用户 open_id（以 {USER_OPEN_ID_PREFIX} 开头），不回显收到的值"
        )
    if any(character.isspace() for character in text):
        raise ValueError("open_id 不得包含空白字符，不回显收到的值")
    return text


def _safe_feishu_code(value: object) -> str:
    """把飞书业务错误码渲染成审计安全的分类标签。

    响应体是不可信的外部数据，只在 ``value`` 是货真价实的 ``int``（排除
    ``bool``）时插值，否则退化成固定标签，防止响应内容注入进异常消息/审计行。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return f"feishu_code_{value}"
    return "feishu_code_invalid"


def _build_markdown_content(title: str, markdown: str) -> str:
    """把标题与正文拼成一次建档的 ``content``：``<title>…</title>`` ＋ 空行 ＋ 正文。

    只做拼接，不改写正文一个字符——正文里的 ``-12.85%``/``3-5%`` 属于数据
    本身，不是语法噪音。标题里若含尖括号，拼接会破坏这个标签的边界，由
    调用方在拼接**之前**拦下，这里不做任何静默转义。
    """
    return f"{_TITLE_OPEN_TAG}{title}{_TITLE_CLOSE_TAG}\n\n{markdown}"


def _validated_document_content(title: str, markdown: str) -> str:
    """校验标题与正文、拼出一次建档的 ``content``。

    不适合走这条路时抛 :data:`PRE_FLIGHT_DEGRADE_REASONS` 里的原因码
    （``definite=True``）：标题含尖括号会破坏标签边界，不做静默转义或剥离；
    正文过长不拿去撞 504，失败关闭时一个请求都还没发出去，调用方改走两步
    段落路径是安全的。
    """
    text = (title or "").strip()
    if not text:
        raise ValueError("文档标题不能为空")
    body_text = markdown if isinstance(markdown, str) else ""
    if not body_text.strip():
        raise ValueError("文档正文不能为空")
    if _TITLE_OPEN_TAG[0] in text or _TITLE_CLOSE_TAG[-1] in text:
        raise FeishuDocxDeliveryError(TITLE_NOT_EMBEDDABLE, definite=True)
    content = _build_markdown_content(text, body_text)
    if len(content) > MAX_MARKDOWN_CHARS:
        raise FeishuDocxDeliveryError(BODY_TOO_LONG, definite=True)
    return content


def _degraded_reason(data: Mapping[str, Any]) -> str | None:
    """按服务端自陈判定这次建档有没有降级；``result="failed"`` 直接抛确定性失败。

    判定顺序与"拿不准倒向多说一句"的方向都是刻意的：``result="failed"`` →
    确定性失败；``warnings`` 非空 → 降级（不看 ``result``，服务端可能同时
    给出 ``success`` 与警告，以警告为准）；``result`` 是除 ``success`` 之外
    的任何取值（含未来新增的未登记取值）→ 降级，不枚举白名单；``result``
    键不存在 → 不降级。**不是"全部降级都会被发现"的保证**：服务端静默
    丢弃某些内容且不产生任何痕迹时，本函数看不见，不会告知用户。
    """
    result = data.get("result")
    if isinstance(result, str) and result.strip().lower() == _RESULT_FAILED:
        raise FeishuDocxDeliveryError(DOCS_AI_RESULT_FAILED, definite=True)
    warnings = data.get("warnings")
    if isinstance(warnings, (list, tuple)) and any(warning for warning in warnings):
        return SERVER_SIMPLIFIED_BODY
    if result is None:
        return None
    if isinstance(result, str) and result.strip().lower() == _RESULT_SUCCESS:
        return None
    return SERVER_SIMPLIFIED_BODY


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
    """默认传输层：只发 HTTPS，**不重试**任何请求，交由调用方决定要不要重试。

    **HTTP 5xx 一律不解析响应体，直接判"结果不明"（``definite=False``）**：
    一次建档超时返回的是 HTTP 504 ＋ 一个非 0 的业务码，但**服务端其实已经
    把整篇文档建出来了**。照常解析响应体会让那个非 0 的 ``code`` 被判成
    "飞书明确拒绝"，把一次"可能已经建好文档"的调用记成确定性失败，与真实
    世界相反——5xx 是服务端/网关侧的故障或超时，永远不证明请求没有生效，
    因此响应体里的业务码在这里不具备判别力，不读。4xx 与 2xx 仍照常解析：
    飞书的业务错误码走这两类状态码返回。
    """
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:  # 地址来自受控配置且已校验 https
            return json.loads(response.read())
    except HTTPError as error:
        if error.code >= 500:
            # 见本函数文档字符串：5xx 不解析响应体，结果不明。
            raise FeishuDocxDeliveryError(f"http_{error.code}", definite=False) from error
        try:
            return json.loads(error.read())
        except Exception as parse_error:
            raise FeishuDocxDeliveryError(f"http_{error.code}", definite=False) from parse_error
    except (URLError, OSError, TimeoutError) as error:
        raise FeishuDocxDeliveryError("transport_error", definite=False) from error
    except ValueError as error:
        raise FeishuDocxDeliveryError("invalid_json", definite=False) from error


class LarkDocxDelivery:
    """飞书 docx 文档交付：建文档、写正文、授予「可管理」、协作者读回。

    构造函数**只存参数**：不发请求、不缓存令牌（纪律同
    :class:`lingxi.adapters.feishu_group_message.FeishuGroupMessages`）。传输层
    由 ``transport`` 注入，默认是本模块的 :func:`urllib_transport`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant_access_token: Callable[[], str],
        tenant_domain: str,
        transport: Callable[..., Any] | None = None,
        markdown_convert_enabled: bool = False,
    ) -> None:
        """校验 base_url、令牌供给与 tenant_domain；不发请求、不缓存令牌。"""
        self._base_url = _require_https(base_url)
        if not callable(tenant_access_token):
            raise ValueError("tenant_access_token 必须是返回令牌字符串的可调用对象")
        self._tenant_access_token = tenant_access_token
        self._tenant_domain = _require_tenant_domain(tenant_domain)
        self._transport: Callable[..., Any] = transport or urllib_transport
        # 止损闸：默认 False（构造函数自身的默认值＝零行为变化；真正生效的值
        # 由装配层显式传入）。不是本类自己读环境变量（adapters/ 不直接读
        # os.environ），由装配层把解析好的布尔值传进来。
        self._markdown_convert_enabled = bool(markdown_convert_enabled)

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(dict(params))}"
        token = self._tenant_access_token()
        if not isinstance(token, str) or not token:
            # 结果不明：令牌供给没有按约定返回非空字符串。不是飞书拒绝，是调用方
            # 传入的供给本身坏了——同样不能静默，必须让调用方看见。
            raise FeishuDocxDeliveryError("tenant_access_token_missing", definite=False)
        return self._transport(method, url, body=body, token=token)

    def _data(self, response: Any) -> Mapping[str, Any]:
        """飞书业务错误码非 0 → 抛出 :class:`FeishuDocxDeliveryError`（``definite=True``）。

        这是本模块唯一判定"飞书明确拒绝"的位置，刻意不做静默降级——一旦
        这里改成"记日志后继续"，所有写操作都会在飞书拒绝的情况下被上层
        误判为成功。
        """
        if not isinstance(response, Mapping):
            raise FeishuDocxDeliveryError("invalid_response_shape", definite=False)
        code = response.get("code")
        if code not in (None, 0, "0"):
            raise FeishuDocxDeliveryError(_safe_feishu_code(code), definite=True)
        data = response.get("data")
        return data if isinstance(data, Mapping) else {}

    def create_document(self, title: str) -> str:
        """建一篇新文档，返回 ``document_id``。

        ``POST /docx/v1/documents``，S0 探针实测的请求体只有 ``title`` 一个字段
        （不传 ``folder_token`` 时飞书把文档建在应用的默认位置）。
        """
        text = (title or "").strip()
        if not text:
            raise ValueError("文档标题不能为空")
        data = self._data(self._call("POST", _DOCX_DOCUMENTS_PATH, body={"title": text}))
        document = data.get("document")
        if not isinstance(document, Mapping):
            raise LookupError("建文档响应缺少 document 字段：结果不明，不能确定文档是否已建好")
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise LookupError("建文档响应缺少可回读标识 document_id：结果不明")
        logger.info("飞书 docx 文档已建 document_id_len=%s", len(document_id))
        return document_id

    def write_paragraphs(self, document_id: str, paragraphs: Sequence[str]) -> None:
        """把 ``paragraphs`` 逐段写成正文，一次调用消费掉一次外部写请求预算。

        ``POST /docx/v1/documents/{document_id}/blocks/{document_id}/children``：
        S0 探针实测根 block 的 ``block_id`` 就是 ``document_id`` 本身，多段正文
        对应 ``children`` 数组里的多个 ``block_type=2``（文本段落）block，一次
        请求的 ``index`` 固定为 0（本模块只服务"整篇正文一次写完"这个场景，不
        提供中途插入）。
        """
        doc_id = _require_document_id(document_id)
        texts = list(paragraphs) if paragraphs is not None else []
        if not texts:
            raise ValueError("正文段落不能为空")
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"第 {index + 1} 段正文不能为空")
        children = [
            {
                "block_type": _TEXT_PARAGRAPH_BLOCK_TYPE,
                "text": {"elements": [{"text_run": {"content": text}}]},
            }
            for text in texts
        ]
        self._data(
            self._call(
                "POST",
                f"{_DOCX_DOCUMENTS_PATH}/{doc_id}/blocks/{doc_id}/children",
                body={"children": children, "index": 0},
            )
        )
        logger.info("飞书 docx 正文已写入 document_id_len=%s 段落数=%s", len(doc_id), len(texts))

    @property
    def markdown_convert_enabled(self) -> bool:
        """这套部署要不要走「服务端一次建档写全文」这条路。

        只读、不带副作用。刻意做成属性而不是让
        :meth:`create_document_with_markdown` 在开关关闭时抛一个原因码——
        关闭时走段落路径不是降级（是这套部署本来就要求的排版），用降级
        机制表达它会让调用方把一次正常交付误告知成"格式已简化"。
        """
        return self._markdown_convert_enabled

    def create_document_with_markdown(self, title: str, markdown: str) -> CreatedDocument:
        """**一次调用**建档并写完整篇正文，返回 :class:`CreatedDocument`。

        ``POST /open-apis/docs_ai/v1/documents``，标题以 ``<title>…</title>``
        拼在正文最前面。发出请求之前的两道守卫见 :func:`_validated_document_content`，
        是调用方**唯一**允许捕获并改走两步段落路径的两个原因码；其余一切都
        必须原样向上抛——**绝不重试**：超时之后改走段落路径会真的建出第二篇
        完整文档（第一篇很可能已经建好，只是拿不到 id），结果不明交由调用方
        按 ``uncertain`` 处理，不自动重发。返回值里的 ``degraded_reason`` 由
        :func:`_degraded_reason` 判定，调用方必须接住并如实告知用户。
        """
        content = _validated_document_content(title, markdown)

        data = self._data(
            self._call(
                "POST",
                _DOCS_AI_DOCUMENTS_PATH,
                body={"format": _MARKDOWN_FORMAT, "content": content},
            )
        )
        degraded_reason = _degraded_reason(data)
        document = data.get("document")
        if not isinstance(document, Mapping):
            raise LookupError("一次建档响应缺少 document 字段：结果不明，不能确定文档是否已建好")
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise LookupError("一次建档响应缺少可回读标识 document_id：结果不明")
        logger.info(
            "飞书 docx 文档已一次建档并写入正文 document_id_len=%s content_len=%s degraded_reason=%s",
            len(document_id),
            len(content),
            degraded_reason,
        )
        return CreatedDocument(document_id=document_id, degraded_reason=degraded_reason)

    def grant_full_access(self, document_id: str, open_id: str) -> None:
        """对 ``open_id`` 这个人授予文档级「可管理」（唯一的授予档位）。

        ``POST /drive/v1/permissions/{document_id}/members?type=docx``。
        """
        doc_id = _require_document_id(document_id)
        member_id = _require_user_open_id(open_id)
        self._data(
            self._call(
                "POST",
                f"/drive/v1/permissions/{doc_id}/members",
                params={"type": DOCX_PERMISSION_TYPE},
                body={
                    "member_type": OPENID_MEMBER_TYPE,
                    "member_id": member_id,
                    "perm": FULL_ACCESS_PERM,
                },
            )
        )
        logger.info("飞书 docx 已授予可管理 document_id_len=%s", len(doc_id))

    def read_members(self, document_id: str) -> list[dict[str, Any]]:
        """读回协作者列表，供调用方判定"真实创建 + 权限读回后才算成功"。

        ``GET /drive/v1/permissions/{document_id}/members?type=docx``。真实
        响应把协作者数组放在 ``data.items``（每一项形状是
        ``{member_id, member_type, perm, perm_type}``），docx 类型与 folder
        权限对象类型的响应形状不同。优先读 ``items``；取不到时降级读一次
        ``members``（兼容旧形状或未来可能的回归，不代表它是当前真实形状）。
        返回的每一项只保留 ``member_type``/``member_id``/``perm`` 三个字段，
        不透传飞书响应里可能携带的其它字段。
        """
        doc_id = _require_document_id(document_id)
        data = self._data(
            self._call(
                "GET",
                f"/drive/v1/permissions/{doc_id}/members",
                params={"type": DOCX_PERMISSION_TYPE},
            )
        )
        members = data.get("items")
        if not isinstance(members, list):
            members = data.get("members")
        if not isinstance(members, list):
            raise LookupError("读回协作者响应缺少 items/members 字段：结果不明")
        return [
            {
                "member_type": member.get("member_type"),
                "member_id": member.get("member_id"),
                "perm": member.get("perm"),
            }
            for member in members
            if isinstance(member, Mapping)
        ]

    def read_body_children(self, document_id: str) -> list[dict[str, Any]]:
        """读回正文根 block（``document_id`` 自身）当前的子块列表。

        与 :meth:`write_paragraphs` 写入的是同一个坐标（同一个根 block、
        同一个 ``children`` 集合），这里只是把同一个位置反过来读一遍，不做
        任何推断。调用方据此判断"这篇文档是否已经写过正文"，非空即跳过重驱
        写正文步。响应形状比照 :meth:`read_members`：协作者列表放在
        ``data.items``，同一接口族同一口径，缺失时抛 ``LookupError`` 归类为
        结果不明（成功响应缺可回读结构 ≠ 确定为空）。
        """
        doc_id = _require_document_id(document_id)
        data = self._data(
            self._call("GET", f"{_DOCX_DOCUMENTS_PATH}/{doc_id}/blocks/{doc_id}/children")
        )
        children = data.get("items")
        if not isinstance(children, list):
            raise LookupError("读回正文根 block 子块响应缺少 items 字段：结果不明")
        return [child for child in children if isinstance(child, Mapping)]

    def document_url(self, document_id: str) -> str:
        """拼出用户可直接打开的文档链接（见模块文档「文档 URL 的构造」一节）。

        纯本地拼接，不发起任何请求。
        """
        doc_id = _require_document_id(document_id)
        return f"https://{self._tenant_domain}/docx/{doc_id}"


__all__ = [
    "BODY_TOO_LONG",
    "DOCS_AI_RESULT_FAILED",
    "DOCX_PERMISSION_TYPE",
    "FULL_ACCESS_PERM",
    "MAX_MARKDOWN_CHARS",
    "OPENID_MEMBER_TYPE",
    "PRE_FLIGHT_DEGRADE_REASONS",
    "SERVER_SIMPLIFIED_BODY",
    "TITLE_NOT_EMBEDDABLE",
    "USER_OPEN_ID_PREFIX",
    "CreatedDocument",
    "FeishuDocxDeliveryError",
    "LarkDocxDelivery",
    "REQUEST_TIMEOUT_SECONDS",
    "Transport",
    "urllib_transport",
]

# 说明：`create_document_with_markdown`/`read_body_children` 都是
# `LarkDocxDelivery` 的实例方法，不单独导出符号——同
# `create_document`/`write_paragraphs`/`grant_full_access`/`read_members`
# 既有方法一样，只通过类本身暴露。
