"""飞书组织快照与多维表格关联规则的纯函数断言。"""

from __future__ import annotations

import unittest

from lingxi.adapters.feishu_bitable_association import analyze_association


class FeishuBitableOrgAssociationTest(unittest.TestCase):
    def test_personnel_id_is_exact_user_id_join_and_open_id_mismatch_is_not_joined(self) -> None:
        snapshot_rows = [
            {"tenant_key": "tenant-a", "user_id": "user-a", "open_id": "open-a", "name": "甲"},
            {"tenant_key": "tenant-a", "user_id": "user-b", "open_id": "open-b", "name": "乙"},
        ]
        bitable_rows = [
            {"人员ID": "user-a", "open_id": "unrelated-open", "人员姓名": "甲", "工号": "1001"},
            {"人员ID": "user-a", "open_id": "unrelated-open-2", "人员姓名": "甲", "工号": "1001"},
            {"人员ID": "not-in-snapshot", "open_id": "open-a", "人员姓名": "甲", "工号": "1002"},
        ]

        report = analyze_association(bitable_rows, snapshot_rows)

        self.assertEqual(report["joins"]["personnel_id_to_user_id"], {"matched_rows": 2, "matched_values": 1})
        self.assertEqual(report["joins"]["open_id_to_open_id"], {"matched_rows": 1, "matched_values": 1})
        self.assertEqual(report["joins"]["both_keys_same_member"], {"rows_with_both_keys": 0, "same_member_rows": 0})

    def test_name_only_match_is_reported_as_ambiguous_without_becoming_a_primary_join(self) -> None:
        snapshot_rows = [
            {"tenant_key": "tenant-a", "user_id": "user-a", "open_id": "open-a", "name": "重名"},
            {"tenant_key": "tenant-b", "user_id": "user-b", "open_id": "open-b", "name": "重名"},
        ]
        bitable_rows = [{"人员ID": "", "open_id": "", "人员姓名": "重名", "工号": "1001"}]

        report = analyze_association(bitable_rows, snapshot_rows)

        self.assertEqual(report["joins"]["name_only"], {
            "matched_rows": 1,
            "unique_snapshot_name_matches": 0,
            "ambiguous_snapshot_name_matches": 1,
        })
        self.assertEqual(report["joins"]["personnel_id_to_user_id"], {"matched_rows": 0, "matched_values": 0})

    def test_bitable_values_can_be_read_from_text_objects_and_snapshot_name_objects(self) -> None:
        snapshot_rows = [
            {
                "tenant_key": "tenant-a",
                "user_id": "user-a",
                "open_id": "open-a",
                "name": {"default_value": "甲"},
            },
        ]
        bitable_rows = [
            {
                "人员ID": [{"text": "user-a"}],
                "open_id": [{"text": "open-a"}],
                "人员姓名": [{"text": "甲"}],
                "工号": 1001,
            },
        ]

        report = analyze_association(bitable_rows, snapshot_rows)

        self.assertEqual(report["joins"]["both_keys_same_member"], {"rows_with_both_keys": 1, "same_member_rows": 1})


if __name__ == "__main__":
    unittest.main()
