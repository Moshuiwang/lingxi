"""#673「跟我们说过话」状态留存：入站到期后不丢失，旁路两态各自可分辨。

真库断言：迁移 `0095` 新增的两个触发器——入站侧（``PostgresGatewayStore.
transaction().insert_inbound_event``）与建档侧（认领此前已存在的入站）——都只能
经真实调用点走到 `app_user` 四列，不经种列；到期删除 `inbound_event` 后四列不受
影响是本单成立与否的唯一硬判据（见 `AppUserSurvivesInboundEventExpiryTest`）。
三态读数与「旧成功不覆盖新失败」见 `ContactStateOrderingTest`。
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from postgres_schema import psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import PostgresGatewayStore
from lingxi.adapters.postgres_identity import (
    CONTACT_REACHABLE,
    CONTACT_UNAVAILABLE,
    CONTACT_UNKNOWN,
    PostgresAppUserStore,
)
from lingxi.core.ids import new_id

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，数据库约束类断言未验证（需真实 PostgreSQL）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，数据库约束类断言未验证"
)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class ContactReachabilityPostgresTestCase(unittest.TestCase):
    """建一行最小合法 ``app_user`` 供本文件全部用例复用。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.store = PostgresAppUserStore(self._dsn)
        self.gateway = PostgresGatewayStore(self._dsn)
        self.open_id = f"ou_{new_id('test')}"
        self._insert_app_user(self.open_id)

    def _insert_app_user(self, open_id: str) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      department, tenant_key, provisioning_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')""",
                (
                    new_id("usr"),
                    open_id,
                    f"{open_id}_u",
                    f"{open_id}_un",
                    "测试用户",
                    "测试部门",
                    "t1",
                ),
            )

    def _column(self, open_id: str, column: str) -> object:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {column} FROM app_user WHERE feishu_open_id = %s", (open_id,))
            row = cursor.fetchone()
        return row[0] if row else None

    def _insert_real_inbound_event(self, *, open_id: str, event_id: str) -> None:
        """走真实入站路径的同一个适配器方法——本文件不直接 UPDATE app_user 四列。"""
        with self.gateway.transaction() as tx:
            tx.insert_inbound_event(
                event_id=event_id,
                event_type="im.message.receive_v1",
                user_open_id=open_id,
                trace_id=f"trc_{event_id}",
            )

    def _delete_all_inbound_events(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM inbound_event")


class RealInboundPathTest(ContactReachabilityPostgresTestCase):
    """② 的写入面：四列只能经真实入站路径写入。"""

    def test_a_real_inbound_event_stamps_first_and_last_inbound_at(self) -> None:
        self.assertIsNone(self._column(self.open_id, "first_inbound_at"))

        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_1")

        first = self._column(self.open_id, "first_inbound_at")
        last = self._column(self.open_id, "last_inbound_at")
        self.assertIsNotNone(first)
        self.assertEqual(first, last)

    def test_a_second_real_inbound_event_advances_last_but_not_first(self) -> None:
        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_1")
        first_after_one = self._column(self.open_id, "first_inbound_at")

        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_2")

        self.assertEqual(self._column(self.open_id, "first_inbound_at"), first_after_one)
        self.assertGreaterEqual(
            self._column(self.open_id, "last_inbound_at"),
            self._column(self.open_id, "first_inbound_at"),
        )

    def test_an_inbound_event_before_the_app_user_row_exists_is_adopted_at_provisioning(
        self,
    ) -> None:
        """当时不建幽灵行：入站早于建档是一次 0 行的空写；建档后这条早到的事件被认领。"""
        open_id = "ou_speaks_before_provisioning"
        self._insert_real_inbound_event(open_id=open_id, event_id="evt_before_provisioning")
        self.assertIsNone(self.store.get_by_open_id(open_id), "此时不建幽灵行")

        self._insert_app_user(open_id)

        first = self._column(open_id, "first_inbound_at")
        self.assertIsNotNone(first, "建档时应认领此前已经存在的入站事件")
        self.assertEqual(first, self._column(open_id, "last_inbound_at"))

    def test_a_later_real_inbound_event_clears_a_recorded_unavailable_mark(self) -> None:
        """⑤：同一人后来成功入站会清掉「发不进去」这个标记。"""
        self.store.record_contact_unavailable(
            open_id=self.open_id, when=datetime.now(UTC), code="feishu_code_230013"
        )
        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)

        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_speaks_again")

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_REACHABLE)
        self.assertIsNone(self._column(self.open_id, "outbound_unavailable_at"))
        self.assertIsNone(self._column(self.open_id, "outbound_unavailable_code"))


class AppUserSurvivesInboundEventExpiryTest(ContactReachabilityPostgresTestCase):
    """② 本单成立与否的唯一硬判据 + ⑥ 的否定断言。"""

    def test_first_and_last_inbound_at_do_not_change_after_inbound_event_is_purged(self) -> None:
        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_1")
        first_before = self._column(self.open_id, "first_inbound_at")
        last_before = self._column(self.open_id, "last_inbound_at")

        self._delete_all_inbound_events()

        self.assertEqual(self._column(self.open_id, "first_inbound_at"), first_before)
        self.assertEqual(self._column(self.open_id, "last_inbound_at"), last_before)

    def test_clearing_the_whole_inbound_event_table_does_not_re_mark_an_old_talker_as_new(
        self,
    ) -> None:
        """否定断言（⑥）：把入站事件整表清空后，老用户不会被重新判为「从没说过话」。

        判据直接调用两处守卫真正读的谓词等价写法——``first_inbound_at IS NULL``；
        真正的守卫本身在 ``test_identity_postgres_records.py`` /
        ``test_postgres_late_readiness_recovery.py`` 已经用真实调用覆盖，这里额外
        钉住"清表不改变判定"这个更直接的属性。
        """
        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_1")

        self._delete_all_inbound_events()

        armed = self.store.mark_preprovision_notice_pending(open_id=self.open_id)
        self.assertFalse(armed, "入站事件已被清空，但持久状态仍记得这个人说过话")
        self.assertIsNone(self._column(self.open_id, "preprovision_notice_armed_at"))

    def test_an_event_adopted_at_provisioning_time_also_survives_the_purge(self) -> None:
        """③ 与②的组合：建档时认领的入站同样不随 `inbound_event` 到期消失。"""
        open_id = "ou_speaks_before_provisioning_survives"
        self._insert_real_inbound_event(open_id=open_id, event_id="evt_before_provisioning_2")
        self._insert_app_user(open_id)
        first_before = self._column(open_id, "first_inbound_at")
        last_before = self._column(open_id, "last_inbound_at")
        self.assertIsNotNone(first_before)

        self._delete_all_inbound_events()

        self.assertEqual(self._column(open_id, "first_inbound_at"), first_before)
        self.assertEqual(self._column(open_id, "last_inbound_at"), last_before)
        self.assertFalse(self.store.mark_preprovision_notice_pending(open_id=open_id))


class ContactStateOrderingTest(ContactReachabilityPostgresTestCase):
    """⑤：三态可分辨，未知不按可用处理；旧成功不覆盖新失败。"""

    def test_a_brand_new_row_is_unknown_not_available(self) -> None:
        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNKNOWN)

    def test_an_unknown_open_id_reads_as_no_row_not_as_unknown_state(self) -> None:
        self.assertIsNone(self.store.contact_reachability(open_id="ou_never_heard_of"))

    def test_a_successful_bypass_marks_reachable(self) -> None:
        self.store.record_contact_reachable(open_id=self.open_id, when=datetime.now(UTC))

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_REACHABLE)

    def test_a_failed_bypass_marks_unavailable(self) -> None:
        self.store.record_contact_unavailable(
            open_id=self.open_id, when=datetime.now(UTC), code="feishu_code_230013"
        )

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)
        self.assertEqual(
            self._column(self.open_id, "outbound_unavailable_code"), "feishu_code_230013"
        )

    def test_an_old_success_does_not_override_a_newer_failure(self) -> None:
        """旧成功不覆盖新失败：失败发生在成功之后，重放一次更早的成功结果时不得清掉它。"""
        now = datetime.now(UTC)
        earlier = now - timedelta(hours=1)
        self.store.record_contact_unavailable(
            open_id=self.open_id, when=now, code="feishu_code_230013"
        )

        self.store.record_contact_reachable(open_id=self.open_id, when=earlier)

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)
        self.assertEqual(
            self._column(self.open_id, "outbound_unavailable_code"), "feishu_code_230013"
        )

    def test_a_newer_success_does_override_an_older_failure(self) -> None:
        """对照组：成功确实晚于失败时，标记应当被清掉——上一条用例不是"永不清除"。"""
        earlier = datetime.now(UTC) - timedelta(hours=1)
        self.store.record_contact_unavailable(
            open_id=self.open_id, when=earlier, code="feishu_code_230013"
        )

        self.store.record_contact_reachable(open_id=self.open_id, when=datetime.now(UTC))

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_REACHABLE)

    def test_a_failure_older_than_the_latest_inbound_does_not_mark_unavailable(self) -> None:
        """旧失败不覆盖新事实：这个人在失败时刻之后又开过口，失败已经是陈旧证据。"""
        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_spoke_after")
        stale = datetime.now(UTC) - timedelta(days=3)

        written = self.store.record_contact_unavailable(
            open_id=self.open_id, when=stale, code="feishu_code_230013"
        )

        self.assertFalse(written)
        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_REACHABLE)

    def test_a_failure_older_than_the_recorded_failure_does_not_move_it_back(self) -> None:
        newer = datetime.now(UTC)
        self.store.record_contact_unavailable(open_id=self.open_id, when=newer, code="newer")

        written = self.store.record_contact_unavailable(
            open_id=self.open_id, when=newer - timedelta(hours=2), code="older"
        )

        self.assertFalse(written)
        self.assertEqual(self._column(self.open_id, "outbound_unavailable_code"), "newer")

    def test_a_replayed_earlier_first_contact_moves_first_inbound_at_back(self) -> None:
        """④ 回填拨得回真正首次：先记 now，再回放一条更早的证据，first 要往前拨。"""
        now = datetime.now(UTC)
        earlier = now - timedelta(days=30)
        self.store.record_contact_reachable(open_id=self.open_id, when=now)

        self.store.record_contact_reachable(open_id=self.open_id, when=earlier)

        self.assertEqual(self._column(self.open_id, "first_inbound_at"), earlier)
        self.assertEqual(self._column(self.open_id, "last_inbound_at"), now)

    def test_a_backfilled_inbound_event_older_than_a_recorded_failure_does_not_clear_it(
        self,
    ) -> None:
        """⑤ 对称守卫：比已记录失败更旧的入站事件（回填/延迟到达）不该把它清掉。"""
        self.store.record_contact_unavailable(
            open_id=self.open_id, when=datetime.now(UTC), code="feishu_code_230013"
        )
        stale = datetime.now(UTC) - timedelta(days=3)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO inbound_event
                     (feishu_event_id, received_at, event_type, user_open_id, trace_id)
                   VALUES (%s, %s, 'im.message.receive_v1', %s, %s)""",
                ("evt_stale_backfill", stale, self.open_id, "trc_stale_backfill"),
            )

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)

    def test_a_live_failure_after_an_inbound_is_still_recorded(self) -> None:
        """对照组：失败晚于最近入站时照常落下——上两条不是"永远写不进去"。"""
        self._insert_real_inbound_event(open_id=self.open_id, event_id="evt_before_failure")

        written = self.store.record_contact_unavailable(
            open_id=self.open_id,
            when=datetime.now(UTC) + timedelta(seconds=1),
            code="feishu_code_230013",
        )

        self.assertTrue(written)
        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)

    def test_record_contact_reachable_returns_false_for_an_unknown_open_id(self) -> None:
        self.assertFalse(
            self.store.record_contact_reachable(open_id="ou_nobody", when=datetime.now(UTC))
        )

    def test_record_contact_unavailable_returns_false_for_an_unknown_open_id(self) -> None:
        self.assertFalse(
            self.store.record_contact_unavailable(
                open_id="ou_nobody", when=datetime.now(UTC), code="feishu_code_230013"
            )
        )


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))

    def actions(self) -> list[str]:
        return [action for action, _ in self.records]


class RecorderAssemblyTest(ContactReachabilityPostgresTestCase):
    """``build_contact_reachability_recorder`` 接线到真实存取口（日志兜底分支）。

    未配置管理群时走 ``_LogOnlyContactNotifier``（只记日志，零网络调用）；本用例
    因此可以对失败结局做真实端到端验证，而不违反"失败结局只用注入式替身、不对
    真实人员发送"的取证边界——这里连"发送"这个动作本身都没有发生，只有落库。
    """

    def _recorder(self, audit):
        from lingxi.apps.scheduler.config import SchedulerConfig
        from lingxi.apps.scheduler.contact_reachability_assembly import (
            build_contact_reachability_recorder,
        )

        config = SchedulerConfig.from_env(
            {
                "LINGXI_POSTGRES_DSN": self._dsn,
                "LINGXI_DELEGATED_CREDENTIAL_KEY": "x" * 44,
                "LINGXI_DELEGATED_CREDENTIAL_PATH": "/tmp/lingxi-credential-test",
                "LINGXI_FEISHU_APP_ID": "cli_test",
                "LINGXI_FEISHU_APP_SECRET": "secret",
            }
        )
        return build_contact_reachability_recorder(config, self._dsn, audit=audit)

    def test_a_successful_outcome_is_written_and_audited(self) -> None:
        audit = RecordingAudit()
        record = self._recorder(audit)

        record(self.open_id, True, None)

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_REACHABLE)
        self.assertIn("outreach.contact_marked_reachable", audit.actions())

    def test_a_failed_outcome_is_written_and_generates_a_todo(self) -> None:
        audit = RecordingAudit()
        record = self._recorder(audit)

        record(self.open_id, False, "feishu_code_230013")

        self.assertEqual(self.store.contact_reachability(open_id=self.open_id), CONTACT_UNAVAILABLE)
        self.assertIn("outreach.contact_marked_unavailable", audit.actions())
        # 未配置管理群 chat_id：走日志兜底出口（零网络调用），但待办仍然被"发送"
        # 并留痕——不是配了群才生效的可选步骤。
        self.assertIn("outreach.contact_todo_notified", audit.actions())


if __name__ == "__main__":
    unittest.main()
