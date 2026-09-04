"""待确认操作：管理员写动作的通用状态机（``prepare``/``confirm``/``cancel``）。

只有类型与纯函数，没有 I/O——真实读写住在 ``adapters/postgres_pending_action.py``；
与 ``core/admin/registry.py`` 同一层次划分：判定逻辑在这里，判定需要的外部数据（登记表
条目、目标当前状态、时钟）由调用方（adapter）读取后作为参数传入（代码框架第二节）。
字段与迁移 ``0068_pending_action`` 一一对应；机制性质的"为什么"写在那个迁移文件的
头部，不在此重复。

prepare/confirm/cancel 是三个自由函数而不是一个大状态机类：三个决策点各自需要的输入
形状不同（prepare 只看当前目标状态；confirm 需要登记表条目 + 目标当前状态 + 时钟；
cancel 只需要发起人核对 + 时钟），合成一个类需要一个大分支把三种调用都装进同一个
接口，反而让每种调用要传的"用不上的字段"变多。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from lingxi.core.admin.registry import AdminRegistryEntry, AdminRole, is_authorized_admin

#: 待确认操作十分钟内必须由发起管理员本人确认或取消，否则惰性过期（见迁移 0068
#: 文件头部「为什么不做本地行的过期后台清理」）。十分钟是私聊单人确认卡片的合理
#: 窗口——太短会让管理员来不及看完卡片就点错，太长会让"已经不该继续有效"的操作
#: 停留过久（合同：过期是安全终态之一，不是便利特性，取短不取长）。
PENDING_ACTION_TTL_SECONDS = 600


class PendingActionType(str, Enum):
    """待确认操作支持的写动作。

    取值即数据库 ``action_type`` 列，两处一致靠约定（与 ``AdminRole`` 相同的
    取舍）。后三个成员（本地权限授权/抑制/收回）与前两个 MVP 写动作共用同一套
    prepare/confirm 骨架，差异见 ``adapters/postgres_pending_action.py`` 模块
    文档「本地权限收回（revoke）如何复用同一套机制」。
    """

    SUSPEND_USER = "suspend_user"
    RESUME_USER = "resume_user"
    LOCAL_PERMISSION_GRANT = "local_permission_grant"
    LOCAL_PERMISSION_SUPPRESS = "local_permission_suppress"
    LOCAL_PERMISSION_REVOKE = "local_permission_revoke"


#: 本地权限动作类型的集合（授权/抑制/收回三者），供多处"这是不是一个本地权限
#: 动作"的判断复用，不在各处各自枚举三个字面量——未来新增第四个本地权限动作
#: 类型时只需要改这一处。
LOCAL_PERMISSION_ACTION_TYPES: frozenset[PendingActionType] = frozenset(
    {
        PendingActionType.LOCAL_PERMISSION_GRANT,
        PendingActionType.LOCAL_PERMISSION_SUPPRESS,
        PendingActionType.LOCAL_PERMISSION_REVOKE,
    }
)


class PendingActionStatus(str, Enum):
    """待确认操作的状态机取值，与迁移 ``0068`` 的 CHECK 一致。"""

    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


#: 四个终态；``PENDING`` 是唯一允许再次决策的状态。
TERMINAL_STATUSES = frozenset(
    {
        PendingActionStatus.EXECUTED,
        PendingActionStatus.CANCELLED,
        PendingActionStatus.EXPIRED,
        PendingActionStatus.FAILED,
    }
)

#: 每种写动作要求的角色。MVP 登记表三类角色合并授予，这条映射此刻恒真，但独立
#: 写出而不是直接判 ``is_authorized_admin``——一旦未来出现"只授予部分角色"的
#: 真实管理员，本模块不需要改一行就能表达"停用/恢复只需要权限管理员角色"这条
#: 本来就存在的合同
#: 要求。本地权限授权/抑制/收回三条同样要求 ``permission_admin`` 角色。
REQUIRED_ROLE: dict[PendingActionType, AdminRole] = {
    PendingActionType.SUSPEND_USER: AdminRole.PERMISSION_ADMIN,
    PendingActionType.RESUME_USER: AdminRole.PERMISSION_ADMIN,
    PendingActionType.LOCAL_PERMISSION_GRANT: AdminRole.PERMISSION_ADMIN,
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: AdminRole.PERMISSION_ADMIN,
    PendingActionType.LOCAL_PERMISSION_REVOKE: AdminRole.PERMISSION_ADMIN,
}

#: 账号状态五取值中，允许发起对应动作的当前状态。suspend 对 ``enabled`` 有效；
#: resume 只对 ``suspended`` 有效；``deleting``/``deleted`` 都不接受。本地权限
#: 授权/抑制要求该键当前"无生效行"（``absent``）；``LOCAL_PERMISSION_REVOKE``
#: 复用同一列但语义相反，要求当前必须"active"才允许发起收回。
VALID_SOURCE_STATES: dict[PendingActionType, frozenset[str]] = {
    PendingActionType.SUSPEND_USER: frozenset({"enabled"}),
    PendingActionType.RESUME_USER: frozenset({"suspended"}),
    PendingActionType.LOCAL_PERMISSION_GRANT: frozenset({"absent"}),
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: frozenset({"absent"}),
    PendingActionType.LOCAL_PERMISSION_REVOKE: frozenset({"active"}),
}

#: 每种写动作执行后写入的目标账号状态；本地权限三类动作不改变 ``account_state``
#: （它们写入的是迁移 ``0072`` 的 ``local_permission_override`` 表，不是
#: ``app_user``），映射为 ``None``——``adapters/postgres_pending_action.py`` 据此
#: 判断"这次 EXECUTE 需不需要更新 app_user.account_state"，不是把 ``None`` 当成
#: 一个真实的账号状态字面量。
TARGET_ACCOUNT_STATE: dict[PendingActionType, str | None] = {
    PendingActionType.SUSPEND_USER: "suspended",
    PendingActionType.RESUME_USER: "enabled",
    PendingActionType.LOCAL_PERMISSION_GRANT: None,
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: None,
    PendingActionType.LOCAL_PERMISSION_REVOKE: None,
}


@dataclass(frozen=True)
class PendingAction:
    """一条已经从数据库读出的待确认操作快照。字段与迁移 ``0068`` 一一对应。"""

    id: str
    action_type: PendingActionType
    target_open_id: str
    target_state_snapshot: str
    initiated_by_open_id: str
    status: PendingActionStatus
    card_delivered: bool
    card_id: str | None
    reason: str | None
    created_at: datetime
    #: 十分钟动作确认窗口——与全库保留到期语义的 ``expires_at`` 同名反义，改名
    #: 避免混淆（见迁移 ``0068`` 文件头部「为什么是 confirm_deadline_at」，opus P2-2）。
    confirm_deadline_at: datetime
    decided_at: datetime | None
    decided_by_open_id: str | None
    #: CardKit 整卡级 sequence 记账，见迁移 ``0068`` 文件头部「为什么需要 card_sequence
    #: 记账」（opus P2-1）。默认值 0 与数据库列 DEFAULT 一致；大多数调用方不需要关心
    #: 这个字段，只有 ``core/admin/card_callback.py`` 的终态更新路径会用到。
    card_sequence: int = 0
    #: 本地权限三类动作（授权/抑制/收回）确认执行所需的结构化参数，JSON 字符串
    #: ``{"company_id": ..., "metric_name": ..., "reason": ...}``；迁移 ``0073``
    #: 新增列，``suspend_user``/``resume_user`` 恒为 ``None``（数据库 CHECK 强制
    #: 这条对应关系，见该迁移文件头部）。默认值 ``None`` 保持既有构造点（本卡之前
    #: 写下的全部 ``PendingAction(...)`` 调用点）不需要改一行。
    payload: str | None = None
    #: 管理卡上下文反向链接。确认卡由管理卡提交产生时保存原管理卡
    #: ``message_id``，这样确认/取消/后台重算都能在原卡上恢复状态；旧文本命令
    #: 与历史行保持 ``None``。
    origin_card_message_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        """当前状态是否已经是四个终态之一。"""
        return self.status in TERMINAL_STATUSES

    def is_expired(self, *, now: datetime) -> bool:
        """相对于给定时刻，这条待确认操作是否已过确认截止时间。"""
        return self.confirm_deadline_at <= now


@dataclass(frozen=True)
class PrepareDecision:
    """``prepare_action`` 的纯逻辑结论。``ok=False`` 时 adapter 不得写入任何行。"""

    ok: bool
    code: str = ""
    message: str = ""


#: ``not_found`` 分支的文案，按动作类型分化：收回场景下 ``current_account_state
#: is None`` 说的是"没查到这一条本地权限登记"，不是"用户记录"，两者用词不同。
_NOT_FOUND_MESSAGE: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "未找到该用户记录。",
    PendingActionType.RESUME_USER: "未找到该用户记录。",
    PendingActionType.LOCAL_PERMISSION_GRANT: "未找到该用户记录。",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "未找到该用户记录。",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "未找到该条本地权限登记。",
}

#: ``target_state_changed`` 分支的文案，按动作类型分化——三类本地权限动作各有
#: 专属措辞，不落进"当 resume 处理"的通用分支（对补充授权/屏蔽指标/撤销命令
#: 毫无意义）。文案里的动词用「撤销」而不是退役的「收回」，与
#: ``core/admin/notification._ACTION_LABEL`` 的取值逐字一致——管理卡上点的
#: 按钮、确认卡上的标题都是「撤销」，拒绝提示不能让管理员去找一个不存在的入口。
_TARGET_STATE_CHANGED_MESSAGE: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "该用户当前不是启用状态，无需停用（或当前状态不支持停用）。",
    PendingActionType.RESUME_USER: "该用户当前不是停用状态，无需恢复（或当前状态不支持恢复）。",
    PendingActionType.LOCAL_PERMISSION_GRANT: "该项本地权限当前已有生效登记，无需重复发起（如需更改请先撤销）。",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "该项本地权限当前已有生效登记，无需重复发起（如需更改请先撤销）。",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "该条本地权限当前不是生效状态，无需撤销（或已被撤销/替代）。",
}


def decide_prepare(
    *, action_type: PendingActionType, current_account_state: str | None
) -> PrepareDecision:
    """目标当前状态是否允许发起这个动作。

    不检查发起人角色——那一步已经由 ``AdminCommandRouter.route()`` 在解析到这条
    命令之前完成（未通过默认拒绝判定不会走到这里，见 ``core/admin/router.py``）。
    """
    if current_account_state is None:
        return PrepareDecision(ok=False, code="not_found", message=_NOT_FOUND_MESSAGE[action_type])
    valid_states = VALID_SOURCE_STATES[action_type]
    if current_account_state not in valid_states:
        return PrepareDecision(
            ok=False,
            code="target_state_changed",
            message=_TARGET_STATE_CHANGED_MESSAGE[action_type],
        )
    return PrepareDecision(ok=True)


#: 拦截文案里"这是哪一类动作"的中文展示名——不直接展示英文字面量，管理员看
#: 不懂内部取值。本地权限三类动作的展示名逐字取自
#: ``core/admin/notification._ACTION_LABEL``，与那张卡片标题的用词必须对得
#: 上。不 import 复用同一份对象：依赖方向是 notification → pending_action，
#: 反向引用会造成循环导入，展示文案就地各维护一份、靠用例锁死取值一致。
_ACTION_TYPE_DISPLAY_NAME: dict[PendingActionType, str] = {
    PendingActionType.SUSPEND_USER: "停用用户",
    PendingActionType.RESUME_USER: "恢复用户",
    PendingActionType.LOCAL_PERMISSION_GRANT: "补充授权",
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: "屏蔽指标",
    PendingActionType.LOCAL_PERMISSION_REVOKE: "撤销",
}

#: 拦截文案里绝对时间的展示偏移——与 ``core/daily_report.py`` 既有的
#: ``_BEIJING_OFFSET`` 同一惯例（UTC 存储 + 8 小时固定偏移展示为北京时间、
#: 显式标注「北京时间」，不为此引入 zoneinfo 依赖）；本模块此前没有任何一处
#: 展示绝对时间，这是本仓管理员面向文案里的第一处，因此照抄这一既有先例而不是
#: 另立一套格式。
_DISPLAY_TIMEZONE_OFFSET = timedelta(hours=8)


def format_in_flight_conflict_message(*, blocking: PendingAction) -> str:
    """``prepare()`` 撞上"同一目标已有一条在途 pending 行"时的拦截文案。

    带自助解法而不只是一句"已有操作在途"：把 ``blocking``（已确认结构上仍是
    ``status='pending'`` 的那一行）的动作类型/发起时间/截止时间摘要，连同
    "已过期会自动释放、未过期请先处理旧卡"这条指引一起渲染成一段文案。纯
    函数、不做任何 I/O 或时钟读取，先归一到 UTC 再加 8 小时展示，避免传入
    字段本身非 UTC 时被二次偏移。
    """
    started = blocking.created_at.astimezone(UTC) + _DISPLAY_TIMEZONE_OFFSET
    deadline = blocking.confirm_deadline_at.astimezone(UTC) + _DISPLAY_TIMEZONE_OFFSET
    action_name = _ACTION_TYPE_DISPLAY_NAME[blocking.action_type]
    return (
        "该用户当前已有一条待确认操作在途："
        f"{action_name}，发起于 {started:%Y-%m-%d %H:%M}（北京时间），"
        f"将于 {deadline:%Y-%m-%d %H:%M}（北京时间）过期。"
        "已过期的旧操作会在下次发起同类命令时自动释放，无需手动处理；"
        "尚未过期时请先在原确认卡片上点击确认执行或取消，再重新发起。"
    )


class ConfirmResultKind(str, Enum):
    """``decide_confirm`` 的分支判定。

    取值各自唯一（不与接口设计的错误码字符串直接相等）——``NOT_INITIATOR`` 与
    ``ROLE_REVOKED`` 在[接口设计「通用约定·错误模型」]
    (../../../../docs/技术设计/接口设计.md) 里对应同一个错误码 ``not_authorized``，
    但两者在这里必须是**两个不同的枚举成员**：Python `Enum` 对相同取值的成员会
    自动合并成别名（第二个定义的名字变成第一个的别名，`is`/`==` 判断因此会把两个
    语义不同的分支错误地判成"同一件事"）。真正对外的错误码字符串由下面的
    ``ERROR_CODE`` 映射按需派生，不通过枚举取值本身表达。
    """

    EXECUTE = "execute"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"
    EXPIRE = "expired"
    NOT_INITIATOR = "not_initiator"
    ROLE_REVOKED = "role_revoked"
    TARGET_DRIFTED = "target_drifted"


#: 内部分支 → 接口设计统一错误码表的映射；``EXECUTE``（成功分支）没有错误码，
#: 不出现在这个映射里。
ERROR_CODE: dict[ConfirmResultKind, str] = {
    ConfirmResultKind.ALREADY_TERMINAL: "already_executed",
    ConfirmResultKind.NOT_FOUND: "not_found",
    ConfirmResultKind.EXPIRE: "action_expired",
    ConfirmResultKind.NOT_INITIATOR: "not_authorized",
    ConfirmResultKind.ROLE_REVOKED: "not_authorized",
    ConfirmResultKind.TARGET_DRIFTED: "target_state_changed",
}


@dataclass(frozen=True)
class ConfirmDecision:
    """``decide_confirm`` 的纯逻辑结论。"""

    kind: ConfirmResultKind
    message: str
    #: 仅 ``EXECUTE`` 时非空：adapter 应当把目标 ``account_state`` 改成这个值。
    new_account_state: str | None = None
    #: 仅需要把 ``pending_action`` 转终态的分支非空（``EXPIRE``/``ROLE_REVOKED``/
    #: ``TARGET_DRIFTED``/``EXECUTE``）；``NOT_INITIATOR``/``NOT_FOUND``/
    #: ``ALREADY_TERMINAL`` 均不写入新终态（``NOT_INITIATOR`` 保留原样等待正确的人
    #: 点击，后两者本就已经是终态或从未真正送达）。
    terminal_status: PendingActionStatus | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """这次确认是否成功执行。"""
        return self.kind is ConfirmResultKind.EXECUTE

    @property
    def code(self) -> str:
        """接口设计统一错误码；成功分支为空字符串。"""
        return "" if self.ok else ERROR_CODE[self.kind]


def _confirm_not_found(pending: PendingAction | None) -> ConfirmDecision | None:
    """找不到，或从未真正送达（``card_delivered=False``，两者在调用方眼里刻意不可区分）。"""
    if pending is None or not pending.card_delivered:
        return ConfirmDecision(kind=ConfirmResultKind.NOT_FOUND, message="未找到该待确认操作。")
    return None


def _confirm_already_terminal(pending: PendingAction) -> ConfirmDecision | None:
    """早已是终态：幂等返回既有结果，不当作新的确认处理（重复点击/回调/重试只返回既有结果）。"""
    if pending.status is not PendingActionStatus.PENDING:
        return ConfirmDecision(
            kind=ConfirmResultKind.ALREADY_TERMINAL,
            message=_terminal_message(pending.status),
        )
    return None


def _confirm_not_initiator(pending: PendingAction, clicker_open_id: str) -> ConfirmDecision | None:
    """点击人不是发起人：不改变任何字段，且必须排在过期判定之前。

    确认卡可以被转发，因此"非发起人点了一张已经过期的卡"是真实可达的：若过期
    判定排在前面，会把这条记录转成 ``EXPIRED`` 并把点击者错记成决定人、发出
    本不该给他的终态卡与群通知。排在这里之后，非发起人无论卡片是否已过期都
    只得到同一句话、不触发任何写入。
    """
    if pending.initiated_by_open_id != clicker_open_id:
        return ConfirmDecision(
            kind=ConfirmResultKind.NOT_INITIATOR,
            message="只有发起该操作的管理员本人可以确认。",
        )
    return None


def _confirm_expired(pending: PendingAction, now: datetime) -> ConfirmDecision | None:
    """已过期：首次发现过期时才转出 ``EXPIRE``（再次点击会先在终态检查被拦住）。"""
    if pending.is_expired(now=now):
        return ConfirmDecision(
            kind=ConfirmResultKind.EXPIRE,
            message="该待确认操作已过期，请重新查询后再发起。",
            terminal_status=PendingActionStatus.EXPIRED,
            reason="expired",
        )
    return None


def _confirm_role_revoked(
    pending: PendingAction, registry_entry: AdminRegistryEntry | None
) -> ConfirmDecision | None:
    """发起人当前角色已经不满足这个动作类型所需角色（含条目被撤销、条目不存在）。"""
    required_role = REQUIRED_ROLE[pending.action_type]
    if (
        registry_entry is None
        or not is_authorized_admin(registry_entry)
        or not registry_entry.has_role(required_role)
    ):
        return ConfirmDecision(
            kind=ConfirmResultKind.ROLE_REVOKED,
            message="当前角色已无权执行该操作，请重新查询后再发起。",
            terminal_status=PendingActionStatus.FAILED,
            reason="role_revoked",
        )
    return None


def _confirm_target_drifted(
    pending: PendingAction, current_account_state: str | None
) -> ConfirmDecision | None:
    """目标当前状态与 prepare 时刻的快照不一致。"""
    if current_account_state != pending.target_state_snapshot:
        return ConfirmDecision(
            kind=ConfirmResultKind.TARGET_DRIFTED,
            message="目标用户状态已经变化，请重新查询后再发起。",
            terminal_status=PendingActionStatus.FAILED,
            reason="target_drifted",
        )
    return None


def decide_confirm(
    *,
    pending: PendingAction | None,
    clicker_open_id: str,
    now: datetime,
    registry_entry: AdminRegistryEntry | None,
    current_account_state: str | None,
) -> ConfirmDecision:
    """确认卡片点击"确认执行"按钮时的完整核对链，纯函数、不做任何写入。

    调用顺序即核对顺序，任一步不通过立即返回：找不到/未送达 → 早已是终态 →
    点击人不是发起人 → 已过期 → 角色已不满足 → 目标状态已漂移 → 全部通过则
    ``EXECUTE``（连同 ``terminal_status=EXECUTED`` 交给 adapter，adapter 还要
    在同一事务里先成功写审计才能真正提交）。各步判据见对应辅助函数 docstring。
    """
    if (blocked := _confirm_not_found(pending)) is not None:
        return blocked
    assert pending is not None
    if (blocked := _confirm_already_terminal(pending)) is not None:
        return blocked
    if (blocked := _confirm_not_initiator(pending, clicker_open_id)) is not None:
        return blocked
    if (blocked := _confirm_expired(pending, now)) is not None:
        return blocked
    if (blocked := _confirm_role_revoked(pending, registry_entry)) is not None:
        return blocked
    if (blocked := _confirm_target_drifted(pending, current_account_state)) is not None:
        return blocked

    return ConfirmDecision(
        kind=ConfirmResultKind.EXECUTE,
        message="已确认执行。",
        new_account_state=TARGET_ACCOUNT_STATE[pending.action_type],
        terminal_status=PendingActionStatus.EXECUTED,
    )


class CancelResultKind(str, Enum):
    """``decide_cancel`` 的分支判定。"""

    CANCEL = "cancel"
    ALREADY_TERMINAL = "already_terminal"
    NOT_FOUND = "not_found"
    EXPIRE = "expired"
    NOT_INITIATOR = "not_initiator"


#: 同上：内部分支 → 统一错误码表；``CANCEL``（成功分支）不出现在这个映射里。
CANCEL_ERROR_CODE: dict[CancelResultKind, str] = {
    CancelResultKind.ALREADY_TERMINAL: "already_executed",
    CancelResultKind.NOT_FOUND: "not_found",
    CancelResultKind.EXPIRE: "action_expired",
    CancelResultKind.NOT_INITIATOR: "not_authorized",
}


@dataclass(frozen=True)
class CancelDecision:
    """``decide_cancel`` 的纯逻辑结论。"""

    kind: CancelResultKind
    message: str
    terminal_status: PendingActionStatus | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """这次取消是否成功执行。"""
        return self.kind is CancelResultKind.CANCEL

    @property
    def code(self) -> str:
        """接口设计统一错误码；成功分支为空字符串。"""
        return "" if self.ok else CANCEL_ERROR_CODE[self.kind]


def decide_cancel(
    *, pending: PendingAction | None, clicker_open_id: str, now: datetime
) -> CancelDecision:
    """「取消」按钮的核对链，比确认短。

    取消不执行任何业务变更，因此不需要重新核对角色或目标状态漂移——放弃一个
    即将过期的操作，即使角色已经变化也应当总是安全。判定顺序与
    :func:`decide_confirm` 逐条对齐：发起人判定排在过期判定之前，非发起人点
    一张已过期的卡不得把它翻成 ``EXPIRED``、不得被记成决定人、不得触发群通知。
    """
    if pending is None or not pending.card_delivered:
        return CancelDecision(kind=CancelResultKind.NOT_FOUND, message="未找到该待确认操作。")

    if pending.status is not PendingActionStatus.PENDING:
        return CancelDecision(
            kind=CancelResultKind.ALREADY_TERMINAL, message=_terminal_message(pending.status)
        )

    if pending.initiated_by_open_id != clicker_open_id:
        return CancelDecision(
            kind=CancelResultKind.NOT_INITIATOR, message="只有发起该操作的管理员本人可以取消。"
        )

    if pending.is_expired(now=now):
        return CancelDecision(
            kind=CancelResultKind.EXPIRE,
            message="该待确认操作已过期。",
            terminal_status=PendingActionStatus.EXPIRED,
            reason="expired",
        )

    return CancelDecision(
        kind=CancelResultKind.CANCEL,
        message="已取消，未做任何变更。",
        terminal_status=PendingActionStatus.CANCELLED,
        reason="cancelled_by_admin",
    )


class PendingActionAuditWriteFailedError(RuntimeError):
    """审计写入失败，事务已整体回滚，可安全重试（接口设计错误码 ``audit_write_failed``）。

    ``pending_action`` 与目标 ``app_user`` 均未发生任何变化。定义在这个纯类型
    模块而不是 adapter 模块，是为了让调用方能在不引入 psycopg
    依赖链的情况下拿到这个类型去写 ``except``。真正抛出它的地方是 adapter 的
    ``confirm()``/``cancel()``（审计调用失败时）。
    """


class PendingActionTransientFailureError(RuntimeError):
    """确认/取消操作命中数据库瞬时故障（死锁、锁等待超时，或其他操作性错误）。

    事务已整体回滚，``pending_action`` 与目标 ``app_user`` 均未发生任何变化，
    调用方可以安全重试；「停用一个正在聊天的用户」最容易撞见锁等待超时。定义
    在这个纯类型模块而不是 adapter 模块，与 :class:`PendingActionAuditWriteFailedError`
    同一取舍，让调用方能在不引入 psycopg 依赖链的情况下拿到这个类型去写
    ``except``。``classification`` 只是抛出方 psycopg 异常的类名，仅用于审计
    记录，不参与控制流判断。
    """

    def __init__(self, classification: str) -> None:
        """记录数据库瞬时故障的分类，供审计记录。"""
        super().__init__(f"数据库瞬时故障，事务已回滚，可重试：{classification}")
        self.classification = classification


def _terminal_message(status: PendingActionStatus) -> str:
    if status is PendingActionStatus.EXECUTED:
        return "该操作已经执行过，不会重复执行。"
    if status is PendingActionStatus.CANCELLED:
        return "该操作已经取消。"
    if status is PendingActionStatus.EXPIRED:
        return "该操作已经过期。"
    return "该操作已经结束，无法继续操作。"
