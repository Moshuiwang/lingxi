"""群聊 @ 机器人固定引导（Issue #318）：节流判定 ``GroupMentionHintThrottle`` 与
三条件应答决策 ``GroupMentionHintResponder``。

从 ``apps/gateway/__init__.py`` 纯移动拆出（Trace #373 S-H1-2，Issue #371 第 1 条，
裁定 D10）：只搬定义，不改任何签名、判据或文档字符串。``apps/gateway/__init__.py``
通过 ``from .group_mention_hint import GroupMentionHintResponder,
build_group_mention_hint_throttle`` 取回这两个装配点唯一用到的名字（构造
``GroupMentionHintResponder`` 实例、传给 ``make_event_handler``）；
``GROUP_MENTION_HINT_CONTENT_KEY``/``GroupMentionHintThrottle`` 类本身在
``__init__.py`` 里没有直接使用者，不再重复转发。外部调用点（含
``tests/test_gateway_pipeline.py``）仍然从 ``lingxi.apps.gateway`` 顶层导入
``GroupMentionHintResponder``/``build_group_mention_hint_throttle``，不感知
本次拆分。

**落点选择：留在 ``apps/gateway/`` 而不是 ``core/conversation/``**（D10「实施者
择优」）：``GroupMentionHintResponder.maybe_respond``/``_maybe_respond`` 的唯一
入参类型是 ``lingxi.adapters.feishu_events.NonPrivateChatError``——一个 adapters
层定义的解析异常类型，方法体读取它的 ``chat_id``/``mentioned_open_ids``/
``message_id`` 三个属性。代码框架第二节规则 1 要求 ``core/`` 不 import
``adapters/``；把这两个类移进 ``core/conversation/`` 要么违反这条规则（真的
import 该类型），要么需要新引入一个解耦用的 Protocol/dataclass 来替代
``NonPrivateChatError`` 的类型标注——后者已经不是「纯移动」，会改变本卡不允许
改变的公开签名形状。因此选择同目录内独立模块，与既有
``delivery.py``/``document_delivery.py``/``config.py`` 拆分先例一致：
职责仍然是"把内嵌进入口文件的规则实现挪进独立模块"，解决 #371 第 1 条指出的
``__init__.py`` 体量与"apps 只做组装"外观问题，但不跨越三层 import 边界。
``GroupMentionHintThrottle`` 本身不依赖任何 gateway/adapters 类型，单独搬去
``core/conversation/`` 是可行的，但与 ``GroupMentionHintResponder`` 是同一个
Issue #318 特性里紧耦合的一对（后者持有前者的实例、共用同一份类文档交叉引用），
拆成两处会打散审查单元，因此两者放在同一个新模块。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from lingxi.adapters.feishu_events import NonPrivateChatError
from lingxi.config.content import default_content_catalog
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)


#: 内容目录里群聊@机器人固定引导的键（Issue #318）。
GROUP_MENTION_HINT_CONTENT_KEY = "gateway.group_mention_hint"
#: 同一个群一小时内最多发一条固定引导（Issue #318 待决点 1：防刷屏）。
GROUP_MENTION_HINT_THROTTLE_SECONDS = 3600.0


class GroupMentionHintThrottle:
    """按 ``chat_id`` 节流的进程内存节流器（Issue #318）。

    **已知限制，登记于此**：状态只活在这个 Python 进程的内存里，不落库、不加
    迁移——进程重启（部署、崩溃重启）会让节流窗口清零，重启后紧接着的一次 @
    有可能在"上一次窗口内"又放行一条。代价止步于"同一个群多收到一条不涉及任何
    数据或权限的固定文案"，与本卡「进程内存节流、不建表」的既定取舍一致，不是
    需要另开工作项修的缺陷。

    **判定（``allow``）与记账（``mark_sent``）拆成两个方法**（Issue #328 opus
    审查 P1-1）：此前是一次调用就顺带把节流位记上，`GroupMentionHintResponder`
    因此必须在**发送之前**就消耗掉这次的节流额度——`send_text` 抛出未预期异常
    时，额度已经烧掉但消息其实没发出去，且异常会原样向上抛出 `make_event_
    handler`，飞书按未处理事件重投，重投的这条又被同一节流窗口拦下，形成
    "点了却什么都没发生、之后一小时内都不会再试"的静默故障。现在只有真正调用
    `send_text` **成功之后**才调用 `mark_sent`，失败的发送不消耗额度、下一次
    到来的同一群消息仍然可以重试。

    ``last_sent_at`` 只增不减：本类没有清理路径，键域随「被 @ 过至少一次的
    群」数量增长，量级与真实群聊数同阶，不是无界增长（长期运行如需回收可以
    另开加固项，这里只登记现状，不是缺陷）。
    """

    def __init__(
        self,
        *,
        window_seconds: float = GROUP_MENTION_HINT_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._clock = clock
        self._last_sent_at: dict[str, float] = {}

    def allow(self, chat_id: str) -> bool:
        """只读判定，不产生任何副作用——多次调用（例如失败重试）互不影响。"""

        last = self._last_sent_at.get(chat_id)
        if last is None:
            return True
        return self._clock() - last >= self._window_seconds

    def mark_sent(self, chat_id: str) -> None:
        """真正发送成功之后才调用，记下这次节流窗口的起点。"""

        self._last_sent_at[chat_id] = self._clock()


def build_group_mention_hint_throttle(
    *,
    window_seconds: float = GROUP_MENTION_HINT_THROTTLE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> GroupMentionHintThrottle:
    return GroupMentionHintThrottle(window_seconds=window_seconds, clock=clock)


class GroupMentionHintResponder:
    """群聊 @ 机器人固定引导（Issue #318）：要不要发这条提示的唯一判定入口。

    三个条件同时成立才发送，任一不成立都维持此前"群聊完全静默"的行为：

    1. ``bot_open_id`` 已配置——未配置＝功能整体关闭，失败关闭（合同边界见
       ``GatewayConfig.bot_open_id`` 文档）；
    2. 精确命中：机器人自身 open_id 出现在这条消息的 ``mentioned_open_ids`` 里
       ——@ 别的同事、或者压根没有任何 @，都不算；
    3. 同一个 ``chat_id`` 没有被节流器拦下。

    发送用的是 gateway 既有的出站回复通道（与私聊问数共用同一个
    ``Replies.send_text`` 实现，见 ``build_supervisor``），不新开一条出站路径。
    ``chat_id`` 只进模块 logger 的脱敏日志行（``redact_identifier``，全仓库唯一
    允许缩短飞书标识的地方），不进结构化审计字段——`V-花名册-34` 与
    ``tests/test_roster_audit_duty.py::RedactedIdentifierUsageTest`` 要求它的
    返回值只能作日志参数使用（不可反查也不可比较），与
    ``credential_rotation.py``/``admin_bootstrap/__init__.py`` 等既有先例同一
    姿态；结构化审计（``event.group_mention_hint_sent``）只带内容目录的键/
    版本，不带任何身份或群标识，与 ``onboarding_runner.py``「审计不带 open_id
    （含脱敏形式）」同一条纪律，也不记消息正文——与 ``_LoggingAudit``「审计不记
    用户正文」的既有纪律一致。

    **``maybe_respond`` 整体是尽力而为、失败关闭的旁路**（Issue #328 opus 审查
    P1-1）：`make_event_handler` 在记完 `event.rejected_non_private_chat` 之后
    直接调用它、不包一层 try/except（见调用点文档），此前 `send_text` 抛出的
    任何未预期异常都会原样向上抛穿 `make_event_handler`，被飞书判定为事件处理
    失败而重投——但节流位在发送**之前**已经记上（见 `GroupMentionHintThrottle`
    文档），重投的这条又被同一节流窗口拦下，用户侧表现为"@了一次，什么都没
    发生，之后一小时都不会再试"。现在整个判定与发送过程包在一个 try/except
    里：任何异常都记一条 `event.group_mention_hint_failed`（只含异常类名，不含
    正文或标识）然后正常返回——事件正常 ack，不阻塞、不重投、不带崩本次群聊
    越界判定的其余职责；节流额度只在 `send_text` 真正成功之后才消耗。
    """

    def __init__(
        self,
        *,
        bot_open_id: str | None,
        replies: Any,
        audit: Any,
        throttle: GroupMentionHintThrottle,
    ) -> None:
        self._bot_open_id = bot_open_id
        self._replies = replies
        self._audit = audit
        self._throttle = throttle

    def maybe_respond(self, error: "NonPrivateChatError") -> None:
        try:
            self._maybe_respond(error)
        except Exception as failure:  # noqa: BLE001 - 引导是尽力而为的旁路，
            # 失败不得带走事件 ack、不得让飞书判定为处理失败而重投（见类文档）。
            logger.error(
                "gateway.group_mention_hint.failed chat_id=%s error=%s",
                redact_identifier(error.chat_id) if error.chat_id else None,
                type(failure).__name__,
            )
            self._audit.record("event.group_mention_hint_failed", error=type(failure).__name__)

    def _maybe_respond(self, error: "NonPrivateChatError") -> None:
        if not self._bot_open_id:
            return
        if self._bot_open_id not in error.mentioned_open_ids:
            return
        if not error.chat_id or not error.message_id:
            # 结构异常导致读不出 chat_id/message_id：没有地方可回，维持静默
            # （与本类其余分支同一条"读不出就当没有"的纪律）。
            return
        if not self._throttle.allow(error.chat_id):
            return

        content = default_content_catalog().text(GROUP_MENTION_HINT_CONTENT_KEY)
        self._replies.send_text(
            chat_id=error.chat_id,
            thread_id=None,
            reply_to_message_id=error.message_id,
            text=content.text,
        )
        # 只有发送真正成功才消耗节流额度——见 `GroupMentionHintThrottle` 文档。
        self._throttle.mark_sent(error.chat_id)
        # `chat_id` 只写进这一行脱敏日志，不进下面的结构化审计字段——见本类文档
        # 「`V-花名册-34`」段落。
        logger.info(
            "gateway.group_mention_hint.sent chat_id=%s", redact_identifier(error.chat_id)
        )
        self._audit.record(
            "event.group_mention_hint_sent",
            content_key=content.key,
            content_version=content.version,
        )
