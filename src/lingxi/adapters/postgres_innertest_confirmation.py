"""本人确认事务：资格、版本、唯一持久阶段与审计一起提交。"""

from dataclasses import replace
from datetime import UTC, datetime

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_admin_followup import enqueue_followups
from lingxi.adapters.postgres_innertest import InnertestPrincipal
from lingxi.adapters.postgres_pending_action import ConfirmOutcome
from lingxi.core.admin.followup import FollowupSpec
from lingxi.core.admin.innertest import InnertestError, target_digest
from lingxi.core.admin.pending_action import ConfirmDecision, ConfirmResultKind, PendingActionStatus

#: 落明确终态的拒绝分支。过期与目标漂移是既有的两条；授权已变化补进来是因为
#: 它同样是「这批不可能再被批准」的确定结果——留在 ``pending`` 会让卡片按钮
#: 一直在，绑定若已转给他人就再也没人能收口它。
_TERMINAL_REJECTIONS = frozenset(
    {
        ConfirmResultKind.EXPIRE,
        ConfirmResultKind.TARGET_DRIFTED,
        ConfirmResultKind.ROLE_REVOKED,
    }
)


def _rejection_message(code):
    """按拒绝分支给管理员一句能据以行动的话。"""
    if code is ConfirmResultKind.TARGET_DRIFTED:
        return "人员资料已变化，本批已失效，请重新查询后准备。"
    if code is ConfirmResultKind.ROLE_REVOKED:
        return "发起这批之后管理授权已变化，本次未执行；请重新发起。"
    return "操作不存在、已失效或不是本人确认。"


class InnertestPendingActions:
    """现有确认入口的扩员适配，其他管理动作逐字委托既有仓储。"""

    def __init__(self, original, service):
        """不创建第二种回调入口或持久动作表。"""
        self.original, self.service = original, service

    def __getattr__(self, name):
        """现有卡片记账与其他管理能力保留原实现。"""
        return getattr(self.original, name)

    def confirm(self, *, pending_action_id, clicker_open_id, now=None, trace_id=None):
        """先判动作种类；扩员仍只允许飞书验证的点击主体。"""
        pending = self.original.get(pending_action_id=pending_action_id)
        if pending is None or pending.action_type.value != "innertest_additions":
            return self.original.confirm(
                pending_action_id=pending_action_id,
                clicker_open_id=clicker_open_id,
                now=now,
                trace_id=trace_id,
            )
        return self._decide(pending_action_id, clicker_open_id, now, cancel=False)

    def cancel(self, *, pending_action_id, clicker_open_id, now=None):
        """取消也重核绑定，旧卡或他人不能更改批次。"""
        pending = self.original.get(pending_action_id=pending_action_id)
        if pending is None or pending.action_type.value != "innertest_additions":
            return self.original.cancel(
                pending_action_id=pending_action_id, clicker_open_id=clicker_open_id, now=now
            )
        return self._decide(pending_action_id, clicker_open_id, now, cancel=True)

    def _decide(self, action, clicker, now, *, cancel):
        """锁次序为名单版本、动作、绑定；并发同版本只有一个确认成功。"""
        service = self.service
        with connect(service.dsn) as connection, connection.transaction():
            with connection.cursor() as cursor:
                version = service._version(cursor, lock=True)
                pending = self.original._lock_pending_action_row(cursor, action)
                cursor.execute(
                    "SELECT id,initiated_by,binding_id,binding_version,roster_version,trace_id,target_digest "
                    "FROM innertest_batch WHERE pending_action_id=%s AND scope=%s FOR UPDATE",
                    (action, service.scope),
                )
                batch = cursor.fetchone()
                moment = now or datetime.now(UTC)
                code = self._guarded_code(connection, pending, batch, clicker, version, moment)
                if code is None and not cancel:
                    code = self._confirm_current_items(connection, cursor, batch, action)
                if code:
                    return self._rejected(connection, pending, batch, code, moment)
                if not cancel:
                    cursor.execute(
                        "UPDATE innertest_roster_version SET version=version+1,"
                        "updated_at=now() WHERE scope=%s",
                        (service.scope,),
                    )
                state = "cancelled" if cancel else "executed"
                cursor.execute(
                    "UPDATE pending_action SET status=%s,decided_at=%s,"
                    "decided_by_open_id=%s WHERE id=%s",
                    (state, moment, clicker, action),
                )
                cursor.execute(
                    "UPDATE innertest_batch SET status=%s WHERE id=%s", (state, batch[0])
                )
                service._audit(connection, batch[0], clicker, batch[5], state)
                updated = replace(
                    pending,
                    status=PendingActionStatus(state),
                    decided_at=moment,
                    decided_by_open_id=clicker,
                )
                enqueue_followups(
                    connection,
                    pending_action_id=action,
                    trace_id=batch[5],
                    items=(
                        FollowupSpec(
                            subject_key=batch[0], stage="terminal_card_refresh", batch_id=batch[0]
                        ),
                    ),
                )
        decision = ConfirmDecision(
            kind=ConfirmResultKind.EXECUTE,
            message="已取消。" if cancel else "已加入内测资格，开通结果请逐人查询。",
            terminal_status=PendingActionStatus(state),
        )
        return ConfirmOutcome(decision=decision, pending=updated)

    #: ``_principal`` 用来表达「发起这批时的授权已经不成立」的两个码。绑定被换、
    #: 被停用、版本与批次记录的不一致都归到这里；其余码（名单不可用、审计写不了）
    #: 不是授权问题，继续原样抛出，不能混进同一句回执里。
    _AUTHORIZATION_CODES = frozenset({"binding_disabled", "not_authorized"})

    def _guarded_code(self, connection, pending, batch, clicker, version, now):
        """授权类失败收口成明确拒绝，走与角色被撤同一条终态路径。

        这类失败在事务里被拒、零写入，结果是确定的。让它冒到卡片回调的兜底里，
        管理员只会看到一句结果不明：既不知道有没有执行，也不知道该重新发起；
        而待确认操作会停在 ``pending``，卡片按钮还在，绑定若已转给他人就永远
        不会收口。
        """
        try:
            return self._guard(connection, pending, batch, clicker, version, now)
        except InnertestError as error:
            if error.code not in self._AUTHORIZATION_CODES:
                raise
            return ConfirmResultKind.ROLE_REVOKED

    def _guard(self, connection, pending, batch, clicker, version, now):
        """送达与本人、动作和版本四条件同时成立才可写资格。"""
        if pending is None or batch is None or not pending.card_delivered:
            return ConfirmResultKind.NOT_FOUND
        if pending.action_type.value != "innertest_additions":
            return ConfirmResultKind.NOT_FOUND
        if (
            pending.initiated_by_open_id != clicker
            or pending.target_open_id != clicker
            or batch[1] != clicker
        ):
            return ConfirmResultKind.NOT_INITIATOR
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT email,open_id,personnel_id,result_code FROM innertest_batch_item WHERE batch_id=%s",
                (batch[0],),
            )
            if (
                pending.status is PendingActionStatus.PENDING
                and target_digest(cursor.fetchall()) != batch[6]
            ):
                return ConfirmResultKind.TARGET_DRIFTED
        # 终态检查必须排在授权校验之前：已经终态的批次无论授权还成不成立都不该
        # 再被改写。反过来的次序会让「已执行 + 事后撤权 + 重放点击」把 executed
        # 覆盖成 failed，资格却还在，回执还说本次未执行——等于抹掉真实结果。
        if pending.status is not PendingActionStatus.PENDING:
            return ConfirmResultKind.ALREADY_TERMINAL
        principal = InnertestPrincipal(batch[2], batch[1], batch[3])
        self.service._principal(connection, principal)
        if now >= pending.confirm_deadline_at:
            return ConfirmResultKind.EXPIRE
        if batch[4] != version or pending.target_state_snapshot != str(version):
            return ConfirmResultKind.TARGET_DRIFTED
        return None

    def _confirm_current_items(self, connection, cursor, batch, action):
        """先撤销本批所有资格和阶段写入，再在外层有效事务中记录明确失效。"""
        try:
            with connection.transaction():
                self._confirm_items(connection, cursor, batch, action)
        except InnertestError as error:
            if error.code != "stale_confirmation":
                raise
            return ConfirmResultKind.TARGET_DRIFTED
        return None

    def _confirm_items(self, connection, cursor, batch, action):
        """新增每人两阶段，未建档 user_id 保持 NULL；无提交后内存队列。"""
        cursor.execute(
            "SELECT id,email,open_id,personnel_id FROM innertest_batch_item "
            "WHERE batch_id=%s AND result_code='new' ORDER BY id FOR UPDATE",
            (batch[0],),
        )
        items = cursor.fetchall()
        if not items:
            raise InnertestError("stale_confirmation")
        for item_id, email, open_id, personnel in items:
            target = self.service.locator(email, connection=connection)
            if (
                getattr(target, "open_id", None) != open_id
                or getattr(target, "personnel_id", None) != personnel
            ):
                raise InnertestError("stale_confirmation")
            cursor.execute(
                "INSERT INTO innertest_membership(scope,open_id,email,batch_id) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING open_id",
                (self.service.scope, open_id, email, batch[0]),
            )
            if cursor.fetchone() is None:
                raise InnertestError("stale_confirmation")
            cursor.execute(
                "SELECT id,permission_version FROM app_user WHERE feishu_open_id=%s FOR SHARE",
                (open_id,),
            )
            user = cursor.fetchone()
            ref = self._enqueue_person(connection, action, batch, item_id, user)
            cursor.execute(
                "UPDATE innertest_batch_item SET result_code='added',followup_id=%s WHERE id=%s",
                (ref.id, item_id),
            )

    @staticmethod
    def _enqueue_person(connection, action, batch, item_id, user):
        """两阶段沿用唯一仓储端口，不另存领取状态。"""
        ref = enqueue_followups(
            connection,
            pending_action_id=action,
            trace_id=batch[5],
            items=(
                FollowupSpec(
                    subject_key=item_id,
                    stage="innertest_preprovision",
                    batch_id=batch[0],
                    batch_item_id=item_id,
                    target_user_id=user[0] if user else None,
                    target_version=user[1] if user else None,
                ),
            ),
        )[0]
        enqueue_followups(
            connection,
            pending_action_id=action,
            trace_id=batch[5],
            items=(
                FollowupSpec(
                    subject_key=item_id,
                    stage="innertest_readiness_check",
                    batch_id=batch[0],
                    batch_item_id=item_id,
                    target_user_id=user[0] if user else None,
                    target_version=user[1] if user else None,
                    depends_on_id=ref.id,
                ),
            ),
        )
        return ref

    def _rejected(self, connection, pending, batch, code, moment):
        """过期和目标漂移落明确终态，原请求仍可查且不能继续批准。"""
        decision = ConfirmDecision(
            kind=code,
            message=_rejection_message(code),
        )
        if decision.kind in _TERMINAL_REJECTIONS:
            state = "expired" if decision.kind is ConfirmResultKind.EXPIRE else "failed"
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE pending_action SET status=%s,decided_at=%s,reason=%s WHERE id=%s",
                    (state, moment, decision.kind.value, pending.id),
                )
                cursor.execute(
                    "UPDATE innertest_batch SET status=%s WHERE id=%s", (state, batch[0])
                )
            self.service._audit(connection, batch[0], batch[1], batch[5], state)
            pending = replace(pending, status=PendingActionStatus(state), decided_at=moment)
            decision = replace(
                decision, terminal_status=PendingActionStatus(state), reason=decision.kind.value
            )
            enqueue_followups(
                connection,
                pending_action_id=pending.id,
                trace_id=batch[5],
                items=(
                    FollowupSpec(
                        subject_key=batch[0], stage="terminal_card_refresh", batch_id=batch[0]
                    ),
                ),
            )
        return ConfirmOutcome(decision=decision, pending=pending)
