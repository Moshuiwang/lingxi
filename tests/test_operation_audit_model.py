"""运营操作审计条目的纯逻辑断言：枚举、逐列校验、执行者标签回退、凭据形状失败关闭。

``operation_audit`` 表没有自由文本列，模型的原则是**只拒绝、不脱敏**：本文件的核心
是一组否定断言——五类长得像凭据或业务正文的值（含空白、``=`` 赋值形态、``bearer``、
带口令的连接串、超长）逐列都必须在构造时 ``ValueError``，而不是被截断或替换后写进去。
合法样本必须原样通过，否则否定断言只是在证明「什么都写不进去」。

数据全部为虚构化名，不含任何真实人员数据。
"""

from __future__ import annotations

import unittest
from importlib import metadata
from types import MappingProxyType
from unittest import mock

from lingxi.core.admin.operation_audit import (
    PHASES_REQUIRING_DECIDER,
    EntryPoint,
    OperationAuditEntry,
    OperationAuditLedger,
    OperationPhase,
    executor_label,
    format_actor_roles,
    parse_actor_roles,
)
from lingxi.core.admin.registry import AdminRole

DIGEST = "sha256:" + "ab" * 32
ULID = "01HXYZABCDEFGHJKMNPQRSTVWX"


def valid_entry(**overrides: object) -> OperationAuditEntry:
    """一条每一列都填上合法值的 executed 行；用例只覆盖自己关心的列。"""
    fields: dict[str, object] = {
        "operation_id": f"opr_{ULID}",
        "operation": "preprovision.apply",
        "phase": OperationPhase.EXECUTED,
        "initiated_by": "ou_admin_fake",
        "actor_roles": frozenset(AdminRole),
        "entry_point": EntryPoint.OPS_SCRIPT,
        "decided_by": "ou_decider_fake",
        "executor": f"scheduler-script:preprovision@2.5.0:opr_{ULID}",
        "purpose": "preprovision_roster",
        "target_kind": "roster",
        "target_count": 3,
        "target_digest": DIGEST,
        "target_user_id": f"usr_{ULID}",
        "result_code": "skipped:already_provisioned",
        "result_counts": {"provisioned": 2, "skipped": 1},
        "evidence_ref": f"innertest_batch:ibt_{ULID}",
        "pending_action_id": f"pac_{ULID}",
        "trace_id": f"trc_{ULID}",
    }
    fields.update(overrides)
    return OperationAuditEntry(**fields)


class EnumTest(unittest.TestCase):
    def test_phase_and_entry_point_values_match_the_table_checks(self) -> None:
        self.assertEqual(
            {phase.value for phase in OperationPhase},
            {"prepared", "confirmed", "cancelled", "rejected", "executed"},
        )
        self.assertEqual(
            {entry.value for entry in EntryPoint},
            {"ops_script", "restricted_channel", "feishu_card", "scheduler_followup"},
        )
        self.assertEqual(
            PHASES_REQUIRING_DECIDER, {OperationPhase.CONFIRMED, OperationPhase.CANCELLED}
        )

    def test_phase_and_entry_point_must_be_the_enums_not_strings(self) -> None:
        with self.assertRaises(ValueError):
            valid_entry(phase="executed")
        with self.assertRaises(ValueError):
            valid_entry(entry_point="ops_script")


class ValidEntryTest(unittest.TestCase):
    def test_a_fully_populated_entry_is_accepted_verbatim(self) -> None:
        entry = valid_entry()

        self.assertEqual(entry.operation, "preprovision.apply")
        self.assertEqual(entry.result_code, "skipped:already_provisioned")
        self.assertEqual(entry.target_digest, DIGEST)
        self.assertEqual(dict(entry.result_counts), {"provisioned": 2, "skipped": 1})
        self.assertIsInstance(entry.result_counts, MappingProxyType)
        self.assertEqual(entry.actor_roles, frozenset(AdminRole))

    def test_a_minimal_prepared_entry_needs_only_the_six_required_columns(self) -> None:
        entry = OperationAuditEntry(
            operation_id=f"opr_{ULID}",
            operation="outreach.welcome_card",
            phase=OperationPhase.PREPARED,
            initiated_by="ou_admin_fake",
            actor_roles=frozenset({AdminRole.OPS_ADMIN}),
            entry_point=EntryPoint.OPS_SCRIPT,
        )

        self.assertIsNone(entry.decided_by)
        self.assertEqual(dict(entry.result_counts), {})

    def test_result_codes_generated_by_the_scripts_are_accepted(self) -> None:
        for code in ("completed", "partial", "failed_ValueError", "not_started:TypeError"):
            with self.subTest(code=code):
                self.assertEqual(valid_entry(result_code=code).result_code, code)

    def test_an_empty_role_snapshot_is_allowed_and_round_trips_as_an_empty_string(self) -> None:
        entry = valid_entry(actor_roles=frozenset())

        self.assertEqual(format_actor_roles(entry.actor_roles), "")
        self.assertEqual(parse_actor_roles(""), frozenset())

    def test_the_entry_is_frozen(self) -> None:
        entry = valid_entry()

        with self.assertRaises(AttributeError):
            entry.result_code = "completed"
        with self.assertRaises(TypeError):
            entry.result_counts["provisioned"] = 99


class PhaseRuleTest(unittest.TestCase):
    def test_confirmed_and_cancelled_need_a_decider(self) -> None:
        for phase in (OperationPhase.CONFIRMED, OperationPhase.CANCELLED):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                valid_entry(phase=phase, decided_by=None)
            self.assertEqual(valid_entry(phase=phase).phase, phase)

    def test_executed_needs_executor_and_result_code(self) -> None:
        with self.assertRaises(ValueError):
            valid_entry(executor=None)
        with self.assertRaises(ValueError):
            valid_entry(result_code=None)

    def test_prepared_and_rejected_do_not_need_them(self) -> None:
        for phase in (OperationPhase.PREPARED, OperationPhase.REJECTED):
            entry = valid_entry(phase=phase, decided_by=None, executor=None, result_code=None)
            self.assertEqual(entry.phase, phase)


#: 凭据与业务正文的五种形状：每一种都逐列试一遍，任何一列放行都是一条真实的泄漏路径。
SECRET_SHAPES = {
    "whitespace": "sk live ABC",
    "assignment": "token=sk_live_ABC",
    "bearer": "Bearer_sk_live_ABC",
    "credential_url": "postgresql://app:hunter2@db.internal/lingxi",
    "overlong": "a" * 200,
}
STRING_COLUMNS = (
    "operation_id",
    "operation",
    "initiated_by",
    "decided_by",
    "executor",
    "purpose",
    "target_kind",
    "target_digest",
    "target_user_id",
    "result_code",
    "evidence_ref",
    "pending_action_id",
    "trace_id",
)


class SecretShapeRefusalTest(unittest.TestCase):
    """五类凭据形状 × 十三个字符串列：全部失败关闭，没有任何一列做脱敏或截断。"""

    def test_every_secret_shape_is_refused_on_every_string_column(self) -> None:
        for column in STRING_COLUMNS:
            for shape, value in SECRET_SHAPES.items():
                with self.subTest(column=column, shape=shape), self.assertRaises(ValueError):
                    valid_entry(**{column: value})

    def test_the_length_limit_is_per_column_not_a_single_constant(self) -> None:
        """摘要列固定 71 位、执行者标签可到 128 位；码列 65 位就拒绝。"""
        self.assertEqual(valid_entry(result_code="a" * 64).result_code, "a" * 64)
        with self.assertRaises(ValueError):
            valid_entry(result_code="a" * 65)
        long_executor = "scheduler-script:preprovision@2.5.0:opr_" + "A" * 60
        self.assertEqual(valid_entry(executor=long_executor).executor, long_executor)

    def test_values_that_are_not_the_column_shape_are_refused_even_without_a_secret(self) -> None:
        for column, value in (
            ("operation", "Preprovision.Apply"),
            ("target_digest", "md5:" + "ab" * 16),
            ("evidence_ref", "innertest_batch"),
            ("initiated_by", "someone@example.com"),
            ("executor", "scheduler"),
        ):
            with self.subTest(column=column), self.assertRaises(ValueError):
                valid_entry(**{column: value})

    def test_non_string_values_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            valid_entry(result_code=12)
        with self.assertRaises(ValueError):
            valid_entry(operation="")

    def test_result_counts_only_take_code_keys_and_non_negative_integers(self) -> None:
        for bad in (
            {"token=abc": 1},
            {"Provisioned": 1},
            {"provisioned": "sk_live_ABC"},
            {"provisioned": True},
            {"provisioned": -1},
            [("provisioned", 1)],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                valid_entry(result_counts=bad)

    def test_target_count_must_be_a_non_negative_integer(self) -> None:
        for bad in (-1, True, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                valid_entry(target_count=bad)
        self.assertEqual(valid_entry(target_count=0).target_count, 0)

    def test_actor_roles_only_take_registry_roles(self) -> None:
        with self.assertRaises(ValueError):
            valid_entry(actor_roles="super_admin")
        with self.assertRaises(ValueError):
            valid_entry(actor_roles=frozenset({"super_admin"}))


class ActorRolesTextTest(unittest.TestCase):
    def test_roles_are_written_in_registry_order_regardless_of_set_order(self) -> None:
        text = format_actor_roles([AdminRole.SUPER_ADMIN, AdminRole.PERMISSION_ADMIN])

        self.assertEqual(text, "permission_admin,super_admin")
        self.assertEqual(
            parse_actor_roles(text), frozenset({AdminRole.SUPER_ADMIN, AdminRole.PERMISSION_ADMIN})
        )

    def test_an_unknown_role_name_does_not_parse(self) -> None:
        with self.assertRaises(ValueError):
            parse_actor_roles("root")


class ExecutorLabelTest(unittest.TestCase):
    def test_the_label_carries_service_version_and_run_id(self) -> None:
        with mock.patch.object(metadata, "version", return_value="2.5.0"):
            self.assertEqual(executor_label("scheduler"), "scheduler@2.5.0")
            self.assertEqual(
                executor_label("scheduler-script:outreach", run_id="prk_01"),
                "scheduler-script:outreach@2.5.0:prk_01",
            )

    def test_an_uninstalled_package_falls_back_to_unknown(self) -> None:
        with mock.patch.object(
            metadata, "version", side_effect=metadata.PackageNotFoundError("lingxi")
        ):
            label = executor_label("scheduler", run_id="run_01")

        self.assertEqual(label, "scheduler@unknown:run_01")
        self.assertEqual(valid_entry(executor=label).executor, label)


class LedgerPortTest(unittest.TestCase):
    def test_a_recording_object_satisfies_the_port(self) -> None:
        class Recording:
            def __init__(self) -> None:
                self.entries: list[OperationAuditEntry] = []

            def record(self, entry: OperationAuditEntry) -> str:
                self.entries.append(entry)
                return f"opa_{len(self.entries)}"

        ledger: OperationAuditLedger = Recording()

        self.assertEqual(ledger.record(valid_entry()), "opa_1")


if __name__ == "__main__":
    unittest.main()
