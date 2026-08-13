"""外部文本的角色边界与模型输出的最后一道约束。

这不是通用内容审核器，也不是把模型行为假设成可靠的 prompt firewall。它只做两件
确定性的事：把外部系统返回的自由文本包成不可伪造的「待分析内容」数据段，并在
worker 输出离开进程前移除已知的敏感值、内部工具标识和系统提示。工具是否能执行
仍由 ``ToolGateway`` 判定；这里不复制工具白名单或权限规则。

模块保持在 ``core``，不读环境、不访问网络或文件，便于用固定夹具证明负向边界。

出口约束的产品合同（[#141](https://github.com/Moshuiwang/lingxi/issues/141)、
[#142](https://github.com/Moshuiwang/lingxi/issues/142)、
[#149](https://github.com/Moshuiwang/lingxi/issues/149)，产品负责人 2026-08-13 拍板）：

1. 能确定边界的敏感片段只替换对应片段，一次命中不得让整段有效结论消失。
2. 只有在已知敏感内容覆盖了全部可展示正文、没有任何有效业务内容幸存时，
   才整段拒发（``withheld``），使用独立终态，不伪装成功。
3. 系统提示类标记检测覆盖 ASCII 大小写变体、全角折叠、有界零宽字符穿插与
   繁简变体（见 ``_SYSTEM_PROMPT_MARKERS``），但这是**针对已知复现样本的定向
   加固**，不是通用 NFKC/同形字防护。片段切分只施加于 ``system_prompt``——
   它是唯一「哪怕泄露一句也不允许」的敏感来源；``forbidden_values``（生产上
   即 ``external_texts``）不做片段切分，因为那类内容是模型被期望原样引用的
   合法业务依据，片段级匹配会把正常问答整体遮蔽。检测方式是**确定性的结构化
   切分**（换行、句末标点、固定长度下界），不是语义相似度或模糊匹配。
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple, TypeAlias


EXTERNAL_TEXT_LABEL = "待分析内容"
SAFE_OUTPUT_FALLBACK = "本次未取得可确认结果，请稍后重试。"
# 与 SAFE_OUTPUT_FALLBACK 刻意不同：后者是"模型没给出内容"，与安全无关；这一条
# 专门用于"已知敏感内容覆盖了全部正文，没有可安全展示的业务结论"（#141 的
# withheld 终态）。两者语义不同，不能共用同一句文案，否则运营无法从文案本身
# 区分"这轮到底发生了什么"。
WITHHELD_MESSAGE = "本次结果涉及需要保护的内容，已被安全策略拦截，未能提供结果。"

ExternalTextItems: TypeAlias = Mapping[str, object] | Iterable[tuple[str, object]]

_SOURCE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
# mcp__ 工具名后缀有界（{1,40}）：本仓库真实工具名形如
# ``mcp__bi-metric__list_metrics``（约 30 字符），40 留有余量又不至于让流式场景
# 的"跨块保留窗口"膨胀到拖慢正常短回答——已知工具名另由 internal_tool_names
# 的精确匹配兜底，这条正则只是捕捉未登记名称的通用背兜。
_PROCESS_MARKERS = re.compile(
    r"(?:\bmcp__[A-Za-z0-9_.-]{1,40}\b|\b(?:pretooluse|posttooluse|posttoolfailure)\b|"
    r"\b(?:tool_use_id|trace_id)\s{0,4}=?)",
    re.IGNORECASE,
)
_PROCESS_MARKER_MAX_LEN = 45
# 英文分支沿用原有 system[_ -]?prompt，放宽到 0-4 个空白/连接符，覆盖
# "System_Prompt"、"system - prompt" 这类轻微变体；中文分支是 #142 缺口二的
# 修复——原实现只认英文，中文用户反而没有防护。两个分支都是**字面 token 匹配**，
# 不做语义判断，符合"不做无边界模糊拦截"的合同要求。
#
# 归一化绕过面（复查发现，#149 修复）：模型可以被诱导用全角字母、在字符间插入
# 零宽字符、或用繁体「系統」代替简体「系统」来吐出标记，逃过纯字面匹配。以下
# 只针对这三类具体复现样本（全角、零宽插入、繁简变体），不是通用 NFKC/同形字
# 防护——那需要更大范围的评估，不在本次修复范围内。

# 全角折叠：把 U+FF01-FF5E（全角 ASCII，含全角字母/数字/标点）与 U+3000
# （全角空格）逐字符折成对应半角字符。这是**逐字符 1:1** 映射，不改变字符串
# 长度或位置，折叠后在其上匹配到的偏移量可以直接复用在原文上，不需要额外的
# 位置重映射（区别于"零宽字符剥离"那种会改变长度的做法）。
_FULLWIDTH_FOLD = str.maketrans(
    {chr(code_point): chr(code_point - 0xFEE0) for code_point in range(0xFF01, 0xFF5F)}
    | {"　": " "}
)


def _fold_for_marker_matching(text: str) -> str:
    """只为系统提示标记检测折叠全角字符；不改变正文本身，也不改变长度。"""

    return text.translate(_FULLWIDTH_FOLD)


# 零宽字符容忍：攻击者可以在标记字面量的字符之间插入零宽字符（渲染不可见）
# 打断连续子串匹配。用**有界**重复（{0,4}，与既有 [\s_-]{0,4} 同一纪律）而不是
# 无界 ``*``——无界重复会让"一个命中最长可能多少字符"失去上界，破坏
# ``StreamingOutputGuard`` 有界尾部缓冲的前提（见下面 ``_max_pattern_length``
# 用到的 ``_SYSTEM_PROMPT_MARKER_MAX_LEN``）。
_ZERO_WIDTH_CHARS = "​‌‍⁠﻿"
_ZW_GAP = f"[{_ZERO_WIDTH_CHARS}]{{0,4}}"


def _zw_tolerant(literal: str) -> str:
    """把字面量拆成字符间允许穿插有界零宽字符的正则片段。"""

    return _ZW_GAP.join(re.escape(char) for char in literal)


_SYSTEM_PROMPT_MARKERS = re.compile(
    r"(?:\b"
    + _zw_tolerant("system")
    + r"[\s_-]{0,4}"
    + _zw_tolerant("prompt")
    + r"\b|"
    + _zw_tolerant("系")
    + _ZW_GAP
    + r"[统統]"
    + _ZW_GAP
    + r"[\s]{0,4}"
    + _zw_tolerant("提示")
    + r"(?:"
    + _zw_tolerant("词")
    + r"|"
    + _zw_tolerant("语")
    + r")?)",
    re.IGNORECASE,
)
# 上界推导（英文分支是最长的一支）："system" 6 字符、5 个字符间隙；连接符
# 间隙至多 4；"prompt" 6 字符、5 个字符间隙；每个零宽间隙至多 4 个字符：
# 6 + 5*4 + 4 + 6 + 5*4 = 56。中文分支明显更短。改坏这个推导（例如改大零宽
# 间隙上限却不同步改这个常量）由 test_input_safety.py 的最坏情况用例验红。
_SYSTEM_PROMPT_MARKER_MAX_LEN = 56

# 片段检测的下界：短于它的候选片段大多是常见短词，拦截它们只会误伤正常业务
# 回答（合同第 4 条「不做无边界模糊拦截」）。具体取值是实现决定，由白盒测试与
# 变异验红固化，不是可调的运行期配置。
_MIN_FRAGMENT_CHARS = 6
# 结构性切分边界：换行与中英文句末标点。不含逗号、冒号等弱边界——切得越碎，
# 短片段误伤正常文本的概率越高；只切句子级边界是精度与召回之间的确定性折中。
_FRAGMENT_BOUNDARY = re.compile(r"[\n。！？.!?;；]+")

# "命中区间之外是否还幸存真实业务内容"的判定不能用 ``str.strip()``——它只剔除
# 空白，遮蔽后残留的孤立标点（比如整段被替换后只剩一个句号）会被误判成"幸存
# 的业务结论"，进而放弃本该触发的 withheld 终态（复查发现，见 #149 修复说明）。
# 改用"是否存在至少一个词/数字/CJK 字符"：\w 在 Python 的 Unicode 正则里覆盖
# 字母、数字与 CJK 表意文字，恰好排除纯标点/空白残渣。
_MEANINGFUL_CONTENT = re.compile(r"\w", re.UNICODE)

_PLACEHOLDER_VALUE = "【已隐藏】"
_PLACEHOLDER_TOOL = "【内部能力已隐藏】"
_PLACEHOLDER_PROCESS = "【内部标识已隐藏】"
_PLACEHOLDER_SYSTEM = "【内部提示已隐藏】"
# 复合命中（同一段文字同时撞上多类规则）时，展示这一个通用占位符，不逐类堆叠——
# 堆叠只会把"到底藏了什么"的猜测空间变大，而不是变小。
_PLACEHOLDER_GENERIC = "【已隐藏】"

# 决定"复合命中时展示哪个占位符"与"reasons 输出顺序"的固定优先级：工具名泄露
# 最具体也最敏感，其次是已知敏感值/片段，再次是系统提示与内部过程标记。
_REASON_PRIORITY: tuple[str, ...] = (
    "internal_tool_name",
    "forbidden_value",
    "forbidden_fragment",
    "system_prompt_marker",
    "process_marker",
)
_PLACEHOLDER_BY_REASON: Mapping[str, str] = {
    "internal_tool_name": _PLACEHOLDER_TOOL,
    "forbidden_value": _PLACEHOLDER_VALUE,
    "forbidden_fragment": _PLACEHOLDER_VALUE,
    "process_marker": _PLACEHOLDER_PROCESS,
    "system_prompt_marker": _PLACEHOLDER_SYSTEM,
}


class InputSafetyError(ValueError):
    """外部文本封装请求不满足可判定的结构约束。"""


@dataclass(frozen=True)
class OutputConstraintResult:
    """输出约束的结果；只保留原因码，不把被拦截的原文带到出口。

    ``blocked`` 表示发生过任何改写（局部遮蔽、整段拒发或空产出兜底三选一）；
    ``withheld`` 单独标记"整段拒发"这一类——合同要求它有独立、可查询的判定，
    不能靠"blocked=True 且 text 恰好等于某个字符串"这种脆弱的隐式约定去猜。
    """

    text: str
    blocked: bool
    withheld: bool
    reasons: tuple[str, ...]


class _Span(NamedTuple):
    start: int
    end: int
    reason: str


def normalize_external_texts(values: ExternalTextItems | None) -> tuple[tuple[str, str], ...]:
    """规范化外部文本并固定遍历顺序。

    MCP / 花名册字段的来源名是受控的调用方元数据，文本本身完全不可信。映射输入
    按来源名排序，避免调用方把 ``set`` / ``dict`` 的遍历顺序带进 prompt 或证据。
    """

    if values is None:
        return ()
    if isinstance(values, Mapping):
        items = tuple(values.items())
    else:
        items = tuple(values)

    normalized: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise InputSafetyError("外部文本必须是 source 与 text 的二元组")
        source, text = item
        if not isinstance(source, str) or _SOURCE_NAME.fullmatch(source) is None:
            raise InputSafetyError("外部文本来源名不合法，不能进入上下文")
        normalized.append((source, text if isinstance(text, str) else str(text)))
    return tuple(sorted(normalized, key=lambda pair: pair[0]))


def wrap_external_text(source: str, text: object) -> str:
    """把一段外部自由文本标为数据，而不是系统指令。

    载荷中的 XML / 方括号边界都经过实体编码：恶意文本即使包含完整的开闭标签或
    ``[/待分析内容]``，也只能成为数据字符，不能闭合本侧边界。这里保留可读正文，
    不把外部输入丢弃或当成权限授予。
    """

    normalized = normalize_external_texts(((source, text),))
    safe_source, safe_text = normalized[0]
    escaped_text = _escape_external_payload(safe_text)
    return (
        f'<lingxi-external-content role="data" source="{safe_source}">\n'
        f"[{EXTERNAL_TEXT_LABEL}]\n"
        f"{escaped_text}\n"
        f"[/{EXTERNAL_TEXT_LABEL}]\n"
        "</lingxi-external-content>"
    )


def render_external_context(values: ExternalTextItems | None) -> str:
    """渲染一组外部文本；来源顺序固定，内容边界逐段独立。"""

    return "\n\n".join(wrap_external_text(source, text) for source, text in normalize_external_texts(values))


def compose_agent_prompt(question: str, external_texts: ExternalTextItems | None = None) -> str:
    """为受控测试 / 编排调用构造带数据边界的 Agent 输入。"""

    if not isinstance(question, str):
        raise InputSafetyError("Agent 问题必须是字符串")
    context = render_external_context(external_texts)
    return question if not context else f"{question}\n\n{context}"


def constrain_output(
    text: object,
    *,
    forbidden_values: Iterable[object] = (),
    internal_tool_names: Iterable[object] = (),
    system_prompt: str | None = None,
    fallback_text: str = SAFE_OUTPUT_FALLBACK,
    withheld_text: str = WITHHELD_MESSAGE,
) -> OutputConstraintResult:
    """约束模型最终正文，使已知内部 / 敏感内容不能离开 worker。

    调用方把固定假凭据、其他用户标识、外部注入原文和系统提示作为
    ``forbidden_values`` 传入；内部工具名单独列出，便于报告同时覆盖放行与拒绝的
    调用。检测在原文上一次性定位全部命中区间后合并重叠区间再统一替换——不再
    按值逐个 ``str.replace()``，因为那种做法在命中多个值时结果依赖执行顺序，
    也无法回答"这段文字替换后还剩多少真实内容"（#141 的核心缺陷）。

    - 局部命中：只替换命中的区间，其余正文原样保留（合同第 1 条）。
    - 整段命中：合并去重后，若原文里**不属于任何命中区间**的字符全部是空白，
      说明没有任何可安全展示的业务结论，此时才整段拒发（合同第 2 条），
      ``withheld=True``，正文替换为 ``withheld_text``。
    - 模型确实没给出任何内容（空产出）与"内容被安全拦截"是两件不同的事，前者
      用 ``fallback_text`` 且不计入 ``withheld``，避免运营把"模型没查到数据"
      和"结果被安全策略拦下"混为一谈。
    """

    candidate = text if isinstance(text, str) else ""
    spans = _find_spans(
        candidate,
        forbidden_values=forbidden_values,
        internal_tool_names=internal_tool_names,
        system_prompt=system_prompt,
    )
    merged = _merge_spans(spans)
    redacted, reasons, kept_original = _apply_spans(candidate, merged, len(candidate))

    if reasons and not _has_meaningful_content(kept_original):
        _ensure_terminal_text_is_safe(withheld_text, forbidden_values, system_prompt, internal_tool_names)
        return OutputConstraintResult(
            text=withheld_text,
            blocked=True,
            withheld=True,
            reasons=(*reasons, "no_safe_content"),
        )

    if reasons:
        return OutputConstraintResult(text=redacted, blocked=True, withheld=False, reasons=reasons)

    if not candidate.strip():
        _ensure_terminal_text_is_safe(fallback_text, forbidden_values, system_prompt, internal_tool_names)
        return OutputConstraintResult(
            text=fallback_text,
            blocked=True,
            withheld=False,
            reasons=("empty_output",),
        )

    return OutputConstraintResult(text=candidate, blocked=False, withheld=False, reasons=())


@dataclass(frozen=True)
class StreamRelease:
    """一次增量安全处理调用可以对外释放的内容。"""

    text: str
    withheld: bool
    reasons: tuple[str, ...]


class StreamingOutputGuard:
    """跨事件边界的增量输出安全处理（#149 流式出口补充合同）。

    面向 #151/#152 打算做的"回答区逐步更新"：调用方每收到模型的一段文本就
    ``feed()`` 一次，只有确认不会与后续内容组成禁止片段的部分才会被放行；
    ``finish()`` 在回合结束时处理剩余缓冲，并给出"这一整轮到底有没有幸存的真实
    业务内容"的最终判定。

    实现方式是一个**有界尾部缓冲**：已知敏感值/片段/工具名/标记里最长的一条
    决定了"最坏情况下一个命中还需要多少字符才能拼完"，缓冲区只保留这么多"尚未
    确认安全"的尾部字符，更早的部分一旦经过检测就可以释放。这是**确定性**的
    窗口计算，不是对模型输出做语义预测；配置的敏感文本越长，能开始流式释放前
    需要攒的字符也越多——宁可多等，不猜测（与合同"无法确定安全边界时才整段
    拒发"同一立场）。

    ``withheld`` 只能在 ``finish()`` 里被判定：只有看到全文，才能回答"这一整轮
    有没有任何真实内容幸存"。命中前已经放行的占位符替换文本不受影响——占位符
    本身永远不包含被拦截的原文，"事后判定为 withheld"改变的只是"如何呈现这轮
    结果"，不构成已经发生的泄露。调用方如果要在最终判定为 withheld 时撤回
    已经展示的中间内容（例如编辑掉已发送的飞书卡片），由消费方自行处理，这不是
    本对象的职责范围。
    """

    def __init__(
        self,
        *,
        forbidden_values: Iterable[object] = (),
        internal_tool_names: Iterable[object] = (),
        system_prompt: str | None = None,
        fallback_text: str = SAFE_OUTPUT_FALLBACK,
        withheld_text: str = WITHHELD_MESSAGE,
    ) -> None:
        self._forbidden_values = tuple(forbidden_values)
        self._internal_tool_names = tuple(internal_tool_names)
        self._system_prompt = system_prompt
        self._fallback_text = fallback_text
        self._withheld_text = withheld_text
        self._buffer = ""
        self._hold_back = _max_pattern_length(
            self._forbidden_values, self._internal_tool_names, self._system_prompt
        )
        self._withheld = False
        self._finished = False
        self._reasons: list[str] = []
        self._has_real_content = False

    @property
    def withheld(self) -> bool:
        return self._withheld

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(self._reasons)

    def feed(self, chunk: object) -> str:
        """喂入一段增量文本，返回本次可以安全对外释放的部分（可能为空串）。"""

        if self._finished:
            raise InputSafetyError("StreamingOutputGuard 已经 finish，不能再 feed")
        if self._withheld:
            # 合同："withheld 后禁止后续释放"——一旦判定整段拒发，后续片段即使
            # 单独看起来干净，也不能再对外释放，避免拼出一个半真半假的答案。
            return ""
        text = chunk if isinstance(chunk, str) else ""
        if not text:
            return ""
        self._buffer += text

        spans = _merge_spans(
            _find_spans(
                self._buffer,
                forbidden_values=self._forbidden_values,
                internal_tool_names=self._internal_tool_names,
                system_prompt=self._system_prompt,
            )
        )
        safe_boundary = max(0, len(self._buffer) - self._hold_back)
        release_end = safe_boundary
        for start, end, _reasons in spans:
            if start < release_end < end:
                # 安全边界正好切在一个已命中区间中间：把释放点收缩到区间起点，
                # 等下一次 feed 把区间完整纳入已确认区域，不能只放行半个占位符。
                release_end = start
                break
        if release_end <= 0:
            return ""

        window_spans = [(s, e, r) for s, e, r in spans if e <= release_end]
        released, reasons_used, kept = _apply_spans(self._buffer, window_spans, release_end)
        self._buffer = self._buffer[release_end:]
        self._record(reasons_used, kept)
        return released

    def finish(self) -> StreamRelease:
        """处理剩余缓冲并给出本轮的最终判定；只能调用一次。"""

        if self._finished:
            raise InputSafetyError("StreamingOutputGuard 已经 finish，不能重复调用")
        self._finished = True
        if self._withheld:
            return StreamRelease(text="", withheld=True, reasons=tuple(self._reasons))

        tail = self._buffer
        self._buffer = ""
        spans = _merge_spans(
            _find_spans(
                tail,
                forbidden_values=self._forbidden_values,
                internal_tool_names=self._internal_tool_names,
                system_prompt=self._system_prompt,
            )
        )
        released, reasons_used, kept = _apply_spans(tail, spans, len(tail))
        self._record(reasons_used, kept)

        if self._reasons and not self._has_real_content:
            self._withheld = True
            _ensure_terminal_text_is_safe(
                self._withheld_text, self._forbidden_values, self._system_prompt, self._internal_tool_names
            )
            return StreamRelease(text=self._withheld_text, withheld=True, reasons=tuple(self._reasons))

        if not self._reasons and not tail.strip() and not self._has_real_content:
            # 整轮什么都没释放过、也没有任何安全触发：与一次性 constrain_output
            # 的"空产出"分支同一语义，不是安全拦截。
            _ensure_terminal_text_is_safe(
                self._fallback_text, self._forbidden_values, self._system_prompt, self._internal_tool_names
            )
            return StreamRelease(text=self._fallback_text, withheld=False, reasons=("empty_output",))

        return StreamRelease(text=released, withheld=False, reasons=tuple(reasons_used))

    def _record(self, reasons_used: tuple[str, ...], kept: str) -> None:
        for reason in reasons_used:
            if reason not in self._reasons:
                self._reasons.append(reason)
        if _has_meaningful_content(kept):
            self._has_real_content = True


def _has_meaningful_content(text: str) -> bool:
    """判断命中区间之外幸存的文本里是否还有真实业务内容（而不只是标点残渣）。"""

    return _MEANINGFUL_CONTENT.search(text) is not None


def _find_spans(
    candidate: str,
    *,
    forbidden_values: Iterable[object],
    internal_tool_names: Iterable[object],
    system_prompt: str | None,
) -> list[_Span]:
    spans: list[_Span] = []
    forbidden = _unique_texts((*forbidden_values, system_prompt))
    for value in forbidden:
        for start in _iter_substring_positions(candidate, value):
            spans.append(_Span(start, start + len(value), "forbidden_value"))

    # 片段切分（#142 缺口一）只施加于 ``system_prompt``：它是唯一"绝不允许被
    # 引用哪怕一句"的敏感来源。``forbidden_values`` 在生产上唯一来源是
    # ``external_texts``（指标描述等外部业务文本），这类内容是模型回答问题时
    # 被期望原样引用的合法依据——对它做片段级匹配会把"这个指标怎么定义的"这
    # 类正常问答整体遮蔽，超出 #142 缺口一的范围（复查发现）。整串精确匹配
    # （上面的 ``forbidden_value``）仍然覆盖 ``forbidden_values``，只是不再对
    # 它们做结构化切分。
    for fragment in _derive_fragments(system_prompt) if system_prompt else ():
        for start in _iter_substring_positions(candidate, fragment):
            spans.append(_Span(start, start + len(fragment), "forbidden_fragment"))

    for name in _unique_texts(internal_tool_names):
        for start in _iter_substring_positions(candidate, name):
            spans.append(_Span(start, start + len(name), "internal_tool_name"))

    for match in _PROCESS_MARKERS.finditer(candidate):
        spans.append(_Span(match.start(), match.end(), "process_marker"))
    # 系统提示标记在**全角折叠后**的副本上匹配：折叠是逐字符 1:1 映射（见
    # ``_fold_for_marker_matching``），不改变长度或位置，因此这里得到的偏移量
    # 可以直接当作原文 ``candidate`` 上的偏移量使用。
    folded_for_markers = _fold_for_marker_matching(candidate)
    for match in _SYSTEM_PROMPT_MARKERS.finditer(folded_for_markers):
        spans.append(_Span(match.start(), match.end(), "system_prompt_marker"))

    return spans


def _merge_spans(spans: list[_Span]) -> list[tuple[int, int, frozenset[str]]]:
    if not spans:
        return []
    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    merged: list[tuple[int, int, set[str]]] = []
    cur_start, cur_end, cur_reasons = ordered[0].start, ordered[0].end, {ordered[0].reason}
    for span in ordered[1:]:
        if span.start < cur_end:
            cur_end = max(cur_end, span.end)
            cur_reasons.add(span.reason)
        else:
            merged.append((cur_start, cur_end, cur_reasons))
            cur_start, cur_end, cur_reasons = span.start, span.end, {span.reason}
    merged.append((cur_start, cur_end, cur_reasons))
    return [(start, end, frozenset(reasons)) for start, end, reasons in merged]


def _apply_spans(
    text: str,
    merged: list[tuple[int, int, frozenset[str]]],
    window_end: int,
) -> tuple[str, tuple[str, ...], str]:
    """把 ``merged`` 区间在 ``text[:window_end]`` 内替换成占位符。

    返回替换后的文本、按固定优先级排序的原因码，以及"命中区间之外幸存的原文"
    （用于判定是否还有真实业务内容）。``window_end`` 允许流式场景只对缓冲区里
    已确认安全的前缀做替换，其余部分留在缓冲区等待下一次判定。
    """

    result: list[str] = []
    kept: list[str] = []
    reasons_used: set[str] = set()
    cursor = 0
    for start, end, reasons in merged:
        if start >= window_end:
            break
        end = min(end, window_end)
        gap = text[cursor:start]
        result.append(gap)
        kept.append(gap)
        reasons_used.update(reasons)
        result.append(_choose_placeholder(reasons))
        cursor = end
    tail = text[cursor:window_end]
    result.append(tail)
    kept.append(tail)
    ordered_reasons = tuple(reason for reason in _REASON_PRIORITY if reason in reasons_used)
    return "".join(result), ordered_reasons, "".join(kept)


def _choose_placeholder(reasons: Iterable[str]) -> str:
    reason_set = set(reasons)
    for reason in _REASON_PRIORITY:
        if reason in reason_set:
            return _PLACEHOLDER_BY_REASON[reason]
    return _PLACEHOLDER_GENERIC


def _iter_substring_positions(candidate: str, needle: str) -> Iterable[int]:
    if not needle:
        return
    start = 0
    while True:
        idx = candidate.find(needle, start)
        if idx == -1:
            return
        yield idx
        start = idx + 1


def _derive_fragments(value: str) -> tuple[str, ...]:
    """把较长的敏感文本切成可独立命中的片段（#142 缺口一：只泄露一段不拦）。

    只按结构性边界切分（换行、中英文句末标点），不做语义判断；短于下界的片段
    不参与匹配。太短的整体不值得切分——切出来的任何一段要么等于原值（已被整串
    匹配覆盖），要么短于下界，两种情况都没有增量价值。
    """

    if len(value) < _MIN_FRAGMENT_CHARS * 2:
        return ()
    fragments: list[str] = []
    seen: set[str] = set()
    for piece in _FRAGMENT_BOUNDARY.split(value):
        piece = piece.strip()
        if len(piece) >= _MIN_FRAGMENT_CHARS and piece != value and piece not in seen:
            fragments.append(piece)
            seen.add(piece)
    return tuple(fragments)


def _max_pattern_length(
    forbidden_values: tuple[object, ...],
    internal_tool_names: tuple[object, ...],
    system_prompt: str | None,
) -> int:
    lengths = [len(value) for value in _unique_texts((*forbidden_values, system_prompt))]
    lengths.extend(len(name) for name in _unique_texts(internal_tool_names))
    lengths.append(_PROCESS_MARKER_MAX_LEN)
    lengths.append(_SYSTEM_PROMPT_MARKER_MAX_LEN)
    return max(lengths, default=0)


def _ensure_terminal_text_is_safe(
    terminal_text: str,
    forbidden_values: Iterable[object],
    system_prompt: str | None,
    internal_tool_names: Iterable[object],
) -> None:
    if not isinstance(terminal_text, str) or not terminal_text.strip():
        raise InputSafetyError("输出约束的安全终态不能为空")
    values = _unique_texts((*forbidden_values, system_prompt))
    tool_names = _unique_texts(internal_tool_names)
    if any(value in terminal_text for value in values) or any(name in terminal_text for name in tool_names):
        raise InputSafetyError("输出约束的安全终态包含被禁止的原文")


def _escape_external_payload(text: str) -> str:
    escaped = html.escape(text, quote=True)
    # ``html.escape`` 不处理方括号；编码它们，避免载荷伪造本侧可读边界标记。
    return escaped.replace("[", "&#91;").replace("]", "&#93;")


def _unique_texts(values: Iterable[object]) -> tuple[str, ...]:
    unique: set[str] = set()
    for value in values:
        if isinstance(value, str) and value:
            unique.add(value)
    return tuple(unique)
