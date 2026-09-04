"""飞书电子表格交付适配器：生产适配器，只做 API 面，不接线。

建表、写单元格、对个人 ``open_id`` 授予表格级 ``full_access``、协作者读回，
全部用应用身份令牌调通。交付形态：随对话交付给个人 + 表格级
``full_access`` + 所有权留机器人。接线属于 ``apps/gateway/document_delivery.py``。

**姿态选择：裸 HTTP**，同 :mod:`lingxi.adapters.feishu_docx_delivery`：
标准库 ``urllib``、零新增依赖、不建 client、不发请求。**与文档交付的
差异点**：建表响应自带 ``url``；版本号混用（建表/查询/授权/读回是 v3，
写单元格是 v2）；写值天然幂等，检查点恢复路径可以无条件重放。

**失败语义：不静默**：业务错误码明确非 0 时抛
:class:`FeishuSheetsDeliveryError`；缺失可回读标识抛 ``LookupError``；
``code`` 字段缺失本身也不当作成功。日志与异常消息不落令牌、请求/响应正文。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

#: 出站超时。与 ``feishu_docx_delivery.REQUEST_TIMEOUT_SECONDS`` 同量级：一次
#: 调用挂死不该无界占住调用方。
REQUEST_TIMEOUT_SECONDS = 20

#: 权限接口的 ``type`` 查询参数：电子表格（区别于 docx/folder 等其它对象类型）。
SHEET_PERMISSION_TYPE = "sheet"

#: 唯一的授予档位：表格级「可管理」，同文档交付。
FULL_ACCESS_PERM = "full_access"

#: 授权的成员标识类型：飞书用户 ``open_id``。
OPENID_MEMBER_TYPE = "openid"

#: 飞书用户 ``open_id`` 前缀，用于入口形状校验（同
#: ``feishu_docx_delivery.USER_OPEN_ID_PREFIX`` 的理由：把群/租户标识误传成
#: 用户 open_id，要在**发出去之前**失败）。
USER_OPEN_ID_PREFIX = "ou_"

_SPREADSHEETS_V3_PATH = "/sheets/v3/spreadsheets"


class FeishuSheetsDeliveryError(RuntimeError):
    """飞书电子表格交付失败。``code`` 供程序判断，消息里不含凭据、正文或标识符。

    ``definite``：``True`` 表示飞书明确拒绝（收到业务错误码），``False`` 表示
    结果不明（传输层异常、超时、响应形状不对）。判别口径同
    :class:`lingxi.adapters.feishu_docx_delivery.FeishuDocxDeliveryError`。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        """记录安全分类字符串；``definite`` 缺省时按 feishu_code_ 前缀推断。"""
        super().__init__(f"飞书电子表格交付失败：{code}")
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


def _require_spreadsheet_token(spreadsheet_token: str) -> str:
    text = (spreadsheet_token or "").strip()
    if not text:
        raise ValueError("spreadsheet_token 不能为空")
    if any(character.isspace() for character in text):
        raise ValueError("spreadsheet_token 不得包含空白字符，不回显收到的值")
    return text


def _require_sheet_id(sheet_id: str) -> str:
    text = (sheet_id or "").strip()
    if not text:
        raise ValueError("sheet_id 不能为空")
    if any(character.isspace() for character in text):
        raise ValueError("sheet_id 不得包含空白字符，不回显收到的值")
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
    """默认传输层：只发 HTTPS，不重试有副作用的请求。"""
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
        try:
            return json.loads(error.read())
        except Exception as parse_error:
            raise FeishuSheetsDeliveryError(f"http_{error.code}", definite=False) from parse_error
    except (URLError, OSError, TimeoutError) as error:
        raise FeishuSheetsDeliveryError("transport_error", definite=False) from error
    except ValueError as error:
        raise FeishuSheetsDeliveryError("invalid_json", definite=False) from error


class LarkSheetsDelivery:
    """飞书电子表格交付：建表、查默认 sheet_id、写单元格、授予「可管理」、协作者读回。

    构造函数**只存参数**：不发请求、不缓存令牌。传输层由 ``transport``
    注入，默认是本模块的 :func:`urllib_transport`；不需要 ``tenant_domain``
    ——建表响应自带 ``url``。
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant_access_token: Callable[[], str],
        transport: Callable[..., Any] | None = None,
    ) -> None:
        """校验 base_url 与令牌供给；不发请求、不缓存令牌。"""
        self._base_url = _require_https(base_url)
        if not callable(tenant_access_token):
            raise ValueError("tenant_access_token 必须是返回令牌字符串的可调用对象")
        self._tenant_access_token = tenant_access_token
        self._transport: Callable[..., Any] = transport or urllib_transport

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
            raise FeishuSheetsDeliveryError("tenant_access_token_missing", definite=False)
        return self._transport(method, url, body=body, token=token)

    def _data(self, response: Any) -> Mapping[str, Any]:
        """飞书业务错误码非 0 → 抛出 :class:`FeishuSheetsDeliveryError`（``definite=True``）。

        这是本模块唯一判定"飞书明确拒绝"的位置，刻意不做静默降级：一旦这里
        改成"记日志后继续"，所有写操作都会在飞书拒绝的情况下被上层误判为
        成功。**fail-closed**：``code`` 字段缺失（``None``）不当作成功放行
        ——真实成功响应均带 ``code=0``，因此只有显式 ``0``/``"0"`` 才视为
        成功，``code`` 缺失一律判「结果不明」，交给调用方按传输层异常同等
        对待，防止「HTTP 500 + ``{}``」被当作成功响应、后续写值照常进行。
        """
        if not isinstance(response, Mapping):
            raise FeishuSheetsDeliveryError("invalid_response_shape", definite=False)
        code = response.get("code")
        if code is None:
            raise FeishuSheetsDeliveryError("missing_code", definite=False)
        if code not in (0, "0"):
            raise FeishuSheetsDeliveryError(_safe_feishu_code(code), definite=True)
        data = response.get("data")
        return data if isinstance(data, Mapping) else {}

    def create_spreadsheet(self, title: str) -> tuple[str, str]:
        """建一张新电子表格，返回 ``(spreadsheet_token, url)``。

        ``POST /sheets/v3/spreadsheets`` body ``{title}``——S-W0-3 探针实测
        ``data.spreadsheet`` 同时带 ``spreadsheet_token``/``url``，不需要像
        docx 那样额外调用一次接口才能拿到链接（见模块文档「与文档交付的差异点」
        第 1 条）。
        """
        text = (title or "").strip()
        if not text:
            raise ValueError("表格标题不能为空")
        data = self._data(self._call("POST", _SPREADSHEETS_V3_PATH, body={"title": text}))
        spreadsheet = data.get("spreadsheet")
        if not isinstance(spreadsheet, Mapping):
            raise LookupError("建表响应缺少 spreadsheet 字段：结果不明，不能确定表格是否已建好")
        spreadsheet_token = spreadsheet.get("spreadsheet_token")
        url = spreadsheet.get("url")
        if not isinstance(spreadsheet_token, str) or not spreadsheet_token:
            raise LookupError("建表响应缺少可回读标识 spreadsheet_token：结果不明")
        if not isinstance(url, str) or not url:
            raise LookupError("建表响应缺少可回读的 url：结果不明")
        logger.info("飞书电子表格已建 spreadsheet_token_len=%s", len(spreadsheet_token))
        return spreadsheet_token, url

    def get_default_sheet_id(self, spreadsheet_token: str) -> str:
        """查询新建表格默认已有的那一个 sheet 的 ``sheet_id``（写值前必须先拿到）。

        ``GET /sheets/v3/spreadsheets/{token}/sheets/query``——S-W0-3 探针实测
        ``data.sheets`` 是数组，新建表格默认已有 1 个 sheet，取第一个。纯只读
        调用，天然可以在检查点恢复路径上无条件重放，不需要单独持久化
        （见模块文档「与文档交付的差异点」第 3 条：只有写值步的幂等性才需要
        特别说明，查询本身不产生任何副作用）。
        """
        token = _require_spreadsheet_token(spreadsheet_token)
        data = self._data(self._call("GET", f"{_SPREADSHEETS_V3_PATH}/{token}/sheets/query"))
        sheets = data.get("sheets")
        if not isinstance(sheets, list) or not sheets:
            raise LookupError("查询 sheet 列表响应缺少非空 sheets 字段：结果不明")
        first_sheet = sheets[0]
        if not isinstance(first_sheet, Mapping):
            raise LookupError("查询 sheet 列表响应的第一项不是对象：结果不明")
        sheet_id = first_sheet.get("sheet_id")
        if not isinstance(sheet_id, str) or not sheet_id:
            raise LookupError("查询 sheet 列表响应缺少可回读标识 sheet_id：结果不明")
        return sheet_id

    def write_values(
        self, spreadsheet_token: str, sheet_id: str, rows: Sequence[Sequence[str]]
    ) -> None:
        """把 ``rows``（行×列的单元格文本）写入表格，从 ``A1`` 起按行列展开。

        ``PUT /sheets/v2/spreadsheets/{token}/values``——**注意版本号**：这是
        飞书 sheets API 已知的既有形状（建表/查询是 v3，写值是 v2），不是本模块
        选错版本号。``range`` 用 ``A1`` 起始、按最长行的列数与总行数算出结束
        坐标，是同一个"整块覆盖"语义：天然幂等。入参形状校验见
        :func:`_validated_matrix`。
        """
        token = _require_spreadsheet_token(spreadsheet_token)
        sid = _require_sheet_id(sheet_id)
        matrix = _validated_matrix(rows)
        row_count = len(matrix)
        column_count = max(len(row) for row in matrix)
        end_column = _column_letter(column_count)
        cell_range = f"{sid}!A1:{end_column}{row_count}"
        self._data(
            self._call(
                "PUT",
                f"/sheets/v2/spreadsheets/{token}/values",
                body={"valueRange": {"range": cell_range, "values": matrix}},
            )
        )
        logger.info(
            "飞书电子表格已写值 spreadsheet_token_len=%s 行数=%s 列数=%s",
            len(token),
            row_count,
            column_count,
        )

    def grant_full_access(self, spreadsheet_token: str, open_id: str) -> None:
        """对 ``open_id`` 这个人授予表格级「可管理」（唯一的授予档位）。

        ``POST /drive/v1/permissions/{token}/members?type=sheet``——与 docx 唯一
        差异是 ``type=sheet``（docx 是 ``type=docx``），端点、字段名完全一致。
        """
        token = _require_spreadsheet_token(spreadsheet_token)
        member_id = _require_user_open_id(open_id)
        self._data(
            self._call(
                "POST",
                f"/drive/v1/permissions/{token}/members",
                params={"type": SHEET_PERMISSION_TYPE},
                body={
                    "member_type": OPENID_MEMBER_TYPE,
                    "member_id": member_id,
                    "perm": FULL_ACCESS_PERM,
                },
            )
        )
        logger.info("飞书电子表格已授予可管理 spreadsheet_token_len=%s", len(token))

    def read_members(self, spreadsheet_token: str) -> list[dict[str, Any]]:
        """读回协作者列表，供调用方判定"真实创建 + 权限读回后才算成功"。

        ``GET /drive/v1/permissions/{token}/members?type=sheet``。响应形状同
        ``feishu_docx_delivery.LarkDocxDelivery.read_members``（S-W0-3 探针实测：
        协作者数组在 ``data.items``，每一项 ``{member_id, member_type, perm}``）
        ——同一接口族、同一口径，只是 ``type`` 参数值不同。返回的每一项只保留
        ``member_type``/``member_id``/``perm`` 三个字段，不透传其它字段。
        """
        token = _require_spreadsheet_token(spreadsheet_token)
        data = self._data(
            self._call(
                "GET",
                f"/drive/v1/permissions/{token}/members",
                params={"type": SHEET_PERMISSION_TYPE},
            )
        )
        members = data.get("items")
        if not isinstance(members, list):
            raise LookupError("读回协作者响应缺少 items 字段：结果不明")
        return [
            {
                "member_type": member.get("member_type"),
                "member_id": member.get("member_id"),
                "perm": member.get("perm"),
            }
            for member in members
            if isinstance(member, Mapping)
        ]


def _validated_matrix(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    """把 ``rows`` 校验成矩形的字符串矩阵；形状不对直接 ``ValueError``。

    校验先于 ``list(row)`` 展开：不能让 ``list(row)`` 先撞上 ``TypeError``
    才失败——调用方按异常类型分流终态（``ValueError`` → 明确
    ``failed``），入参形状错误发出请求之前就已确定，没有"可能已经生效"的
    空间。矩形校验是第二道纵深防线：上游已把矩阵补齐成矩形，这里不假设
    上游一定守约，收到不规则矩阵直接失败关闭，不猜测该怎么补。
    """
    if rows is None or not isinstance(rows, (list, tuple)):
        raise ValueError("表格内容必须是行的列表")
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"第 {index + 1} 行必须是单元格列表")
    matrix = [list(row) for row in rows]
    if not matrix:
        raise ValueError("表格内容不能为空")
    for index, row in enumerate(matrix):
        if not row:
            raise ValueError(f"第 {index + 1} 行不能为空")
        for cell in row:
            if not isinstance(cell, str):
                raise ValueError(f"第 {index + 1} 行包含非字符串单元格")
    row_lengths = {len(row) for row in matrix}
    if len(row_lengths) > 1:
        raise ValueError("表格内容必须是矩形矩阵：各行列数必须相同（调用方未按约定补齐）")
    return matrix


def _column_letter(column_count: int) -> str:
    """把 1-based 列数转成飞书表格的列字母坐标（1→A，26→Z，27→AA，……）。

    纯本地计算，不发起任何请求。只服务 :meth:`LarkSheetsDelivery.write_values`
    构造 ``range`` 结束坐标——``MAX_SHEET_COLUMNS``（表格分支硬上限，见
    ``core/execution/document_delivery.py``）当前取 40，远小于 26×26，这里的
    两位字母上限已经足够覆盖，但实现本身不假设这个上限、支持任意正整数。
    """
    if not isinstance(column_count, int) or column_count < 1:
        raise ValueError("column_count 必须是正整数")
    letters = ""
    remaining = column_count
    while remaining > 0:
        remaining, remainder = divmod(remaining - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


__all__ = [
    "FULL_ACCESS_PERM",
    "OPENID_MEMBER_TYPE",
    "SHEET_PERMISSION_TYPE",
    "USER_OPEN_ID_PREFIX",
    "FeishuSheetsDeliveryError",
    "LarkSheetsDelivery",
    "REQUEST_TIMEOUT_SECONDS",
    "Transport",
    "urllib_transport",
]
