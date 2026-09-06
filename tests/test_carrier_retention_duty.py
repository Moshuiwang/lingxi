"""四个内容载体的九十天到期清理（Trace #643 IN-04，工作卡 #648）。

产品合同「数据保留与删除」把"任务问题"与"待确认操作参数"点名写进了九十天上限，
并要求"每份可识别内容自写入起不得晚于九十天删除或不可逆脱敏"、"恢复后的当前运行
环境必须**按内容原始写入时间**重新执行适用清理"。这四个载体此前一个消费者都没有：

* ``task.content_expires_at``（承载 ``task.prompt``，用户问题原文）——``0057`` 的触发器
  写入，全仓无任何到期消费者；
* ``inbound_event.expires_at``（含 ``user_open_id``）——同上；
* ``queue_failure_notice.expires_at``——``0058`` 写入，无人读取，且此前**不在**数据库
  设计第九节的载体表里；
* ``pending_action``——连到期列都没有，欠账登记在 ``0068`` 文件尾，本批由 ``0089`` 补上。

断言分层照 ``tests/test_permission_retention_duty.py`` 与
``tests/test_content_capture_retention_duty.py``：

1. **职责本身**（纯逻辑、假适配器）：每轮四面各调一次、停止中一条都不处置、失败关闭
   并留一条只含表名与异常类型的审计；
2. **源码守卫**：四个到期处置方法都必须有生产调用方——"方法在、没人调"正是这个缺陷
   的正身，按 AST 找取用点而不是按字符串找（docstring 里的方法名也能让 grep 变绿）；
3. **装配**：``build_loop`` 真的把它接进了 ``lingxi-scheduler``，摘掉注册必须变红；
4. **真库**（有 ``LINGXI_POSTGRES_DSN`` 时）五组对照：过期/未过期、边界时间、重复执行、
   清理失败后恢复、旧备份恢复后按原始写入时间。

数据全部为虚构化名，不含任何真实人员数据。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from postgres_schema import psycopg_available

from lingxi.apps.scheduler import (
    CarrierRetentionReport,
    ExpiredCarrierRetentionDuty,
    SchedulerConfig,
    build_loop,
)

OWNER_ID = "usr_carrier_retention"
CONVERSATION_ID = "cnv_carrier_retention"

BASE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}

#: 九十天上限。四张表的到期列都由触发器固定成"来源时间 + 2160 小时"，因此"造一条
#: 已经到期的行"的唯一正确姿势是把来源时间推到九十天以前，而不是去写到期列。
RETENTION_WINDOW = timedelta(hours=2160)

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，四个载体的到期清理断言未验证（需真实 PostgreSQL 16）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，四个载体的到期清理断言未验证"
)


def _dsn_for_database(dsn: str, database: str) -> str:
    """把一个连接串换成"同一台服务器、另一个库"的连接串（URL 形式）。

    ``alembic`` 的 ``env.py`` 只接受 URL 形式的连接串，因此这里从 ``psycopg`` 解析出的
    连接参数重新拼一个 URL，而不是直接改字符串——连接串既可能是 URL 也可能是
    ``key=value`` 形式，字符串替换在后者上会静默拼出一个连不上的值。口令若存在只在
    进程内存里参与拼接，不打印、不落盘。
    """

    from urllib.parse import quote

    from psycopg.conninfo import conninfo_to_dict

    parameters = conninfo_to_dict(dsn)
    user = str(parameters.get("user", "") or "")
    password = str(parameters.get("password", "") or "")
    host = str(parameters.get("host", "localhost") or "localhost")
    port = str(parameters.get("port", "") or "")
    credentials = quote(user, safe="")
    if password:
        credentials = f"{credentials}:{quote(password, safe='')}"
    authority = f"{credentials}@{host}" if credentials else host
    if port:
        authority = f"{authority}:{port}"
    return f"postgresql://{authority}/{quote(database, safe='')}"


class FakeCarriers:
    """四面到期处置的假实现：记下调用顺序，可以让指定的一面抛异常。"""

    def __init__(self, *, counts: dict[str, int] | None = None, fails: str = "") -> None:
        self._counts = counts or {}
        self._fails = fails
        self.calls: list[str] = []

    def _answer(self, name: str) -> int:
        self.calls.append(name)
        if self._fails == name:
            raise RuntimeError(f"{name} 处置失败：这条异常正文本身就不该进审计")
        return self._counts.get(name, 0)

    def redact_expired_task_prompts(self) -> int:
        return self._answer("redact_expired_task_prompts")

    def purge_expired_inbound_events(self) -> int:
        return self._answer("purge_expired_inbound_events")

    def redact_expired_pending_actions(self) -> int:
        return self._answer("redact_expired_pending_actions")

    def purge_expired_queue_failure_notices(self) -> int:
        return self._answer("purge_expired_queue_failure_notices")


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))


class DutyBehaviourTest(unittest.TestCase):
    """职责本身：每轮四面各一次、停止即不处置、失败关闭且审计不带内容。"""

    def test_one_call_per_face_per_round_and_the_counts_are_reported(self) -> None:
        audit = RecordingAudit()
        carriers = FakeCarriers(
            counts={
                "redact_expired_task_prompts": 3,
                "purge_expired_inbound_events": 5,
                "redact_expired_pending_actions": 2,
                "purge_expired_queue_failure_notices": 7,
            }
        )
        duty = ExpiredCarrierRetentionDuty(carriers=carriers, audit=audit)

        report = duty.run_once()

        assert report is not None
        self.assertEqual(report.task_prompts_redacted, 3)
        self.assertEqual(report.inbound_events_purged, 5)
        self.assertEqual(report.pending_actions_redacted, 2)
        self.assertEqual(report.queue_failure_notices_purged, 7)
        self.assertEqual(report.total, 17)
        self.assertEqual(
            carriers.calls,
            [
                "redact_expired_task_prompts",
                "purge_expired_inbound_events",
                "redact_expired_pending_actions",
                "purge_expired_queue_failure_notices",
            ],
            "每轮四面各调一次，不循环到处置完",
        )
        self.assertEqual(
            audit.entries,
            [("carrier_retention.completed", report.audit_facts())],
        )

    def test_nothing_is_processed_once_stopping(self) -> None:
        carriers = FakeCarriers()
        duty = ExpiredCarrierRetentionDuty(carriers=carriers, audit=RecordingAudit())
        duty.request_stop()

        self.assertIsNone(duty.run_once())
        self.assertEqual(carriers.calls, [])
        self.assertTrue(duty.stopping)

    def test_a_shared_stop_event_stops_this_duty_too(self) -> None:
        stop = threading.Event()
        carriers = FakeCarriers()
        duty = ExpiredCarrierRetentionDuty(carriers=carriers, audit=RecordingAudit(), stop=stop)

        stop.set()

        self.assertIsNone(duty.run_once())
        self.assertEqual(carriers.calls, [])

    def test_failure_is_closed_and_the_audit_carries_only_the_exception_type(self) -> None:
        """失败不被吞掉：原样上抛，审计只留表名与异常类型，不留异常正文。"""

        audit = RecordingAudit()
        carriers = FakeCarriers(fails="redact_expired_pending_actions")
        duty = ExpiredCarrierRetentionDuty(carriers=carriers, audit=audit)

        with self.assertLogs("lingxi.apps.scheduler.retention", level="ERROR") as logs:
            with self.assertRaises(RuntimeError):
                duty.run_once()

        self.assertEqual(
            [action for action, _ in audit.entries], ["carrier_retention.sweep_failed"]
        )
        fields = audit.entries[0][1]
        self.assertEqual(fields, {"table": "pending_action", "error": "RuntimeError"})
        self.assertNotIn("不该进审计", repr(audit.entries))
        self.assertNotIn("不该进审计", "\n".join(logs.output))
        self.assertNotIn(
            "purge_expired_queue_failure_notices",
            carriers.calls,
            "前一面失败时本轮后面几面不再执行——下一轮从各自的水位继续",
        )

    def test_a_round_that_found_nothing_still_records_the_completed_audit(self) -> None:
        """一轮零处置也要留下"这一轮真的跑过"的事实，否则"没到期"与"没跑"不可分。"""

        audit = RecordingAudit()
        duty = ExpiredCarrierRetentionDuty(carriers=FakeCarriers(), audit=audit)

        report = duty.run_once()

        self.assertEqual(report, CarrierRetentionReport())
        self.assertEqual(audit.entries[0][0], "carrier_retention.completed")
        self.assertEqual(
            audit.entries[0][1],
            {
                "task_prompts_redacted": 0,
                "inbound_events_purged": 0,
                "pending_actions_redacted": 0,
                "queue_failure_notices_purged": 0,
            },
        )


def _reads_attribute(path: Path, attribute: str) -> bool:
    """源码里有没有真的取用 ``x.<attribute>``（含调用）。"""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Attribute) and node.attr == attribute for node in ast.walk(tree)
    )


class ProductionCallerSourceTest(unittest.TestCase):
    """**这个缺陷的正身**：到期处置方法在、没人调。

    与 ``tests/test_permission_retention_duty.py::ProductionCallerSourceTest`` 同一条
    守卫，扩到本批四个载体：**按 AST 找调用点，不按字符串找**——一句 docstring 里的
    方法名也能让 ``grep`` 变绿，而缺陷发生前那些 docstring 本来就都在。
    """

    DEFINING_MODULE = "postgres_carrier_retention.py"
    EXPIRY_METHODS = (
        "redact_expired_task_prompts",
        "purge_expired_inbound_events",
        "redact_expired_pending_actions",
        "purge_expired_queue_failure_notices",
    )

    def test_every_expiry_method_has_a_production_caller(self) -> None:
        source_root = Path(inspect.getsourcefile(build_loop)).parents[2]
        self.assertEqual(source_root.name, "lingxi")
        for method in self.EXPIRY_METHODS:
            with self.subTest(method=method):
                callers = sorted(
                    path.relative_to(source_root).as_posix()
                    for path in source_root.rglob("*.py")
                    if path.name != self.DEFINING_MODULE and _reads_attribute(path, method)
                )
                self.assertTrue(
                    callers,
                    f"{method} 在 src/ 全树没有生产调用方——九十天上限又只存在于一个没人读的列里",
                )


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class AssemblyTest(unittest.TestCase):
    """装配层断言：``build_loop`` 是 ``main()`` 唯一的装配入口，因此这一组就是"四个载体
    的到期清理真的每轮都会跑"的证据。把职责从清单里摘掉，它立刻变红。"""

    def _config(self, **overrides: str) -> SchedulerConfig:
        from cryptography.fernet import Fernet

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = {
            **BASE_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(directory.name) / "delegated.enc"),
            **{f"LINGXI_{key.upper()}": value for key, value in overrides.items()},
        }
        return SchedulerConfig.from_env(environment)

    def test_the_duty_is_assembled_into_the_scheduler_process(self) -> None:
        loop = build_loop(self._config())
        matching = [duty for duty in loop.duties if isinstance(duty, ExpiredCarrierRetentionDuty)]

        self.assertEqual(len(matching), 1, "载体到期清理必须恰好注册一条")
        self.assertIn("载体到期清理", [duty.name for duty in loop.duties])
        loop.request_stop()
        self.assertTrue(matching[0].stopping, "一个停止标志贯穿全部职责")

    def test_it_is_wired_unconditionally_and_holds_the_real_adapter(self) -> None:
        """注册的不能是一个空壳：处置口必须真的是那个适配器，且没有任何可选前置。"""

        from lingxi.adapters.postgres_carrier_retention import PostgresCarrierRetention

        loop = build_loop(self._config())
        (duty,) = [item for item in loop.duties if isinstance(item, ExpiredCarrierRetentionDuty)]

        self.assertIsInstance(duty._carriers, PostgresCarrierRetention)

    def test_it_does_not_widen_the_permission_retention_delete_surface(self) -> None:
        """新载体走**自己**的职责，不塞进权限链那条。

        ``tests/test_permission_retention_duty.py::RetentionSweepPostgresTest::
        test_the_delete_surface_is_not_widened`` 钉死了权限链那条职责的删除面，那是
        正确纪律；这条断言从另一面钉住同一件事——两条职责各自独立注册。
        """

        from lingxi.apps.scheduler import PermissionRetentionSweepDuty

        loop = build_loop(self._config())
        carrier = [d for d in loop.duties if isinstance(d, ExpiredCarrierRetentionDuty)]
        permission = [d for d in loop.duties if isinstance(d, PermissionRetentionSweepDuty)]

        self.assertEqual(len(carrier), 1)
        self.assertEqual(len(permission), 1)
        self.assertIsNot(carrier[0], permission[0])


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class RealDatabaseTest(unittest.TestCase):
    """真库五组对照：过期/未过期、边界、重复执行、失败后恢复、旧备份按原始写入时间。"""

    @classmethod
    def setUpClass(cls) -> None:
        from postgres_schema import ensure_production_schema

        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from postgres_schema import reset_production_rows

        from lingxi.adapters.postgres import connect

        reset_production_rows(self._dsn)
        self._connect = connect
        self._seed_owner()

    # ---- 夹具 -------------------------------------------------------------

    def _execute(self, sql: str, parameters: tuple = (), *, dsn: str | None = None) -> None:
        with self._connect(dsn or self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            connection.commit()

    def _fetch(self, sql: str, parameters: tuple = (), *, dsn: str | None = None) -> list[tuple]:
        with self._connect(dsn or self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return list(cursor.fetchall())

    def _seed_owner(self) -> None:
        """一行合法的 ``app_user`` + 会话。六个身份字段全有或全无（迁移 ``008`` 的 CHECK）。"""

        self._execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id,
                  display_name, department, tenant_key, provisioning_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')
               ON CONFLICT (id) DO NOTHING""",
            (
                OWNER_ID,
                "ou_carrier_retention",
                "u_carrier_retention",
                "un_carrier_retention",
                "化名甲",
                "数据部",
                "tk_carrier_retention",
            ),
        )
        self._execute(
            """INSERT INTO conversation (id, user_id, feishu_chat_id, feishu_thread_id)
               VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (CONVERSATION_ID, OWNER_ID, "chat_carrier_retention", "topic_carrier_retention"),
        )

    def _seed_task(
        self, task_id: str, *, created_at: datetime, prompt: str = "用户问题原文"
    ) -> None:
        # content_expires_at 由 0057 的触发器按 created_at + 2160 小时固定（写什么都会被
        # 覆盖），因此"造一条已到期的任务"的唯一姿势是把 created_at 推到九十天以前。
        self._execute(
            """INSERT INTO task
                 (id, conversation_id, user_id, inbound_event_id, prompt, status,
                  target_worker_version, attempts, created_at, content_expires_at)
               VALUES (%s, %s, %s, %s, %s, 'succeeded', 'stable', 1, %s, %s)""",
            (task_id, CONVERSATION_ID, OWNER_ID, f"evt_{task_id}", prompt, created_at, created_at),
        )

    def _seed_inbound_event(self, event_id: str, *, received_at: datetime) -> None:
        self._execute(
            """INSERT INTO inbound_event
                 (feishu_event_id, received_at, event_type, user_open_id, trace_id, expires_at)
               VALUES (%s, %s, 'im.message.receive_v1', %s, %s, %s)""",
            (event_id, received_at, "ou_carrier_retention", f"trc_{event_id}", received_at),
        )

    def _seed_queue_failure_notice(self, event_id: str, *, created_at: datetime) -> None:
        self._execute(
            """INSERT INTO queue_failure_notice (feishu_event_id, created_at, expires_at)
               VALUES (%s, %s, %s)""",
            (event_id, created_at, created_at),
        )

    def _seed_pending_action(
        self,
        action_id: str,
        *,
        created_at: datetime,
        status: str = "executed",
        action_type: str = "local_permission_grant",
        payload: str | None = '{"company_id":"c1","metric_name":"营收"}',
        target_suffix: str = "",
    ) -> None:
        decided_at = None if status == "pending" else created_at
        decided_by = None if status == "pending" else "ou_admin_decider"
        self._execute(
            """INSERT INTO pending_action
                 (id, action_type, target_open_id, target_state_snapshot,
                  initiated_by_open_id, status, payload, created_at,
                  confirm_deadline_at, decided_at, decided_by_open_id,
                  retention_expires_at)
               VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                action_id,
                action_type,
                f"ou_target_carrier{target_suffix}",
                "ou_admin_initiator",
                status,
                payload,
                created_at,
                created_at + timedelta(minutes=10),
                decided_at,
                decided_by,
                created_at,
            ),
        )

    def _retention(self, dsn: str | None = None):
        from lingxi.adapters.postgres_carrier_retention import PostgresCarrierRetention

        return PostgresCarrierRetention(dsn or self._dsn)

    def _sweep_all(self, now: datetime, *, dsn: str | None = None) -> dict[str, int]:
        retention = self._retention(dsn)
        return {
            "task": retention.redact_expired_task_prompts(now=now),
            "inbound_event": retention.purge_expired_inbound_events(now=now),
            "pending_action": retention.redact_expired_pending_actions(now=now),
            "queue_failure_notice": retention.purge_expired_queue_failure_notices(now=now),
        }

    def _seed_all_four(self, now: datetime, *, age: timedelta, tag: str) -> None:
        moment = now - age
        self._seed_task(f"tsk_{tag}", created_at=moment)
        self._seed_inbound_event(f"evt_{tag}", received_at=moment)
        self._seed_queue_failure_notice(f"qfn_{tag}", created_at=moment)
        self._seed_pending_action(f"pac_{tag}", created_at=moment, target_suffix=f"_{tag}")

    # ---- 第一组：过期 / 未过期对照 -----------------------------------------

    def test_expired_content_is_removed_and_fresh_content_is_untouched(self) -> None:
        """四个载体同时造过期与未过期各一行：到期的处置掉，未到期的一个字段都不动。"""

        now = datetime.now(UTC)
        self._seed_all_four(now, age=timedelta(days=91), tag="old")
        self._seed_all_four(now, age=timedelta(days=1), tag="new")

        processed = self._sweep_all(now)

        self.assertEqual(
            processed,
            {"task": 1, "inbound_event": 1, "pending_action": 1, "queue_failure_notice": 1},
        )

        # 到期任务：问题原文没了，行与它承载的交付事实还在。
        self.assertEqual(
            self._fetch("SELECT prompt, status FROM task WHERE id = 'tsk_old'"),
            [("", "succeeded")],
        )
        # 未到期任务：原文逐字未动。
        self.assertEqual(
            self._fetch("SELECT prompt FROM task WHERE id = 'tsk_new'"), [("用户问题原文",)]
        )

        # 两张删除面：到期行消失，未到期行留着。
        self.assertEqual(
            [row[0] for row in self._fetch("SELECT feishu_event_id FROM inbound_event")],
            ["evt_new"],
        )
        self.assertEqual(
            [row[0] for row in self._fetch("SELECT feishu_event_id FROM queue_failure_notice")],
            ["qfn_new"],
        )

        # 到期待确认操作：三列 open_id 与 payload 都不再是原值，行还在（本地权限覆盖的
        # NOT NULL 外键依赖它），脱敏值逐行唯一。
        (old_row,) = self._fetch(
            """SELECT target_open_id, initiated_by_open_id, decided_by_open_id, payload,
                      content_redacted_at IS NOT NULL
                 FROM pending_action WHERE id = 'pac_old'"""
        )
        self.assertEqual(old_row[0], "redacted:pac_old")
        self.assertEqual(old_row[1], "redacted:pac_old")
        self.assertEqual(old_row[2], "redacted:pac_old")
        self.assertEqual(old_row[3], "{}")
        self.assertTrue(old_row[4], "脱敏水位必须写上，否则下一轮会把同一行再算一次")

        (new_row,) = self._fetch(
            """SELECT target_open_id, initiated_by_open_id, payload, content_redacted_at
                 FROM pending_action WHERE id = 'pac_new'"""
        )
        self.assertEqual(new_row[0], "ou_target_carrier_new")
        self.assertEqual(new_row[1], "ou_admin_initiator")
        self.assertEqual(new_row[2], '{"company_id":"c1","metric_name":"营收"}')
        self.assertIsNone(new_row[3])

    def test_several_expired_pending_rows_do_not_collide_on_the_partial_unique_index(self) -> None:
        """多条到期但仍停在 ``pending`` 的行同轮脱敏：脱敏值带行标识，不撞部分唯一索引。

        ``pending_action_single_pending_target_idx`` 是 ``target_open_id`` 上
        ``WHERE status = 'pending'`` 的唯一索引。脱敏成同一个常量会让整批失败——一次
        本该静默完成的合规动作变成每轮都失败的告警。
        """

        now = datetime.now(UTC)
        moment = now - timedelta(days=95)
        for index in range(3):
            self._seed_pending_action(
                f"pac_pending_{index}",
                created_at=moment,
                status="pending",
                action_type="suspend_user",
                payload=None,
                target_suffix=f"_pending_{index}",
            )

        redacted = self._retention().redact_expired_pending_actions(now=now)

        self.assertEqual(redacted, 3)
        values = sorted(
            row[0]
            for row in self._fetch(
                "SELECT target_open_id FROM pending_action WHERE status = 'pending'"
            )
        )
        self.assertEqual(
            values, ["redacted:pac_pending_0", "redacted:pac_pending_1", "redacted:pac_pending_2"]
        )
        # suspend_user 的 payload 必须保持 NULL（0073 的双向等价 CHECK）。
        self.assertEqual(
            self._fetch("SELECT DISTINCT payload FROM pending_action WHERE status = 'pending'"),
            [(None,)],
        )

    # ---- 第二组：边界时间 --------------------------------------------------

    def test_the_expiry_boundary_is_inclusive_and_one_microsecond_earlier_is_not(self) -> None:
        """恰好到期的行处置掉，早一微秒的判定不处置——判据是 ``<= now``，不是 ``< now``。

        边界写在到期列上而不是"造数时间"上：四张表的到期列都是"来源时间 + 2160 小时"，
        因此把判定时刻取成 ``created_at + 2160 小时`` 就是那一瞬。
        """

        source = datetime.now(UTC) - RETENTION_WINDOW
        boundary = source + RETENTION_WINDOW
        self._seed_all_four(boundary, age=RETENTION_WINDOW, tag="edge")

        # 早一微秒：还没到期，四面都不动。
        before = self._sweep_all(boundary - timedelta(microseconds=1))
        self.assertEqual(
            before,
            {"task": 0, "inbound_event": 0, "pending_action": 0, "queue_failure_notice": 0},
            "早一微秒就不该处置：到期判据是 <= now",
        )
        self.assertEqual(
            self._fetch("SELECT prompt FROM task WHERE id = 'tsk_edge'"), [("用户问题原文",)]
        )

        # 恰好那一瞬：四面都处置。
        at_boundary = self._sweep_all(boundary)
        self.assertEqual(
            at_boundary,
            {"task": 1, "inbound_event": 1, "pending_action": 1, "queue_failure_notice": 1},
        )

    def test_a_naive_moment_and_a_bad_limit_are_refused_before_any_write(self) -> None:
        """朴素时刻与非法批量上限在任何写入之前被拒——四面逐一验。"""

        now = datetime.now(UTC)
        self._seed_all_four(now, age=timedelta(days=91), tag="guard")
        retention = self._retention()
        methods = (
            retention.redact_expired_task_prompts,
            retention.purge_expired_inbound_events,
            retention.redact_expired_pending_actions,
            retention.purge_expired_queue_failure_notices,
        )
        for method in methods:
            with self.subTest(method=method.__name__):
                with self.assertRaises(ValueError):
                    method(now=datetime(2026, 9, 6, 12, 0))
                for bad in (0, -1, True):
                    with self.assertRaises(ValueError):
                        method(limit=bad)  # type: ignore[arg-type]

        # 一条都没被处置：拒绝发生在任何写入之前。
        self.assertEqual(
            self._fetch("SELECT prompt FROM task WHERE id = 'tsk_guard'"), [("用户问题原文",)]
        )
        self.assertEqual(len(self._fetch("SELECT 1 FROM inbound_event")), 1)
        self.assertEqual(len(self._fetch("SELECT 1 FROM queue_failure_notice")), 1)
        self.assertEqual(
            self._fetch("SELECT content_redacted_at FROM pending_action WHERE id = 'pac_guard'"),
            [(None,)],
        )

    # ---- 第三组：重复执行 --------------------------------------------------

    def test_running_the_sweep_again_changes_nothing_more(self) -> None:
        """幂等：第二轮四面都返回 0，且已处置的行不再被改写一次。"""

        now = datetime.now(UTC)
        self._seed_all_four(now, age=timedelta(days=100), tag="idem")

        first = self._sweep_all(now)
        self.assertEqual(
            first,
            {"task": 1, "inbound_event": 1, "pending_action": 1, "queue_failure_notice": 1},
        )
        (watermark,) = self._fetch(
            "SELECT content_redacted_at FROM pending_action WHERE id = 'pac_idem'"
        )

        second = self._sweep_all(now + timedelta(days=1))
        self.assertEqual(
            second,
            {"task": 0, "inbound_event": 0, "pending_action": 0, "queue_failure_notice": 0},
            "重复执行不该把同一行再算一次",
        )
        self.assertEqual(
            self._fetch("SELECT content_redacted_at FROM pending_action WHERE id = 'pac_idem'"),
            [watermark],
            "脱敏水位不随重复执行往后挪",
        )
        self.assertEqual(self._fetch("SELECT prompt FROM task WHERE id = 'tsk_idem'"), [("",)])

    def test_the_batch_limit_leaves_the_rest_for_the_next_round(self) -> None:
        """小批量、每轮一次：一轮只处置 ``limit`` 行，剩下的下一轮收走，一行都不漏。"""

        now = datetime.now(UTC)
        moment = now - timedelta(days=120)
        for index in range(3):
            self._seed_task(f"tsk_batch_{index}", created_at=moment)
            self._seed_inbound_event(f"evt_batch_{index}", received_at=moment)

        retention = self._retention()
        self.assertEqual(retention.redact_expired_task_prompts(now=now, limit=2), 2)
        self.assertEqual(retention.redact_expired_task_prompts(now=now, limit=2), 1)
        self.assertEqual(retention.redact_expired_task_prompts(now=now, limit=2), 0)
        self.assertEqual(
            self._fetch("SELECT DISTINCT prompt FROM task WHERE id LIKE %s", ("tsk_batch_%",)),
            [("",)],
        )

        self.assertEqual(retention.purge_expired_inbound_events(now=now, limit=2), 2)
        self.assertEqual(retention.purge_expired_inbound_events(now=now, limit=2), 1)
        self.assertEqual(retention.purge_expired_inbound_events(now=now, limit=2), 0)
        self.assertEqual(self._fetch("SELECT 1 FROM inbound_event"), [])

    # ---- 第四组：清理失败后恢复 --------------------------------------------

    def test_a_failed_round_recovers_on_the_next_round_without_moving_any_expiry(self) -> None:
        """一轮被锁挡住而失败：这一轮什么都没改、到期时间没被往后挪，下一轮照常收走。

        真实的失败注入——另一条独占连接对 ``task`` 上 ``ACCESS EXCLUSIVE`` 锁，本轮的
        ``UPDATE`` 会在适配器自己的 ``lock_timeout``（2 秒）内响亮失败，而不是无限等待。
        对应 ``V-保留-16``：清理失败只能重试，不会把任何行的到期时间往后挪。
        """

        import psycopg

        now = datetime.now(UTC)
        self._seed_all_four(now, age=timedelta(days=91), tag="fail")
        (expiry_before,) = self._fetch("SELECT content_expires_at FROM task WHERE id = 'tsk_fail'")

        blocker = psycopg.connect(self._dsn, autocommit=False)
        self.addCleanup(blocker.close)
        try:
            with blocker.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("LOCK TABLE task IN ACCESS EXCLUSIVE MODE")
            with self.assertRaises(psycopg.errors.Error):
                self._retention().redact_expired_task_prompts(now=now)
        finally:
            blocker.rollback()

        # 失败那一轮什么都没改：原文还在，到期时间分毫未动。
        self.assertEqual(
            self._fetch("SELECT prompt, content_expires_at FROM task WHERE id = 'tsk_fail'"),
            [("用户问题原文", expiry_before[0])],
        )

        # 下一轮照常收走——失败只能重试，不会把内容留过九十天。
        self.assertEqual(self._retention().redact_expired_task_prompts(now=now), 1)
        self.assertEqual(self._fetch("SELECT prompt FROM task WHERE id = 'tsk_fail'"), [("",)])

    def test_one_failing_face_does_not_stop_the_others_from_the_next_round(self) -> None:
        """一面失败不让其余三面的内容永远留下：下一轮四面都收干净。"""

        import psycopg

        now = datetime.now(UTC)
        self._seed_all_four(now, age=timedelta(days=91), tag="partial")

        blocker = psycopg.connect(self._dsn, autocommit=False)
        self.addCleanup(blocker.close)
        try:
            with blocker.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("LOCK TABLE task IN ACCESS EXCLUSIVE MODE")
            duty = ExpiredCarrierRetentionDuty(carriers=self._retention(), audit=RecordingAudit())
            with self.assertRaises(psycopg.errors.Error):
                duty.run_once()
        finally:
            blocker.rollback()

        processed = self._sweep_all(now)
        self.assertEqual(
            processed,
            {"task": 1, "inbound_event": 1, "pending_action": 1, "queue_failure_notice": 1},
        )

    # ---- 第五组：旧备份恢复后按原始写入时间 --------------------------------

    def test_a_restored_backup_is_cleaned_by_original_write_time(self) -> None:
        """把行搬进一个**新建的**运行环境后，清理仍按内容**原始写入时间**判定。

        合同原文："恢复后的当前运行环境必须按内容原始写入时间重新执行适用清理……不能
        把恢复时间当成新的保留起点。" 这里造的正是那个场景：一份九十天前写入、恢复
        当天才落进新库的数据，落库瞬间就已经过期，第一轮清理必须全部收走。

        恢复用 ``COPY … TO STDOUT`` / ``COPY … FROM STDIN`` 把四个载体整表搬过去，落进
        一个由**同一条 alembic 链**新建的库——与 ``pg_restore``（不带
        ``--disable-triggers``）走的是同一条路径：行级触发器照常触发，因此这条用例同时
        证明了"到期列在恢复后由 ``created_at`` 重算，仍然等于原始写入时间 + 2160 小时"，
        而不是被恢复时间顶掉。不调用外部 ``pg_dump``/``pg_restore`` 可执行文件，是为了
        不让这条断言依赖测试机上是否装了对应版本的客户端工具。
        """

        import psycopg
        from postgres_schema import alembic_upgrade_head
        from psycopg import sql

        carriers = (
            "app_user",
            "conversation",
            "task",
            "inbound_event",
            "queue_failure_notice",
            "pending_action",
        )

        # 原库：九十天前写入的内容，此刻已经到期但还没被清理过（例如清理职责在那台
        # 机器上从没跑起来）。
        now = datetime.now(UTC)
        self._seed_all_four(now, age=timedelta(days=91), tag="restore")

        restored_name = "lingxi_carrier_restore_probe"
        maintenance = _dsn_for_database(self._dsn, "postgres")
        restored_dsn = _dsn_for_database(self._dsn, restored_name)

        def drop_restored() -> None:
            with psycopg.connect(maintenance, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(restored_name)
                    )
                )

        drop_restored()
        self.addCleanup(drop_restored)
        with psycopg.connect(maintenance, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored_name)))
        alembic_upgrade_head(restored_dsn)

        # 搬数据：逐表 COPY，父表在前。
        with (
            psycopg.connect(self._dsn) as source,
            psycopg.connect(restored_dsn) as target,
        ):
            for table in carriers:
                statement = sql.SQL("COPY {} TO STDOUT (FORMAT binary)").format(
                    sql.Identifier(table)
                )
                restore = sql.SQL("COPY {} FROM STDIN (FORMAT binary)").format(
                    sql.Identifier(table)
                )
                with source.cursor().copy(statement) as reader:
                    with target.cursor().copy(restore) as writer:
                        for block in reader:
                            writer.write(block)
            target.commit()

        # 恢复后的库：到期列仍然指向原始写入时间 + 2160 小时，不是恢复时间。
        (restored_expiry,) = self._fetch(
            "SELECT content_expires_at FROM task WHERE id = 'tsk_restore'", dsn=restored_dsn
        )
        self.assertLess(
            restored_expiry[0],
            now,
            "恢复后的到期时间必须还在过去——把恢复时间当成新起点就会变成未来",
        )

        # 第一轮清理就把四个载体全部收走。
        processed = self._sweep_all(now, dsn=restored_dsn)
        self.assertEqual(
            processed,
            {"task": 1, "inbound_event": 1, "pending_action": 1, "queue_failure_notice": 1},
            "恢复后的第一轮清理必须按原始写入时间收走全部到期内容",
        )
        self.assertEqual(
            self._fetch("SELECT prompt FROM task WHERE id = 'tsk_restore'", dsn=restored_dsn),
            [("",)],
        )
        self.assertEqual(self._fetch("SELECT 1 FROM inbound_event", dsn=restored_dsn), [])
        self.assertEqual(self._fetch("SELECT 1 FROM queue_failure_notice", dsn=restored_dsn), [])
        self.assertEqual(
            self._fetch(
                "SELECT target_open_id FROM pending_action WHERE id = 'pac_restore'",
                dsn=restored_dsn,
            ),
            [("redacted:pac_restore",)],
        )

    # ---- 迁移侧：到期列由触发器写死，写入方改不动也后移不了 ------------------

    def test_the_retention_expiry_of_a_pending_action_cannot_be_chosen_or_postponed(self) -> None:
        """``pending_action.retention_expires_at`` 由触发器固定，调用方写什么都被覆盖。"""

        import psycopg

        created = datetime.now(UTC) - timedelta(days=10)
        self._seed_pending_action("pac_frozen", created_at=created, target_suffix="_frozen")

        (stored,) = self._fetch(
            "SELECT retention_expires_at, created_at FROM pending_action WHERE id = 'pac_frozen'"
        )
        self.assertEqual(stored[0], stored[1] + RETENTION_WINDOW)

        # 直接改到期列：触发器按 created_at 重算，改不动。
        self._execute(
            "UPDATE pending_action SET retention_expires_at = %s WHERE id = 'pac_frozen'",
            (created + timedelta(days=3650),),
        )
        self.assertEqual(
            self._fetch("SELECT retention_expires_at FROM pending_action WHERE id = 'pac_frozen'"),
            [(stored[0],)],
        )

        # 改创建时间把期限往后挪：响亮拒绝。
        with self.assertRaises(psycopg.errors.RaiseException):
            self._execute("UPDATE pending_action SET created_at = now() WHERE id = 'pac_frozen'")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
