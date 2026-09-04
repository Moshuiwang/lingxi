"""``/memory`` 命令面：查询、登记、删除、清空当前用户的记忆。

调用点在事件管线的忙碌判定之前（与 ``/stop`` 同一姿态）：记忆是元数据操作，不必
等当前任务跑完。三个写操作各记一条审计；只读与用法提示不记。
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
        """``/memory`` 命令面：查/登记/删除/清空当前用户的记忆（Issue #357 S-H3-3，
        D1 显式登记范围）。调用点放在忙碌判定之前，与 ``/stop`` 同一姿态——见
        「第 6 步的延伸」调用处注释。三个写操作（remember/forget/clear）各自记一条
        审计（``command.memory_remember``/``command.memory_forget``/
        ``command.memory_clear``），与既有 ``command.new``/``command.stop`` 同一
        姿态；``list`` 与格式不对的用法提示是只读/无副作用操作，不单独记审计。

        ``remember`` 在真正写库之前先过一遍 ``config.content.validate_user_visible_
        text`` 安全校验（Trace #373 H3 批 codex 外审②修复③）：与 worker 注入路径
        （``adapters/postgres_user_memory.PostgresUserMemoryReader._is_entry_safe``）
        和 ``/memory list`` 展示路径（``_render_memory_list``）复用同一道检查器，
        撞线（协议词、看起来像系统指令的多行文本）时**直接拒绝登记**并回执
        ``memory.remember_unsafe`` 说明原因，不写库、不记 ``command.memory_remember``
        审计——此前登记侧没有这道校验，用户会先收到「已登记，下一次提问开始生效」的
        回执，实际这条记忆在每次注入时都被注入侧静默跳过、永远不生效，回执与事实
        不符。
        """

        if memory_command.kind is MemoryCommandKind.LIST:
            entries = tx.list_user_memory(user_id=user.user_id)
            deferred.append(self._render_memory_list(entries))
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
            return Outcome(handled_as=HandledAs.COMMAND)

        if memory_command.kind is MemoryCommandKind.CLEAR:
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
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
            return Outcome(handled_as=HandledAs.COMMAND)

        if memory_command.kind is MemoryCommandKind.FORGET:
            # 短序号解析需要先查一次当前用户的记忆列表（rc22 B-8-1，#439
            # TOP-10）：与随后的 tx.forget_user_memory 同一个数据库事务、同一个
            # 连接，中途不会有别的写者插队——见 _resolve_forget_target_id 文档
            # 「forget 时刻的同一确定性排序」这条口径的成立依据。
            entries = tx.list_user_memory(user_id=user.user_id)
            target_id = self._resolve_forget_target_id(memory_command, entries)
            forgotten_entry = (
                tx.forget_user_memory(user_id=user.user_id, memory_id=target_id)
                if target_id is not None
                else None
            )
            deferred.append(self._render_forget_receipt(forgotten_entry))
            if forgotten_entry is not None:
                # 未命中（不存在/不属于本人/序号越界）不审计为一次「删除」事件——
                # 结构上没有发生任何写操作，与 forget_user_memory 的跨用户零生效
                # 同一条纪律（不产生「有人尝试删了别人一条记忆」这样的误导性事实）。
                self._audit.record(
                    "command.memory_forget",
                    event_id=message.event_id,
                    user_id=user.user_id,
                    conversation_id=conversation.conversation_id,
                    memory_id=forgotten_entry.memory_id,
                    trace_id=message.trace_id,
                )
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
            return Outcome(handled_as=HandledAs.COMMAND)

        if memory_command.kind is MemoryCommandKind.REMEMBER:
            # 登记前先过一遍与注入侧同一道安全校验（Trace #373 H3 批 codex 外审②
            # 修复③）：此前只有 worker 注入路径（``adapters/postgres_user_memory.
            # PostgresUserMemoryReader._is_entry_safe``）与 ``/memory list`` 展示
            # 路径复用了 ``validate_user_visible_text``，登记路径本身没有——用户
            # 登记一条撞线内容（协议词、看起来像系统指令的多行文本）会先收到
            # ``memory.remembered``「已登记，下一次提问开始生效」的回执，实际
            # 这条记忆在每一次注入时都会被静默跳过、永远不生效，回执与事实不符。
            # 这里在真正写库之前拒绝，回执明确告诉用户「没有登记成功、为什么」，
            # 不产生「说了成功但其实没生效」的落差。检查内容同注入侧：
            # ``f"{memory_key}\n{memory_value}"`` 一起校验（同一次撞线判定，不
            # 分别校验两个字段——理由同 ``_is_entry_safe`` 的调用形状）。
            try:
                validate_user_visible_text(
                    f"{memory_command.memory_key}\n{memory_command.memory_value}"
                )
            except ContentSafetyError:
                deferred.append(self._texts.catalog.text("memory.remember_unsafe"))
                tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
                return Outcome(handled_as=HandledAs.COMMAND)

            memory_id = tx.remember_user_memory(
                user_id=user.user_id,
                memory_type=memory_command.memory_type,
                memory_key=memory_command.memory_key,
                memory_value=memory_command.memory_value,
            )
            if memory_id is None:
                deferred.append(self._texts.catalog.text("memory.limit_exceeded"))
            else:
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
            tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
            return Outcome(handled_as=HandledAs.COMMAND)

        # NONE：以 /memory 开头但子命令形状不对——用法提示，不算错误也不审计
        # （与合法但空操作的 list 同一姿态：读多写少，不是需要留痕的业务决定）。
        deferred.append(self._texts.catalog.text("memory.usage_help"))
        tx.mark_handled_as(event_id=message.event_id, handled_as=HandledAs.COMMAND)
        return Outcome(handled_as=HandledAs.COMMAND)

    def _render_memory_list(self, entries: list[UserMemoryEntry]) -> RenderedContent:
        """把 ``/memory list`` 查到的记忆渲染成用户可见文本。

        **短序号取代裸 id 展示**（rc22 B-8-1，#439 TOP-10）：此前每一行末尾都
        带一个 ``mem_`` 前缀的裸 ULID（``id: mem_01ARZ...``）供 ``/memory
        forget`` 引用——这是全仓库唯一波及普通用户的裸 ULID 展示面，对不熟悉
        内部标识格式的用户不友好。这里改成按 ``entries`` 顺序从 1 开始编号的
        短序号，``/memory forget <序号>`` 同样可以引用（原始 ``mem_`` id 仍然
        兼容，见 ``commands.parse_memory_command`` 文档）。序号顺序与
        ``forget`` 时刻 ``tx.list_user_memory`` 返回的顺序能对上，**不是靠两处
        约定一致**，而是两处调用的是同一个 ``ORDER BY created_at ASC`` 查询
        （``adapters/postgres_conversation/_transaction.py`` 的
        ``list_user_memory``），结构上没有第二个排序键可以漂移。

        每一行单独过一次内容安全校验（``content.toml`` 的 ``_validate_user_visible_
        text``）：``memory_key``/``memory_value`` 是用户自己写入的自由文本，结构上
        无法保证它不会撞上协议泄漏词表（``mcp__``/``trace_id`` 等，见该文件文档）。
        单条撞线时替换成不回显内容的安全占位行，不让**一条**记忆的内容让整个
        ``/memory list`` 崩掉——那会让用户永久看不到自己登记过的其余记忆，除非先
        盲猜是哪一条、用 ``/memory forget`` 删掉。
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
        """把 ``/memory forget`` 的入参（短序号或原始 ``mem_`` id）解析成要传给
        ``tx.forget_user_memory`` 的具体 id（rc22 B-8-1，#439 TOP-10）。

        序号解析用的是**本次调用内、真正执行删除之前**这一次 ``list_user_
        memory`` 的查询结果——它与随后的 ``tx.forget_user_memory`` 共享同一个
        数据库事务、同一个连接，中途不会有别的写者插队。``/memory list`` 展示
        的序号和这里解析用的序号，只要两次查询之间用户自己的记忆集合没有变化，
        指向的就是同一条记录；如果确实变化了（这次 forget 之前，用户又
        remember/forget 了别的条目），这里按**当刻重新排序**解析，不去猜测
        用户上一次看到的是哪一份快照——调用方（``_handle_memory_command``）在
        回执里回显被删条目的实际内容（``_render_forget_receipt``），让用户能
        自行核对删的是不是自己想删的那一条，这是覆盖这个「列表与删除之间
        记忆集合变化」边缘情形取的口径，不是缺陷。

        传入完整 ``mem_`` id 时原样透传，不在这里做存在性/归属预检查——那仍然
        只由 ``forget_user_memory`` 自己的 ``WHERE ... AND user_id`` 结构性把关
        （跨用户传入他人 id 时哪怕在 ``entries`` 里也找不到，因为 ``entries``
        本身就已经是「按 user_id 过滤」的结果），保持这条路径与新增短序号解析
        之前完全一致的行为，不因为新增能力改变旧路径的判定顺序。
        """

        if memory_command.memory_serial is not None:
            index = memory_command.memory_serial - 1
            if 0 <= index < len(entries):
                return entries[index].memory_id
            return None
        return memory_command.memory_id

    def _render_forget_receipt(self, entry: UserMemoryEntry | None) -> RenderedContent:
        """``/memory forget`` 的回执（rc22 B-8-1，#439 TOP-10）：命中时回显被删
        条目的实际内容，供用户自行核对删的是不是那一条——短序号解析存在「列表
        与删除之间记忆集合变化」的边缘情形（见 ``_resolve_forget_target_id``
        文档），回显内容是这个口径下用户唯一能自校验的手段。未命中（不存在/
        不属于本人/序号越界，三者不区分，理由同既有 ``memory.forget_not_
        found`` 文案）沿用既有拒绝文案。

        被删内容本身撞上安全校验时（同 ``_render_memory_list`` 的道理：
        ``memory_key``/``memory_value`` 是用户自由文本，无法结构性保证不撞协议
        泄漏词表）退化成不回显内容的安全占位回执——「删除成功」这个正向结果
        不能反过来让内容安全校验失效。
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
