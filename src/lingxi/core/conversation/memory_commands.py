"""``/memory`` 命令面：查询、登记、删除、清空当前用户的记忆。

调用点在事件管线的忙碌判定之前（与 ``/stop`` 同一姿态）：记忆是元数据操作，不需要
等当前任务跑完。三个写操作各记一条审计；``list`` 与用法提示是只读、无副作用操作，
不单独留痕。用户自己写的记忆正文一律先过内容安全校验再展示或写库——注入侧、展示侧、
登记侧共用同一道检查器，任何一侧漏掉都会让回执与事实不符。
"""

from __future__ import annotations

from lingxi.config.content import (
    ContentSafetyError,
    RenderedContent,
    validate_user_visible_text,
)
from lingxi.core.user_memory import UserMemoryEntry

from .commands import MemoryCommand, MemoryCommandKind
from .gateway_texts import GatewayTexts
from .ports import AuditSink, HandledAs, InboundMessage, Outcome


class MemoryCommandHandler:
    """把一条已解析的 ``/memory`` 命令落成回执、审计与事件终态。"""

    def __init__(self, *, texts: GatewayTexts, audit: AuditSink) -> None:
        """记住文案取值口与审计出口，两者与调用它的管线同一份实例。"""
        self._texts = texts
        self._audit = audit

    def handle(
        self,
        tx,
        message: InboundMessage,
        user,
        conversation,
        memory_command: MemoryCommand,
        deferred: list[RenderedContent],
    ) -> Outcome:
        """按子命令分派；无论走哪一支，这条事件都以"命令"终态收尾。

        Args:
            tx: 当前事务，读写都在它上面完成。
            message: 触发命令的入站消息。
            user: 命令发起人。
            conversation: 命令所在话题，只用于审计字段。
            memory_command: 已解析的子命令。
            deferred: 事务提交后才发出的回执。

        Returns:
            恒为命令终态的处理结论。
        """
        if memory_command.kind is MemoryCommandKind.LIST:
            entries = tx.list_user_memory(user_id=user.user_id)
            deferred.append(self._render_memory_list(entries))
        elif memory_command.kind is MemoryCommandKind.CLEAR:
            self._clear(tx, message, user, conversation, deferred)
        elif memory_command.kind is MemoryCommandKind.FORGET:
            self._forget(tx, message, user, conversation, memory_command, deferred)
        elif memory_command.kind is MemoryCommandKind.REMEMBER:
            self._remember(tx, message, user, conversation, memory_command, deferred)
        else:
            # 以 ``/memory`` 开头但子命令形状不对：给用法提示，不算错误也不审计。
            deferred.append(self._texts.catalog.text("memory.usage_help"))
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
        return Outcome(handled_as=HandledAs.COMMAND)

    def _clear(self, tx, message: InboundMessage, user, conversation, deferred) -> None:
        """清空这个人的全部记忆，回执带上被清掉的条数。"""
        count = tx.clear_user_memory(user_id=user.user_id)
        deferred.append(self._texts.catalog.text("memory.cleared", count=count))
        self._audit.record(
            "command.memory_clear",
            event_id=message.event_id,
            user_id=user.user_id,
            conversation_id=conversation.conversation_id,
            cleared_count=count,
            trace_id=message.trace_id,
        )

    def _forget(
        self, tx, message: InboundMessage, user, conversation, memory_command, deferred
    ) -> None:
        """删掉一条记忆；未命中时只回拒绝文案，不留痕。

        短序号解析先查一次当前列表：这次查询与随后的删除同一个事务、同一条连接，
        中途不会有别的写者插队。未命中（不存在／不属于本人／序号越界）**不审计成
        一次「删除」**——结构上根本没有发生写操作，记下来只会产生「有人尝试删了
        别人一条记忆」这样的误导性事实。
        """
        entries = tx.list_user_memory(user_id=user.user_id)
        target_id = self._resolve_forget_target_id(memory_command, entries)
        forgotten_entry = (
            tx.forget_user_memory(user_id=user.user_id, memory_id=target_id)
            if target_id is not None
            else None
        )
        deferred.append(self._render_forget_receipt(forgotten_entry))
        if forgotten_entry is not None:
            self._audit.record(
                "command.memory_forget",
                event_id=message.event_id,
                user_id=user.user_id,
                conversation_id=conversation.conversation_id,
                memory_id=forgotten_entry.memory_id,
                trace_id=message.trace_id,
            )

    def _remember(
        self, tx, message: InboundMessage, user, conversation, memory_command, deferred
    ) -> None:
        """登记一条记忆；撞线内容在写库之前就拒绝。

        校验用的是与注入侧、展示侧同一道检查器，键和值合成一段一起过（同一次撞线
        判定）。没有这道校验时，用户会先收到「已登记，下一次提问开始生效」的回执，
        而这条记忆在每一次注入时都被静默跳过、永远不生效——回执与事实不符。这里
        改成当场拒绝并说明原因，不产生「说了成功但其实没生效」的落差。
        """
        try:
            validate_user_visible_text(
                f"{memory_command.memory_key}\n{memory_command.memory_value}"
            )
        except ContentSafetyError:
            deferred.append(self._texts.catalog.text("memory.remember_unsafe"))
            return

        memory_id = tx.remember_user_memory(
            user_id=user.user_id,
            memory_type=memory_command.memory_type,
            memory_key=memory_command.memory_key,
            memory_value=memory_command.memory_value,
        )
        if memory_id is None:
            deferred.append(self._texts.catalog.text("memory.limit_exceeded"))
            return
        deferred.append(self._texts.catalog.text("memory.remembered"))
        self._audit.record(
            "command.memory_remember",
            event_id=message.event_id,
            user_id=user.user_id,
            conversation_id=conversation.conversation_id,
            memory_id=memory_id,
            memory_type=memory_command.memory_type,
            trace_id=message.trace_id,
        )

    def _render_memory_list(self, entries: list[UserMemoryEntry]) -> RenderedContent:
        """把 ``/memory list`` 查到的记忆渲染成用户可见文本。

        行首用从 1 开始的短序号而不是裸内部标识：``/memory forget <序号>`` 引用的
        就是它。序号能和删除时刻对上**不是靠两处约定一致**，而是两处调用的是同一个
        按创建时间排序的查询，结构上没有第二个排序键可以漂移。

        每一行单独过一次内容安全校验：记忆正文是用户自由文本，无法保证它不撞上协议
        泄漏词表。单条撞线时换成不回显内容的占位行——不能让**一条**记忆把整个列表
        崩掉，那会让用户永久看不到其余记忆，除非先盲猜是哪一条再删掉。
        """
        catalog = self._texts.catalog
        if not entries:
            return catalog.text("memory.list_empty")
        lines: list[str] = []
        for serial, entry in enumerate(entries, start=1):
            type_label = catalog.text(f"memory.type_label.{entry.memory_type}").text
            try:
                rendered_entry = catalog.text(
                    "memory.list_entry",
                    serial=serial,
                    type_label=type_label,
                    memory_key=entry.memory_key,
                    memory_value=entry.memory_value,
                )
            except ContentSafetyError:
                rendered_entry = catalog.text(
                    "memory.list_entry_unsafe",
                    serial=serial,
                    type_label=type_label,
                )
            lines.append(rendered_entry.text)
        return catalog.text("memory.list", entries="\n".join(lines))

    def _resolve_forget_target_id(
        self, memory_command: MemoryCommand, entries: list[UserMemoryEntry]
    ) -> str | None:
        """把 ``/memory forget`` 的入参（短序号或原始 id）解析成要删的那一条。

        序号按**删除当刻**的这份列表解析，不去猜用户上一次看到的是哪一份快照：
        列表与删除之间集合真的变过时，回执会回显被删条目的实际内容，让用户自行
        核对。这是覆盖该边缘情形取的口径，不是缺陷。

        传入完整 id 时原样透传，不在这里做存在性／归属预检查——那仍然只由删除语句
        自己的按人过滤结构性把关，保持这条路径与短序号能力加入之前完全一致。
        """
        if memory_command.memory_serial is not None:
            index = memory_command.memory_serial - 1
            if 0 <= index < len(entries):
                return entries[index].memory_id
            return None
        return memory_command.memory_id

    def _render_forget_receipt(self, entry: UserMemoryEntry | None) -> RenderedContent:
        """``/memory forget`` 的回执：命中时回显被删条目的实际内容。

        回显是短序号口径下用户唯一能自校验"删对了没有"的手段。未命中的三种情形
        （不存在／不属于本人／序号越界）刻意不区分，沿用同一条拒绝文案。

        被删内容本身撞上安全校验时退化成不回显内容的占位回执——「删除成功」这个
        正向结果不能反过来让内容安全校验失效。
        """
        catalog = self._texts.catalog
        if entry is None:
            return catalog.text("memory.forget_not_found")
        type_label = catalog.text(f"memory.type_label.{entry.memory_type}").text
        try:
            return catalog.text(
                "memory.forgotten",
                type_label=type_label,
                memory_key=entry.memory_key,
                memory_value=entry.memory_value,
            )
        except ContentSafetyError:
            return catalog.text("memory.forgotten_unsafe", type_label=type_label)
