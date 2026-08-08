#!/usr/bin/env python3
"""受控环境中的「四达文档会议助手」正式重授权入口。

这是一次性初始化/恢复操作，不是常驻服务，也不是普通员工入口。授权码只通过
交互式标准输入回读，不放进命令行参数；终端只打印授权地址、结果类别和安全
提示，不打印任何令牌或回调参数原值。

需要由受控环境注入的变量：

* ``LINGXI_POSTGRES_DSN``
* ``LINGXI_DELEGATED_CREDENTIAL_KEY``
* ``LINGXI_DELEGATED_CREDENTIAL_PATH``
* ``LINGXI_FEISHU_APP_ID`` / ``LINGXI_FEISHU_APP_SECRET``
* ``LINGXI_FEISHU_BASE_URL``
* ``LINGXI_FEISHU_REDIRECT_URI``
* ``LINGXI_FEISHU_AUTHORIZATION_ENDPOINT``
* ``LINGXI_FEISHU_SCOPE``（必须含 ``offline_access``）

可选变量：``LINGXI_DELEGATED_REAUTH_STATE_PATH``、
``LINGXI_DELEGATED_REAUTH_STATE_KEY``、``LINGXI_DELEGATED_SUBJECT_OPEN_ID``。
未提供主体时从正式 ``feishu_delegated_subject`` 登记表读取，不能从回调参数猜测。
"""

from __future__ import annotations

import os
import sys
from urllib.parse import parse_qs, urlparse

from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
from lingxi.adapters.feishu_reauthorization import (
    FeishuReauthorizationEntry,
    HostFileAuthorizationStateStore,
)


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需的环境变量：{name}")
    return value


def parse_callback_url(raw_url: str) -> tuple[str, str | None, str | None]:
    """只提取回调所需字段；错误消息不回显用户粘贴的 URL。"""

    try:
        parsed = urlparse(raw_url.strip())
    except ValueError as error:
        raise ValueError("回调地址格式无效") from error
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("回调地址必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError("回调地址不得包含 URL fragment")
    query = parse_qs(parsed.query, keep_blank_values=True)

    def one(name: str) -> str | None:
        values = query.get(name, [])
        if len(values) != 1 or not values[0]:
            return None
        return values[0]

    state, code, error = one("state"), one("code"), one("error")
    if state is None or (code is not None and error is not None) or (code is None and error is None):
        raise ValueError("回调必须包含一个 state 和一个授权结果")
    return state, code, error


def main() -> int:
    try:
        credential_path = required("LINGXI_DELEGATED_CREDENTIAL_PATH")
        credential_key = required("LINGXI_DELEGATED_CREDENTIAL_KEY")
        vault = HostFileDelegatedCredentialVault(
            required("LINGXI_POSTGRES_DSN"),
            credential_key,
            credential_path,
        )
        state_path = os.environ.get("LINGXI_DELEGATED_REAUTH_STATE_PATH", "").strip() or credential_path + ".reauth-state"
        state_key = os.environ.get("LINGXI_DELEGATED_REAUTH_STATE_KEY", "").strip() or credential_key
        entry = FeishuReauthorizationEntry(
            app_id=required("LINGXI_FEISHU_APP_ID"),
            redirect_uri=required("LINGXI_FEISHU_REDIRECT_URI"),
            scope=required("LINGXI_FEISHU_SCOPE"),
            authorization_endpoint=required("LINGXI_FEISHU_AUTHORIZATION_ENDPOINT"),
            state_store=HostFileAuthorizationStateStore(state_path, state_key),
            vault=vault,
            exchanger=FeishuAuthorizationClient(
                base_url=required("LINGXI_FEISHU_BASE_URL"),
                app_id=required("LINGXI_FEISHU_APP_ID"),
                app_secret=required("LINGXI_FEISHU_APP_SECRET"),
            ),
        )
        start = entry.begin(
            expected_subject_open_id=os.environ.get("LINGXI_DELEGATED_SUBJECT_OPEN_ID") or None,
        )
        print("请在受控浏览器中打开以下授权地址，并完成“四达文档会议助手”同意：")
        print(start.authorization_url)
        print("完成后粘贴完整 HTTPS 回跳地址（授权码只通过本次交互输入）：")
        state, code, error = parse_callback_url(input())
        result = entry.handle_callback(state, code=code, error=error)
        print(f"重授权结果：{result.code}；{result.message}")
        return 0 if result.ok else 1
    except (EOFError, KeyboardInterrupt):
        print("重授权已中断；本次授权未写入凭据，请重新发起。", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - 终端只报告失败类别
        print(f"重授权入口失败：{type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
