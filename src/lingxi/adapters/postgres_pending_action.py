"""待确认操作（``pending_action``，迁移 ``0068``）的唯一 PostgreSQL 落点。

三个职责，与 ``core/admin/pending_action.py`` 的三个纯函数一一对应：

1. :meth:`PostgresPendingActionStore.prepare` —— 读目标当前状态、调用
   ``decide_prepare``，通过则插入一行 ``pending`` 记录（``card_delivered=FALSE``）。
   同一 ``target_open_id`` 已有在途 ``pending`` 行时，插入会撞上迁移 ``0068`` 的
   部分唯一索引，本方法捕获后转译为友好拒绝（外部审查交叉裁定，codex P1-5，ABA）。
2. :meth:`PostgresPendingActionStore.confirm`/:meth:`~.cancel` —— ``SELECT ... FOR
   UPDATE`` 锁定待确认操作与目标 ``app_user`` 行，调用对应纯函数取得决策，按决策
   执行 ``UPDATE``，同一事务内写审计；审计失败则整个事务回滚（写路径
   ``audit.record`` 必须与状态变更同事务，代码框架第三节；这句"同一事务"的准确
   边界见 :meth:`~.confirm` 文档）。``confirm()`` 对发起人当前角色的核对同样在
   这个事务、这个连接内完成（``SELECT ... FROM admin_registry ... FOR SHARE``），
   不再经由独立注入的登记表查询端口——见 :meth:`~.confirm` 文档"为什么角色核对
   要在同一事务内完成"。
3. :meth:`~.mark_card_delivered`/:meth:`~.mark_send_failed` —— 卡片发送结果的落库。
4. :meth:`~.next_card_sequence` —— CardKit 整卡级 sequence 记账，供
   ``core/admin/card_callback.py`` 在终态更新前换取本次调用要用的 sequence
   （迁移 ``0068`` 文件头部「为什么需要 card_sequence 记账」）。

真实并发下"最多成功一次"的机制（``SELECT ... FOR UPDATE`` 序列化并发确认，真正提供
保证的是行锁本身而不是条件更新的影响行数）见迁移 ``0068`` 文件头部。

**已知边界**：``pending_action.result`` 列本模块从未写入（``confirm()``/``cancel()``
只写 ``status``/``reason``/``decided_at``/``decided_by_open_id``）——它是为未来消费方
（本地抑制、单用户重同步、登记表变更等，迁移 ``0068`` 文件头部）预留的空列，当前
读到的 ``result`` 恒为 ``NULL``，不代表"这条待确认操作没有可描述的执行结果"。

## 本地权限授权/抑制（迁移 ``0073``，#319 S-P-1b）如何复用同一套机制

``LOCAL_PERMISSION_GRANT``/``LOCAL_PERMISSION_SUPPRESS`` 两个动作类型复用与
``suspend_user``/``resume_user`` 完全相同的 prepare/confirm 骨架，差异只在
"目标当前状态是什么"与"EXECUTE 时具体写哪张表"两处：

- **``prepare()``**：不读 ``app_user.account_state``，改为按 ``payload``
  （``{"company_id", "metric_name", "reason"}``）里的公司×指标键，查迁移 ``0072``
  的 ``local_permission_override`` 表是否已有 ``entry_status='active'`` 的同极性
  同键行，得到 ``"absent"``/``"present"`` 写入 ``target_state_snapshot``——与
  ``account_state`` 是同一列，只是取值域不同，``decide_prepare``/``decide_confirm``
  两个纯函数不需要关心这个区别（它们只做字符串比较）。
- **``confirm()``**：EXECUTE 分支按 ``TARGET_ACCOUNT_STATE[pending.action_type]``
  是否为 ``None`` 判断要不要更新 ``app_user.account_state``——本地权限两类动作
  该值恒为 ``None``（见 ``core/admin/pending_action.TARGET_ACCOUNT_STATE`` 文档），
  改为解析 ``pending.payload`` 并调用 :func:`~lingxi.adapters.
  postgres_local_permission._insert_locked`。**真正的漂移检测不经过
  ``decide_confirm`` 的字符串比较**（本模块不在 confirm 时刻重新查询
  ``local_permission_override``，只把 prepare 时刻的快照原样喂回
  ``decide_confirm``，这条比较对本地权限两类动作恒真）——而是让这次 INSERT
  直接撞迁移 ``0072`` 的部分唯一索引：撞上就说明 prepare 到 confirm 之间已经有
  另一笔（可能是一次真正并发的写入，也可能是一次绕过 pending_action 机制的直接
  写入）抢先在同一个键上落了地，本模块捕获 :class:`~lingxi.adapters.
  postgres_local_permission.DuplicateActiveOverride` 后把决策**降级**为既有
  ``ConfirmResultKind.TARGET_DRIFTED``/``FAILED``/``reason="target_drifted"``
  ——不新增错误码，复用管理员已经熟悉的"目标状态已经变化，请重新查询后再发起"
  文案。这条 INSERT 包在一层 ``connection.transaction()``（psycopg 的
  SAVEPOINT）里，撞索引时只回滚这条 INSERT 本身，外层事务（``pending_action``
  终态更新、审计）仍然可以正常提交——与 :meth:`~.prepare` 对同一类冲突
  （``pending_action_single_pending_target_idx``）的处理同一姿态。

## 本地权限收回（迁移 0073 已扩容 CHECK，#319 S-P-1b 卡 B）如何复用同一套机制

``LOCAL_PERMISSION_REVOKE`` 与 grant/suppress 共用同一个 prepare/confirm 骨架，
但输入形状相反——grant/suppress 按"用户+方向+公司+指标"这个键定位；revoke
按 override_id 这一行本身定位（管理员从 ``/admin user`` 回显里复制这个 id，
不需要重新填一遍公司/指标）。差异集中在三处：

- ``prepare()``：复用既有的 ``target_open_id`` 形参承载 override_id（不是真实
  的 open_id——router 层拿到的 ``command.identifier`` 对 revoke 命令而言就是
  override_id，见 ``core/admin/router.py`` 的 ``_dispatch_write_action`` 调用点），
  按这个 id 联表 ``local_permission_override``/``app_user`` 一次查出：这一行现在
  的 ``entry_status``（写进 ``target_state_snapshot``，取值域收窄成
  ``VALID_SOURCE_STATES[REVOKE] = {"active"}``）、这一行的属主真实 open_id（写进
  ``PendingAction.target_open_id``，供确认卡展示"目标：xxx"）、以及这一行的
  ``direction``/``company_id``/``metric_name``（连同管理员这次填写的收回
  ``reason`` 一起序列化进 ``payload``，供 ``core/admin/notification.py`` 渲染
  "含被收回的方向/公司/指标"——卡 B 设计卡显式要求）。行不存在时把 current_state
  置 ``None``，与 grant/suppress 对"目标不存在"的处理同一姿态，落进
  ``decide_prepare`` 既有的"未找到"拒绝分支，不产生任何新代码路径。
- 自我目标防呆放在这里、不在 router 层（设计卡"检查点位置不同"）：router 拿到
  的 ``target_identifier`` 只是一个不透明的 override_id 字符串，在查库之前无法
  判断它的属主是不是操作者本人——查到属主 open_id 之后立刻核对，相等则直接拒绝、
  不继续往下建任何 ``pending_action`` 行，与 router 层同语义（见
  :data:`_REVOKE_SELF_TARGET_FORBIDDEN_CODE`，取值与 ``core/admin/router.
  _SELF_TARGET_FORBIDDEN_CODE`` 相同字符串，两处不互相 import，各自独立定义
  ——与全仓库既有的"结构相同、不共享导入"的 Protocol/常量惯例一致）。
- ``confirm()``：EXECUTE 分支解析 ``payload`` 取出 override_id，调用
  :func:`~lingxi.adapters.postgres_local_permission._revoke_locked`（条件
  ``UPDATE ... WHERE entry_status = 'active'``）。与 grant/suppress 的 INSERT
  撞唯一索引不同，这里没有异常可捕获——``_revoke_locked`` 用返回的布尔值
  （``rowcount == 1``）直接说明这一行在 prepare 到这一刻之间是否仍然是
  ``active``：如果不是（已经被另一条路径抢先收回），同样降级为既有
  ``ConfirmResultKind.TARGET_DRIFTED``/``FAILED``/``reason="target_drifted"``，
  不新增错误码——与 grant/suppress 那条 SAVEPOINT 降级路径同一姿态，只是这里的
  冲突信号是条件更新的影响行数，不是唯一索引异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from lingxi.adapters.admin_registry import admin_registry_entry_from_row
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_conversation import _Transaction
from lingxi.adapters.postgres_local_permission import (
    DuplicateActiveOverride,
    _insert_locked,
    _revoke_locked,
)
from lingxi.core.admin.pending_action import (
    PENDING_ACTION_TTL_SECONDS,
    CancelDecision,
    ConfirmDecision,
    ConfirmResultKind,
    PendingAction,
    PendingActionAuditWriteFailed,
    PendingActionStatus,
    PendingActionTransientFailure,
    PendingActionType,
    PrepareDecision,
    decide_cancel,
    decide_confirm,
    decide_prepare,
)
from lingxi.core.ids import new_id
from lingxi.core.permission.local_override import LocalPermissionOverrideEntry, OverrideDirection

#: ``prepare()`` 撞上同目标在途唯一索引时对外的错误码与文案（外部审查交叉裁定，
#: codex P1-5）——与 ``core/admin/pending_action.ERROR_CODE``/``CANCEL_ERROR_CODE``
#: 同一风格的字面量表，但这条拒绝发生在插入阶段（数据库约束触发），不经过
#: ``decide_prepare`` 这个纯函数，因此不适合定义在那个模块的 ``ConfirmResultKind``/
#: ``CancelResultKind`` 枚举里——那两个枚举描述的是"读到一行之后怎么判"，这里是
#: "连一行都插不进去"。
TARGET_HAS_PENDING_ACTION_CODE = "target_has_pending_action"
TARGET_HAS_PENDING_ACTION_MESSAGE = "该用户当前已有一条待确认操作在途，请先处理（确认或取消）后再重新发起。"

#: 收回的自我目标防呆拒绝码/文案（#319 S-P-1b 卡 B）：取值与 ``core/admin/
#: router._SELF_TARGET_FORBIDDEN_CODE``/拒绝文案相同字符串——两个模块结构上
#: 不互相 import（``core/admin/router.py`` 不 import adapters/），因此各自独立
#: 定义同一取值，与全仓库既有的"结构相同、不共享导入"的 Protocol/常量惯例一致
#: （对照本模块自己的 ``AuditSink`` Protocol 与 ``core/admin/router.AuditSink``
#: 结构相同但分别定义）。检查点为什么在这里而不在 router 层，见模块文档「本地
#: 权限收回如何复用同一套机制」。
_REVOKE_SELF_TARGET_FORBIDDEN_CODE = "self_target_forbidden"
_REVOKE_SELF_TARGET_FORBIDDEN_MESSAGE = "不能对自己发起该操作。"

#: 本地权限授权/抑制两类动作类型 → 迁移 ``0072`` ``direction`` 列取值的映射
#: （``REVOKE`` 不在这张表里，且这不是"未登记"的历史遗留——收回的 ``direction``
#: 不是一个固定值，而是要按 override_id 从被收回的那一行本身读出来，见模块文档
#: 「本地权限收回如何复用同一套机制」）。``prepare()`` 的基线观测查询、
#: ``confirm()`` 的 EXECUTE 写入分支共用同一张表。
_DIRECTION_BY_ACTION_TYPE: dict[PendingActionType, OverrideDirection] = {
    PendingActionType.LOCAL_PERMISSION_GRANT: OverrideDirection.GRANT,
    PendingActionType.LOCAL_PERMISSION_SUPPRESS: OverrideDirection.SUPPRESS,
}


class AuditSink(Protocol):
    """与 ``core/admin/router.AuditSink`` 结构相同——签名本身不接收连接对象，
    "审计与状态变更同事务"由 :meth:`~PostgresPendingActionStore.confirm`/
    :meth:`~.cancel` 在同一个 ``with connection.transaction()`` 块内调用它来实现：
    调用失败即异常向上传播，事务整体回滚（见两个方法的实现与
    :class:`PendingActionAuditWriteFailed`）。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class PrepareOutcome:
    decision: PrepareDecision
    pending: PendingAction | None = None


@dataclass(frozen=True)
class ConfirmOutcome:
    decision: ConfirmDecision
    #: 仅当待确认操作 ID 从未存在过（含伪造）时为 ``None``——其余全部分支
    #: （含"未送达"）都有一个真实存在的行可以返回。
    pending: PendingAction | None = None


@dataclass(frozen=True)
class CancelOutcome:
    decision: CancelDecision
    pending: PendingAction | None = None


_SELECT_COLUMNS = (
    "id, action_type, target_open_id, target_state_snapshot, initiated_by_open_id,"
    " status, card_delivered, card_id, reason, created_at, confirm_deadline_at,"
    " decided_at, decided_by_open_id, card_sequence, payload"
)


def _row_to_pending_action(row: tuple) -> PendingAction:
    (
        id_,
        action_type,
        target_open_id,
        target_state_snapshot,
        initiated_by_open_id,
        status,
        card_delivered,
        card_id,
        reason,
        created_at,
        confirm_deadline_at,
        decided_at,
        decided_by_open_id,
        card_sequence,
        payload,
    ) = row
    return PendingAction(
        id=id_,
        action_type=PendingActionType(action_type),
        target_open_id=target_open_id,
        target_state_snapshot=target_state_snapshot,
        initiated_by_open_id=initiated_by_open_id,
        status=PendingActionStatus(status),
        card_delivered=card_delivered,
        card_id=card_id,
        reason=reason,
        created_at=created_at,
        confirm_deadline_at=confirm_deadline_at,
        decided_at=decided_at,
        decided_by_open_id=decided_by_open_id,
        card_sequence=card_sequence,
        payload=payload,
    )


class PostgresPendingActionStore:
    """``pending_action`` 表的唯一真实实现。

    **不再接受 ``registry`` 参数（外部审查交叉裁定，codex P1-4）**：``confirm()``
    对发起人角色的核对现在直接在自己的事务、自己的连接上查询 ``admin_registry``
    （见该方法文档），不再通过独立注入的登记表查询端口去读——那条独立查询走的是
    另一条连接，读到的角色状态不受本事务任何行锁保护，在"读到角色"与"提交这次
    确认"之间的窗口里，一次并发撤权可能悄悄溜过去而不被察觉（TOCTOU）。
    """

    def __init__(
        self,
        dsn: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
        audit: AuditSink,
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        self._audit = audit

    def get(self, *, pending_action_id: str) -> PendingAction | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s", (pending_action_id,)
            )
            row = cursor.fetchone()
        return _row_to_pending_action(row) if row is not None else None

    def prepare(
        self,
        *,
        action_type: PendingActionType,
        target_open_id: str,
        initiated_by_open_id: str,
        company_id: str | None = None,
        metric_name: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> PrepareOutcome:
        """读目标当前状态、判定是否可以发起，通过则插入一行 ``pending`` 记录
        （``card_delivered=FALSE``，调用方随后发卡片，成功再调用
        :meth:`mark_card_delivered`，失败调用 :meth:`mark_send_failed`）。

        ``company_id``/``metric_name``/``reason`` 三个参数只有
        ``action_type in LOCAL_PERMISSION_ACTION_TYPES``（授权/抑制/收回）时才会
        被使用。授权/抑制：三个参数原样序列化成 JSON 写入新增的 ``payload`` 列
        （迁移 ``0073``），"目标当前状态"改按这个键去查迁移 ``0072`` 的表是否
        已有生效行（模块文档「本地权限授权/抑制如何复用同一套机制」）。收回：
        只用 ``reason``（管理员这次填写的收回原因），``company_id``/
        ``metric_name`` 被忽略——``target_open_id`` 这个形参改为承载 override_id，
        真正的公司/指标/方向从这一行本身查出来（模块文档「本地权限收回如何复用
        同一套机制」）。``suspend_user``/``resume_user`` 两类动作忽略全部三个
        参数（保持沿用 ``account_state`` 的既有行为，逐字节不变）。
        """

        from psycopg.errors import UniqueViolation

        moment = now or datetime.now(timezone.utc)
        pending_id = new_id("pac")
        is_local_permission_action = action_type in _DIRECTION_BY_ACTION_TYPE
        is_revoke_action = action_type is PendingActionType.LOCAL_PERMISSION_REVOKE
        payload = (
            json.dumps(
                {"company_id": company_id, "metric_name": metric_name, "reason": reason},
                ensure_ascii=False,
            )
            if is_local_permission_action
            else None
        )
        # 收回的 INSERT 目标是被收回那一行的真实属主 open_id，不是本方法收到的
        # ``target_open_id`` 形参（那里装的是 override_id）——见下面 revoke 分支
        # 对这个变量的重新赋值；grant/suppress/suspend/resume 三类动作里它恒等于
        # 收到的 ``target_open_id``，不改变既有行为。
        resolved_target_open_id = target_open_id
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    if is_revoke_action:
                        override_id = target_open_id
                        cursor.execute(
                            "SELECT lpo.entry_status, lpo.direction, lpo.company_id,"
                            "       lpo.metric_name, au.feishu_open_id"
                            "  FROM local_permission_override lpo"
                            "  JOIN app_user au ON au.id = lpo.user_id"
                            " WHERE lpo.id = %s",
                            (override_id,),
                        )
                        revoke_row = cursor.fetchone()
                        if revoke_row is None:
                            current_state = None
                        else:
                            (
                                entry_status,
                                direction_value,
                                found_company_id,
                                found_metric_name,
                                owner_open_id,
                            ) = revoke_row
                            if owner_open_id == initiated_by_open_id:
                                # 自我目标防呆（模块文档「本地权限收回如何复用同一
                                # 套机制」）：查到属主之后立刻核对，相等则直接拒绝，
                                # 不再往下调用 decide_prepare、不产生任何
                                # pending_action 行。
                                return PrepareOutcome(
                                    decision=PrepareDecision(
                                        ok=False,
                                        code=_REVOKE_SELF_TARGET_FORBIDDEN_CODE,
                                        message=_REVOKE_SELF_TARGET_FORBIDDEN_MESSAGE,
                                    )
                                )
                            current_state = entry_status
                            resolved_target_open_id = owner_open_id
                            payload = json.dumps(
                                {
                                    "override_id": override_id,
                                    "direction": direction_value,
                                    "company_id": found_company_id,
                                    "metric_name": found_metric_name,
                                    "reason": reason,
                                },
                                ensure_ascii=False,
                            )
                    elif is_local_permission_action:
                        cursor.execute(
                            "SELECT id, account_state FROM app_user WHERE feishu_open_id = %s",
                            (target_open_id,),
                        )
                        row = cursor.fetchone()
                        user_id = row[0] if row is not None else None

                        if user_id is None:
                            current_state = None
                        else:
                            direction = _DIRECTION_BY_ACTION_TYPE[action_type]
                            cursor.execute(
                                "SELECT 1 FROM local_permission_override"
                                " WHERE user_id = %s AND direction = %s AND company_id = %s"
                                "   AND metric_name = %s AND entry_status = 'active'",
                                (user_id, direction.value, company_id, metric_name),
                            )
                            current_state = "present" if cursor.fetchone() is not None else "absent"
                    else:
                        cursor.execute(
                            "SELECT id, account_state FROM app_user WHERE feishu_open_id = %s",
                            (target_open_id,),
                        )
                        row = cursor.fetchone()
                        current_state = row[1] if row is not None else None

                    decision = decide_prepare(
                        action_type=action_type, current_account_state=current_state
                    )
                    if not decision.ok:
                        return PrepareOutcome(decision=decision)

                    confirm_deadline_at = moment + timedelta(seconds=PENDING_ACTION_TTL_SECONDS)
                    # 嵌套事务块 = psycopg 的 SAVEPOINT：撞上迁移 0068 的同目标在途
                    # 唯一索引时，只回滚这条 INSERT 本身，外层事务（目前只做了一次
                    # 只读 SELECT）保持可继续提交的干净状态（外部审查交叉裁定，
                    # codex P1-5，ABA）。
                    try:
                        with connection.transaction():
                            cursor.execute(
                                """
                                INSERT INTO pending_action
                                    (id, action_type, target_open_id, target_state_snapshot,
                                     initiated_by_open_id, status, card_delivered, created_at,
                                     confirm_deadline_at, payload)
                                VALUES (%s, %s, %s, %s, %s, 'pending', FALSE, %s, %s, %s)
                                """,
                                (
                                    pending_id,
                                    action_type.value,
                                    resolved_target_open_id,
                                    current_state,
                                    initiated_by_open_id,
                                    moment,
                                    confirm_deadline_at,
                                    payload,
                                ),
                            )
                    except UniqueViolation:
                        return PrepareOutcome(
                            decision=PrepareDecision(
                                ok=False,
                                code=TARGET_HAS_PENDING_ACTION_CODE,
                                message=TARGET_HAS_PENDING_ACTION_MESSAGE,
                            )
                        )
        pending = self.get(pending_action_id=pending_id)
        assert pending is not None  # 刚提交的行，同一连接的下一次读必然可见
        return PrepareOutcome(decision=decision, pending=pending)

    def mark_card_delivered(self, *, pending_action_id: str, card_id: str) -> None:
        """卡片确认送达（收到权威"平台已接收"回读）后置真。

        **同时把 ``card_sequence`` 基线抬到至少 2**（Issue #96 卡片回调应答修复，
        序号基线正式化）：编排者在 `biai-stage` 用受控探针实测——CardKit 建卡
        （``cardkit.v1.card.create``）与随后作为消息发出（``im.v1.message.reply``）
        各自消耗一次整卡级 sequence，序号 1、2 两次全量更新会被平台判定为
        "内容未变"而幂等吞掉（``code=0`` 但卡片视觉不刷新）；真正生效的更新必须
        携带 ``sequence>=3``。这条基线此前只在 stage 用临时数据库触发器验证，本次
        改动把探针验证过的事实正式落进代码——部署本修复后，stage 侧的临时触发器
        随之撤除。``next_card_sequence()`` 因此在卡片送达后第一次调用会返回 3，
        与真实 CardKit 的整卡计数器对齐。

        用 ``GREATEST`` 而不是覆盖式赋值 ``= 2``：``mark_card_delivered`` 结构上
        不会被同一条待确认操作调用第二次，但 ``GREATEST`` 让这条语句即使被重复
        调用也不会把已经因为其它路径前进过的 sequence 倒退回 2。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET card_delivered = TRUE, card_id = %s,"
                " card_sequence = GREATEST(card_sequence, 2)"
                " WHERE id = %s AND status = 'pending'",
                (card_id, pending_action_id),
            )

    def mark_send_failed(self, *, pending_action_id: str, now: datetime | None = None) -> None:
        """卡片确认发送失败（明确拒绝，不是结果不明）：立即把待确认操作转终态
        ``failed``，``card_delivered`` 保持 ``FALSE``——合同"卡片发送失败时本次
        操作不执行"。提前终态化是清洁收尾，不是正确性所必需的第二道防线
        （``card_delivered=FALSE`` 本身已经让 :func:`~lingxi.core.admin.
        pending_action.decide_confirm`/``decide_cancel`` 一律拒绝），只是避免这一行
        无限期停留在看似"仍在等待"的 ``pending``。
        """

        moment = now or datetime.now(timezone.utc)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET status = 'failed', reason = 'card_send_failed',"
                " decided_at = %s WHERE id = %s AND status = 'pending'",
                (moment, pending_action_id),
            )

    def next_card_sequence(self, *, pending_action_id: str) -> int:
        """CardKit 整卡级 sequence 记账（外部审查交叉裁定，opus P2-1）：原子自增并
        取号，供 :mod:`lingxi.core.admin.card_callback` 在终态更新前换取本次调用要用
        的 sequence——见迁移 ``0068`` 文件头部「为什么需要 card_sequence 记账」。

        单条 ``UPDATE ... RETURNING`` 语句本身就是原子操作：并发的两次调用各自拿到
        不同的、严格递增的返回值，不需要额外加锁或包一层显式事务。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE pending_action SET card_sequence = card_sequence + 1"
                " WHERE id = %s RETURNING card_sequence",
                (pending_action_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise LookupError(f"待确认操作不存在，无法分配 card sequence：{pending_action_id}")
        return row[0]

    def confirm(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None = None
    ) -> ConfirmOutcome:
        """确认卡片"确认执行"按钮的完整事务：详见 ``core/admin/pending_action.
        decide_confirm`` 的分支文档。审计写入失败时整个 ``with connection.
        transaction()`` 块回滚，包装为 :class:`PendingActionAuditWriteFailed` 向上
        抛出——``pending_action``、目标 ``app_user`` 均保持事务开始前的状态，调用方
        （``core/admin/card_callback.py``）不得将其视为任何一种终态。

        **"同一事务"这句话的准确边界（外部审查交叉裁定，opus P2-2 附带项）**：只在
        失败方向成立——``audit.record()`` 抛异常时，本方法尚未提交，整个数据库事务
        连同已经写下的任何 ``UPDATE`` 一起回滚。成功方向不是原子的：``audit.record()``
        当前的真实实现是结构化日志出口（``audit_event`` 表本身仍未建，见[数据库设计
        「六、管理与待确认操作」](../../../docs/技术设计/数据库设计.md#六管理与待确认操作)），
        一旦这次调用返回（不抛异常）就视为"审计已完成"，但日志行本身不参与、也不可能
        参与 PostgreSQL 的提交/回滚——如果 ``audit.record()`` 成功之后、本方法真正
        ``COMMIT`` 之前又发生了其它导致事务回滚的故障（连接中断、后续语句超时……），
        已经输出的日志行不会被撤回，而 ``pending_action``/``app_user`` 确实回滚了，
        产生一条"审计说发生了，数据库说没发生"的幽灵记录。这是全仓 ``AuditSink``
        抽象的既有性质（结构化日志不是可回滚的存储），不是本方法独有的缺口。

        **为什么角色核对要在同一事务内完成（外部审查交叉裁定，codex P1-4）**：发起人
        当前角色是否仍然有效，判定用的是 :func:`~lingxi.core.admin.pending_action.
        decide_confirm` 需要的 ``registry_entry``——这条读取如果走一条独立的、不受本
        事务任何锁保护的连接，"读到角色"与"提交这次确认"之间就会存在一个 TOCTOU
        窗口：并发的一次撤权可以在这个窗口内悄悄发生而不被本次确认看见，本次确认仍然
        会用撤权前的旧角色执行。下面对 ``admin_registry`` 的查询改为在**同一个连接、
        同一个事务**里，并对目标行取 ``FOR SHARE``——这不要求本方法自己去修改
        ``admin_registry``（``FOR SHARE`` 是共享锁，允许其它事务同时也持共享锁读），
        但会与任何试图 ``UPDATE`` 该行（撤权）的并发事务互斥：撤权事务若已经拿到该行
        的排它锁但还没提交，本方法的 ``FOR SHARE`` 会等它提交或回滚，读到的必然是
        "撤权前"或"撤权后"两个确定状态之一，不会出现两者之间的中间态。

        **数据库瞬时故障不向上传播成裸 psycopg 异常（批次 4 F1，Issue #304）**：本方法
        捕获 ``psycopg.errors.OperationalError`` 并转译为
        :class:`~lingxi.core.admin.pending_action.PendingActionTransientFailure`
        （事务已整体回滚，语义与转译方式见该类文档）。选 ``OperationalError`` 而不是
        分别列举 ``DeadlockDetected``/``LockNotAvailable`` 两个具体子类：psycopg 把
        这两者都归在 ``OperationalError`` 之下（分别对应 SQLSTATE 类 40「事务回滚」
        与类 55「对象当前状态不满足操作前提」），与仓库既有的"只取异常基类"分类
        惯例一致（对照 ``adapters/postgres_identity.py`` 的 ``provision()``：那里按
        ``sqlstate`` 在 ``DriverError``（``psycopg.errors.Error``）这一最宽的基类下
        再细分拒绝原因；本方法不需要细分到"因为哪种操作性故障"再分别处理——两者
        对调用方（``core/admin/card_callback.py``）而言是同一个结论"稍后重试"，
        细分类只作为 ``classification`` 字段进审计，不分叉控制流，因此在
        ``OperationalError`` 这一层捕获就足够，不必逐个 import 更具体的子类）。
        真正实现"执行体"的是 :meth:`_confirm_locked`——拆成独立方法是为了让这层
        ``try/except`` 不必重新缩进整段已经很深的事务体（外层方法只做"调用 + 转译
        异常"，不改变内层任何一行原有逻辑）。
        """

        from psycopg.errors import OperationalError

        try:
            pending, decision = self._confirm_locked(
                pending_action_id=pending_action_id, clicker_open_id=clicker_open_id, now=now
            )
        except OperationalError as error:
            raise PendingActionTransientFailure(type(error).__name__) from error

        if pending is None:
            return ConfirmOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return ConfirmOutcome(decision=decision, pending=refreshed)

    def _confirm_locked(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None
    ) -> tuple[PendingAction | None, ConfirmDecision]:
        """:meth:`confirm` 的事务体本身，逐字保留自拆分前的 ``confirm()``——拆分
        只为了让 :meth:`confirm` 能在外层包一层不影响这里任何缩进的
        ``try/except OperationalError``（见 :meth:`confirm` 文档"数据库瞬时故障"
        一节）。不单独对外暴露，不是 :class:`PendingActionDecider` 协议的一部分。
        """

        pending: PendingAction | None = None
        decision: ConfirmDecision
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s FOR UPDATE",
                        (pending_action_id,),
                    )
                    row = cursor.fetchone()
                    pending = _row_to_pending_action(row) if row is not None else None

                    registry_entry = None
                    if pending is not None:
                        cursor.execute(
                            """
                            SELECT feishu_open_id, label, permission_admin_granted,
                                   ops_admin_granted, super_admin_granted, entry_status
                              FROM admin_registry
                             WHERE feishu_open_id = %s AND entry_status = 'active'
                             FOR SHARE
                            """,
                            (pending.initiated_by_open_id,),
                        )
                        registry_row = cursor.fetchone()
                        registry_entry = (
                            admin_registry_entry_from_row(registry_row)
                            if registry_row is not None
                            else None
                        )

                    current_account_state: str | None = None
                    # 内部 user_id（而非发起/核对全程使用的 feishu_open_id）只在
                    # suspend_user 真正 EXECUTE 时才用得到——排队会话保留正文清理
                    # （见下方 decision.ok 分支）落在 agent_session_cleanup.user_id
                    # 这一列，它引用的是 app_user.id，不是飞书身份。同一条
                    # SELECT ... FOR UPDATE 顺带取出，不为此再加一次查询。
                    target_user_id: str | None = None
                    if pending is not None:
                        cursor.execute(
                            "SELECT id, account_state FROM app_user WHERE feishu_open_id = %s"
                            " FOR UPDATE",
                            (pending.target_open_id,),
                        )
                        target_row = cursor.fetchone()
                        target_user_id = target_row[0] if target_row is not None else None
                        current_account_state = target_row[1] if target_row is not None else None

                        if pending.action_type in _DIRECTION_BY_ACTION_TYPE:
                            # 本地权限两类动作的"当前状态"不是 app_user.account_state
                            # （上面那一列在这里只用来判断目标用户是否还存在）；真正
                            # 的漂移检测不经过这里的字符串比较，而是让下面 EXECUTE
                            # 分支的 INSERT 直接撞迁移 0072 的部分唯一索引（模块文档
                            # 「本地权限授权/抑制如何复用同一套机制」）。这里把
                            # prepare 时刻的快照原样喂回 decide_confirm，让这条比较
                            # 对本地权限动作恒真——除非目标用户在 prepare 到 confirm
                            # 之间被删除（target_user_id 为 None），那种情况下必须
                            # 仍然判定为漂移，不能假装"无变化"。
                            current_account_state = (
                                pending.target_state_snapshot if target_user_id is not None else None
                            )
                        elif pending.action_type is PendingActionType.LOCAL_PERMISSION_REVOKE:
                            # 同一手法（模块文档「本地权限收回如何复用同一套机制」）：
                            # 真正的漂移检测不经过这里的字符串比较，而是让下面 EXECUTE
                            # 分支的条件 UPDATE（_revoke_locked）的 rowcount 说话。
                            current_account_state = (
                                pending.target_state_snapshot if target_user_id is not None else None
                            )

                    # 两把行锁（待确认操作、目标账号）与 admin_registry 的 FOR SHARE
                    # 都已经拿到——现在才取时钟（外部审查交叉裁定，codex P1-3）：等锁
                    # 期间如果被并发事务卡住，锁前取的时间会比真正执行判定的时刻更早，
                    # 让本该过期的操作因为一段等锁窗口而被误判为"仍然有效"。显式注入的
                    # ``now`` 原样使用，不重新取时钟（测试需要确定性时钟）。
                    moment = now if now is not None else datetime.now(timezone.utc)

                    decision = decide_confirm(
                        pending=pending,
                        clicker_open_id=clicker_open_id,
                        now=moment,
                        registry_entry=registry_entry,
                        current_account_state=current_account_state,
                    )

                    if pending is not None and decision.terminal_status is not None:
                        if decision.ok:
                            if pending.action_type in (
                                PendingActionType.SUSPEND_USER,
                                PendingActionType.RESUME_USER,
                            ):
                                assert decision.new_account_state is not None
                                cursor.execute(
                                    "UPDATE app_user SET account_state = %s, updated_at = now()"
                                    " WHERE feishu_open_id = %s AND account_state = %s",
                                    (
                                        decision.new_account_state,
                                        pending.target_open_id,
                                        current_account_state,
                                    ),
                                )
                                if cursor.rowcount != 1:
                                    # SELECT ... FOR UPDATE 已经把目标行锁到本事务提交为止，
                                    # 这里理论上不可能发生；响亮失败好过静默当成"改成功了"。
                                    raise RuntimeError(
                                        "并发修改目标账号状态，预期影响 1 行，"
                                        f"实际 {cursor.rowcount} 行"
                                    )

                                if pending.action_type is PendingActionType.SUSPEND_USER:
                                    # 停用「感知即清」（产品合同「数据保留与删除」：
                                    # 停用一经感知即触发，统一、全部、不可逆清除，
                                    # 不等待下一次问数任务入队）——本事务已经持有目标
                                    # app_user 行锁，"感知"这一刻就是这里：在
                                    # account_state 真正翻转为 suspended 的同一个数据库
                                    # 事务、同一个连接上，为该用户的全部会话排队保留
                                    # 正文清理。用的是 assert 之前已经证明非空的
                                    # target_user_id（本分支能走到，说明上面按
                                    # feishu_open_id 查到的行确实存在且刚被本事务改过）。
                                    # 复用 _Transaction 上与 clear_delivered_content_
                                    # for_conversation 同型的方法（Issue #304 批次 4 从
                                    # postgres_conversation 独立事务实现下沉而来，见该
                                    # 方法文档"为什么"）：这不是新发明一条同事务写路径
                                    # ——_Transaction 本就是 postgres_conversation 包
                                    # __init__.py 显式 re-export、供包外调用方复用的
                                    # 类型（模块文档"既有调用点用到的名字全部保留"）。
                                    # 审计写入失败、或本方法后面任何一步导致事务整体
                                    # 回滚时，这里已经排队的清理请求随事务一起撤销，
                                    # 不会出现"账号其实没改成功、清理却已经生效"的
                                    # 不一致（PendingActionAuditWriteFailed 分支同一
                                    # 姿态）。
                                    assert target_user_id is not None
                                    _Transaction(connection).clear_delivered_content_for_user(
                                        user_id=target_user_id, reason="user_cleared"
                                    )
                                    # 用户记忆同一姿态一并清除（Issue #357 S-H3-3
                                    # c 节）：同一个已持有的 connection/事务，失败
                                    # 一起回滚，不产生"账号已停用、记忆却还在"的
                                    # 半套状态。记忆没有"resume 恢复"语义（硬
                                    # DELETE，见迁移 0076），与上面的保留正文清理
                                    # 同一条不变量。
                                    _Transaction(connection).clear_user_memory(
                                        user_id=target_user_id
                                    )
                            elif pending.action_type in _DIRECTION_BY_ACTION_TYPE:
                                # 本地权限授权/抑制的 EXECUTE 分支：不改 app_user，
                                # 改写迁移 0072 的 local_permission_override 表
                                # （模块文档「本地权限授权/抑制如何复用同一套机制」）。
                                # target_user_id 在这里结构上不可能为 None——上面
                                # 把 current_account_state 设为 None 的唯一场景
                                # （目标用户已被删除）会让 decide_confirm 判定
                                # TARGET_DRIFTED 而不是 EXECUTE，走不到这个分支。
                                assert target_user_id is not None
                                assert pending.payload is not None
                                payload_data = json.loads(pending.payload)
                                entry = LocalPermissionOverrideEntry(
                                    user_id=target_user_id,
                                    direction=_DIRECTION_BY_ACTION_TYPE[pending.action_type],
                                    company_id=payload_data["company_id"],
                                    metric_name=payload_data["metric_name"],
                                    reason=payload_data["reason"],
                                    initiated_by_open_id=pending.initiated_by_open_id,
                                    pending_action_id=pending.id,
                                    created_at=moment,
                                )
                                override_id = new_id("lpo")
                                try:
                                    # 嵌套事务块 = psycopg 的 SAVEPOINT，与 prepare()
                                    # 对 pending_action_single_pending_target_idx 的
                                    # 处理同一姿态：撞上迁移 0072 的部分唯一索引时只
                                    # 回滚这条 INSERT 本身，外层事务（pending_action
                                    # 终态更新、审计）仍然可以正常提交。撞索引说明
                                    # prepare 到这一刻之间，已经有另一笔在同一个键上
                                    # 抢先落地——降级为既有 TARGET_DRIFTED/FAILED，
                                    # 不新增错误码（模块文档）。
                                    with connection.transaction():
                                        _insert_locked(cursor, override_id=override_id, entry=entry)
                                except DuplicateActiveOverride:
                                    decision = ConfirmDecision(
                                        kind=ConfirmResultKind.TARGET_DRIFTED,
                                        message="目标用户状态已经变化，请重新查询后再发起。",
                                        terminal_status=PendingActionStatus.FAILED,
                                        reason="target_drifted",
                                    )
                            elif pending.action_type is PendingActionType.LOCAL_PERMISSION_REVOKE:
                                # 本地权限收回的 EXECUTE 分支：不改 app_user，条件
                                # UPDATE 迁移 0072 的 local_permission_override 表
                                # （模块文档「本地权限收回如何复用同一套机制」）。
                                # payload 在 prepare() 时刻已经把 override_id 连同
                                # 被收回那一行的方向/公司/指标一起写入（供
                                # core/admin/notification.py 渲染）。
                                assert pending.payload is not None
                                payload_data = json.loads(pending.payload)
                                revoked = _revoke_locked(
                                    cursor,
                                    override_id=payload_data["override_id"],
                                    revoked_pending_action_id=pending.id,
                                    moment=moment,
                                )
                                if not revoked:
                                    # 与 grant/suppress 的 DuplicateActiveOverride
                                    # 降级同一姿态，只是这里的冲突信号是条件更新的
                                    # 影响行数（rowcount == 0），不是唯一索引异常
                                    # ——prepare 到这一刻之间，这一行已经被另一条
                                    # 路径抢先收回（entry_status 不再是 'active'）。
                                    decision = ConfirmDecision(
                                        kind=ConfirmResultKind.TARGET_DRIFTED,
                                        message="目标用户状态已经变化，请重新查询后再发起。",
                                        terminal_status=PendingActionStatus.FAILED,
                                        reason="target_drifted",
                                    )

                        try:
                            self._audit.record(
                                "admin.pending_action.confirmed",
                                pending_action_id=pending.id,
                                action_type=pending.action_type.value,
                                outcome=decision.kind.value,
                                initiated_by=pending.initiated_by_open_id,
                                clicker=clicker_open_id,
                            )
                        except Exception as error:  # noqa: BLE001 - 见类文档，失败即整体回滚
                            raise PendingActionAuditWriteFailed(
                                "确认操作的审计写入失败，事务已回滚，操作未执行"
                            ) from error

                        cursor.execute(
                            "UPDATE pending_action SET status = %s, reason = %s,"
                            " decided_at = %s, decided_by_open_id = %s"
                            " WHERE id = %s AND status = 'pending'",
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            # 锁失效场景下的纵深防线（外部审查交叉裁定，opus P3-2）：
                            # 本方法从取得行锁到这里全程持有它，正常情况下不可能有
                            # 别的事务在期间把状态改离 'pending'；响亮失败好过静默
                            # 当成"改成功了"（与上面 app_user 的 rowcount 校验同一
                            # 姿态）。
                            raise RuntimeError(
                                "待确认操作状态在本事务持锁期间被改变，预期影响 1 行，"
                                f"实际 {cursor.rowcount} 行"
                            )

        return pending, decision

    def cancel(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None = None
    ) -> CancelOutcome:
        """"取消"按钮的事务：核对链更短（见 ``decide_cancel``），同样在同一事务内
        先写审计、失败即整体回滚。"审计与时钟"两条注意事项与 :meth:`~.confirm`
        文档同一姿态（结构化日志出口的"同一事务"只在失败方向成立；时钟在拿到行锁
        之后才取，外部审查交叉裁定，codex P1-3）。

        数据库瞬时故障的捕获与转译同 :meth:`~.confirm`（批次 4 F1，Issue #304）：
        本方法风险面更小（不涉及 ``clear_delivered_content_for_user`` 那组容易
        撞锁序的写入），但 ``pending_action``/``app_user`` 的 ``FOR UPDATE`` 本身
        仍可能撞上并发持锁超过 ``lock_timeout``，同一姿态兜底，不留一个不对称的
        缺口。
        """

        from psycopg.errors import OperationalError

        try:
            pending, decision = self._cancel_locked(
                pending_action_id=pending_action_id, clicker_open_id=clicker_open_id, now=now
            )
        except OperationalError as error:
            raise PendingActionTransientFailure(type(error).__name__) from error

        if pending is None:
            return CancelOutcome(decision=decision, pending=None)
        refreshed = self.get(pending_action_id=pending_action_id)
        assert refreshed is not None
        return CancelOutcome(decision=decision, pending=refreshed)

    def _cancel_locked(
        self, *, pending_action_id: str, clicker_open_id: str, now: datetime | None
    ) -> tuple[PendingAction | None, CancelDecision]:
        """:meth:`cancel` 的事务体本身，拆分理由与 :meth:`_confirm_locked` 相同。"""

        pending: PendingAction | None = None
        decision: CancelDecision
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM pending_action WHERE id = %s FOR UPDATE",
                        (pending_action_id,),
                    )
                    row = cursor.fetchone()
                    pending = _row_to_pending_action(row) if row is not None else None

                    moment = now if now is not None else datetime.now(timezone.utc)

                    decision = decide_cancel(
                        pending=pending, clicker_open_id=clicker_open_id, now=moment
                    )

                    if pending is not None and decision.terminal_status is not None:
                        try:
                            self._audit.record(
                                "admin.pending_action.cancelled",
                                pending_action_id=pending.id,
                                action_type=pending.action_type.value,
                                outcome=decision.kind.value,
                                initiated_by=pending.initiated_by_open_id,
                                clicker=clicker_open_id,
                            )
                        except Exception as error:  # noqa: BLE001 - 见 confirm() 同一姿态
                            raise PendingActionAuditWriteFailed(
                                "取消操作的审计写入失败，事务已回滚，操作未执行"
                            ) from error

                        cursor.execute(
                            "UPDATE pending_action SET status = %s, reason = %s,"
                            " decided_at = %s, decided_by_open_id = %s"
                            " WHERE id = %s AND status = 'pending'",
                            (
                                decision.terminal_status.value,
                                decision.reason,
                                moment,
                                clicker_open_id,
                                pending.id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            # 锁失效场景下的纵深防线，与 confirm() 同一姿态（外部审查
                            # 交叉裁定，opus P3-2）。
                            raise RuntimeError(
                                "待确认操作状态在本事务持锁期间被改变，预期影响 1 行，"
                                f"实际 {cursor.rowcount} 行"
                            )

        return pending, decision
