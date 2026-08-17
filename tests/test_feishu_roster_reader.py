"""花名册分页 reader 与完整性判定的断言（无网络、无凭据）。

真实调用不属于本切片（Issue #52 的 G-READ 判定：专用主体凭据未落盘，真实面等
bootstrap 重授权）。这里全部跑在注入的假传输 / 假分页源上，锁住四件事：

1. 分页游标怎么推进、什么时候停、撞到防御上限时是**失败**而不是"读完了"；
2. 失败分成"明确失败"与"结果不明"两类，且分错方向会变红；
3. 完整性事实（总数对账、必需列形态、重复键、空值行）逐项可判定；
4. 输出与日志里没有任何业务字段值。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from lingxi.adapters.feishu_directory import FeishuDirectoryError
from lingxi.adapters.feishu_roster_bitable import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    BitableRosterPages,
    RosterFailureKind,
    RosterReadError,
    RosterReadStatus,
    RosterRecordPage,
    RosterRow,
    read_roster_records,
    read_roster_snapshot,
)

LOGGER_NAME = "lingxi.adapters.feishu_roster_bitable"

# 固定化名与假标识（验证与门禁十三：测试数据用固定化名）。这几个字符串同时是
# "不得出现在输出 / 审计 / 日志里"的探针值。
FAKE_NAME = "化名甲"
FAKE_EMAIL = "jiaming.jia@example.invalid"
FAKE_EMPLOYEE_NO = "700123"
FAKE_PERSONNEL_ID = "fs-u-0001"
FAKE_TOKEN = "fake-user-access-token-0001"


def _record(
    personnel_id: str | None = FAKE_PERSONNEL_ID,
    *,
    name: str | None = FAKE_NAME,
    email: str | None = FAKE_EMAIL,
    employee_no: str | None = FAKE_EMPLOYEE_NO,
    record_id: str = "rec-0001",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一条原始记录；传 ``None`` 表示该列在这一行上没有值。"""

    fields: dict[str, Any] = {}
    if personnel_id is not None:
        fields["人员ID"] = personnel_id
    if email is not None:
        fields["邮箱"] = email
    if name is not None:
        fields["人员姓名"] = name
    if employee_no is not None:
        fields["工号"] = employee_no
    if extra:
        fields.update(extra)
    return {"record_id": record_id, "fields": fields}


class _FakePageSource:
    """按脚本返回分页的假源；脚本项可以是异常，用来注入失败形态。"""

    def __init__(self, pages: list[Any]) -> None:
        self._pages = list(pages)
        self.calls: list[str | None] = []

    def fetch_page(self, page_token: str | None = None) -> RosterRecordPage:
        self.calls.append(page_token)
        index = len(self.calls) - 1
        if index >= len(self._pages):
            raise AssertionError("分页源被多读了一页")
        page = self._pages[index]
        if isinstance(page, Exception):
            raise page
        return page


class _RecordingTransport:
    """记录每次调用参数的假传输；脚本项可以是异常。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, *, body: Any = None, token: str | None = None
    ) -> Any:
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        index = len(self.calls) - 1
        if index >= len(self._responses):
            raise AssertionError("传输层被多调了一次")
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response


class _CountingToken:
    """假的「已就绪凭据」provider：只数被取了几次，不做任何 I/O。"""

    def __init__(self, token: Any = FAKE_TOKEN, error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.token


def _page(records: list[Any], next_page_token: str | None = None, total: int | None = None):
    return RosterRecordPage(tuple(records), next_page_token, total)


def _bitable_response(items: list[Any], *, has_more: bool = False, page_token: str | None = None, total: Any = None):
    data: dict[str, Any] = {"items": items, "has_more": has_more}
    if page_token is not None:
        data["page_token"] = page_token
    if total is not None:
        data["total"] = total
    return {"code": 0, "msg": "success", "data": data}


class RosterPaginationTest(unittest.TestCase):
    """分页游标推进与防御上限。"""

    def test_pages_are_read_until_the_cursor_is_exhausted(self) -> None:
        source = _FakePageSource(
            [
                _page([_record("fs-u1", record_id="rec-1")], "page-2"),
                _page([_record("fs-u2", record_id="rec-2")], "page-3"),
                _page([_record("fs-u3", record_id="rec-3")], None, total=3),
            ]
        )

        outcome = read_roster_snapshot(source)

        self.assertEqual(source.calls, [None, "page-2", "page-3"])
        self.assertEqual(outcome.status, RosterReadStatus.COMPLETE)
        self.assertEqual([row.personnel_id for row in outcome.rows], ["fs-u1", "fs-u2", "fs-u3"])
        self.assertEqual(outcome.integrity.pages_read, 3)

    def test_single_page_round_needs_exactly_one_call(self) -> None:
        source = _FakePageSource([_page([_record()], None, total=1)])

        outcome = read_roster_snapshot(source)

        self.assertEqual(source.calls, [None])
        self.assertTrue(outcome.complete_nonempty)

    def test_hitting_the_page_limit_is_a_failure_not_a_finished_round(self) -> None:
        # 上限是防御：撞上它说明游标没有正常收敛，此时"已经读到的行"不构成一轮。
        source = _FakePageSource([_page([_record()], "page-2"), _page([_record()], "page-3")])

        outcome = read_roster_snapshot(source, max_pages=2)

        self.assertEqual(outcome.status, RosterReadStatus.FAILED)
        assert outcome.failure is not None
        self.assertEqual(outcome.failure.code, "pagination_limit")
        self.assertEqual(outcome.failure.kind, RosterFailureKind.INDETERMINATE)
        self.assertEqual(outcome.failure.partial_pages, 2)
        self.assertEqual(outcome.failure.partial_rows, 2)

    def test_a_cursor_that_does_not_advance_stops_the_round(self) -> None:
        # 游标原地打转时继续读只会把同一批行滚进结果，行数越读越多而永远不结束。
        source = _FakePageSource([_page([_record()], "page-2"), _page([_record()], "page-2")])

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.status, RosterReadStatus.FAILED)
        assert outcome.failure is not None
        self.assertEqual(outcome.failure.code, "pagination_stalled")

    def test_partial_rows_are_never_returned_as_data(self) -> None:
        # 半轮的行如果照样返回，快照层就会拿一份"看起来正常、其实骤减"的数据去替换。
        source = _FakePageSource(
            [_page([_record("fs-u1"), _record("fs-u2")], "page-2"), RosterReadError("transport_error", definite=False)]
        )

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.rows, ())
        self.assertEqual(outcome.integrity.row_count, 0)
        assert outcome.failure is not None
        self.assertEqual(outcome.failure.partial_rows, 2)

    def test_max_pages_must_be_a_positive_integer(self) -> None:
        with self.assertRaises(ValueError):
            read_roster_snapshot(_FakePageSource([]), max_pages=0)


class RosterFailureClassificationTest(unittest.TestCase):
    """明确失败 vs 结果不明。分错方向会让告警指向错误的原因。"""

    def _outcome_for(self, error: Exception):
        return read_roster_snapshot(_FakePageSource([error]))

    def test_business_error_code_is_a_definite_failure(self) -> None:
        # 服务端完整返回并明确拒绝：权限被回收、Base 被删这类要人介入的情况。
        outcome = self._outcome_for(RosterReadError("feishu_code_91403"))

        assert outcome.failure is not None
        self.assertEqual(outcome.failure.kind, RosterFailureKind.DEFINITE)
        self.assertTrue(outcome.failure.definite)
        self.assertEqual(outcome.failure.code, "feishu_code_91403")

    def test_transport_error_is_indeterminate(self) -> None:
        outcome = self._outcome_for(RosterReadError("transport_error", definite=False))

        assert outcome.failure is not None
        self.assertEqual(outcome.failure.kind, RosterFailureKind.INDETERMINATE)
        self.assertFalse(outcome.failure.definite)

    def test_pagination_stalled_is_indeterminate_because_the_row_total_is_unknown(self) -> None:
        outcome = self._outcome_for(RosterReadError("pagination_stalled", definite=False))

        assert outcome.failure is not None
        self.assertEqual(outcome.failure.kind, RosterFailureKind.INDETERMINATE)

    def test_page_limit_is_indeterminate(self) -> None:
        outcome = self._outcome_for(RosterReadError("pagination_limit", definite=False))

        assert outcome.failure is not None
        self.assertEqual(outcome.failure.kind, RosterFailureKind.INDETERMINATE)

    def test_every_failure_status_is_failed_regardless_of_kind(self) -> None:
        for error in (RosterReadError("feishu_code_1254005"), RosterReadError("invalid_response_shape", definite=False)):
            with self.subTest(code=error.code):
                self.assertEqual(self._outcome_for(error).status, RosterReadStatus.FAILED)

    def test_unexpected_exceptions_are_not_swallowed_into_a_read_failure(self) -> None:
        # 未预期异常被吞成"源头读取失败"，会让真正的缺陷每天安静地重试下去。
        source = _FakePageSource([ZeroDivisionError("实现缺陷")])

        with self.assertRaises(ZeroDivisionError):
            read_roster_snapshot(source)

    def test_non_mapping_records_are_rejected_instead_of_silently_dropped(self) -> None:
        source = _FakePageSource([_page([_record(), "不是对象"], None)])

        outcome = read_roster_snapshot(source)

        assert outcome.failure is not None
        self.assertEqual(outcome.failure.code, "invalid_page_item")
        self.assertEqual(outcome.rows, ())


class RosterIntegrityTest(unittest.TestCase):
    """完整性判定：空源、总数对账、必需列形态、缺字段行、重复键。"""

    def test_empty_source_is_its_own_status_and_never_complete(self) -> None:
        # 空源不得被当成"整轮读完的花名册"，否则比对层会把全体已开通用户报成移除。
        outcome = read_roster_snapshot(_FakePageSource([_page([], None, total=0)]))

        self.assertEqual(outcome.status, RosterReadStatus.EMPTY_SOURCE)
        self.assertFalse(outcome.complete_nonempty)
        self.assertEqual(outcome.rows, ())
        self.assertIsNone(outcome.failure)

    def test_empty_source_does_not_report_column_drift(self) -> None:
        # 零行时没有判定列是否存在的证据；报成"整列缺失"会把空源解释成表结构变了。
        outcome = read_roster_snapshot(_FakePageSource([_page([], None)]))

        self.assertEqual(outcome.integrity.absent_columns, ())

    def test_source_claiming_rows_while_returning_none_is_incomplete_not_empty(self) -> None:
        # 源头自报 1206 行却一行没给：两者都保旧，但报给管理员的原因不能反。
        outcome = read_roster_snapshot(_FakePageSource([_page([], None, total=1206)]))

        self.assertEqual(outcome.status, RosterReadStatus.INCOMPLETE)
        self.assertIs(outcome.integrity.total_matches_rows, False)

    def test_reported_total_matching_accumulated_rows_is_complete(self) -> None:
        source = _FakePageSource(
            [_page([_record("fs-u1"), _record("fs-u2")], "page-2", total=3), _page([_record("fs-u3")], None, total=3)]
        )

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.status, RosterReadStatus.COMPLETE)
        self.assertEqual(outcome.integrity.reported_total, 3)
        self.assertIs(outcome.integrity.total_matches_rows, True)

    def test_reported_total_below_accumulated_rows_is_incomplete(self) -> None:
        source = _FakePageSource([_page([_record("fs-u1"), _record("fs-u2")], None, total=1)])

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.status, RosterReadStatus.INCOMPLETE)
        self.assertIs(outcome.integrity.total_matches_rows, False)
        # 行还在：这一轮分页确实读完了，是源头自己的账对不上。可用性由状态承担。
        self.assertEqual(len(outcome.rows), 2)
        self.assertFalse(outcome.complete_nonempty)

    def test_missing_total_is_recorded_as_unknown_not_as_a_passed_check(self) -> None:
        # 飞书是否一定返回 total 未回源确认；缺失时不能假装校验过。
        outcome = read_roster_snapshot(_FakePageSource([_page([_record()], None)]))

        self.assertIsNone(outcome.integrity.reported_total)
        self.assertIsNone(outcome.integrity.total_matches_rows)
        self.assertEqual(outcome.status, RosterReadStatus.COMPLETE)

    def test_a_required_column_absent_from_every_row_is_incomplete(self) -> None:
        # 列被改名或删掉的信号：归一后整列取不到值，比对层会把它当成"全员该字段变空"。
        source = _FakePageSource([_page([_record(employee_no=None), _record("fs-u2", employee_no=None)], None)])

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.status, RosterReadStatus.INCOMPLETE)
        self.assertEqual(outcome.integrity.absent_columns, ("工号",))

    def test_individual_rows_missing_a_field_stay_complete(self) -> None:
        # 受控读取实测：1206 行里 1197 行有工号。个别行缺字段是常态，只上报计数。
        source = _FakePageSource(
            [_page([_record("fs-u1"), _record("fs-u2", employee_no=None), _record("fs-u3")], None)]
        )

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.status, RosterReadStatus.COMPLETE)
        self.assertEqual(dict(outcome.integrity.blank_column_rows)["工号"], 1)
        self.assertEqual(outcome.integrity.absent_columns, ())

    def test_rows_without_personnel_id_are_counted(self) -> None:
        source = _FakePageSource([_page([_record("fs-u1"), _record(None, record_id="rec-2")], None)])

        outcome = read_roster_snapshot(source)

        self.assertEqual(outcome.integrity.rows_without_personnel_id, 1)

    def test_duplicate_personnel_id_rows_are_counted_without_deduplication(self) -> None:
        # 实测存在同一人员 ID 多行；reader 不去重（比对层判 AMBIGUOUS），只报计数。
        source = _FakePageSource(
            [
                _page(
                    [
                        _record("fs-u1", record_id="rec-1"),
                        _record("fs-u1", record_id="rec-2", employee_no="700124"),
                        _record("fs-u2", record_id="rec-3", employee_no="700125"),
                    ],
                    None,
                )
            ]
        )

        outcome = read_roster_snapshot(source)

        duplicates = {item.field: item for item in outcome.integrity.duplicates}
        self.assertEqual(duplicates["personnel_id"].keys, 1)
        self.assertEqual(duplicates["personnel_id"].rows, 2)
        self.assertEqual(len(outcome.rows), 3)
        # 重复不是形态问题：它不应该让整轮快照停更。
        self.assertEqual(outcome.status, RosterReadStatus.COMPLETE)

    def test_duplicate_employee_numbers_are_counted_on_their_own_key(self) -> None:
        source = _FakePageSource(
            [
                _page(
                    [
                        _record("fs-u1", record_id="rec-1", employee_no="700200"),
                        _record("fs-u2", record_id="rec-2", employee_no="700200"),
                        _record("fs-u3", record_id="rec-3", employee_no="700201"),
                    ],
                    None,
                )
            ]
        )

        outcome = read_roster_snapshot(source)

        duplicates = {item.field: item for item in outcome.integrity.duplicates}
        self.assertEqual(duplicates["employee_no"].keys, 1)
        self.assertEqual(duplicates["employee_no"].rows, 2)
        self.assertEqual(duplicates["personnel_id"].keys, 0)

    def test_blank_keys_are_not_counted_as_duplicates(self) -> None:
        # 九行没有工号不是"一组九行重复"。把空值算进去会让每一轮都报一个不存在的问题。
        source = _FakePageSource(
            [
                _page(
                    [
                        _record("fs-u1", record_id="rec-1", employee_no=None),
                        _record("fs-u2", record_id="rec-2", employee_no=None),
                        _record("fs-u3", record_id="rec-3", employee_no=None),
                    ],
                    None,
                )
            ]
        )

        outcome = read_roster_snapshot(source)

        duplicates = {item.field: item for item in outcome.integrity.duplicates}
        self.assertEqual(duplicates["employee_no"].keys, 0)
        self.assertEqual(duplicates["employee_no"].rows, 0)

    def test_identifiers_are_compared_on_their_full_value(self) -> None:
        # 前 6 位相同、完整值不同的两个 ID 不得被算成同一个人（V-花名册-05 同一口径）。
        source = _FakePageSource(
            [_page([_record("ou_abc123456789", record_id="rec-1"), _record("ou_abc123999999", record_id="rec-2")], None)]
        )

        outcome = read_roster_snapshot(source)

        duplicates = {item.field: item for item in outcome.integrity.duplicates}
        self.assertEqual(duplicates["personnel_id"].keys, 0)

    def test_required_columns_are_configurable(self) -> None:
        source = _FakePageSource([_page([_record(extra={"部门": "化名部门"})], None)])

        outcome = read_roster_snapshot(source, required_columns=("人员ID", "部门"))

        self.assertEqual([item.column for item in outcome.integrity.blank_column_rows], ["人员ID", "部门"])
        self.assertEqual(outcome.integrity.absent_columns, ())

    def test_configured_columns_do_not_widen_what_the_rows_keep(self) -> None:
        # 形态校验可以看任意一列，但保留下来的仍然只有匹配链需要的四个字段。
        source = _FakePageSource([_page([_record(extra={"部门": "化名部门", "账号状态": "在职"})], None)])

        outcome = read_roster_snapshot(source, required_columns=("人员ID", "部门", "账号状态"))

        self.assertEqual(RosterRow._fields, ("personnel_id", "email", "name", "employee_no", "record_id"))
        self.assertNotIn("化名部门", repr(outcome.rows))
        self.assertNotIn("在职", repr(outcome.rows))

    def test_row_count_always_matches_the_returned_rows(self) -> None:
        for pages, expected in (
            ([_page([_record()], None)], 1),
            ([_page([], None)], 0),
            ([RosterReadError("transport_error", definite=False)], 0),
        ):
            with self.subTest(expected=expected):
                outcome = read_roster_snapshot(_FakePageSource(pages))
                self.assertEqual(outcome.integrity.row_count, len(outcome.rows))
                self.assertEqual(outcome.integrity.row_count, expected)


class RosterAuditDisciplineTest(unittest.TestCase):
    """输出与日志不含业务字段值。"""

    def _sensitive_values(self) -> tuple[str, ...]:
        return (FAKE_NAME, FAKE_EMAIL, FAKE_EMPLOYEE_NO, FAKE_PERSONNEL_ID)

    def test_audit_facts_carry_counts_and_codes_only(self) -> None:
        source = _FakePageSource([_page([_record(), _record("fs-u2", record_id="rec-2")], None, total=2)])

        outcome = read_roster_snapshot(source)
        serialized = json.dumps(outcome.audit_facts(), ensure_ascii=False)

        for value in self._sensitive_values():
            self.assertNotIn(value, serialized)
        self.assertEqual(outcome.audit_facts()["rows"], 2)
        self.assertEqual(outcome.audit_facts()["status"], "complete")

    def test_failure_audit_names_the_code_but_no_row_data(self) -> None:
        source = _FakePageSource([_page([_record()], "page-2"), RosterReadError("feishu_code_91403")])

        outcome = read_roster_snapshot(source)
        serialized = json.dumps(outcome.audit_facts(), ensure_ascii=False)

        self.assertIn("feishu_code_91403", serialized)
        self.assertEqual(outcome.audit_facts()["failure_kind"], "definite")
        for value in self._sensitive_values():
            self.assertNotIn(value, serialized)

    def test_the_round_summary_log_contains_no_field_values(self) -> None:
        source = _FakePageSource([_page([_record()], None, total=1)])

        with self.assertLogs(LOGGER_NAME, level="INFO") as captured:
            read_roster_snapshot(source)

        text = "\n".join(captured.output)
        for value in self._sensitive_values():
            self.assertNotIn(value, text)


class BitableRosterPagesTest(unittest.TestCase):
    """分页传输：URL 形状、凭据注入、错误翻译。"""

    def _pages(self, transport: Any, token: Any = None, **overrides: Any) -> BitableRosterPages:
        parameters: dict[str, Any] = {
            "base_url": "https://open.feishu.example.invalid/open-apis",
            "app_token": "bascnFakeAppToken",
            "table_id": "tblFakeTableId",
            "access_token": token if token is not None else _CountingToken(),
            "transport": transport,
        }
        parameters.update(overrides)
        return BitableRosterPages(**parameters)

    def test_construction_touches_no_transport_and_no_credential(self) -> None:
        # 可注入面的底线：构造不建 client、不发请求、不取凭据（V-花名册-27 同口径）。
        transport = _RecordingTransport([])
        token = _CountingToken()

        self._pages(transport, token)

        self.assertEqual(transport.calls, [])
        self.assertEqual(token.calls, 0)

    def test_first_page_url_carries_base_table_and_page_size(self) -> None:
        transport = _RecordingTransport([_bitable_response([_record()], total=1)])

        page = self._pages(transport).fetch_page()

        url = transport.calls[0]["url"]
        self.assertIn("/bitable/v1/apps/bascnFakeAppToken/tables/tblFakeTableId/records?", url)
        self.assertIn(f"page_size={DEFAULT_PAGE_SIZE}", url)
        self.assertNotIn("page_token", url)
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(page.total, 1)

    def test_page_size_is_configurable_within_the_local_bound(self) -> None:
        transport = _RecordingTransport([_bitable_response([])])

        self._pages(transport, page_size=200).fetch_page()

        self.assertIn("page_size=200", transport.calls[0]["url"])

    def test_page_size_outside_the_bound_fails_at_construction(self) -> None:
        for size in (0, -1, MAX_PAGE_SIZE + 1, True, "500"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self._pages(_RecordingTransport([]), page_size=size)

    def test_cursor_is_sent_back_on_the_next_page(self) -> None:
        transport = _RecordingTransport(
            [
                _bitable_response([_record()], has_more=True, page_token="page-2"),
                _bitable_response([_record("fs-u2", record_id="rec-2")]),
            ]
        )
        pages = self._pages(transport)

        first = pages.fetch_page()
        second = pages.fetch_page(first.next_page_token)

        self.assertEqual(first.next_page_token, "page-2")
        self.assertIn("page_token=page-2", transport.calls[1]["url"])
        self.assertIsNone(second.next_page_token)

    def test_has_more_without_a_usable_cursor_is_a_stalled_round(self) -> None:
        for data in ({}, {"page_token": ""}, {"page_token": "page-1"}):
            with self.subTest(data=data):
                transport = _RecordingTransport([_bitable_response([], has_more=True, **data)])
                with self.assertRaises(RosterReadError) as raised:
                    self._pages(transport).fetch_page("page-1")
                self.assertEqual(raised.exception.code, "pagination_stalled")
                self.assertFalse(raised.exception.definite)

    def test_business_error_code_becomes_a_definite_read_error(self) -> None:
        transport = _RecordingTransport([{"code": 91403, "msg": "Forbidden"}])

        with self.assertRaises(RosterReadError) as raised:
            self._pages(transport).fetch_page()

        self.assertEqual(raised.exception.code, "feishu_code_91403")
        self.assertTrue(raised.exception.definite)

    def test_transport_layer_errors_keep_their_indeterminate_meaning(self) -> None:
        # 目录 adapter 的错误在边界上被翻译成花名册自己的类型，definite 语义原样保留。
        transport = _RecordingTransport([FeishuDirectoryError("transport_error")])

        with self.assertRaises(RosterReadError) as raised:
            self._pages(transport).fetch_page()

        self.assertEqual(raised.exception.code, "transport_error")
        self.assertFalse(raised.exception.definite)

    def test_malformed_responses_are_indeterminate(self) -> None:
        cases = {
            "invalid_response_shape": ["不是对象"],
            "items_missing": [{"code": 0, "data": {"items": "不是列表"}}],
            "invalid_page_item": [{"code": 0, "data": {"items": ["不是对象"], "has_more": False}}],
        }
        for code, responses in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(RosterReadError) as raised:
                    self._pages(_RecordingTransport(responses)).fetch_page()
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(raised.exception.definite)

    def test_a_non_integer_total_is_treated_as_absent(self) -> None:
        transport = _RecordingTransport([_bitable_response([_record()], total="1206")])

        self.assertIsNone(self._pages(transport).fetch_page().total)

    def test_the_ready_token_goes_to_the_header_and_never_into_the_url(self) -> None:
        transport = _RecordingTransport([_bitable_response([]), _bitable_response([])])
        token = _CountingToken()
        pages = self._pages(transport, token)

        pages.fetch_page()
        pages.fetch_page()

        self.assertEqual([call["token"] for call in transport.calls], [FAKE_TOKEN, FAKE_TOKEN])
        # 每页现取一次：整轮读取中途轮换凭据时，下一页自然用上新值。
        self.assertEqual(token.calls, 2)
        for call in transport.calls:
            self.assertNotIn(FAKE_TOKEN, call["url"])

    def test_an_empty_ready_token_is_an_indeterminate_read_failure(self) -> None:
        transport = _RecordingTransport([])

        with self.assertRaises(RosterReadError) as raised:
            self._pages(transport, _CountingToken(token="")).fetch_page()

        self.assertEqual(raised.exception.code, "access_token_missing")
        self.assertFalse(raised.exception.definite)
        self.assertEqual(transport.calls, [])

    def test_a_failing_credential_provider_is_not_disguised_as_a_source_failure(self) -> None:
        # 拿不到凭据是本侧的配置问题；折成"花名册读取失败"会把排查引向花名册。
        transport = _RecordingTransport([])

        with self.assertRaises(LookupError):
            self._pages(transport, _CountingToken(error=LookupError("凭据未就绪"))).fetch_page()

        self.assertEqual(transport.calls, [])

    def test_configuration_errors_do_not_echo_the_configured_value(self) -> None:
        secret_shaped = "bascnRealLookingAppToken"
        for overrides in (
            {"base_url": "http://open.feishu.example.invalid"},
            {"app_token": " "},
            {"app_token": "bascnRealLooking AppToken"},
            {"table_id": "tblRealLooking\tTableId"},
            {"app_token": None},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError) as raised:
                    self._pages(_RecordingTransport([]), **overrides)
                self.assertNotIn(secret_shaped, str(raised.exception))
                for value in overrides.values():
                    if isinstance(value, str) and value.strip():
                        self.assertNotIn(value, str(raised.exception))

    def test_access_token_must_be_callable(self) -> None:
        with self.assertRaises(ValueError):
            self._pages(_RecordingTransport([]), access_token=FAKE_TOKEN)

    def test_the_pages_object_still_satisfies_the_existing_record_page_reader(self) -> None:
        # 既有装配点按 `RecordPageReader` 接线；同一个对象要能直接交给它，
        # 不必为两条读取路径各接一次线。
        transport = _RecordingTransport(
            [
                _bitable_response([_record()], has_more=True, page_token="page-2"),
                _bitable_response([_record("fs-u2", record_id="rec-2")]),
            ]
        )

        rows = read_roster_records(self._pages(transport))

        self.assertEqual([row.personnel_id for row in rows], [FAKE_PERSONNEL_ID, "fs-u2"])

    def test_a_full_round_through_the_transport_reaches_a_complete_outcome(self) -> None:
        transport = _RecordingTransport(
            [
                _bitable_response([_record()], has_more=True, page_token="page-2", total=2),
                _bitable_response([_record("fs-u2", record_id="rec-2", employee_no="700124")], total=2),
            ]
        )

        outcome = read_roster_snapshot(self._pages(transport))

        self.assertEqual(outcome.status, RosterReadStatus.COMPLETE)
        self.assertEqual(outcome.integrity.pages_read, 2)
        self.assertIs(outcome.integrity.total_matches_rows, True)
        self.assertTrue(outcome.complete_nonempty)


if __name__ == "__main__":
    unittest.main()
