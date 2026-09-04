"""本地权限覆盖的纯逻辑：条目类型、同键冲突判定、供聚合复用的序列化辅助。

只回答「一批本地覆盖条目最终生效成什么」这一个问题，零 I/O：条目从哪里来、聚合点
怎么把结果与银河翻译结果取并集都不在这里。语义定稿：真实权限 = (银河翻译结果 ∪
本地授权) − 本地抑制，抑制优先级最高。展开到
单个 ``(company_id, metric_name)`` 键上：如果同一个键同时有生效的 grant 与
suppress（迁移的部分唯一索引允许共存），最终这个键只出现在抑制结果里
（:func:`resolve_local_overrides`）。这不是"从并集里减去抑制"那个更大公式本身
（本模块拿不到银河翻译结果），只处理本地覆盖内部 grant/suppress 打架时听谁的这个
更小的子问题——``grants`` 字段读出来就已经是"本地净授权"，调用方不需要再对着
``suppressions`` 做一次减法。

不在这里做大小写/全半角归一：``metric_name`` 要与翻译层产出的指标名逐字匹配，
提前归一会在将来某处只有一侧被归一时制造一次静默的错范围。
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

    只表示当前生效（``entry_status='active'``）的条目：撤销状态、撤销时间等
    历史字段不在这里，是审计与适配器读写层的关注点。构造时响亮失败（见
    ``__post_init__``），不让一条残缺条目（例如空原因、空发起人）流进聚合。
    """

    user_id: str
    direction: OverrideDirection
    company_id: str
    metric_name: str
    reason: str
    initiated_by_open_id: str
    pending_action_id: str
    created_at: datetime
    # 职位+公司范围授权的来源信息。展开后的每条公司×指标行共享同一
    # ``permission_group_id``；旧的逐指标行保持 None，不做历史迁移。
    position_name: str | None = None
    company_scope: str | None = None
    permission_group_id: str | None = None

    def __post_init__(self) -> None:
        """构造期校验：字段不得为空白、时间必须带时区，否则响亮失败。"""
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
        """构造期校验：grants 与 suppressions 不得有交集。"""
        overlap = self.grants & self.suppressions
        if overlap:
            raise ValueError(
                "本地覆盖的授权集合与抑制集合不得有交集：suppress 必须赢，这些键不应同时出现在两侧"
            )


def resolve_local_overrides(
    *, user_id: str, entries: Iterable[LocalPermissionOverrideEntry]
) -> ResolvedLocalOverrides:
    """把一个用户的全部生效本地覆盖条目，解决成两个互斥的集合。

    去重是 ``frozenset`` 的既有性质，调用方不需要先去重。冲突判定：同一个
    ``(company_id, metric_name)`` 键同时出现在 grant 与 suppress 两侧时，
    suppress 赢，最终只出现在 :attr:`ResolvedLocalOverrides.suppressions`。

    ``user_id`` 是显式参数而不是从 ``entries`` 里现取：条目的 ``user_id`` 不一致
    立即拒绝——挡的是"把两个用户的条目混进同一次聚合"，那会造成跨用户的权限
    串扰，宁可响亮失败，也不去猜"以第一条条目的 user_id 为准"。
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

    与 :func:`~lingxi.core.permission.metric_translation.translate_company_functions`
    的产出同一粒度，供聚合把本地覆盖与银河翻译结果按同一形状做并集/减集运算。
    每个公司下的指标名按字符串排序去重返回：恒定顺序是"同一份内容永远序列化成
    同一串字节"这条上层保证的前提，这里提前满足，不留给序列化那一层去补。
    """
    grouped: dict[str, set[str]] = {}
    for company_id, metric_name in pairs:
        grouped.setdefault(company_id, set()).add(metric_name)
    return {company: tuple(sorted(metrics)) for company, metrics in grouped.items()}


def audit_fields(entry: LocalPermissionOverrideEntry) -> dict[str, Any]:
    """一条本地覆盖条目的完整审计字段。

    与 :meth:`~lingxi.core.permission.publish_row.PermissionAggregate.audit_facts`
    刻意不同——那个方法只留计数与机器编号；本函数全量留痕，因为"谁批准他看这些
    数据"必须能被追溯到唯一管理员本人与那一次确认卡，留一半事实等于没有回答
    这个问题。这里出现的字段没有一个是 roster 类人员资料（真实姓名/工号/邮箱
    一个都不在 :class:`LocalPermissionOverrideEntry` 里）：``user_id``/
    ``pending_action_id`` 是 Lingxi 内部不透明标识，``initiated_by_open_id``
    是飞书平台标识。
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
