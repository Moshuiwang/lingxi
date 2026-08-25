"""执行层的合成审计链。

Issue #23 的实测结论决定了这个模块的形状：单一 hook 还原不了"这一轮到底发生了
什么"，必须由三个来源合成——

===================  ==========================================================
要审计的事           来源
===================  ==========================================================
工具抛错             ``PostToolUseFailure`` 回调
调用被拒             **执行层自己在 ``PreToolUse`` 记账**；权限拒绝没有任何 hook
                     回调（``PermissionDenied`` / ``PermissionRequest`` 实测均为
                     0，且已确认不是事件名未注册）
用户实际没拿到结果   **解析工具回执内容**；被 MCP 包成 ``isError=false`` 正常响应
                     的业务失败不会触发 ``PostToolUseFailure``
===================  ==========================================================

因此本模块刻意不把"没有报错"等同于"用户拿到了结果"。无法归类的回执记为
:attr:`ToolResultKind.UNCLASSIFIED`，回合结论相应记为
:attr:`UserResultStatus.UNKNOWN`——未知是合法结论，不用推测填满。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .tool_policy import DenyReasonCode, PolicyVerdict, is_well_formed_tool_name

# 前缀 [A-Za-z0-9_-]* 是为了盖住 access_key / x-auth-token / apiKey 这类变体。
# 这套模式是**尽力而为**的兜底，不是完备保证：无键名上下文的裸 token 盖不住。
# 结构化入参靠 AuditRedactor 的字段白名单（默认拒绝）保证，这里只兜非结构化正文。
_SECRET_WORDS = (
    r"[A-Za-z0-9_-]*"
    r"(?:token|secret|password|passwd|pwd|authorization|auth|key|credential|cookie|session|signature)"
)
_SECRET_KEY = re.compile(rf"({_SECRET_WORDS})", re.IGNORECASE)
# 错误原文来自工具回执，不经过入参那套字段白名单，因此这里按赋值形态兜底脱敏，
# 保证 V-审计-03（审计明细中不出现凭据、完整令牌）在错误路径上同样成立。
_SECRET_ASSIGNMENT = re.compile(
    rf"((?:{_SECRET_WORDS})[\"']?\s*[:=]\s*)"
    rf"(\"[^\"]*\"|'[^']*'|(?:bearer|basic)\s+[^\s,;)}}\]]+|[^\s,;)}}\]]+)",
    re.IGNORECASE,
)
_SECRET_SCHEME = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE)
_MAX_KEPT_TEXT = 200
_MAX_KEPT_ERROR_TEXT = 2000

# 产品合同要求「不在审计中保存凭据、完整令牌」，这是绝对措辞，靠认键名做不到——
# 没有键名上下文的裸令牌永远盖不住。因此对自由文本再加一道**与键名无关的长度上界**。
#
# 判定条件（见 _mask_token_runs）：16 字符以上且**含数字**的连串，或 32 字符以上的
# 任意连串。含数字这一条是为了区分随机令牌和 `SIMULATED_UPSTREAM_FAILURE` 这类
# 全字母错误码——后者是有诊断价值的审计事实，抹掉它等于用可读性换一个假的安全感。
#
# 这个上界的**边界要说清楚**：它对随机生成的令牌是结构性的（长度 ≥16 的随机
# 字母数字串几乎必然含数字），但对纯字母且短于 32 字符的秘密无效。
#
# 曾经写过"那一类由字段白名单承担"——**那句话是假的闭合**：错误正文、回执正文和
# 工具名根本不经过字段白名单，没有任何一道规则覆盖它们里面的纯字母短秘密。这条
# 残余缺口由产品负责人在 PR #36 第三轮审核中知情接受，`V-审计-03` 已按实际能力
# 写明，不得再声称对自由文本的凭据保证是绝对的。
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+/=_-]{16,}")


class ToolResultKind(str, Enum):
    """一次工具回执的归类。"""

    OK = "ok"
    TOOL_ERROR = "tool_error"
    BUSINESS_FAILURE = "business_failure"
    EMPTY_RESULT = "empty_result"
    UNCLASSIFIED = "unclassified"


class UserResultStatus(str, Enum):
    """这一回合里，用户到底有没有拿到业务结果。"""

    OBTAINED = "obtained"
    NOT_OBTAINED = "not_obtained"
    NO_TOOL_CALL = "no_tool_call"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResultRules:
    """把工具回执归类为成功还是业务失败的规则。

    规则是**显式配置**的，不做自然语言猜测。``failure_text_markers`` 默认为空：
    外部 MCP 用什么措辞表达"指标不存在"这类业务失败，必须先从真实回执里确认再
    登记，否则宁可记为 :attr:`ToolResultKind.UNCLASSIFIED`。
    """

    failure_keys: frozenset[str] = frozenset({"error", "errors"})
    truthy_success_keys: frozenset[str] = frozenset({"ok", "success"})
    status_keys: frozenset[str] = frozenset({"status", "state"})
    failure_status_values: frozenset[str] = frozenset({"error", "failed", "failure"})
    # 用 tuple 而不是 frozenset：命中多个键时若按集合迭代顺序取第一个，
    # 分类结果会随 str hash 随机化在进程之间翻转（同一份回执给出不同审计结论）。
    # 判定改成与顺序无关：任一登记集合非空即 OK，全部为空才是 EMPTY_RESULT。
    # metrics 来自 #37 A1 已确认的真实 list_metrics 成功契约；只有非空集合才
    # 判 OK，空集合仍是 EMPTY_RESULT，其他形状仍保守记 UNCLASSIFIED。
    empty_collection_keys: tuple[str, ...] = ("data", "rows", "items", "results", "metrics")
    failure_text_markers: tuple[str, ...] = ()


UNGATED_TOOL_NAME = "<未经执行层判定>"
AUDIT_FAULT_TOOL_NAME = "<审计记账失败>"


@dataclass(frozen=True)
class ToolCallAudit:
    """一次工具调用在审计里的完整投影。

    ``allowed`` 有三个取值：``True`` 放行、``False`` 拒绝、``None`` **本层未判定**。
    第三种不是理论情况：``disallowed_tools`` 这类规则层拦截发生在 ``PreToolUse``
    之前，执行层根本收不到回调，只能从消息流里的错误回执发现它存在（L4a 实测，
    见 Issue #29）。把它记成"放行"会让审计谎报，记成"拒绝"会谎报是本层拦的。
    """

    tool_use_id: str | None
    tool_name: str
    tool_input: Mapping[str, Any]
    allowed: bool | None
    deny_reason_code: DenyReasonCode | None = None
    deny_reason_text: str | None = None
    executed: bool = False
    error: str | None = None
    result_kind: ToolResultKind | None = None

    @property
    def denied(self) -> bool:
        return self.allowed is False

    @property
    def gated(self) -> bool:
        """这次调用是否经过了执行层的判定。"""

        return self.allowed is not None


@dataclass(frozen=True)
class TurnAuditSummary:
    """一个回合的审计结论。可直接落审计表，也可用于门禁断言。"""

    calls: tuple[ToolCallAudit, ...]
    denied_calls: tuple[ToolCallAudit, ...]
    failed_calls: tuple[ToolCallAudit, ...]
    ungated_calls: tuple[ToolCallAudit, ...]
    user_result: UserResultStatus
    final_text_bytes: int
    terminal_result_count: int
    terminal_ok: bool

    @property
    def denied_tool_names(self) -> tuple[str, ...]:
        return tuple(call.tool_name for call in self.denied_calls)

    @property
    def executed_tool_names(self) -> tuple[str, ...]:
        return tuple(call.tool_name for call in self.calls if call.executed)


class AuditRedactor:
    """按显式字段白名单裁剪工具入参。

    默认不收录任何入参字段：新增字段必须显式加入白名单才会进入审计明细，
    避免将来某个工具多一个字段就把业务内容或凭据静默带进审计。
    """

    def __init__(self, allowed_input_fields: Iterable[str] = ()) -> None:
        self._allowed = frozenset(allowed_input_fields)

    def redact(self, tool_input: Any) -> dict[str, Any]:
        if not isinstance(tool_input, Mapping):
            return {}
        redacted: dict[str, Any] = {}
        for key, value in tool_input.items():
            raw = str(key)
            # 字段**名**同样是模型可控文本：未进白名单的字段虽然只记 `omitted`，
            # 键名本身还是会落库。值已经过一道自由文本脱敏，键不过是实现自己的
            # 内部不一致。白名单成员判断仍用原名，脱敏只作用于落库的那份。
            name = _redact_free_text(raw)
            if _SECRET_KEY.search(raw):
                redacted[name] = "[REDACTED]"
            elif raw not in self._allowed:
                redacted[name] = {"omitted": True}
            else:
                redacted[name] = self._summarize(value)
        return redacted

    @staticmethod
    def _summarize(value: Any) -> Any:
        # 数字值原样保留是**有意**的：字段进白名单意味着我们已显式批准记录它，
        # 而 epoch 毫秒、雪花 ID 这类长数字正是排障要看的事实。若某个字段可能
        # 承载凭据，正确的做法是不要把它加进白名单，而不是在这里抹数字。
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str) and len(value) <= _MAX_KEPT_TEXT:
            # 字段进了白名单**不代表它的值可以原样落库**：白名单管的是"记不记这个
            # 字段"，值里混进凭据是另一回事，必须单独过一道。
            return _redact_free_text(value)
        return _digest(value)


class TurnAudit:
    """按回合累积事实的审计记账本。

    调用方（worker）负责把 hook 回调和 SDK 消息流分别喂进来；本类不感知 SDK 类型。
    """

    def __init__(self, *, rules: ResultRules | None = None, redactor: AuditRedactor | None = None) -> None:
        self._rules = rules or ResultRules()
        self._redactor = redactor or AuditRedactor()
        self.start_turn()

    def start_turn(self) -> None:
        """清空累积状态，开始记录新的一个回合。

        ``ClaudeAgentOptions.hooks`` 是**会话级**的，一个会话会跑多个回合。若不翻页，
        ``terminal_result_count`` 从第二回合起必然大于 1、``terminal_ok`` 恒为假，
        ``user_result`` 也会把多个回合的调用混在一起判。调用方每开一个回合必须先调本方法。
        """

        self._calls: list[dict[str, Any]] = []
        self._by_tool_use_id: dict[str, dict[str, Any]] = {}
        self._final_text: str = ""
        self._terminal_result_count: int = 0

    # ---- 来源一 / 来源二：hook 记账 ----

    def record_decision(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        tool_use_id: str | None,
        verdict: PolicyVerdict,
    ) -> None:
        """记录 ``PreToolUse`` 的判定。放行和拒绝都记，拒绝额外带上理由。"""

        record = self._new_record(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_input=tool_input,
            allowed=not verdict.denied,
        )
        record["deny_reason_code"] = verdict.reason_code
        record["deny_reason_text"] = verdict.model_reason if verdict.denied else None

    def record_audit_fault(self, *, tool_name: str, tool_use_id: str | None) -> None:
        """记账本身失败时的最后一道留痕：不做脱敏、不碰入参，只保证不再抛异常。

        判定结果此时已经算出并会照常返回，所以这条记录只说明"这次调用的明细丢了"，
        不说明它被放行还是被拒绝——因此记为本层未判定。
        """

        self._calls.append(
            {
                "tool_use_id": tool_use_id,
                "tool_name": AUDIT_FAULT_TOOL_NAME,
                "tool_input": {},
                "allowed": None,
                "deny_reason_code": None,
                "deny_reason_text": None,
                "executed": False,
                "error": f"审计记账失败，明细丢失（工具名长度 {len(tool_name)}）",
                "result_kind": ToolResultKind.UNCLASSIFIED,
            }
        )

    def record_executed(self, *, tool_name: str, tool_use_id: str | None) -> None:
        """记录 ``PostToolUse``：工具确实执行完毕（不代表业务上成功）。

        找不到对应判定记录时**不丢弃**，补一条"本层未判定"的记录。否则一次真实
        执行成功、却绕过了本层判定的调用会完全不留痕，审计还会反过来断言这个回合
        "没有任何工具调用"——那是一条假的否定结论。屏障失效（hook 注册失败、超时、
        事件名变更）恰恰就落在这条路径上，必须可检出。
        """

        record = self._locate(tool_use_id, tool_name)
        if record is None:
            record = self._new_record(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                tool_input=None,
                allowed=None,
            )
        record["executed"] = True

    def record_failure(
        self,
        *,
        tool_name: str,
        tool_input: Any,
        tool_use_id: str | None,
        error: Any,
    ) -> None:
        """记录 ``PostToolUseFailure``：工具抛错。语义是"工具失败"，不是"用户没拿到结果"。"""

        record = self._locate(tool_use_id, tool_name)
        if record is None:
            record = self._new_record(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                tool_input=tool_input,
                allowed=None,
            )
        record["error"] = _error_text(error)
        record["result_kind"] = ToolResultKind.TOOL_ERROR

    # ---- 来源三：解析工具回执 ----

    def record_tool_result(self, *, tool_use_id: str | None, content: Any, is_error: Any = None) -> ToolResultKind:
        """解析 SDK 消息流里的工具回执，判定用户这一次到底拿到了什么。

        找不到对应 ``PreToolUse`` 记录的失败回执**不丢弃**，而是记成一条"本层未判定"
        的调用。否则规则层（``disallowed_tools``）拦下的调用会在审计里彻底消失——
        L4a 实测中就出现过这一幕。
        """

        kind = classify_tool_result(content, is_error=is_error, rules=self._rules)
        record = self._locate(tool_use_id, None)
        if record is None:
            record = self._new_record(
                tool_use_id=tool_use_id,
                tool_name=UNGATED_TOOL_NAME,
                tool_input=None,
                allowed=None,
            )
            # 成功的回执同样建记录：只记失败会让"执行了但没经过本层判定"这种
            # 最危险的情况恰好隐身。错误正文只在失败时留存，成功回执不留业务数据。
            if kind is not ToolResultKind.OK:
                record["error"] = _error_text(_coerce(content)[0])
        if record["result_kind"] is not ToolResultKind.TOOL_ERROR:
            record["result_kind"] = kind
        return kind

    # ---- 回合收口 ----

    def record_final_text(self, text: Any) -> None:
        self._final_text = text if isinstance(text, str) else ""

    def record_terminal_result(self) -> None:
        self._terminal_result_count += 1

    def summary(self) -> TurnAuditSummary:
        calls = tuple(ToolCallAudit(**record) for record in self._calls)
        denied = tuple(call for call in calls if call.denied)
        failed = tuple(
            call
            for call in calls
            if call.result_kind in {ToolResultKind.TOOL_ERROR, ToolResultKind.BUSINESS_FAILURE}
        )
        return TurnAuditSummary(
            calls=calls,
            denied_calls=denied,
            failed_calls=failed,
            ungated_calls=tuple(call for call in calls if not call.gated),
            user_result=self._user_result(calls),
            final_text_bytes=len(self._final_text.encode("utf-8")),
            terminal_result_count=self._terminal_result_count,
            terminal_ok=bool(self._final_text.strip()) and self._terminal_result_count == 1,
        )

    @staticmethod
    def _user_result(calls: Sequence[ToolCallAudit]) -> UserResultStatus:
        """从各次调用的归类合成回合结论；证据不足时返回 UNKNOWN 而不是猜测。

        **一次成功不能代表整个回合。** 原实现见到任意一条 ``OK`` 就直接判
        :attr:`UserResultStatus.OBTAINED`，于是「``list_metrics`` 成功拿到目录 +
        ``describe_metric`` 业务失败」这种最常见的问数路径会被审计写成"用户拿到了
        结果"——`V-执行-06` 要防的恰好就是这个。工具抛错和完全没有回执的调用同样
        会被这条捷径盖掉。

        本层**没有**区分「承载最终业务结果的调用」和「辅助调用」的信息：那要靠
        执行器知道哪一次回答了用户的问题。因此混合回合一律保守记为
        :attr:`UserResultStatus.UNKNOWN`——宁可说不知道，不可谎报已送达。等执行器
        切片能标出主查询之后，这里才有条件给出比 UNKNOWN 更精确的结论。
        """

        if not calls:
            return UserResultStatus.NO_TOOL_CALL
        # 被本层拒绝的调用不参与"拿没拿到结果"的判断（它压根没执行）；
        # 本层未判定但确实失败了的调用要参与，否则规则层拦截会让结论偏乐观。
        kinds = [call.result_kind for call in calls if not call.denied]
        if not kinds:
            # 全部调用都被本层拒绝：用户确定没拿到结果。
            return UserResultStatus.NOT_OBTAINED
        # 没有回执（result_kind is None）与无法归类同样是"证据不足"，不是成功。
        inconclusive = any(kind is None or kind is ToolResultKind.UNCLASSIFIED for kind in kinds)
        failed = any(
            kind in {ToolResultKind.TOOL_ERROR, ToolResultKind.BUSINESS_FAILURE, ToolResultKind.EMPTY_RESULT}
            for kind in kinds
        )
        if inconclusive:
            return UserResultStatus.UNKNOWN
        if failed:
            # 有成功也有失败：分不清哪一次承载最终结果，记未知；
            # 一次成功都没有：证据足以判定用户没拿到。
            if any(kind is ToolResultKind.OK for kind in kinds):
                return UserResultStatus.UNKNOWN
            return UserResultStatus.NOT_OBTAINED
        return UserResultStatus.OBTAINED

    def _new_record(
        self,
        *,
        tool_use_id: str | None,
        tool_name: str,
        tool_input: Any,
        allowed: bool | None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            # 合法工具名（`mcp__bi-metric__list_metrics` 这种）是必须原样保留的
            # 审计事实，不能被长串抹除吃掉；畸形工具名则是模型可控的任意文本，
            # 按自由文本处理。
            #
            # 这里**有意**不把未知的合法形态工具名投影成哈希或长度：对一个拒绝式
            # 白名单来说，"模型试图调用什么"是最重要的那条审计事实，抹掉它等于
            # 让屏障的留痕失去用处。代价是模型若把凭据当成工具名发出来，它会原样
            # 落进审计——这条已知残余缺口见 `V-审计-03`，由产品负责人知情接受。
            "tool_name": tool_name if is_well_formed_tool_name(tool_name) else _redact_free_text(tool_name),
            "tool_input": self._redactor.redact(tool_input),
            "allowed": allowed,
            "deny_reason_code": None,
            "deny_reason_text": None,
            "executed": False,
            "error": None,
            "result_kind": None,
        }
        self._calls.append(record)
        if tool_use_id:
            self._by_tool_use_id[tool_use_id] = record
        return record

    def _locate(self, tool_use_id: str | None, tool_name: str | None) -> dict[str, Any] | None:
        """按 ``tool_use_id`` 精确定位；没有 id 时只在**无歧义**的情况下按工具名回退。

        回退曾经取"最后一条同名放行记录"，同名调用并行时必然挂错：成功的那次被标成
        失败，真正失败的那次留空，回合结论还会从 NOT_OBTAINED 被拉成 UNKNOWN。
        现在候选多于一条就不猜，返回 ``None`` 让调用方另建一条未判定记录。
        """

        if tool_use_id and tool_use_id in self._by_tool_use_id:
            return self._by_tool_use_id[tool_use_id]
        if tool_name is None:
            return None
        candidates = [
            record
            for record in self._calls
            if record["tool_name"] == tool_name and record["allowed"] is True and record["result_kind"] is None
        ]
        return candidates[0] if len(candidates) == 1 else None


def classify_tool_result(content: Any, *, is_error: Any = None, rules: ResultRules | None = None) -> ToolResultKind:
    """把一次工具回执归类。

    ``is_error`` 为真时直接是工具错误；否则按结构化规则判断业务失败与空结果。
    既不匹配失败规则、又拿不出可识别数据的回执归为
    :attr:`ToolResultKind.UNCLASSIFIED`，由调用方决定如何处理。
    """

    rules = rules or ResultRules()
    if bool(is_error):
        return ToolResultKind.TOOL_ERROR

    text, parsed = _coerce(content)
    # 已登记的失败措辞先判：否则被包在 JSON 数组或对象里的失败措辞会被
    # 结构化规则短路成 OK。
    for marker in rules.failure_text_markers:
        if marker and marker in text:
            return ToolResultKind.BUSINESS_FAILURE
    if parsed is not None:
        structured = _classify_structured(parsed, rules)
        if structured is not None:
            return structured
        # 能解析成 JSON **不等于**用户拿到了结果。既没命中失败规则、也拿不出
        # 可识别的非空数据集合时记为未知，由调用方决定，不得当作已送达。
        return ToolResultKind.UNCLASSIFIED
    if text.strip():
        return ToolResultKind.UNCLASSIFIED
    return ToolResultKind.EMPTY_RESULT


def _classify_structured(parsed: Any, rules: ResultRules) -> ToolResultKind | None:
    if not isinstance(parsed, Mapping):
        if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes)):
            if len(parsed) == 0:
                return ToolResultKind.EMPTY_RESULT
            # 非空数组**不等于**成功：`[{"error": ...}]`、`[{"success": false}]`
            # 都是非空数组。先按失败规则查元素，查不出就是未知，不判 OK——
            # 顶层数组没有可识别的成功结构，"没报错"不能当成"拿到了结果"。
            for element in parsed:
                if isinstance(element, Mapping) and _failure_in_mapping(element, rules):
                    return ToolResultKind.BUSINESS_FAILURE
            return ToolResultKind.UNCLASSIFIED
        return None
    if _failure_in_mapping(parsed, rules):
        return ToolResultKind.BUSINESS_FAILURE
    metrics_present = "metrics" in rules.empty_collection_keys and "metrics" in parsed
    metrics_empty = False
    if metrics_present:
        metrics = parsed.get("metrics")
        if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
            return ToolResultKind.UNCLASSIFIED
        metrics_empty = len(metrics) == 0
    seen_collection = False
    saw_success = False
    saw_unclassified = False
    for key in rules.empty_collection_keys:
        value = parsed.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            seen_collection = True
            if len(value) == 0:
                continue
            if key == "metrics":
                metric_kind = _classify_metrics_collection(value, rules)
                if metric_kind is ToolResultKind.BUSINESS_FAILURE:
                    return metric_kind
                if metric_kind is ToolResultKind.UNCLASSIFIED:
                    saw_unclassified = True
                    continue
            saw_success = True
    # 同一响应里同时出现明确成功集合与模糊 metrics，仍保守记未知；否则一次
    # 辅助集合成功会把畸形业务载荷盖掉，重演 V-执行-17 的回合级问题。
    if saw_unclassified:
        return ToolResultKind.UNCLASSIFIED
    # metrics 是这次真实问数调用的业务载荷。它明确为空时，其他辅助集合即使
    # 非空也不足以证明用户拿到了指标结果；纯 metrics 空集合仍按 EMPTY_RESULT。
    if metrics_empty and saw_success:
        return ToolResultKind.UNCLASSIFIED
    if saw_success:
        return ToolResultKind.OK
    return ToolResultKind.EMPTY_RESULT if seen_collection else None


def _classify_metrics_collection(value: Sequence[Any], rules: ResultRules) -> ToolResultKind:
    """锁定 #37 A1 的稳定成功边界，不把“数组非空”偷换成“业务数据有效”。

    真实 ``list_metrics`` 的条目是有内容的对象。空对象、null、字符串等形状都
    缺少足够证据，记未知；条目自身出现已登记失败形状时，整次回执记业务失败。
    """

    for item in value:
        if not isinstance(item, Mapping) or not item:
            return ToolResultKind.UNCLASSIFIED
        if _failure_in_mapping(item, rules):
            return ToolResultKind.BUSINESS_FAILURE
    return ToolResultKind.OK


def _failure_in_mapping(parsed: Mapping[str, Any], rules: ResultRules) -> bool:
    """按显式登记的失败规则判断一个映射是否表示业务失败。"""

    for key in rules.failure_keys:
        if key in parsed and parsed[key]:
            return True
    for key in rules.truthy_success_keys:
        if key in parsed and parsed[key] is False:
            return True
    for key in rules.status_keys:
        value = parsed.get(key)
        if isinstance(value, str) and value.lower() in rules.failure_status_values:
            return True
    return False


def _coerce(content: Any) -> tuple[str, Any]:
    """把 SDK 的回执内容统一成 (纯文本, 解析出的 JSON 或 None)。"""

    if isinstance(content, Mapping):
        return _dump(content), content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                value = block.get("text")
                parts.append(value if isinstance(value, str) else _dump(block))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
        return text, _try_json(text)
    if isinstance(content, str):
        return content, _try_json(content)
    if content is None:
        return "", None
    return str(content), None


def _try_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return None


def _dump(value: Any) -> str:
    # 入参由模型控制，深度嵌套会让 json.dumps 抛 RecursionError；这里必须兜住，
    # 否则记账异常会沿 PreToolUse 回调向上抛，把拒绝响应一起带走。
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        try:
            return str(value)
        except Exception:  # noqa: BLE001 - 审计取值绝不能反过来打断判定
            return "<unrepresentable>"


def _digest(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else _dump(value)
    raw = text.encode("utf-8")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _error_text(error: Any) -> str:
    text = error if isinstance(error, str) else _dump(error)
    return _redact_free_text(text)[:_MAX_KEPT_ERROR_TEXT]


def redact_free_text(text: str) -> str:
    """自由文本脱敏的**公开出口**。

    审计明细在记账时已经脱敏，但执行器还要把两类模型可控文本送出进程：最终正文，
    以及不在白名单里的工具名（后者在审计对象里**有意**原样保留，见 `V-审计-03` 的
    残余缺口表）。这两处必须在出口再过一道，否则"凭据不进日志、不进证据"就只覆盖
    了审计明细一半的出口。

    这道脱敏的能力边界与 :func:`_redact_free_text` 完全相同，**不是绝对保证**：
    纯字母且短于 32 字符的秘密盖不住。不得据此声称出口是干净的。
    """

    return _redact_free_text(text)


def redact_free_text_with_count(text: str) -> tuple[str, int]:
    """与 :func:`redact_free_text` 输出**逐字节相同**的脱敏结果，额外返回命中次数。

    唯一消费方是内测轮内容级采集（Issue #251/#304 批次 3，
    ``core/innertest_content_capture.py``）：采集要求"凭据类命中即以占位符替换
    并计数标注"，而 :func:`redact_free_text` 只回答"替换后的文本"，回答不了
    "替换了几处"。这里不重新发明一套脱敏规则——三段替换（赋值语句、
    ``bearer``/``basic`` 认证头、含数字或超长的裸令牌串）与 :func:`_redact_free_text`
    逐条对应，只是把"是否真的发生了替换"从 ``sub()`` 的静默副作用改成显式计数。

    ``_TOKEN_RUN`` 那一段尤其不能直接用 ``re.subn()`` 数：``_mask_token_run`` 对
    纯字母且短于 32 字符的候选串**原样放行**（这正是 `V-审计-03` 登记的已知残余
    缺口），``subn()`` 只统计"正则命中了几次"而不管回调有没有真的改写内容，会把
    未被脱敏的候选也计入命中数——因此改用回调内部按"替换前后是否不同"自行计数。

    这个计数只服务采集侧的可观测性标注，**不是**安全边界的一部分：安全边界仍然是
    返回的文本本身。计数带有与 `V-审计-03` 相同的已知局限（纯字母短秘密不计入），
    不得据此声称"命中数为零"等价于"这段文本没有凭据"。
    """

    count = 0

    def _sub_assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]"

    def _sub_scheme(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)} [REDACTED]"

    def _sub_token(match: re.Match[str]) -> str:
        nonlocal count
        run = match.group(0)
        masked = _mask_token_run(match)
        if masked != run:
            count += 1
        return masked

    step = _SECRET_ASSIGNMENT.sub(_sub_assignment, text)
    step = _SECRET_SCHEME.sub(_sub_scheme, step)
    step = _TOKEN_RUN.sub(_sub_token, step)
    # 内部一致性核对（本仓库无 -O 优化运行、assert 在生产路径已有先例见
    # core/daily_report.py）：两条实现必须逐字节同文，任何一次改动让它们分岔
    # 都必须响亮失败，而不是让「计数」与「实际替换的文本」各说各话。
    assert step == _redact_free_text(text)
    return step, count


#: 匹配 URL 查询串里形如 ``?key=value``/``&key=value`` 的一段，只捕获**值**
#: （group 1 是含分隔符与等号的前缀，原样保留；group 2 是要盖住的值）。不要求
#: 完整解析成合法 URL——第三方 SDK 的连接日志可能把 URL 嵌在别的文字里
#: （如 ``"connected to wss://...?ticket=..."``），逐段匹配比先 urlparse 更稳。
_QUERY_PARAM_VALUE = re.compile(r'([?&][^&=\s"\']+=)([^&\s"\']*)')


def redact_query_parameter_values(text: str) -> str:
    """把文本里 URL 查询参数的**值**统一替换为掩码，参数名保留（Issue #176）。

    第三方 SDK（如飞书官方 ``lark_oapi``）建立长连接时会把完整 URL 打进日志，
    查询串里可能携带 ``ticket``/``access_key`` 一类一次性凭据材料。这里不假设
    具体参数名单——**宁可过宽**：只要形状匹配查询参数，值一律替换为 ``***``，
    不区分参数名是否"看起来像"凭据。保留参数名是为了让日志仍能诊断出查询串的
    形状（少了哪个参数、参数是否为空），而不是把整段查询串整体抹掉。

    与 :func:`redact_free_text` 是两类互补脱敏，不是重复实现：那一个按**键名
    关键词**匹配（``token``/``secret``/``password`` 等），管的是自由文本里的
    赋值语句；这一个按**URL 查询串的位置**匹配，管的是查询参数值本身，不要求
    参数名匹配任何关键词——两者可以同时施加，互不冲突。
    """

    return _QUERY_PARAM_VALUE.sub(lambda m: m.group(1) + "***", text)


def _redact_free_text(text: str) -> str:
    """自由文本（错误原文、回执正文、畸形工具名）的脱敏。

    比 :func:`_redact_secrets` 多一道长串抹除，用来兑现合同里「不保存完整令牌」
    的绝对要求——键名匹配是尽力而为，长度上界是结构性的。
    """

    return _TOKEN_RUN.sub(_mask_token_run, _redact_secrets(text))


def _mask_token_run(match: re.Match[str]) -> str:
    run = match.group(0)
    if any(char.isdigit() for char in run) or len(run) >= 32:
        return f"[REDACTED:{len(run)}字符]"
    return run


def _redact_secrets(text: str) -> str:
    text = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    return _SECRET_SCHEME.sub(lambda m: f"{m.group(1)} [REDACTED]", text)
