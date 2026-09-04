"""#16 身份链路的真实 PostgreSQL 断言；不访问飞书、不使用真实凭据。

认领断言：V-开通-01、V-开通-06（真库部分）、V-开通-15、V-开通-16、V-身份-01、V-身份-02、V-身份-03、
V-身份-11（首次建立的反向 CAS，真库部分）。
另含「完整性校验不过不提交半轮快照」这条硬规则的真库负向测试。

Issue #89 S-B-03 追加 `AppUserProvisioningContractTest`：写侧建档服务合同
（`core.identity.provisioning`）在真库上的那一半——CHECK 与专用主体触发器**真的**会
拒绝、拒绝后零行残留且不破坏既有档案、同一 `open_id` 重复建档幂等返回。合同本身的
分类规则在 `tests/test_identity_provisioning_contract.py`（无需数据库）。

缺 ``LINGXI_POSTGRES_DSN`` 时整类跳过并说明原因，不静默通过——数据库约束类
断言只能在真库上验证（验证与门禁第五节）。
"""

from __future__ import annotations

import dataclasses
import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from postgres_schema import psycopg_available, reset_production_rows

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
from lingxi.core.identity.provisioning import (
    ProvisioningOutcome,
    ProvisioningRejection,
    ProvisioningRequest,
)

FAKE_TOKEN = "fake-refresh-token-for-tests-only"
DELEGATED_SUBJECT = "ou_delegated_authorization_subject"
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，数据库约束类断言未验证（需真实 PostgreSQL）"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，数据库约束类断言未验证"
)


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


def batch(
    members: tuple[SnapshotMember, ...], *, app_keys: frozenset[str] | None = None
) -> SnapshotBatch:
    keys = frozenset(item.member_key for item in members)
    return SnapshotBatch(
        tenants=(TenantScope("tenant_a", True, keys if app_keys is None else app_keys, keys),),
        departments=(SnapshotDepartment("tenant_a", "dept_a", "测试部门"),),
        members=members,
    )


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
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
        self.vault = HostFileDelegatedCredentialVault(
            self._dsn, Fernet.generate_key().decode(), str(self.path)
        )
        self.issued_at = datetime.now(UTC)

    def _save(
        self,
        *,
        seconds: int = 7 * 24 * 3600,
        token: str = FAKE_TOKEN,
        issued_at: datetime | None = None,
    ) -> None:
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(token), seconds, "offline_access"),
            issued_at=issued_at or self.issued_at,
        )

    def test_registered_subject_open_id_reads_the_registered_subject(self) -> None:
        self._save()

        self.assertEqual(self.vault.registered_subject_open_id(), DELEGATED_SUBJECT)

    def test_registered_subject_open_id_returns_none_when_registration_is_missing(self) -> None:
        self.assertIsNone(self.vault.registered_subject_open_id())

    def test_registered_subject_open_id_reads_a_changed_registration(self) -> None:
        self._save()
        changed_subject = "ou_changed_delegated_subject"
        self.execute(
            "UPDATE feishu_delegated_subject SET subject_open_id = %s",
            (changed_subject,),
        )

        self.assertEqual(self.vault.registered_subject_open_id(), changed_subject)

    def test_ciphertext_lives_only_on_disk_and_the_database_holds_no_credential(self) -> None:
        self._save()

        blob = self.path.read_bytes()
        self.assertNotIn(FAKE_TOKEN.encode(), blob)
        self.assertEqual(
            self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"), DELEGATED_SUBJECT
        )
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
        self.assertEqual(
            self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"),
            "ou_another_subject",
        )

    def test_an_unknown_purpose_is_rejected_by_the_database(self) -> None:
        with self.assertRaises(self._psycopg.errors.CheckViolation):
            self.execute(
                "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES ('employee_token', 'ou_x')"
            )

    def test_the_rotation_point_follows_the_lifetime_returned_by_feishu(self) -> None:
        self._save(seconds=7 * 24 * 3600)

        credential = self.vault.load()

        assert credential is not None
        self.assertAlmostEqual(
            (credential.refresh_at - self.issued_at).total_seconds(), 7 * 24 * 3600 * 0.8, delta=2
        )
        self.assertAlmostEqual(
            (credential.expires_at - self.issued_at).total_seconds(), 7 * 24 * 3600, delta=2
        )

    def test_load_returns_the_plaintext_only_through_an_explicit_reveal(self) -> None:
        self._save()

        credential = self.vault.load()

        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.grant.refresh_token.reveal(), FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, repr(credential))

    def test_claiming_a_due_credential_succeeds_exactly_once(self) -> None:
        # 一次性凭据不能被同一轮扫描领两次；消费标记就是那道门。
        self._save(seconds=3600, issued_at=datetime.now(UTC) - timedelta(seconds=3500))

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

        self._save(seconds=3600, issued_at=datetime.now(UTC) - timedelta(seconds=3500))
        results: list[object] = []

        def worker() -> None:
            results.append(self.vault.claim_due())

        threads = [_threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for item in results if item is not None), 1)

    def test_an_undecryptable_credential_file_is_kept_instead_of_deleted(self) -> None:
        """对抗审查 2026-09-02 C-2：解不开 ≠ 无效，绝不因此删掉密文。

        这条用例此前断言的是相反的行为（"revoked rather than retried"）。改断言
        不是为了迁就实现：登记表**这时有行**，旧实现把解密失败降级成 ``{}`` 之后
        正好命中「文件主体与登记不一致」分支，于是**一次主密钥配错就等于永久销毁
        一次性 refresh_token**——正确密钥换回来也救不回。删是不可逆的，留是可逆的。
        """

        self._save()
        original = self.path.read_bytes()
        self.path.write_bytes(b"\x01\x02broken")

        self.assertIsNone(self.vault.load(), "解不开时不得返回凭据")
        self.assertTrue(self.path.exists(), "解密失败不得删除凭据文件")
        self.assertEqual(self.path.read_bytes(), b"\x01\x02broken", "文件必须原样保留")

        # 领取路径同样不删：轮换扫描每 60 s 跑一次，删一次就没有第二次机会。
        self.assertIsNone(self.vault.claim_due())
        self.assertTrue(self.path.exists(), "claim_due 解密失败同样不得删除")

        # 把正确的密文放回去，凭据仍然可用——这正是"留着"换来的可恢复性。
        self.path.write_bytes(original)
        recovered = self.vault.load()
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.subject_open_id, DELEGATED_SUBJECT)

    def test_a_wrong_master_key_never_destroys_the_credential_file(self) -> None:
        """C-2 的上线场景：scheduler 与 reauthorize 两侧主密钥不一致。

        用**另一把合法 Fernet 密钥**（不是坏字节）读同一份文件——这才是配错密钥的
        真实形状：文件结构完好、只是解不开。原实现会在这里 unlink。
        """

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        self._save()
        ciphertext = self.path.read_bytes()

        wrong_key_vault = HostFileDelegatedCredentialVault(
            self._dsn, Fernet.generate_key().decode(), str(self.path)
        )
        self.assertIsNone(wrong_key_vault.load())
        self.assertIsNone(wrong_key_vault.claim_due())
        self.assertTrue(self.path.exists(), "配错主密钥不得删除凭据文件")
        self.assertEqual(self.path.read_bytes(), ciphertext, "密文必须字节不变")

        # 配对的密钥回来时，同一份文件照常可用。
        self.assertIsNotNone(self.vault.load())

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
        self.vault = HostFileDelegatedCredentialVault(
            self._dsn, Fernet.generate_key().decode(), str(self.path)
        )
        self.issued_at = datetime.now(UTC) - timedelta(days=6)
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
        later = datetime.now(UTC) + timedelta(seconds=3600)
        self.assertIsNone(self.vault.claim_due(now=later))
        self.assertIsNone(self.vault.load(now=later))

    def test_saving_the_replacement_clears_the_consumed_marker(self) -> None:
        self.vault.claim_due()
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(
                SecretToken("fake-next-token"), 7 * 24 * 3600, "offline_access"
            ),
        )

        self.assertIsNotNone(self.vault.load())

    def test_a_stale_consumed_file_is_swept_with_a_distinct_log(self) -> None:
        """进程在续期后、落盘前死掉的形状：旧令牌已作废，收殓而不是留给未来重放。"""
        self.vault.claim_due()

        with self.assertLogs("lingxi.adapters.delegated_credentials", level="ERROR") as captured:
            cleared = self.vault.revoke_stale_consumed(
                max_age_seconds=0, now=datetime.now(UTC) + timedelta(seconds=1)
            )

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


class OnDemandRefreshCeilingTest(IdentityPostgresTestCase):
    """按需续期模式（``claim_due(for_supply=True)``）在真库 + 真文件上的语义。

    日报改成按日取一次派生短期令牌之后，凭据领取多了一条"还没到期也可以领"的路径。
    这条路径与"每 UTC 日至多一次"的上界**捆在同一个开关上**，而上界的判据落在凭据
    文件里、由本模块在自己的文件锁内判定——**这是唯一的一道**：进程内的账本副本不认识
    凭据代际，人工重授权换来的新凭据会被旧账本一直拒到第二天（收口轮 P1）。一次性令牌
    被高频消费正是 2026-08-08 那次事故的形状（AGENTS.md 工作底线）。

    认领断言：``V-身份-03``（凭据仍只以密文落盘、判据不含任何令牌值）、
    ``V-身份-04``（被拒的领取不得让凭据进入不可恢复状态）。
    """

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "delegated-credential.enc"
        self.vault = HostFileDelegatedCredentialVault(
            self._dsn, Fernet.generate_key().decode(), str(self.path)
        )
        # 刚发放：轮换点在 5.6 天之后，因此"到期领取"这条路径拿不到它。
        self.issued_at = datetime.now(UTC)

    def _save(
        self,
        *,
        refresh_consumed_at: datetime | None = None,
        refresh_consumed_count: int | None = None,
        seconds: int = 7 * 24 * 3600,
    ) -> None:
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(FAKE_TOKEN), seconds, "offline_access"),
            issued_at=self.issued_at,
            refresh_consumed_at=refresh_consumed_at,
            refresh_consumed_count=refresh_consumed_count,
        )

    def test_a_credential_that_is_not_due_yet_is_only_claimable_for_supply(self) -> None:
        self._save()

        self.assertIsNone(self.vault.claim_due(), "到期领取这条路径不受影响")
        claimed = self.vault.claim_due(for_supply=True)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.subject_open_id, DELEGATED_SUBJECT)

    def test_the_claim_carries_back_the_moment_generated_inside_the_lock(self) -> None:
        """权威消费时刻由锁内生成并随领取交出（收口轮 P2-a）。

        调用方拿它原样写回新凭据，"哪一天已经消费过"因此只有一个时钟说了算；两处各算
        一次的话，等锁跨过 UTC 午夜就会得到不同的日期。
        """

        self._save()
        before = datetime.now(UTC)

        claimed = self.vault.claim_due(for_supply=True)

        after = datetime.now(UTC)
        self.assertIsNotNone(claimed.consumed_at)
        self.assertGreaterEqual(claimed.consumed_at, before)
        self.assertLessEqual(claimed.consumed_at, after)
        self.assertEqual(claimed.consumed_at.utcoffset(), timedelta(0))

    def test_the_moment_is_taken_after_the_lock_is_acquired(self) -> None:
        """ "现在几点"必须在**拿到锁之后**取（收口轮 P2-a）。

        锁外先算好、再进去等锁，等锁跨过 UTC 午夜时 D+1 的那次领取会被当成 D，当天
        因此可以再消费一次一次性凭据。这里用真实的锁竞争把它钉住：另一个持有者压住锁
        两百毫秒，领取拿到的时刻必须晚于锁被释放的那一刻。
        """

        import fcntl
        import threading

        self._save()
        lock_path = self.path.with_name(self.path.name + ".lock")
        released_at: list[datetime] = []
        holding = threading.Event()

        def hold_the_lock() -> None:
            with open(lock_path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                holding.set()
                time.sleep(0.2)
                released_at.append(datetime.now(UTC))
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        self.addCleanup(holder.join)
        self.assertTrue(holding.wait(timeout=5))

        claimed = self.vault.claim_due(for_supply=True)
        holder.join()

        self.assertIsNotNone(claimed)
        self.assertGreaterEqual(
            claimed.consumed_at, released_at[0], "时刻在锁外取的话会早于锁被释放的那一刻"
        )

    # 一个远离 UTC 午夜的固定锚点：涉及"同一 UTC 日"的用例用它，避免真的在 CI 跑到
    # 接近午夜时产生偶发的跨日误判（Issue #276 新增用例的确定性要求）。
    NOON = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

    def test_a_second_claim_within_the_minimum_interval_is_refused(self) -> None:
        """距上一次消费未满最小间隔（默认 5 分钟）：被拒，且不置位消费标记。

        变异验红锚点：把间隔判定挪到 ``payload["consumed_at"] = ...`` 之后，本用例
        必须变红（`self.vault.load()` 会因为凭据被标成"消费中"而变成 ``None``）。
        """

        from lingxi.core.identity.credentials import RefreshMinIntervalNotElapsedError

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)

        with self.assertRaises(RefreshMinIntervalNotElapsedError) as raised:
            self.vault.claim_due(for_supply=True, now=self.NOON + timedelta(minutes=1))

        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        # 异常带着"是哪一次消费"占用了这次名额，调用方据此判断自己的记号还算不算数。
        self.assertEqual(raised.exception.consumed_at, self.NOON)
        self.assertTrue(self.path.exists(), "被拒的领取不得删除凭据")
        self.assertIsNotNone(self.vault.load(), "被拒的领取不得把凭据标成消费中")

    def test_a_second_claim_after_the_interval_elapses_succeeds(self) -> None:
        """**本次改动的正向锚点**：同一 UTC 日内、间隔已过 ⇒ 第二次换取成功。

        在旧判据「每 UTC 日至多一次」下，这条用例必然失败——那正是本次要推翻的形状
        （Issue #276，产品负责人 2026-08-21 裁定）。默认间隔 5 分钟不用显式传参，
        因此这里也顺带证明了"默认值本身生效"（约束 3）。
        """

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)

        claimed = self.vault.claim_due(
            for_supply=True, now=self.NOON + timedelta(minutes=5, seconds=1)
        )

        self.assertIsNotNone(claimed, "同一天、间隔已过，必须能再换一次")
        self.assertEqual(claimed.refresh_consumed_count, 2, "当日计数在上一次的基础上加一")

    def test_the_ceiling_cannot_be_disabled_by_a_sentinel_value(self) -> None:
        """**约束 4 的直接钉子**：``for_supply=True`` 不接受能让检查整体消失的哨兵值。

        两个新参数只能调整门槛的大小，不能传一个值把检查关掉——否则"放开到期判定、
        同时施加频率上界"这两件事就被参数拆开了，而 docstring 明确说了这是不允许的。
        """

        self._save()

        # **0 也是哨兵**（冻结候选审查 2026-08-21 的 F5）："至少隔 0" 对任何时刻都
        # 成立，等于把这道上界整体关掉。此前的校验写的是 `< timedelta(0)`，只挡住
        # 负数，与 docstring 承诺的"不接受 None、0 或更小"不符。
        # 变异验红：把 `delegated_credentials.py` 里的 `<= timedelta(0)` 改回
        # `< timedelta(0)` 之后重跑本用例，`timedelta(0)` 那一条子用例会红。
        for disabled_interval in (timedelta(0), timedelta(seconds=-1)):
            with self.subTest(min_interval=disabled_interval):
                with self.assertRaises(ValueError):
                    self.vault.claim_due(for_supply=True, min_interval=disabled_interval)
        for disabled in (0, -1):
            with self.subTest(daily_limit=disabled):
                with self.assertRaises(ValueError):
                    self.vault.claim_due(for_supply=True, daily_limit=disabled)

    def test_a_tiny_positive_interval_is_still_a_real_check(self) -> None:
        """0 被拒之后，"几乎没有间隔"的语义由一个**极小的正值**承担——它仍然是一道
        真的检查（同一时刻再领一次照样被挡），只是门槛小到不干扰别的断言。"""

        from lingxi.core.identity.credentials import RefreshMinIntervalNotElapsedError

        tiny = timedelta(microseconds=1)
        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)

        with self.assertRaises(RefreshMinIntervalNotElapsedError):
            # 与上一次消费**同一时刻**：间隔为 0，仍然小于 1 微秒，照样被挡。
            self.vault.claim_due(for_supply=True, now=self.NOON, min_interval=tiny)

    def test_the_daily_limit_is_reached_with_a_distinct_reason(self) -> None:
        """当日已达上界：抛出「日上界」，与「最小间隔」不是同一个 reason（约束 2）。

        间隔已过（+1 小时，不撞最小间隔）但当日已经用满注入的上界（1），因此这里
        钉住的只是"日上界"这一条判据，不与最小间隔混在一起。
        """

        from lingxi.core.identity.credentials import RefreshDailyLimitReachedError

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)

        with self.assertRaises(RefreshDailyLimitReachedError) as raised:
            self.vault.claim_due(for_supply=True, now=self.NOON + timedelta(hours=1), daily_limit=1)

        self.assertNotIn(FAKE_TOKEN, str(raised.exception))
        self.assertEqual(raised.exception.consumed_at, self.NOON)

    def test_the_two_ceilings_are_distinguishable_on_the_same_credential(self) -> None:
        """同一条领取路径下，最小间隔与当日上界必须能被明确区分（约束 2 的直接钉子）。"""

        from lingxi.core.identity.credentials import (
            RefreshDailyLimitReachedError,
            RefreshMinIntervalNotElapsedError,
        )

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)

        with self.assertRaises(RefreshMinIntervalNotElapsedError):
            self.vault.claim_due(
                for_supply=True, now=self.NOON + timedelta(seconds=1), daily_limit=1
            )

        with self.assertRaises(RefreshDailyLimitReachedError):
            self.vault.claim_due(for_supply=True, now=self.NOON + timedelta(hours=1), daily_limit=1)

    def test_the_ceiling_resets_across_utc_midnight_judged_by_the_locks_clock(self) -> None:
        """**跨 UTC 日界**：当日计数归零，且用锁内的当前时刻判定（收口轮 P2-a 的延伸）。

        用一个很低的当日上界（1）把"计数复位"从"间隔已过"里剥离出来单独钉住：不这样
        隔离的话，任何足以跨过午夜的时间差也早已足以满足默认 5 分钟的最小间隔，无法
        证明复位真的是靠"日界"而不是单纯"隔了很久"。
        """

        from lingxi.core.identity.credentials import RefreshDailyLimitReachedError

        near_midnight = datetime(2026, 8, 18, 23, 59, 30, tzinfo=UTC)
        self._save(refresh_consumed_at=near_midnight, refresh_consumed_count=1)

        # 6 分钟之后（已过最小间隔）、仍是 8-19 的 00:05:30——已经跨过 UTC 午夜，
        # 但故意先验证"跨天之后同一个低上界照常放行"，再验证"没跨天则会被挡住"。
        with self.assertRaises(RefreshDailyLimitReachedError):
            # 20 秒之后：还没过最小间隔，也还是同一个 UTC 日，用日上界为 1 的当日
            # 计数（已是 1）钉住"没跨天时会被挡住"这一半。
            # 最小间隔用一个**极小的正值**（不是 0——0 是被拒的哨兵，见
            # `test_the_ceiling_cannot_be_disabled_by_a_sentinel_value`）：这里要隔离
            # 的是"日界有没有让计数复位"，不希望最小间隔抢先把这次领取挡下来，
            # 20 秒已经远超 1 微秒，因此抛出来的必然是日上界那一条。
            self.vault.claim_due(
                for_supply=True,
                now=near_midnight + timedelta(seconds=20),
                daily_limit=1,
                min_interval=timedelta(microseconds=1),
            )

        claimed = self.vault.claim_due(
            for_supply=True, now=near_midnight + timedelta(minutes=6), daily_limit=1
        )
        self.assertIsNotNone(claimed, "跨过 UTC 午夜之后是新的一天，当日计数归零")
        self.assertEqual(claimed.refresh_consumed_count, 1, "新一天的计数从 1 开始")

    def test_a_freshly_authorized_credential_resets_both_ceilings(self) -> None:
        """**收口轮 P1 在凭据层的那一半，延伸到日上界**：新授权不带消费标记与计数，
        因此当天立刻可用——即使旧账本此刻正卡在一个很紧的日上界上。

        刚做完人工重新授权就被上界挡住，会让运维在恢复之后还要再等；这也正是进程内
        账本副本做不到的事——它只记得"已经用过、用了几次"，认不出这是一条新凭据。
        """

        from lingxi.core.identity.credentials import RefreshDailyLimitReachedError

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)
        with self.assertRaises(RefreshDailyLimitReachedError):
            # 用一个紧上界（1）确认"消费过一次"确实卡住了后续领取，不是因为间隔。
            self.vault.claim_due(for_supply=True, now=self.NOON + timedelta(hours=1), daily_limit=1)

        # 人工重授权：同一天写入一条全新凭据，**不带**消费时刻与计数。
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(
                SecretToken("fake-reauthorized-token"), 7 * 24 * 3600, "offline_access"
            ),
        )

        claimed = self.vault.claim_due(
            for_supply=True, now=self.NOON + timedelta(hours=1), daily_limit=1
        )
        self.assertIsNotNone(claimed, "重授权当天即可恢复供给，即使日上界仍然是 1")
        self.assertEqual(
            claimed.refresh_consumed_count, 1, "新凭据的当日计数从 1 开始，不是延续旧账本"
        )

    def test_an_expired_credential_is_revoked_before_the_ceiling_is_consulted(self) -> None:
        """失效优先于一切：一条已经失效的凭据要被清掉并要求重新授权，
        而不是先报一句频率上界的拒绝把真正的问题盖住。"""

        from lingxi.core.identity.credentials import RefreshRateLimitedError

        moment = datetime.now(UTC)
        self._save(refresh_consumed_at=moment, refresh_consumed_count=1, seconds=3600)

        later = moment + timedelta(seconds=3601)
        try:
            claimed = self.vault.claim_due(for_supply=True, now=later)
        except RefreshRateLimitedError:  # pragma: no cover - 次序错了才会走到这里
            self.fail("失效判定必须排在频率上界之前")

        self.assertIsNone(claimed)
        self.assertFalse(self.path.exists())

    def test_the_consumption_moment_and_count_survive_a_rotation_write(self) -> None:
        """轮换写回时把领取时拿到的时刻**与计数**一起落盘：上界因此跟着最新那一代走。

        这里显式把 ``claimed.refresh_consumed_count`` 传回 ``save()``——不传的话
        （见下面 ``test_a_caller_that_forgets_to_thread_the_count_resets_it_silently``）
        当日计数会被这次写入悄悄清零，日上界因此形同虚设。
        """

        from lingxi.core.identity.credentials import RefreshDailyLimitReachedError

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=1)
        claimed = self.vault.claim_due(
            for_supply=True, now=self.NOON + timedelta(hours=1), daily_limit=2
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.refresh_consumed_count, 2)

        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(
                SecretToken("fake-next-token"), 7 * 24 * 3600, "offline_access"
            ),
            replacing_generation=claimed.generation,
            expected_registered_subject_open_id=DELEGATED_SUBJECT,
            refresh_consumed_at=claimed.consumed_at,
            refresh_consumed_count=claimed.refresh_consumed_count,
        )

        with self.assertRaises(RefreshDailyLimitReachedError) as raised:
            # 当日上界仍是 2、已经用掉 2 次：第三次在同一天必须被挡住。
            self.vault.claim_due(for_supply=True, now=self.NOON + timedelta(hours=2), daily_limit=2)
        self.assertEqual(raised.exception.consumed_at, claimed.consumed_at)

        self.assertIsNotNone(
            self.vault.claim_due(
                for_supply=True, now=claimed.consumed_at + timedelta(days=1), daily_limit=2
            ),
            "第二天照常领得到，计数归零",
        )

    def test_a_caller_that_forgets_to_thread_the_count_resets_it_silently(self) -> None:
        """**Issue #276 最容易踩的坑，直接钉住**：``save`` 每次都重建整份 payload，
        新增的 ``refresh_consumed_count`` 不显式传入就会被清零——且不会有任何东西报错。

        这里刻意**不传** ``refresh_consumed_count`` 来复现这个形状：同一天、同一个紧
        上界（1）本该继续挡住第二次领取，但因为落盘时计数被悄悄清空，反而又能领到。
        这不是本次改动想要的行为，而是证明"忘记串参数"这个坑真实存在、且必须靠调用方
        （`credential_rotation.py`）显式传参堵住——生产代码那一侧的对应钉子在
        ``tests/test_roster_access_token_supply.py`` 里对 ``vault.saved[...]
        ["refresh_consumed_count"]`` 的断言。
        """

        self._save()  # 全新凭据，尚未消费过
        claimed = self.vault.claim_due(for_supply=True, now=self.NOON)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.refresh_consumed_count, 1)

        # 故意漏传 refresh_consumed_count。
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(
                SecretToken("fake-next-token"), 7 * 24 * 3600, "offline_access"
            ),
            replacing_generation=claimed.generation,
            expected_registered_subject_open_id=DELEGATED_SUBJECT,
            refresh_consumed_at=claimed.consumed_at,
        )

        # 当日上界是 1、理应已经用满；但计数被上面那次 save() 悄悄清零，因此这里
        # 反而领得到——这正是"字段没串进 save()"这个坑的可观察后果。
        reclaimed = self.vault.claim_due(
            for_supply=True, now=claimed.consumed_at + timedelta(hours=1), daily_limit=1
        )
        self.assertIsNotNone(reclaimed, "计数被悄悄清零，紧上界因此形同虚设")

    def test_a_legacy_payload_without_the_count_field_is_treated_as_not_yet_consumed_today(
        self,
    ) -> None:
        """向后兼容：不含 ``refresh_consumed_count`` 字段的旧凭据文件正常加载，且按
        "当日尚未消费"处理（Issue #276 之前落盘的凭据、以及 ``biai-stage`` 上现存的
        那一份真实凭据都是这个形状）。

        ``refresh_consumed_at`` 特意设在间隔之外（1 小时前），只让"计数缺字段"这一个
        维度参与判定，不与最小间隔混在一起。
        """

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=None)

        claimed = self.vault.claim_due(
            for_supply=True, now=self.NOON + timedelta(hours=1), daily_limit=1
        )

        self.assertIsNotNone(claimed, "缺失的计数字段必须按 0 处理，不能把日上界误判为已达")
        self.assertEqual(claimed.refresh_consumed_count, 1)

    def test_the_credential_file_still_carries_no_token_in_plaintext(self) -> None:
        """`V-身份-03`：新增的判据是时刻与计数，不是凭据；文件依然整体是密文。"""

        self._save(refresh_consumed_at=self.NOON, refresh_consumed_count=3)

        blob = self.path.read_bytes()
        self.assertNotIn(FAKE_TOKEN.encode(), blob)
        self.assertNotIn(b"refresh_consumed_at", blob)
        self.assertNotIn(b"refresh_consumed_count", blob)


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
        self.vault = HostFileDelegatedCredentialVault(
            self._dsn, Fernet.generate_key().decode(), str(self.path)
        )
        self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken(FAKE_TOKEN), 3600, ""),
            issued_at=datetime.now(UTC) - timedelta(seconds=3500),
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
        self.execute("UPDATE feishu_delegated_subject SET subject_open_id = 'ou_new_subject_b'")

        with self.assertLogs("lingxi.adapters.delegated_credentials", level="ERROR") as captured:
            credential = self.vault.load()

        self.assertIsNone(credential)
        self.assertFalse(self.path.exists())
        self.assertTrue(any("不一致" in line for line in captured.output))

    def test_save_cas_rejects_a_changed_registered_subject_without_overwriting_it(self) -> None:
        self.vault.revoke(reason="reset")
        changed_subject = "ou_new_subject_b"
        self.execute(
            "UPDATE feishu_delegated_subject SET subject_open_id = %s",
            (changed_subject,),
        )

        saved = self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(SecretToken("fake-stale-save"), 7 * 24 * 3600, ""),
            expected_registered_subject_open_id=DELEGATED_SUBJECT,
        )

        self.assertFalse(saved)
        self.assertEqual(
            self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"), changed_subject
        )
        self.assertFalse(self.path.exists())


class SubjectBootstrapCasTest(IdentityPostgresTestCase):
    """Issue #137：首次建立专用授权主体的反向 CAS（登记为空才允许，V-身份-11）。

    这条判定必须由真库证明：``ON CONFLICT DO NOTHING`` 在已有登记时返回零行，
    是数据库在同一事务里做的判断，不是应用先读后写。

    **只有真库判定留在这里。** 同一断言里「两种 CAS 不能同时传」那一条是
    ``save()`` 开头的纯内存 ``ValueError``，与数据库无关，挂在这个门控下等于在
    没有数据库的机器上从来不跑；它已搬到
    ``tests/test_identity_credentials.py::VaultSaveArgumentGuardTest``（#215）。
    """

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        from cryptography.fernet import Fernet

        from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "delegated-credential.enc"
        self.vault = HostFileDelegatedCredentialVault(
            self._dsn, Fernet.generate_key().decode(), str(self.path)
        )

    def _bootstrap(self, subject: str = DELEGATED_SUBJECT, *, token: str = FAKE_TOKEN) -> bool:
        return self.vault.save(
            subject_open_id=subject,
            grant=AuthorizationGrant(SecretToken(token), 7 * 24 * 3600, "offline_access"),
            require_absent_registration=True,
        )

    def test_an_empty_registry_accepts_exactly_one_bootstrap(self) -> None:
        self.assertTrue(self._bootstrap())

        self.assertEqual(
            self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"), DELEGATED_SUBJECT
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_delegated_subject"), 1)
        credential = self.vault.load()
        assert credential is not None
        self.assertEqual(credential.subject_open_id, DELEGATED_SUBJECT)

    def test_an_existing_registration_is_never_overwritten_by_a_bootstrap(self) -> None:
        self.assertTrue(self._bootstrap())
        configured_at = self.scalar("SELECT configured_at FROM feishu_delegated_subject")

        second = self._bootstrap("ou_another_bootstrap_subject", token="fake-second-token")

        self.assertFalse(second)
        self.assertEqual(
            self.scalar("SELECT subject_open_id FROM feishu_delegated_subject"), DELEGATED_SUBJECT
        )
        self.assertEqual(
            self.scalar("SELECT configured_at FROM feishu_delegated_subject"), configured_at
        )
        credential = self.vault.load()
        assert credential is not None
        self.assertEqual(
            credential.grant.refresh_token.reveal(), FAKE_TOKEN, "被拒的首次建立不得改写凭据文件"
        )

    def test_bootstrapping_the_same_subject_twice_is_still_refused(self) -> None:
        self.assertTrue(self._bootstrap())

        self.assertFalse(self._bootstrap(token="fake-repeat-token"))

    def test_renewal_takes_over_after_a_bootstrap(self) -> None:
        self.assertTrue(self._bootstrap())

        renewed = self.vault.save(
            subject_open_id=DELEGATED_SUBJECT,
            grant=AuthorizationGrant(
                SecretToken("fake-renewed-token"), 7 * 24 * 3600, "offline_access"
            ),
            expected_registered_subject_open_id=DELEGATED_SUBJECT,
        )

        self.assertTrue(renewed)
        credential = self.vault.load()
        assert credential is not None
        self.assertEqual(credential.grant.refresh_token.reveal(), "fake-renewed-token")

    def test_bootstrapping_an_employee_open_id_is_still_rejected_by_the_database(self) -> None:
        """V-身份-02 的反向触发器对首次建立同样有效：错误主体进不了登记。"""

        self.execute(
            """INSERT INTO app_user (id, feishu_open_id, feishu_user_id, feishu_union_id,
                                     display_name, department, tenant_key)
               VALUES ('usr_bootstrap_guard', 'ou_employee_bootstrap', 'u_b', 'un_b', '某员工', '部门', 't_a')"""
        )

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self._bootstrap("ou_employee_bootstrap")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_delegated_subject"), 0)


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
                    with (
                        connect(self._dsn) as connection_two,
                        connection_two.cursor() as cursor_two,
                    ):
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
        now = datetime.now(UTC)
        newer = store.commit_batch(batch((member(),)), source_app_id="cli_fake", started_at=now)

        older = store.commit_batch(
            batch((member(),)), source_app_id="cli_fake", started_at=now - timedelta(hours=1)
        )

        self.assertEqual(
            self.scalar("SELECT status FROM feishu_org_sync_run WHERE id = %s", (newer,)),
            "complete",
        )
        self.assertEqual(
            self.scalar("SELECT status FROM feishu_org_sync_run WHERE id = %s", (older,)),
            "superseded",
        )


class OrgSnapshotTest(IdentityPostgresTestCase):
    """完整性校验不过就不提交半轮快照——这里是它的真库负向测试。"""

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresOrgSnapshotStore

        self.store = PostgresOrgSnapshotStore(self._dsn)

    def test_a_complete_batch_is_written_in_one_go(self) -> None:
        run_id = self.store.commit_batch(batch((member(),)), source_app_id="cli_fake")

        self.assertEqual(
            self.scalar("SELECT status FROM feishu_org_sync_run WHERE id = %s", (run_id,)),
            "complete",
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_member_snapshot"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_tenant_snapshot"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_department_snapshot"), 1)

    def test_an_incomplete_batch_leaves_no_member_row_at_all(self) -> None:
        broken = batch((member(),), app_keys=frozenset({"ou_zhang", "ou_only_visible_to_app"}))

        with self.assertRaises(SnapshotIntegrityError):
            self.store.commit_batch(broken, source_app_id="cli_fake")

        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_member_snapshot"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM feishu_org_tenant_snapshot"), 0)
        self.assertEqual(
            self.scalar("SELECT count(*) FROM feishu_org_sync_run WHERE status = 'complete'"), 0
        )
        self.assertEqual(self.scalar("SELECT status FROM feishu_org_sync_run"), "failed")

    def test_a_failed_batch_never_becomes_the_source_of_a_location(self) -> None:
        with self.assertRaises(SnapshotIntegrityError):
            self.store.commit_batch(batch((member(open_id="  "),)), source_app_id="cli_fake")

        lookup = self.store.lookup("ou_zhang")

        self.assertIs(lookup.availability, DirectoryAvailability.UNAVAILABLE)
        self.assertEqual(lookup.members, ())

    def test_a_lookup_by_user_id_finds_the_member_in_the_latest_complete_snapshot(self) -> None:
        """Issue #541 预开通：邮箱 → 花名册 ``personnel_id``（＝飞书 ``user_id``）
        → 组织快照成员，是**只有预开通需要**的反方向定位。"""

        self.store.commit_batch(batch((member(),)), source_app_id="cli_fake")

        lookup = self.store.lookup_by_user_id("user_zhang")

        self.assertIs(lookup.availability, DirectoryAvailability.AVAILABLE)
        self.assertEqual([m.open_id for m in lookup.members], ["ou_zhang"])

    def test_a_lookup_by_user_id_returns_no_member_for_an_unknown_id(self) -> None:
        self.store.commit_batch(batch((member(),)), source_app_id="cli_fake")

        lookup = self.store.lookup_by_user_id("user_nobody")

        self.assertIs(lookup.availability, DirectoryAvailability.AVAILABLE)
        self.assertEqual(lookup.members, ())

    def test_a_lookup_by_user_id_returns_every_candidate_when_the_id_is_reused(self) -> None:
        """``feishu_org_member_snapshot`` 对 ``user_id`` **不设唯一约束**（账号复用
        换人按 #34 方案 C 留给管理员侧审计）。查询必须如实返回多条，由调用方失败
        关闭——预开通那一侧就是这么做的（``locate_by_email``）。"""

        self.store.commit_batch(
            batch(
                (
                    member(),
                    member(member_key="ou_zhang_2", open_id="ou_zhang_2", union_id="union_zhang_2"),
                )
            ),
            source_app_id="cli_fake",
        )

        lookup = self.store.lookup_by_user_id("user_zhang")

        self.assertEqual(sorted(m.open_id for m in lookup.members), ["ou_zhang", "ou_zhang_2"])

    def test_a_failed_batch_is_never_the_source_of_a_user_id_lookup_either(self) -> None:
        with self.assertRaises(SnapshotIntegrityError):
            self.store.commit_batch(batch((member(open_id="  "),)), source_app_id="cli_fake")

        lookup = self.store.lookup_by_user_id("user_zhang")

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

        self.execute(
            "UPDATE feishu_org_sync_run SET expires_at = now() + interval '900 days' WHERE id = 'orgsync_expiry'"
        )
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

        for forbidden in (
            "status",
            "is_activated",
            "is_exited",
            "is_frozen",
            "is_resigned",
            "is_unjoin",
        ):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_a_member_row_missing_an_identity_field_is_refused_by_the_database(self) -> None:
        self.execute(
            "INSERT INTO feishu_org_sync_run (id, source_app_id, status, expires_at) "
            "VALUES ('orgsync_guard', 'cli_fake', 'staging', now())"
        )
        for column in ("open_id", "user_id", "union_id", "display_name"):
            with self.subTest(column=column):
                values = {
                    "open_id": "ou_x",
                    "user_id": "user_x",
                    "union_id": "union_x",
                    "display_name": "张一",
                }
                values[column] = "   "
                with self.assertRaises(self._psycopg.errors.CheckViolation):
                    self.execute(
                        "INSERT INTO feishu_org_member_snapshot "
                        "(id, sync_run_id, tenant_key, member_key, open_id, user_id, union_id, display_name) "
                        "VALUES ('member_x', 'orgsync_guard', 'tenant_a', 'ou_x', %s, %s, %s, %s)",
                        (
                            values["open_id"],
                            values["user_id"],
                            values["union_id"],
                            values["display_name"],
                        ),
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
            started_at=datetime.now(UTC) - timedelta(days=91),
        )

        lookup = self.store.lookup("ou_zhang")

        self.assertIs(lookup.availability, DirectoryAvailability.STALE)
        self.assertEqual(lookup.members, ())

    def test_an_unknown_open_id_yields_no_candidate_without_falling_back_to_a_name(self) -> None:
        self.store.commit_batch(batch((member(),)), source_app_id="cli_fake")

        self.assertEqual(self.store.lookup("ou_absent").members, ())
        self.assertEqual(self.store.lookup("ou_zha").members, ())

    def test_has_complete_run_on_reflects_a_persisted_watermark(self) -> None:
        """F8：当日水位必须能对进程重启保持——真库上验证 ``has_complete_run_on``
        只认 ``started_at`` 落在查询那个 UTC 日历日、且状态是 ``complete`` 的批次。"""

        today = datetime.now(UTC)
        self.assertFalse(self.store.has_complete_run_on(today.date()), "还没提交过任何批次")

        self.store.commit_batch(batch((member(),)), source_app_id="cli_fake", started_at=today)

        self.assertTrue(self.store.has_complete_run_on(today.date()))
        self.assertFalse(
            self.store.has_complete_run_on((today - timedelta(days=1)).date()),
            "只认真正落在那一天的批次，不能因为“有过完成批次”就对任何日期都返回真",
        )

    def test_has_complete_run_on_ignores_failed_runs(self) -> None:
        with self.assertRaises(SnapshotIntegrityError):
            self.store.commit_batch(batch((member(open_id="  "),)), source_app_id="cli_fake")

        self.assertFalse(
            self.store.has_complete_run_on(datetime.now(UTC).date()),
            "失败批次不能让水位查询误判成“今天已经成功过一轮”",
        )


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
            employment=EmploymentStatus(
                is_activated=True,
                is_exited=False,
                is_frozen=False,
                is_resigned=False,
                is_unjoin=False,
            ),
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

        self.assertEqual(
            (written.employee_no, written.email), ("00080001", "Roster.User@Example-Corp.invalid")
        )
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(
            (loaded.employee_no, loaded.email), ("00080001", "Roster.User@Example-Corp.invalid")
        )
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
        self.assertEqual(
            stored, [(draft.display_name, "00080002", "baseline.user@example-corp.invalid")]
        )
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

        draft = dataclasses.replace(
            self._draft(), employee_no="00080003", email="blank.field@example.invalid"
        )
        with self.assertRaises(IdentityStorageIntegrityError) as raised:
            self.users.record_identity(draft)

        self.assertIn("employee_no", str(raised.exception))
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM app_user WHERE feishu_open_id = %s", (draft.feishu_open_id,)
            ),
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

        draft = dataclasses.replace(
            self._draft(), employee_no="00080004", email="Rewrite.Field@Example.invalid"
        )
        with self.assertRaises(IdentityStorageIntegrityError) as raised:
            self.users.record_identity(draft)

        self.assertIn("email", str(raised.exception))
        self.assertEqual(
            self.scalar(
                "SELECT count(*) FROM app_user WHERE feishu_open_id = %s", (draft.feishu_open_id,)
            ),
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
        self.execute(
            "UPDATE app_user SET permission_record_id = 'rec_matched', permission_version = 3"
        )

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
                        (
                            values["feishu_user_id"],
                            values["feishu_union_id"],
                            values["display_name"],
                            values["tenant_key"],
                        ),
                    )
        self.assertEqual(self.users.count(), 0)

    def test_the_user_record_has_no_employment_status_column(self) -> None:
        columns = {
            name
            for (name,) in self.query(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'app_user'"
            )
        }

        for forbidden in (
            "status",
            "is_activated",
            "is_exited",
            "is_frozen",
            "is_resigned",
            "is_unjoin",
            "employment_status",
        ):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_the_user_record_has_no_credential_column(self) -> None:
        columns = {
            name
            for (name,) in self.query(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'app_user'"
            )
        }

        for forbidden in (
            "refresh_token",
            "access_token",
            "encrypted_refresh_token",
            "authorization_code",
        ):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_two_people_with_the_same_display_name_both_get_a_record(self) -> None:
        """硬约束 3：姓名不是唯一键。"""
        self.users.record_identity(self._draft(display_name="张三"))
        self.users.record_identity(
            self._draft(
                open_id="ou_second",
                member_key="ou_second",
                user_id="user_second",
                union_id="union_second",
                display_name="张三",
            )
        )

        self.assertEqual(self.users.count(), 2)
        self.assertEqual(self.scalar("SELECT count(DISTINCT feishu_user_id) FROM app_user"), 2)

    def test_a_latin_only_name_is_recorded_unchanged(self) -> None:
        """V-开通-08。"""
        self.users.record_identity(
            self._draft(display_name="Alice Smith", display_name_locale="en-US")
        )

        self.assertEqual(self.scalar("SELECT display_name FROM app_user"), "Alice Smith")
        self.assertEqual(self.scalar("SELECT display_name_locale FROM app_user"), "en-US")


class AppUserProvisioningContractTest(IdentityPostgresTestCase):
    """Issue #89 S-B-03：写侧建档服务合同在真库上的那一半。

    这里要证明的不是"分类函数返回了什么"（那在纯逻辑用例里），而是**防线还在**：
    数据库的 CHECK 与专用主体触发器真的会拒绝 `provision()` 这条路径，拒绝之后库里
    零行残留，而重复建档幂等返回同一条。合同的语义正文见
    `src/lingxi/core/identity/provisioning.py`。
    """

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresAppUserStore

        self.users = PostgresAppUserStore(self._dsn)

    def _identity(self, **overrides):
        candidate = member(**overrides)
        located = locate_by_open_id(candidate.open_id, (candidate,))
        decision = decide_first_contact(
            open_id=candidate.open_id,
            location=located,
            employment=EmploymentStatus(
                is_activated=True,
                is_exited=False,
                is_frozen=False,
                is_resigned=False,
                is_unjoin=False,
            ),
            directory=DirectoryAvailability.AVAILABLE,
            delegated_subject_open_id=DELEGATED_SUBJECT,
        )
        assert decision.draft is not None
        return decision.draft

    def _roster_row(self, **overrides) -> dict:
        row = {
            "personnel_id": "user_zhang",
            "employee_no": "00080001",
            "email": "Roster.User@Example-Corp.invalid",
            "name": "张一",
            "record_id": "rec_1",
        }
        row.update(overrides)
        return row

    def _breaking_trigger(self, name: str, body: str) -> None:
        """在 `app_user` 上装一个会破坏写入的触发器，用例结束后拆掉。"""

        drop = f"DROP TRIGGER IF EXISTS {name} ON app_user; DROP FUNCTION IF EXISTS {name}();"
        self.execute(drop)
        self.addCleanup(lambda: self.execute(drop))
        self.execute(
            f"""CREATE FUNCTION {name}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                {body}
            END
            $$;
            CREATE TRIGGER {name}
            BEFORE INSERT OR UPDATE ON app_user
            FOR EACH ROW EXECUTE FUNCTION {name}();"""
        )

    def test_provisioning_the_same_open_id_twice_returns_the_existing_record(self) -> None:
        """幂等语义：重复建档返回「已存在」而不是报错。

        对账扫描会把孤儿事件再交接一次，崩溃点还可能落在「编排已经跑了一半」之后；
        重复建档若报错，一次**已经成功**的建档会被重入判成内部故障。
        """

        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())

        first = self.users.provision(request)
        second = self.users.provision(request)

        self.assertIs(first.outcome, ProvisioningOutcome.CREATED)
        self.assertIs(second.outcome, ProvisioningOutcome.ALREADY_PROVISIONED)
        self.assertTrue(first.provisioned and second.provisioned)
        self.assertEqual(first.app_user_id, second.app_user_id)
        self.assertEqual(self.users.count(), 1)

    def test_concurrent_provisioning_creates_exactly_one_record(self) -> None:
        """并发重投同样只建一条，其余全部拿到「已存在」。"""

        import threading as _threading

        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())
        results: list = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(self.users.provision(request))
            except BaseException as error:  # noqa: BLE001 - 测试只收集
                errors.append(error)

        workers = [_threading.Thread(target=run) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.scalar("SELECT count(*) FROM app_user"), 1)
        self.assertTrue(all(result.provisioned for result in results))
        self.assertEqual(len({result.app_user_id for result in results}), 1)
        self.assertEqual(
            sum(1 for result in results if result.outcome is ProvisioningOutcome.CREATED), 1
        )

    def test_reentry_never_rewinds_the_provisioning_state_or_the_permission_columns(self) -> None:
        """V-开通-01 的重入面：已推进的用户不会被再建一次打回 `matching`。"""

        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())
        self.users.provision(request)
        self.execute(
            "UPDATE app_user SET provisioning_state = 'mcp_syncing', "
            "permission_record_id = 'rec_matched', permission_version = 3"
        )

        again = self.users.provision(request)

        self.assertIs(again.outcome, ProvisioningOutcome.ALREADY_PROVISIONED)
        self.assertEqual(
            self.query(
                "SELECT provisioning_state, permission_record_id, permission_version FROM app_user"
            ),
            [("mcp_syncing", "rec_matched", 3)],
        )

    def test_reentry_never_revives_a_suspended_account(self) -> None:
        """停用中的账号被重入建档，不得复活。

        这是 #65 轻审 P2-2 那个孤儿窗口里的真实竞争：一条"已认领、没确认交接"的事件
        可能在整整一个对账扫描周期之后才被重新交接，而管理员完全可能在这段时间里停用
        这个账号。重入把 `account_state` 写回 `enabled`，等于让建档服务替管理员撤销了
        一次停用，而且没有任何人会知道。
        """

        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())
        self.users.provision(request)
        self.execute("UPDATE app_user SET account_state = 'suspended'")

        again = self.users.provision(request)

        self.assertIs(again.outcome, ProvisioningOutcome.ALREADY_PROVISIONED)
        self.assertTrue(again.provisioned, "重入仍然是「档在」，停用与否由编排层复核")
        self.assertEqual(self.scalar("SELECT account_state FROM app_user"), "suspended")
        self.assertEqual(self.users.count(), 1)

    def test_the_roster_archive_is_written_verbatim(self) -> None:
        """V-开通-15：写侧存的是花名册原值，不是匹配时刻的小写归一值。"""

        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())

        result = self.users.provision(request)
        loaded = self.users.get_by_open_id(self._identity().feishu_open_id)

        self.assertTrue(result.provisioned)
        assert loaded is not None
        self.assertEqual(
            (loaded.employee_no, loaded.email), ("00080001", "Roster.User@Example-Corp.invalid")
        )
        self.assertEqual(
            self.query(
                "SELECT employee_no, email FROM app_user WHERE id = %s", (result.app_user_id,)
            ),
            [("00080001", "Roster.User@Example-Corp.invalid")],
        )

    def test_provisioning_never_writes_a_permission_record(self) -> None:
        """V-开通-01：匹配确认前不占位再回填。"""

        self.users.provision(
            ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())
        )

        self.assertIsNone(self.scalar("SELECT permission_record_id FROM app_user"))
        self.assertEqual(self.scalar("SELECT permission_version FROM app_user"), 0)

    def test_an_incomplete_identity_is_refused_by_the_database_and_leaves_no_row(self) -> None:
        """V-开通-06：残缺资料由数据库的「全有或全无」CHECK 拒绝，写侧不短路它。"""

        for field in (
            "feishu_user_id",
            "feishu_union_id",
            "display_name",
            "department",
            "tenant_key",
        ):
            with self.subTest(field=field):
                request = ProvisioningRequest(
                    dataclasses.replace(self._identity(), **{field: "   "})
                )

                result = self.users.provision(request)

                self.assertIs(result.outcome, ProvisioningOutcome.REJECTED)
                self.assertIs(result.rejection, ProvisioningRejection.INCOMPLETE_IDENTITY)
                self.assertFalse(result.rejection.is_storage_fault)
                self.assertEqual(result.missing_fields, (field,))
                self.assertIsNone(result.app_user_id)
                self.assertEqual(self.users.count(), 0)

    def test_a_request_without_an_open_id_never_reaches_the_table(self) -> None:
        """数据库对「六字段全空」是放行的，这一格由写侧自己守，否则会堆出垃圾档案。"""

        request = ProvisioningRequest(
            dataclasses.replace(
                self._identity(),
                feishu_open_id="  ",
                feishu_user_id="",
                feishu_union_id="",
                display_name="",
                department="",
                tenant_key="",
            )
        )

        result = self.users.provision(request)

        self.assertIs(result.rejection, ProvisioningRejection.INCOMPLETE_IDENTITY)
        self.assertIn("feishu_open_id", result.missing_fields)
        self.assertEqual(self.users.count(), 0)

    def test_a_rejected_provision_does_not_disturb_the_existing_record(self) -> None:
        """拒绝整条回滚：既有档案不会被一次残缺的重入写坏。"""

        good = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())
        created = self.users.provision(good)
        before = self.query("SELECT display_name, department, employee_no, email FROM app_user")

        broken = self.users.provision(
            ProvisioningRequest(dataclasses.replace(self._identity(), department=" "))
        )

        self.assertIs(broken.outcome, ProvisioningOutcome.REJECTED)
        self.assertEqual(self.users.count(), 1)
        self.assertEqual(self.scalar("SELECT id FROM app_user"), created.app_user_id)
        self.assertEqual(
            self.query("SELECT display_name, department, employee_no, email FROM app_user"), before
        )

    def test_the_delegated_subject_is_refused_by_the_trigger_and_leaves_no_row(self) -> None:
        """V-身份-02：写侧路径同样绕不过数据库那一道。"""

        self.execute(
            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES ('org_directory_sync', %s)",
            (DELEGATED_SUBJECT,),
        )
        request = ProvisioningRequest(
            dataclasses.replace(self._identity(), feishu_open_id=DELEGATED_SUBJECT)
        )

        result = self.users.provision(request)

        self.assertIs(result.outcome, ProvisioningOutcome.REJECTED)
        self.assertIs(result.rejection, ProvisioningRejection.DELEGATED_SUBJECT)
        self.assertFalse(result.rejection.is_storage_fault)
        self.assertEqual(result.missing_fields, ())
        self.assertEqual(self.users.count(), 0)

    def test_a_dropped_roster_field_is_reported_as_a_storage_fault(self) -> None:
        """V-开通-15：库把工号吞了要走内部故障出口，不能显示成「没有银河权限」。"""

        self._breaking_trigger(
            "test_i89_provision_drop_employee_no", "NEW.employee_no := NULL; RETURN NEW;"
        )
        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())

        result = self.users.provision(request)

        self.assertIs(result.outcome, ProvisioningOutcome.REJECTED)
        self.assertIs(result.rejection, ProvisioningRejection.STORAGE_INTEGRITY)
        self.assertTrue(result.rejection.is_storage_fault)
        self.assertEqual(self.users.count(), 0)

    def test_an_unrecognised_database_refusal_is_raised_instead_of_being_classified(self) -> None:
        """不认识的拒绝原样抛出：否则它会伪装成一个确定性的业务终态。"""

        self._breaking_trigger(
            "test_i89_provision_unknown_refusal", "RAISE EXCEPTION '别的触发器拒绝'; RETURN NEW;"
        )
        request = ProvisioningRequest.from_roster_row(self._identity(), self._roster_row())

        with self.assertRaises(self._psycopg.errors.RaiseException):
            self.users.provision(request)
        self.assertEqual(self.users.count(), 0)

    def test_the_daily_report_baseline_reads_what_provisioning_wrote(self) -> None:
        """V-开通-16：#52 日报比对的基线就是这条写侧路径落下的三字段。"""

        from lingxi.adapters.postgres_roster_audit import PostgresRosterBaselineReader

        row = self._roster_row()
        self.users.provision(ProvisioningRequest.from_roster_row(self._identity(), row))
        self.execute("UPDATE app_user SET provisioning_state = 'active'")

        baseline = PostgresRosterBaselineReader(self._dsn).load_active_baseline()

        self.assertEqual(len(baseline), 1)
        self.assertEqual(
            (baseline[0].display_name, baseline[0].employee_no, baseline[0].email),
            ("张一", row["employee_no"], row["email"]),
        )


class ProvisioningStateAdvanceTest(IdentityPostgresTestCase):
    """开通状态推进：**只前进不回退**，且写 `active` 还要求账号此刻是启用的。

    两条都写在 `WHERE` 里而不是先读后写（Epic D / S-D-02）：两条并发的开通链会读到
    同一个旧状态，于是后到的那条能把 `active` 写回 `provisioning`——一个已经开通完的
    用户因此重新变成「开通中」，问数被拒。而「先读账号状态、再写 active」之间的窗口，
    后果是把一个刚被管理员停用的人标成「开通完成」。
    """

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresAppUserStore

        self.users = PostgresAppUserStore(self._dsn)
        candidate = member()
        located = locate_by_open_id(candidate.open_id, (candidate,))
        decision = decide_first_contact(
            open_id=candidate.open_id,
            location=located,
            employment=EmploymentStatus(
                is_activated=True,
                is_exited=False,
                is_frozen=False,
                is_resigned=False,
                is_unjoin=False,
            ),
            directory=DirectoryAvailability.AVAILABLE,
            delegated_subject_open_id=DELEGATED_SUBJECT,
        )
        assert decision.draft is not None
        self.user_id = self.users.record_identity(decision.draft).id

    def _state(self) -> str:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT provisioning_state FROM app_user WHERE id = %s", (self.user_id,))
            return str(cursor.fetchone()[0])

    def _suspend(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET account_state = 'suspended' WHERE id = %s", (self.user_id,)
            )

    def test_the_chain_advances_step_by_step(self) -> None:
        status = self.users.read_status(self.user_id)
        assert status is not None
        self.assertEqual((status.account_state, status.provisioning_state), ("enabled", "matching"))

        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="provisioning"))
        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="mcp_syncing"))
        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="active"))
        self.assertEqual(self._state(), "active")

    def test_it_never_goes_backwards(self) -> None:
        self.users.advance_provisioning_state(self.user_id, to="active")

        self.assertFalse(self.users.advance_provisioning_state(self.user_id, to="provisioning"))
        self.assertFalse(self.users.advance_provisioning_state(self.user_id, to="mcp_syncing"))
        self.assertEqual(self._state(), "active", "`V-开通-04`：失败不得把状态打回去")

    def test_a_suspended_account_is_never_written_active(self) -> None:
        self.users.advance_provisioning_state(self.user_id, to="mcp_syncing")
        self._suspend()

        self.assertFalse(self.users.advance_provisioning_state(self.user_id, to="active"))
        self.assertEqual(self._state(), "mcp_syncing")

    def test_a_suspended_account_can_still_be_read_back(self) -> None:
        self._suspend()
        status = self.users.read_status(self.user_id)

        assert status is not None
        self.assertEqual(status.account_state, "suspended")

    def test_an_unknown_state_is_refused_instead_of_treated_as_first(self) -> None:
        """拼错的状态名不能被当成「排在最前面」而把任何用户推成 active。"""

        with self.assertRaises(ValueError):
            self.users.advance_provisioning_state(self.user_id, to="actve")

    def test_a_missing_user_reads_back_as_none(self) -> None:
        self.assertIsNone(self.users.read_status("usr_does_not_exist"))

    # ---- 迁移 0087：预开通的两处接缝 ---------------------------------

    def _started_at(self):
        return self.scalar(
            "SELECT provisioning_started_at FROM app_user WHERE id = %s", (self.user_id,)
        )

    def test_entering_provisioning_stamps_the_lease_origin(self) -> None:
        """Issue #541：``provisioning_started_at`` 是停摆兜底在**没有 ``inbound_event``
        行**时唯一可用的租约起点，必须与"推进到分水岭"同真同假（同一条 UPDATE）。"""

        self.assertIsNone(self._started_at(), "推进之前不该有起点")

        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="provisioning"))

        self.assertIsNotNone(self._started_at())

    def test_a_refused_advance_does_not_stamp_the_lease_origin(self) -> None:
        """空写不能留下一个"这次开通从现在开始"的假事实。"""

        self.users.advance_provisioning_state(self.user_id, to="active")
        self.execute(
            "UPDATE app_user SET provisioning_started_at = NULL WHERE id = %s", (self.user_id,)
        )

        self.assertFalse(self.users.advance_provisioning_state(self.user_id, to="provisioning"))
        self.assertIsNone(self._started_at())

    def test_later_advances_do_not_refresh_the_lease_origin(self) -> None:
        """租约起点只在进入分水岭那一刻写一次：被后续无关推进刷新的列会让租约永远
        不到期，那正是"不用 ``updated_at`` 兜底"的理由。"""

        self.users.advance_provisioning_state(self.user_id, to="provisioning")
        first = self._started_at()

        self.users.advance_provisioning_state(self.user_id, to="mcp_syncing")
        self.users.advance_provisioning_state(self.user_id, to="active")

        self.assertEqual(self._started_at(), first)

    def _armed_at(self):
        return self.scalar(
            "SELECT preprovision_notice_armed_at FROM app_user WHERE id = %s", (self.user_id,)
        )

    def _open_id(self) -> str:
        return str(
            self.scalar("SELECT feishu_open_id FROM app_user WHERE id = %s", (self.user_id,))
        )

    def test_the_first_chat_line_is_armed_once(self) -> None:
        self.assertTrue(self.users.mark_preprovision_notice_pending(open_id=self._open_id()))
        armed = self._armed_at()

        self.assertFalse(
            self.users.mark_preprovision_notice_pending(open_id=self._open_id()),
            "同一份名单重跑必须零变化，不能把同一句话重新挂起一次",
        )
        self.assertEqual(self._armed_at(), armed)

    def test_a_person_already_talking_to_us_is_never_armed(self) -> None:
        """那句解释的全部意义是"你没经历过开通等待"；对一个已经在聊的人说它只会
        莫名其妙。判据放在 SQL 里，因为调用点（开通链的通知出口）看不到入站事件。"""

        self.execute(
            """INSERT INTO inbound_event
                 (feishu_event_id, received_at, event_type, user_open_id, handled_as, trace_id)
               VALUES ('evt_chatty', now(), 'im.message.receive_v1', %s, 'task_queued', 'trc_chatty')""",
            (self._open_id(),),
        )

        self.assertFalse(self.users.mark_preprovision_notice_pending(open_id=self._open_id()))
        self.assertIsNone(self._armed_at())

    def test_arming_an_unknown_open_id_changes_nothing(self) -> None:
        self.assertFalse(self.users.mark_preprovision_notice_pending(open_id="ou_nobody"))


class StalledProvisioningAbortTest(IdentityPostgresTestCase):
    """`abort_stalled_provisioning` 与 `_PROVISIONING_ORDER` 收紧的真库断言
    （Issue #282，`V-开通-19`）。

    这是「当场收口」与停摆扫描职责共用的**唯一**写入方式：条件更新只从调用方明确
    列出的中途格收口，绝不碰 ``active``，绝不碰已停用账号——四条断言都只有真库能
    证伪（``UPDATE ... WHERE`` 谓词的属性）。
    """

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_identity import PostgresAppUserStore

        self.users = PostgresAppUserStore(self._dsn)
        candidate = member()
        located = locate_by_open_id(candidate.open_id, (candidate,))
        decision = decide_first_contact(
            open_id=candidate.open_id,
            location=located,
            employment=EmploymentStatus(
                is_activated=True,
                is_exited=False,
                is_frozen=False,
                is_resigned=False,
                is_unjoin=False,
            ),
            directory=DirectoryAvailability.AVAILABLE,
            delegated_subject_open_id=DELEGATED_SUBJECT,
        )
        assert decision.draft is not None
        self.user_id = self.users.record_identity(decision.draft).id

    def _state(self) -> str:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT provisioning_state FROM app_user WHERE id = %s", (self.user_id,))
            return str(cursor.fetchone()[0])

    def _suspend(self) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET account_state = 'suspended' WHERE id = %s", (self.user_id,)
            )

    def test_provisioning_can_be_aborted(self) -> None:
        self.users.advance_provisioning_state(self.user_id, to="provisioning")

        self.assertTrue(
            self.users.abort_stalled_provisioning(
                user_id=self.user_id,
                expected_states=("provisioning", "mcp_syncing"),
                reason="stalled_lease_expired",
            )
        )
        self.assertEqual(self._state(), "aborted")

    def test_mcp_syncing_can_be_aborted(self) -> None:
        self.users.advance_provisioning_state(self.user_id, to="provisioning")
        self.users.advance_provisioning_state(self.user_id, to="mcp_syncing")

        self.assertTrue(
            self.users.abort_stalled_provisioning(
                user_id=self.user_id,
                expected_states=("provisioning", "mcp_syncing"),
                reason="publish_failed",
            )
        )
        self.assertEqual(self._state(), "aborted")

    def test_an_active_user_is_never_aborted(self) -> None:
        """**否定断言**：绝不把已经成功的人打回失败——这是本方法存在的核心边界。"""

        self.users.advance_provisioning_state(self.user_id, to="provisioning")
        self.users.advance_provisioning_state(self.user_id, to="mcp_syncing")
        self.users.advance_provisioning_state(self.user_id, to="active")

        self.assertFalse(
            self.users.abort_stalled_provisioning(
                user_id=self.user_id,
                expected_states=("provisioning", "mcp_syncing"),
                reason="stalled_lease_expired",
            )
        )
        self.assertEqual(self._state(), "active")

    def test_a_miscalled_expected_states_still_cannot_touch_an_active_user(self) -> None:
        """外部独立审查 P2-3：即使调用方手滑把 `active` 传进 `expected_states`（不该
        发生，但这条防线**不依赖**调用方自觉传对），SQL 里独立的
        `provisioning_state <> 'active'` 仍然必须挡住——安全边界的来源是 SQL 本身，
        不是调用方的自律。"""

        self.users.advance_provisioning_state(self.user_id, to="provisioning")
        self.users.advance_provisioning_state(self.user_id, to="mcp_syncing")
        self.users.advance_provisioning_state(self.user_id, to="active")

        self.assertFalse(
            self.users.abort_stalled_provisioning(
                user_id=self.user_id,
                expected_states=("provisioning", "mcp_syncing", "active"),
                reason="stalled_lease_expired",
            )
        )
        self.assertEqual(self._state(), "active")

    def test_a_suspended_account_is_never_aborted(self) -> None:
        """**否定断言**：已停用账号的中途状态原样保留，交给账号停用流程自己的语义
        处理，不被收口顺手改写。"""

        self.users.advance_provisioning_state(self.user_id, to="provisioning")
        self._suspend()

        self.assertFalse(
            self.users.abort_stalled_provisioning(
                user_id=self.user_id,
                expected_states=("provisioning", "mcp_syncing"),
                reason="stalled_lease_expired",
            )
        )
        self.assertEqual(self._state(), "provisioning")

    def test_a_user_that_never_started_provisioning_is_never_aborted(self) -> None:
        """**否定断言**：不越界收口没起跑的人——`matching`/`guest` 不在
        `expected_states` 允许的中途格里。"""

        self.assertEqual(self._state(), "matching")

        self.assertFalse(
            self.users.abort_stalled_provisioning(
                user_id=self.user_id,
                expected_states=("provisioning", "mcp_syncing"),
                reason="stalled_lease_expired",
            )
        )
        self.assertEqual(self._state(), "matching")

    def test_advance_to_aborted_never_writes_through_advance_provisioning_state(self) -> None:
        """**否定断言**：`_PROVISIONING_ORDER` 加了 `"aborted": 0` 之后，
        `advance_provisioning_state(to="aborted")` 必须仍然返回 `False` 且库里一行
        都不动——证明「只前进不回退」没有被这次改动开出后门，`aborted` 只能由
        `abort_stalled_provisioning` 这个专用入口写入。"""

        self.users.advance_provisioning_state(self.user_id, to="provisioning")

        self.assertFalse(self.users.advance_provisioning_state(self.user_id, to="aborted"))
        self.assertEqual(self._state(), "provisioning")

    def test_aborted_can_advance_all_the_way_back_to_active(self) -> None:
        """收口之后用户的下一条消息触发一条全新的链：`aborted` 必须能重新推进到
        `matching`/`provisioning`/`mcp_syncing`/`active`，否则它是一条死胡同
        （Issue #282「必须一并修」一节）。"""

        self.users.advance_provisioning_state(self.user_id, to="provisioning")
        self.users.abort_stalled_provisioning(
            user_id=self.user_id,
            expected_states=("provisioning", "mcp_syncing"),
            reason="stalled_lease_expired",
        )
        self.assertEqual(self._state(), "aborted")

        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="matching"))
        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="provisioning"))
        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="mcp_syncing"))
        self.assertTrue(self.users.advance_provisioning_state(self.user_id, to="active"))
        self.assertEqual(self._state(), "active")

    def test_expected_states_must_be_given_explicitly(self) -> None:
        with self.assertRaises(ValueError):
            self.users.abort_stalled_provisioning(
                user_id=self.user_id, expected_states=(), reason="stalled_lease_expired"
            )


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
        employed = EmploymentStatus(
            is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False
        )

        for _ in range(3):
            decision = self._handle("ou_zhang", employed)

        self.assertIs(decision.outcome, FirstContactOutcome.RECORD_READY)
        self.assertEqual(self.users.count(), 1)

    def test_a_frozen_member_is_refused_and_nothing_is_written(self) -> None:
        self.snapshots.commit_batch(batch((member(),)), source_app_id="cli_fake")
        frozen = EmploymentStatus(
            is_activated=True, is_exited=False, is_frozen=True, is_resigned=False, is_unjoin=False
        )

        decision = self._handle("ou_zhang", frozen)

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertEqual(self.users.count(), 0)

    def test_an_unlocatable_sender_is_not_authorized_and_nothing_is_written(self) -> None:
        self.snapshots.commit_batch(batch((member(),)), source_app_id="cli_fake")
        employed = EmploymentStatus(
            is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False
        )

        decision = self._handle("ou_absent", employed)

        self.assertIs(decision.outcome, FirstContactOutcome.NOT_AUTHORIZED)
        self.assertEqual(self.users.count(), 0)

    def test_without_any_snapshot_the_sender_gets_a_terminal_state_and_nothing_is_written(
        self,
    ) -> None:
        """V-身份-04 的库侧一半：专用授权失效 → 没有可用快照 → 不写半条资料。"""
        employed = EmploymentStatus(
            is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False
        )

        decision = self._handle("ou_zhang", employed)

        self.assertIs(decision.outcome, FirstContactOutcome.DIRECTORY_UNAVAILABLE)
        self.assertEqual(self.users.count(), 0)

    def test_the_delegated_subject_never_gets_a_record_even_if_it_is_in_the_snapshot(self) -> None:
        subject = member(
            member_key=DELEGATED_SUBJECT,
            open_id=DELEGATED_SUBJECT,
            user_id="user_delegated",
            union_id="union_delegated",
            display_name="专用授权账号",
        )
        self.snapshots.commit_batch(batch((member(), subject)), source_app_id="cli_fake")
        employed = EmploymentStatus(
            is_activated=True, is_exited=False, is_frozen=False, is_resigned=False, is_unjoin=False
        )

        decision = self._handle(DELEGATED_SUBJECT, employed)

        self.assertIs(decision.outcome, FirstContactOutcome.DELEGATED_SUBJECT_IGNORED)
        self.assertEqual(self.users.count(), 0)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class AppUserEmailBindingTest(IdentityPostgresTestCase):
    """rc25 S-2a / 对抗审查 X-1：一个规范化邮箱至多绑一个 ``app_user``。

    两层防线各有一半只能在真库上验证：
    - **迁移 0085 的部分唯一索引**——它是"两个人不可能同时绑同一个邮箱"的结构性
      保证，纯逻辑用例证明不了它真的建出来了、也证明不了它的表达式与
      ``normalize_email`` 同口径；
    - **``PostgresEmailBindingSource`` 的回读**——SQL 的 ``lower(btrim(email))``
      必须与应用层归一化对齐，对不齐的闸等于没有闸。

    编排层"命中即失败关闭、零副作用"的断言在
    ``tests/test_onboarding_runner.EmailAlreadyBoundTests``；这里额外跑一次**真库上
    的整条链**，证明第二个人既不建档、也不排发布意图。
    """

    EMAIL = "Shared.Mailbox@Example-Corp.invalid"
    NORMALIZED = "shared.mailbox@example-corp.invalid"

    def setUp(self) -> None:
        super().setUp()
        from lingxi.adapters.postgres_email_binding import PostgresEmailBindingSource

        self.bindings = PostgresEmailBindingSource(self._dsn)

    def _insert(self, user_id: str, open_id: str, email: str | None) -> None:
        """直插一行 ``app_user``。身份六列必须**全有**（基线 CHECK：``V-开通-06``
        的"不得留下半条记录"），因此这里按 ``open_id`` 派生齐全，不是只填两列。"""

        self.execute(
            """INSERT INTO app_user
                 (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                  department, tenant_key, email)
               VALUES (%s, %s, %s, %s, %s, '测试部门', 'tenant_a', %s)""",
            (user_id, open_id, f"fs_{open_id}", f"un_{open_id}", "共用邮箱用例", email),
        )

    # ---- 数据库侧：部分唯一索引 --------------------------------------

    def test_a_second_row_with_the_same_normalized_email_is_refused(self) -> None:
        """**变异锚点**：删掉迁移 0085 的唯一索引，这条用例必须变红。

        大小写与首尾空白都不构成"不同的邮箱"——正式表行键 ``record_key`` 用的就是
        去空白 + 小写之后的那一份，索引口径必须与它逐字一致。
        """

        self._insert("usr_first", "ou_first", self.EMAIL)
        with self.assertRaises(self._psycopg.errors.UniqueViolation):
            self._insert("usr_second", "ou_second", "  SHARED.MAILBOX@example-corp.invalid ")

    def test_updating_a_row_onto_someone_elses_email_is_refused(self) -> None:
        """建档是 ``ON CONFLICT (feishu_open_id) DO UPDATE``：把某一行的邮箱改成
        别人已经占用的那一个，同样必须被拒——只挡 INSERT 的索引挡不住换绑。"""

        self._insert("usr_first", "ou_first", self.EMAIL)
        self._insert("usr_second", "ou_second", "another@example-corp.invalid")
        with self.assertRaises(self._psycopg.errors.UniqueViolation):
            self.execute("UPDATE app_user SET email = %s WHERE id = 'usr_second'", (self.EMAIL,))

    def test_rows_without_a_usable_email_are_not_constrained(self) -> None:
        """建档不以邮箱为前提（基线：工号与邮箱可空）。``NULL`` 与纯空白都不进索引，
        否则第二个没填邮箱的人就建不了档。"""

        self._insert("usr_a", "ou_a", None)
        self._insert("usr_b", "ou_b", None)
        self._insert("usr_c", "ou_c", "   ")
        self._insert("usr_d", "ou_d", "")
        self.assertEqual(self.scalar("SELECT count(*) FROM app_user"), 4)

    # ---- 适配器侧：回读口径 ------------------------------------------

    def test_the_binding_source_matches_on_the_normalized_email(self) -> None:
        self._insert("usr_first", "ou_first", f"  {self.EMAIL.upper()}  ")

        bound = self.bindings.bindings_for_email(self.NORMALIZED)

        self.assertEqual(
            [(item.user_id, item.feishu_open_id) for item in bound], [("usr_first", "ou_first")]
        )
        self.assertEqual(self.bindings.bindings_for_email("nobody@example-corp.invalid"), ())
        self.assertEqual(self.bindings.bindings_for_email(""), ())

    def test_blank_emails_are_never_returned_as_a_binding(self) -> None:
        """空白邮箱不是"绑定"：它不进索引，也不该让判定层拿它当冲突。"""

        self._insert("usr_blank", "ou_blank", "   ")
        self.assertEqual(self.bindings.bindings_for_email(""), ())
        self.assertEqual(self.bindings.bindings_for_email("   "), ())

    # ---- 编排层：真库上的整条链 --------------------------------------

    def test_a_second_person_sharing_the_email_is_stopped_before_any_write(self) -> None:
        """**变异锚点**：拿掉 ``_run`` 里那道闸，这条用例必须变红。

        真库上跑完整条开通链：库里先有一个**别人**的 ``app_user`` 行占着这个邮箱，
        第二个人首聊进来后必须以 ``LX-ONBOARD-001`` 失败关闭，且 ``app_user``
        零新增行、``publish_outbox`` 零行。
        """

        from test_onboarding_runner import OPEN_ID, ROSTER_ROWS, FakeRoster, run_once

        from lingxi.adapters.postgres_email_binding import PostgresEmailBindingSource
        from lingxi.adapters.postgres_identity import PostgresAppUserStore
        from lingxi.adapters.postgres_permission_publish import PostgresPermissionPublishStore

        roster_email = ROSTER_ROWS[0]["email"]
        self._insert("usr_incumbent", "ou_someone_else", roster_email)

        parts, _ = run_once(
            provisioning=PostgresAppUserStore(self._dsn),
            users=PostgresAppUserStore(self._dsn),
            email_bindings=PostgresEmailBindingSource(self._dsn),
            decisions=PostgresPermissionPublishStore(self._dsn),
            roster=FakeRoster(ROSTER_ROWS),
        )

        result = parts["audit"].facts("onboarding.result")
        self.assertEqual(result["state"], "internal_error")
        self.assertEqual(result["failure_reason"], "email_already_bound")
        self.assertEqual(parts["tokens"].calls, [])
        self.assertEqual(parts["tokens"].adopt_calls, [])
        self.assertEqual(parts["environment"].calls, [])
        # 真库读回：第二个人一行都没建，一条发布意图都没排。
        self.assertEqual(
            self.query("SELECT id, feishu_open_id FROM app_user ORDER BY id"),
            [("usr_incumbent", "ou_someone_else")],
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM publish_outbox"), 0)
        self.assertNotEqual(OPEN_ID, "ou_someone_else")


if __name__ == "__main__":
    unittest.main()
