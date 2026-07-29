"""执行层的运行时工具边界：默认拒绝，只放行显式白名单。

为什么不用 ``allowed_tools`` / ``disallowed_tools`` 作为判定层：Issue #23 的第一轮
验证中，模型执行了一次未被明确禁用的 ``CronCreate``——只列出允许的 MCP 工具不等于
其他内置工具不可执行。因此判定层是本模块，两个 SDK 选项只作为纵深防御的外层。

本模块的核心不变量：**未出现在白名单里的工具名一律拒绝**。新增内置工具、SDK 升级
带来的新工具、模型臆造的工具名，全部自动落入拒绝分支，不需要维护禁用名单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

_VALID_TOOL_NAME = re.compile(r"\A[A-Za-z0-9_.-]+\Z")


class ToolDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class DenyReasonCode(str, Enum):
    """拒绝原因的内部编码。只进审计，不进模型上下文，也不进用户可见文案。"""

    NOT_IN_WHITELIST = "not_in_whitelist"
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


# 这段文案会原样进入模型上下文，同时约束两件事：
# 1. 不要重试——Issue #23 补测中观察到措辞直接影响模型是否重复调用同一工具；
# 2. 不要把内部工具名转述给用户——用户可见文案里不出现内部标识是产品合同要求。
DENY_REASON_TEMPLATE = (
    "该操作不在本次会话批准的只读范围内，已在执行前被拒绝。"
    "请不要重试这个操作，也不要改用其他方式绕过它。"
    "请继续用已批准的查询能力完成用户请求的其余部分；"
    "如果因此无法回答，请直接说明这一部分无法查询，"
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

    @staticmethod
    def _deny(tool_name: str, reason_code: DenyReasonCode) -> PolicyVerdict:
        return PolicyVerdict(
            decision=ToolDecision.DENY,
            tool_name=tool_name,
            reason_code=reason_code,
            model_reason=DENY_REASON_TEMPLATE,
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
