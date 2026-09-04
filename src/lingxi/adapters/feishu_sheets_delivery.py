"""飞书电子表格交付适配器（Issue #354 S-H3-2：生产适配器，只做 API 面，不接线）。

来源：#354 S-W0-3 真实 API 探针（2026-08-28，四步全通，[评论](
https://github.com/Moshuiwang/lingxi/issues/354#issuecomment-5443426083)，证据
等级 6，Bot-Test 真实调用）——建表、写单元格、对个人 ``open_id`` 授予表格级
``full_access``（未被平台降级）、协作者读回，全部用应用身份令牌
（``tenant_access_token``）真实调通，且**没有遇到任何 scope/权限报错**，不需要
额外申请 scope。交付形态依据 D2 裁定：同构 #341 文档交付路由（决策记录
``docs/决策记录/2026-08-23-正式产物路由为随对话文档与文档级可管理授权.md``）——
随对话交付给个人 + 表格级 ``full_access`` + 所有权留机器人。

本模块只做协议细节，接线（何时建表、检查点、失败分类落库）属于
``apps/gateway/document_delivery.py`` 的职责；这里只保证一件事：调用形态与
飞书契约完全对齐，失败不静默。

## 姿态选择：裸 HTTP，同 :mod:`lingxi.adapters.feishu_docx_delivery`

与文档交付适配器同一习惯：标准库 ``urllib``、零新增依赖、构造函数只存参数、
不建 client、不发请求；传输层可注入，默认实现在函数内部延迟 import。本模块与
``feishu_docx_delivery`` 是结构对称的两个并列适配器，不是互相 import 复用同一个
类——两者的请求形状（版本号、参数、返回结构）差异足够大（见下文「与文档交付的
差异点」），提炼公共基类的收益小于增加的间接层，S-H3-2 卡边界也明确"不重构共享
层"。

## 令牌供给：``Callable[[], str]``

同 ``feishu_docx_delivery.LarkDocxDelivery`` 的姿态：构造函数接收
``tenant_access_token: Callable[[], str]``，每次调用只管去要一份当下能用的令牌，
不重新发明"要不要现在去换一次令牌"的判断。

## 与文档交付的差异点（S-W0-3 探针实测，不是障碍）

1. **建表响应自带 ``url``**：不需要像 docx 那样额外拼 ``tenant_domain``——飞书
   sheets 建表接口比 docx 建档接口多返回这一个字段，:meth:`LarkSheetsDelivery.
   create_spreadsheet` 因此直接把 ``url`` 一并返回给调用方持久化（检查点落库
   见 ``adapters/postgres_document_delivery.py`` 的 ``resource_url`` 列），不在
   本模块猜测或拼接链接格式。
2. **两个版本号混用**：建表/查询默认 sheet_id/授权/读回都是 ``/sheets/v3/...``
   或 ``/drive/v1/...``，写单元格却是 ``/sheets/v2/...``——这是飞书 sheets API
   已知的既有形状，不是本模块选错版本号。
3. **写值天然幂等，不需要"是否已写过"的检查点判据**：``PUT .../values`` 按显式
   ``range`` 覆盖对应区域的值，同一份内容重放多次得到的是同一个最终状态（同一个
   ``range`` 被同样的 ``values`` 再覆盖一次），不会像 docx 的
   ``blocks/{id}/children`` 追加接口那样越写越长。因此 :meth:`write_values`
   在 gateway 消费循环的检查点恢复路径上可以无条件重放，不需要
   ``feishu_docx_delivery.read_body_children`` 那样"先读一遍现有内容"的额外
   判据（Issue #353 修复只对 docx 生效，sheets 这条路径的失败模式本身就不存在）。
4. **``type`` 参数值是 ``sheet``**（不是 ``docx``）：``drive/v1/permissions`` 的
   授权/读回接口，端点与字段完全一致，唯一差异是这一个查询参数值。

## 失败语义：不静默

会发起真实调用的方法都不捕获任何未预期异常。飞书业务错误码明确非 0 时抛出
:class:`FeishuSheetsDeliveryError`（``definite=True``，判别口径同
:class:`lingxi.adapters.feishu_docx_delivery.FeishuDocxDeliveryError`）；响应本身
成功（``code`` 为 0）但缺失可回读标识时抛出 ``LookupError``——这种"结果不明"
不属于飞书明确拒绝。**响应缺失 ``code`` 字段本身也不当作成功**（Trace #373
H3 批 codex 外审②修复①）：``urllib_transport`` 对 ``HTTPError`` 若能从响应体
解析出 JSON 就原样返回，不看这份 JSON 里有没有 ``code``——``_data`` 因此
fail-closed，只有显式 ``0``/``"0"`` 才继续，``code`` 缺失一律抛
``FeishuSheetsDeliveryError("missing_code", definite=False)``，防止「HTTP 500 +
``{}``」被误判成功。传输层异常（连接失败、超时、JSON 解析失败）由默认传输
:func:`urllib_transport` 分类为 ``FeishuSheetsDeliveryError(definite=False)``。

## 凭据与内容边界

日志与异常消息不落 ``tenant_access_token``、请求/响应正文（表格标题、单元格
文本、``open_id``、``url``）。业务错误码只以货真价实的 ``int`` 形式拼进
``code`` 字段，不透传飞书 ``msg`` 原文——同
``feishu_docx_delivery._safe_feishu_code`` 的注入防护理由。
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

#: 决策记录 2026-08-23 裁定的授予档位：表格级「可管理」，同文档交付。
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
        super().__init__(f"飞书电子表格交付失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


def _require_https(base_url: str) -> str:
    """飞书出站必须 HTTPS：误配 ``http://`` 会把 Bearer token 明文上路。"""

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url 必须由配置注入，不得写死在代码里")
    text = base_url.strip()
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
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
    """校验用户 ``open_id`` 形状；不合法就快速失败，且不回显取到的值——理由同
    ``feishu_docx_delivery._require_user_open_id``：把群/租户标识误传成用户
    open_id，要在**发出去之前**失败，而不是把「可管理」权限授予一个错误的收件人。
    """

    text = (open_id or "").strip()
    if not text.startswith(USER_OPEN_ID_PREFIX) or len(text) <= len(USER_OPEN_ID_PREFIX):
        raise ValueError(f"open_id 必须是飞书用户 open_id（以 {USER_OPEN_ID_PREFIX} 开头），不回显收到的值")
    if any(character.isspace() for character in text):
        raise ValueError("open_id 不得包含空白字符，不回显收到的值")
    return text


def _safe_feishu_code(value: object) -> str:
    """把飞书业务错误码渲染成审计安全的分类标签。理由与
    ``feishu_docx_delivery._safe_feishu_code`` 相同：响应体是不可信的外部数据，
    只在 ``value`` 是货真价实的 ``int``（排除 ``bool``）时插值，否则退化成固定
    标签，防止响应内容注入进异常消息/审计行。
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return f"feishu_code_{value}"
    return "feishu_code_invalid"


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, *, body: Mapping[str, Any] | None = ..., token: str | None = ...
    ) -> Any: ...


def urllib_transport(method: str, url: str, *, body: Mapping[str, Any] | None = None, token: str | None = None) -> Any:
    """默认传输层：只发 HTTPS，不重试有副作用的请求（同
    ``feishu_docx_delivery.urllib_transport`` 的姿态）。
    """

    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # 地址来自受控配置且已校验 https
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
    """飞书电子表格交付：建表、查默认 sheet_id、写单元格、授予「可管理」、
    协作者读回。

    构造函数**只存参数**：不发请求、不缓存令牌（纪律同
    ``feishu_docx_delivery.LarkDocxDelivery``）。传输层由 ``transport`` 注入，
    默认是本模块的 :func:`urllib_transport`。不需要 ``tenant_domain``——建表
    响应自带 ``url``，见模块文档「与文档交付的差异点」第 1 条。
    """

    def __init__(
        self,
        *,
        base_url: str,
        tenant_access_token: Callable[[], str],
        transport: Callable[..., Any] | None = None,
    ) -> None:
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
        """飞书业务错误码非 0 → 抛出 :class:`FeishuSheetsDeliveryError`
        （``definite=True``）；这是本模块唯一判定"飞书明确拒绝"的位置，**刻意
        不做静默降级**——同 ``feishu_docx_delivery.LarkDocxDelivery._data`` 的
        理由：一旦这里被改成"记日志后继续"，所有写操作都会在飞书拒绝的情况下
        被上层误判为成功。

        **fail-closed（Trace #373 H3 批 codex 外审②修复①）**：``code`` 字段
        缺失（``None``）不当作成功放行——``urllib_transport`` 对 ``HTTPError``
        若能从响应体解析出 JSON 就原样返回（不管这份 JSON 有没有 ``code``
        字段），组合起来会出现「HTTP 500 + ``{}``」被当作成功响应、后续写值
        照常进行、最终交付空表或未更新的表格这一路径。S-W0-3 探针（#354 最新
        评论，证据等级 6）实测四个端点的真实成功响应**均带 ``code=0``**，因此
        只有显式 ``0``/``"0"`` 才视为成功；``code`` 缺失一律判「结果不明」
        （``definite=False``），交给调用方按传输层异常同等对待，不静默放行。

        变异锚点：把 ``code is None`` 这条分支删掉（退回"``None`` 也算成功"），
        本模块 ``MissingCodeTest`` 一组用例会从抛出
        ``FeishuSheetsDeliveryError`` 变红成静默返回空 dict。
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
        data = self._data(
            self._call("GET", f"{_SPREADSHEETS_V3_PATH}/{token}/sheets/query")
        )
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

    def write_values(self, spreadsheet_token: str, sheet_id: str, rows: Sequence[Sequence[str]]) -> None:
        """把 ``rows``（行×列的单元格文本）写入表格，从 ``A1`` 起按行列展开。

        ``PUT /sheets/v2/spreadsheets/{token}/values``——**注意版本号**：这是
        飞书 sheets API 已知的既有形状（建表/查询是 v3，写值是 v2），不是本模块
        选错版本号（见模块文档「与文档交付的差异点」第 2 条）。``range`` 用
        ``A1`` 起始、按最长行的列数与总行数算出结束坐标（S-W0-3 探针实测的最小
        形状是单元格 ``A1:A1``，这里推广到多行多列，仍是同一个"整块覆盖"语义：
        天然幂等，见模块文档「与文档交付的差异点」第 3 条）。

        **入参在 :meth:`list`/:meth:`list(row)` 之前先做形状校验**（Trace #373
        H3 批量审查 P2-1）：``rows`` 或其中某一行不是可迭代的列表/元组时（例如
        单行是 ``None``），直接抛本模块 ``ValueError``——不能让 ``list(row)``
        先撞上 ``TypeError`` 才失败：调用方 ``_process_sheet_claim`` 按异常类型
        分流终态（``ValueError`` → 明确 ``failed``，未列入白名单的异常类型 →
        ``uncertain``），发出任何 HTTP 请求之前的入参形状错误没有"可能已经生效"
        的空间，必须落 ``failed`` 而不是误判成结果不明。

        **矩形防御断言**（Trace #373 H3 批量审查 P1）：飞书对「``range`` 宽度
        大于某一行实际列数」这种不规则输入的真实语义本仓库从未验证过（探针只测
        过单格 ``A1:A1``），静默把不规则矩阵连同按最长行算出的 ``range`` 一起
        发出去，有写出错位数据且不被飞书拒绝的风险。上游 ``core.execution.
        document_delivery.build_sheet_request`` 已经把矩阵补齐成矩形，这里是
        第二道纵深防线：不假设上游一定守约，收到不规则矩阵直接 ``ValueError``
        失败关闭，不猜测该怎么补。
        """

        token = _require_spreadsheet_token(spreadsheet_token)
        sid = _require_sheet_id(sheet_id)
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
        """对 ``open_id`` 这个人授予表格级「可管理」（决策记录 2026-08-23 裁定的
        唯一授予档位）。

        ``POST /drive/v1/permissions/{token}/members?type=sheet``——与 docx 唯一
        差异是 ``type=sheet``（docx 是 ``type=docx``），端点、字段名完全一致，
        S-W0-3 探针实测：对个人 openid 原样接受、无降级。
        """

        token = _require_spreadsheet_token(spreadsheet_token)
        member_id = _require_user_open_id(open_id)
        self._data(
            self._call(
                "POST",
                f"/drive/v1/permissions/{token}/members",
                params={"type": SHEET_PERMISSION_TYPE},
                body={"member_type": OPENID_MEMBER_TYPE, "member_id": member_id, "perm": FULL_ACCESS_PERM},
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
            self._call("GET", f"/drive/v1/permissions/{token}/members", params={"type": SHEET_PERMISSION_TYPE})
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
