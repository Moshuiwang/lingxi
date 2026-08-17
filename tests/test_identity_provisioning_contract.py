"""Issue #89 写侧建档服务合同的纯逻辑断言（不需要数据库）。

认领断言：V-开通-01（请求里不存在权限字段）、V-开通-06（残缺身份的原因分类）、
V-开通-15（花名册字段按原值存档、不被归一改写）、V-身份-02（专用主体拒绝的分类标记
与迁移正文一致）。

真库那一半——CHECK 与触发器**真的**会拒绝、拒绝后零行残留、幂等返回同一条——在
`tests/test_identity_postgres_records.py`，按 DSN 门控。两边合起来才是完整证据：
这里证明分类规则本身可证伪，那边证明防线还在。
"""

from __future__ import annotations

import dataclasses
import unittest

from lingxi.core.identity.first_contact import IdentityRecordDraft
from lingxi.core.identity.provisioning import (
    DELEGATED_SUBJECT_REJECTION_MARKER,
    IDENTITY_REQUIRED_FIELDS,
    ROSTER_ARCHIVE_FIELDS,
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_RAISE_EXCEPTION,
    IdentityProvisioning,
    ProvisioningOutcome,
    ProvisioningRejection,
    ProvisioningRequest,
    ProvisioningResult,
    classify_write_failure,
    missing_identity_fields,
)
from lingxi.core.identity.roster_audit import ArchivedIdentity, compare_roster
from lingxi.core.permission.account_match import match_galaxy_account

from postgres_schema import MIGRATIONS_DIRECTORY, VERSIONS_DIRECTORY

# `app_user` 的两条迁移血统正文：冻结的顶层编号 SQL 与 alembic 基线 revision
# （代码框架第六节：基线是编号文件的逐字节副本）。**按内容筛选而不是写死文件名**——
# 硬编码生产迁移文件名会被 `tests/test_postgres_schema_fixture.py` 的守卫拦下，
# 那条守卫防的正是"真库用例挑几个文件建库"，而这里做的是源码一致性核对。
_APP_USER_TABLE_MARKER = "CREATE TABLE app_user"
# 「全有或全无」CHECK 的起点注释；它到最近一个 `\n);` 之间就是那条约束的正文。
_ALL_OR_NOTHING_MARKER = "-- 定位失败或资料不完整时不得留下半条记录"


def app_user_migration_lineages() -> tuple[tuple[str, str], ...]:
    sources = tuple(
        (path.name, path.read_text(encoding="utf-8"))
        for path in [*sorted(MIGRATIONS_DIRECTORY.glob("*.sql")), *sorted(VERSIONS_DIRECTORY.glob("*.py"))]
        if _APP_USER_TABLE_MARKER in path.read_text(encoding="utf-8")
    )
    assert len(sources) == 2, "app_user 的迁移血统不再是两条，写侧合同的核对面需要同步修订"
    return sources


def draft(**overrides) -> IdentityRecordDraft:
    values = {
        "feishu_open_id": "ou_zhang",
        "feishu_user_id": "user_zhang",
        "feishu_union_id": "union_zhang",
        "display_name": "张一",
        "display_name_locale": "zh-CN",
        "department": "测试部门",
        "tenant_key": "tenant_a",
    }
    values.update(overrides)
    return IdentityRecordDraft(**values)


class ProvisioningRequestTest(unittest.TestCase):
    """请求的最小充分字段：身份草稿 + 花名册原值，没有第三部分。"""

    def test_the_request_carries_no_permission_field_at_all(self) -> None:
        """V-开通-01：匹配确认前不占位，因此权限字段连出现的地方都没有。"""

        names = {field.name for field in dataclasses.fields(ProvisioningRequest)}

        self.assertEqual(names, {"identity", "employee_no", "email"})
        for forbidden in ("permission_record_id", "permission_version", "galaxy_user_id"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, names)
        # 草稿那一侧同样挡住：`permission_record_id` 是恒为 None 的常量字段。
        self.assertIsNone(ProvisioningRequest(draft()).to_draft().permission_record_id)

    def test_roster_values_are_archived_verbatim(self) -> None:
        """V-开通-15：只去首尾空白，不小写、不截断、不补零。"""

        row = {
            "personnel_id": "user_zhang",
            "employee_no": "  00080001 ",
            "email": " Roster.User@Example-Corp.invalid ",
            "name": "张一",
        }

        request = ProvisioningRequest.from_roster_row(draft(), row)

        self.assertEqual(request.employee_no, "00080001")
        self.assertEqual(request.email, "Roster.User@Example-Corp.invalid")
        written = request.to_draft()
        self.assertEqual(written.employee_no, "00080001")
        self.assertEqual(written.email, "Roster.User@Example-Corp.invalid")

    def test_a_blank_roster_cell_becomes_none_rather_than_an_empty_string(self) -> None:
        request = ProvisioningRequest.from_roster_row(
            draft(), {"employee_no": "   ", "email": None}
        )

        self.assertIsNone(request.employee_no)
        self.assertIsNone(request.email)

    def test_a_missing_roster_row_is_allowed_because_provisioning_does_not_depend_on_it(self) -> None:
        request = ProvisioningRequest.from_roster_row(draft(), None)

        self.assertEqual((request.employee_no, request.email), (None, None))

    def test_roster_fields_on_the_draft_alone_are_refused_instead_of_silently_dropped(self) -> None:
        """工号是匹配银河的主键，静默丢掉它等于把这个人退化成纯邮箱匹配。"""

        for field in ROSTER_ARCHIVE_FIELDS:
            with self.subTest(field=field):
                request = ProvisioningRequest(draft(**{field: "值"}))
                with self.assertRaises(ValueError) as raised:
                    request.to_draft()
                self.assertIn(field, str(raised.exception))

    def test_the_archived_email_does_not_go_through_the_match_time_normalisation(self) -> None:
        """存档取花名册原值而不是 `AccountMatch.matched_email`。

        后者是为匹配做过小写归一的比较值；写进 `app_user` 会让 #52 的日报把每个
        含大写字母的邮箱**天天**报成变化，而资料一个字都没变。
        """

        row = {
            "personnel_id": "user_zhang",
            "employee_no": "00080001",
            "email": "Roster.User@Example-Corp.invalid",
            "name": "张一",
        }
        galaxy = [{"user_id": "g_1", "user_name": "00080001", "email": "roster.user@example-corp.invalid"}]

        match = match_galaxy_account("user_zhang", [row], galaxy)
        archived_from_roster = ProvisioningRequest.from_roster_row(draft(), row)

        self.assertEqual(match.state, "matched")
        self.assertEqual(match.matched_email, "roster.user@example-corp.invalid")
        self.assertNotEqual(archived_from_roster.email, match.matched_email)

        # 存进去的那一份与花名册当日值比对：零差异。
        written = archived_from_roster.to_draft()
        clean = compare_roster(
            [
                ArchivedIdentity(
                    app_user_id="usr_1",
                    personnel_id="user_zhang",
                    display_name=written.display_name,
                    employee_no=written.employee_no or "",
                    email=written.email or "",
                )
            ],
            [row],
        )
        self.assertEqual(clean.entries, ())

        # 换成匹配用的归一值：同一天、同一行花名册，日报立刻报出邮箱变化。
        noisy = compare_roster(
            [
                ArchivedIdentity(
                    app_user_id="usr_1",
                    personnel_id="user_zhang",
                    display_name=written.display_name,
                    employee_no=match.matched_employee_no or "",
                    email=match.matched_email or "",
                )
            ],
            [row],
        )
        self.assertEqual([entry.changed_fields for entry in noisy.entries], [("email",)])


class MissingIdentityFieldsTest(unittest.TestCase):
    """V-开通-06：残缺身份的字段名可分辨，且只有字段名。"""

    def test_the_field_list_matches_the_database_check_in_both_lineages(self) -> None:
        """这张表必须与建 `app_user` 那条「全有或全无」CHECK 逐字段一致。

        两条血统各核对一遍：只核对一条，另一条改了不会有任何东西变红。
        """

        for name, text in app_user_migration_lineages():
            with self.subTest(migration=name):
                constraint = text.split(_ALL_OR_NOTHING_MARKER, 1)[1].split("\n);", 1)[0]
                for field in IDENTITY_REQUIRED_FIELDS:
                    with self.subTest(field=field):
                        self.assertIn(f"NULLIF(BTRIM({field}), '') IS NOT NULL", constraint)
                # 多一个字段没进表、少一个字段留在表里，都会让这条计数变红。
                self.assertEqual(constraint.count("IS NOT NULL"), len(IDENTITY_REQUIRED_FIELDS))

    def test_a_complete_draft_has_no_missing_field(self) -> None:
        self.assertEqual(missing_identity_fields(draft()), ())

    def test_every_required_field_is_reported_when_blank(self) -> None:
        for field in IDENTITY_REQUIRED_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(missing_identity_fields(draft(**{field: "   "})), (field,))
                self.assertEqual(missing_identity_fields(draft(**{field: None})), (field,))

    def test_the_report_is_ordered_by_the_fixed_field_table_not_by_hash(self) -> None:
        blank = draft(feishu_union_id="", display_name="", feishu_open_id="")

        self.assertEqual(
            missing_identity_fields(blank),
            ("feishu_open_id", "feishu_union_id", "display_name"),
        )

    def test_the_optional_roster_fields_are_not_part_of_the_all_or_nothing_rule(self) -> None:
        """建档不以工号 / 邮箱为前提（2026-08-05 决策），它们由花名册读取步骤填充。"""

        self.assertEqual(missing_identity_fields(draft(employee_no=None, email=None)), ())


class BlockingGapTest(unittest.TestCase):
    """数据库故意不管的那一格：六字段全空是合法形态，`ON CONFLICT` 对 NULL 也不去重。"""

    def test_a_complete_request_leaves_the_defence_to_the_database(self) -> None:
        self.assertEqual(ProvisioningRequest(draft()).blocking_gap, ())

    def test_a_partial_request_still_goes_to_the_database(self) -> None:
        """只缺部门时不短路：那条 CHECK 是绕不过去的一道，得真的走一遍才证明得了。"""

        request = ProvisioningRequest(draft(department="  "))

        self.assertEqual(request.blocking_gap, ())
        self.assertEqual(request.missing_identity_fields, ("department",))

    def test_a_request_without_an_open_id_is_stopped_by_the_write_side_itself(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(open_id=value):
                request = ProvisioningRequest(draft(feishu_open_id=value))
                self.assertIn("feishu_open_id", request.blocking_gap)


class ClassifyWriteFailureTest(unittest.TestCase):
    """把数据库拒绝翻译成可分辨原因；不认识的必须返回 None。"""

    def test_the_delegated_subject_trigger_is_recognised_by_its_own_message(self) -> None:
        """V-身份-02：应用层已挡一次，数据库那道拒绝要能被认出来。"""

        self.assertIs(
            classify_write_failure(
                sqlstate=SQLSTATE_RAISE_EXCEPTION,
                message=f'ERROR:  {DELEGATED_SUBJECT_REJECTION_MARKER}\nCONTEXT: PL/pgSQL',
            ),
            ProvisioningRejection.DELEGATED_SUBJECT,
        )

    def test_the_marker_is_the_literal_both_migration_lineages_raise(self) -> None:
        """两条血统各自的正文都要对上：只核对一条，另一条改了不会有任何东西变红。"""

        for name, text in app_user_migration_lineages():
            with self.subTest(migration=name):
                self.assertIn(DELEGATED_SUBJECT_REJECTION_MARKER, text)

    def test_another_raise_exception_is_not_mistaken_for_the_delegated_subject(self) -> None:
        self.assertIsNone(
            classify_write_failure(sqlstate=SQLSTATE_RAISE_EXCEPTION, message="别的触发器拒绝")
        )

    def test_a_check_violation_with_missing_fields_is_an_incomplete_identity(self) -> None:
        self.assertIs(
            classify_write_failure(
                sqlstate=SQLSTATE_CHECK_VIOLATION,
                message='new row violates check constraint "app_user_check"',
                missing_fields=("department",),
            ),
            ProvisioningRejection.INCOMPLETE_IDENTITY,
        )

    def test_a_check_violation_on_a_complete_request_is_not_classified(self) -> None:
        """请求看着完整却仍被 CHECK 拒绝：不是业务结果，是我们不认识的约束。"""

        self.assertIsNone(
            classify_write_failure(
                sqlstate=SQLSTATE_CHECK_VIOLATION,
                message='new row violates check constraint "app_user_provisioning_state_check"',
            )
        )

    def test_unrelated_sqlstates_are_never_swallowed(self) -> None:
        for sqlstate in ("23505", "40001", "57014", None):
            with self.subTest(sqlstate=sqlstate):
                self.assertIsNone(
                    classify_write_failure(
                        sqlstate=sqlstate,
                        message=DELEGATED_SUBJECT_REJECTION_MARKER,
                        missing_fields=("department",),
                    )
                )


class ProvisioningResultTest(unittest.TestCase):
    """结果的不变式由类型强制，不靠调用方自觉。"""

    def test_success_carries_a_user_id_and_no_rejection(self) -> None:
        created = ProvisioningResult.created("usr_1")
        existing = ProvisioningResult.already_provisioned("usr_1")

        self.assertIs(created.outcome, ProvisioningOutcome.CREATED)
        self.assertIs(existing.outcome, ProvisioningOutcome.ALREADY_PROVISIONED)
        self.assertTrue(created.provisioned)
        self.assertTrue(existing.provisioned)
        self.assertEqual(created.app_user_id, existing.app_user_id)
        self.assertIsNone(existing.rejection)

    def test_a_rejection_can_never_carry_a_user_id(self) -> None:
        """「拒绝了但顺手给个用户标识」在类型层面构造不出来。"""

        with self.assertRaises(ValueError):
            ProvisioningResult(
                ProvisioningOutcome.REJECTED,
                "usr_1",
                ProvisioningRejection.INCOMPLETE_IDENTITY,
            )

    def test_a_rejection_must_carry_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            ProvisioningResult(ProvisioningOutcome.REJECTED)

    def test_a_success_must_carry_a_user_id(self) -> None:
        for outcome in (ProvisioningOutcome.CREATED, ProvisioningOutcome.ALREADY_PROVISIONED):
            with self.subTest(outcome=outcome):
                with self.assertRaises(ValueError):
                    ProvisioningResult(outcome)

    def test_a_success_cannot_smuggle_a_rejection_reason(self) -> None:
        with self.assertRaises(ValueError):
            ProvisioningResult(
                ProvisioningOutcome.CREATED, "usr_1", ProvisioningRejection.STORAGE_INTEGRITY
            )

    def test_a_rejection_reports_field_names_only(self) -> None:
        rejected = ProvisioningResult.rejected(
            ProvisioningRejection.INCOMPLETE_IDENTITY, missing_fields=("department", "tenant_key")
        )

        self.assertFalse(rejected.provisioned)
        self.assertIsNone(rejected.app_user_id)
        for name in rejected.missing_fields:
            with self.subTest(field=name):
                self.assertIn(name, IDENTITY_REQUIRED_FIELDS)


class RejectionRoutingTest(unittest.TestCase):
    """存储故障与业务结论不得合并——这条分界决定用户看到哪一句话。"""

    def test_only_the_storage_fault_is_flagged_as_one(self) -> None:
        self.assertTrue(ProvisioningRejection.STORAGE_INTEGRITY.is_storage_fault)
        self.assertFalse(ProvisioningRejection.INCOMPLETE_IDENTITY.is_storage_fault)
        self.assertFalse(ProvisioningRejection.DELEGATED_SUBJECT.is_storage_fault)

    def test_the_three_reasons_never_collapse_into_one_another(self) -> None:
        values = [reason.value for reason in ProvisioningRejection]

        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(len(values), 3)


class ProvisioningPortTest(unittest.TestCase):
    """注入口的参数表本身就是数据边界。"""

    def test_the_port_takes_a_request_and_nothing_else(self) -> None:
        import inspect

        signature = inspect.signature(IdentityProvisioning.provision)

        self.assertEqual(list(signature.parameters), ["self", "request"])

    def test_the_postgres_store_satisfies_the_port(self) -> None:
        """真实实现与合同同名同参；装配时不会因为签名漂移在运行期才发现。"""

        import inspect

        from lingxi.adapters.postgres_identity import PostgresAppUserStore

        self.assertEqual(
            inspect.signature(PostgresAppUserStore.provision),
            inspect.signature(IdentityProvisioning.provision),
        )


class ReentryTouchesNoLifecycleColumnTest(unittest.TestCase):
    """幂等重入不得改写生命周期与权限列——由 `DO UPDATE SET` 的**列表本身**保证。

    这条保证此前只是"那几列没写在语句里"这个事实，没有任何东西会在它被写回去时变红。
    源码级核对是仓库既有做法（`adapters/postgres_roster_audit` 的只读断言同一形状）。
    真库那一半（停用后重入不复活）在 `tests/test_identity_postgres_records.py`。
    """

    # 重入绝不能出现在赋值左侧的列。前两列是准入与启停状态：一次孤儿事件重交接把
    # `account_state` 写回 `enabled`，等于让建档服务替管理员撤销了一次停用。
    PROTECTED_COLUMNS = (
        "account_state",
        "provisioning_state",
        "permission_record_id",
        "permission_version",
        "permission_checked_at",
        "workspace_state",
        "publish_state",
        "mcp_sync_state",
        "posix_uid",
        "erased_at",
    )

    def _update_clause(self) -> str:
        import inspect

        from lingxi.adapters.postgres_identity import PostgresAppUserStore

        source = inspect.getsource(PostgresAppUserStore.record_identity)
        self.assertIn("DO UPDATE SET", source, "建档语句不再是 upsert，本守卫需要同步修订")
        return source.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]

    def test_no_lifecycle_or_permission_column_is_assigned_on_conflict(self) -> None:
        clause = self._update_clause()

        for column in self.PROTECTED_COLUMNS:
            with self.subTest(column=column):
                self.assertNotIn(column, clause)

    def test_the_guard_actually_looks_at_the_conflict_branch(self) -> None:
        """守卫自身的正向锚点：切出来的确实是刷新那一段，不是空串。"""

        clause = self._update_clause()

        self.assertIn("display_name = EXCLUDED.display_name", clause)
        self.assertIn("employee_no", clause)
        self.assertIn("updated_at = now()", clause)


def _psycopg_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("psycopg") is not None


PSYCOPG_SKIP_REASON = "跳过：未安装 psycopg，驱动异常到建档结果的映射未验证（本组不需要数据库，只需要驱动）"


@unittest.skipUnless(_psycopg_available(), PSYCOPG_SKIP_REASON)
class WriteFailureMappingTest(unittest.TestCase):
    """驱动异常 → 建档结果的映射。**不需要数据库**，只需要驱动的异常类型。

    真库那一半（约束与触发器**真的**会拒、拒绝后零行残留）在
    `tests/test_identity_postgres_records.py`，按 DSN 门控。这里补的是没有数据库
    也能跑的那一半：映射接错、幂等分支接反、把不认识的拒绝吞成业务终态，都会红。
    """

    def _store(self, outcome):
        from lingxi.adapters.postgres_identity import PostgresAppUserStore

        class _Stub(PostgresAppUserStore):
            """只替换 `record_identity`，`provision` 走真实实现。"""

            def __init__(self) -> None:
                super().__init__("dsn-not-used-by-this-test")
                self.drafts: list = []

            def record_identity(self, draft):
                self.drafts.append(draft)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        return _Stub()

    def _record(self, *, created: bool):
        from lingxi.adapters.postgres_identity import AppUserRecord

        return AppUserRecord(
            id="usr_1",
            provisioning_state="matching",
            permission_record_id=None,
            created=created,
            employee_no="00080001",
            email="Roster.User@Example-Corp.invalid",
        )

    def _request(self, **overrides) -> ProvisioningRequest:
        return ProvisioningRequest.from_roster_row(
            draft(**overrides),
            {"employee_no": "00080001", "email": "Roster.User@Example-Corp.invalid"},
        )

    def test_a_fresh_insert_maps_to_created(self) -> None:
        store = self._store(self._record(created=True))

        result = store.provision(self._request())

        self.assertIs(result.outcome, ProvisioningOutcome.CREATED)
        self.assertEqual(result.app_user_id, "usr_1")

    def test_an_existing_row_maps_to_already_provisioned_rather_than_an_error(self) -> None:
        """幂等语义的映射面：重入不得被读成失败，也不得被读成新建。"""

        store = self._store(self._record(created=False))

        result = store.provision(self._request())

        self.assertIs(result.outcome, ProvisioningOutcome.ALREADY_PROVISIONED)
        self.assertTrue(result.provisioned)
        self.assertEqual(result.app_user_id, "usr_1")

    def test_the_roster_values_reach_the_write_verbatim(self) -> None:
        store = self._store(self._record(created=True))

        store.provision(self._request())

        self.assertEqual(
            (store.drafts[0].employee_no, store.drafts[0].email),
            ("00080001", "Roster.User@Example-Corp.invalid"),
        )

    def test_a_partial_identity_is_still_sent_to_the_database(self) -> None:
        """写侧不抢在数据库前面短路：那条 CHECK 得真的走一遍才证明得了它还在。"""

        from psycopg.errors import CheckViolation

        store = self._store(CheckViolation('new row violates check constraint "app_user_check"'))

        result = store.provision(self._request(department="   "))

        self.assertEqual(len(store.drafts), 1)
        self.assertIs(result.rejection, ProvisioningRejection.INCOMPLETE_IDENTITY)
        self.assertEqual(result.missing_fields, ("department",))

    def test_a_request_without_an_open_id_never_reaches_the_write(self) -> None:
        store = self._store(self._record(created=True))

        result = store.provision(ProvisioningRequest(draft(feishu_open_id="  ")))

        self.assertEqual(store.drafts, [])
        self.assertIs(result.rejection, ProvisioningRejection.INCOMPLETE_IDENTITY)

    def test_the_delegated_subject_trigger_maps_without_field_names(self) -> None:
        from psycopg.errors import RaiseException

        store = self._store(RaiseException(DELEGATED_SUBJECT_REJECTION_MARKER))

        result = store.provision(self._request())

        self.assertIs(result.rejection, ProvisioningRejection.DELEGATED_SUBJECT)
        self.assertEqual(result.missing_fields, ())

    def test_a_read_back_mismatch_maps_to_a_storage_fault(self) -> None:
        from lingxi.adapters.postgres_identity import IdentityStorageIntegrityError

        store = self._store(IdentityStorageIntegrityError("app_user employee_no 持久化回读不一致"))

        result = store.provision(self._request())

        self.assertIs(result.rejection, ProvisioningRejection.STORAGE_INTEGRITY)
        self.assertTrue(result.rejection.is_storage_fault)

    def test_an_unrecognised_driver_error_is_re_raised_not_classified(self) -> None:
        """吞掉它等于让一条未知约束伪装成「无可用银河权限」。"""

        from psycopg.errors import RaiseException, SerializationFailure, UniqueViolation

        for error in (
            RaiseException("别的触发器拒绝"),
            UniqueViolation("duplicate key"),
            SerializationFailure("could not serialize access"),
        ):
            with self.subTest(error=type(error).__name__):
                store = self._store(error)
                with self.assertRaises(type(error)):
                    store.provision(self._request())

    def test_a_check_violation_on_a_complete_request_is_re_raised(self) -> None:
        from psycopg.errors import CheckViolation

        store = self._store(
            CheckViolation('new row violates check constraint "app_user_provisioning_state_check"')
        )

        with self.assertRaises(CheckViolation):
            store.provision(self._request())


if __name__ == "__main__":
    unittest.main()
