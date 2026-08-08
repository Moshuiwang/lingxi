"""「四达文档会议助手」正式重授权的一次性运维入口。

这是随正式应用包发布的受控恢复作业，不是常驻服务，也不是普通员工入口。
授权地址只在受控终端显示；一次性 code 由已认证的 OAuth Bridge WebSocket
转发，正式换码、身份绑定与凭据写入仍由 ``lingxi.adapters.feishu_reauthorization``
负责。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
from lingxi.adapters.feishu_reauthorization import (
    FeishuReauthorizationEntry,
    HostFileAuthorizationStateStore,
    ReauthorizationResult,
)
from lingxi.adapters.oauth_bridge_client import (
    OAuthBridgeClient,
    OAuthBridgeMessage,
    OAuthBridgeResultSender,
)


logger = logging.getLogger(__name__)
DEFAULT_BRIDGE_WAIT_SECONDS = 10 * 60


def required(name: str, env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = source.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必需的环境变量：{name}")
    return value


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


def bridge_wait_seconds(env: Mapping[str, str]) -> int:
    raw = env.get("LINGXI_OAUTH_BRIDGE_WAIT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_BRIDGE_WAIT_SECONDS
    try:
        seconds = int(raw)
    except ValueError as error:
        raise RuntimeError("LINGXI_OAUTH_BRIDGE_WAIT_SECONDS 必须是正整数秒") from error
    if seconds <= 0:
        raise RuntimeError("LINGXI_OAUTH_BRIDGE_WAIT_SECONDS 必须是正整数秒")
    return seconds


def handle_bridge_message(
    entry: FeishuReauthorizationEntry,
    result_sender: OAuthBridgeResultSender,
    message: OAuthBridgeMessage,
) -> ReauthorizationResult:
    """把 Bridge 消息接到 E1 正式回调；传输层不参与建档或凭据判断。"""

    if message.type == "oauth_code":
        result = entry.handle_callback(message.state, code=message.code)
    elif message.type == "oauth_cancelled":
        result = entry.handle_callback(message.state, error="access_denied")
    else:
        raise ValueError("未识别的 OAuth Bridge 消息")
    result_sender.send_result(message.state, "identity_confirmed" if result.ok else "retry")
    return result


def main(
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    source = os.environ if env is None else env
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    bridge: OAuthBridgeClient | None = None
    bridge_thread: threading.Thread | None = None
    try:
        credential_path = required("LINGXI_DELEGATED_CREDENTIAL_PATH", source)
        state_path = source.get("LINGXI_DELEGATED_REAUTH_STATE_PATH", "").strip()
        state_path = state_path or credential_path + ".reauth-state"
        state_path, credential_path = validate_reauthorization_paths(state_path, credential_path)

        entry = _build_entry(source, state_path=str(state_path), credential_path=str(credential_path))
        start = entry.begin(expected_subject_open_id=source.get("LINGXI_DELEGATED_SUBJECT_OPEN_ID") or None)
        bridge = OAuthBridgeClient(
            required("LINGXI_OAUTH_BRIDGE_URL", source),
            required("LINGXI_OAUTH_BRIDGE_TOKEN", source),
        )
        completed = threading.Event()
        results: list[ReauthorizationResult] = []

        def receive_bridge_message(message: OAuthBridgeMessage) -> None:
            try:
                results.append(handle_bridge_message(entry, bridge, message))
            except Exception as error:  # noqa: BLE001 - 入口只暴露脱敏失败类别
                logger.warning("正式重授权 Bridge 回调处理失败 error=%s", type(error).__name__)
                results.append(
                    ReauthorizationResult(
                        False,
                        "bridge_failed",
                        "OAuth Bridge 回调未能完成，请重新发起授权。",
                        True,
                    )
                )
                try:
                    bridge.send_result(message.state, "retry")
                except Exception as send_error:  # noqa: BLE001 - 只记录错误类别
                    logger.warning("OAuth Bridge 结果回传失败 error=%s", type(send_error).__name__)
            finally:
                completed.set()

        bridge.register_state_handler(start.state, receive_bridge_message)
        bridge_thread = bridge.start()
        print("请在受控浏览器中打开以下授权地址，并完成“四达文档会议助手”同意：", file=output)
        print(start.authorization_url, file=output)
        print("授权结果将通过 OAuth Bridge 回传；本入口不接收或显示回跳地址。", file=output)
        if not completed.wait(bridge_wait_seconds(source)):
            print("OAuth Bridge 回调等待超时；本次授权未确认，请重新运行入口发起新的授权。", file=errors)
            return 1
        result = results[0]
        print(f"重授权结果：{result.code}；{result.message}", file=output)
        return 0 if result.ok else 1
    except (EOFError, KeyboardInterrupt):
        print("重授权已中断；本次授权未写入凭据，请重新发起。", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - 终端只报告失败类别
        print(f"重授权入口失败：{type(error).__name__}", file=errors)
        return 1
    finally:
        if bridge is not None:
            bridge.stop()
        if bridge_thread is not None:
            bridge_thread.join(timeout=5)


__all__ = [
    "main",
    "bridge_wait_seconds",
    "handle_bridge_message",
    "required",
    "validate_reauthorization_paths",
]
