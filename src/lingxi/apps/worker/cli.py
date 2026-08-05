"""``python -m lingxi.apps.worker`` 的命令行外壳。

输出契约（受控验证要引用它，因此写死在这里）：

- **stdout**：恰好一个 JSON 对象，就是回合报告。配置错误时也是一个 JSON 对象，
  只是 ``turn`` 为空、``failure.code`` 为 ``config_error``。
- **stderr**：结构化日志，每行一个 JSON 对象，都带 ``trace_id``。不写日志文件、
  不自行轮转（`V-部署-04`）。
- **退出码**：0 回合正常收口；2 回合跑完但没收口（正文为空、终止结果不是恰好一次）；
  3 配置错误；4 会话失败（含 SDK 未安装）。

日志里刻意不出现问题原文与最终正文，只出现字节数、计数与状态：受控验证的证据
"只保留事件类型、计数、状态、长度、哈希和脱敏摘要"（Issue #37 验证与证据）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Mapping, TextIO

from lingxi.core.ids import new_ulid

from .config import WorkerConfigError, load_config
from .report import config_error_report
from .turn import WorkerTurnExecutor

EXIT_OK = 0
EXIT_TURN_NOT_CLOSED = 2
EXIT_CONFIG_ERROR = 3
EXIT_SESSION_FAILED = 4


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
        trace_id = env.get("LINGXI_WORKER_TRACE_ID") or new_ulid()
        _log(err, trace_id, "error", "worker.config.invalid", message=str(error))
        _emit(out, config_error_report(trace_id=trace_id, message=str(error)))
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

    report = asyncio.run(WorkerTurnExecutor(config).run_turn(config.question))
    turn = report["turn"]
    _log(
        err,
        config.trace_id,
        "error" if report["failure"] else "info",
        "worker.turn.finished",
        closed=turn["closed"],
        user_result=turn["user_result"],
        terminal_result_count=turn["terminal_result_count"],
        sdk_result_message_count=turn["sdk_result_message_count"],
        final_text_bytes=turn["final_text_bytes"],
        call_count=report["audit"]["call_count"],
        denied_count=report["audit"]["denied_count"],
        failed_count=report["audit"]["failed_count"],
        ungated_count=report["audit"]["ungated_count"],
        failure=report["failure"],
    )
    _emit(out, report)

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
