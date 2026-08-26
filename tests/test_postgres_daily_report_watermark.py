"""`adapters/postgres_daily_report_watermark.py` 的真库断言（Issue #325）。

只验证真库才能证伪的部分：主键真的是 ``(report_date, chat_id)`` 复合唯一约束
（数据库层面拒绝重复行，不是只靠 Python 侧的 ``ON CONFLICT`` 语句"看起来"幂等）、
不同 ``chat_id``/不同 ``report_date`` 互不干扰。表结构由
``migrations/alembic/versions/0071_daily_report_watermark.py`` 建立，测试库走
``ensure_production_schema`` 的整条 alembic 链，与生产同源。
"""

from __future__ import annotations

import os
import unittest
from datetime import date

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_daily_report_watermark import PostgresDailyReportWatermark

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，内测每日通报送达水位的真库断言未验证"
)

CHAT_A = "oc_fake_admin_group_a"
CHAT_B = "oc_fake_admin_group_b"
DAY_1 = date(2026, 8, 24)
DAY_2 = date(2026, 8, 25)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class DailyReportWatermarkPostgresTestCase(unittest.TestCase):
    """本文件全部真库用例的共同底座。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)
        self.watermark = PostgresDailyReportWatermark(self._dsn)

    def _row_count(self) -> int:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM daily_report_watermark")
            (count,) = cursor.fetchone()
        return int(count)


class ExistenceTests(DailyReportWatermarkPostgresTestCase):
    def test_a_never_marked_window_is_not_already_sent(self) -> None:
        self.assertFalse(self.watermark.already_sent(report_date=DAY_1, chat_id=CHAT_A))

    def test_marking_makes_it_already_sent(self) -> None:
        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.assertTrue(self.watermark.already_sent(report_date=DAY_1, chat_id=CHAT_A))

    def test_marking_persists_across_a_fresh_store_instance(self) -> None:
        """新构造一个 `PostgresDailyReportWatermark` 实例（不共享任何 Python 对象、
        只共享 DSN）依然能读到之前那份标记——这正是"跨进程重启"在真库层面的
        还原：新进程与旧进程之间除了数据库以外没有任何共享状态。"""

        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        restarted = PostgresDailyReportWatermark(self._dsn)
        self.assertTrue(restarted.already_sent(report_date=DAY_1, chat_id=CHAT_A))

    def test_mark_sent_is_idempotent_and_leaves_exactly_one_row(self) -> None:
        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.assertEqual(self._row_count(), 1)

    def test_a_different_chat_id_on_the_same_day_is_independent(self) -> None:
        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.assertFalse(self.watermark.already_sent(report_date=DAY_1, chat_id=CHAT_B))

    def test_a_different_day_for_the_same_chat_id_is_independent(self) -> None:
        self.watermark.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.assertFalse(self.watermark.already_sent(report_date=DAY_2, chat_id=CHAT_A))

    def test_four_marks_across_four_independent_store_instances_still_leave_one_row(self) -> None:
        """对应实测形状（2026-08-25 单日同窗口因多次部署重启收到四条通报）：
        四个各自独立构造、只共享 DSN 的实例依次判重再标记，数据库约束保证结果
        仍然只有一行——不依赖任何单一 Python 进程的内存状态。"""

        for _ in range(4):
            store = PostgresDailyReportWatermark(self._dsn)
            if not store.already_sent(report_date=DAY_1, chat_id=CHAT_A):
                store.mark_sent(report_date=DAY_1, chat_id=CHAT_A)
        self.assertEqual(self._row_count(), 1)


class DatabaseConstraintTests(DailyReportWatermarkPostgresTestCase):
    """否定断言：判重不是靠"应用记得只调一次"这种自觉，是数据库主键约束本身。"""

    def test_the_database_itself_rejects_a_duplicate_row_without_on_conflict(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO daily_report_watermark (report_date, chat_id) VALUES (%s, %s)",
                (DAY_1, CHAT_A),
            )
        import psycopg

        with self.assertRaises(psycopg.errors.UniqueViolation):
            with connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO daily_report_watermark (report_date, chat_id) VALUES (%s, %s)",
                    (DAY_1, CHAT_A),
                )
        # 上一条语句所在的事务已经因为异常回滚，行数必须仍然恰好一行——约束
        # 拒绝的是重复写入本身，不是"写入两行后又神奇地少了一行"。
        self.assertEqual(self._row_count(), 1)


class ValidationTests(DailyReportWatermarkPostgresTestCase):
    """构造参数校验——在触达数据库之前就快速失败。"""

    def test_already_sent_rejects_a_blank_chat_id(self) -> None:
        with self.assertRaises(ValueError):
            self.watermark.already_sent(report_date=DAY_1, chat_id="")

    def test_mark_sent_rejects_a_blank_chat_id(self) -> None:
        with self.assertRaises(ValueError):
            self.watermark.mark_sent(report_date=DAY_1, chat_id="   ")

    def test_already_sent_rejects_a_non_date_report_date(self) -> None:
        with self.assertRaises(TypeError):
            self.watermark.already_sent(report_date="2026-08-24", chat_id=CHAT_A)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
