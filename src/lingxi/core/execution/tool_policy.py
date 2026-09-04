"""执行层的运行时工具边界：默认拒绝，只放行显式白名单。

为什么不用 ``allowed_tools`` / ``disallowed_tools`` 作为判定层：Issue #23 的第一轮
验证中，模型执行了一次未被明确禁用的 ``CronCreate``——只列出允许的 MCP 工具不等于
其他内置工具不可执行。因此判定层是本模块，两个 SDK 选项只作为纵深防御的外层。

本模块的核心不变量：**未出现在白名单里的工具名一律拒绝**。新增内置工具、SDK 升级
带来的新工具、模型臆造的工具名，全部自动落入拒绝分支，不需要维护禁用名单。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

_VALID_TOOL_NAME = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
_MCP_TOOL_PREFIX = "mcp__"
_CONTROLLED_TOOL_NAMES = frozenset({"Skill"})

# 问数 MCP 服务的原生查询工具前缀（如 ``mcp__query__query_metric``）。只用于判定
# 「查询包装式越界」的重定向场景（见 ``DENY_REDIRECT_TEMPLATE``），不是新的白名单
# 匹配规则——白名单仍然只接受精确工具名，这里只读已经在白名单里的工具名判断查询
# 能力本身是否可用。
_QUERY_TOOL_PREFIX = "mcp__query__"


def is_well_formed_tool_name(tool_name: object) -> bool:
    """工具名是否是合法标识符形态。

    审计侧用它区分两种情况：合法工具名（如 ``mcp__bi-metric__list_metrics``）
    是必须原样保留的审计事实；畸形工具名则是模型可控的任意文本，要按自由文本
    脱敏。
    """

    return isinstance(tool_name, str) and _VALID_TOOL_NAME.fullmatch(tool_name) is not None


class ToolDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class DenyReasonCode(str, Enum):
    """拒绝原因的内部编码。只进审计，不进模型上下文，也不进用户可见文案。"""

    NOT_IN_WHITELIST = "not_in_whitelist"
    # 「查询包装式越界」的重定向场景（2026-08-27 事故，见 DENY_REDIRECT_TEMPLATE）：
    # 被拒工具本身不是原生查询工具，但白名单里确实有查询工具可用——说明模型只是
    # 调用方式用错了，不是查询能力真的不可用。与 NOT_IN_WHITELIST 分开编码，供
    # 观测区分「越界后无路可走」和「越界但本该改走原生工具」这两类不同性质的拒绝。
    NOT_IN_WHITELIST_QUERY_REDIRECT = "not_in_whitelist_query_redirect"
    SKILL_NOT_APPROVED = "skill_not_approved"
    MISSING_SKILL_NAME = "missing_skill_name"
    MALFORMED_TOOL_NAME = "malformed_tool_name"


@dataclass(frozen=True)
class PolicyVerdict:
    """一次工具调用的判定结果。

    ``model_reason`` 会原样进入模型上下文，因此它的措辞是产品行为的一部分，
    见 :data:`DENY_REASON_TEMPLATE`。``reason_code`` 只进审计。
    """

    decision: ToolDecision
    tool_name: str
    reason_code: DenyReasonCode | None = None
    model_reason: str | None = None

    @property
    def denied(self) -> bool:
        return self.decision is ToolDecision.DENY


# 这段文案会原样进入模型上下文，同时约束三件事：
# 1. 不要重试——Issue #23 补测中观察到措辞直接影响模型是否重复调用同一工具；
# 2. 不要把内部工具名转述给用户——用户可见文案里不出现内部标识是产品合同要求；
# 3. 不要编造归因（Issue #291）——2026-08-21 真实事故：白名单前缀配错导致真实
#    工具全被拒绝，模型按旧模板"用业务语言说明无法查询"的指引，自行把"本侧配置
#    错误"翻译成了"用户账号缺权限"，四条回复一致建议用户"联系数据平台管理员""重新
#    登录后重试"——而事实是该用户权限完全正常，系统上午刚开通完成。旧模板本身没有
#    错，缺的是**明确禁止**这种归因方向：拒绝原因是本侧的临时限制，不是用户的问题，
#    不能让用户去解决一个他解决不了、也不该由他负责的配置问题。
# 注（Issue #349，2026-08-27）：原模板曾写"问题已经被记录"——这是一句本层实际
# 没有兑现的承诺，判定层本身不写任何记录/告警系统，说了也无法验证。已删除，只
# 保留"这是系统侧的临时限制"这一句本层确实能保证为真的表述。
DENY_REASON_TEMPLATE = (
    "该操作不在本次会话批准的只读范围内，已在执行前被拒绝。"
    "请不要重试这个操作，也不要改用其他方式绕过它。"
    "请继续用已批准的查询能力完成用户请求的其余部分；"
    "如果因此无法回答，请直接向用户说明这一部分暂时无法查询，"
    "并明确说明这是系统侧的临时限制，不需要用户自己处理；"
    "绝对不要说这与用户的账号、权限或登录状态有关，"
    "也绝对不要建议用户重新登录、联系管理员或自行申请权限——"
    "这不是用户的问题，不能把本侧的配置限制说成用户这边缺权限；"
    "用业务语言说明，不要向用户提及内部工具名称或本条规则。"
)


# 「查询包装式越界」导回模板（Issue #349，2026-08-27 生产事故修复）。
#
# 事故经过（07:36-08:42 stage 四连问数失败）：qwen3.7-plus 概率性地用
# ``Bash: claude mcp call query …`` 包装调用问数 MCP，命中白名单拒绝；但上面
# ``DENY_REASON_TEMPLATE`` 里"也不要改用其他方式绕过它"这句话，把模型接下来
# 想改用原生 ``mcp__query__`` 工具的**正确**路径也一并禁止了——会话记录证明模型
# 全程看得见原生工具（session jsonl 思考原文："Looking at the tools I have, I
# can see the query_metric…"），却因为这句话不敢调用，转而向用户回复"这一部分
# 暂时无法查询、系统侧临时限制"，续聊上下文又把这个错误结论自我强化。
#
# 与 DENY_REASON_TEMPLATE 的区别只在这一种场景：被拒的调用方式本身不是原生查询
# 工具，但白名单里确实有查询工具可用——查询能力没有真的不可用，模型只是选错了
# 调用方式。这段文案专门把模型导回原生工具，同时保留旧模板的三条纪律（不重试
# 这种越界方式、不向用户暴露内部工具名/规则、不把系统侧限制说成用户的问题）。
#
# 措辞上刻意不出现"暂时无法查询""不可用"这类字样（即使写在否定句里）——2026-08-21
# 与 2026-08-27 两次事故都证明模型会直接转述看到的字面词组，不管它出现在肯定句
# 还是否定句里；唯一可靠的做法是这些词组本身就不出现在文案里。
DENY_REDIRECT_TEMPLATE = (
    "该调用方式已被拒绝，但查询能力本身可用：请不要重试这种调用方式，"
    "也不要再用命令行或任何其他包装方式尝试；"
    "请立即改用本次会话提供的原生查询工具"
    "（工具名以 mcp__query__ 开头）直接完成这次查询。"
    "不要因为这次调用被拒绝，就告诉用户这部分查询有问题或者需要等待——"
    "只是这一种调用方式不被允许，换用原生查询工具即可正常完成，"
    "不要向用户提及这次被拒绝的调用方式或任何内部工具名称。"
    "绝对不要说这与用户的账号、权限或登录状态有关，"
    "也绝对不要建议用户重新登录、联系管理员或自行申请权限——"
    "这不是用户的问题；"
    "用业务语言说明，不要向用户提及内部工具名称或本条规则。"
)


class ToolPolicyError(ValueError):
    """白名单配置本身不合法。构造期就失败，不留到运行期才发现。"""


class ToolPolicy:
    """默认拒绝的工具白名单。

    白名单只接受**精确工具名**。不支持通配符和前缀匹配：``mcp__bi-metric__*``
    这类写法会让 MCP 服务端新增一个写工具就自动获得放行权，与"新增工具默认拒绝"
    的不变量直接冲突，因此在构造期拒绝。
    """

    def __init__(
        self,
        *,
        allowed_tools: object,
        allowed_skills: object = (),
    ) -> None:
        self._allowed_tools = self._freeze_names(allowed_tools, field="allowed_tools")
        self._allowed_skills = self._freeze_names(allowed_skills, field="allowed_skills", allow_empty=True)
        unsupported = sorted(
            name
            for name in self._allowed_tools
            if name not in _CONTROLLED_TOOL_NAMES and not name.startswith(_MCP_TOOL_PREFIX)
        )
        if unsupported:
            raise ToolPolicyError(
                "allowed_tools 只能包含明确批准的 mcp__ 只读工具或 Skill；"
                f"不允许配置内置/派生工具：{', '.join(unsupported)}"
            )
        if "Skill" in self._allowed_tools and not self._allowed_skills:
            raise ToolPolicyError("白名单放行了 Skill 工具，必须同时给出允许的 Skill 名单")

    @property
    def allowed_tools(self) -> frozenset[str]:
        return self._allowed_tools

    @property
    def allowed_skills(self) -> frozenset[str]:
        return self._allowed_skills

    def decide(self, tool_name: object, tool_input: Mapping[str, Any] | None = None) -> PolicyVerdict:
        """判定一次工具调用。任何无法确认为白名单内的输入都返回拒绝。"""

        if not isinstance(tool_name, str) or not _VALID_TOOL_NAME.fullmatch(tool_name):
            return self._deny(self._display_name(tool_name), DenyReasonCode.MALFORMED_TOOL_NAME)
        if tool_name not in self._allowed_tools:
            if self._is_query_redirect_candidate(tool_name):
                return self._deny(tool_name, DenyReasonCode.NOT_IN_WHITELIST_QUERY_REDIRECT)
            return self._deny(tool_name, DenyReasonCode.NOT_IN_WHITELIST)
        if tool_name == "Skill":
            return self._decide_skill(tool_input)
        return PolicyVerdict(decision=ToolDecision.ALLOW, tool_name=tool_name)

    def _decide_skill(self, tool_input: Mapping[str, Any] | None) -> PolicyVerdict:
        """Skill 是一个工具名下的多个能力，因此还要判定具体加载哪个 Skill。"""

        skill_name = None
        if isinstance(tool_input, Mapping):
            skill_name = tool_input.get("skill")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return self._deny("Skill", DenyReasonCode.MISSING_SKILL_NAME)
        if skill_name not in self._allowed_skills:
            return self._deny("Skill", DenyReasonCode.SKILL_NOT_APPROVED)
        return PolicyVerdict(decision=ToolDecision.ALLOW, tool_name="Skill")

    def _is_query_redirect_candidate(self, tool_name: str) -> bool:
        """是否命中「查询包装式越界」的重定向场景。

        条件：被拒的调用本身不是原生查询工具（否则导回它自己没有意义——覆盖
        ``mcp__query__`` 前缀下未获批准的具体工具名被拒的情况），且白名单里
        确实存在至少一个查询工具（否则查询能力本身就不可用，维持旧模板，不能
        许诺一个实际不存在的替代路径）。
        """

        if tool_name.startswith("mcp__"):
            # 任何原生 MCP 工具（含 mcp__query__ 自身与 mcp__delivery__ 等其他
            # 服务器的工具）被拒都不是「包装式越界」：模型用的已经是原生调用
            # 形态，只是该工具本身未获批准——导回「查询工具」在语义上答非所问
            # （2026-08-27 跨批合流时 test_document_delivery 的开关关用例坐实），
            # 维持通用模板。重定向只面向 Bash 之类的非 MCP 包装通道。
            return False
        return any(name.startswith(_QUERY_TOOL_PREFIX) for name in self._allowed_tools)

    @staticmethod
    def _deny(tool_name: str, reason_code: DenyReasonCode) -> PolicyVerdict:
        template = (
            DENY_REDIRECT_TEMPLATE
            if reason_code is DenyReasonCode.NOT_IN_WHITELIST_QUERY_REDIRECT
            else DENY_REASON_TEMPLATE
        )
        return PolicyVerdict(
            decision=ToolDecision.DENY,
            tool_name=tool_name,
            reason_code=reason_code,
            model_reason=template,
        )

    @staticmethod
    def _display_name(tool_name: object) -> str:
        """拒绝畸形工具名时仍要在审计里留下可辨认的痕迹，但不回显任意长内容。"""

        if isinstance(tool_name, str):
            return tool_name[:120] if tool_name else "<empty>"
        return f"<{type(tool_name).__name__}>"

    @staticmethod
    def _freeze_names(values: object, *, field: str, allow_empty: bool = False) -> frozenset[str]:
        if isinstance(values, str) or not hasattr(values, "__iter__"):
            raise ToolPolicyError(f"{field} 必须是名称集合，不能是单个字符串或非可迭代对象")
        names: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ToolPolicyError(f"{field} 含空名称或非字符串项")
            if "*" in value or "?" in value:
                raise ToolPolicyError(f"{field} 不允许通配符：{value}；白名单只接受精确名称")
            names.add(value)
        if not names and not allow_empty:
            raise ToolPolicyError(f"{field} 不能为空：空白名单意味着没有任何能力，应显式声明")
        return frozenset(names)
