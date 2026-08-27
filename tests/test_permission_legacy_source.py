"""``core/permission/legacy_source.py`` 的纯逻辑测试（S-P-2，Issue #319 / Trace #328）。

不需要数据库、不需要真实飞书调用——``find_rows``/``read_row`` 都是注入的假传输层。
两个调用点各自的接线测试（真实的 ``PermissionRefreshDuty``/``AutoOnboardingRunner``
装配 + 审计事件）分别在 ``tests/test_permission_refresh_duty.py::LegacySourceMergeTest``
与 ``tests/test_onboarding_runner.py::LegacySourceMergeTests``。

两处变异锚点已实测验红、验证后原样还原（本卡要求，结果登记在此，不重复登记在别处）：

1. **legacy 并集分支删除**：临时把 ``merge_permission_sources`` 非通配分支里
   ``| set(legacy_map.get(key, ()))`` 那一段从并集表达式里删掉后，
   ``ReadLegacyPermissionsTests.test_a_matching_row_parses_permissions_into_company_metric_map``
   与 ``tests/test_permission_refresh_duty.py::LegacySourceMergeTest.
   test_legacy_permissions_are_unioned_into_the_published_metrics`` 由绿转红（存量指标
   消失）；改回后复绿。
2. **异常吞成静默**：临时把 :func:`resolve_legacy_source` 里的
   ``except LegacySourceError as error:`` 分支改成直接 ``return {}``（不记审计）后，
   ``ResolveLegacySourceTests.test_read_failure_skips_and_audits_with_reason_and_error``
   由绿转红（审计列表为空）；改回后复绿。
"""

from __future__ import annotations

import unittest
from typing import Any

from lingxi.core.permission.legacy_source import (
    REASON_LEGACY_KEY_MISMATCH,
    REASON_LEGACY_MULTIPLE_ROWS,
    REASON_LEGACY_READ_FAILED,
    REASON_LEGACY_UNPARSEABLE,
    LegacySourceError,
    read_legacy_permissions,
    resolve_legacy_source,
)
from lingxi.core.permission.publish import ExistingPermissionRow

EMAIL = "Xiaoming@Example.com"
NORMALIZED_EMAIL = "xiaoming@example.com"


def _row(
    *,
    record_id: str = "rec_1",
    record_key: str = NORMALIZED_EMAIL,
    email: str = NORMALIZED_EMAIL,
    permissions: str = '{"1011":["legacy_metric"]}',
) -> ExistingPermissionRow:
    return ExistingPermissionRow(
        record_id=record_id,
        fields={"record_key": record_key, "email": email, "permissions": permissions},
    )


class FakeLegacyTable:
    """``LegacyPermissionTable`` 的假实现：``find_rows``/``read_row`` 都可以注入
    固定返回值或直接抛异常，供失败路径用例复现传输层故障。"""

    def __init__(
        self,
        *,
        rows: tuple[ExistingPermissionRow, ...] = (),
        find_error: Exception | None = None,
        fields_by_record_id: dict[str, dict[str, Any]] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._find_error = find_error
        self._fields = fields_by_record_id or {}
        self._read_error = read_error
        self.find_calls: list[tuple[str, str]] = []
        self.read_calls: list[str] = []

    def find_rows(self, *, record_key: str, email: str) -> tuple[ExistingPermissionRow, ...]:
        self.find_calls.append((record_key, email))
        if self._find_error is not None:
            raise self._find_error
        return self._rows

    def read_row(self, record_id: str) -> dict[str, Any]:
        self.read_calls.append(record_id)
        if self._read_error is not None:
            raise self._read_error
        return self._fields.get(record_id, {})


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.records.append((action, dict(fields)))


class ReadLegacyPermissionsTests(unittest.TestCase):
    """:func:`read_legacy_permissions` 的失败语义矩阵。"""

    def test_no_matching_row_returns_empty_mapping(self) -> None:
        """新用户从未在旧系统留下权限行：预期情形，不是错误。"""

        table = FakeLegacyTable(rows=())

        result = read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(result, {})

    def test_lookup_uses_the_normalized_email_for_both_keys(self) -> None:
        """``record_key``/``email`` 两个查找键取同一个规范化邮箱，与发布路径同源。"""

        table = FakeLegacyTable(rows=())

        read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(table.find_calls, [(NORMALIZED_EMAIL, NORMALIZED_EMAIL)])

    def test_a_matching_row_parses_permissions_into_company_metric_map(self) -> None:
        row = _row(permissions='{"1011":["日活","收入"]}')
        table = FakeLegacyTable(
            rows=(row,), fields_by_record_id={"rec_1": {"permissions": '{"1011":["日活","收入"]}'}}
        )

        result = read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(result, {"1011": ("日活", "收入")})
        self.assertEqual(table.read_calls, ["rec_1"])

    def test_multiple_matching_rows_raise_conflict(self) -> None:
        table = FakeLegacyTable(rows=(_row(record_id="rec_1"), _row(record_id="rec_2")))

        with self.assertRaises(LegacySourceError) as ctx:
            read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(ctx.exception.code, REASON_LEGACY_MULTIPLE_ROWS)
        self.assertIsNone(ctx.exception.detail)
        self.assertEqual(table.read_calls, [], "冲突时不该再去读任何一行")

    def test_a_row_with_a_different_record_key_raises_key_mismatch(self) -> None:
        """命中的行 ``record_key`` 与我们要查的口径不一致：与 ``publish_claim`` 的
        ``CONFLICT`` 分支同一姿态，失败关闭而不是猜。"""

        table = FakeLegacyTable(rows=(_row(record_key="someone-else@example.invalid"),))

        with self.assertRaises(LegacySourceError) as ctx:
            read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(ctx.exception.code, REASON_LEGACY_KEY_MISMATCH)
        self.assertIsNone(ctx.exception.detail)

    def test_find_rows_failure_raises_read_failed_with_the_error_class_name(self) -> None:
        table = FakeLegacyTable(find_error=RuntimeError("注入的传输层故障"))

        with self.assertRaises(LegacySourceError) as ctx:
            read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(ctx.exception.code, REASON_LEGACY_READ_FAILED)
        self.assertEqual(ctx.exception.detail, "RuntimeError")
        self.assertIs(ctx.exception.__cause__.__class__, RuntimeError, "原始异常经 __cause__ 保留")

    def test_read_row_failure_raises_read_failed_with_the_error_class_name(self) -> None:
        table = FakeLegacyTable(rows=(_row(),), read_error=ValueError("注入的读回故障"))

        with self.assertRaises(LegacySourceError) as ctx:
            read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(ctx.exception.code, REASON_LEGACY_READ_FAILED)
        self.assertEqual(ctx.exception.detail, "ValueError")

    def test_unparseable_permissions_text_raises_unparseable_with_the_error_class_name(self) -> None:
        table = FakeLegacyTable(
            rows=(_row(),), fields_by_record_id={"rec_1": {"permissions": "不是合法 JSON"}}
        )

        with self.assertRaises(LegacySourceError) as ctx:
            read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(ctx.exception.code, REASON_LEGACY_UNPARSEABLE)
        self.assertEqual(ctx.exception.detail, "ValueError")

    def test_a_blank_permissions_cell_on_an_existing_row_is_unparseable_not_empty(self) -> None:
        """行存在但 ``permissions`` 列缺失/空白：与 ``parse_permissions`` 自身的既有
        判据一致——不当作"没有存量权限"静默放过，按无法解析处理。"""

        table = FakeLegacyTable(rows=(_row(),), fields_by_record_id={"rec_1": {}})

        with self.assertRaises(LegacySourceError) as ctx:
            read_legacy_permissions(email=EMAIL, table=table)

        self.assertEqual(ctx.exception.code, REASON_LEGACY_UNPARSEABLE)


class ResolveLegacySourceTests(unittest.TestCase):
    """:func:`resolve_legacy_source` 的「读取 + 降级 + 审计」姿态。"""

    def test_table_absent_returns_none_and_records_no_audit(self) -> None:
        """装配层未接线（``table=None``）：静默按"没有存量源"处理，不告警。"""

        audit = RecordingAudit()

        result = resolve_legacy_source(
            email=EMAIL, table=None, audit=audit, action="permission_refresh.legacy_source_skipped", user="usr_1"
        )

        self.assertIsNone(result)
        self.assertEqual(audit.records, [])

    def test_success_returns_the_parsed_mapping_without_any_audit(self) -> None:
        table = FakeLegacyTable(
            rows=(_row(),), fields_by_record_id={"rec_1": {"permissions": '{"1011":["日活"]}'}}
        )
        audit = RecordingAudit()

        result = resolve_legacy_source(
            email=EMAIL, table=table, audit=audit, action="permission_refresh.legacy_source_skipped", user="usr_1"
        )

        self.assertEqual(result, {"1011": ("日活",)})
        self.assertEqual(audit.records, [])

    def test_read_failure_skips_and_audits_with_reason_and_error(self) -> None:
        table = FakeLegacyTable(find_error=RuntimeError("注入的传输层故障"))
        audit = RecordingAudit()

        result = resolve_legacy_source(
            email=EMAIL, table=table, audit=audit, action="permission_refresh.legacy_source_skipped", user="usr_1"
        )

        self.assertIsNone(result)
        self.assertEqual(len(audit.records), 1)
        action, fields = audit.records[0]
        self.assertEqual(action, "permission_refresh.legacy_source_skipped")
        self.assertEqual(fields["user"], "usr_1")
        self.assertEqual(fields["reason"], REASON_LEGACY_READ_FAILED)
        self.assertEqual(fields["error"], "RuntimeError")

    def test_conflict_failure_audits_without_an_error_field(self) -> None:
        """``multiple_rows``/``record_key_mismatch`` 不源自某个被捕获的异常，审计里
        因此没有 ``error`` 字段——不是遗漏，是没有可报告的异常类名。"""

        table = FakeLegacyTable(rows=(_row(record_id="rec_1"), _row(record_id="rec_2")))
        audit = RecordingAudit()

        resolve_legacy_source(
            email=EMAIL, table=table, audit=audit, action="onboarding.legacy_source_skipped", user="usr_1"
        )

        action, fields = audit.records[0]
        self.assertEqual(action, "onboarding.legacy_source_skipped")
        self.assertEqual(fields["reason"], REASON_LEGACY_MULTIPLE_ROWS)
        self.assertNotIn("error", fields)

    def test_the_action_name_is_taken_verbatim_from_the_caller(self) -> None:
        """两个调用点各传各的动作名（``permission_refresh.legacy_source_skipped``/
        ``onboarding.legacy_source_skipped``），本函数不硬编码任何一个。"""

        table = FakeLegacyTable(find_error=RuntimeError("boom"))
        audit = RecordingAudit()

        resolve_legacy_source(
            email=EMAIL, table=table, audit=audit, action="some.custom.action", user="usr_1"
        )

        self.assertEqual(audit.records[0][0], "some.custom.action")
