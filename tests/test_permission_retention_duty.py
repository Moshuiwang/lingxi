"""权限链九十天到期清理职责的断言（Epic C 冻结缺陷 F1）。

`redact_expired_payloads`（擦 ``publish_outbox.payload``，里面有邮箱与姓名）与
`purge_expired_checks`（删 ``mcp_sync_check`` 到期行）随 Issue #156 的 S-C-01 / S-C-02
一起交付，但**当时 ``src/`` 全树零调用方**——只有文档与测试提到它们，而数据库设计、
迁移 `0064` / `0065` 的头部都在用现在时说「到期后由 X 擦成 / 删除」。后果是跑满九十天
之后含个人数据的行原样留存，违反产品合同的保留上界，而文档让人以为它在跑。这与 #151
空闲会话清理是**同一个形状的缺陷**（方法在、没人调，内审 P2-2）。

本文件因此有三层断言，缺一层都盖不住这个缺陷：

1. **职责本身**（纯逻辑，假适配器）：每轮各调用一次、失败关闭并留审计、审计不含个人
   数据、停止中一条都不领、判定记录那一面没装配时只跑另一面；
2. **真库**：过期行真的被擦 / 删，**未过期的一个都不动**，并且删除面没有被扩大到别的
   表或别的列；
3. **装配**：`build_loop` 真的把这条路径接进了 `lingxi-scheduler`，外加一条源码级断言
   ——生产源码里必须存在这两个方法的调用点。第三层就是这个缺陷的正身：把调用点删掉
   必须变红。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from postgres_schema import ensure_production_schema, reset_production_rows

from lingxi.apps.scheduler import (
    PermissionRetentionReport,
    PermissionRetentionSweepDuty,
    SchedulerConfig,
    build_loop,
)

SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，权限链到期清理的真库断言未验证（需真实 PostgreSQL 16）"
)

# 规格公开的测试向量主密钥（= ASCII "0123456789abcdef0123456789abcdef"），**非生产密钥**。
SPEC_MASTER_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
# biai-agent 加密规格 v1 的**公开测试向量**（非生产令牌）。
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"

USER_A = "usr_retention_a"
USER_B = "usr_retention_b"
EMAIL_A = "jiaming.jia@example.invalid"
EMAIL_B = "yiming.yi@example.invalid"
NAME_A = "化名甲"

BASE_ENV = {
    "LINGXI_POSTGRES_DSN": "postgresql://user@localhost:5432/lingxi",
    "LINGXI_FEISHU_APP_ID": "cli_fake",
    "LINGXI_FEISHU_APP_SECRET": "secret_fake",
}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


class FakeOutbox:
    def __init__(self, *, redacted: int = 0, error: Exception | None = None) -> None:
        self._redacted = redacted
        self._error = error
        self.calls = 0

    def redact_expired_payloads(self) -> int:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._redacted


class FakeChecks:
    def __init__(self, *, purged: int = 0, error: Exception | None = None) -> None:
        self._purged = purged
        self._error = error
        self.calls = 0

    def purge_expired_checks(self) -> int:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._purged


class FakeNotices:
    """``onboarding_completion_notice`` 的到期整行删除（V-开通-18，迁移 0066）。"""

    def __init__(self, *, purged: int = 0, error: Exception | None = None) -> None:
        self._purged = purged
        self._error = error
        self.calls = 0

    def purge_expired_notices(self) -> int:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._purged


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))

    @property
    def actions(self) -> list[str]:
        return [action for action, _ in self.entries]


def build_duty(
    *,
    outbox: FakeOutbox | None = None,
    checks: FakeChecks | None = None,
    notices: FakeNotices | None = None,
    stop: threading.Event | None = None,
) -> tuple[PermissionRetentionSweepDuty, dict]:
    parts = {
        "outbox": outbox if outbox is not None else FakeOutbox(),
        "checks": checks,
        "notices": notices if notices is not None else FakeNotices(),
        "audit": RecordingAudit(),
    }
    duty = PermissionRetentionSweepDuty(
        outbox=parts["outbox"],
        checks=parts["checks"],
        notices=parts["notices"],
        audit=parts["audit"],
        stop=stop,
    )
    return duty, parts


# --------------------------------------------------------------------------
# 一、职责本身
# --------------------------------------------------------------------------


class SweepTest(unittest.TestCase):
    def test_every_round_sweeps_all_three_tables_exactly_once(self) -> None:
        duty, parts = build_duty(
            outbox=FakeOutbox(redacted=3), checks=FakeChecks(purged=7), notices=FakeNotices(purged=2)
        )

        report = duty.run_once()

        self.assertEqual(parts["outbox"].calls, 1)
        self.assertEqual(parts["checks"].calls, 1)
        self.assertEqual(parts["notices"].calls, 1)
        self.assertEqual(
            report,
            PermissionRetentionReport(redacted=3, purged=7, notices_purged=2, checks_wired=True),
        )

    def test_the_round_does_not_loop_until_empty(self) -> None:
        """每轮**只调用一次**，不在职责内部循环到擦空（同 ``RetentionCleanupDuty``）。

        一次调用就是一个事务，因此没有半擦状态；"擦空为止"会让一个积压很多到期行的库
        在单轮里长时间持锁，也让 ``SIGTERM`` 的退出时间不再有上界。
        """

        duty, parts = build_duty(
            outbox=FakeOutbox(redacted=500), checks=FakeChecks(purged=500), notices=FakeNotices(purged=500)
        )

        duty.run_once()

        self.assertEqual(
            (parts["outbox"].calls, parts["checks"].calls, parts["notices"].calls), (1, 1, 1)
        )

    def test_a_stopping_duty_sweeps_nothing(self) -> None:
        """`V-保留-17`「停止领取新工作」：已经在停止中就一条都不处置。"""

        stop = threading.Event()
        stop.set()
        duty, parts = build_duty(checks=FakeChecks(), stop=stop)

        self.assertIsNone(duty.run_once())
        self.assertEqual(
            (parts["outbox"].calls, parts["checks"].calls, parts["notices"].calls), (0, 0, 0)
        )
        self.assertEqual(parts["audit"].actions, [])

    def test_one_stop_flag_reaches_the_duty(self) -> None:
        stop = threading.Event()
        duty, _ = build_duty(stop=stop)

        self.assertFalse(duty.stopping)
        duty.request_stop()
        self.assertTrue(duty.stopping)
        self.assertTrue(stop.is_set())


class UnwiredChecksTest(unittest.TestCase):
    def test_without_the_master_key_only_the_payload_face_runs(self) -> None:
        """缺 MCP 令牌主密钥时判定记录那一面不装配，**内容擦除那一面照常**。

        带个人数据的是 ``publish_outbox.payload``；它一轮都不能少擦。
        """

        duty, parts = build_duty(outbox=FakeOutbox(redacted=2), checks=None)

        report = duty.run_once()

        self.assertEqual(parts["outbox"].calls, 1)
        self.assertFalse(duty.checks_wired)
        self.assertEqual(
            report,
            PermissionRetentionReport(redacted=2, purged=0, notices_purged=0, checks_wired=False),
        )
        self.assertEqual(
            parts["audit"].entries,
            [
                (
                    "permission_retention.completed",
                    {
                        "redacted": 2,
                        "purged": 0,
                        "notices_purged": 0,
                        "checks_wired": False,
                    },
                )
            ],
            "报告必须说得出'那一面没装配'，否则 purged=0 读起来像'没有到期行'",
        )


class FailClosedTest(unittest.TestCase):
    """**失败关闭**：任何一面失败都留审计并原样上抛，绝不折成 0。

    吞掉异常会让"到期内容一直没被处置"表现成每轮一条正常的完成审计，而保留违规最不该
    有的形态就是它悄无声息。
    """

    def test_a_failing_payload_sweep_is_audited_and_raised(self) -> None:
        duty, parts = build_duty(outbox=FakeOutbox(error=RuntimeError("锁等待超时")), checks=FakeChecks())

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(
            parts["audit"].entries,
            [("permission_retention.sweep_failed", {"table": "publish_outbox", "error": "RuntimeError"})],
        )
        self.assertEqual(parts["checks"].calls, 0, "上一面没做完就不往下走")
        self.assertNotIn("permission_retention.completed", parts["audit"].actions)

    def test_a_failing_check_purge_is_audited_and_raised(self) -> None:
        duty, parts = build_duty(checks=FakeChecks(error=RuntimeError("连接断开")))

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(
            parts["audit"].entries,
            [("permission_retention.sweep_failed", {"table": "mcp_sync_check", "error": "RuntimeError"})],
        )
        self.assertNotIn("permission_retention.completed", parts["audit"].actions)

    def test_a_failing_notice_purge_is_audited_and_raised(self) -> None:
        duty, parts = build_duty(notices=FakeNotices(error=RuntimeError("连接断开")))

        with self.assertRaises(RuntimeError):
            duty.run_once()

        self.assertEqual(
            parts["audit"].entries,
            [
                (
                    "permission_retention.sweep_failed",
                    {"table": "onboarding_completion_notice", "error": "RuntimeError"},
                )
            ],
        )
        self.assertNotIn("permission_retention.completed", parts["audit"].actions)

    def test_the_failure_audit_carries_only_the_exception_type(self) -> None:
        """否定断言：异常**正文**可能带上被处置那一行的内容，因此一个字都不进审计。"""

        duty, parts = build_duty(
            outbox=FakeOutbox(error=RuntimeError(f"擦除失败 payload={{'email': '{EMAIL_A}'}}"))
        )

        with self.assertRaises(RuntimeError):
            duty.run_once()

        rendered = repr(parts["audit"].entries)
        self.assertNotIn(EMAIL_A, rendered)
        self.assertNotIn("payload=", rendered)

    def test_a_duty_failure_does_not_take_the_other_duties_with_it(self) -> None:
        """`V-保留-15`：一个职责失败不影响同一轮里的其余职责（由 ``SchedulerLoop`` 隔离）。"""

        from lingxi.apps.scheduler import SchedulerLoop

        duty, _ = build_duty(outbox=FakeOutbox(error=RuntimeError("炸")))

        class Other:
            name = "另一个职责"

            def __init__(self) -> None:
                self.calls = 0

            def run_once(self) -> int:
                self.calls += 1
                return 1

        other = Other()
        SchedulerLoop(duties=(duty, other), interval_seconds=1).run_once()

        self.assertEqual(other.calls, 1)


class AuditContentTest(unittest.TestCase):
    def test_the_round_audit_carries_only_counts(self) -> None:
        """摘要只有计数：两个适配器方法的返回值本来也只有条数（同 ``V-保留-14`` 的纪律）。"""

        duty, parts = build_duty(outbox=FakeOutbox(redacted=1), checks=FakeChecks(purged=1))

        duty.run_once()

        action, fields = parts["audit"].entries[0]
        self.assertEqual(action, "permission_retention.completed")
        self.assertEqual(set(fields), {"redacted", "purged", "notices_purged", "checks_wired"})
        self.assertTrue(all(isinstance(value, (int, bool)) for value in fields.values()))


# --------------------------------------------------------------------------
# 二、真库：真的擦 / 真的删，且**未过期的一个都不动**
# --------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class RetentionSweepPostgresTest(unittest.TestCase):
    """只有真库能证伪这一组：到期判据是两条 SQL 的 ``content_expires_at`` 谓词，而那一列
    由触发器从 ``created_at`` / ``started_at`` 推导、调用方改不动。在假适配器上跑，
    无论谓词怎么写都是绿的。

    **过期行只能用夹具直接 INSERT 造**：``publish_outbox.created_at`` 一经写入就被触发器
    钉死（改它会 ``RAISE EXCEPTION``），因此不存在"先建一条、再把它改老"的路径；生产代码
    也不该有。这里写一条九十一天前的行，走的仍是同一张表、同一个触发器。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from lingxi.adapters.mcp_token_cipher import McpTokenCipher
        from lingxi.adapters.postgres import connect
        from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
        from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
        from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

        reset_production_rows(self._dsn)
        self._connect = connect
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for user_id, email, name in (
                (USER_A, EMAIL_A, NAME_A),
                (USER_B, EMAIL_B, "化名乙"),
            ):
                cursor.execute(
                    """INSERT INTO app_user
                         (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                          department, tenant_key, email)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, f"ou_{user_id}", f"fs_{user_id}", f"on_{user_id}", name,
                     "测试部门", "tenant-fake", email),
                )
        self.outbox = PostgresPermissionPublishStore(self._dsn)
        self.checks = PostgresMcpTokenStore(
            self._dsn, cipher=McpTokenCipher(SPEC_MASTER_KEY)
        )
        self.notices = PostgresLateReadinessStore(self._dsn)
        self.audit = RecordingAudit()
        self.duty = PermissionRetentionSweepDuty(
            outbox=self.outbox, checks=self.checks, notices=self.notices, audit=self.audit
        )

    # -- 夹具 ---------------------------------------------------------------

    def _intent(self, user_id: str, email: str, *, age_days: int, version: int = 1) -> str:
        """直接写一条 ``age_days`` 天前的发布意图，返回它的 ``id``。"""

        outbox_id = f"pub_fixture_{user_id}_{version}"
        payload = {
            "记录标识": email,
            "邮箱": email,
            "姓名": NAME_A,
            "权限": '{"1011":["商务"]}',
            "状态": "approved",
            "更新时间": "2026-08-17T03:00:00Z",
        }
        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO publish_outbox
                     (id, user_id, permission_version, reason, payload, status, attempts,
                      last_error, external_record_id, created_at, content_expires_at)
                   VALUES (%s, %s, %s, 'permission_refresh', %s::jsonb, 'failed', 5,
                           'http_500', 'rec_1', now() - %s::interval, now())""",
                (outbox_id, user_id, version, json.dumps(payload, ensure_ascii=False),
                 timedelta(days=age_days)),
            )
        return outbox_id

    def _check(self, user_id: str, *, age_days: int, version: int = 1) -> str:
        from lingxi.core.permission.mcp_readiness import (
            ReadinessAttempt,
            ReadinessBinding,
            ReadinessOutcome,
        )

        started = datetime.now(timezone.utc) - timedelta(days=age_days)
        return self.checks.record_attempt(
            ReadinessAttempt(
                binding=ReadinessBinding(user_id, version),
                attempt_no=1,
                outcome=ReadinessOutcome.WAITING,
                started_at=started,
                finished_at=started + timedelta(milliseconds=120),
                error_code="empty_metrics",
                metric_count=0,
            )
        )

    def _payload(self, outbox_id: str) -> dict:
        stored = self.outbox.load(outbox_id)
        assert stored is not None
        return dict(stored.payload)

    def _check_ids(self) -> set[str]:
        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM mcp_sync_check")
            return {str(row[0]) for row in cursor.fetchall()}

    # -- 断言 ---------------------------------------------------------------

    def test_the_round_redacts_only_the_expired_payload(self) -> None:
        expired = self._intent(USER_A, EMAIL_A, age_days=91)
        fresh = self._intent(USER_B, EMAIL_B, age_days=1)

        report = self.duty.run_once()

        self.assertEqual(report.redacted, 1)
        self.assertEqual(self._payload(expired), {}, "过期快照被擦成空对象")
        self.assertEqual(self._payload(fresh)["邮箱"], EMAIL_B, "未过期的一个字都不动")

    def test_the_round_purges_only_the_expired_checks(self) -> None:
        expired = self._check(USER_A, age_days=91)
        fresh = self._check(USER_B, age_days=1)

        report = self.duty.run_once()

        self.assertEqual(report.purged, 1)
        self.assertEqual(self._check_ids(), {fresh}, "只删过期那一行")
        self.assertNotIn(expired, self._check_ids())

    def test_nothing_expired_means_nothing_touched(self) -> None:
        """否定断言：没有到期行时**一行都不动**（不是"顺手清一批"）。"""

        fresh_intent = self._intent(USER_A, EMAIL_A, age_days=1)
        fresh_check = self._check(USER_A, age_days=1)

        report = self.duty.run_once()

        self.assertEqual((report.redacted, report.purged), (0, 0))
        self.assertEqual(self._payload(fresh_intent)["邮箱"], EMAIL_A)
        self.assertEqual(self._check_ids(), {fresh_check})

    def test_the_delete_surface_is_not_widened(self) -> None:
        """**不扩大删除面**（F1 的硬约束）：只擦 ``payload``、只删过期 ``mcp_sync_check``
        行，别的表与别的列一个都不碰。

        变异这条：把任一 SQL 的到期谓词去掉、或让职责顺手删 ``publish_outbox`` 的行、
        或碰 ``mcp_access_token`` / ``app_user``，这条断言立刻变红。
        """

        expired = self._intent(USER_A, EMAIL_A, age_days=91)
        self._check(USER_A, age_days=91)
        issued = self.checks.issue_token(USER_A)

        self.duty.run_once()

        with self._connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM publish_outbox")
            self.assertEqual(int(cursor.fetchone()[0]), 1, "到期的是内容，不是行本身")
            cursor.execute("SELECT count(*) FROM app_user")
            self.assertEqual(int(cursor.fetchone()[0]), 2, "用户表一行都不碰")
            cursor.execute("SELECT token_cipher FROM mcp_access_token WHERE user_id = %s", (USER_A,))
            self.assertEqual(str(cursor.fetchone()[0]), issued.token_cipher, "令牌密文一个字不动")

        stored = self.outbox.load(expired)
        assert stored is not None
        self.assertEqual(stored.payload, {})
        self.assertIsNone(stored.last_error, "擦的是 payload 与 last_error")
        # 运行事实留下：它们不可再映射到人（user_id 是内部 ULID，且随 app_user CASCADE）。
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.permission_version, 1)
        self.assertEqual(stored.attempts, 5)
        self.assertEqual(stored.external_record_id, "rec_1")

    def test_no_person_data_reaches_the_audit(self) -> None:
        self._intent(USER_A, EMAIL_A, age_days=91)

        self.duty.run_once()

        rendered = repr(self.audit.entries)
        for secret in (EMAIL_A, NAME_A, TOKEN_CIPHER):
            self.assertNotIn(secret, rendered)

    def test_the_sweep_is_idempotent(self) -> None:
        """幂等（`V-保留-10`）：已经擦过 / 删过的行不会被重复计数。"""

        self._intent(USER_A, EMAIL_A, age_days=91)
        self._check(USER_A, age_days=91)

        first = self.duty.run_once()
        second = self.duty.run_once()

        self.assertEqual((first.redacted, first.purged), (1, 1))
        self.assertEqual((second.redacted, second.purged), (0, 0))

    def test_without_the_check_face_the_payload_is_still_redacted(self) -> None:
        """缺主密钥时判定记录留着，但**带个人数据的那一面照常跑**。"""

        expired = self._intent(USER_A, EMAIL_A, age_days=91)
        stale_check = self._check(USER_A, age_days=91)
        duty = PermissionRetentionSweepDuty(
            outbox=self.outbox, checks=None, notices=self.notices, audit=self.audit
        )

        report = duty.run_once()

        self.assertEqual(report.redacted, 1)
        self.assertFalse(report.checks_wired)
        self.assertEqual(self._payload(expired), {})
        self.assertEqual(self._check_ids(), {stale_check}, "那一面没装配，行留着")


# --------------------------------------------------------------------------
# 三、装配：这条路径真的被调用
# --------------------------------------------------------------------------


class ProductionCallerSourceTest(unittest.TestCase):
    """**这个缺陷的正身**：方法在、没人调。

    **按 AST 找调用点，不按字符串找**：一句 docstring 里的方法名也能让 ``grep`` 变绿，
    而缺陷发生前那些 docstring 本来就都在。这里要的是真的有人取用那个属性。
    """

    OWNERS = {
        "redact_expired_payloads": "postgres_permission_publish.py",
        "purge_expired_checks": "postgres_mcp_token.py",
    }

    def test_both_expiry_methods_have_a_production_caller(self) -> None:
        source_root = Path(inspect.getsourcefile(build_loop)).parents[2]
        self.assertEqual(source_root.name, "lingxi")
        for method, defining_module in self.OWNERS.items():
            with self.subTest(method=method):
                callers = sorted(
                    path.relative_to(source_root).as_posix()
                    for path in source_root.rglob("*.py")
                    if path.name != defining_module
                    and _reads_attribute(path, method)
                )
                self.assertTrue(
                    callers,
                    f"{method} 在 src/ 全树没有生产调用方——那就是 F1 那个缺陷本身",
                )


def _reads_attribute(path: Path, attribute: str) -> bool:
    """源码里有没有真的取用 ``x.<attribute>``（含调用）。"""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Attribute) and node.attr == attribute for node in ast.walk(tree)
    )


@unittest.skipUnless(
    importlib.util.find_spec("psycopg") and importlib.util.find_spec("cryptography"),
    "跳过：build_loop 会真的构造凭据保管与清理适配器，需要 psycopg 与 cryptography",
)
class AssemblyTest(unittest.TestCase):
    """装配层断言：``build_loop`` 是 ``main()`` 唯一的装配入口，因此这一组就是"到期清理
    真的每轮都会跑"的证据。把职责从清单里摘掉，它立刻变红。"""

    def _config(self, **overrides: str) -> SchedulerConfig:
        """装配用的最小配置。会真的构造凭据保管对象，因此需要一个有效 Fernet 密钥与
        可写路径——两者都是本地临时物，不涉及任何真实凭据。"""

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
        matching = [
            duty for duty in loop.duties if isinstance(duty, PermissionRetentionSweepDuty)
        ]

        self.assertEqual(len(matching), 1, "权限链到期清理必须恰好注册一条")
        self.assertIn("权限链到期清理", [duty.name for duty in loop.duties])
        # 一个停止标志贯穿全部职责。
        loop.request_stop()
        self.assertTrue(matching[0].stopping)

    def test_the_assembled_duty_holds_the_real_adapters(self) -> None:
        """注册的不能是一个空壳：三个端口必须真的是那三个适配器。"""

        from lingxi.adapters.postgres_late_readiness_recovery import PostgresLateReadinessStore
        from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
        from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

        loop = build_loop(self._config(mcp_token_encrypt_key=SPEC_MASTER_KEY))
        (duty,) = [
            item for item in loop.duties if isinstance(item, PermissionRetentionSweepDuty)
        ]

        self.assertIsInstance(duty._outbox, PostgresPermissionPublishStore)
        self.assertIsInstance(duty._checks, PostgresMcpTokenStore)
        self.assertIsInstance(duty._notices, PostgresLateReadinessStore)
        self.assertTrue(duty.checks_wired)

    def test_without_the_master_key_the_duty_still_registers(self) -> None:
        """缺主密钥只关掉判定记录那一面，并留下**恰一条**审计；擦除那一面照常。"""

        audit = RecordingAudit()
        loop = build_loop(self._config(), audit=audit)
        (duty,) = [
            item for item in loop.duties if isinstance(item, PermissionRetentionSweepDuty)
        ]

        self.assertFalse(duty.checks_wired)
        self.assertIsNotNone(duty._outbox, "带个人数据的那一面无条件装配")
        retention = [
            entry for entry in audit.entries if entry[0].startswith("permission_retention.")
        ]
        self.assertEqual(
            [action for action, _ in retention], ["permission_retention.checks_not_wired"]
        )
        fields = retention[0][1]
        self.assertEqual(fields["reason"], "missing_environment_variable")
        self.assertEqual(fields["variable"], "LINGXI_MCP_TOKEN_ENCRYPT_KEY")
        self.assertNotIn(SPEC_MASTER_KEY, repr(fields), "只报变量名，不回显任何值")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
