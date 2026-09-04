"""花名册多维表格只读 adapter 的归一化形状断言（无网络、无凭据）。

真实调用不属于本切片：这里只锁住分页、字段取值与归一化形状，
读取传输由调用方注入，adapter 自身不 import 任何 SDK。

**入口只有一个**：`read_roster_snapshot`。legacy `read_roster_records` 已随 S-B-04 的
日报接线删除（PR #208 二级审查 P2-1），因此本文件的归一化断言也改从唯一入口进——
这些形状必须在真正被使用的那条路径上成立，而不是只在一条已经没人调用的路径上成立。
分页失败语义、四态判定与完整性事实在 `tests/test_feishu_roster_reader.py`。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_roster_bitable import (
    ROSTER_FIELD_NAMES,
    RosterReadStatus,
    RosterRecordPage,
    RosterRow,
    read_roster_snapshot,
)


class _FakePageSource:
    """按页返回原始记录的假传输；记录调用参数以便断言只读分页行为。"""

    def __init__(self, pages: list[tuple[list[dict[str, object]], str | None]]) -> None:
        self._pages = pages
        self.calls: list[str | None] = []

    def fetch_page(self, page_token: str | None = None) -> RosterRecordPage:
        self.calls.append(page_token)
        records, next_page_token = self._pages[len(self.calls) - 1]
        return RosterRecordPage(tuple(records), next_page_token, None)


def _rows(pages: list[tuple[list[dict[str, object]], str | None]]) -> tuple[RosterRow, ...]:
    outcome = read_roster_snapshot(_FakePageSource(pages))
    assert outcome.status is not RosterReadStatus.FAILED, outcome.failure
    return outcome.rows


class FeishuRosterBitableTest(unittest.TestCase):
    def test_all_pages_are_read_until_the_page_token_is_exhausted(self) -> None:
        source = _FakePageSource(
            [
                (
                    [
                        {
                            "fields": {
                                "人员ID": "fs-u1",
                                "邮箱": "jiaming.jia@example.invalid",
                                "人员姓名": "化名甲",
                            }
                        }
                    ],
                    "page-2",
                ),
                (
                    [
                        {
                            "fields": {
                                "人员ID": "fs-u2",
                                "邮箱": "yiming.yi@example.invalid",
                                "人员姓名": "化名乙",
                            }
                        }
                    ],
                    None,
                ),
            ]
        )

        outcome = read_roster_snapshot(source)

        self.assertEqual(source.calls, [None, "page-2"])
        self.assertEqual([row.personnel_id for row in outcome.rows], ["fs-u1", "fs-u2"])
        self.assertEqual(outcome.integrity.pages_read, 2)

    def test_object_shaped_cell_values_are_reduced_to_text(self) -> None:
        rows = _rows(
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

        self.assertEqual(
            rows[0], RosterRow("fs-u1", "jiaming.jia@example.invalid", "化名甲", "10001", "rec1")
        )

    def test_duplicate_personnel_id_rows_are_preserved_for_the_matcher(self) -> None:
        rows = _rows(
            [
                (
                    [
                        {
                            "fields": {
                                "人员ID": "fs-u1",
                                "邮箱": "a@example.invalid",
                                "人员姓名": "化名甲",
                            }
                        },
                        {
                            "fields": {
                                "人员ID": "fs-u1",
                                "邮箱": "b@example.invalid",
                                "人员姓名": "化名甲",
                            }
                        },
                    ],
                    None,
                )
            ]
        )

        # 花名册实测存在同一人员 ID 的重复行；adapter 不去重，由匹配层统一判为无可用权限。
        self.assertEqual(len(rows), 2)

    def test_missing_optional_fields_become_empty_text(self) -> None:
        rows = _rows([([{"fields": {"人员ID": "fs-u1"}}], None)])

        self.assertEqual(rows[0].email, "")
        self.assertEqual(rows[0].name, "")

    def test_rows_expose_only_the_fields_the_match_chain_needs(self) -> None:
        self.assertEqual(ROSTER_FIELD_NAMES, ("人员ID", "邮箱", "人员姓名", "工号"))
        self.assertEqual(
            RosterRow._fields, ("personnel_id", "email", "name", "employee_no", "record_id")
        )

    def test_rows_can_be_handed_to_the_matcher_as_mappings(self) -> None:
        from lingxi.core.permission.account_match import MATCHED, match_galaxy_account

        # 不做 _asdict：接线声明的是「行可直接交给匹配器」，就按原样传（终轮 Codex）。
        rows = list(
            _rows(
                [
                    (
                        [
                            {
                                "fields": {
                                    "人员ID": "fs-u1",
                                    "邮箱": "jiaming.jia@example.invalid",
                                    "人员姓名": "化名甲",
                                    "工号": "10001",
                                }
                            }
                        ],
                        None,
                    )
                ]
            )
        )

        result = match_galaxy_account(
            "fs-u1",
            rows,
            [
                {
                    "user_id": "U1",
                    "user_name": "10001",
                    "email": "jiaming.jia@example.invalid",
                    "nick_name": "化名甲",
                }
            ],
        )

        self.assertEqual(result.state, MATCHED)
        # 独立复查发现的接线陷阱：字段名对不上时工号会静默丢失、整体退化为纯
        # 邮箱匹配，而只断言 MATCHED 抓不到。必须钉住「确实是按工号命中的」。
        self.assertEqual(result.matched_key, "employee_no")
        self.assertEqual(result.galaxy_user_id, "U1")


if __name__ == "__main__":
    unittest.main()
