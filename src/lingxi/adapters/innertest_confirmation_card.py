"""Gateway 消费准备卡意图，scheduler 不取得飞书发送凭据。"""

from datetime import UTC, datetime

from lingxi.adapters.postgres_innertest import InnertestPrincipal
from lingxi.core.admin.followup_consumer import FollowupResult
from lingxi.core.admin.innertest import InnertestError, target_digest
from lingxi.core.admin.notification import (
    ConfirmCardButton,
    RenderedConfirmCard,
    render_card_payload,
)


class InnertestConfirmationCard:
    """发卡结果不明保持 unknown，不因查询或同 request_key 重新发送。"""

    def __init__(self, *, service, store, create_card, send_card):
        """CardKit 建卡与发送分开，取得 card_id 后先持久保存。"""
        self.service, self.store = service, store
        self.create_card, self.send_card = create_card, send_card

    def __call__(self, item):
        """发送前重读本人绑定和批次；网络在事务外。"""
        try:
            target = self._read(item)
        except InnertestError as error:
            return FollowupResult("skipped", error.code)
        if target is None:
            return FollowupResult("skipped", "stale_confirmation")
        recipient, card, card_id = target
        if card_id is None:
            card_id = self.create_card(render_card_payload(card))
            with self.store.transaction() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE pending_action p SET card_id=%s FROM admin_action_followup f "
                    "WHERE p.id=%s AND f.id=%s AND f.lease_owner=%s AND f.attempt=%s "
                    "AND f.status='running' AND p.status='pending'",
                    (card_id, item.pending_action_id, item.id, item.lease_owner, item.attempt),
                )
                if cursor.rowcount != 1:
                    return FollowupResult("unknown", "card_unknown")
        if self._read(item) is None:
            return FollowupResult("skipped", "stale_confirmation")
        try:
            message_id = self.send_card(
                open_id=recipient,
                card={"type": "card", "data": {"card_id": card_id}},
                dedupe_key=item.id,
            )
        except Exception as error:
            return FollowupResult(
                "failed" if getattr(error, "definite", False) else "unknown",
                "card_failed" if getattr(error, "definite", False) else "card_unknown",
            )
        if not message_id:
            return FollowupResult("unknown", "card_unknown")
        self._delivered(item, card_id, message_id)
        return FollowupResult(external_ref=message_id)

    def _read(self, item):
        """每次发送动作前校验当前绑定，旧卡、旧版本不发。"""
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.initiated_by,b.binding_id,b.binding_version,b.roster_version,"
                "p.status,p.confirm_deadline_at,p.card_id,p.card_delivered,b.target_digest FROM innertest_batch b "
                "JOIN pending_action p ON p.id=b.pending_action_id WHERE b.id=%s AND b.scope=%s",
                (item.batch_id, self.service.scope),
            )
            row = cursor.fetchone()
            if row is None or row[4] != "pending" or row[5] <= datetime.now(UTC) or row[7]:
                return None
            self.service._principal(connection, InnertestPrincipal(row[1], row[0], row[2]))
            if self.service._version(cursor) != row[3]:
                return None
            cursor.execute(
                "SELECT email,open_id,personnel_id,result_code FROM innertest_batch_item WHERE batch_id=%s",
                (item.batch_id,),
            )
            if target_digest(cursor.fetchall()) != row[8]:
                return None
            cursor.execute(
                "SELECT email,personnel_id FROM innertest_batch_item "
                "WHERE batch_id=%s AND result_code='new' ORDER BY email",
                (item.batch_id,),
            )
            people = cursor.fetchall()
        body = f"加入内测资格，不授业务权限；开通结果逐人查询。\n实际新增 {len(people)} 人：\n"
        body += "\n".join(f"{email}（{personnel}）" for email, personnel in people)
        body += "\n请本人在10分钟内确认；取消不新增资格。"
        buttons = tuple(
            ConfirmCardButton(
                label=label,
                value={"pending_action_id": item.pending_action_id, "decision": decision},
            )
            for label, decision in (("确认执行", "confirm"), ("取消", "cancel"))
        )
        return (
            row[0],
            RenderedConfirmCard(title="确认加入内测资格", body=body, buttons=buttons),
            row[6],
        )

    def _delivered(self, item, card_id, message_id):
        """平台接收与阶段成功同事务；中断仍有外发标记而不是未发。"""
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_action_followup SET status='succeeded',result_code='card_delivered',"
                "external_ref=%s,finished_at=now(),updated_at=now(),lease_owner=NULL,lease_until=NULL "
                "WHERE id=%s AND status='running' AND lease_owner=%s AND attempt=%s RETURNING id",
                (message_id, item.id, item.lease_owner, item.attempt),
            )
            if cursor.fetchone() is None:
                return
            cursor.execute(
                "UPDATE pending_action SET card_delivered=true,card_id=%s,"
                "card_sequence=GREATEST(card_sequence,2) WHERE id=%s AND status='pending'",
                (card_id, item.pending_action_id),
            )
