"""问数卡片的顺序、限流与失败回退。

这里不依赖飞书 SDK。卡片适配器只实现 ``CardTransport``，因此 L2 可以用内存假实现
验证序号、话题隔离和失败回退，L4a 再验证 CardKit 的真实字段与投递效果。

**Issue #152 起支持从持久化状态恢复（resume）**：Gateway 消费循环崩溃重启后不会重新
``start()`` 一张已经建好的卡片——它把上一次持久化的 ``card_id``/``sequence``/
``message_id``/``fallback_needed`` 作为 ``initial_*`` 参数传回来，本类从那一点继续，
不产生第二张有效卡片（`V-卡片-01`、状态合同第 7 条）。

**「明确失败」与「结果不明」不是同一件事，判别用白名单而不是黑名单**（独立审核
B-1 首次修复于 2026-08-14；独立审核 R-1 于同日把判别方向从黑名单反转为白名单，
红线：重复投递）。``start()``/``finish()`` 的终态更新/``send_fallback()`` 这三处
外发调用只认一种"明确失败"：``DeliveryRejected``（定义见下方）——真实 adapter
``adapters.feishu_delivery`` **只有**在服务端已经给出完整响应、且业务错误码明确
表示拒绝（``response.success()`` 为假、``code``/``msg`` 字段可读）时才抛出它。
除它之外的**一切**异常——``lark_oapi`` 内部 JSON 解析失败
（``json.JSONDecodeError``）、响应结构缺失（``response.success()`` 为真但拿不到
``card_id``/``message_id`` 这类可回读标识）、``requests`` 的网络类异常
（``requests.exceptions.RequestException`` 及其子类，全部继承自内置
``OSError``，读超时/连接重置时抛出）、以及任何其它未预期的异常——都不能确定
服务端是否已经处理这次调用，默认归"结果不明"：三处外发原样 ``raise``，不降级、
不清空 ``_fallback_needed``——调用方（``apps.gateway.delivery``）据此不清预留位、
不重试、不改道，转入既有 ``uncertain`` 告警路径，等待人工核对是否已经送达
（issue #152 状态合同第 6 条）。本模块因此不需要 import ``requests`` 或
``lark_oapi``，只需要认识 ``DeliveryRejected`` 这一个类型——这正是白名单相对
黑名单的好处：新出现的异常类型（SDK 升级引入的新异常、此前没想到的响应形状）
默认落进"结果不明"这个更安全的分支，而不是黑名单下默认落进"明确失败"这个更
危险的分支。终态**关闭**（``finish()`` 的第二次外部调用）不受这条规则约束——
它的失败语义本身就与异常类型无关（见 ``finish()`` 文档：更新已经成功后仅关闭
失败不构成结果丢失），任何异常落进既有 ``except Exception`` 分支效果都完全
一致，无需特殊处理。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from lingxi.config.content import ContentCatalog, RenderedCard, RenderedContent, default_content_catalog


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

    这是本模块与 ``apps.gateway.delivery`` 唯一当作"明确失败"处理的异常类型
    （白名单，独立审核 R-1，2026-08-14）：真实 adapter（``adapters.feishu_delivery``）
    只在 ``response.success()`` 为假、且能读出 ``code``/``msg`` 字段时才抛出它。
    除它之外的任何异常——JSON 解析失败、响应缺失可回读标识、网络类异常、或任何
    未预期的异常——都不属于这个类型，因此都会落进本模块三处外发调用的默认分支
    （"结果不明"，原样 ``raise``，见模块说明）。定义在这里而不是
    ``adapters.feishu_delivery``：后者已经依赖本模块的 ``CardTransport``/
    ``TextTransport`` Protocol，若异常类型反过来定义在 adapter 侧，本模块要捕获
    它就必须反向依赖 adapter，形成循环 import；异常类型属于 ``CardTransport``/
    ``TextTransport`` 这份协议契约的一部分，放在协议定义的同一处更自然。
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

        终态更新+关闭（``CardStream.finish()``）不经过 ``allow()`` 的单话题 500ms
        节流——那两帧是结果本身，被节流吞掉等于结果丢失，比不节流更糟（独立审核
        P2-2）。但全进程 50 次/秒的预算是 CardKit 的硬限制，终态这两次调用同样要
        计入，否则并发多话题同时终态时全局计数会失真、放过原本该被节流的其它
        话题。只推进全局窗口，不改动单话题的 500ms 记录——终态调用不受单话题
        节流约束，也不应该反过来去占用/影响它。
        """

        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        self._global_updates.append(now)


@dataclass(frozen=True)
class CardStreamResult:
    card_id: str | None
    sequence: int
    fallback_text: bool


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
    ) -> None:
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._reply_to_message_id = reply_to_message_id
        self._transport = transport
        self._fallback = fallback
        self._catalog = catalog or default_content_catalog()
        self._mark_external_side_effect = mark_external_side_effect
        self._monotonic = monotonic
        # resume 场景：非 None/非零表示接着一次此前已经持久化的进度继续，而不是从零建卡
        # （Issue #152、状态合同第 7 条：重启不产生第二张有效卡片）。
        self._card_id: str | None = initial_card_id
        self._sequence = initial_sequence
        self._message_id: str | None = initial_message_id
        self._fallback_needed = initial_fallback_needed
        self._last_update: float | None = None
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
        card = self._status_card(action="processing", elapsed_seconds=0)
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
            # 明确失败（白名单，独立审核 R-1）：卡片路径统一走同话题文本回退。
            self._notify_send("card_non_final", False)
            self._fallback_needed = True
        except Exception:  # noqa: BLE001 - 结果不明：建卡是否已经在服务端生效不可知，
            # 不能假装明确失败——原样抛给调用方，不降级、不清预留位、不重试。
            raise

    def update(self, *, elapsed_seconds: int, action: str = "processing") -> None:
        if self._card_id is None or self._fallback_needed:
            return
        now = self._monotonic()
        if not self._rate_limiter.allow(topic=self._topic, now=now):
            return
        card = self._status_card(action=action, elapsed_seconds=max(0, elapsed_seconds))
        self._sequence += 1
        try:
            self._before_external()
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_non_final", True)
            self._last_update = now
        except Exception:  # noqa: BLE001
            self._notify_send("card_non_final", False)
            self._fallback_needed = True

    def finish(
        self,
        *,
        result: str | None = None,
        failure: RenderedContent | None = None,
        elapsed_seconds: int = 0,
    ) -> None:
        """终态更新 + 关闭。**这两步的失败语义并不对称**（独立审核 P1-2）：

        - 终态 **更新** 失败：完整答案还没有确定写进卡片，用户看不到结果——必须
          整体降级为文本兜底，否则结果丢失。
        - 终态更新已经成功之后，仅 **关闭** 失败：答案已经在卡片里对用户可见，
          结果没有丢——按 G-CARD 实测（未手动关闭的流式卡片距上次开启 10 分钟后
          由平台自动关闭），关闭失败本身不构成"结果丢失"，只是收尾没做完。此时
          绝不能再触发文本兜底：那样只会让同一条答案在同一话题里出现两遍，是
          比"卡片没关"更差的用户体验，也直接违反"不得同时形成卡片终态与重复
          文本终态"（`V-卡片-03`）。因此关闭失败**不**置位 ``_fallback_needed``。
        """

        if self._card_id is None or self._fallback_needed:
            return
        if failure is None:
            body = result or self._catalog.card("query.empty").body
            completed = self._catalog.text(
                "worker.status",
                action=self._catalog.text("worker.action.completed").text,
                elapsed_seconds=max(0, elapsed_seconds),
            ).text
            body = f"{completed}\n{body}"
            card = self._catalog.card("query.result", result=body)
        else:
            card = self._catalog.card("query.failure", message=failure.text)

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except DeliveryRejected:
            # 明确失败（白名单，独立审核 R-1）：终态正文还没确定送达，只能整体降级。
            self._notify_send("card_final", False)
            self._fallback_needed = True
            return
        except Exception:  # noqa: BLE001 - 结果不明：终态正文是否已经写进卡片对进程
            # 不可知——绝不能当"明确失败"整体降级为文本兜底，那正是本次复现的跨
            # 通道重复投递。原样抛给调用方，不降级、不清预留位、不重试。
            raise

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.close(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except Exception:  # noqa: BLE001 - 见上面的方法说明：不降级，避免重复投递
            # 独立审核 B-1/R-1 均不延伸到这里：关闭失败——无论是 DeliveryRejected
            # 还是任何其它异常——都不改变"更新已经成功、答案对用户可见"这个结论，
            # 本身就与异常是否属于结果不明无关，因此不单独拆出 ``except DeliveryRejected``。
            self._notify_send("card_final", False)

    def send_fallback(self, content: RenderedContent) -> str | None:
        """发一次同话题文本兜底；不需要时（未降级）返回 ``None`` 且不产生外部调用。

        调用方负责在这次调用之前完成"外发前预留位"的持久化（Issue #151 审核 P3-6、
        本类文件头注释）——文本发送没有飞书原生幂等键，这里的异常刻意不吞掉，交由
        调用方区分"同步捕获的明确失败"（``DeliveryRejected``，可以清预留位、下一轮
        重试）、"结果不明"（除 ``DeliveryRejected`` 以外的一切异常，独立审核 R-1
        白名单：转 ``uncertain``，不清预留位、不重试）与"进程崩溃"（预留位留在
        数据库里，下一轮必须识别为 ``uncertain`` 而不是自动重发）。
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
            # 明确失败（白名单，独立审核 R-1）。
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

    def _status_card(self, *, action: str, elapsed_seconds: int) -> RenderedCard:
        action_key = "worker.action.completed" if action == "completed" else "worker.action.processing"
        action_text = self._catalog.text(action_key).text
        status = self._catalog.text(
            "worker.status", action=action_text, elapsed_seconds=elapsed_seconds
        ).text
        return self._catalog.card("query.status", status=status)

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
