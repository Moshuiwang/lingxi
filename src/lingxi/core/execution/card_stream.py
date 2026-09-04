"""问数卡片的顺序、限流与失败回退。

不依赖飞书 SDK：卡片适配器只实现 ``CardTransport``，L2 用内存假实现验证序号、
话题隔离和失败回退，L4a 再验证 CardKit 的真实字段与投递效果。

**支持从持久化状态恢复（resume）**：崩溃重启后不重新建卡，``initial_*`` 参数
把上一次持久化的 card_id/sequence/message_id/fallback_needed 传回来接着走，
不产生第二张有效卡片（`V-卡片-01`）。正文是"已走过的步骤名"追加式列表，同一
状态连续出现只原地刷新用时；``initial_progress_history`` 让这份累积状态同样
能跨轮询 resume。同一步骤身份持续过久会被明示为"停滞"。

**「明确失败」与「结果不明」判别用白名单**：三处外发调用只认 ``DeliveryRejected``
为"明确失败"，其余异常一律归"结果不明"、原样 ``raise``、不降级不重试。终态
**关闭**失败不受此规则约束：更新已成功时仅关闭失败不构成结果丢失。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from lingxi.config.content import (
    ContentCatalog,
    RenderedCard,
    RenderedContent,
    default_content_catalog,
)

# 语义化进度动作码：``apps/worker/service.py`` 编码写入
# ``task_delivery_event.content``，``apps/gateway/delivery.py`` 的消费循环
# 解码后交给 ``CardStream.update()``。两侧共享同一份常量，不各写一份字符串
# 字面量、悄悄漂移。
PROGRESS_ACTION_PROCESSING = "processing"
PROGRESS_ACTION_QUERYING = "querying"
PROGRESS_ACTION_COMPOSING = "composing"
# 非问数查询工具的调用（含被拒绝的越界包装调用）：独立文案，不再与"模型正在
# 输出文本"共用同一句话，避免两者交替出现时卡片长时间显示同一句文案的"读秒
# 卡住感"。
PROGRESS_ACTION_WORKING = "working"
PROGRESS_ACTION_COMPLETED = "completed"

# 问数查询工具的已知子步骤：真实问数 MCP 注册的原生只读工具全集。**白名单式
# 映射**：只有落在这个集合里的子步骤名才会被编码进 progress 事件；不在其中
# 的字符串在 `encode_progress_action`/`decode_progress_action` 两处都静默
# 丢弃、退回通用文案——两处都过滤是因为后者的输入来自数据库已落库的字段，
# 不能假设它一定是本侧刚编码出来的那一份。
QUERY_STEP_LIST_METRICS = "list_metrics"
QUERY_STEP_DESCRIBE_METRIC = "describe_metric"
QUERY_STEP_SEARCH_DIMENSION = "search_dimension"
QUERY_STEP_QUERY_METRIC = "query_metric"
KNOWN_QUERY_STEPS: frozenset[str] = frozenset(
    {
        QUERY_STEP_LIST_METRICS,
        QUERY_STEP_DESCRIBE_METRIC,
        QUERY_STEP_SEARCH_DIMENSION,
        QUERY_STEP_QUERY_METRIC,
    }
)

# 工具名 → 用户语文案键的白名单式映射表。只覆盖 :data:`KNOWN_QUERY_STEPS`
# 里的四个已知子步骤，具体文案由 ``content.toml`` 维护。查不到的键
# （``dict.get`` 默认分支）一律落回通用文案：未来协议新增工具但这里忘了
# 同步登记，效果只是退化成通用文案，不会把新工具的原始名字泄漏出去。
_QUERY_STEP_ACTION_TEXT_KEYS: dict[str, str] = {
    QUERY_STEP_LIST_METRICS: "worker.action.querying_list_metrics",
    QUERY_STEP_DESCRIBE_METRIC: "worker.action.querying_describe_metric",
    QUERY_STEP_SEARCH_DIMENSION: "worker.action.querying_search_dimension",
    QUERY_STEP_QUERY_METRIC: "worker.action.querying_query_metric",
}

# 历史行的完成时措辞：与上面 `_QUERY_STEP_ACTION_TEXT_KEYS` 一一对应的
# "已..."变体，供 `_render_step_line(completed=True)` 选用——累积列表里除
# 最后一行外，全部行代表"已经翻篇的步骤"，不该继续用"正在..."现在时措辞。
_QUERY_STEP_ACTION_TEXT_KEYS_DONE: dict[str, str] = {
    QUERY_STEP_LIST_METRICS: "worker.action.querying_list_metrics_done",
    QUERY_STEP_DESCRIBE_METRIC: "worker.action.querying_describe_metric_done",
    QUERY_STEP_SEARCH_DIMENSION: "worker.action.querying_search_dimension_done",
    QUERY_STEP_QUERY_METRIC: "worker.action.querying_query_metric_done",
}

# 非查询类动作的完成时措辞（同上一条注释）。
_ACTION_DONE_TEXT_KEYS: dict[str, str] = {
    PROGRESS_ACTION_PROCESSING: "worker.action.processing_done",
    PROGRESS_ACTION_COMPOSING: "worker.action.composing_done",
    PROGRESS_ACTION_WORKING: "worker.action.working_done",
}
_DEFAULT_QUERYING_DONE_TEXT_KEY = "worker.action.querying_metrics_done"

# 静态防回归：编码出的 `PROGRESS_ACTION_QUERYING` 形态最终落进
# ``task_delivery_event.content``，迁移 0075 给这一列加了 32 字节的数据库层
# CHECK 约束，``core/delivery/ports.PROGRESS_CONTENT_MAX_LENGTH`` 是应用层
# 落地。未来加更长的子步骤名却忘了核对这条契约会导致真实环境写库失败；这里
# 把最坏编码长度钉成一条 import 期静态断言，回归时直接炸在 import。
_MAX_KNOWN_QUERY_STEP_NAME_LENGTH = max(len(step) for step in KNOWN_QUERY_STEPS)
# "querying:"（9 字节）+ 两位数计数上界 "99"（2 字节）+ ":"（1 字节）+ 最长
# 子步骤名——与 `core/delivery/ports.py::PROGRESS_CONTENT_MAX_LENGTH` 注释里
# "两位数计数"这条假设一致，不是本模块凭空另设的预算。
_WORST_CASE_QUERYING_CONTENT_LENGTH = (
    len(f"{PROGRESS_ACTION_QUERYING}:") + len("99") + len(":") + _MAX_KNOWN_QUERY_STEP_NAME_LENGTH
)
assert _WORST_CASE_QUERYING_CONTENT_LENGTH <= 32, (
    "KNOWN_QUERY_STEPS 里最长子步骤名让 encode_progress_action 的最坏输出超过了"
    "迁移 0075 CHECK 与 core/delivery/ports.PROGRESS_CONTENT_MAX_LENGTH 约定的"
    "32 字节契约——新增子步骤名前必须先核对这条预算，不能只改这里不改那两处"
)


def encode_progress_action(
    action: str, *, query_count: int | None = None, query_step: str | None = None
) -> str:
    """把语义化进度状态编码进一条 progress 事件的 ``content`` 字段（worker 侧）。

    认识三种动作：``PROGRESS_ACTION_QUERYING``（必须带 ``>=1`` 的
    ``query_count``；可选 ``query_step``，只有落在 :data:`KNOWN_QUERY_STEPS`
    白名单里才会被编码，否则退回不带步骤名的通用编码）、``PROGRESS_ACTION_
    COMPOSING`` 与 ``PROGRESS_ACTION_WORKING``。``PROGRESS_ACTION_PROCESSING``
    （默认状态）不经过这个函数：调用方直接传 ``content=None``。**不回显工具名、
    参数或任何查询内容**：编码后的字符串只可能是白名单内的固定形状，永远不含
    调用方传入的原始工具名或输入正文。
    """

    if action == PROGRESS_ACTION_QUERYING:
        if query_count is None or query_count < 1:
            raise ValueError("querying 动作必须带一个 >=1 的 query_count")
        if isinstance(query_step, str) and query_step in KNOWN_QUERY_STEPS:
            return f"{PROGRESS_ACTION_QUERYING}:{query_count}:{query_step}"
        return f"{PROGRESS_ACTION_QUERYING}:{query_count}"
    if action == PROGRESS_ACTION_COMPOSING:
        return PROGRESS_ACTION_COMPOSING
    if action == PROGRESS_ACTION_WORKING:
        return PROGRESS_ACTION_WORKING
    raise ValueError(f"未知的进度动作：{action!r}")


def decode_progress_action(content: str | None) -> tuple[str, int | None, str | None]:
    """反解析（Gateway 消费侧）。返回 ``(action, query_count, query_step)``。

    任何无法识别的形状——``None``（进度事件从未携带语义、或来自旧版 worker）、
    格式不对的字符串、越界的计数、不在 :data:`KNOWN_QUERY_STEPS` 白名单里的
    步骤名——一律退回安全默认值（整体退回 ``(PROGRESS_ACTION_PROCESSING, None,
    None)``，或仅步骤名退回 ``None``）：这只是一张状态卡片要选哪句文案，宁可
    显示默认文案，也不能让一条脏数据（含蓄意构造、注入内部标识的 `content`）
    炸掉整条投递消费循环，或把不可信内容带进用户可见卡片。
    """

    if content == PROGRESS_ACTION_COMPOSING:
        return PROGRESS_ACTION_COMPOSING, None, None
    if content == PROGRESS_ACTION_WORKING:
        return PROGRESS_ACTION_WORKING, None, None
    if isinstance(content, str) and content.startswith(f"{PROGRESS_ACTION_QUERYING}:"):
        _, _, remainder = content.partition(":")
        raw_count, _, raw_step = remainder.partition(":")
        if raw_count.isdigit():
            count = int(raw_count)
            if count >= 1:
                step = raw_step if raw_step in KNOWN_QUERY_STEPS else None
                return PROGRESS_ACTION_QUERYING, count, step
    return PROGRESS_ACTION_PROCESSING, None, None


@dataclass(frozen=True)
class CardCreated:
    """建卡并把它作为消息发出后的结果。

    ``card_id`` 用于后续的流式更新/关闭；``message_id`` 是发送消息接口返回的可回读
    标识（G-CARD 实测：卡片与文本共用同一发送接口与响应结构），在卡片整个生命周期内
    只在这里产生一次，后续流式更新不产生新的 message_id。
    """

    card_id: str
    message_id: str


class DeliveryRejected(Exception):
    """服务端已经给出完整响应、并以明确的业务错误码拒绝这次外发——不是「结果不明」。

    本模块与 ``apps.gateway.delivery`` 唯一当作"明确失败"处理的异常类型：
    真实 adapter 只在 ``response.success()`` 为假、且能读出 ``code``/``msg``
    字段时才抛出它。其余任何异常都不属于这个类型，落进三处外发调用的默认
    分支（"结果不明"，原样 ``raise``）。定义在这里而不是 adapter 侧：adapter
    已经依赖本模块的 ``CardTransport``/``TextTransport`` Protocol，异常类型
    反过来定义在 adapter 侧会形成循环 import。
    """

    def __init__(
        self, message: str = "", *, code: int | str | None = None, log_id: str | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.log_id = log_id
        super().__init__(message or f"服务端明确拒绝：code={code} log_id={log_id}")


class CardTransport(Protocol):
    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedCard,
    ) -> CardCreated: ...

    def update(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...

    def close(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...


class TextTransport(Protocol):
    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> str: ...


SendOutcomeCallback = Callable[[str, bool], None]


# G-CARD 已知平台行为：未手动关闭的流式卡片**距上次开启**（不是距上次更新）
# 10 分钟后由平台自动关闭。保活刷新不成立（计时锚点是"上次开启"）；选定方案
# 是接近阈值时主动换成"即将改用文字消息"提示并立即降级，复用现成的降级路径。
# 阈值取 9 分钟（留 1 分钟余量）：兜底计时最长 12 秒，跨过阈值后最多 12 秒内
# 会触发这条提示，届时距真实自动关闭仍有≥48 秒余量。
CARD_AUTO_CLOSE_HANDOFF_SECONDS = 540.0


class CardRateLimiter:
    """一个 worker 进程共享：单话题 500ms、全进程 50 次/秒。"""

    def __init__(self) -> None:
        self._last_by_topic: dict[str, float] = {}
        self._global_updates: deque[float] = deque()

    def allow(self, *, topic: str, now: float) -> bool:
        last = self._last_by_topic.get(topic)
        if last is not None and now - last < 0.5:
            return False
        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        if len(self._global_updates) >= 50:
            return False
        self._last_by_topic[topic] = now
        self._global_updates.append(now)
        return True

    def record(self, *, topic: str, now: float) -> None:
        """无条件记一次已经发生的调用，不做节流判断、不可能返回拒绝。

        终态更新+关闭（``CardStream.finish()``）不经过 ``allow()`` 的单话题
        500ms 节流——那两帧是结果本身，被节流吞掉等于结果丢失，比不节流更糟。
        但全进程 50 次/秒的预算是 CardKit 的硬限制，终态这两次调用同样要计入，
        否则并发多话题同时终态时全局计数会失真。只推进全局窗口，不改动单话题
        的 500ms 记录——终态调用不受单话题节流约束，也不应该反过来影响它。
        """

        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        self._global_updates.append(now)


@dataclass(frozen=True)
class CardStreamResult:
    card_id: str | None
    sequence: int
    fallback_text: bool


@dataclass(frozen=True)
class _StepRecord:
    """累积列表里的一条原始信号，不是已经渲染好的字符串。

    保留原始信号交给 :meth:`CardStream._accumulated_status_card` 在**每次
    渲染时**决定"这一行是不是列表里的最后一行"，只有最后一行才用"正在..."
    的现在时措辞，其余全部改用"已..."的完成时措辞——避免历史行永远停在它
    被追加那一刻的现在时措辞（见 :func:`CardStream._render_step_line` 的
    ``completed`` 参数）。
    """

    action: str
    elapsed_seconds: int
    query_count: int | None = None
    query_step: str | None = None


@dataclass(frozen=True)
class ProgressStepSnapshot:
    """一条已经持久化的进度信号，按时间顺序重放用来重建卡片正文的累积状态。

    字段与 ``decode_progress_action`` 的返回值加一个 ``elapsed_seconds``
    对齐——``apps/gateway/delivery.py`` 从 outbox 里读出历史事件解码成这个
    形状，见 ``CardStream.__init__`` 的 ``initial_progress_history`` 参数。
    """

    elapsed_seconds: int
    action: str
    query_count: int | None = None
    query_step: str | None = None


# 卡片正文累积列表的行数上限（防御性上限，不是产品承诺）。真实任务正常情况
# 下远远不会触顶，这里只是防止极端场景让卡片正文无界增长。触顶后丢弃最旧的
# 行，保留最近发生的步骤——用户此刻最关心的是"最近发生了什么"。
MAX_ACCUMULATED_STEP_LINES = 40

# 停滞判定阈值：同一枚身份持续跨过这么多秒仍未换新（`_accumulate_step` 只
# 原地刷新，不追加新行）就判定"停滞"。**取值恒等于 worker 侧兜底强制刷新
# 间隔（``_PROGRESS_FALLBACK_SECONDS``）的 2 倍**——若两者相等，恰好一个兜底
# 周期的正常静默会被误判成停滞；两处常量各自独立登记、互不 import。
STALL_THRESHOLD_SECONDS = 24


class CardStream:
    """一个任务一个实例；绝不跨话题共享卡片序号或限流状态。"""

    def __init__(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        transport: CardTransport,
        fallback: TextTransport,
        catalog: ContentCatalog | None = None,
        mark_external_side_effect: Callable[[], bool | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        rate_limiter: CardRateLimiter | None = None,
        on_send_outcome: SendOutcomeCallback | None = None,
        initial_card_id: str | None = None,
        initial_sequence: int = 0,
        initial_message_id: str | None = None,
        initial_fallback_needed: bool = False,
        initial_progress_history: Sequence[ProgressStepSnapshot] = (),
    ) -> None:
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._reply_to_message_id = reply_to_message_id
        self._transport = transport
        self._fallback = fallback
        self._catalog = catalog or default_content_catalog()
        self._mark_external_side_effect = mark_external_side_effect
        self._monotonic = monotonic
        # resume 场景：非 None/非零表示接着一次此前已经持久化的进度继续，而
        # 不是从零建卡，重启不产生第二张有效卡片。
        self._card_id: str | None = initial_card_id
        self._sequence = initial_sequence
        self._message_id: str | None = initial_message_id
        self._fallback_needed = initial_fallback_needed
        self._last_update: float | None = None
        # 过程流式的累积正文：Gateway 每批次都重新构造全新实例，``__init__``
        # 用 ``initial_progress_history`` 重放找回上一轮的累积状态，不出现
        # "正文突然变短"的回退观感。``_last_step_identity`` 记录最近一次信号，
        # 同一种信号只原地刷新；``_current_step_started_at`` 记录当前身份第一
        # 次出现时的用时，供停滞判定使用，resume 重放会正确重建这个锚点。
        self._step_records: list[_StepRecord] = []
        self._last_step_identity: tuple[str, int | None, str | None] | None = None
        self._current_step_started_at: int = 0
        for snapshot in initial_progress_history:
            self._accumulate_step(
                action=snapshot.action,
                elapsed_seconds=snapshot.elapsed_seconds,
                query_count=snapshot.query_count,
                query_step=snapshot.query_step,
            )
        self._rate_limiter = rate_limiter or CardRateLimiter()
        self._on_send_outcome = on_send_outcome

    @property
    def fallback_needed(self) -> bool:
        return self._fallback_needed

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def card_id(self) -> str | None:
        return self._card_id

    @property
    def message_id(self) -> str | None:
        """当前投递通道（卡片或文本兜底）绑定的可回读标识；尚未取得时为 ``None``。"""

        return self._message_id

    def start(self) -> None:
        """建卡并发出。resume 场景下（已经有 ``card_id`` 或已经降级）直接不做事——
        调用方按持久化状态决定要不要调用这一步，这里的判断只是防御性幂等。
        """

        if self._card_id is not None or self._fallback_needed:
            return
        # 初始占位文案（"正在处理 · 0 秒"）刻意**不**计入累积步骤列表：它这一
        # 刻还没有对应任何持久化的 progress 事件，历史重建只能从 outbox 里
        # 真实存在的信号重放——算进去会让重启后正文少一行。第一条累积行来自
        # 第一次真实信号触发的 `update()`。
        card = self._catalog.card(
            "query.status",
            status=self._render_step_line(action=PROGRESS_ACTION_PROCESSING, elapsed_seconds=0),
        )
        try:
            self._before_external()
            created = self._transport.create(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                card=card,
            )
            self._card_id = created.card_id
            self._message_id = created.message_id
            self._notify_send("card_non_final", True)
            self._last_update = self._monotonic()
            # 创建本身就是该话题的首帧，后续更新也要遵守 500ms 间隔。
            self._rate_limiter.allow(topic=self._topic, now=self._last_update)
        except DeliveryRejected:
            # 明确失败：卡片路径统一走同话题文本回退。
            self._notify_send("card_non_final", False)
            self._fallback_needed = True
        except Exception:  # 结果不明：建卡是否已经在服务端生效不可知，
            # 不能假装明确失败——原样抛给调用方，不降级、不清预留位、不重试。
            raise

    def update(
        self,
        *,
        elapsed_seconds: int,
        action: str = PROGRESS_ACTION_PROCESSING,
        query_count: int | None = None,
        query_step: str | None = None,
    ) -> None:
        if self._card_id is None or self._fallback_needed:
            return
        if elapsed_seconds >= CARD_AUTO_CLOSE_HANDOFF_SECONDS:
            # G-CARD 10 分钟自动关闭提前收口（见 ``CARD_AUTO_CLOSE_HANDOFF_
            # SECONDS`` 上方取舍说明）：改写成"即将改用文字消息"提示并立即
            # 降级，之后每次调用都走既有文本兜底路径。**只会触发一次**：
            # `_emit_handoff_notice()` 的 `finally` 无条件置位
            # `_fallback_needed`，本方法顶部的早退分支从此拦住后续每一次调用。
            self._emit_handoff_notice()
            return
        now = self._monotonic()
        if not self._rate_limiter.allow(topic=self._topic, now=now):
            return
        self._accumulate_step(
            action=action,
            elapsed_seconds=max(0, elapsed_seconds),
            query_count=query_count,
            query_step=query_step,
        )
        card = self._accumulated_status_card()
        self._sequence += 1
        try:
            self._before_external()
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_non_final", True)
            self._last_update = now
        except Exception:
            self._notify_send("card_non_final", False)
            self._fallback_needed = True

    def _accumulate_step(
        self,
        *,
        action: str,
        elapsed_seconds: int,
        query_count: int | None = None,
        query_step: str | None = None,
    ) -> None:
        """过程流式：把这一次信号记进"已走过的步骤"列表。

        识别"新步骤"只看语义身份 ``(action, query_count, query_step)`` 是否
        与上一次不同——``query_count`` 递增也算新步骤。身份不变时只原地刷新，
        不重复追加，否则长任务会反复刷屏同一行；只有 ``PROGRESS_ACTION_
        QUERYING`` 才把 ``query_count``/``query_step`` 计入身份。同时算出
        这枚身份已经持续了多久，跨过 :data:`STALL_THRESHOLD_SECONDS` 才交给
        ``_render_step_line`` 切换成停滞明示文案；换成新身份时清零锚点。
        """

        if action == PROGRESS_ACTION_QUERYING:
            identity = (action, query_count, query_step)
        else:
            identity = (action, None, None)
        is_repeat = bool(self._step_records) and self._last_step_identity == identity
        record = _StepRecord(
            action=action,
            elapsed_seconds=elapsed_seconds,
            query_count=query_count,
            query_step=query_step,
        )
        if is_repeat:
            self._step_records[-1] = record
        else:
            self._current_step_started_at = elapsed_seconds
            self._step_records.append(record)
            if len(self._step_records) > MAX_ACCUMULATED_STEP_LINES:
                del self._step_records[0]
        self._last_step_identity = identity

    def _accumulated_status_card(self) -> RenderedCard:
        """把当前累积的步骤列表渲染成一张 ``query.status`` 卡片：正文是"已走过
        的步骤名"按时间顺序换行追加，飞书官方的流式打字机效果天然承担"新行
        出现"的动态观感，这里只负责让正文本身持续变长。

        **每次调用都重新渲染全部行**：只有列表里**最后一行**（当前正在发生
        的步骤）用"正在..."的现在时措辞，其余历史行一律改用"已..."的完成时
        措辞，避免一条历史行永远停在它被追加那一刻的现在时措辞。停滞判定只对
        最后一行计算——历史行代表"已经翻篇的步骤"，不存在"停滞"这个概念。
        """

        last_index = len(self._step_records) - 1
        lines: list[str] = []
        for index, record in enumerate(self._step_records):
            if index == last_index:
                duration = max(0, record.elapsed_seconds - self._current_step_started_at)
                stalled_seconds = duration if duration >= STALL_THRESHOLD_SECONDS else None
                lines.append(
                    self._render_step_line(
                        action=record.action,
                        elapsed_seconds=record.elapsed_seconds,
                        query_count=record.query_count,
                        query_step=record.query_step,
                        stalled_seconds=stalled_seconds,
                        completed=False,
                    )
                )
            else:
                lines.append(
                    self._render_step_line(
                        action=record.action,
                        elapsed_seconds=record.elapsed_seconds,
                        query_count=record.query_count,
                        query_step=record.query_step,
                        completed=True,
                    )
                )
        return self._catalog.card("query.status", status="\n".join(lines))

    def _emit_handoff_notice(self) -> None:
        """接近 G-CARD 10 分钟自动关闭阈值时的最后一帧卡片更新。

        刻意不经过 ``CardRateLimiter.allow()`` 的单话题 500ms 节流——这是一次
        性的关键提示（同一实例生命周期最多触发一次），被节流吞掉就等于用户
        永远看不到这句解释；与 ``finish()`` 的终态两帧同一姿态，改用
        ``record()`` 无条件记入全进程 50 次/秒预算。发送本身失败不改变"接下来
        改走文本通道"这个既定动作——``finally`` 保证 ``_fallback_needed`` 一定
        被置位；即使这一帧没有送达，后续文本终态仍会正常送达最终答案。
        """

        now = self._monotonic()
        card = self._status_card_handoff_notice()
        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=now)
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_non_final", True)
            self._last_update = now
        except Exception:  # 见方法文档：发送失败不改变既定降级动作
            self._notify_send("card_non_final", False)
        finally:
            self._fallback_needed = True

    def _build_finish_card(
        self, *, result: str | None, failure: RenderedContent | None, elapsed_seconds: int
    ) -> RenderedCard:
        """按结果/失败构造终态卡片正文。

        ``failure`` 非空时统一按"可能含模型正文"处理（区分不了我们自己的固定
        文案与 STOPPED 终态携带的模型残余正文，两者都已在 worker 侧出口净化
        过）。``result`` 非空时目录校验只保留协议泄漏这一道，不用固定模板的
        自然语言词表拦截模型日常措辞；没有结果时用 ``query.empty`` 卡自己的
        模板一次性说成一句连贯的话，不先拼"已完成"再拼"没有结果"制造矛盾感。
        """

        if failure is not None:
            return self._catalog.card(
                "query.failure", message=failure.text, contains_model_text=True
            )
        if result:
            completed = self._catalog.text(
                "worker.status",
                action=self._catalog.text("worker.action.completed").text,
                elapsed_seconds=max(0, elapsed_seconds),
            ).text
            body = f"{completed}\n{result}"
            return self._catalog.card("query.result", result=body, contains_model_text=True)
        return self._catalog.card("query.empty", elapsed_seconds=max(0, elapsed_seconds))

    def finish(
        self,
        *,
        result: str | None = None,
        failure: RenderedContent | None = None,
        elapsed_seconds: int = 0,
    ) -> None:
        """终态更新 + 关闭。**这两步的失败语义并不对称**：

        - 终态 **更新** 失败：完整答案还没有确定写进卡片，用户看不到结果——必须
          整体降级为文本兜底，否则结果丢失。
        - 更新已经成功之后仅 **关闭** 失败：答案已经在卡片里对用户可见，结果
          没有丢，只是收尾没做完，此时绝不能再触发文本兜底——那样会让同一条
          答案在同一话题里出现两遍，直接违反"不得同时形成卡片终态与重复文本
          终态"（`V-卡片-03`）。因此关闭失败**不**置位 ``_fallback_needed``。
        """

        if self._card_id is None or self._fallback_needed:
            return
        card = self._build_finish_card(
            result=result, failure=failure, elapsed_seconds=elapsed_seconds
        )

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except DeliveryRejected:
            # 明确失败：终态正文还没确定送达，只能整体降级。
            self._notify_send("card_final", False)
            self._fallback_needed = True
            return
        except Exception:  # 结果不明：终态正文是否已经写进卡片对进程
            # 不可知——绝不能当"明确失败"整体降级为文本兜底，那正是本次复现的跨
            # 通道重复投递。原样抛给调用方，不降级、不清预留位、不重试。
            raise

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.close(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except Exception:  # 见上面的方法说明：不降级，避免重复投递
            # 这条规则不延伸到这里：关闭失败——无论是 DeliveryRejected 还是
            # 任何其它异常——都不改变"更新已经成功、答案对用户可见"这个结论，
            # 因此不单独拆出 ``except DeliveryRejected``。
            self._notify_send("card_final", False)

    def send_fallback(self, content: RenderedContent) -> str | None:
        """发一次同话题文本兜底；不需要时（未降级）返回 ``None`` 且不产生外部调用。

        调用方负责在这次调用之前完成"外发前预留位"的持久化——文本发送没有飞书
        原生幂等键，这里的异常刻意不吞掉，交由调用方区分"同步捕获的明确失败"
        （``DeliveryRejected``，可以清预留位、下一轮重试）、"结果不明"（除
        ``DeliveryRejected`` 以外的一切异常：转 ``uncertain``，不清预留位、
        不重试）与"进程崩溃"（预留位留在数据库里，下一轮识别为 ``uncertain``
        而不是自动重发）。
        """

        if not self._fallback_needed:
            return None
        try:
            self._before_external()
            message_id = self._fallback.send_text(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                text=content.text,
            )
        except DeliveryRejected:
            # 明确失败：同步捕获，可以清预留位、下一轮重试。
            self._notify_send("message_final", False)
            raise
        except Exception:
            # 结果不明：文本是否已经送达对进程不可知，不计入"发送失败"的告警统计
            # （那会把可能已经成功的一次发送计成失败）；原样抛给调用方，不清预留位、
            # 不重试。
            raise
        self._message_id = message_id
        self._notify_send("message_final", True)
        return message_id

    def _select_step_action_text(
        self,
        *,
        action: str,
        query_count: int | None,
        query_step: str | None,
        completed: bool,
    ) -> str:
        """按语义化进度动作码 + 是否完成选一句文案键并渲染成文字。

        ``completed=True`` 时选用"已..."的完成时措辞，供
        :meth:`_accumulated_status_card` 渲染历史行；``False``（默认）保留
        "正在..."现在时措辞，只用于列表最后一行。``query_count`` 缺失时
        ``querying`` 退化为默认 ``processing`` 文案，不抛错也不渲染缺占位符
        的残句。``query_step`` 是白名单式映射：查不到的键（``dict.get`` 默认
        分支，第三层防御）一律落回通用文案 ``worker.action.querying_metrics``，
        不做任何字符串拼接或动态键名。
        """

        if action == PROGRESS_ACTION_COMPLETED:
            return self._catalog.text("worker.action.completed").text
        if action == PROGRESS_ACTION_QUERYING and query_count is not None:
            if completed:
                text_key = _QUERY_STEP_ACTION_TEXT_KEYS_DONE.get(
                    query_step, _DEFAULT_QUERYING_DONE_TEXT_KEY
                )
            else:
                text_key = _QUERY_STEP_ACTION_TEXT_KEYS.get(
                    query_step, "worker.action.querying_metrics"
                )
            return self._catalog.text(text_key, count=query_count).text
        if action == PROGRESS_ACTION_COMPOSING:
            # 完成时措辞查 _ACTION_DONE_TEXT_KEYS 表，与上面查询类分支的
            # _QUERY_STEP_ACTION_TEXT_KEYS_DONE 同一查表姿态，取值与
            # "worker.action.composing_done" 字面量逐字节相同。
            key = (
                _ACTION_DONE_TEXT_KEYS[PROGRESS_ACTION_COMPOSING]
                if completed
                else "worker.action.composing"
            )
            return self._catalog.text(key).text
        if action == PROGRESS_ACTION_WORKING:
            key = (
                _ACTION_DONE_TEXT_KEYS[PROGRESS_ACTION_WORKING]
                if completed
                else "worker.action.working"
            )
            return self._catalog.text(key).text
        key = (
            _ACTION_DONE_TEXT_KEYS[PROGRESS_ACTION_PROCESSING]
            if completed
            else "worker.action.processing"
        )
        return self._catalog.text(key).text

    def _render_step_line(
        self,
        *,
        action: str,
        elapsed_seconds: int,
        query_count: int | None = None,
        query_step: str | None = None,
        stalled_seconds: int | None = None,
        completed: bool = False,
    ) -> str:
        """按语义化进度动作码选文案，渲染成累积列表里的一行。

        ``stalled_seconds`` 与 ``completed=True`` 不会同时出现——停滞是"当前
        步骤"才有意义的判断，调用方只对最后一行计算 ``stalled_seconds``。
        非 ``None`` 时改用 ``worker.status_stalled`` 明示停滞的措辞而不是
        常规的 ``worker.status``；``action_text`` 本身不变——它已经带着"停滞
        发生在哪一步"这个位置信息，停滞措辞只是换了个更明确的后缀。
        """

        action_text = self._select_step_action_text(
            action=action, query_count=query_count, query_step=query_step, completed=completed
        )
        if stalled_seconds is not None:
            return self._catalog.text(
                "worker.status_stalled", action=action_text, stalled_seconds=stalled_seconds
            ).text
        return self._catalog.text(
            "worker.status", action=action_text, elapsed_seconds=elapsed_seconds
        ).text

    def _status_card_handoff_notice(self) -> RenderedCard:
        """G-CARD 10 分钟自动关闭提前收口的固定文案，复用 ``query.status``
        卡片形状——不是常规的"{action} · {elapsed_seconds} 秒"状态句，直接
        展示一句完整、不含占位符的说明。
        """

        return self._catalog.card(
            "query.status", status=self._catalog.text("worker.card_handoff_notice").text
        )

    @property
    def _topic(self) -> str:
        return f"{self._chat_id}\x00{self._thread_id or ''}"

    def _before_external(self) -> None:
        if self._mark_external_side_effect is not None:
            marked = self._mark_external_side_effect()
            if marked is False:
                raise RuntimeError("任务已不再由当前 worker 持有")

    def _notify_send(self, operation: str, succeeded: bool) -> None:
        """把发送结果交给告警层；告警层故障不能改变用户任务的出站语义。"""

        if self._on_send_outcome is None:
            return
        try:
            self._on_send_outcome(operation, succeeded)
        except Exception:
            # 告警输入失败不能反向把已成功的用户交付改成失败，也不能中断文本回退。
            return
