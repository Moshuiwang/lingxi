"""``onboarding_failure`` 表（迁移 ``0077``）的真库断言（Issue #337）。

只有真库能证伪：``ON CONFLICT (trace_id) DO NOTHING`` 的幂等性、``CHECK`` 约束
（取值范围、非空）是否真的在数据库层面挡住违规写入——这两类断言不能用假实现
证明，见验证与门禁第五节「数据库约束类断言必须在真库上验证」。

表结构由 alembic 全链建立（``ensure_production_schema``），测试库与生产同源。
"""

from __future__ import annotations

import os
import unittest

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_onboarding_failure import (
    PostgresFailureReasonRecorder,
    fetch_failure_reason,
)
from lingxi.core.ids import new_ulid

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，onboarding_failure 表的真库断言未验证"
    "（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，onboarding_failure 表的真库断言未验证"
)


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class OnboardingFailurePostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()


class RecordAndFetchTests(OnboardingFailurePostgresTestCase):
    def test_a_recorded_failure_can_be_fetched_back(self) -> None:
        trace_id = new_ulid()
        recorder = PostgresFailureReasonRecorder(self._dsn)

        recorder.record_failure(
            trace_id=trace_id,
            failure_reason="directory_unavailable",
            event_type="onboarding.result",
        )
        row = fetch_failure_reason(self._dsn, trace_id=trace_id)

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.failure_reason, "directory_unavailable")
        self.assertEqual(row.event_type, "onboarding.result")
        self.assertTrue(row.occurred_at)

    def test_an_unknown_trace_id_fetches_none(self) -> None:
        self.assertIsNone(fetch_failure_reason(self._dsn, trace_id=new_ulid()))

    def test_stalled_provisioning_event_type_is_accepted(self) -> None:
        """迁移 0077 的 CHECK 允许两个已知取值——本用例覆盖另一个（``onboarding.
        result`` 已由 `test_a_recorded_failure_can_be_fetched_back` 覆盖）。"""

        trace_id = new_ulid()
        recorder = PostgresFailureReasonRecorder(self._dsn)

        recorder.record_failure(
            trace_id=trace_id,
            failure_reason="stalled_lease_expired",
            event_type="stalled_provisioning.aborted",
        )
        row = fetch_failure_reason(self._dsn, trace_id=trace_id)

        assert row is not None
        self.assertEqual(row.event_type, "stalled_provisioning.aborted")


class IdempotencyTests(OnboardingFailurePostgresTestCase):
    """``trace_id`` 主键 + ``ON CONFLICT DO NOTHING``（迁移 0077 文件头部
    「幂等」一节）：同一条链正常只产生一次终态，但进程重启等原因可能让同一个
    ``trace_id`` 被处理第二次——先落的那一行必须保持不变，不覆盖、不报错。"""

    def test_a_second_write_for_the_same_trace_id_does_not_overwrite(self) -> None:
        trace_id = new_ulid()
        recorder = PostgresFailureReasonRecorder(self._dsn)

        recorder.record_failure(
            trace_id=trace_id, failure_reason="directory_unavailable", event_type="onboarding.result"
        )
        # 第二次写入不同的 failure_reason：模拟同一条链因为进程重启被重新处理，
        # 这次判出了不同的终态——先落的那一行必须原样保留。
        recorder.record_failure(
            trace_id=trace_id, failure_reason="mcp_sync_timeout", event_type="onboarding.result"
        )

        row = fetch_failure_reason(self._dsn, trace_id=trace_id)
        assert row is not None
        self.assertEqual(row.failure_reason, "directory_unavailable", "先落的那一行不应被覆盖")
        self.assertEqual(
            self.query("SELECT count(*) FROM onboarding_failure WHERE trace_id = %s", (trace_id,))[
                0
            ][0],
            1,
            "同一个 trace_id 不应出现第二行",
        )

    def test_a_second_write_does_not_raise(self) -> None:
        """否定断言：第二次写入不得抛异常——``ON CONFLICT DO NOTHING`` 必须真的
        生效，不是"恰好没撞上唯一约束"。"""

        trace_id = new_ulid()
        recorder = PostgresFailureReasonRecorder(self._dsn)

        recorder.record_failure(
            trace_id=trace_id, failure_reason="a", event_type="onboarding.result"
        )
        try:
            recorder.record_failure(
                trace_id=trace_id, failure_reason="b", event_type="onboarding.result"
            )
        except Exception as error:  # noqa: BLE001 - 本用例本身就是在证明这里不该抛
            self.fail(f"第二次写入不应该抛异常：{type(error).__name__}: {error}")


class CheckConstraintTests(OnboardingFailurePostgresTestCase):
    """迁移 0077 的 ``CHECK`` 约束是数据库层面的第二道防线（`PostgresFailure
    ReasonRecorder` 在应用层已经校验同样的规则，这里主动绕过应用层校验、直接
    对表写入，证明数据库本身也会拒绝——否则一次未来的代码改动如果不小心跳过了
    应用层校验，违规数据仍然进不了库）。"""

    def _insert_raw(self, *, trace_id: str, failure_reason: str, event_type: str) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO onboarding_failure (trace_id, failure_reason, event_type) "
                "VALUES (%s, %s, %s)",
                (trace_id, failure_reason, event_type),
            )

    def test_unknown_event_type_is_rejected(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert_raw(
                trace_id=new_ulid(), failure_reason="x", event_type="some.other.event"
            )

    def test_blank_failure_reason_is_rejected(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert_raw(
                trace_id=new_ulid(), failure_reason="   ", event_type="onboarding.result"
            )

    def test_blank_trace_id_is_rejected(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self._insert_raw(trace_id="  ", failure_reason="x", event_type="onboarding.result")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
