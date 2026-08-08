#!/usr/bin/env python3
"""用已加密保存的用户续期凭据验证飞书关联组织。

本脚本只允许 Bot-Test 受控环境运行：刷新凭据在内存中解密并立即轮换，
短期 access token 不落盘。终端输出只包含结果数量、命中状态和字段名，
不输出成员资料或可关联身份标识。
"""

from __future__ import annotations

import json
import logging
import os
from urllib.request import Request

from lingxi.adapters.postgres import connect
from lingxi.adapters.oauth_bridge import FeishuOAuthIdentityLoader, OAuthTokenGrant
from lingxi.adapters.refresh_tokens import PostgresRefreshTokenVault


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"缺少 {name}")
    return value


def main() -> int:
    logging.basicConfig(
        filename=os.environ.get("LINGXI_OAUTH_PROBE_LOG_PATH", ".runtime/organization-probe.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    vault = PostgresRefreshTokenVault(required("LINGXI_POSTGRES_DSN"), required("LINGXI_OAUTH_REFRESH_TOKEN_KEY"))
    with connect(vault._dsn, timeouts=vault._timeouts) as connection, connection.cursor() as cursor:  # noqa: SLF001 -- 受控验收读取密文
        cursor.execute("SELECT feishu_open_id, encrypted_refresh_token FROM feishu_user_refresh_token ORDER BY updated_at DESC LIMIT 2")
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError("当前受控库中没有唯一的用户续期凭据，不能猜测查询对象")
    open_id, encrypted = str(rows[0][0]), bytes(rows[0][1])
    refresh_token = vault._cipher.decrypt(encrypted).decode()  # noqa: SLF001 -- 仅内存中用于飞书单次轮换
    response = FeishuOAuthIdentityLoader._json(Request(  # noqa: SLF001 -- 复用固定飞书 HTTPS JSON 读取
        "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
        data=json.dumps({
            "grant_type": "refresh_token",
            "client_id": required("FEISHU_APP_ID"),
            "client_secret": required("FEISHU_APP_SECRET"),
            "refresh_token": refresh_token,
        }).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    ))
    access_token, next_refresh, expires = response.get("access_token"), response.get("refresh_token"), response.get("refresh_token_expires_in")
    if response.get("code") not in (0, "0") or not isinstance(access_token, str) or not isinstance(next_refresh, str) or not isinstance(expires, int):
        raise RuntimeError("飞书未确认本次续期")
    vault.replace(open_id, OAuthTokenGrant(next_refresh, expires, response.get("scope") if isinstance(response.get("scope"), str) else ""))
    report = FeishuOAuthIdentityLoader("probe", "probe", "https://example.test")._organization_probe(access_token)
    wang = report.get("王志鹏")
    print(json.dumps({
        "组织遍历": report.get("组织遍历"),
        "王志鹏是否命中": isinstance(wang, dict),
        "王志鹏可返回字段": sorted(wang) if isinstance(wang, dict) else [],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # 受控终端只报告失败类别，避免异常消息意外携带外部身份资料。
        print(f"probe_failed={type(error).__name__}")
        raise SystemExit(1)
