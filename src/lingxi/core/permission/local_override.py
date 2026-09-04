"""本地权限覆盖的纯逻辑：条目类型、同键冲突判定、供聚合复用的序列化辅助。

[Issue #319](https://github.com/Moshuiwang/lingxi/issues/319) 的 S-P-1a（编排者拟稿，
产品负责人 2026-08-26 裁定推翻 [2026-08-24 决策记录](../../../../docs/决策记录/2026-08-24-管理员职责集与银河外权限动作边界.md)
第 4 条）。本模块只回答「一批本地覆盖条目最终生效成什么」这一个问题，**零 I/O**：
条目从哪里来（:mod:`lingxi.adapters.postgres_local_permission`）、聚合点怎么把结果
与银河翻译结果取并集（S-P-3，尚未接线）都不在这里。

## 语义定稿：`suppress` 赢

真实权限 `= (银河翻译结果 ∪ 本地授权) − 本地抑制`（#319 正文，抑制优先级最高）。
展开到单个 `(company_id, metric_name)` 键上：如果一个用户在同一个键上**同时**有一条
生效的 `grant` 与一条生效的 `suppress`（两者按迁移 ``0072`` 的部分唯一索引允许共存，
索引按 ``direction`` 分区），最终这个键必须只出现在「抑制」结果里，不出现在「授权」
结果里——:func:`resolve_local_overrides` 就是把这条规则钉成一个可以被指着看、也能被
改坏的函数，供 S-P-3 聚合直接复用，不需要重新发明一次。

这条规则**不是**"从并集里减去抑制"这个更大公式本身（那需要银河翻译结果，本模块
拿不到也不需要拿到）；它只处理"本地覆盖内部，grant 和 suppress 打架时听谁的"这一个
更小、更纯的子问题。上层公式即使不调用本函数、只是分别对 `grants`/`suppressions`
两个集合做 `(银河 ∪ grants) - suppressions` 集合运算，数学上也会得到同一个结果——
本函数存在的意义是把这条隐含在集合代数里的规则显式化：`grants` 字段本身就已经不
包含被同键抑制覆盖的条目，调用方读 `ResolvedLocalOverrides.grants` 时看到的就是
"本地净授权"，不需要自己再对着 `suppressions` 做一次减法才能回答"这个人到底被
本地授权了什么"这类审计/展示类问题。

## 为什么不在这里做「大小写/全半角归一」

与 :mod:`lingxi.core.permission.publish_row` 的「零归一」纪律同一姿态：
``metric_name`` 要与翻译层（:mod:`lingxi.core.permission.metric_translation`）
产出的指标名逐字匹配，问数 MCP 本身逐字匹配大小写与全半角。本模块提前做任何
``strip``/``casefold``/全半角转换，都会在将来某个指标名两侧只有一处被归一时，
制造一次静默的错范围——方向不可控，因此干脆一次都不做。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class OverrideDirection(str, Enum):
    """迁移 ``0072`` ``direction`` 列的两个取值，字符串枚举与列值逐字对应。"""

    GRANT = "grant"
    SUPPRESS = "suppress"


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or value.strip() == ""


@dataclass(frozen=True)
class LocalPermissionOverrideEntry:
    """一条本地权限覆盖条目（迁移 ``0072`` 一行的内存表示）。

    只表示**当前生效**（``entry_status='active'``）的条目：撤销状态、撤销时间、
    撤销所用确认卡等历史字段不在这里——那些是审计与适配器读写层的关注点
    （:mod:`lingxi.adapters.postgres_local_permission`），本聚合输入不需要知道
    一条条目"曾经"处于什么状态，只需要知道它"现在"生效且内容是什么。

    ``__post_init__`` 与全仓 fail-closed 不变量同一姿态
    （:class:`lingxi.core.permission.publish_row.PermissionAggregate` 是同型先例）：
    宁可在构造时响亮失败，也不让一条残缺条目（例如空原因、空发起人）流进聚合。
    """

    user_id: str
    direction: OverrideDirection
    company_id: str
    metric_name: str
    reason: str
    initiated_by_open_id: str
    pending_action_id: str
    created_at: datetime
    # #493 职位+公司范围授权的来源信息。展开后的每条公司×指标行共享同一
    # ``permission_group_id``；旧的逐指标行保持 None，不做历史迁移。
    position_name: str | None = None
    company_scope: str | None = None
    permission_group_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.direction, OverrideDirection):
            raise ValueError("本地权限覆盖条目的 direction 必须是 OverrideDirection 枚举")
        for field_name in (
            "user_id",
            "company_id",
            "metric_name",
            "reason",
            "initiated_by_open_id",
            "pending_action_id",
        ):
            if _blank(getattr(self, field_name)):
                raise ValueError(f"本地权限覆盖条目字段 {field_name} 不得为空或空白")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            # 与 publish_row.format_updated_at 同一条纪律：时间一律 UTC，naive 时间
            # 会让跨时区部署对"这条覆盖是什么时候生效的"产生歧义。
            raise ValueError("本地权限覆盖条目的 created_at 必须带时区")
        for field_name in ("position_name", "company_scope", "permission_group_id"):
            value = getattr(self, field_name)
            if value is not None and _blank(value):
                raise ValueError(f"本地权限覆盖条目的字段 {field_name} 不得为空白")

    @property
    def key(self) -> tuple[str, str]:
        """本地覆盖参与集合运算的最小维度：``(公司ID, 指标名)``。"""

        return (self.company_id, self.metric_name)


@dataclass(frozen=True)
class ResolvedLocalOverrides:
    """一个用户的本地覆盖，按「同键冲突判定」（`suppress` 赢）解决之后的结果。

    :attr:`grants` 与 :attr:`suppressions` 互斥（由 ``__post_init__`` 强制）：
    一旦某个键同时出现在两侧，构造过程本身（:func:`resolve_local_overrides`）
    已经把它从 ``grants`` 里剔除，这里的校验是纵深防线——防止将来有人绕过
    :func:`resolve_local_overrides` 直接手拼一个自相矛盾的实例。
    """

    grants: frozenset[tuple[str, str]]
    suppressions: frozenset[tuple[str, str]]

    def __post_init__(self) -> None:
        overlap = self.grants & self.suppressions
        if overlap:
            raise ValueError(
                "本地覆盖的授权集合与抑制集合不得有交集：suppress 必须赢，这些键不应同时出现在两侧"
            )


def resolve_local_overrides(
    *, user_id: str, entries: Iterable[LocalPermissionOverrideEntry]
) -> ResolvedLocalOverrides:
    """把一个用户的全部生效本地覆盖条目，解决成两个互斥的集合。

    **去重**：多条条目落在同一个 ``(direction, company_id, metric_name)`` 上
    （例如管理员对同一笔授权重复确认，或适配器读路径意外重复返回同一行）时，
    集合运算天然折叠成一条——这不是本函数专门做的一步，是 ``frozenset`` 的
    既有性质，但值得写在这里：调用方不需要在传入前自己先去重。

    **冲突判定**：同一个 ``(company_id, metric_name)`` 键如果同时出现在
    ``grant`` 与 ``suppress`` 两侧，``suppress`` 赢——最终只出现在
    :attr:`ResolvedLocalOverrides.suppressions`，从 ``grants`` 里剔除。

    ``user_id`` 是显式参数而不是从 ``entries`` 里现取：所有传入条目的
    ``user_id`` 必须与它一致，不一致立即拒绝（fail-closed）——这条校验挡的是
    "调用方按用户取数时不小心把两个用户的条目混进同一次聚合"，那类混入如果
    不被发现，后果是**跨用户的权限串扰**，比"忘了传 user_id 从而无法校验"更
    危险，因此宁可要求调用方显式声明它期望的 user_id，用不匹配的条目让函数
    响亮失败，也不去猜"以第一条条目的 user_id 为准"。
    """

    normalized_user = user_id.strip() if isinstance(user_id, str) else ""
    if not normalized_user:
        raise ValueError("聚合本地覆盖前必须指定非空 user_id")

    grants: set[tuple[str, str]] = set()
    suppressions: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.user_id != normalized_user:
            raise ValueError(
                "本地覆盖条目的 user_id 与聚合目标不一致：聚合必须按用户隔离，"
                "不得把别的用户的条目混入同一次解析"
            )
        if entry.direction is OverrideDirection.SUPPRESS:
            suppressions.add(entry.key)
        else:
            grants.add(entry.key)

    # suppress 赢：净授权集合里剔除同时被抑制的键（模块文档「语义定稿」）。
    grants -= suppressions
    return ResolvedLocalOverrides(grants=frozenset(grants), suppressions=frozenset(suppressions))


def to_company_metric_map(pairs: frozenset[tuple[str, str]]) -> dict[str, tuple[str, ...]]:
    """把一组 ``(company_id, metric_name)`` 对转成 ``{公司ID: (指标名, …)}`` 形状。

    与 :func:`lingxi.core.permission.metric_translation.translate_company_functions`
    的产出、以及 :func:`lingxi.core.permission.publish_row.
    serialize_translated_permissions` 的输入同一粒度——供 S-P-3 聚合把本地覆盖与
    银河翻译结果按同一形状做并集/减集运算，不需要在聚合点重新写一次分组逻辑。

    每个公司下的指标名按字符串排序去重返回：与 ``publish_row.aggregate_permission``
    对 ``functions``/``companies`` 的既有排序纪律同一姿态——恒定顺序是"同一份内容
    永远序列化成同一串字节"这条更上层保证（``publish_row`` 模块文档）的前提之一，
    这里提前满足它，不留给序列化那一层去补。
    """

    grouped: dict[str, set[str]] = {}
    for company_id, metric_name in pairs:
        grouped.setdefault(company_id, set()).add(metric_name)
    return {company: tuple(sorted(metrics)) for company, metrics in grouped.items()}


def audit_fields(entry: LocalPermissionOverrideEntry) -> dict[str, Any]:
    """一条本地覆盖条目的**完整**审计字段。

    与 :meth:`lingxi.core.permission.publish_row.PermissionAggregate.audit_facts`
    刻意不同——那个方法只留计数与机器编号，主动排除任何可能牵涉真实人员资料的值；
    本函数反过来**全量**留痕，因为 [#319](https://github.com/Moshuiwang/lingxi/issues/319)
    「审计归属设计」明确要求"发起人、目标用户、授权内容、原因文本、确认卡留痕、
    时间戳——全量入审计"，这正是本地覆盖机制存在的合规前提："谁批准他看这些数据"
    必须能被追溯到唯一管理员本人与那一次确认卡，留一半事实等于没有回答这个问题。

    这里出现的字段没有一个是 roster 类人员资料（真实姓名/工号/邮箱一个都不在
    :class:`LocalPermissionOverrideEntry` 里）：``user_id``/``pending_action_id``
    是 Lingxi 内部不透明标识，``initiated_by_open_id`` 是飞书平台标识，两者与
    ``pending_action`` 现有审计（`adapters/postgres_pending_action.py` 的
    ``confirm()``/``cancel()``）把 ``target_open_id``/``initiated_by_open_id``
    直接写入审计是同一惯例。
    """

    fields = {
        "user_id": entry.user_id,
        "direction": entry.direction.value,
        "company_id": entry.company_id,
        "metric_name": entry.metric_name,
        "reason": entry.reason,
        "initiated_by_open_id": entry.initiated_by_open_id,
        "pending_action_id": entry.pending_action_id,
        "created_at": entry.created_at.isoformat(),
    }
    for name in ("position_name", "company_scope", "permission_group_id"):
        value = getattr(entry, name)
        if value is not None:
            fields[name] = value
    return fields
