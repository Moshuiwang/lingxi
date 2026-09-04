"""花名册多维表格的只读 adapter（正式壳）。

它承接的是受控验证已经证明的读取模式（`scripts/read_feishu_bitable_association.py`
与 `adapters/feishu_bitable_association.py` 这两个**测试资产**），把其中会长期存在的
部分固定下来：分页读取、对象型单元格取文本、只取匹配链路需要的字段。

本模块分两段，都在同一个外部系统（花名册多维表格）上，因此不拆成第二个 adapter：

1. **归一化**（`field_text` / `normalize_record`）：把一条原始记录变成 `RosterRow`。
2. **分页 reader 与完整性判定**（`BitableRosterPages` / `read_roster_snapshot`，
   Issue [#52](https://github.com/Moshuiwang/lingxi/issues/52)）：按 `page_token` 读完
   整轮，并把「这一轮能不能当作快照」所需的事实结算成 :class:`RosterReadOutcome`。
   **判定「要不要替换快照」不在这里**：门槛与保旧告警在 `core/identity/roster_snapshot.py`，
   持久载体在 `adapters/postgres_roster_snapshot.py`（迁移 `0063_roster_snapshot`）。

**只剩一条读取路径**（S-B-04，消解 PR #208 二级审查 P2-1）：早先那条 legacy
``read_roster_records``（读完全部分页、只返回行）已随日报接线一并**删除**。它只回答
「行是什么」，把「花名册真的空了」和「这一轮没读完」压成同一个空元组，而保旧判定要的
恰恰是这两者的区别；两条路径并存的代价是总有调用方接到不带判定的那一条。
:func:`read_roster_snapshot` 是唯一入口。

**真实调用未验证（证据等级 L1）**，与 `adapters/feishu_directory.py`、
`adapters/feishu_group_message.py` 同一姿态：全部断言跑在注入的假传输层上。真实读取
所需的专用主体凭据自 2026-08-09 起未落盘（Issue #52 的 G-READ 判定），真实面等
bootstrap 重授权。本模块因此**不 import 任何 SDK、不读任何凭据文件、构造时不发请求**：
`access_token` 由调用方以「已就绪凭据」的形式注入。真实读取的 scope、Base 与可复跑
方式见[飞书组织快照与多维表格关联](../../../docs/技术设计/飞书组织快照与多维表格关联.md)。

边界（同上文与产品合同）：花名册是**下游人员信息表**，不是权限权威，不得据此
创建或扩大任何 Lingxi 权限；这里只把它当作「飞书 user_id → 邮箱」的查表。
同一人员 ID 的重复行**保留不去重**——实测存在，去重会把歧义变成静默的错误选择。

数据范围没有因为本次新增而扩大（`V-花名册-09`）：保留进内存的仍然只有
:data:`ROSTER_FIELD_NAMES` 这四列，完整性判定读到的其余原始列**只产生计数**，值
不进任何返回对象、日志或审计（`V-花名册-33`）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote, urlencode

from lingxi.adapters.feishu_directory import FeishuDirectoryError, urllib_transport

logger = logging.getLogger(__name__)

# 匹配链路只需要这四个字段；其余业务字段一律不读入内存，减少可识别数据的暴露面。
ROSTER_FIELD_NAMES: tuple[str, ...] = ("人员ID", "邮箱", "人员姓名", "工号")

_FIELD_TEXT_KEYS = ("text", "value", "name", "default_value", "zh_cn", "en_us")


class RosterRow(NamedTuple):
    """一行花名册记录的归一化形态，可直接作为匹配层的输入映射。"""

    personnel_id: str
    email: str
    name: str
    # 命名与匹配层输入键一致（employee_no）：曾叫 work_no，与
    # core/permission/account_match 的键名对不上，工号在接线处会静默丢失
    # （独立复查实测：该形态下全部退化为纯邮箱匹配）。
    employee_no: str
    record_id: str

    def get(self, key: str, default: object = None) -> object:
        """让本行可直接交给 ``match_galaxy_account``（它按 Mapping 的 ``get``
        取值）。NamedTuple 没有 ``get``，此前必须先 ``_asdict()``，文档却声称
        可直接组合（终轮 Codex 发现的接口不兼容）。"""

        return getattr(self, key, default)


def field_text(value: Any) -> str:
    """把多维表格的单元格值取成文本。

    多维表格的同一列在不同字段类型下可能是字符串、数字、对象或对象数组，
    受控读取中三种形态都真实出现过。
    """

    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        for child in value:
            text = field_text(child)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in _FIELD_TEXT_KEYS:
            if key in value:
                text = field_text(value[key])
                if text:
                    return text
        for child in value.values():
            text = field_text(child)
            if text:
                return text
    return ""


def normalize_record(record: Any) -> RosterRow:
    """把一条原始记录归一成 `RosterRow`；缺字段给空串，不给 `None`。

    判的是 `Mapping` 而不是 `dict`：分页 reader 在上一层已经按 `Mapping` 挡下非对象项，
    两层用不同的类型判据会让"通过了上层校验的记录在这里静默归一成空行"。

    **形状不对就响亮失败**（PR #208 二级审查 P2-1）：记录本身不是对象、或它的 ``fields``
    不是对象时，此前会静默归一成一条全空的行。持久快照落地后这条静默路径的代价变了
    ——那条全空行会**被写进快照**，在比对时表现为一个"资料被清空"的人，而源头其实只是
    给了一个形状不对的记录。这里抛 :class:`RosterReadError`（结果不明），与上一层对
    非对象项抛 ``invalid_page_item`` 同一姿态：整轮判失败、保留上一份快照，而不是让
    一条坏记录混进一份看起来正常的快照。
    """

    if not isinstance(record, Mapping):
        raise RosterReadError("invalid_page_item", definite=False)
    fields = record.get("fields", {})
    if not isinstance(fields, Mapping):
        raise RosterReadError("invalid_record_fields", definite=False)
    return RosterRow(
        personnel_id=field_text(fields.get("人员ID")),
        email=field_text(fields.get("邮箱")),
        name=field_text(fields.get("人员姓名")),
        employee_no=field_text(fields.get("工号")),
        record_id=field_text(record.get("record_id")),
    )


# ---------------------------------------------------------------------------
# 分页 reader 与完整性判定（Issue #52）
# ---------------------------------------------------------------------------

# 单页记录数。受控读取（2026-08-05）用 500 读完 1206 行，实测成立；平台上限本身
# 未回源确认，因此这里只把它作为**本地**的合法上界——配置越界在构造时就失败，
# 而不是等到当天第一次读取被飞书拒绝。
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 500

# 翻页安全上界。它是防御，不是容量规划：撞上上界是**错误**，不是"读完了"
# （受控读取 1206 行在 500/页 下占 3 页）。`page_token` 因外部异常一直非空时，
# 宁可判失败也不空转、更不把半轮结果当成一轮。
DEFAULT_MAX_PAGES = 200

# 形态校验的基准列。默认就是匹配链要用的四列（:data:`ROSTER_FIELD_NAMES`），
# 与 2026-08-05 受控读取的 38 列 / 1206 行历史参照对应的那一部分。
#
# **可配置只影响计数，不影响保留的字段集**：无论传什么列进来，返回的仍然只有
# `RosterRow` 的四个字段，其余列的**值**一次都不会离开这个函数（`V-花名册-09`）。
DEFAULT_REQUIRED_COLUMNS: tuple[str, ...] = ROSTER_FIELD_NAMES

# 重复判定的键。两个都是真实会出问题的键：人员 ID 重复会让比对层判 `AMBIGUOUS`
# （`V-花名册-06`），工号重复会让银河账号匹配落到多条上。实测 1206 行里有 1177 个
# 不同人员 ID、1197 行有工号——重复与空值**都是常态**，因此只上报计数，不做任何
# 自动取舍。
DEFAULT_DUPLICATE_KEY_FIELDS: tuple[str, ...] = ("personnel_id", "employee_no")


class RosterReadStatus(Enum):
    """一轮读取的结论。四态各自对应调用方一个明确动作。

    这四态是给持久快照层（Issue #52「源头返回空 / 失败 / 超时 / 半轮不覆盖」）用的：
    只有 :attr:`COMPLETE` 才**可能**成为候选快照，其余三态一律保留上一份快照。
    「是否替换、如何告警」由快照层决定，本模块只负责把事实结算清楚。
    """

    # 整轮读完、完整性判定通过、且有行。
    COMPLETE = "complete"
    # 整轮读完、没有任何失败，但零行。**这不是"全员离职"**，是需要人看一眼的异常。
    EMPTY_SOURCE = "empty_source"
    # 整轮读完，但完整性判定不通过（源头自报总数与累计行数不符，或某个必需列整列
    # 没有任何值）。行拿到了，可信度没有。
    INCOMPLETE = "incomplete"
    # 读取过程本身失败，未读完。具体原因见 :attr:`RosterReadOutcome.failure`。
    FAILED = "failed"


class RosterFailureKind(Enum):
    """失败的两类语义，纪律沿用 `adapters/feishu_delivery.py` 的白名单分类。

    两类都导致"保留上一份快照"，区分是给**告警与重试**用的：明确失败要人去改配置
    或权限，结果不明下一轮重试通常就好了。把结果不明说成明确失败，会让一次网络抖动
    被当成"花名册权限被回收"；反过来则会让真正被拒绝的读取每天安静地重试下去。
    """

    # 飞书完整返回了响应，且业务错误码明确不为 0：服务端明确拒绝了这次读取。
    DEFINITE = "definite"
    # 其余一切：传输异常、超时、响应体不可解析、响应形状不对、游标停滞、翻页超上限。
    # 共同点是**读到多少行是未知的**，半轮结果不能代表源头。
    INDETERMINATE = "indeterminate"


class RosterReadError(RuntimeError):
    """花名册读取失败。``code`` 供程序判断，消息里不含凭据、人员资料或 Base 标识。

    ``definite`` 的含义与 :class:`lingxi.adapters.feishu_directory.FeishuDirectoryError`
    一致，默认由 ``code`` 是否为业务错误码推出。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"花名册多维表格读取失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


class ColumnCount(NamedTuple):
    """某个必需列上"取不到文本"的行数。只有列名与计数，没有值。"""

    column: str
    rows: int


class DuplicateCount(NamedTuple):
    """某个键字段上的重复情况。只有字段名与计数，**没有任何键值**。"""

    field: str
    # 出现了不止一行的键有几个。
    keys: int
    # 这些键一共涉及多少行。
    rows: int


@dataclass(frozen=True)
class RosterIntegrity:
    """一轮读取的完整性事实。**整个对象不含任何业务字段值**（`V-花名册-33`）。

    所有序列都按固定次序生成（必需列按输入清单序、键字段按输入清单序），因此同样的
    输入永远得到同样的对象，与 ``PYTHONHASHSEED`` 无关。
    """

    row_count: int = 0
    pages_read: int = 0
    # 源头自报的总数。**飞书是否一定返回 `total` 未经回源确认**，因此允许缺失：
    # 缺失时 :attr:`total_matches_rows` 为 ``None``，而不是假装校验过。
    reported_total: int | None = None
    # ``True`` 一致 / ``False`` 不一致 / ``None`` 源头没给总数，无法判定。
    total_matches_rows: bool | None = None
    # 在**所有**行里都取不到文本的必需列。这是"列被改名或删掉"的信号：零行时不产生
    # 该结论（无行可判），否则空源会被误报成表结构漂移。
    absent_columns: tuple[str, ...] = ()
    # 逐列的空值行数。个别行缺字段是实测常态（1206 行里 1197 行有工号），只上报。
    blank_column_rows: tuple[ColumnCount, ...] = ()
    # 没有人员 ID 的行数：这些行与任何用户都对不上，比对层会直接忽略它们。
    rows_without_personnel_id: int = 0
    duplicates: tuple[DuplicateCount, ...] = ()

    @property
    def shape_ok(self) -> bool:
        """形态是否通过。**重复行不算形态问题**——它是花名册的实测常态，由比对层
        判 `AMBIGUOUS`（`V-花名册-06`）；把它算成"这轮不可用"会让快照因为两行重复
        而整轮停更。"""

        return not self.absent_columns and self.total_matches_rows is not False


@dataclass(frozen=True)
class RosterReadFailure:
    """失败事实。``partial_*`` 只是计数，用来解释"读到哪里断的"。"""

    code: str
    kind: RosterFailureKind
    partial_pages: int = 0
    partial_rows: int = 0

    @property
    def definite(self) -> bool:
        return self.kind is RosterFailureKind.DEFINITE


@dataclass(frozen=True)
class RosterReadOutcome:
    """一轮读取的完整结果，供持久快照层（原子替换 / 空源保旧）判定。

    **失败时 :attr:`rows` 恒为空**。半轮读到的行留在
    :attr:`RosterReadFailure.partial_rows` 里作为计数，不作为数据返回——把半轮行交出去
    等于把"骤减"包装成一份看起来正常的快照，而这正是空源保护要挡的那个形状。
    """

    status: RosterReadStatus
    rows: tuple[RosterRow, ...] = ()
    integrity: RosterIntegrity = RosterIntegrity()
    failure: RosterReadFailure | None = None

    @property
    def complete_nonempty(self) -> bool:
        """整轮读完、完整性通过、且非空。这是"可以拿去替换快照"的**必要条件**；
        是否真的替换、以及空源与失败如何告警，由快照层决定，不在本模块。"""

        return self.status is RosterReadStatus.COMPLETE

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实：只有状态、计数、列名与错误码。

        这个方法是"日志纪律"的落点：调用方拿它去记审计，就不会顺手把
        :attr:`rows` 序列化进去。姓名、工号、邮箱、人员 ID 一个都不在这里
        （`V-花名册-33`）。
        """

        return {
            "status": self.status.value,
            "rows": self.integrity.row_count,
            "pages": self.integrity.pages_read,
            "reported_total": self.integrity.reported_total,
            "total_matches_rows": self.integrity.total_matches_rows,
            "absent_columns": list(self.integrity.absent_columns),
            "blank_column_rows": {
                item.column: item.rows for item in self.integrity.blank_column_rows
            },
            "rows_without_personnel_id": self.integrity.rows_without_personnel_id,
            "duplicate_keys": {item.field: item.keys for item in self.integrity.duplicates},
            "duplicate_rows": {item.field: item.rows for item in self.integrity.duplicates},
            "failure_code": self.failure.code if self.failure is not None else None,
            "failure_kind": self.failure.kind.value if self.failure is not None else None,
            "partial_pages": self.failure.partial_pages if self.failure is not None else 0,
            "partial_rows": self.failure.partial_rows if self.failure is not None else 0,
        }


class RosterRecordPage(NamedTuple):
    """一页原始记录。除了记录与游标，还带一个 ``total``：完整性判定要拿源头自报的
    总数与累计行数对账，而那个数字在分页响应里，取不到就传 ``None``。"""

    records: tuple[Any, ...]
    next_page_token: str | None = None
    total: int | None = None


class RosterPageSource(Protocol):
    """按页返回花名册记录的只读传输（可注入面）。"""

    def fetch_page(self, page_token: str | None = None) -> RosterRecordPage: ...


def _require_https(base_url: object) -> str:
    """飞书出站必须 HTTPS；误配 http:// 会把 Bearer token 明文上路。不回显收到的值。"""

    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ValueError("飞书 base_url 必须以 https:// 开头（不回显收到的值）")
    return base_url.rstrip("/")


def _require_identifier(value: object, label: str) -> str:
    """校验 Base / 表标识非空且不含空白。**不回显值**：它们是外部标识，一旦进错误
    消息就会被日志、CI 输出和工单一路复制出去。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须由配置注入，不得为空（不回显收到的值）")
    text = value.strip()
    if any(character.isspace() for character in text):
        raise ValueError(f"{label}不得包含空白字符（不回显收到的值）")
    return text


class BitableRosterPages:
    """花名册多维表格的分页读取传输。

    构造函数**只存参数**：不 import SDK、不建 client、不发请求、不读凭据文件
    （同 `V-花名册-27` 的口径）。凭据以 ``access_token`` 可调用对象注入——本类拿到的
    永远是"已经就绪的短期令牌"，它既不知道令牌从哪来，也没有任何读取凭据的路径。
    每页现取一次：凭据在整轮读取中途轮换时，下一页自然用上新值。

    令牌只走 ``Authorization`` 请求头（由传输层负责），**不进 URL、不进日志、不进
    异常消息**。
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_token: str,
        table_id: str,
        access_token: Callable[[], str],
        transport: Callable[..., Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._base_url = _require_https(base_url)
        self._app_token = _require_identifier(app_token, "花名册 Base app_token")
        self._table_id = _require_identifier(table_id, "花名册表 table_id")
        if not callable(access_token):
            raise ValueError("access_token 必须是返回已就绪短期令牌的可调用对象")
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise ValueError(f"page_size 必须是 1 到 {MAX_PAGE_SIZE} 之间的整数")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size 必须是 1 到 {MAX_PAGE_SIZE} 之间的整数")
        self._access_token = access_token
        self._transport: Callable[..., Any] = transport or urllib_transport
        self._page_size = page_size

    @property
    def page_size(self) -> int:
        return self._page_size

    def _records_url(self, page_token: str | None) -> str:
        parameters: dict[str, Any] = {"page_size": self._page_size}
        if page_token:
            parameters["page_token"] = page_token
        path = (
            f"/bitable/v1/apps/{quote(self._app_token, safe='')}"
            f"/tables/{quote(self._table_id, safe='')}/records"
        )
        return f"{self._base_url}{path}?{urlencode(parameters)}"

    def _token(self) -> str:
        """取已就绪令牌。

        **provider 自己抛出的异常原样上抛，不折成 :class:`RosterReadError`**：拿不到
        凭据是本侧的配置 / 授权问题，不是"花名册源头异常"。把两者混在一起，会让日报和
        告警指向错误的地方（去查花名册，而问题在凭据）。只有"provider 正常返回却给了
        空值"这一种才算读取失败——那时确实无法判断源头状态。
        """

        token = self._access_token()
        if not isinstance(token, str) or not token:
            raise RosterReadError("access_token_missing", definite=False)
        return token

    def fetch_page(self, page_token: str | None = None) -> RosterRecordPage:
        """读一页；把飞书的响应形状与错误码翻译成本模块的语义，不泄漏给上层。"""

        token = self._token()
        try:
            response = self._transport("GET", self._records_url(page_token), body=None, token=token)
        except FeishuDirectoryError as error:
            # 共用同一份 urllib 传输（全仓库只有一份），但它的错误类型属于目录 adapter；
            # 在边界上翻译成花名册自己的错误类型，`definite` 语义原样保留。
            raise RosterReadError(error.code, definite=error.definite) from error

        if not isinstance(response, Mapping):
            raise RosterReadError("invalid_response_shape", definite=False)
        code = response.get("code")
        if code not in (None, 0, "0"):
            # 服务端完整返回并明确拒绝：明确失败。
            raise RosterReadError(f"feishu_code_{code}")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise RosterReadError("invalid_response_shape", definite=False)
        items = data.get("items")
        if not isinstance(items, list):
            raise RosterReadError("items_missing", definite=False)
        for item in items:
            if not isinstance(item, Mapping):
                # 静默丢弃非对象项会让被丢的行躲过完整性校验、半轮被标成完整
                # （`feishu_directory` 的同一处教训）。
                raise RosterReadError("invalid_page_item", definite=False)

        next_page_token: str | None = None
        if data.get("has_more") is True:
            candidate = data.get("page_token")
            if not isinstance(candidate, str) or not candidate or candidate == page_token:
                # 源头明确说还有数据，却没给可用游标：这一轮读到多少行是未知的。
                raise RosterReadError("pagination_stalled", definite=False)
            next_page_token = candidate

        total = data.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            total = None
        return RosterRecordPage(tuple(items), next_page_token, total)


def _integrity(
    *,
    rows: Sequence[RosterRow],
    blank_counts: Mapping[str, int],
    required_columns: Sequence[str],
    duplicate_key_fields: Sequence[str],
    pages_read: int,
    reported_total: int | None,
) -> RosterIntegrity:
    """把一轮读取结算成完整性事实。纯计数，不看任何值。"""

    row_count = len(rows)
    duplicates: list[DuplicateCount] = []
    for field in duplicate_key_fields:
        occurrences: dict[str, int] = {}
        for row in rows:
            key = _row_text(row, field)
            if not key:
                # 空键不是重复：实测 1206 行里有 9 行没有工号，把它们算成"一组九行重复"
                # 会让每一轮都报出一个不存在的问题（`V-花名册-07` 的同一条不对称口径）。
                continue
            occurrences[key] = occurrences.get(key, 0) + 1
        repeated = [count for count in occurrences.values() if count > 1]
        duplicates.append(DuplicateCount(field, len(repeated), sum(repeated)))

    return RosterIntegrity(
        row_count=row_count,
        pages_read=pages_read,
        reported_total=reported_total,
        total_matches_rows=None if reported_total is None else reported_total == row_count,
        # 零行时不判定列缺失：没有行就没有证据，否则空源会被同时报成表结构漂移。
        absent_columns=(
            tuple(column for column in required_columns if blank_counts.get(column, 0) == row_count)
            if row_count
            else ()
        ),
        blank_column_rows=tuple(
            ColumnCount(column, blank_counts.get(column, 0)) for column in required_columns
        ),
        rows_without_personnel_id=sum(1 for row in rows if not _row_text(row, "personnel_id")),
        duplicates=tuple(duplicates),
    )


def _row_text(row: Any, field: str) -> str:
    """取归一化行上的一个字段并归一为可比较文本。"""

    value = row.get(field) if hasattr(row, "get") else getattr(row, field, "")
    return value.strip() if isinstance(value, str) else ""


def read_roster_snapshot(
    source: RosterPageSource,
    *,
    required_columns: Sequence[str] = DEFAULT_REQUIRED_COLUMNS,
    duplicate_key_fields: Sequence[str] = DEFAULT_DUPLICATE_KEY_FIELDS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> RosterReadOutcome:
    """读完整轮花名册，并结算"这一轮能不能当作快照"所需的全部事实。

    **本函数不抛外部失败**：预期的读取失败（:class:`RosterReadError`）被结算成
    :attr:`RosterReadStatus.FAILED` 的结果对象，因为调用方要的是"保留上一份快照并
    带着原因告警"，而不是一个会把整轮职责带走的异常。未预期的异常一律不捕获、原样
    向上传播（纪律同 `adapters/feishu_delivery.py`）：把它们也吞成"读取失败"会让真正的
    缺陷伪装成源头异常，每天安静地重试下去。

    四态的判定次序是刻意的：**先判完整性、再判空**。源头自报 1206 行却一行没给，是
    "读取不完整"而不是"花名册空了"——两者都保旧，但报给管理员的原因不能反。
    """

    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise ValueError("max_pages 必须是正整数")
    # 去重且保序（PR #208 二级审查 P3-1）：同一列在清单里出现两次时，下面的空值统计
    # 会对同一行加两次，于是"整列都取不到值"的判据（计数 == 行数）永远不成立，
    # **整列缺失的检测被静默击穿**——列被改名或删掉时这一轮会被判成 COMPLETE 并顶掉
    # 好快照。`dict.fromkeys` 保留首次出现的次序，让完整性事实的输出次序仍然稳定。
    required = tuple(dict.fromkeys(required_columns))
    key_fields = tuple(dict.fromkeys(duplicate_key_fields))

    rows: list[RosterRow] = []
    blank_counts: dict[str, int] = {column: 0 for column in required}
    pages_read = 0
    reported_total: int | None = None
    page_token: str | None = None
    failure: RosterReadFailure | None = None

    try:
        for _ in range(max_pages):
            page = source.fetch_page(page_token)
            pages_read += 1
            if page.total is not None:
                reported_total = page.total
            for record in page.records:
                if not isinstance(record, Mapping):
                    raise RosterReadError("invalid_page_item", definite=False)
                fields = record.get("fields")
                fields = fields if isinstance(fields, Mapping) else {}
                for column in required:
                    # 只统计"取不到文本"，**值不出这个循环**：形态校验可以配置到任意
                    # 一列，保留下来的仍然只有 RosterRow 的四个字段（`V-花名册-09`）。
                    if not field_text(fields.get(column)):
                        blank_counts[column] += 1
                rows.append(normalize_record(record))
            if not page.next_page_token:
                break
            if page.next_page_token == page_token:
                # 游标原地打转：再读一页只会拿到同一批行，行数会越滚越大而永远读不完。
                raise RosterReadError("pagination_stalled", definite=False)
            page_token = page.next_page_token
        else:
            raise RosterReadError("pagination_limit", definite=False)
    except RosterReadError as error:
        failure = RosterReadFailure(
            code=error.code,
            kind=RosterFailureKind.DEFINITE if error.definite else RosterFailureKind.INDETERMINATE,
            partial_pages=pages_read,
            partial_rows=len(rows),
        )

    if failure is not None:
        outcome = RosterReadOutcome(
            status=RosterReadStatus.FAILED,
            # 半轮的行一行都不返回，理由见 RosterReadOutcome 的类文档。
            rows=(),
            integrity=RosterIntegrity(pages_read=pages_read),
            failure=failure,
        )
    else:
        integrity = _integrity(
            rows=rows,
            blank_counts=blank_counts,
            required_columns=required,
            duplicate_key_fields=key_fields,
            pages_read=pages_read,
            reported_total=reported_total,
        )
        if not integrity.shape_ok:
            status = RosterReadStatus.INCOMPLETE
        elif not rows:
            status = RosterReadStatus.EMPTY_SOURCE
        else:
            status = RosterReadStatus.COMPLETE
        outcome = RosterReadOutcome(status=status, rows=tuple(rows), integrity=integrity)

    # 只记计数、列名与错误码，依据是 `V-花名册-33`：**审计与日志**不含花名册字段值。
    # （此前这行援引的是"日报脱敏口径"，那条口径已被产品负责人 2026-08-08 的 D2 裁定
    # 覆盖——受控管理群的日报**可以**展示姓名 / 工号 / 邮箱原值。日志侧的约束与日报
    # 正文无关、也没有随之放宽：日志会流向排障、CI 输出与工单，那些地方不是受控管理群。）
    logger.info("花名册读取结束 %s", outcome.audit_facts())
    return outcome
