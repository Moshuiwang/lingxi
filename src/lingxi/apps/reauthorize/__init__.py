"""「四达文档会议助手」正式重授权的一次性运维入口。

这是随正式应用包发布的受控恢复作业，不是常驻服务，也不是普通员工入口。
授权码与完整回调地址只在关闭回显的交互输入中出现；正式编排仍由
``lingxi.adapters.feishu_reauthorization`` 负责。
"""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlparse

from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
from lingxi.adapters.feishu_reauthorization import (
    FeishuReauthorizationEntry,
    HostFileAuthorizationStateStore,
)


def required(name: str, env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = source.get(name, "").strip()
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


def _normalized_path(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    # realpath 也能在目标尚不存在时规范化 ..，并识别已经存在的符号链接别名。
    return Path(os.path.realpath(os.path.expanduser(value.strip())))


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def validate_reauthorization_paths(state_path: str, credential_path: str) -> tuple[Path, Path]:
    """启动前拒绝 state 文件及其锁与凭据文件及其锁的任何重叠。"""

    state = _normalized_path(state_path, "重授权 state 文件路径")
    credential = _normalized_path(credential_path, "专用授权凭据文件路径")
    state_paths = {state, _lock_path(state)}
    credential_paths = {credential, _lock_path(credential)}
    if state_paths & credential_paths:
        raise ValueError("重授权 state 路径不得与凭据文件或锁文件冲突")
    return state, credential


def _build_entry(
    env: Mapping[str, str],
    *,
    state_path: str,
    credential_path: str,
) -> FeishuReauthorizationEntry:
    credential_key = required("LINGXI_DELEGATED_CREDENTIAL_KEY", env)
    vault = HostFileDelegatedCredentialVault(
        required("LINGXI_POSTGRES_DSN", env),
        credential_key,
        credential_path,
    )
    state_key = env.get("LINGXI_DELEGATED_REAUTH_STATE_KEY", "").strip() or credential_key
    return FeishuReauthorizationEntry(
        app_id=required("LINGXI_FEISHU_APP_ID", env),
        redirect_uri=required("LINGXI_FEISHU_REDIRECT_URI", env),
        scope=required("LINGXI_FEISHU_SCOPE", env),
        authorization_endpoint=required("LINGXI_FEISHU_AUTHORIZATION_ENDPOINT", env),
        state_store=HostFileAuthorizationStateStore(state_path, state_key),
        vault=vault,
        exchanger=FeishuAuthorizationClient(
            base_url=required("LINGXI_FEISHU_BASE_URL", env),
            app_id=required("LINGXI_FEISHU_APP_ID", env),
            app_secret=required("LINGXI_FEISHU_APP_SECRET", env),
        ),
    )


def read_callback_url() -> str:
    """关闭终端回显读取完整回调地址，避免授权码进入终端录制。"""

    return getpass.getpass("完成后粘贴完整 HTTPS 回跳地址（输入不会回显）：")


def main(
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    callback_reader: Callable[[], str] | None = None,
) -> int:
    source = os.environ if env is None else env
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        credential_path = required("LINGXI_DELEGATED_CREDENTIAL_PATH", source)
        state_path = source.get("LINGXI_DELEGATED_REAUTH_STATE_PATH", "").strip()
        state_path = state_path or credential_path + ".reauth-state"
        validate_reauthorization_paths(state_path, credential_path)

        entry = _build_entry(source, state_path=state_path, credential_path=credential_path)
        start = entry.begin(expected_subject_open_id=source.get("LINGXI_DELEGATED_SUBJECT_OPEN_ID") or None)
        print("请在受控浏览器中打开以下授权地址，并完成“四达文档会议助手”同意：", file=output)
        print(start.authorization_url, file=output)
        reader = callback_reader or read_callback_url
        state, code, error = parse_callback_url(reader())
        result = entry.handle_callback(state, code=code, error=error)
        print(f"重授权结果：{result.code}；{result.message}", file=output)
        return 0 if result.ok else 1
    except (EOFError, KeyboardInterrupt):
        print("重授权已中断；本次授权未写入凭据，请重新发起。", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - 终端只报告失败类别
        print(f"重授权入口失败：{type(error).__name__}", file=errors)
        return 1


__all__ = [
    "main",
    "parse_callback_url",
    "read_callback_url",
    "required",
    "validate_reauthorization_paths",
]
