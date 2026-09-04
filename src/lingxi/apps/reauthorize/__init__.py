"""「四达文档会议助手」正式重授权的一次性运维入口。

这是随正式应用包发布的受控恢复作业，不是常驻服务。授权地址只在受控
终端显示；一次性 code 由已认证的 OAuth Bridge WebSocket 转发，正式
换码、身份绑定与凭据写入仍由 ``adapters.feishu_reauthorization`` 负责。
同一入口承载两种运维语义：续期（默认，无参数）为已登记的专用授权主体
换新凭据；首次建立（``--bootstrap-subject <open_id> --confirm-bootstrap``）
在主体登记为空时建立第一个专用授权主体，两个参数缺一不可——防误触的
门，首次建立是一次性动作，不该被一条历史命令或一次回车重放。首次建立
逐次审计留痕（发起/拒绝/完成/失败），出口是 :class:`BootstrapAuditHandler`；
写失败不静默，发起前写不下去就直接中止，建立后写不下去按不可恢复报错
并以非零退出码结束，要求人工补记。已知边界（知情接受）：换码成功且
凭据已落盘后，向 Bridge 回传结果失败会让入口以 ``retry`` 语义退出、
提示与落盘事实不符；复审条件是该入口成为高频路径或真实触发过此窗口。
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import socket
import sys
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from lingxi.adapters.delegated_credentials import HostFileDelegatedCredentialVault
from lingxi.adapters.feishu_directory import FeishuAuthorizationClient
from lingxi.adapters.feishu_reauthorization import (
    MODE_BOOTSTRAP,
    MODE_RENEWAL,
    FeishuReauthorizationEntry,
    HostFileAuthorizationStateStore,
    ReauthorizationResult,
    ReauthorizationStart,
    SubjectAlreadyRegisteredError,
)
from lingxi.adapters.oauth_bridge_client import (
    OAuthBridgeClient,
    OAuthBridgeMessage,
    OAuthBridgeResultSender,
)
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)
DEFAULT_BRIDGE_WAIT_SECONDS = 10 * 60
BOOTSTRAP_AUDIT_EVENT = "reauthorize.bootstrap"


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


def parse_arguments(argv: Sequence[str] = ()) -> argparse.Namespace:
    """解析入口参数。空参数即续期模式，与本入口既有行为一致。

    刻意不回落到 ``sys.argv``：``main`` 也能被程序内调用，让它去读宿主进程的
    命令行会把别人的参数解释成一次首次建立。命令行由 ``__main__`` 显式传入。
    """
    parser = argparse.ArgumentParser(
        prog="python -m lingxi.apps.reauthorize",
        description="专用授权的一次性受控入口：默认为已登记主体续期；显式确认后可首次建立主体。",
    )
    parser.add_argument(
        "--bootstrap-subject",
        metavar="OPEN_ID",
        default=None,
        help="首次建立模式：要登记的专用授权主体 open_id。必须与 --confirm-bootstrap 一起使用。",
    )
    parser.add_argument(
        "--confirm-bootstrap",
        action="store_true",
        help="确认执行首次建立。缺少本参数时 --bootstrap-subject 不会执行任何写入。",
    )
    return parser.parse_args(list(argv))


def resolve_mode(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, str | None]:
    """把参数组合解释成运维模式；任何不完整或互相矛盾的组合一律拒绝。

    只给主体不给确认、只给确认不给主体，都是"疑似误触"而不是"能猜出意图"，
    因此不猜、不半做，直接失败并说明本次没有任何修改。
    """
    subject = (args.bootstrap_subject or "").strip()
    confirmed = bool(args.confirm_bootstrap)
    if not subject and not confirmed:
        return MODE_RENEWAL, None
    if not confirmed:
        raise RuntimeError(
            "首次建立专用授权主体需要显式确认：请同时传入 --confirm-bootstrap；本次未做任何修改。"
        )
    if not subject:
        raise RuntimeError(
            "--confirm-bootstrap 必须与 --bootstrap-subject <open_id> 同时使用；本次未做任何修改。"
        )
    configured = (env.get("LINGXI_DELEGATED_SUBJECT_OPEN_ID") or "").strip()
    if configured and configured != subject:
        # 环境里配着另一个主体说明这台机器的用途与本次命令不一致；不确定就不做。
        raise RuntimeError(
            "命令行首次建立主体与 LINGXI_DELEGATED_SUBJECT_OPEN_ID 配置不一致，拒绝执行；本次未做任何修改。"
        )
    return MODE_BOOTSTRAP, subject


def _runtime_actor() -> str:
    """运行时身份：审计要回答"谁"。取不到时如实写未知，不编造。"""
    try:
        return getpass.getuser()
    except Exception:  # 容器里没有 passwd 条目时不能因此中断审计
        return f"uid:{os.getuid()}"


def _runtime_host() -> str:
    try:
        return socket.gethostname()
    except Exception:  # 同上
        return "unknown-host"


class BootstrapAuditHandler(logging.Handler):
    """首次建立的审计出口：把审计行写进受控终端，并如实反映有没有真的写下去。

    做成 logging Handler 而不是自建写文件函数：脱敏值经由日志参数流出。
    与标准 ``StreamHandler`` 的唯一区别是不吞异常——logging 默认把
    ``emit`` 里的错误交给 ``handleError`` 静默处理，而"审计写失败"恰恰
    是本入口必须看见并据以中止的事实。``written`` 计数让调用方还能区分
    "写成功"与"被日志级别过滤掉、根本没写"。
    """

    def __init__(self, stream: TextIO) -> None:
        super().__init__(level=logging.INFO)
        self._stream = stream
        self.written = 0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "audit_event", None) != BOOTSTRAP_AUDIT_EVENT:
            # 同一 logger 上的普通诊断日志不进审计出口。
            return
        self._stream.write(record.getMessage() + "\n")
        self._stream.flush()
        self.written += 1


def _write_audit(
    handler: BootstrapAuditHandler,
    action: str,
    *,
    subject_open_id: str | None,
    outcome: str,
    errors: TextIO,
    now: datetime | None = None,
) -> bool:
    """写一条首次建立审计：谁、在哪台机器、何时、哪个脱敏主体、模式与结果。

    返回是否确实落地。失败绝不吞掉——调用方据此中止或按不可恢复上报。
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    before = handler.written
    try:
        logger.info(
            "event=%s action=%s mode=%s at=%s actor=%s host=%s pid=%s subject=%s outcome=%s",
            BOOTSTRAP_AUDIT_EVENT,
            action,
            MODE_BOOTSTRAP,
            moment.isoformat(),
            _runtime_actor(),
            _runtime_host(),
            os.getpid(),
            redact_identifier(subject_open_id),
            outcome,
            extra={"audit_event": BOOTSTRAP_AUDIT_EVENT},
        )
    except Exception as error:  # 审计出口失败只报类别
        _report_audit_failure(action, type(error).__name__, errors=errors)
        return False
    if handler.written == before:
        _report_audit_failure(action, "not_recorded", errors=errors)
        return False
    return True


def _report_audit_failure(action: str, reason: str, *, errors: TextIO) -> None:
    logger.error("首次建立审计留痕失败 action=%s reason=%s", action, reason)
    try:
        print(f"审计留痕失败（{reason}）：首次建立的 {action} 记录未能写下。", file=errors)
    except Exception:  # 终端也不可写时至少 logger 已经报过
        pass


def _build_entry(
    env: Mapping[str, str],
    *,
    state_path: str,
    credential_path: str,
    mode: str = MODE_RENEWAL,
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
        mode=mode,
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
    argv: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    audit_stream: TextIO | None = None,
) -> int:
    """一次性入口：装好首次建立的审计出口，再把控制权交给实际流程。

    审计 handler 只在本次运行、且命令行确实带了首次建立参数时挂上，结束即摘掉，
    不给宿主进程留下长期副作用。
    """
    errors = sys.stderr if stderr is None else stderr
    arguments = parse_arguments(argv)
    handler = BootstrapAuditHandler(errors if audit_stream is None else audit_stream)
    if not (arguments.bootstrap_subject or arguments.confirm_bootstrap):
        return _run(arguments, handler, env=env, stdout=stdout, stderr=stderr)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)  # 审计行是 INFO：不能被默认级别悄悄过滤掉
    try:
        return _run(arguments, handler, env=env, stdout=stdout, stderr=stderr)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _resolve_mode_or_reject(
    arguments: argparse.Namespace,
    source: Mapping[str, str],
    handler: BootstrapAuditHandler,
    errors: TextIO,
) -> tuple[str, str | None] | None:
    """解析运维模式；参数组合非法时打印+审计+返回 ``None``（调用方据此 return 1）。"""
    try:
        return resolve_mode(arguments, source)
    except RuntimeError as error:
        # 参数组合类拒绝的原因全是本文件里写死的中文常量，可以原样告诉运维；
        # 其余异常仍然只报类别，避免外部内容被带到终端。
        print(str(error), file=errors)
        _write_audit(
            handler,
            "rejected",
            subject_open_id=(arguments.bootstrap_subject or "").strip() or None,
            outcome="unconfirmed_arguments",
            errors=errors,
        )
        return None


def _build_entry_paths_and_client(
    source: Mapping[str, str], mode: str
) -> FeishuReauthorizationEntry:
    credential_path = required("LINGXI_DELEGATED_CREDENTIAL_PATH", source)
    state_path = source.get("LINGXI_DELEGATED_REAUTH_STATE_PATH", "").strip()
    state_path = state_path or credential_path + ".reauth-state"
    state_path, credential_path = validate_reauthorization_paths(state_path, credential_path)
    return _build_entry(
        source,
        state_path=str(state_path),
        credential_path=str(credential_path),
        mode=mode,
    )


def _begin_reauthorization(
    entry: FeishuReauthorizationEntry,
    mode: str,
    subject: str | None,
    source: Mapping[str, str],
    handler: BootstrapAuditHandler,
    errors: TextIO,
) -> ReauthorizationStart | None:
    """（可能）先写 bootstrap 的 requested 审计，再调用 ``entry.begin()``。

    返回 ``None`` 表示已经打印并审计过拒绝理由，调用方应 ``return 1``。
    """
    if mode == MODE_BOOTSTRAP:
        # 先留痕再动作：审计写不下去就不发起授权，避免"建了但没人知道"。
        if not _write_audit(
            handler,
            "requested",
            subject_open_id=subject,
            outcome="pending",
            errors=errors,
        ):
            print("首次建立已中止：审计留痕失败，未发起授权，也未修改任何登记。", file=errors)
            return None
    try:
        return entry.begin(
            expected_subject_open_id=(
                subject
                if mode == MODE_BOOTSTRAP
                else source.get("LINGXI_DELEGATED_SUBJECT_OPEN_ID") or None
            )
        )
    except SubjectAlreadyRegisteredError:
        print(
            "已存在专用授权主体登记，首次建立拒绝执行；未发起授权，也未修改任何登记。"
            "如需更换主体，请先执行单独授权的删除动作。",
            file=errors,
        )
        _write_audit(
            handler,
            "rejected",
            subject_open_id=subject,
            outcome="subject_exists",
            errors=errors,
        )
        return None


class _BridgeSession:
    """``_run_bridge_exchange`` 与 ``_run`` 共享的 bridge/线程持有处。

    用可变容器而不是返回值传出 bridge/thread：即便交换过程中途抛异常
    （例如 :func:`bridge_wait_seconds` 读到非法环境变量），已经创建/
    启动的 bridge 与线程也必须能被 ``_run`` 的 ``finally`` 清理，返回
    值只有函数正常返回时才会被赋值，接不住这种中途失败的情形。
    """

    def __init__(self) -> None:
        self.bridge: OAuthBridgeClient | None = None
        self.thread: threading.Thread | None = None


def _run_bridge_exchange(
    entry: FeishuReauthorizationEntry,
    start: ReauthorizationStart,
    source: Mapping[str, str],
    output: TextIO,
    errors: TextIO,
    session: _BridgeSession,
) -> tuple[list[ReauthorizationResult], bool]:
    """建立 Bridge、打印授权地址、注册回调、启动线程并等待完成。

    创建成功的 bridge/线程立即写进 ``session``，供调用方即便本函数中途
    异常也能在 ``finally`` 里清理。
    """
    session.bridge = OAuthBridgeClient(
        required("LINGXI_OAUTH_BRIDGE_URL", source),
        required("LINGXI_OAUTH_BRIDGE_TOKEN", source),
    )
    bridge = session.bridge
    completed = threading.Event()
    results: list[ReauthorizationResult] = []

    def receive_bridge_message(message: OAuthBridgeMessage) -> None:
        try:
            results.append(handle_bridge_message(entry, bridge, message))
        except Exception as error:  # 入口只暴露脱敏失败类别
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
            except Exception as send_error:  # 只记录错误类别
                logger.warning("OAuth Bridge 结果回传失败 error=%s", type(send_error).__name__)
        finally:
            completed.set()

    bridge.register_state_handler(start.state, receive_bridge_message)
    session.thread = bridge.start()
    print("请在受控浏览器中打开以下授权地址，并完成“四达文档会议助手”同意：", file=output)
    print(start.authorization_url, file=output)
    print("授权结果将通过 OAuth Bridge 回传；本入口不接收或显示回跳地址。", file=output)
    completed_in_time = completed.wait(bridge_wait_seconds(source))
    return results, completed_in_time


def _finalize_bootstrap_result(
    handler: BootstrapAuditHandler,
    subject: str | None,
    result: ReauthorizationResult,
    errors: TextIO,
) -> int:
    """Bootstrap 模式下写 completed/failed 审计；返回进程退出码。"""
    written = _write_audit(
        handler,
        "completed" if result.ok else "failed",
        subject_open_id=subject,
        outcome=result.code,
        errors=errors,
    )
    if not written:
        if result.ok:
            # 登记已经发生却没留下痕迹：这不是可以重试的失败，重复运行
            # 只会被"登记为空才允许"再拒一次，正确动作是人工补记。
            print(
                "不可恢复：专用授权主体已首次建立，但审计留痕失败；"
                "请立即人工补记本次建立，不要重复运行首次建立。",
                file=errors,
            )
        return 1
    return 0 if result.ok else 1


def _handle_bridge_timeout(
    mode: str, subject: str | None, handler: BootstrapAuditHandler, errors: TextIO
) -> None:
    print(
        "OAuth Bridge 回调等待超时；本次授权未确认，请重新运行入口发起新的授权。",
        file=errors,
    )
    if mode == MODE_BOOTSTRAP:
        _write_audit(
            handler, "failed", subject_open_id=subject, outcome="bridge_timeout", errors=errors
        )


def _handle_run_interruption(
    mode: str,
    subject: str | None,
    handler: BootstrapAuditHandler,
    errors: TextIO,
    *,
    message: str,
    outcome: str,
) -> int:
    """``_run`` 主体被中断或未预期异常时的共同收口：打印+（bootstrap 下）审计+返回 1。"""
    print(message, file=errors)
    if mode == MODE_BOOTSTRAP:
        _write_audit(handler, "failed", subject_open_id=subject, outcome=outcome, errors=errors)
    return 1


def _run(
    arguments: argparse.Namespace,
    handler: BootstrapAuditHandler,
    *,
    env: Mapping[str, str] | None,
    stdout: TextIO | None,
    stderr: TextIO | None,
) -> int:
    source = os.environ if env is None else env
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    session = _BridgeSession()

    resolved = _resolve_mode_or_reject(arguments, source, handler, errors)
    if resolved is None:
        return 1
    mode, subject = resolved

    try:
        entry = _build_entry_paths_and_client(source, mode)
        start = _begin_reauthorization(entry, mode, subject, source, handler, errors)
        if start is None:
            return 1

        results, completed_in_time = _run_bridge_exchange(
            entry, start, source, output, errors, session
        )
        if not completed_in_time:
            _handle_bridge_timeout(mode, subject, handler, errors)
            return 1

        result = results[0]
        print(f"重授权结果：{result.code}；{result.message}", file=output)
        if mode == MODE_BOOTSTRAP:
            return _finalize_bootstrap_result(handler, subject, result, errors)
        return 0 if result.ok else 1
    except (EOFError, KeyboardInterrupt):
        return _handle_run_interruption(
            mode,
            subject,
            handler,
            errors,
            message="重授权已中断；本次授权未写入凭据，请重新发起。",
            outcome="interrupted",
        )
    except Exception as error:  # 终端只报告失败类别
        return _handle_run_interruption(
            mode,
            subject,
            handler,
            errors,
            message=f"重授权入口失败：{type(error).__name__}",
            outcome=type(error).__name__,
        )
    finally:
        if session.bridge is not None:
            session.bridge.stop()
        if session.thread is not None:
            session.thread.join(timeout=5)


__all__ = [
    "main",
    "BootstrapAuditHandler",
    "bridge_wait_seconds",
    "handle_bridge_message",
    "parse_arguments",
    "required",
    "resolve_mode",
    "validate_reauthorization_paths",
]
