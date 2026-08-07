"""每日花名册资料比对：花名册当前值 vs 建档时刻存档的三字段。

比对模型由产品负责人 2026-08-06 拍板为**选项 A**（[Issue #52]
(https://github.com/Moshuiwang/lingxi/issues/52)）：基线不是「昨天的快照」，而是
`app_user` 上建档时刻就存下来的 `display_name` / `employee_no` / `email` 三个字段
（[数据库设计第三节](../../../../docs/技术设计/数据库设计.md)：「不需要额外的历史表」）。
因此本切片**零新表、零迁移**，也没有保留清理与 CASCADE 的联动。

已知代价（决策时已接受，PR 登记为 R1）：同一处漂移在人工处理前会**每天重复**出现在
日报里。日对日快照能去掉这份噪声，但代价是新增一张长期存放可识别用户内容的表。

三条容易写错、且每一条都有对应断言的地方：

1. **人员 ID 用完整值查表，绝不截断。** `open_user_id` 的 6 位前缀在 710 人实测中有
   57 组碰撞（[当前能力](../../../../docs/当前能力.md)），截断比对会把两个人判成同一个人
   （`V-花名册-05`）。
2. **空值口径是不对称的**（`V-花名册-07`）。存档为空的字段不参与比对——否则一个建档时
   就没留邮箱的用户会天天被误报；但存档有值、花名册变空**必须报**，丢值往往是转交前兆。
3. **输出对象里没有任何字段值**（`V-花名册-08`）。日报「只展示脱敏摘要」这条产品承诺
   （产品合同 :75，决策二＝甲）在这里由数据结构保证，而不是靠渲染层记得别写：
   :class:`PersonDiff` 只有内部标识、变化的**字段名**和转交标志，值根本没进来过。

本模块是 `core/`：纯函数、无 I/O、不 import `adapters/`，同样输入永远得到同样输出
（`V-花名册-31` 第③面要求重启后当日重发的载荷与首次逐字段一致，因此输出顺序也不能
依赖集合迭代序——凡是进入结果的序列都按固定次序生成）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

# 参与比对的三个存档字段。这个**元组的顺序**同时是日报里字段名的输出顺序：
# 用元组而不是集合，是为了让输出与 `PYTHONHASHSEED` 无关。
ARCHIVED_FIELDS: tuple[str, ...] = ("display_name", "employee_no", "email")

# 字段名到中文标签。日报里出现的是**字段名**，不是字段值。
FIELD_LABELS: Mapping[str, str] = {
    "display_name": "姓名",
    "employee_no": "工号",
    "email": "邮箱",
}

# 存档字段名 → 花名册行里的键名。花名册行的键沿用
# `adapters/feishu_roster_bitable.RosterRow` 的字段名，本模块不认识多维表格的中文列名。
#
# **这张表只有三项，是本切片数据范围的边界。** 部门与账号状态即便出现在输入行里也
# 不会进差异集（`V-花名册-09`）：部门属组织快照持续同步的范畴，飞书在职状态按设计
# 「按需回读、不落库」（数据库设计 :150）。往这里加键＝扩大读取面，须先改定案。
ROSTER_KEYS: Mapping[str, str] = {
    "display_name": "name",
    "employee_no": "employee_no",
    "email": "email",
}

# 转交迹象＝姓名与邮箱**同时**变化。工号不参与：工号是匹配银河的主键，它变化时
# 更可能是花名册补录而不是换人；姓名或邮箱单独变化也不算（`V-花名册-04`）。
HANDOVER_FIELDS: frozenset[str] = frozenset({"display_name", "email"})


class DiffKind(Enum):
    """一条日报条目的类别。

    A 方案下**没有「新增人员」这一态**：比对集由 `app_user` 里已开通的用户决定，
    花名册里多出来的人不是 Lingxi 的用户，本系统对他们没有任何判断（`V-花名册-02`）。
    """

    CHANGED = "changed"
    REMOVED = "removed"
    # 同一人员 ID 在花名册里有多行。实测存在，因此**不静默取任意一行**比对：
    # 取哪一行都是在猜，猜错会把「资料没变」报成变化、或把变化吞掉（`V-花名册-06`）。
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ArchivedIdentity:
    """一名已开通用户的比对基线：标识 + 建档时刻存档的三字段。

    `personnel_id` 是花名册「人员ID」列对应的飞书 `user_id`（当前能力实测：多维表格
    人员ID 与组织快照 `user_id` 精确命中 710 个不同值），也就是 `app_user.feishu_user_id`。
    """

    app_user_id: str
    personnel_id: str
    display_name: str = ""
    employee_no: str = ""
    email: str = ""


@dataclass(frozen=True)
class PersonDiff:
    """一条差异。**刻意不带任何字段值**（`V-花名册-08`）。

    往这个类加一个 `old_value` / `new_value` 就等于推翻决策二＝甲，把已开通用户的
    真实姓名、工号、邮箱投进管理群。
    """

    app_user_id: str
    kind: DiffKind
    # 变化的字段名，按 :data:`ARCHIVED_FIELDS` 的固定次序排列。
    changed_fields: tuple[str, ...] = ()
    handover: bool = False


@dataclass(frozen=True)
class RosterAuditReport:
    entries: tuple[PersonDiff, ...] = ()
    # 本轮纳入比对的已开通用户数。只是计数，不含任何人的资料。
    examined: int = 0

    @property
    def is_empty(self) -> bool:
        """没有任何差异。空差异日不发日报，只记一条审计（`V-花名册-25`）。"""

        return not self.entries

    @property
    def handover_count(self) -> int:
        return sum(1 for entry in self.entries if entry.handover)

    @property
    def removed_count(self) -> int:
        return sum(1 for entry in self.entries if entry.kind is DiffKind.REMOVED)

    @property
    def ambiguous_count(self) -> int:
        return sum(1 for entry in self.entries if entry.kind is DiffKind.AMBIGUOUS)


def _text(value: object) -> str:
    """取成可比较的文本。`None` 与空白一律归一为空串，不产生 ``"None"``。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _field_changed(archived: ArchivedIdentity, row: Mapping[str, object], field: str) -> bool:
    """单个字段是否变化。不对称空值口径见模块文档第 2 条。"""

    stored = _text(getattr(archived, field))
    if not stored:
        # 存档就没有这个字段：没有基线就没有「变化」可言。缺邮箱的用户不得天天误报。
        return False
    # 花名册当前值为空时走的也是这一行：空 != 非空 ⇒ 报变化。丢值必须报。
    return _text(row.get(ROSTER_KEYS[field])) != stored


def compare_roster(
    archived: Sequence[ArchivedIdentity],
    roster_rows: Sequence[Mapping[str, object]],
) -> RosterAuditReport:
    """比对已开通用户的存档三字段与花名册当前值。

    `roster_rows` 是 `adapters.feishu_roster_bitable.RosterRow` 序列（它实现了 `get`），
    也接受任何同键名的映射。行里携带的其他键（部门、账号状态等）一律被忽略。

    比对集的过滤（`provisioning_state='active'` 且 `account_state` 不在删除态）在
    读取层的 SQL 里完成，本函数只对拿到的基线逐个判断——它不知道也不该知道状态字段。
    """

    # 按完整人员 ID 建索引。计数与取行分开：重复行只让计数变大，不让任何一行被选中。
    occurrences: dict[str, int] = {}
    first_row: dict[str, Mapping[str, object]] = {}
    for row in roster_rows:
        key = _text(row.get("personnel_id"))
        if not key:
            # 没有人员 ID 的行无法与任何用户对上，直接丢弃；它不影响任何人的判定。
            continue
        occurrences[key] = occurrences.get(key, 0) + 1
        first_row.setdefault(key, row)

    entries: list[PersonDiff] = []
    # 按内部标识排序：输出顺序必须只由输入决定，不能随字典或集合的迭代序变化。
    for person in sorted(archived, key=lambda item: item.app_user_id):
        count = occurrences.get(person.personnel_id, 0)
        if count == 0:
            # 花名册里查无此人。只上报，不触发任何自动动作（`V-花名册-26`）。
            entries.append(PersonDiff(person.app_user_id, DiffKind.REMOVED))
            continue
        if count > 1:
            entries.append(PersonDiff(person.app_user_id, DiffKind.AMBIGUOUS))
            continue
        row = first_row[person.personnel_id]
        changed = tuple(field for field in ARCHIVED_FIELDS if _field_changed(person, row, field))
        if changed:
            entries.append(
                PersonDiff(
                    person.app_user_id,
                    DiffKind.CHANGED,
                    changed,
                    # `issubset` 而不是相交：只改姓名、只改邮箱都不算转交。
                    HANDOVER_FIELDS.issubset(changed),
                )
            )

    return RosterAuditReport(tuple(entries), examined=len(archived))
