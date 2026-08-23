"""飞书 OAuth v3 授权的无网络断言。

2026-08-23 #146 清退：本文件此前还覆盖 Bot-Test 测试资产 `adapters/refresh_tokens.py`
的令牌轮换断言（`FakeVault`/`FeishuUserTokenRefresher` 两个用例），随该资产一并清退；
`FeishuOAuthIdentityLoader` 属于保留件 `adapters/oauth_bridge.py`（#67 裁定的 E1 授权
基础设施），其断言原样保留。
"""

from __future__ import annotations

import unittest

from lingxi.adapters.oauth_bridge import FeishuOAuthIdentityLoader, OAuthTokenGrant


class FeishuOAuthV3Test(unittest.TestCase):
    def test_authorization_code_exchange_uses_v3_user_info_and_returns_refresh_grant(self) -> None:
        loader = FeishuOAuthIdentityLoader("cli_test", "secret", "https://example.test/callback")
        seen: list[str] = []

        def fake_json(request: object) -> dict[str, object]:
            url = getattr(request, "full_url")
            seen.append(url)
            if url.endswith("/oauth/token"):
                return {"code": 0, "access_token": "access-only-in-memory", "refresh_token": "rotating-secret", "refresh_token_expires_in": 604800, "scope": "auth:user.id:read offline_access"}
            return {"code": 0, "data": {"open_id": "ou_test", "user_id": "u_test", "union_id": "on_test", "name": "测试用户"}}

        loader._json = fake_json  # type: ignore[method-assign]
        loaded = loader.from_authorization_code("one-time-code")

        self.assertEqual(seen, [
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
        ])
        self.assertEqual(loaded.profile.open_id, "ou_test")
        self.assertEqual(loaded.token_grant, OAuthTokenGrant("rotating-secret", 604800, "auth:user.id:read offline_access"))

    def test_missing_offline_access_grant_rejects_authorization(self) -> None:
        loader = FeishuOAuthIdentityLoader("cli_test", "secret", "https://example.test/callback")
        loader._json = lambda _request: {"code": 0, "access_token": "access-only-in-memory"}  # type: ignore[method-assign]

        with self.assertRaisesRegex(ValueError, "可续期"):
            loader.from_authorization_code("one-time-code")

    def test_visible_organization_entity_uses_user_name_and_open_user_id(self) -> None:
        entity = {"user_name": "王志鹏（王志鹏）", "open_user_id": "ou_visible_user"}

        self.assertIn("王志鹏", FeishuOAuthIdentityLoader._normalized_text(entity["user_name"]))
        self.assertEqual(FeishuOAuthIdentityLoader._entity_identifier(entity, "user"), ("ou_visible_user", "open_id"))

    def test_debug_report_masks_organization_identifiers(self) -> None:
        report = FeishuOAuthIdentityLoader._redact_identifiers({
            "open_id": "ou_visible_user",
            "tenant_key": "tenant-fourda",
            "parent_department_ids": ["od_parent_1", "od_parent_2"],
            "name": "王志鹏",
        })

        self.assertEqual(report, {
            "open_id": "ou_vis…",
            "tenant_key": "tenant…",
            "parent_department_ids": ["od_par…", "od_par…"],
            "name": "王志鹏",
        })

    def test_organization_probe_reads_member_from_nested_department_before_querying_details(self) -> None:
        loader = FeishuOAuthIdentityLoader("cli_test", "secret", "https://example.test/callback")
        seen: list[str] = []

        def fake_optional_json(request: object) -> dict[str, object]:
            url = getattr(request, "full_url")
            seen.append(url)
            if url.endswith("collaboration_tenants?page_size=100"):
                return {"code": 0, "data": {"items": [{"tenant_key": "tenant-fourda", "tenant_name": "四达集团"}]}}
            if "target_department_id=od_root" in url:
                return {"code": 0, "data": {"collaboration_entity_list": [{"user_name": "王志鹏（王志鹏）", "open_user_id": "ou_wang"}], "has_more": False}}
            if "visible_organization?page_size=100" in url:
                return {"code": 0, "data": {"collaboration_entity_list": [{"department_name": "产品部", "open_department_id": "od_root"}], "has_more": False}}
            if "collaboration_departments/od_root" in url:
                return {"code": 0, "data": {"target_department": {"name": "产品部", "open_department_id": "od_root"}}}
            if "collaboration_users/ou_wang" in url:
                return {"code": 0, "data": {"target_user": {"name": "王志鹏", "open_id": "ou_wang", "job_title": "负责人"}}}
            raise AssertionError(url)

        loader._optional_json = fake_optional_json  # type: ignore[method-assign]
        report = loader._organization_probe("in-memory-token")

        self.assertEqual(report["组织遍历"]["已读取分页数"], 2)  # type: ignore[index]
        self.assertEqual(report["组织遍历"]["部门范围下钻数"], 1)  # type: ignore[index]
        self.assertEqual(report["组织遍历"]["可见成员条目数"], 1)  # type: ignore[index]
        self.assertEqual(report["王志鹏"], {"name": "王志鹏", "open_id": "ou_wan…", "job_title": "负责人", "parent_departments": []})
        self.assertTrue(any("target_department_id=od_root" in url for url in seen))
