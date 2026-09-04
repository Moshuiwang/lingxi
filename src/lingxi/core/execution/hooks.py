"""把工具边界与审计记账接到 hook 事件上的纯逻辑层。

这里刻意不 import Claude Agent SDK：hook 回调的输入输出都是普通字典，因此
"越界调用是否被拒、拒绝有没有被记账"这类判定可以在没有 SDK、没有模型额度的
CI 里被完整覆盖。SDK 绑定见 ``lingxi.adapters.claude_agent_hooks``。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from .audit import TurnAudit
from .tool_policy import ToolPolicy

# 需要注册的 hook 事件。``PostToolUseFailure`` 是工具抛错的唯一来源；
# ``PermissionDenied`` / ``PermissionRequest`` 实测从不触发，保留注册只为持续
# 验证这一结论，不作为审计依据。
HOOK_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
)

OBSERVATION_ONLY_EVENTS: tuple[str, ...] = (
    "PermissionRequest",
    "PermissionDenied",
)

# 认得的事件名全集。收到不在这里、却带着 tool_name 的事件时留痕，不当作无事发生。
# 这条留痕的覆盖面比字面看起来窄——适配器只按 HOOK_EVENTS 与
# OBSERVATION_ONLY_EVENTS 里的名字注册，SDK 真把事件改名的话，我们注册的名字
# 根本不会再被调用，这条分支永远进不去，只在"SDK 用一个我们认不出的事件名
# 回调我们已注册的 matcher"时生效，不能据此声称"事件改名会自动被发现"。
KNOWN_EVENTS: frozenset[str] = frozenset(HOOK_EVENTS) | frozenset(OBSERVATION_ONLY_EVENTS)

# CLI 对超大工具输出的通用截断形态：把结果存成本地临时文件，提示模型改用
# offset/limit 读那个文件——worker 白名单里没有 Read/Bash，模型照着走只会
# 连续撞必然被拒的工具。正则只锁两段固定短语，DOTALL + 非贪婪跨段匹配并加
# 距离上限防止被正常业务结果巧合命中；与消息缓冲上限异常是两个不同问题：
# 那个是会话级读流异常，这个是单次调用正常返回但被 CLI 自己截断改写。
_MCP_OVERSIZE_RESULT_PATTERN = re.compile(
    r"exceeds maximum allowed tokens.{0,2000}?offset and limit parameters",
    re.IGNORECASE | re.DOTALL,
)

# 替换后模型看到的完整文本。三条硬要求：不含 /tmp 路径、不含"读文件"
# "offset/limit"这类分页引导、明确给出可执行的下一步（缩小范围重新查询）。
# 这里是结构性兜底，与运行时提示词各自独立措辞，不做字符串复用——提示词未来
# 裁剪掉相关条款时，这里的改写仍然生效。
MCP_OVERSIZE_RESULT_REWRITE = (
    "本次查询返回的数据量超过单次可处理的上限，原始结果不可用。"
    "请缩小查询范围后重新调用查询工具——例如缩短时间范围、减少指标或公司数量、"
    "或提高聚合粒度。不要尝试读取任何本地文件路径。"
)


def _iter_tool_result_text_fragments(value: Any) -> list[str]:
    """从任意 MCP 回执结构里递归捞出全部文本片段。

    真实 MCP 回执常见形状不止字符串/``{"text": ...}``/纯内容块数组三种——典型
    还有 ``{"content": [{"type": "text", "text": ...}], "isError": ...}``
    （Agent SDK 的 ``CallToolResult`` 外层信封）。``Mapping`` 既读自己的
    ``"text"`` 也读 ``"content"`` 的内容（``content`` 本身可能是字符串或块
    数组），``list``/``tuple`` 逐项递归——只读顶层 ``"text"`` 会让这一最典型的
    真实回执形状永远拿不到文本，改写因此静默不生效。
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        fragments: list[str] = []
        text = value.get("text")
        if isinstance(text, str):
            fragments.append(text)
        content = value.get("content")
        if content is not None:
            fragments.extend(_iter_tool_result_text_fragments(content))
        return fragments
    if isinstance(value, (list, tuple)):
        fragments = []
        for item in value:
            fragments.extend(_iter_tool_result_text_fragments(item))
        return fragments
    return []


def _tool_result_text(tool_response: Any) -> str:
    """把 :func:`_iter_tool_result_text_fragments` 拼成一段文本。

    递归没能捞出任何文本片段时（既没有标准 ``content``/``text`` 字段，也不是
    数组/字符串），兜底整体 JSON dump 一遍再交给正则——不追求精确字段路径，
    只求不因为一个没预料到的形状而漏判一条本该被改写的截断提示。dump 失败
    时退回空串，等价于"不识别"，不是误判为截断。
    """
    fragments = _iter_tool_result_text_fragments(tool_response)
    if fragments:
        return "\n".join(fragments)
    try:
        import json

        return json.dumps(tool_response, ensure_ascii=False, default=str)
    except Exception:  # 拿不出文本时退回"不识别"
        return ""


def _is_oversize_tool_result(tool_response: Any) -> bool:
    """``tool_response`` 是否命中 CLI 截断提示的特征。

    这里不追求 :mod:`audit` 模块 ``_coerce`` 那种完整归类，两者目的不同：那边要把
    回执分类成功/失败，这里只要够用来判断"是不是那条截断提示"。文本提取本身见
    :func:`_tool_result_text`。
    """
    return bool(_MCP_OVERSIZE_RESULT_PATTERN.search(_tool_result_text(tool_response)))


# 包装拒绝熔断阈值：同一回合内非 MCP 工具被拒累计达到该阈值即终止回合，不再
# 烧尽单次处理轮数上限——防的是模型连续用内置工具包装调用问数 MCP、全部撞
# 白名单拒绝、零原生调用的情形。默认值 5，调整需产品重新决策。触发条件是
# 合取，不是单看拒绝次数：拒绝次数达阈值 **且** 本回合零次放行的原生 MCP
# 调用，这样正常任务不会被误熔断，事故特征仍完整保留在触发范围内。
WRAPPER_DENIAL_FUSE_THRESHOLD = 5


class ToolGateway:
    """执行层的唯一工具判定入口。

    默认拒绝由 :class:`~lingxi.core.execution.tool_policy.ToolPolicy` 保证；本类
    负责在返回拒绝**之前**先记账，这样即使模型此后不再提及该次调用，审计里也有
    这次拒绝及其理由。
    """

    def __init__(
        self,
        *,
        policy: ToolPolicy,
        audit: TurnAudit,
        mark_external_side_effect: Callable[[], None] | None = None,
        raw_pre_tool_use: Callable[[str | None, Any], None] | None = None,
        wrapper_denial_fuse_threshold: int = WRAPPER_DENIAL_FUSE_THRESHOLD,
    ) -> None:
        """装配判定所需的端口与本回合各项计数的初始状态。"""
        self._policy = policy
        self._audit = audit
        self._mark_external_side_effect = mark_external_side_effect
        # 内测轮内容级采集的唯一原始入参出口（可选、默认 None）：``self._audit``
        # 记的是经字段白名单裁剪过的入参，采集要的是裁剪之前的原始值，因此不能
        # 从 ``self._audit`` 反推，必须在这里另开一个独立分支。默认 None 时这个
        # 分支整体不存在，不产生任何额外调用——这是"默认关闭"在这一层的具体
        # 形状：不是多一个 if 分支跳过写库，而是收集器压根没被构造出来。
        self._raw_pre_tool_use = raw_pre_tool_use
        # 语义化进度的工具调用开始通知：默认 None，由 set_tool_call_listener
        # 按回合装配。不做成构造参数——ToolGateway 只建一次，而这个监听器要跟
        # 着每一次 run_turn() 调用传入的回调走（回调闭包了那一次任务的进度
        # 状态），因此需要一个可以在构造之后重新挂载的入口。
        self._on_tool_call: Callable[[str], None] | None = None
        # 包装拒绝熔断：同一回合内累计的「非 MCP 工具被拒」次数。与 TurnAudit
        # 的回合级状态同一姿态——按调用方约定，必须在每次尝试开头调
        # reset_wrapper_denial_fuse()，否则第二个回合会带着第一个回合的计数
        # 继续累加。
        self._wrapper_denial_fuse_threshold = wrapper_denial_fuse_threshold
        self._wrapper_denial_count = 0
        # 熔断只通知一次：达到阈值之后同一回合内继续出现的拒绝（中断请求已发
        # 出但 SDK 还没来得及停下来这段真实存在的窗口）不得重复触发回调、重复
        # 留痕。
        self._wrapper_denial_fuse_tripped = False
        self._on_wrapper_fuse_tripped: Callable[[int], None] | None = None
        # 熔断触发条件的合取项：本回合累计的「放行的 mcp__ 前缀工具调用」次数。
        # 与 _wrapper_denial_count 同一姿态——回合级窗口状态，必须跟着
        # reset_wrapper_denial_fuse() 一起清零，不得跨回合/跨尝试累计。
        self._granted_mcp_count = 0

    @property
    def audit(self) -> TurnAudit:
        """本网关绑定的审计出口。"""
        return self._audit

    @property
    def wrapper_denial_count(self) -> int:
        """本回合累计的「非 MCP 工具被拒」次数，供调用方观测/断言。"""
        return self._wrapper_denial_count

    @property
    def granted_mcp_count(self) -> int:
        """本回合累计的「放行的 mcp__ 前缀工具调用」次数，供调用方观测/断言。

        也是熔断触发条件的合取项——见 ``_update_wrapper_denial_fuse`` 与
        ``WRAPPER_DENIAL_FUSE_THRESHOLD`` 上方注释里的取证依据。
        """
        return self._granted_mcp_count

    def reset_wrapper_denial_fuse(self) -> None:
        """开始新的一个回合前清零包装拒绝熔断计数。

        与 ``TurnAudit.start_turn()`` 同一时机、同一姿态：这是"同一回合内"的
        窗口计数，跨回合不累计。调用方必须在每次尝试开头显式调用；
        ``_granted_mcp_count``（合取项计数）与 ``_wrapper_denial_count`` 同一
        时机一起清零，否则 resume-fallback 的第二次尝试会带着第一次尝试里
        放行过的原生调用痕迹，让本该正常触发的熔断被错误地拦下来。
        """
        self._wrapper_denial_count = 0
        self._wrapper_denial_fuse_tripped = False
        self._granted_mcp_count = 0

    def set_wrapper_fuse_listener(self, callback: Callable[[int], None] | None) -> None:
        """登记（或清除）包装拒绝熔断触发时的回调。

        回调只在阈值**第一次**被达到的那一次调用（由 ``_wrapper_denial_fuse_
        tripped`` 哨兵防重入），入参是触发时的累计拒绝次数。这里只负责"发现
        熔断条件已满足"；真正让回合停下来（向 Agent SDK 会话发出 interrupt）
        是调用方的职责——本类是纯逻辑层，不知道、也不需要知道 SDK 会话的存在。
        回调异常不得影响工具判定本身。
        """
        self._on_wrapper_fuse_tripped = callback

    def set_tool_call_listener(self, callback: Callable[[str], None] | None) -> None:
        """登记（或清除）本回合的工具调用开始通知。

        回调收到的是判定之后规范化的 ``tool_name``（合法工具名原样，畸形输入
        已投影成占位符），不是 hook 事件里未经校验的原始 ``tool_name``。既被
        允许也被拒绝的调用都会通知——这只是"模型发起过一次调用"信号，不代表
        调用真的执行了；调用是否真的执行由 ``PostToolUse``/``PostToolUseFailure``
        记账，两者互不影响。回调异常必须被兜住，不能影响工具判定本身。
        """
        self._on_tool_call = callback

    async def on_hook_event(
        self,
        hook_input: Mapping[str, Any],
        tool_use_id: str | None = None,
        _context: Any = None,
    ) -> dict[str, Any]:
        """Agent SDK 的 hook 回调签名。返回空字典表示不干预。"""
        event = hook_input.get("hook_event_name")
        tool_name = hook_input.get("tool_name")
        tool_input = hook_input.get("tool_input")
        call_id = tool_use_id or hook_input.get("tool_use_id")

        if event == "PreToolUse":
            return self._on_pre_tool_use(tool_name, tool_input, call_id)
        if event == "PostToolUse" and isinstance(tool_name, str):
            if self._is_side_effecting_tool(tool_name):
                self._mark_side_effect()
            self._audit.record_executed(tool_name=tool_name, tool_use_id=call_id)
            # 范围刻意只限只读问数 MCP：改写权限本身就是"能替换模型看到的工具
            # 结果"，只在本 Story 唯一放行的只读面上开这个口子。非 MCP 工具即使
            # 输出恰好命中同一段截断特征，也不改写——``updatedMCPToolOutput``
            # 本来就只对 MCP 生效（SDK 类型声明），这里的前缀判断是双重把关。
            if tool_name.startswith("mcp__") and _is_oversize_tool_result(
                hook_input.get("tool_response")
            ):
                self._audit.record_oversize_rewrite(tool_name=tool_name, tool_use_id=call_id)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedMCPToolOutput": MCP_OVERSIZE_RESULT_REWRITE,
                    }
                }
        elif event == "PostToolUseFailure" and isinstance(tool_name, str):
            if self._is_side_effecting_tool(tool_name):
                self._mark_side_effect()
            self._audit.record_failure(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=call_id,
                error=hook_input.get("error"),
            )
        elif event == "Stop":
            self._audit.record_terminal_result()
        elif event not in KNOWN_EVENTS and isinstance(tool_name, str):
            # 认不出的事件名 + 带工具名 = 判定分支已经失效。本层挡不住（没有别的
            # 手段），但绝不能连痕迹都不留。
            self._audit.record_executed(tool_name=tool_name, tool_use_id=call_id)
        return {}

    def _mark_side_effect(self) -> None:
        if self._mark_external_side_effect is None:
            return
        try:
            self._mark_external_side_effect()
        except Exception:  # 失败也必须保守地继续记审计
            self._audit.record_audit_fault(tool_name="external_side_effect", tool_use_id=None)

    @staticmethod
    def _is_side_effecting_tool(tool_name: str) -> bool:
        """判定一个已执行的工具是否可能产生外部副作用。

        唯一放行能力是只读 MCP；其它真正执行到的工具都按可能有副作用处理。
        ``mcp__delivery__deliver_document`` 不是例外——它只登记/覆盖回合级
        内存状态，没有任何跨进程副作用，真正落库那一步由 UNIQUE 约束与同
        事务写入保证幂等；错误标成"有副作用"会让崩溃恢复把它误判为不安全
        重排。
        """
        return not tool_name.startswith("mcp__")

    def _notify_pre_tool_observers(
        self, verdict_tool_name: str, tool_input: Any, call_id: str | None
    ) -> None:
        """把这次调用同时递给内容采集与进度通知两个可选观察者。

        两者互相独立、失败互不影响，也都不得影响工具判定本身：观察者的可用性
        与本次判定结果无关。
        """
        if self._raw_pre_tool_use is not None:
            try:
                self._raw_pre_tool_use(call_id, tool_input)
            except Exception:  # 采集失败不得影响工具判定本身
                pass
        if self._on_tool_call is not None:
            try:
                self._on_tool_call(verdict_tool_name)
            except Exception:  # 进度通知失败不得影响工具判定本身
                pass

    @staticmethod
    def _build_pre_tool_response(denied: bool, model_reason: str | None) -> dict[str, Any]:
        """按判定结果构造 hook 回调应答；放行时返回空字典表示不干预。"""
        if not denied:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": model_reason,
            }
        }

    def _record_pre_tool_decision(
        self, *, tool_name: str, tool_input: Any, call_id: str | None, verdict: Any
    ) -> None:
        """记账这次判定；记账失败不得降级为放行，只记一次审计缺口。

        必须在应答已经算好之后才调用——记账处理的是模型可控的入参，一旦它
        抛异常，异常会沿 hook 回调向上抛，把这次拒绝一起带走；审计可以失败，
        拒绝不能。
        """
        try:
            self._audit.record_decision(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=call_id,
                verdict=verdict,
            )
        except Exception:  # 见 docstring：审计失败不得降级为放行
            self._audit.record_audit_fault(tool_name=tool_name, tool_use_id=call_id)

    def _update_wrapper_denial_fuse(self, *, denied: bool, tool_name: str) -> None:
        """更新包装拒绝熔断的两支互斥计数，达到阈值时触发一次熔断回调。

        计数口径是「非 MCP 工具被拒」，一律落在 ``tool_name`` 不以 ``mcp__``
        开头这一支，刻意不按拒绝原因码再收窄；放行的 MCP 调用计入独立的合取
        项计数，两支各自独立。合取项判定在这一次拒绝发生的瞬间读取，不是
        回合结束后回溯重算：阈值达成之后的后续放行不会撤销已经发生的触发，
        阈值达成之前的放行则让判定为假、不触发——即"连续打转即熔断，不回溯
        撤销"。
        """
        if denied and not tool_name.startswith("mcp__"):
            self._wrapper_denial_count += 1
            if (
                not self._wrapper_denial_fuse_tripped
                and self._wrapper_denial_count >= self._wrapper_denial_fuse_threshold
                and self._granted_mcp_count == 0
            ):
                self._wrapper_denial_fuse_tripped = True
                if self._on_wrapper_fuse_tripped is not None:
                    try:
                        self._on_wrapper_fuse_tripped(self._wrapper_denial_count)
                    except Exception:  # 熔断通知失败不得影响工具判定本身
                        pass
        elif not denied and tool_name.startswith("mcp__"):
            # 本回合放行过至少一次原生 MCP 调用，说明模型没有陷入"只会包装
            # 绕过、完全摸不到正确工具"的事故模式，不再计入拒绝分支。
            self._granted_mcp_count += 1

    def _on_pre_tool_use(
        self, tool_name: Any, tool_input: Any, call_id: str | None
    ) -> dict[str, Any]:
        """PreToolUse 事件的完整处理：判定 → 通知观察者 → 应答 → 记账 → 更新熔断。"""
        verdict = self._policy.decide(
            tool_name, tool_input if isinstance(tool_input, Mapping) else None
        )
        self._notify_pre_tool_observers(verdict.tool_name, tool_input, call_id)
        response = self._build_pre_tool_response(verdict.denied, verdict.model_reason)
        self._record_pre_tool_decision(
            tool_name=verdict.tool_name, tool_input=tool_input, call_id=call_id, verdict=verdict
        )
        self._update_wrapper_denial_fuse(denied=verdict.denied, tool_name=verdict.tool_name)
        return response
