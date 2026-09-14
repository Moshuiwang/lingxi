"""问答留存语料：落库记录的形状、从回合素材构造记录、窄读取角色的默认拒绝谓词。

产品合同「数据保留与删除」第三条例外设立的独立正式通道：每次真实问数留一行，问题原文、
用户实收的安全正文与模型原文回答两者都存，外加工具调用详情；不设保留上限。它与内测轮
内容级采集只共用回合内的原始素材收集器与凭据形状过滤（``core/content_redaction.py``），
开关、表与生命周期全部独立。四段正文都过同一份凭据形状判据并各自计数，已知局限原样
继承：纯字母且短于 32 字符的裸秘密盖不住，计数是「替换了几处」而不是「零命中即无凭据」。

模型原文那一列不接任何投递、展示或日志路径——合同明令该副本不得回流；本模块只有类型
与纯函数，落库在 ``adapters/postgres_qa_corpus.py``。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from typing import Any, Protocol

from lingxi.core.delivery.ports import TerminalKind
from lingxi.core.delivery.turn_outcome import TerminalDecision
from lingxi.core.execution.audit import redact_free_text_with_count
from lingxi.core.innertest_content_capture import CapturedToolCall, ContentCaptureRecord

#: ``terminal_kind`` 与投递事件同一取值域，表的 CHECK 逐字一致。
TERMINAL_KINDS = frozenset(kind.value for kind in TerminalKind)
#: ``user_result`` 的固定码形状；回合报告里没有它时记 ``unknown``。
UNKNOWN_USER_RESULT = "unknown"
_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_CODE_LENGTH = 64
_MAX_LABEL_LENGTH = 128
_IDENTIFIER_FIELDS = (
    "task_id",
    "conversation_id",
    "user_id",
    "worker_id",
    "worker_version",
    "target_worker_version",
)
_OPTIONAL_LABEL_FIELDS = ("trace_id", "system_prompt_digest", "model", "failure_code")
_TEXT_FIELDS = ("question_content", "answer_delivered", "answer_model_raw")
_COUNT_FIELDS = (
    "question_redaction_count",
    "answer_delivered_redaction_count",
    "answer_model_raw_redaction_count",
)


class CorpusTaskFacts(Protocol):
    """语料需要的任务归属事实；队列领到的任务对象结构上满足它，不引入适配器类型。"""

    task_id: str
    conversation_id: str
    user_id: str
    trace_id: str | None
    task_created_at: datetime | None
    target_worker_version: str


def _checked_label(name: str, value: object, *, optional: bool) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or (not optional and not value):
        raise ValueError(f"{name} 必须是{'字符串' if optional else '非空字符串'}")
    if len(value) > _MAX_LABEL_LENGTH:
        raise ValueError(f"{name} 超过 {_MAX_LABEL_LENGTH} 个字符")


def _checked_count(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")


@dataclass(frozen=True)
class QaCorpusRecord:
    """一条待落库的语料记录：四段正文各带脱敏计数、终态与来源版本。

    构造即校验**形状与长度上限**，不校验内容：正文按合同不设限，这里只保证固定码列
    到不了数据库的 CHECK 才被拒绝。
    """

    task_id: str
    conversation_id: str
    user_id: str
    trace_id: str | None
    task_created_at: datetime | None
    question_content: str
    question_redaction_count: int
    answer_delivered: str
    answer_delivered_redaction_count: int
    answer_model_raw: str
    answer_model_raw_redaction_count: int
    tool_calls: tuple[CapturedToolCall, ...]
    terminal_kind: str
    user_result: str
    failure_code: str | None
    output_safety_withheld: bool
    worker_id: str
    worker_version: str
    target_worker_version: str
    system_prompt_digest: str | None
    model: str | None

    def __post_init__(self) -> None:
        """逐列核对固定码形状、长度上限与计数；任何一项不合法都拒绝构造。"""
        for name in _IDENTIFIER_FIELDS:
            _checked_label(name, getattr(self, name), optional=False)
        for name in _OPTIONAL_LABEL_FIELDS:
            _checked_label(name, getattr(self, name), optional=True)
        for name in _TEXT_FIELDS:
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")
        for name in _COUNT_FIELDS:
            _checked_count(name, getattr(self, name))
        if self.terminal_kind not in TERMINAL_KINDS:
            raise ValueError("terminal_kind 不在投递事件的终态取值域内")
        if (
            not isinstance(self.user_result, str)
            or not _CODE.match(self.user_result)
            or len(self.user_result) > _MAX_CODE_LENGTH
        ):
            raise ValueError("user_result 不是固定码形状")
        if not isinstance(self.output_safety_withheld, bool):
            raise ValueError("output_safety_withheld 必须是布尔值")
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(call, CapturedToolCall) for call in self.tool_calls
        ):
            raise ValueError("tool_calls 必须是 CapturedToolCall 元组")

    @property
    def tool_calls_redaction_count(self) -> int:
        """全部工具调用的脱敏命中次数之和。"""
        return sum(call.redaction_count for call in self.tool_calls)

    def tool_calls_payload(self) -> list[dict[str, Any]]:
        """给适配器写 JSONB 列用的纯 JSON 安全结构，形状与内测采集表同一份。"""
        return [
            {
                "tool_use_id": call.tool_use_id,
                "tool_name": call.tool_name,
                "tool_input": call.tool_input,
                "result_summary": call.result_summary,
                "redaction_count": call.redaction_count,
            }
            for call in self.tool_calls
        ]


def installed_worker_version() -> str:
    """已安装的 lingxi 包版本；源码树直跑（包未安装）时记 ``unknown``。"""
    try:
        return metadata.version("lingxi")
    except metadata.PackageNotFoundError:
        return "unknown"


def _report_turn(report: Mapping[str, Any]) -> Mapping[str, Any]:
    turn = report.get("turn")
    return turn if isinstance(turn, Mapping) else {}


def _failure_code(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_LABEL_LENGTH]


def build_qa_corpus_record(
    capture: ContentCaptureRecord,
    *,
    task: CorpusTaskFacts,
    decision: TerminalDecision,
    report: Mapping[str, Any],
    system_prompt_digest: str | None,
    model: str | None,
) -> QaCorpusRecord:
    """把收口后的回合素材合成一条语料记录。

    问题原文、模型原文与工具调用取自收集器已经过滤并计数的采集记录；用户实收正文取
    ``decision.content.text``——它必须与写进投递事件的那一份逐字同源，因此调用方要传
    终态收口**之后**的 decision（失败追溯引用已经追加进去的那一份）。
    """
    delivered, delivered_count = redact_free_text_with_count(decision.content.text)
    turn = _report_turn(report)
    output_safety = turn.get("output_safety")
    user_result = turn.get("user_result")
    if not isinstance(user_result, str) or not user_result:
        user_result = UNKNOWN_USER_RESULT
    return QaCorpusRecord(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        user_id=task.user_id,
        trace_id=task.trace_id,
        task_created_at=task.task_created_at,
        question_content=capture.question_content,
        question_redaction_count=capture.question_redaction_count,
        answer_delivered=delivered,
        answer_delivered_redaction_count=delivered_count,
        answer_model_raw=capture.answer_content,
        answer_model_raw_redaction_count=capture.answer_redaction_count,
        tool_calls=capture.tool_calls,
        terminal_kind=decision.terminal_kind,
        user_result=user_result,
        failure_code=_failure_code(decision.failure_code),
        output_safety_withheld=bool(
            isinstance(output_safety, Mapping) and output_safety.get("withheld")
        ),
        worker_id=capture.worker_id,
        worker_version=installed_worker_version(),
        target_worker_version=task.target_worker_version,
        system_prompt_digest=system_prompt_digest,
        model=model,
    )


@dataclass(frozen=True)
class QaCorpusReaderEntry:
    """``qa_corpus_reader`` 的一行登记；``label`` 是角色化标签，不是姓名。"""

    id: str
    feishu_open_id: str
    label: str
    entry_status: str
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None = None


def is_authorized_corpus_reader(entry: QaCorpusReaderEntry | None) -> bool:
    """默认拒绝谓词：没有登记、已撤销或带着撤销时间的条目一律不是读取者。

    合同只允许为此设立的窄读取角色读语料；管理员、超级管理员都不因其它角色获得读取权，
    任何读取路径在发出查询之前先过这一条。
    """
    return entry is not None and entry.entry_status == "active" and entry.revoked_at is None
