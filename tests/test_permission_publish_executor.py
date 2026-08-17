"""发布意图消费的纯编排断言（Issue #156 / S-C-01）。

认领断言：`V-权限-02`（写入后逐字段读回，一致才算发布完成）、`V-权限-03`（发布 A 不
改动 B 的行）、`V-权限-09`（同一 ``record_key`` 重复发布收敛到同一行；同邮箱不同
``record_key`` 失败关闭不新建）、`V-权限-10`（读回不一致不记发布完成并告警）、
`V-权限-11`（不写 ``token_cipher``，既有行的该列不被清空）、`V-权限-12`（旧版本不覆盖
新版本；重入不产生第二行）。

外部表格以假传输层注入（形式沿用 ``tests/test_feishu_delivery_classification.py`` 与
花名册读取用例的既有先例）：**本文件不做任何真实飞书调用**。

否定面：
- 旧版本的意图**一次外部调用都不发**；
- 冲突时**既不更新也不新建**（整张假表逐字节不变）；
- 读回不一致时**不**判发布完成、**不**写 ``published_at``；
- 结果不明**不**被当成明确失败；未预期异常**不**被吞成"结果不明"。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lingxi.core.permission.publish import (
    DEFAULT_MAX_ATTEMPTS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_SUPERSEDED,
    ClaimedPublish,
    ExistingPermissionRow,
    PermissionPublishExecutor,
    PermissionTableError,
    PublishAttempt,
    PublishFailureKind,
    PublishOutcome,
    publish_claim,
)
from lingxi.core.permission.publish_row import PUBLISHED_FIELD_NAMES, PublishRow

FAKE_EMAIL = "jiaming.jia@example.invalid"
OTHER_EMAIL = "yiming.yi@example.invalid"
FAKE_NAME = "化名甲"
PERMISSIONS = '{"1011":["商务"]}'
UPDATED_AT = "2026-08-17T03:00:00Z"


def _row(email: str = FAKE_EMAIL, *, permissions: str = PERMISSIONS) -> PublishRow:
    return PublishRow(
        record_key=email,
        email=email,
        name=FAKE_NAME,
        permissions=permissions,
        status="approved",
        updated_at=UPDATED_AT,
    )


def _claim(
    *,
    row: PublishRow | None = None,
    version: int = 1,
    current: int | None = 1,
    attempts: int = 1,
    payload: dict | None = None,
    user_id: str = "usr_A",
) -> ClaimedPublish:
    fields = payload if payload is not None else (row or _row()).fields
    return ClaimedPublish(
        outbox_id="pub_1",
        user_id=user_id,
        permission_version=version,
        payload=fields,
        attempts=attempts,
        current_permission_version=current,
    )


class FakeTable:
    """内存版发布表。记录每一次调用，便于断言"一次外部调用都没发"。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: list[dict] = rows or []
        self.calls: list[str] = []
        self._next = len(self.rows) + 1
        #: 注入故障：方法名 → 要抛的异常。
        self.faults: dict[str, Exception] = {}
        #: 写入后平台"悄悄改掉"的字段（模拟读回不一致）。
        self.mutate_on_write: dict[str, object] = {}

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        error = self.faults.pop(name, None)
        if error is not None:
            raise error

    def _find(self, record_id: str) -> dict:
        for row in self.rows:
            if row["record_id"] == record_id:
                return row
        raise PermissionTableError("feishu_code_1254043")

    def find_rows(self, *, record_key: str, email: str):
        self._maybe_fail("find_rows")
        matched = []
        for row in self.rows:
            fields = row["fields"]
            if str(fields.get("record_key", "")).casefold() == record_key.casefold() or str(
                fields.get("email", "")
            ).casefold() == email.casefold():
                matched.append(ExistingPermissionRow(row["record_id"], dict(fields)))
        return tuple(matched)

    def create_row(self, fields):
        self._maybe_fail("create_row")
        record_id = f"rec_{self._next}"
        self._next += 1
        stored = dict(fields)
        stored.update(self.mutate_on_write)
        self.rows.append({"record_id": record_id, "fields": stored})
        return record_id

    def update_row(self, record_id, fields):
        self._maybe_fail("update_row")
        row = self._find(record_id)
        # 部分更新：未列出的列保持原值（真实平台语义）。
        row["fields"].update(fields)
        row["fields"].update(self.mutate_on_write)

    def read_row(self, record_id):
        self._maybe_fail("read_row")
        return dict(self._find(record_id)["fields"])

    def snapshot(self) -> list[dict]:
        return [{"record_id": row["record_id"], "fields": dict(row["fields"])} for row in self.rows]


class PublishClaimTest(unittest.TestCase):
    def test_create_then_readback_publishes(self) -> None:
        table = FakeTable()
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(attempt.action, "create")
        self.assertEqual(attempt.external_record_id, "rec_1")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(set(table.rows[0]["fields"]), set(PUBLISHED_FIELD_NAMES))

    def test_existing_row_is_updated_and_token_cipher_survives(self) -> None:
        """`V-权限-11`：更新集不含 ``token_cipher``，既有值原样留在表里。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "业务侧写入的密文",
                    },
                }
            ]
        )
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(attempt.action, "update")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], "业务侧写入的密文")
        self.assertEqual(table.rows[0]["fields"]["name"], FAKE_NAME)
        self.assertNotIn("create_row", table.calls)

    def test_repeated_publish_converges_to_one_row(self) -> None:
        """`V-权限-09`：同一 ``record_key`` 重复发布不产生第二行。"""

        table = FakeTable()
        first = publish_claim(_claim(), transport=table)
        second = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(first.published and second.published)
        self.assertEqual(second.action, "update")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(first.external_record_id, second.external_record_id)

    def test_same_email_other_record_key_fails_closed(self) -> None:
        """`V-权限-09` 否定面：既有行用了别的 ``record_key`` 口径 → 不更新也不新建。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {"record_key": "EMP-700123", "email": FAKE_EMAIL, "name": "旧名"},
                }
            ]
        )
        before = table.snapshot()
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.CONFLICT)
        self.assertEqual(attempt.error_code, "record_key_mismatch")
        self.assertFalse(attempt.retryable)
        self.assertEqual(table.snapshot(), before)
        self.assertEqual(table.calls, ["find_rows"])

    def test_multiple_matches_fail_closed(self) -> None:
        table = FakeTable(
            [
                {"record_id": "rec_1", "fields": {"record_key": FAKE_EMAIL, "email": FAKE_EMAIL}},
                {"record_id": "rec_2", "fields": {"record_key": "x", "email": FAKE_EMAIL}},
            ]
        )
        before = table.snapshot()
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.CONFLICT)
        self.assertEqual(attempt.error_code, "multiple_rows")
        self.assertEqual(table.snapshot(), before)

    def test_record_key_case_difference_still_updates(self) -> None:
        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {"record_key": FAKE_EMAIL.upper(), "email": FAKE_EMAIL.upper()},
                }
            ]
        )
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(len(table.rows), 1)

    def test_stale_version_never_touches_the_table(self) -> None:
        """`V-权限-12`：旧版本的意图一次外部调用都不发。"""

        table = FakeTable()
        attempt = publish_claim(_claim(version=1, current=2), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)
        self.assertEqual(table.calls, [])
        self.assertEqual(attempt.next_status(), STATUS_SUPERSEDED)

    def test_missing_user_is_superseded_not_failed(self) -> None:
        attempt = publish_claim(_claim(current=None), transport=FakeTable())
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)
        self.assertEqual(attempt.detail, "user_missing")

    def test_readback_mismatch_is_not_published(self) -> None:
        """`V-权限-10`：平台收下的内容与我们决定发布的不同 → 不记发布完成。"""

        table = FakeTable()
        table.mutate_on_write = {"permissions": "{}"}
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("permissions",))
        self.assertFalse(attempt.published)
        self.assertTrue(attempt.retryable)
        self.assertEqual(attempt.failure_kind, PublishFailureKind.DEFINITE)

    def test_definite_rejection_is_not_uncertain(self) -> None:
        table = FakeTable()
        table.faults["create_row"] = PermissionTableError("feishu_code_91403")
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.REJECTED)
        self.assertEqual(attempt.failure_kind, PublishFailureKind.DEFINITE)
        self.assertEqual(attempt.error_code, "feishu_code_91403")

    def test_indeterminate_failure_is_uncertain(self) -> None:
        table = FakeTable()
        table.faults["find_rows"] = PermissionTableError("transport_error", definite=False)
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(attempt.failure_kind, PublishFailureKind.INDETERMINATE)
        self.assertEqual(attempt.action, "none")

    def test_uncertain_readback_retry_converges_to_one_row(self) -> None:
        """新建成功但读回结果不明时，重试按 ``record_key`` 命中并更新，不建第二行。"""

        table = FakeTable()
        table.faults["read_row"] = PermissionTableError("transport_error", definite=False)
        first = publish_claim(_claim(), transport=table)
        self.assertEqual(first.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(first.action, "create")
        self.assertEqual(first.external_record_id, "rec_1")

        second = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(second.published)
        self.assertEqual(second.action, "update")
        self.assertEqual(len(table.rows), 1)

    def test_invalid_payload_is_not_retried(self) -> None:
        attempt = publish_claim(_claim(payload={"record_key": FAKE_EMAIL}), transport=FakeTable())
        self.assertEqual(attempt.outcome, PublishOutcome.INVALID)
        self.assertFalse(attempt.retryable)
        self.assertEqual(attempt.next_status(), STATUS_FAILED)
        # 只记异常类型，不回显快照内容。
        self.assertEqual(attempt.detail, "ValueError")

    def test_unexpected_exception_is_not_swallowed(self) -> None:
        table = FakeTable()
        table.faults["find_rows"] = RuntimeError("未预期缺陷")
        with self.assertRaises(RuntimeError):
            publish_claim(_claim(), transport=table)

    def test_publishing_one_user_leaves_another_row_untouched(self) -> None:
        """`V-权限-03`：改 A 的权限，B 的发布行逐字段不变。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_B",
                    "fields": {
                        "record_key": OTHER_EMAIL,
                        "email": OTHER_EMAIL,
                        "name": "化名乙",
                        "permissions": '{"2001":["运营"]}',
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                    },
                }
            ]
        )
        before = table.snapshot()[0]
        attempt = publish_claim(_claim(), transport=table)
        self.assertTrue(attempt.published)
        self.assertEqual(table.snapshot()[0], before)
        self.assertEqual(len(table.rows), 2)

    def test_published_attempt_cannot_carry_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            PublishAttempt(
                outcome=PublishOutcome.PUBLISHED,
                outbox_id="pub_1",
                user_id="usr_A",
                permission_version=1,
                external_record_id="rec_1",
                mismatch_fields=("email",),
            )

    def test_published_attempt_requires_external_record_id(self) -> None:
        with self.assertRaises(ValueError):
            PublishAttempt(
                outcome=PublishOutcome.PUBLISHED,
                outbox_id="pub_1",
                user_id="usr_A",
                permission_version=1,
            )


class NextStatusTest(unittest.TestCase):
    def _attempt(self, outcome: PublishOutcome, attempts: int) -> PublishAttempt:
        return PublishAttempt(
            outcome=outcome,
            outbox_id="pub_1",
            user_id="usr_A",
            permission_version=1,
            attempts=attempts,
            mismatch_fields=("email",) if outcome is PublishOutcome.MISMATCH else (),
        )

    def test_retryable_returns_to_pending_until_attempts_run_out(self) -> None:
        self.assertEqual(self._attempt(PublishOutcome.UNCERTAIN, 1).next_status(), STATUS_PENDING)
        self.assertEqual(
            self._attempt(PublishOutcome.UNCERTAIN, DEFAULT_MAX_ATTEMPTS).next_status(),
            STATUS_FAILED,
        )

    def test_conflict_never_retries(self) -> None:
        self.assertEqual(self._attempt(PublishOutcome.CONFLICT, 1).next_status(), STATUS_FAILED)

    def test_mismatch_is_retryable_but_bounded(self) -> None:
        self.assertEqual(self._attempt(PublishOutcome.MISMATCH, 1).next_status(), STATUS_PENDING)
        self.assertEqual(
            self._attempt(PublishOutcome.MISMATCH, 9).next_status(max_attempts=3), STATUS_FAILED
        )

    def test_published_and_superseded_are_terminal(self) -> None:
        published = PublishAttempt(
            outcome=PublishOutcome.PUBLISHED,
            outbox_id="pub_1",
            user_id="usr_A",
            permission_version=1,
            external_record_id="rec_1",
        )
        self.assertEqual(published.next_status(), STATUS_PUBLISHED)
        self.assertEqual(self._attempt(PublishOutcome.SUPERSEDED, 1).next_status(), STATUS_SUPERSEDED)


class FakeStore:
    def __init__(self, claims: list[ClaimedPublish], *, fail_complete: bool = False) -> None:
        self._claims = list(claims)
        self.completed: list[tuple[PublishAttempt, str]] = []
        self._fail_complete = fail_complete

    def claim_next(self):
        return self._claims.pop(0) if self._claims else None

    def complete(self, attempt, *, status):
        if self._fail_complete:
            raise RuntimeError("记账失败")
        self.completed.append((attempt, status))


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))


class ExecutorTest(unittest.TestCase):
    def test_run_once_consumes_until_queue_is_empty(self) -> None:
        table = FakeTable()
        store = FakeStore(
            [
                _claim(user_id="usr_A"),
                _claim(row=_row(OTHER_EMAIL), user_id="usr_B"),
            ]
        )
        audit = RecordingAudit()
        alerts: list[PublishAttempt] = []
        executor = PermissionPublishExecutor(
            store=store, transport=table, audit=audit, on_alert=alerts.append
        )
        attempts = executor.run_once()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(item.published for item in attempts))
        self.assertEqual([status for _, status in store.completed], ["published", "published"])
        self.assertEqual(alerts, [])
        self.assertEqual(
            [action for action, _ in audit.entries],
            ["permission_publish.published", "permission_publish.published"],
        )

    def test_failed_attempt_alerts_and_records(self) -> None:
        table = FakeTable()
        table.mutate_on_write = {"name": "被平台改掉的名字"}
        store = FakeStore([_claim()])
        audit = RecordingAudit()
        alerts: list[PublishAttempt] = []
        executor = PermissionPublishExecutor(
            store=store, transport=table, audit=audit, on_alert=alerts.append
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(alerts, [attempt])
        self.assertEqual(alerts[0].alert_kind, "permission_publish_mismatch")
        self.assertEqual(store.completed[0][1], STATUS_PENDING)

    def test_superseded_does_not_alert(self) -> None:
        store = FakeStore([_claim(version=1, current=5)])
        alerts: list[PublishAttempt] = []
        executor = PermissionPublishExecutor(
            store=store, transport=FakeTable(), audit=RecordingAudit(), on_alert=alerts.append
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)
        self.assertEqual(alerts, [])

    def test_complete_failure_is_recorded_then_raised(self) -> None:
        store = FakeStore([_claim()], fail_complete=True)
        audit = RecordingAudit()
        executor = PermissionPublishExecutor(store=store, transport=FakeTable(), audit=audit)
        with self.assertRaises(RuntimeError):
            executor.run_once()
        self.assertEqual(audit.entries[0][0], "permission_publish.complete_failed")
        self.assertEqual(audit.entries[0][1]["error"], "RuntimeError")

    def test_audit_facts_carry_no_personal_values(self) -> None:
        table = FakeTable()
        store = FakeStore([_claim()])
        audit = RecordingAudit()
        PermissionPublishExecutor(store=store, transport=table, audit=audit).run_once()
        rendered = repr(audit.entries)
        self.assertNotIn(FAKE_EMAIL, rendered)
        self.assertNotIn(FAKE_NAME, rendered)

    def test_the_claim_identity_reaches_the_bookkeeping_unchanged(self) -> None:
        """记账要能绑定到**本次认领**，前提是认领标识被如实带下去（P3-1 的非 SQL 那半）。

        ``attempts`` 在每次认领时自增，是"哪一次认领"的天然版本号；store 的
        ``complete`` 用它做守卫（真库用例
        `test_a_stale_completer_cannot_overwrite_the_new_claimer`）。这里钉住的是它在
        编排层不被改写或写死——一旦被写死，那条守卫就永远比对一个假的次数。
        """

        store = FakeStore([_claim(attempts=3)])
        executor = PermissionPublishExecutor(
            store=store, transport=FakeTable(), audit=RecordingAudit()
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.attempts, 3)
        self.assertEqual(store.completed[0][0].attempts, 3)

    def test_limit_bounds_one_round(self) -> None:
        store = FakeStore([_claim(), _claim(), _claim()])
        executor = PermissionPublishExecutor(
            store=store, transport=FakeTable(), audit=RecordingAudit()
        )
        self.assertEqual(len(executor.run_once(limit=2)), 2)

    def test_without_alert_hook_nothing_is_silently_dropped(self) -> None:
        table = FakeTable()
        table.faults["find_rows"] = PermissionTableError("transport_error", definite=False)
        audit = RecordingAudit()
        executor = PermissionPublishExecutor(
            store=FakeStore([_claim()]), transport=table, audit=audit
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(audit.entries[0][0], "permission_publish.uncertain")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
