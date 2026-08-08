"""#16 身份链路的真实 PostgreSQL 断言；不访问飞书、不使用真实凭据。

认领断言：V-开通-01、V-开通-06（真库部分）、V-开通-15、V-开通-16、V-身份-01、V-身份-02、V-身份-03。
另含「完整性校验不过不提交半轮快照」这条硬规则的真库负向测试。

缺 ``LINGXI_POSTGRES_DSN`` 时整类跳过并说明原因，不静默通过——数据库约束类
断言只能在真库上验证（验证与门禁第五节）。
"""

from __future__ import annotations

import dataclasses
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_identity import IdentityStorageIntegrityError
from lingxi.core.identity.credentials import AuthorizationGrant, SecretToken
from lingxi.core.identity.first_contact import (
    EmploymentStatus,
    FirstContactOutcome,
    decide_first_contact,
    locate_by_open_id,
)
from lingxi.core.identity.org_snapshot import (
    DirectoryAvailability,
    SnapshotBatch,
    SnapshotDepartment,
    SnapshotIntegrityError,
    SnapshotMember,
    TenantScope,
)

from postgres_schema import reset_production_rows

FAKE_TOKEN = "fake-refresh-token-for-tests-only"
DELEGATED_SUBJECT = "ou_delegated_authorization_subject"
SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，数据库约束类断言未验证（需真实 PostgreSQL）"


def member(
    *,
    tenant_key: str = "tenant_a",
    member_key: str = "ou_zhang",
    open_id: str = "ou_zhang",
    user_id: str = "user_zhang",
    union_id: str = "union_zhang",
    display_name: str = "张一",
    display_name_locale: str | None = "zh-CN",
    department_names: tuple[str, ...] = ("测试部门",),
) -> SnapshotMember:
    return SnapshotMember(
        tenant_key=tenant_key,
        member_key=member_key,
        open_id=open_id,
        user_id=user_id,
        union_id=union_id,
        display_name=display_name,
        display_name_locale=display_name_locale,
        department_names=department_names,
    )


def batch(members: tuple[SnapshotMember, ...], *, app_keys: frozenset[str] | None = None) -> SnapshotBatch:
    keys = frozenset(item.member_key for item in members)
    return SnapshotBatch(
        tenants=(TenantScope("tenant_a", True, keys if app_keys is None else app_keys, keys),),
        departments=(SnapshotDepartment("tenant_a", "dept_a", "测试部门"),),
        members=members,
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN"), SKIP_REASON)
class IdentityPostgresTestCase(unittest.TestCase):
    """所有真库用例的共同底座：每个用例前重建 006 / 007 / 008 三张迁移的表。"""

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        cls._psycopg = psycopg
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]

    def setUp(self) -> None:
        self.reset_schema()

    def reset_schema(self) -> None:
        """整条 alembic 链建库，不是挑 006/007/008 三条。

        此前这里硬编码三个文件名。保留清理 revision 在组织快照表上加了授权、又在
        银河侧加了触发器，按名字挑迁移会得到一个"少了一半新对象"的库——而少了的
        部分不会让任何用例变红，只会让它们在一个不完整的库上通过（#54 验收清单 H-02）。

        结构在进程内只建一次，这里每个用例做的是清行：本类有五十余个用例，
        每个都重跑一次整链前滚会把真库这一段抬到门禁超时的量级。
        """

        reset_production_rows(self._dsn)

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def scalar(self, sql: str, parameters: tuple = ()):
        rows = self.query(sql, parameters)
        return rows[0][0] if rows else None


class DelegatedCredentialTest(IdentityPostgresTestCase):
    """V-身份-03（2026-08-05 选项 A 形态）：密文只在宿主机文件，数据库零凭据。"""

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "delegated-credential.enc"
        self.vault = HostFileDelegatedCredentialVault(self._dsn, Fernet.generate_key().decode(), str(self.path))
        self.issued_at = datetime.now(timezone.utc)

    def _save(self, *, seconds: int = 7 * 24 * 3600, token: str = FAKE_TOKEN, issued_at: datetime | None = None) -> None:
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(token), seconds, "offline_access"),
            issued_at=issued_at or self.issued_at,
        )

    def test_ciphertext_lives_only_on_disk_and_the_database_holds_no_credential(self) -> None:
        self._save()

        blob = self.path.read_bytes()
        self.assertNotIn(FAKE_TOKEN.encode(), blob)
        self.assertEqual(self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"), DELEGATED_SUBJECT)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), oct(0o600))

    def test_the_registry_table_has_no_credential_column_at_all(self) -> None:
        """数据库设计原则 3：库里只存「是否已配置」，任何令牌形态的列都不允许存在。"""
        columns = {
            name
            for (name,) in self.query(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'feishu_delegated_subject'"
            )
        }

        self.assertEqual(columns, {"purpose", "subject_open_id", "configured_at", "updated_at"})

    def test_the_registry_does_not_depend_on_a_user_record(self) -> None:
        # 专用授权不是员工，不能因为 app_user 的建档与删除而失效。
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_name = 'feishu_delegated_subject' AND constraint_type = 'FOREIGN KEY'"
            ),
            0,
        )

    def test_at_most_one_delegated_subject_can_exist(self) -> None:
        self._save()
        self.vault.save(
            subject_open_id="ou_another_subject",
            grant=AuthorizationGrant(SecretToken("fake-second-token"), 3600, ""),
            issued_at=self.issued_at,
        )

        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_delegated_subject"), 1)
        self.assertEqual(self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"), "ou_another_subject")

    def test_an_unknown_purpose_is_rejected_by_the_database(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self.execute(
                "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES ('employee_token', 'ou_x')"
            )

    def test_the_rotation_point_follows_the_lifetime_returned_by_feishu(self) -> None:
        self._save(seconds=7 * 24 * 3600)

        credential = self.vault.load()

        assert credential is not None
        self.assertAlmostEqual((credential.refresh_at - self.issued_at).total_seconds(), 7 * 24 * 3600 * 0.8, delta=2)
        self.assertAlmostEqual((credential.expires_at - self.issued_at).total_seconds(), 7 * 24 * 3600, delta=2)

    def test_load_returns_the_plaintext_only_through_an_explicit_reveal(self) -> None:
        self._save()

        credential = self.vault.load()

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.grant.refresh_token.reveal(), FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, repr(credential))

    def test_claiming_a_due_credential_succeeds_exactly_once(self) -> None:
        # 一次性凭据不能被同一轮扫描领两次；消费标记就是那道门。
        self._save(seconds=3600, issued_at=datetime.now(timezone.utc) - timedelta(seconds=3500))

        first = self.vault.claim_due()
        second = self.vault.claim_due()

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_a_credential_that_is_not_due_is_not_claimed(self) -> None:
        self._save(seconds=7 * 24 * 3600)

        self.assertIsNone(self.vault.claim_due())

    def test_concurrent_claims_are_serialized_by_the_file_lock(self) -> None:
        """fcntl 排他锁扮演数据库版 SKIP LOCKED 的角色：并发领取恰好一个成功。"""
        import threading as _threading

        self._save(seconds=3600, issued_at=datetime.now(timezone.utc) - timedelta(seconds=3500))
        results: list[object] = []

        def worker() -> None:
            results.append(self.vault.claim_due())

        threads = [_threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for item in results if item is not None), 1)

    def test_an_undecryptable_credential_file_is_revoked_rather_than_retried(self) -> None:
        self._save()
        self.path.write_bytes(b"\x01\x02broken")

        self.assertIsNone(self.vault.load())
        self.assertFalse(self.path.exists())

    def test_revoking_removes_the_file_but_keeps_the_registry_row(self) -> None:
        """撤销只动凭据文件；登记行是 V-身份-02 触发器的数据来源，必须留下。"""
        self._save()

        self.assertTrue(self.vault.revoke(reason="test"))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_delegated_subject"), 1)
        self.assertIsNone(self.vault.load())
        self.assertFalse(self.vault.revoke(reason="twice"))

    def test_the_delegated_subject_is_still_rejected_after_revocation(self) -> None:
        """撤销后为专用授权主体建档，仍被数据库拒绝（登记行未消失）。"""
        self._save()
        self.assertTrue(self.vault.revoke(reason="test"))

        subject = self.scalar("SELECT subject_open_id FROM feishu_delegated_subject")
        self.assertIsNotNone(subject)
        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id,
                      display_name, department, tenant_key)
                   VALUES ('usr_test_revoked_subject', %s, 'u', 'un', '某人', '部门', 't')""",
                (subject,),
            )


class ConsumedCredentialTest(IdentityPostgresTestCase):
    """一次性令牌的消费语义与双向主体防线（Codex 复查 P1，文件保管形态）。"""

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "delegated-credential.enc"
        self.vault = HostFileDelegatedCredentialVault(self._dsn, Fernet.generate_key().decode(), str(self.path))
        self.issued_at = datetime.now(timezone.utc) - timedelta(days=6)
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(FAKE_TOKEN), 7 * 24 * 3600, "offline_access"),
            issued_at=self.issued_at,
        )

    def test_a_claim_marks_the_credential_consumed_and_hides_it_everywhere(self) -> None:
        claimed = self.vault.claim_due()

        self.assertIsNotNone(claimed)
        # 消费中：旧令牌可能已被飞书作废，load 与再次领取都不得再拿到它——
        # 现在与任何未来时刻都一样（挡住重放的是消费标记本身）。
        self.assertIsNone(self.vault.load())
        self.assertIsNone(self.vault.claim_due())
        later = datetime.now(timezone.utc) + timedelta(seconds=3600)
        self.assertIsNone(self.vault.claim_due(now=later))
        self.assertIsNone(self.vault.load(now=later))

    def test_saving_the_replacement_clears_the_consumed_marker(self) -> None:
        self.vault.claim_due()
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken("fake-next-token"), 7 * 24 * 3600, "offline_access"),
        )

        self.assertIsNotNone(self.vault.load())

    def test_a_stale_consumed_file_is_swept_with_a_distinct_log(self) -> None:
        """进程在续期后、落盘前死掉的形状：旧令牌已作废，收殓而不是留给未来重放。"""
        self.vault.claim_due()

        with self.assertLogs("lingxi.adapters.delegated_credentials", level="ERROR") as captured:
            cleared = self.vault.revoke_stale_consumed(max_age_seconds=0, now=datetime.now(timezone.utc) + timedelta(seconds=1))

        self.assertTrue(cleared)
        self.assertTrue(any("不可恢复" in line for line in captured.output))
        self.assertFalse(self.path.exists())
        # 登记行仍在：V-身份-02 的数据源不随收殓消失。
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_delegated_subject"), 1)

    def test_an_existing_app_user_open_id_cannot_become_the_delegated_subject(self) -> None:
        """反向防线（Codex 复查）：先有员工记录、再把同一 open_id 写成专用授权
        主体，必须被数据库拒绝；且**拒绝发生在密文落盘之前**（save 先登记后写盘）。"""
        self.execute(
            """INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key)
               VALUES ('usr_existing_employee', 'ou_employee_x', 'u_x', 'un_x', '某员工', '部门', 't_a')"""
        )
        self.vault.revoke(reason="reset")

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.vault.save(
                subject_open_id="ou_employee_x",
                grant=AuthorizationGrant(SecretToken("fake-second"), 3600, ""),
            )
        self.assertFalse(self.path.exists())


class CredentialGenerationGuardTest(IdentityPostgresTestCase):
    """终轮 Codex P1：轮换收尾必须世代匹配——期间的新授权不得被旧链覆盖或删除。"""

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "delegated-credential.enc"
        self.vault = HostFileDelegatedCredentialVault(self._dsn, Fernet.generate_key().decode(), str(self.path))
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600, ""),
            issued_at=datetime.now(timezone.utc) - timedelta(seconds=3500),
        )

    def test_a_reauthorization_between_claim_and_save_wins(self) -> None:
        claimed = self.vault.claim_due()
        assert claimed is not None
        # 领取之后专用用户完成了一次新授权：
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken("fake-fresh-token"), 7 * 24 * 3600, ""),
        )

        stale_write = self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken("fake-stale-rotation"), 7 * 24 * 3600, ""),
            replacing_generation=claimed.generation,
        )

        self.assertFalse(stale_write)
        fresh = self.vault.load()
        assert fresh is not None
        self.assertEqual(fresh.grant.refresh_token.reveal(), "fake-fresh-token")

    def test_a_reauthorization_between_claim_and_revoke_survives(self) -> None:
        claimed = self.vault.claim_due()
        assert claimed is not None
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken("fake-fresh-token"), 7 * 24 * 3600, ""),
        )

        self.assertFalse(self.vault.revoke(reason="refresh_failed", generation=claimed.generation))
        self.assertIsNotNone(self.vault.load())

    def test_a_subject_mismatch_between_file_and_registry_fails_closed(self) -> None:
        """终轮 Codex P1：登记指向 B、文件仍是 A 时，A 已在防线之外——
        清除文件并要求重新授权，绝不继续用 A 的凭据。"""
        self.execute(
            "UPDATE feishu_delegated_subject SET subject_open_id = 'ou_new_subject_b'"
        )

        with self.assertLogs("lingxi.adapters.delegated_credentials", level="ERROR") as captured:
            credential = self.vault.load()

        self.assertIsNone(credential)
        self.assertFalse(self.path.exists())
        self.assertTrue(any("不一致" in line for line in captured.output))


class DatabaseConsistencyBackstopTest(IdentityPostgresTestCase):
    """终轮 Codex P2：数据库层兜底——声明计数造假与缺部门直插都被拒。"""

    def test_a_complete_batch_with_fake_counts_and_no_children_is_rejected(self) -> None:
        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                """INSERT INTO feishu_org_sync_run
                     (id, source_app_id, status, started_at, completed_at, tenant_count, department_count, member_count)
                   VALUES ('run_fake', 'cli_fake', 'complete', now(), now(), 3, 1, 5)"""
            )

    def test_an_identity_row_without_a_department_is_rejected_by_the_database(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self.execute(
                """INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id,
                                          display_name, tenant_key)
                   VALUES ('usr_no_dept', 'ou_nd', 'u_nd', 'un_nd', '无部门', 't_nd')"""
            )


class TriggerRaceSerializationTest(IdentityPostgresTestCase):
    """终轮 Codex P1：两个 BEFORE 触发器的 EXISTS 在 MVCC 下互看不见未提交行；
    advisory 锁必须把两条写路径串行化，并发下双向防线仍然成立。"""

    def test_an_uncommitted_app_user_still_blocks_the_registry_write(self) -> None:
        import threading as _threading

        started = _threading.Event()
        outcome: dict[str, object] = {}

        connection_one = connect(self._dsn)
        try:
            cursor = connection_one.cursor()
            cursor.execute(
                """INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id,
                                          display_name, department, tenant_key)
                   VALUES ('usr_race', 'ou_race_subject', 'u_r', 'un_r', '竞态员工', '部门', 't_r')"""
            )

            def registry_writer() -> None:
                started.set()
                try:
                    with connect(self._dsn) as connection_two, connection_two.cursor() as cursor_two:
                        cursor_two.execute(
                            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) "
                            "VALUES ('org_directory_sync', 'ou_race_subject')"
                        )
                except Exception as error:  # noqa: BLE001 - 测试收集
                    outcome["error"] = error

            worker = _threading.Thread(target=registry_writer)
            worker.start()
            started.wait()
            # 触发器在 advisory 锁上排队；提交事务一让它看到已提交的员工行。
            import time as _time

            _time.sleep(0.3)
            connection_one.commit()
            worker.join(timeout=10)
        finally:
            connection_one.close()

        self.assertIn("error", outcome)
        self.assertIsInstance(outcome["error"], self._psycopg.errors.RaiseException)


class SnapshotCommitOrderingTest(IdentityPostgresTestCase):
    """较早启动、较晚完成的批次不得取代更新的批次（Codex 复查 P2）。"""

    def _store(self):
        from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore

        return PostgresOrgSnapshotStore(self._dsn)

    def test_an_older_started_batch_finishing_late_does_not_supersede_the_newer(self) -> None:
        """两轮同步重叠时，后提交但**更早启动**的那轮不得取代更新的数据；
        started_at 经 commit_batch 参数按同步真实开始时刻传入。"""
        store = self._store()
        now = datetime.now(timezone.utc)
        newer = store.commit_batch(batch((member(),)), source_app_id="cli_fake", started_at=now)

        older = store.commit_batch(
            batch((member(),)), source_app_id="cli_fake", started_at=now - timedelta(hours=1)
        )

        self.assertEqual(self.scalar("SELECT status FROM feishu_org_sync_run WHERE id = %s", (newer,)), "complete")
        self.assertEqual(self.scalar("SELECT status FROM feishu_org_sync_run WHERE id = %s", (older,)), "superseded")


class OrgSnapshotTest(IdentityPostgresTestCase):
    """完整性校验不过就不提交半轮快照——这里是它的真库负向测试。"""

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore

        self.store = PostgresOrgSnapshotStore(self._dsn)

    def test_a_complete_batch_is_written_in_one_go(self) -> None:
        run_id = self.store.commit_batch(batch((member(),)), source_app_id="cli_fake")

        self.assertEqual(self.scalar("SELECT status FROM feishu_org_sync_run WHERE id = %s", (run_id,)), "complete")
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_member_snapshot"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_tenant_snapshot"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_department_snapshot"), 1)

    def test_an_incomplete_batch_leaves_no_member_row_at_all(self) -> None:
        broken = batch((member(),), app_keys=frozenset({"ou_zhang", "ou_only_visible_to_app"}))

        with self.assertRaises(SnapshotIntegrityError):
            self.store.commit_batch(broken, source_app_id="cli_fake")

        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_member_snapshot"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_tenant_snapshot"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_sync_run WHERE status = 'complete'"), 0)
        self.assertEqual(self.scalar("SELECT status FROM feishu_org_sync_run"), "failed")

    def test_a_failed_batch_never_becomes_the_source_of_a_location(self) -> None:
        with self.assertRaises(SnapshotIntegrityError):
            self.store.commit_batch(batch((member(open_id="  "),)), source_app_id="cli_fake")

        lookup = self.store.lookup("ou_zhang")

        self.assertIs(lookup.availability, DirectoryAvailability.UNAVAILABLE)
        self.assertEqual(lookup.members, ())

    def test_the_database_refuses_a_complete_run_with_no_members(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self.execute(
                "INSERT INTO feishu_org_sync_run (id, source_app_id, status, completed_at, expires_at, tenant_count, member_count) "
                "VALUES ('orgsync_empty', 'cli_fake', 'complete', now(), now(), 1, 0)"
            )

    def test_the_database_refuses_a_failed_run_that_claims_to_have_content(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self.execute(
                "INSERT INTO feishu_org_sync_run (id, source_app_id, status, expires_at, member_count) "
                "VALUES ('orgsync_lying', 'cli_fake', 'failed', now(), 7)"
            )

    def test_the_expiry_is_pinned_to_the_source_time_and_cannot_be_pushed_out(self) -> None:
        self.execute(
            "INSERT INTO feishu_org_sync_run (id, source_app_id, status, started_at, expires_at) "
            "VALUES ('orgsync_expiry', 'cli_fake', 'staging', now(), now() + interval '900 days')"
        )
        started_at, expires_at = self.query(
            "SELECT started_at, expires_at FROM feishu_org_sync_run WHERE id = 'orgsync_expiry'"
        )[0]
        self.assertEqual(expires_at - started_at, timedelta(hours=2160))

        self.execute("UPDATE feishu_org_sync_run SET expires_at = now() + interval '900 days' WHERE id = 'orgsync_expiry'")
        started_at, expires_at = self.query(
            "SELECT started_at, expires_at FROM feishu_org_sync_run WHERE id = 'orgsync_expiry'"
        )[0]

        self.assertEqual(expires_at - started_at, timedelta(hours=2160))

    def test_the_snapshot_never_stores_employment_status(self) -> None:
        """硬约束 2：存下来的在职状态一定是陈旧的，而它的唯一用途就是拦截陈旧状态。"""
        columns = {
            name
            for (name,) in self.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'feishu_org_member_snapshot'"
            )
        }

        for forbidden in ("status", "is_activated", "is_exited", "is_frozen", "is_resigned", "is_unjoin"):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_a_member_row_missing_an_identity_field_is_refused_by_the_database(self) -> None:
        self.execute(
            "INSERT INTO feishu_org_sync_run (id, source_app_id, status, expires_at) "
            "VALUES ('orgsync_guard', 'cli_fake', 'staging', now())"
        )
        for column in ("open_id", "user_id", "union_id", "display_name"):
            with self.subTest(column=column):
                values = {"open_id": "ou_x", "user_id": "user_x", "union_id": "union_x", "display_name": "张一"}
                values[column] = "   "
                with self.assertRaises(self._psycopg.errors.CheckViolation):
                    self.execute(
                        "INSERT INTO feishu_org_member_snapshot "
                        "(id, sync_run_id, tenant_key, member_key, open_id, user_id, union_id, display_name) "
                        "VALUES ('member_x', 'orgsync_guard', 'tenant_a', 'ou_x', %s, %s, %s, %s)",
                        (values["open_id"], values["user_id"], values["union_id"], values["display_name"]),
                    )

    def test_lookup_returns_the_member_of_the_latest_complete_batch_only(self) -> None:
        self.store.commit_batch(batch((member(display_name="旧的张一"),)), source_app_id="cli_fake")
        self.store.commit_batch(batch((member(display_name="新的张一"),)), source_app_id="cli_fake")

        lookup = self.store.lookup("ou_zhang")

        self.assertIs(lookup.availability, DirectoryAvailability.AVAILABLE)
        self.assertEqual(len(lookup.members), 1)
        self.assertEqual(lookup.members[0].display_name, "新的张一")
        self.assertEqual(lookup.members[0].department_names, ("测试部门",))

    def test_an_expired_batch_is_stale_and_yields_no_candidate(self) -> None:
        self.store.commit_batch(
            batch((member(),)),
            source_app_id="cli_fake",
            started_at=datetime.now(timezone.utc) - timedelta(days=91),
        )

        lookup = self.store.lookup("ou_zhang")

        self.assertIs(lookup.availability, DirectoryAvailability.STALE)
        self.assertEqual(lookup.members, ())

    def test_an_unknown_open_id_yields_no_candidate_without_falling_back_to_a_name(self) -> None:
        self.store.commit_batch(batch((member(),)), source_app_id="cli_fake")

        self.assertEqual(self.store.lookup("ou_absent").members, ())
        self.assertEqual(self.store.lookup("ou_zha").members, ())


class AppUserRecordTest(IdentityPostgresTestCase):
    """V-开通-01 / V-开通-06 / V-开通-15 / V-开通-16 / V-身份-01 / V-身份-02 的真库部分。"""

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresAppUserStore, PostgresOrgSnapshotStore

        self.users = PostgresAppUserStore(self._dsn)
        self.snapshots = PostgresOrgSnapshotStore(self._dsn)

    def _draft(self, **overrides):
        candidate = member(**overrides)
        located = locate_by_open_id(candidate.open_id, (candidate,))
        decision = decide_first_contact(
            open_id=candidate.open_id,
            location=located,
            employment=EmploymentStatus(is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False),
            directory=DirectoryAvailability.AVAILABLE,
            delegated_subject_open_id=DELEGATED_SUBJECT,
        )
        assert decision.draft is not None
        return decision.draft

    def test_roster_fields_default_to_null_and_survive_a_refresh(self) -> None:
        """工号/邮箱来自花名册读取步骤（2026-08-05 决策）：建档默认 NULL；
        已写入后，不携带花名册数据的后续刷新不得把它们抹掉。"""
        self.users.record_identity(self._draft())
        self.assertEqual(self.query("SELECT employee_no, email FROM app_user"), [(None, None)])

        enriched = dataclasses.replace(
            self._draft(), employee_no="80001", email="he.xugong@example-corp.invalid"
        )
        self.users.record_identity(enriched)
        self.users.record_identity(self._draft())

        self.assertEqual(
            self.query("SELECT employee_no, email FROM app_user"),
            [("80001", "he.xugong@example-corp.invalid")],
        )

    def test_roster_fields_round_trip_through_app_user_adapter_and_catalog(self) -> None:
        """V-开通-15：模型字段不能停在内存里，正式适配器必须写入并回读原值。"""

        draft = dataclasses.replace(
            self._draft(), employee_no="00080001", email="Roster.User@Example-Corp.invalid"
        )

        written = self.users.record_identity(draft)
        loaded = self.users.get_by_open_id(draft.feishu_open_id)

        self.assertEqual((written.employee_no, written.email), ("00080001", "Roster.User@Example-Corp.invalid"))
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual((loaded.employee_no, loaded.email), ("00080001", "Roster.User@Example-Corp.invalid"))
        self.assertEqual(
            self.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'app_user' "
                "AND column_name IN ('employee_no', 'email') ORDER BY column_name"
            ),
            [("email",), ("employee_no",)],
        )
        self.assertEqual(
            self.query("SELECT employee_no, email FROM app_user WHERE id = %s", (written.id,)),
            [("00080001", "Roster.User@Example-Corp.invalid")],
        )

    def test_roster_baseline_reads_the_same_nonempty_archive_after_identity_write(self) -> None:
        """V-开通-16：#52 读取的三字段必须来自同一份真实建档基线。"""

        from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader

        draft = dataclasses.replace(
            self._draft(),
            employee_no="00080002",
            email="baseline.user@example-corp.invalid",
            provisioning_state="active",
        )
        written = self.users.record_identity(draft)
        loaded = self.users.get_by_open_id(draft.feishu_open_id)
        baseline = PostgresRosterBaselineReader(self._dsn).load_active_baseline()
        stored = self.query(
            "SELECT display_name, employee_no, email FROM app_user WHERE id = %s", (written.id,)
        )

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(len(baseline), 1)
        self.assertEqual(stored, [(draft.display_name, "00080002", "baseline.user@example-corp.invalid")])
        self.assertTrue(all(stored[0]), "日报基线不得把建档工号或邮箱静默读成空值")
        self.assertEqual(
            (baseline[0].display_name, baseline[0].employee_no, baseline[0].email),
            stored[0],
        )
        self.assertEqual((loaded.employee_no, loaded.email), stored[0][1:])

    def test_empty_roster_field_from_database_fails_closed_and_rolls_back(self) -> None:
        """V-开通-15：模型有工号但数据库回读为空时不得静默建档。"""

        trigger_name = "test_i89_blank_employee_no"
        function_name = "test_i89_blank_employee_no"
        self.execute(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON app_user; "
            f"DROP FUNCTION IF EXISTS {function_name}();"
        )
        self.addCleanup(
            lambda: self.execute(
                f"DROP TRIGGER IF EXISTS {trigger_name} ON app_user; "
                f"DROP FUNCTION IF EXISTS {function_name}();"
            )
        )
        self.execute(
            f"""CREATE FUNCTION {function_name}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                NEW.employee_no := NULL;
                RETURN NEW;
            END
            $$;
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT OR UPDATE ON app_user
            FOR EACH ROW EXECUTE FUNCTION {function_name}();"""
        )

        draft = dataclasses.replace(self._draft(), employee_no="00080003", email="blank.field@example.invalid")
        with self.assertRaises(IdentityStorageIntegrityError) as raised:
            self.users.record_identity(draft)

        self.assertIn("employee_no", str(raised.exception))
        self.assertEqual(
            self.scalar("SELECT count(*) FROM app_user WHERE feishu_open_id = %s", (draft.feishu_open_id,)),
            0,
        )

    def test_rewritten_roster_field_from_database_fails_closed_and_rolls_back(self) -> None:
        """V-开通-15：数据库回读被改写时不得把不一致资料交给后续链路。"""

        trigger_name = "test_i89_rewrite_roster_email"
        function_name = "test_i89_rewrite_roster_email"
        self.execute(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON app_user; "
            f"DROP FUNCTION IF EXISTS {function_name}();"
        )
        self.addCleanup(
            lambda: self.execute(
                f"DROP TRIGGER IF EXISTS {trigger_name} ON app_user; "
                f"DROP FUNCTION IF EXISTS {function_name}();"
            )
        )
        self.execute(
            f"""CREATE FUNCTION {function_name}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                NEW.email := lower(NEW.email);
                RETURN NEW;
            END
            $$;
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT OR UPDATE ON app_user
            FOR EACH ROW EXECUTE FUNCTION {function_name}();"""
        )

        draft = dataclasses.replace(self._draft(), employee_no="00080004", email="Rewrite.Field@Example.invalid")
        with self.assertRaises(IdentityStorageIntegrityError) as raised:
            self.users.record_identity(draft)

        self.assertIn("email", str(raised.exception))
        self.assertEqual(
            self.scalar("SELECT count(*) FROM app_user WHERE feishu_open_id = %s", (draft.feishu_open_id,)),
            0,
        )

    def test_concurrent_upserts_of_the_same_open_id_leave_exactly_one_record(self) -> None:
        """V-身份-01 的并发面：两个连接同时建档同一 open_id，唯一索引 +
        单语句 upsert 必须收敛到一条记录（补上 PR 正文声称过的并发用例）。"""
        import threading as _threading

        errors: list[BaseException] = []

        def upsert() -> None:
            try:
                self.users.record_identity(self._draft())
            except BaseException as error:  # noqa: BLE001 - 测试只收集
                errors.append(error)

        workers = [_threading.Thread(target=upsert) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.scalar("SELECT count(*) FROM app_user"), 1)

    def test_an_identity_switch_clears_stale_roster_fields(self) -> None:
        """账号复用换人（#34 方案 C 不拦截）：同一 open_id 换 user_id 后，
        旧人的工号/邮箱不得残留——工号是匹配银河的主键，残留等于把新人
        接到旧人的权限记录上（独立复查发现）。"""
        enriched = dataclasses.replace(
            self._draft(), employee_no="80001", email="he.xugong@example-corp.invalid"
        )
        self.users.record_identity(enriched)

        handover = dataclasses.replace(
            self._draft(),
            feishu_user_id="user_new_owner",
            feishu_union_id="union_new_owner",
            display_name="接手人",
        )
        self.users.record_identity(handover)

        self.assertEqual(self.query("SELECT employee_no, email FROM app_user"), [(None, None)])

    def test_a_new_identity_is_recorded_without_a_permission_record(self) -> None:
        record = self.users.record_identity(self._draft())

        self.assertTrue(record.created)
        self.assertTrue(record.id.startswith("usr_"))
        self.assertEqual(record.provisioning_state, "matching")
        self.assertIsNone(record.permission_record_id)
        self.assertIsNone(self.scalar("SELECT permission_record_id FROM app_user"))
        self.assertEqual(self.scalar("SELECT permission_version FROM app_user"), 0)

    def test_the_permission_column_exists_so_the_null_is_a_verifiable_fact(self) -> None:
        # 「字段不存在」证明不了「匹配确认前为 NULL」，只有列存在且为 NULL 才是事实。
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'app_user' AND column_name = 'permission_record_id'"
            ),
            1,
        )

    def test_recording_the_same_identity_twice_never_creates_a_second_record(self) -> None:
        """V-身份-01。"""
        first = self.users.record_identity(self._draft())
        second = self.users.record_identity(self._draft(display_name="张一改名"))

        self.assertEqual(first.id, second.id)
        self.assertFalse(second.created)
        self.assertEqual(self.users.count(), 1)
        self.assertEqual(self.scalar("SELECT display_name FROM app_user"), "张一改名")

    def test_a_second_row_with_the_same_open_id_is_refused_by_the_database(self) -> None:
        self.users.record_identity(self._draft())

        with self.assertRaises(self._psycopg.errors.UniqueViolation):
            self.execute(
                "INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key) "
                "VALUES ('usr_duplicate', 'ou_zhang', 'user_other', 'union_other', '另一个张一', '部门', 'tenant_a')"
            )

    def test_recording_again_never_resets_an_advanced_provisioning_state(self) -> None:
        self.users.record_identity(self._draft())
        self.execute("UPDATE app_user SET provisioning_state = 'mcp_syncing'")

        record = self.users.record_identity(self._draft())

        self.assertEqual(record.provisioning_state, "mcp_syncing")

    def test_recording_again_never_touches_the_permission_record(self) -> None:
        self.users.record_identity(self._draft())
        self.execute("UPDATE app_user SET permission_record_id = 'rec_matched', permission_version = 3")

        self.users.record_identity(self._draft())

        self.assertEqual(self.scalar("SELECT permission_record_id FROM app_user"), "rec_matched")
        self.assertEqual(self.scalar("SELECT permission_version FROM app_user"), 3)

    def test_the_delegated_authorization_subject_cannot_be_recorded_as_a_user(self) -> None:
        """V-身份-02：应用层已拦一次，数据库是绕不过去的那一道。"""
        self.execute(
            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES ('org_directory_sync', %s)",
            (DELEGATED_SUBJECT,),
        )

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute(
                "INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, department, tenant_key) "
                "VALUES ('usr_delegated', %s, 'user_delegated', 'union_delegated', '专用授权账号', '部门', 'tenant_a')",
                (DELEGATED_SUBJECT,),
            )
        self.assertEqual(self.users.count(), 0)

    def test_an_existing_record_cannot_be_repointed_at_the_delegated_subject(self) -> None:
        self.users.record_identity(self._draft())
        self.execute(
            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES ('org_directory_sync', %s)",
            (DELEGATED_SUBJECT,),
        )

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.execute("UPDATE app_user SET feishu_open_id = %s", (DELEGATED_SUBJECT,))

    def test_a_half_written_identity_is_refused_by_the_database(self) -> None:
        """V-开通-06：定位失败或资料不完整时不写半条记录。"""
        for column in ("feishu_user_id", "feishu_union_id", "display_name", "tenant_key"):
            with self.subTest(column=column):
                values = {
                    "feishu_user_id": "user_zhang",
                    "feishu_union_id": "union_zhang",
                    "display_name": "张一",
                    "tenant_key": "tenant_a",
                }
                values[column] = "   "
                with self.assertRaises(self._psycopg.errors.CheckViolation):
                    self.execute(
                        "INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name, tenant_key) "
                        "VALUES ('usr_partial', 'ou_partial', %s, %s, %s, %s)",
                        (values["feishu_user_id"], values["feishu_union_id"], values["display_name"], values["tenant_key"]),
                    )
        self.assertEqual(self.users.count(), 0)

    def test_the_user_record_has_no_employment_status_column(self) -> None:
        columns = {
            name
            for (name,) in self.query("SELECT column_name FROM information_schema.columns WHERE table_name = 'app_user'")
        }

        for forbidden in ("status", "is_activated", "is_exited", "is_frozen", "is_resigned", "is_unjoin", "employment_status"):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_the_user_record_has_no_credential_column(self) -> None:
        columns = {
            name
            for (name,) in self.query("SELECT column_name FROM information_schema.columns WHERE table_name = 'app_user'")
        }

        for forbidden in ("refresh_token", "access_token", "encrypted_refresh_token", "authorization_code"):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_two_people_with_the_same_display_name_both_get_a_record(self) -> None:
        """硬约束 3：姓名不是唯一键。"""
        self.users.record_identity(self._draft(display_name="张三"))
        self.users.record_identity(
            self._draft(open_id="ou_second", member_key="ou_second", user_id="user_second", union_id="union_second", display_name="张三")
        )

        self.assertEqual(self.users.count(), 2)
        self.assertEqual(self.scalar("SELECT count(DISTINCT feishu_user_id) FROM app_user"), 2)

    def test_a_latin_only_name_is_recorded_unchanged(self) -> None:
        """V-开通-08。"""
        self.users.record_identity(self._draft(display_name="Alice Smith", display_name_locale="en-US"))

        self.assertEqual(self.scalar("SELECT display_name FROM app_user"), "Alice Smith")
        self.assertEqual(self.scalar("SELECT display_name_locale FROM app_user"), "en-US")


class FirstContactThroughPostgresTest(IdentityPostgresTestCase):
    """把定位、判定与建档串起来跑一次，确认没有旁路能写出半条记录。"""

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresAppUserStore, PostgresOrgSnapshotStore

        self.users = PostgresAppUserStore(self._dsn)
        self.snapshots = PostgresOrgSnapshotStore(self._dsn)

    def _handle(self, open_id: str, employment: EmploymentStatus | None):
        lookup = self.snapshots.lookup(open_id)
        decision = decide_first_contact(
            open_id=open_id,
            location=locate_by_open_id(open_id, lookup.members),
            employment=employment,
            directory=lookup.availability,
            delegated_subject_open_id=DELEGATED_SUBJECT,
        )
        if decision.draft is not None:
            self.users.record_identity(decision.draft)
        return decision

    def test_an_employed_member_gets_exactly_one_record_however_many_times_they_write(self) -> None:
        self.snapshots.commit_batch(batch((member(),)), source_app_id="cli_fake")
        employed = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False)

        for _ in range(3):
            decision = self._handle("ou_zhang", employed)

        self.assertIs(decision.outcome, FirstContactOutcome.RECORD_READY)
        self.assertEqual(self.users.count(), 1)

    def test_a_frozen_member_is_refused_and_nothing_is_written(self) -> None:
        self.snapshots.commit_batch(batch((member(),)), source_app_id="cli_fake")
        frozen = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=True, is_resigned=False, is_unjoin=False)

        decision = self._handle("ou_zhang", frozen)

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertEqual(self.users.count(), 0)

    def test_an_unlocatable_sender_is_not_authorized_and_nothing_is_written(self) -> None:
        self.snapshots.commit_batch(batch((member(),)), source_app_id="cli_fake")
        employed = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False)

        decision = self._handle("ou_absent", employed)

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertEqual(self.users.count(), 0)

    def test_without_any_snapshot_the_sender_gets_a_terminal_state_and_nothing_is_written(self) -> None:
        """V-身份-04 的库侧一半：专用授权失效 → 没有可用快照 → 不写半条资料。"""
        employed = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False)

        decision = self._handle("ou_zhang", employed)

        self.assertIs(decision.outcome, FirstContactOutcome.DIRECTORY_UNAVAILABLE)
        self.assertEqual(self.users.count(), 0)

    def test_the_delegated_subject_never_gets_a_record_even_if_it_is_in_the_snapshot(self) -> None:
        subject = member(member_key=DELEGATED_SUBJECT, open_id=DELEGATED_SUBJECT, user_id="user_delegated", union_id="union_delegated", display_name="专用授权账号")
        self.snapshots.commit_batch(batch((member(), subject)), source_app_id="cli_fake")
        employed = EmploymentStatus(is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False)

        decision = self._handle(DELEGATED_SUBJECT, employed)

        self.assertIs(decision.outcome, FirstContactOutcome.DELEGATED_SUBJECT_IGNORED)
        self.assertEqual(self.users.count(), 0)


if __name__ == "__main__":
    unittest.main()
