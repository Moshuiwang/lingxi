"""花名册多维表格只读 adapter 的形状断言（无网络、无凭据）。

真实调用不属于本切片：这里只锁住分页、字段取值与归一化形状，
读取传输由调用方注入，adapter 自身不 import 任何 SDK。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_roster_bitable import (
    ROSTER_FIELD_NAMES,
    RosterRow,
    read_roster_records,
)


class _FakePagedReader:
    """按页返回记录的假传输；记录调用参数以便断言只读分页行为。"""

    def __init__(self, pages: list[tuple[list[dict[str, object]], str | None]]) -> None:
        self._pages = pages
        self.calls: list[str | None] = []

    def list_records(self, page_token: str | None = None) -> tuple[list[dict[str, object]], str | None]:
        self.calls.append(page_token)
        return self._pages[len(self.calls) - 1]


class FeishuRosterBitableTest(unittest.TestCase):
    def test_all_pages_are_read_until_the_page_token_is_exhausted(self) -> None:
        reader = _FakePagedReader(
            [
                ([{"fields": {"人员ID": "fs-u1", "邮箱": "jiaming.jia@example.invalid", "人员姓名": "化名甲"}}], "page-2"),
                ([{"fields": {"人员ID": "fs-u2", "邮箱": "yiming.yi@example.invalid", "人员姓名": "化名乙"}}], None),
            ]
        )

        rows = read_roster_records(reader)

        self.assertEqual(reader.calls, [None, "page-2"])
        self.assertEqual([row.personnel_id for row in rows], ["fs-u1", "fs-u2"])

    def test_object_shaped_cell_values_are_reduced_to_text(self) -> None:
        reader = _FakePagedReader(
            [
                (
                    [
                        {
                            "record_id": "rec1",
                            "fields": {
                                "人员ID": [{"text": "fs-u1"}],
                                "邮箱": [{"text": "jiaming.jia@example.invalid"}],
                                "人员姓名": [{"text": "化名甲"}],
                                "工号": 10001,
                            },
                        }
                    ],
                    None,
                )
            ]
        )

        rows = read_roster_records(reader)

        self.assertEqual(rows[0], RosterRow("fs-u1", "jiaming.jia@example.invalid", "化名甲", "10001", "rec1"))

    def test_duplicate_personnel_id_rows_are_preserved_for_the_matcher(self) -> None:
        reader = _FakePagedReader(
            [
                (
                    [
                        {"fields": {"人员ID": "fs-u1", "邮箱": "a@example.invalid", "人员姓名": "化名甲"}},
                        {"fields": {"人员ID": "fs-u1", "邮箱": "b@example.invalid", "人员姓名": "化名甲"}},
                    ],
                    None,
                )
            ]
        )

        rows = read_roster_records(reader)

        # 花名册实测存在同一人员 ID 的重复行；adapter 不去重，由匹配层判为转人工。
        self.assertEqual(len(rows), 2)

    def test_missing_optional_fields_become_empty_text(self) -> None:
        reader = _FakePagedReader([([{"fields": {"人员ID": "fs-u1"}}], None)])

        rows = read_roster_records(reader)

        self.assertEqual(rows[0].email, "")
        self.assertEqual(rows[0].name, "")

    def test_rows_expose_only_the_fields_the_match_chain_needs(self) -> None:
        self.assertEqual(ROSTER_FIELD_NAMES, ("人员ID", "邮箱", "人员姓名", "工号"))
        self.assertEqual(RosterRow._fields, ("personnel_id", "email", "name", "work_no", "record_id"))

    def test_rows_can_be_handed_to_the_matcher_as_mappings(self) -> None:
        from lingxi.core.permission.account_match import MATCHED, match_galaxy_account

        reader = _FakePagedReader(
            [([{"fields": {"人员ID": "fs-u1", "邮箱": "jiaming.jia@example.invalid", "人员姓名": "化名甲"}}], None)]
        )
        rows = [row._asdict() for row in read_roster_records(reader)]

        result = match_galaxy_account(
            "fs-u1",
            rows,
            [{"user_id": "U1", "email": "jiaming.jia@example.invalid", "nick_name": "化名甲"}],
        )

        self.assertEqual(result.state, MATCHED)
        self.assertEqual(result.galaxy_user_id, "U1")


if __name__ == "__main__":
    unittest.main()
