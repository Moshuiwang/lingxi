"""``python -m lingxi.apps.worker`` 的命令行外壳。

输出契约（受控验证要引用它，因此写死在这里）：

- **stdout**：恰好一个 JSON 对象，就是回合报告。配置错误时也是一个 JSON 对象，
  只是 ``turn`` 为空、``failure.code`` 为 ``config_error``。
- **stderr**：结构化日志，每行一个 JSON 对象，都带 ``trace_id``。不写日志文件、
  不自行轮转（`V-部署-04`）。
- **退出码**：0 回合正常收口；2 回合跑完但没收口（正文为空、终止结果不是恰好一次、
  SDK 终止消息自报错误）；3 配置错误；4 会话失败（含 SDK 未安装）；
  5 检测到绕过屏障的调用（`ungated_count > 0`，hook 未触发的唯一可观察形状）。

日志里刻意不出现问题原文与最终正文，只出现字节数、计数与状态：受控验证的证据
"只保留事件类型、计数、状态、长度、哈希和脱敏摘要"（Issue #37 验证与证据）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Mapping, TextIO

from lingxi.core.execution.audit import redact_free_text
from lingxi.core.ids import is_ulid, new_ulid

from .config import WorkerConfigError, load_config
from .report import config_error_report
from .turn import WorkerTurnExecutor

EXIT_OK = 0
EXIT_TURN_NOT_CLOSED = 2
EXIT_CONFIG_ERROR = 3
EXIT_SESSION_FAILED = 4
EXIT_GATE_BYPASSED = 5


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    del argv  # 本切片没有命令行参数：全部输入走 LINGXI_ 前缀环境变量。
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    try:
        config = load_config(env)
    except WorkerConfigError as error:
        provided_trace_id = env.get("LINGXI_WORKER_TRACE_ID", "")
        # 只有合法 ULID 才复用：误接进来的令牌不得随错误输出外泄（Codex 复查）。
        trace_id = provided_trace_id if is_ulid(provided_trace_id) else new_ulid()
        # 配置错误文案可能回显运维写串的原值（令牌形态也拦不住），与模型侧
        # 同一标准：出口过自由文本脱敏并截断（独立复查发现）。
        message = redact_free_text(str(error))[:300]
        _log(err, trace_id, "error", "worker.config.invalid", message=message)
        _emit(out, config_error_report(trace_id=trace_id, message=message))
        return EXIT_CONFIG_ERROR

    _log(
        err,
        config.trace_id,
        "info",
        "worker.turn.start",
        read_only_tool=config.read_only_tool,
        question_bytes=len(config.question.encode("utf-8")),
        mcp_servers=sorted(config.mcp_servers),
        workspace_configured=config.workspace is not None,
    )

    report = asyncio.run(WorkerTurnExecutor(config, stderr_stream=err).run_turn(config.question))
    turn = report["turn"]
    gate_bypassed = report["audit"]["ungated_count"] > 0
    _log(
        err,
        config.trace_id,
        "error" if (report["failure"] or gate_bypassed or not turn["closed"]) else "info",
        "worker.turn.finished",
        closed=turn["closed"],
        user_result=turn["user_result"],
        terminal_result_count=turn["terminal_result_count"],
        sdk_result_message_count=turn["sdk_result_message_count"],
        sdk_result_is_error=turn["sdk_result_is_error"],
        sdk_result_subtype=turn["sdk_result_subtype"],
        gate_bypassed=gate_bypassed,
        final_text_bytes=turn["final_text_bytes"],
        call_count=report["audit"]["call_count"],
        denied_count=report["audit"]["denied_count"],
        failed_count=report["audit"]["failed_count"],
        ungated_count=report["audit"]["ungated_count"],
        failure=report["failure"],
    )
    _emit(out, report)

    if gate_bypassed:
        # 安全边界失效优先于一切其他失败态：绕过之后又超时/抛错时，受控验证
        # 必须先看到 5 而不是通用的 4（终轮 Codex 复查发现）。
        return EXIT_GATE_BYPASSED
    if report["failure"]:
        return EXIT_SESSION_FAILED
    return EXIT_OK if turn["closed"] else EXIT_TURN_NOT_CLOSED


def _emit(stream: TextIO, payload: Mapping[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    stream.write("\n")
    stream.flush()


def _log(stream: TextIO, trace_id: str, level: str, event: str, **fields: Any) -> None:
    record = {"level": level, "event": event, "trace_id": trace_id}
    record.update(fields)
    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
    stream.write("\n")
    stream.flush()
