"""问答留存语料：落库记录的形状、从回合素材构造记录、窄读取角色的默认拒绝谓词。

产品合同「数据保留与删除」第三条例外设立的独立正式通道：每次真实问数留一行，问题原文、
用户实收的安全正文与模型原文回答两者都存，外加工具调用详情；不设保留上限。它与内测轮
内容级采集只共用回合内的原始素材收集器与凭据形状过滤（``core/content_redaction.py``），
开关、表与生命周期全部独立。四段正文都过同一份凭据形状判据并各自计数，已知局限原样
继承：纯字母且短于 32 字符的裸秘密盖不住，计数是「替换了几处」而不是「零命中即无凭据」。

模型原文那一列不接任何投递、展示或日志路径——合同写明该副本不得回流；本模块只有类型
与纯函数，落库在 ``adapters/postgres_qa_corpus.py``。

读取侧同样只放纯逻辑：过滤条件的形状、关键词的子串匹配转义、导出行的序列化、审计摘要与
证据指针的计算，以及容量水位的判定；关键词本身不进任何持久列，只进摘要。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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

    构造即校验**形状与长度上限**，不校验内容：正文不设限是合同原话，这里只保证固定码列
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


#: 单次 ``read`` 的行数：默认值与硬顶。硬顶挡的是「一次把整张表打到终端」，不是容量。
DEFAULT_READ_LIMIT = 20
MAX_READ_LIMIT = 200
_MAX_KEYWORD_LENGTH = 128
#: ``ILIKE`` 的转义字符；``%`` / ``_`` 在模式里是通配符，关键词里的它们必须按字面匹配。
LIKE_ESCAPE = "\\"
#: 导出文件只许是导出根正下方的普通文件名；字符集同时满足审计证据指针那一列的形状。
EXPORT_SUFFIX = ".jsonl"
_EXPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
#: 证据指针里「无界」一端的写法，与带时区的时刻码一起落在同一列。
OPEN_MOMENT = "open"


def _checked_moment(name: str, value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须是带时区的时刻")
    return value


@dataclass(frozen=True)
class CorpusFilter:
    """一次读取或导出的过滤条件：按人、时间窗（含起不含止）、关键词，至少给一项。

    构造即校验形状：没有任何条件的过滤等于「整张表」，不是读取角色该有的姿势；朴素时刻
    会按服务端时区被静默解释，一律拒绝。关键词只在这里短暂存在，不进任何持久列。
    """

    user_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    keyword: str | None = None

    def __post_init__(self) -> None:
        """至少一项、时刻带时区、窗口有序、关键词非空且有上限。"""
        if all(value is None for value in (self.user_id, self.since, self.until, self.keyword)):
            raise ValueError("至少要给一项过滤条件：按人、时间窗或关键词")
        if self.user_id is not None:
            _checked_label("user_id", self.user_id, optional=False)
        since = _checked_moment("since", self.since)
        until = _checked_moment("until", self.until)
        if since is not None and until is not None and not since < until:
            raise ValueError("时间窗必须满足 since < until")
        if self.keyword is not None:
            if not isinstance(self.keyword, str) or not self.keyword.strip():
                raise ValueError("keyword 不能为空白")
            if "\x00" in self.keyword:
                raise ValueError("keyword 不能含 NUL 字符")
            if len(self.keyword) > _MAX_KEYWORD_LENGTH:
                raise ValueError(f"keyword 超过 {_MAX_KEYWORD_LENGTH} 个字符")


def checked_read_limit(value: object) -> int:
    """``read`` 的行数上限：正整数且不超过硬顶；布尔值不算整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("limit 必须是正整数")
    if value > MAX_READ_LIMIT:
        raise ValueError(f"limit 不能超过 {MAX_READ_LIMIT}")
    return value


def like_pattern(keyword: str) -> str:
    """把关键词变成子串匹配的 ``ILIKE`` 模式：先转义反斜线、``%`` 与 ``_``，再两头加 ``%``。

    不转义时用户输入的 ``%`` 会匹配任意串、``_`` 会匹配任意单字符——「查含 100% 的问题」
    会变成「查全部」。查询必须带 ``ESCAPE`` 子句并传 :data:`LIKE_ESCAPE`。
    """
    escaped = (
        keyword.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def checked_export_name(value: object) -> str:
    """导出文件名：普通文件名形状、以 ``.jsonl`` 结尾；不接受任何路径分隔符与 ``..``。"""
    if not isinstance(value, str) or not _EXPORT_NAME.match(value):
        raise ValueError("导出文件名只能是字母、数字、点、下划线、连字符组成的普通文件名")
    if not value.endswith(EXPORT_SUFFIX):
        raise ValueError(f"导出文件名必须以 {EXPORT_SUFFIX} 结尾")
    return value


def export_line(*, row_id: str, created_at: datetime, record: QaCorpusRecord) -> str:
    """一行 JSON：内部行标识、两个时刻、终态与四段正文各带计数；不含其它任何标识。

    刻意不带任务、会话、用户、链路、执行者与版本标识：导出物离开数据库之后就只剩文件
    权限这一道控制，能少带的关联信息一律不带；要回溯到人时按行标识回库查。
    """
    payload = {
        "id": row_id,
        "created_at": created_at.isoformat(),
        "task_created_at": (
            None if record.task_created_at is None else record.task_created_at.isoformat()
        ),
        "terminal_kind": record.terminal_kind,
        "user_result": record.user_result,
        "failure_code": record.failure_code,
        "output_safety_withheld": record.output_safety_withheld,
        "question_content": record.question_content,
        "question_redaction_count": record.question_redaction_count,
        "answer_delivered": record.answer_delivered,
        "answer_delivered_redaction_count": record.answer_delivered_redaction_count,
        "answer_model_raw": record.answer_model_raw,
        "answer_model_raw_redaction_count": record.answer_model_raw_redaction_count,
        "tool_calls": record.tool_calls_payload(),
        "tool_calls_redaction_count": record.tool_calls_redaction_count,
    }
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


def digest_label(hexdigest: str) -> str:
    """``sha256:<十六进制>``，与审计 ``target_digest`` 列的形状一致。"""
    if not re.fullmatch(r"[0-9a-f]{64}", hexdigest):
        raise ValueError("摘要必须是 64 位小写十六进制")
    return f"sha256:{hexdigest}"


def rows_digest(row_ids: Sequence[str]) -> str:
    """返回行标识**有序**列表的摘要：事后拿同一段行标识可复算「这次读了哪一段」。"""
    hasher = hashlib.sha256()
    for row_id in row_ids:
        hasher.update(row_id.encode("utf-8"))
        hasher.update(b"\n")
    return digest_label(hasher.hexdigest())


def read_result_counts(user_ids: Iterable[str]) -> dict[str, int]:
    """审计 ``result_counts``：返回了几行、涉及几个人；只有计数，没有任何标识。"""
    ids = list(user_ids)
    return {"rows": len(ids), "users": len(set(ids))}


def _moment_code(moment: datetime | None) -> str:
    if moment is None:
        return OPEN_MOMENT
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def window_evidence_ref(since: datetime | None, until: datetime | None) -> str:
    """读取的证据指针 ``corpus_window:<起>_<止>``：无界一端写 ``open``，时刻按 UTC 编码。"""
    return f"corpus_window:{_moment_code(since)}_{_moment_code(until)}"


def export_evidence_ref(name: str) -> str:
    """导出的证据指针 ``corpus_export:<文件名>``；文件名先过 :func:`checked_export_name`。"""
    return f"corpus_export:{checked_export_name(name)}"


@dataclass(frozen=True)
class CapacityThresholds:
    """容量水位的两条阈值：行数与表总字节数（含索引），任一越过即告警、都不删。"""

    max_rows: int
    max_bytes: int

    def __post_init__(self) -> None:
        """两条阈值都必须是正整数。"""
        for name in ("max_rows", "max_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")


def capacity_watermark(
    rows: int, total_bytes: int, thresholds: CapacityThresholds
) -> tuple[str, ...]:
    """返回被越过的水位名（``rows`` / ``bytes``）；空元组表示两条都在阈值之内。

    「不到期」不等于「不清理」，但容量只告警不删：调用方拿这个结果去重告警，删除只走
    登记在迁移文件头的受控 SQL。scheduler 侧的接线是后续工作项，这里先备好判定。
    """
    for name, value in (("rows", rows), ("total_bytes", total_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} 必须是非负整数")
    crossed = []
    if rows >= thresholds.max_rows:
        crossed.append("rows")
    if total_bytes >= thresholds.max_bytes:
        crossed.append("bytes")
    return tuple(crossed)
