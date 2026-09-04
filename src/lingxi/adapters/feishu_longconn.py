"""飞书长连接接入：错误分类、重连策略与事件泵。

保留官方 ``lark-oapi``，长连接用它的 ``ws.Client``。但**重连策略与终止判定
不交给 SDK**：``Client.start()`` 是一个永久循环、绑死模块级全局事件循环，
进程无法在它之上做优雅停机（`V-部署-03`）；SDK 的重连是固定间隔、没有
退避；同一个 403 在 SDK 里有两种相反的语义（endpoint HTTP 的 403 可重试，
wss 握手头的 403 不可重试），只看异常类型或只看数字都会得到错的结论。

因此本模块自己拥有分类与重连循环，SDK 只承担"建一条连接、把帧交出来"。这也
正是 `V-接入-06` 要求的——终止判定必须落在我们自己可注入的一层。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)

# 飞书长连接的错误码（与 lark_oapi/ws/const.py 一致，此处自己声明一份，
# 避免把 SDK 的私有常量模块变成我们的依赖）。
FORBIDDEN = 403
AUTH_FAILED = 514
EXCEED_CONN_LIMIT = 1000040350


class FailureSource(str, Enum):
    """失败发生在握手的哪一段。分类必须按「来源 + 码」二元组，见模块 docstring 第 3 条。"""

    ENDPOINT_HTTP = "endpoint_http"
    WS_HANDSHAKE = "ws_handshake"
    STREAM = "stream"


class FailureKind(str, Enum):
    """判定结果：终止型不再重连，可重试型交给退避循环。"""

    TERMINAL = "terminal"
    RETRYABLE = "retryable"


@dataclass(frozen=True)
class HandshakeFailure:
    """一次连接失败的**我们自己的**表示。

    刻意不携带 SDK 异常对象：分类函数因此可以在没装 ``lark-oapi`` 的环境里被直接调用，
    测试也能构造出 SDK 暂时造不出来的组合（例如 514 但没有 autherrcode 头）。
    """

    source: FailureSource
    status_code: int | None = None
    auth_errcode: int | None = None
    detail: str = ""


class LongConnectionError(Exception):
    """建连失败。``failure`` 是分类的唯一输入。"""

    def __init__(self, failure: HandshakeFailure) -> None:
        """记录分类用的 failure 表示，消息只含状态码与来源，不含凭据。"""
        super().__init__(
            f"{failure.source.value}: status={failure.status_code} detail={failure.detail}"
        )
        self.failure = failure


def classify_handshake_failure(failure: HandshakeFailure) -> FailureKind:
    """终止型还是可重试？

    终止型只有两种：**403**（凭据或权限被拒，无论来自 endpoint HTTP 还是
    wss 握手头都判终止——重试一个被拒的凭据不会变成通过，只会把日志刷满并
    持续占用飞书侧的连接配额，这比 SDK 严格）；**514 且 autherrcode =
    1000040350**（超出连接数上限，再连只会继续被拒并把已经建好的连接挤掉）。
    其余一律可重试，包括 514 配其他 autherrcode——这一条是刻意保守，把
    "凭据错误"这类 514 也判终止属于扩大解释，会让一次服务端抖动直接停掉
    进程；代价是凭据真错时会一直重试，由审计里的连续失败暴露。
    """
    if failure.status_code == FORBIDDEN:
        return FailureKind.TERMINAL
    if failure.status_code == AUTH_FAILED and failure.auth_errcode == EXCEED_CONN_LIMIT:
        return FailureKind.TERMINAL
    return FailureKind.RETRYABLE


@dataclass(frozen=True)
class BackoffPolicy:
    """重连退避。

    `V-接入-05` 要求「间隔单调退避且首次重连不早于配置下限」，并明确「固定间隔或零间隔
    的忙循环必须使该用例变红」——因此 ``factor`` 必须 > 1，``base`` 必须 > 0，
    在构造期就校验，而不是等到真的忙循环起来才发现。
    """

    base_seconds: float = 1.0
    factor: float = 2.0
    ceiling_seconds: float = 60.0

    def __post_init__(self) -> None:
        """校验退避参数：下限为正、倍数大于 1、上限不小于下限。"""
        if self.base_seconds <= 0:
            raise ValueError("重连退避的下限必须为正数，零间隔会变成忙循环")
        if self.factor <= 1:
            raise ValueError("重连退避的倍数必须大于 1，固定间隔不是退避")
        if self.ceiling_seconds < self.base_seconds:
            raise ValueError("重连退避的上限不能小于下限")

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次重连（从 0 起）之前要等多久。"""
        if attempt < 0:
            raise ValueError("重连次数不能为负")
        return min(self.base_seconds * (self.factor**attempt), self.ceiling_seconds)


class TerminationReason(str, Enum):
    """事件泵停下来的原因：正常停机，还是撞上了终止型错误。"""

    STOPPED = "stopped"  # 收到停机信号，正常退出
    TERMINAL_ERROR = "terminal"  # 终止型错误，不再重连


class EventTransport(Protocol):
    """一条长连接的抽象。

    ``stream()`` 建连并逐条产出**原始事件体**（已解码的 dict）。建连失败抛
    ``LongConnectionError``；连接中断则正常结束迭代或抛其他异常，两者都按可重试处理。

    空闲时**必须**定期产出 ``None`` 心跳，好让 supervisor 有机会检查停机信号。
    一个只在有事件时才让出控制权的实现，会让 ``SIGTERM`` 在没有用户消息时永远不被
    看见（`V-部署-03`）。
    """

    def stream(self) -> Iterator[dict | None]:
        """建连并逐条产出事件；空闲心跳产出 ``None``。"""
        ...


class LongConnectionSupervisor:
    """拥有重连循环的事件泵。

    进程**不监听任何入站端口**（`V-接入-10`）：事件的唯一来源是 ``transport.stream()``
    产出的迭代器，本类没有第二个投递入口，也不启动任何服务端套接字。
    """

    def __init__(
        self,
        *,
        transport: EventTransport,
        handle_event: Callable[[dict], dict | None],
        backoff: BackoffPolicy | None = None,
        # 签名带 ``should_stop``：退避等待必须可被停机打断，不能是一个不可中断的
        # ``time.sleep``。把它做进类型里，注入的假实现也就不会退化成不可打断的。
        sleep: Callable[[float, Callable[[], bool]], None] | None = None,
        audit: Callable[..., None] | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        """装配事件泵；``sleep``/``audit``/``heartbeat`` 未注入时各有默认实现。"""
        self._transport = transport
        self._handle_event = handle_event
        self._backoff = backoff or BackoffPolicy()
        # 注入时钟/注入睡眠：重连间隔断言不能靠真的等 60 秒。
        self._sleep = sleep if sleep is not None else _real_sleep
        self._audit = audit if audit is not None else _log_audit
        self._heartbeat = heartbeat
        # 供断言读取：`V-接入-06` 要求终止型错误下重连次数**恒为 0**。
        self.reconnect_attempts = 0
        self.observed_delays: list[float] = []

    def run(self, *, should_stop: Callable[[], bool]) -> TerminationReason:
        """跑到收到停机信号或遇到终止型错误为止。"""
        attempt = 0
        while not should_stop():
            self._emit_heartbeat()
            try:
                for payload in self._transport.stream():
                    self._emit_heartbeat()
                    if should_stop():
                        break
                    if payload is None:
                        # 空闲心跳，不是事件：传输层在没有消息时定期让出控制权，
                        # 好让上面那句停机检查在「当前没有任何用户消息」时也能跑到。
                        # 不派发、不计数。
                        continue
                    # 收到过真实事件 = 这条连接是健康的。退避重新从下限起算，
                    # 否则一个健康跑了很久、期间抖动过几次的进程，此后每次断线都
                    # 直接等上限（独立复查 P2-5）。
                    attempt = 0
                    self._dispatch(payload)
                # 迭代正常结束 = 对端关闭连接，属可重试。断线的两条路径形态不同
                # （连接线程退出 vs 连接静默死亡），审计里要分得出是哪一种。
                if not should_stop():
                    self._audit(
                        "longconn.closed",
                        reason=getattr(self._transport, "last_close_reason", None),
                    )
            except LongConnectionError as error:
                if classify_handshake_failure(error.failure) is FailureKind.TERMINAL:
                    self._audit(
                        "longconn.terminal",
                        source=error.failure.source.value,
                        status_code=error.failure.status_code,
                        auth_errcode=error.failure.auth_errcode,
                    )
                    return TerminationReason.TERMINAL_ERROR
                self._audit(
                    "longconn.retryable",
                    source=error.failure.source.value,
                    status_code=error.failure.status_code,
                )
            except Exception as error:  # 传输层任何异常都按可重试处理
                self._audit("longconn.stream_failed", error=f"{type(error).__name__}: {error}")

            if should_stop():
                break

            delay = self._backoff.delay_for(attempt)
            self.observed_delays.append(delay)
            self.reconnect_attempts += 1
            attempt += 1
            # 退避等待必须**可被停机信号打断**：上限 60 秒的睡眠里收到 SIGTERM，
            # 不打断就要等满，实测能让停机远超配置的超时（验收 P1-4）。
            self._sleep(delay, should_stop)

        return TerminationReason.STOPPED

    def _emit_heartbeat(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except Exception as error:  # 心跳观察失败不能断开长连接
            try:
                self._audit("longconn.heartbeat_failed", error=type(error).__name__)
            except Exception as audit_error:  # 审计观察失败也不能断流
                logger.error(
                    "长连接心跳失败且审计失败 error=%s audit_error=%s",
                    type(error).__name__,
                    type(audit_error).__name__,
                )

    def _dispatch(self, payload: dict) -> None:
        """把一条事件交给处理器，并把结果（含处理器的返回值）回报给传输层。

        `V-接入-12`：处理器抛异常时长连接不断开、后续事件照常处理。**结果
        必须回报给传输层**：SDK 线程正阻塞在这条事件上等 ack，不回报的话
        落库失败会被飞书当成投递成功，消息静默丢失。**处理器的返回值随
        ``response`` 一起回报**：``card.action.trigger`` 回调的处理器返回
        一个应答字典供 lark SDK marshal 进应答帧；普通消息事件与处理器
        抛异常时都恒为 ``None``。
        """
        error: BaseException | None = None
        response: dict | None = None
        try:
            response = self._handle_event(payload)
        except Exception as caught:  # 见 docstring
            error = caught
            self._audit("event.handler_failed", error=f"{type(caught).__name__}: {caught}")
        finally:
            report = getattr(self._transport, "report", None)
            if report is not None:
                report(payload, error, response)


class PendingEvent:
    """一条正在处理中的事件，以及它的落库结果。

    ``_RawEventSink`` 提交它、阻塞等待；supervisor 处理完之后回填结果并
    唤醒。``response`` 承载处理器成功时的返回值——普通消息事件恒为
    ``None``；``card.action.trigger`` 回调的处理器返回一个应答字典，
    ``_RawEventSink._do_without_validation`` 在成功时把它原样交回给 SDK。
    """

    def __init__(self, payload: dict) -> None:
        """记录待处理的原始 payload；``done`` 由 :meth:`complete` 置位。"""
        self.payload = payload
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.response: dict | None = None

    def complete(self, error: BaseException | None, response: dict | None = None) -> None:
        """回填处理结果并唤醒等待方。"""
        self.error = error
        self.response = response
        self.done.set()


class _RawEventSink:
    """冒充 SDK 的 ``EventDispatcherHandler``，把**原始事件体**交出来，并等它处理完。

    ``ws.Client`` 对 ``event_handler`` 只调用 ``_do_without_validation(bytes)``
    一个方法；``parse_message_event`` 承载着 `V-接入-11`，吃的是 dict。**本
    方法必须阻塞到落库有结果为止**：SDK 在方法返回后立刻回帧，不阻塞会让
    "飞书认为已投递"早于"写进数据库"，进程在这个窗口崩溃会永久丢消息
    （`V-队列-01`）。返回值就是卡片回调应答通道：成功时返回
    ``pending.response``（不恒为 ``None``），供 ``card.action.trigger``
    换上新卡片；普通消息事件恒为 ``None``。
    """

    def __init__(
        self, submit: Callable[[PendingEvent], None], *, ack_timeout_seconds: float
    ) -> None:
        self._submit = submit
        self._ack_timeout_seconds = ack_timeout_seconds

    def _do_without_validation(self, payload: bytes) -> dict | None:
        import json

        pending = PendingEvent(json.loads(payload.decode("utf-8")))
        self._submit(pending)
        if not pending.done.wait(self._ack_timeout_seconds):
            raise TimeoutError(
                f"事件处理未在 {self._ack_timeout_seconds} 秒内完成落库，向飞书返回失败以便平台重投"
            )
        if pending.error is not None:
            raise pending.error
        return pending.response


def translate_sdk_exception(error: BaseException) -> HandshakeFailure:
    """把 SDK 的建连异常翻译成我们自己的 ``HandshakeFailure``。

    ``ClientException(403)`` ← wss 握手响应头里的 403；``ServerException(403)``
    ← endpoint HTTP 接口返回 403；``ClientException(514)`` ← 514 且
    autherrcode = 1000040350（SDK 只在这一种组合下抛 ClientException）；
    ``ServerException(514)`` ← 514 配其他 autherrcode。两个 514 分支的区分
    是这段翻译存在的主要理由：SDK 把"超连接数上限"与"其他鉴权失败"用异常
    类型区分开了，而 autherrcode 本身没有出现在异常对象上。
    """
    from lark_oapi.ws.exception import ClientException, ServerException

    code = getattr(error, "code", None)
    if isinstance(error, ClientException):
        return HandshakeFailure(
            source=FailureSource.WS_HANDSHAKE,
            status_code=code,
            auth_errcode=EXCEED_CONN_LIMIT if code == AUTH_FAILED else None,
            detail=str(error),
        )
    if isinstance(error, ServerException):
        return HandshakeFailure(
            source=FailureSource.ENDPOINT_HTTP,
            status_code=code,
            auth_errcode=None,
            detail=str(error),
        )
    return HandshakeFailure(source=FailureSource.STREAM, detail=f"{type(error).__name__}: {error}")


class LarkEventTransport:
    """基于官方 ``lark-oapi`` 的真实长连接传输层。

    **本类的真实行为未验证（证据等级 1），属 L4a**：全部断言跑在注入的假
    传输层上，不连真实飞书，这是本切片的既定口径。已知边界：``ws.Client``
    没有 ``stop()``，``start()`` 永久阻塞，因此放进 daemon 线程；
    ``auto_reconnect=False``，重连改由 ``LongConnectionSupervisor`` 负责；
    SDK 连接中途断开的异常会被吞掉，靠 ``start()`` 返回或线程结束来发现
    连接已死；建连超时收尾对 pump 线程 ``join(timeout=2)``，未能退出时刻意
    不关闭仍被占用的 loop，累积上限未做真实部署观测。
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        queue_max: int = 1000,
        poll_seconds: float = 0.5,
        ack_timeout_seconds: float = 30.0,
        handshake_timeout_seconds: float = 30.0,
    ) -> None:
        """记录应用凭据与各项超时；不在构造期建立任何连接。"""
        self._app_id = app_id
        self._app_secret = app_secret
        self._queue_max = queue_max
        # 空闲轮询间隔。它决定「停机信号最晚多久被看见」，因此由 gateway 的停机
        # 超时推导而来（见 apps/gateway.build_supervisor），不是随手取的常数。
        self._poll_seconds = poll_seconds
        # 单条事件从收到到落库有结果的上限。超过就向 SDK 抛异常让飞书重投，
        # 而不是无限期占住 SDK 的接收协程。
        self._ack_timeout_seconds = ack_timeout_seconds
        # 建连截止时间。**没有它就有一个活性黑洞**：endpoint 请求卡住、或首轮询之前
        # 连接就断了，``connected_once`` 会恒为 False，于是"掉线"判定永远不成立、
        # 空闲心跳永远产出，supervisor 认为一切正常而永不重连——一条从未连上的连接
        # 能让进程静默失聪到重启为止。
        self._handshake_timeout_seconds = handshake_timeout_seconds
        # 正在等待落库结果的事件，按 payload 的对象身份索引。
        self._pending: dict[int, PendingEvent] = {}
        # 上一条连接是**怎么结束的**，供 supervisor 记审计。断线有两条形态不同的路径
        # （线程退出 vs 静默死亡），只记一句"连接结束"分不出是哪一种（内审 P2-4）。
        self.last_close_reason: str | None = None

    def report(
        self, payload: dict, error: BaseException | None, response: dict | None = None
    ) -> None:
        """Supervisor 处理完一条事件后回填结果，唤醒还在等 ack 的 SDK 线程。

        成功时含处理器的返回值（例如卡片回调应答）。
        """
        pending = self._pending.pop(id(payload), None)
        if pending is not None:
            pending.complete(error, response)

    def stream(self) -> Iterator[dict | None]:
        """建连并逐条产出事件；空闲时定期产出 ``None`` 心跳。

        **心跳不是装饰，是停机与断线检测的唯一入口。** 不带超时地阻塞在队列上
        时，supervisor 只在收到一条事件之后才检查停机信号，于是进程一直挂到
        编排层 SIGKILL；空闲时让出控制权之后，停机与断线检测才有机会发生。
        """
        import queue as queue_module

        # 有界队列：处理慢于收取时必须显式阻塞收取侧，而不是让内存无声增长。
        events: queue_module.Queue = queue_module.Queue(maxsize=self._queue_max)
        finished = object()
        failure: list[BaseException] = []
        fresh_loop, client, thread = self._spawn_pump_thread(events, finished, failure)

        try:
            yield from self._drain(events, finished, thread, client)
            # **在 finally 之前**判断失败。下面的清理会主动 stop 掉这条连接的 loop，
            # 那会让 pump 线程的 run_until_complete 抛错并记进 failure——那是我们
            # 自己造成的，不是连接故障。放到 finally 之后判断的话，每一次正常收尾
            # 都会被报成一次连接错误。
            if failure:
                raise LongConnectionError(translate_sdk_exception(failure[0])) from failure[0]
        finally:
            self._teardown_connection(fresh_loop, thread)

    def _spawn_pump_thread(
        self, events: object, finished: object, failure: list[BaseException]
    ) -> tuple[object, object, threading.Thread]:
        """给这次连接换一个全新的事件循环，起一个后台线程跑 SDK 的 ``ws.Client.start()``。

        SDK 的 loop 是**模块级全局**：连接中途死亡时异常被吞进一个没人
        await 的 task，那个 loop 仍然 running——第二次 ``stream()`` 里的新
        ``client.start()`` 会在已经 running 的 loop 上调用，立刻
        ``RuntimeError``（重连一次就永久失聪）。换新 loop 是唯一能在本地
        复现验证的方向；这是在改第三方模块的全局状态，因此**一个进程只能
        有一条飞书长连接**。旧 loop 在 :meth:`_teardown_connection` 里被
        停掉。
        """
        import asyncio

        import lark_oapi as lark
        from lark_oapi.ws import client as ws_client_module

        fresh_loop = asyncio.new_event_loop()
        ws_client_module.loop = fresh_loop

        client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=_RawEventSink(
                self._submit_pending(events), ack_timeout_seconds=self._ack_timeout_seconds
            ),
            auto_reconnect=False,
        )

        def pump() -> None:
            try:
                client.start()
            except BaseException as error:  # 交给主线程分类
                failure.append(error)
            finally:
                events.put(finished)  # type: ignore[attr-defined]

        thread = threading.Thread(target=pump, name="lingxi-gateway-longconn", daemon=True)
        thread.start()
        return fresh_loop, client, thread

    def _teardown_connection(self, fresh_loop: object, thread: threading.Thread) -> None:
        """连接结束后的清理：唤醒仍在等 ack 的事件，停掉并按需关闭这条连接专属的 loop。

        线程没能在超时内退出时**不关闭** loop——关掉一个仍在被使用的 loop
        会把一个资源泄漏换成一个崩溃；每条连接一个 loop，能关掉时不关就是
        每次重连漏一组文件描述符。
        """
        # 唤醒还在等 ack 的 SDK 线程：连接已经没了，让它们向飞书报失败而不是
        # 一直等到超时。
        for pending in list(self._pending.values()):
            pending.complete(ConnectionError("长连接已结束，事件未完成落库"))
        self._pending.clear()
        try:
            fresh_loop.call_soon_threadsafe(fresh_loop.stop)  # type: ignore[attr-defined]
        except RuntimeError:
            pass  # 已经停了
        thread.join(timeout=2)
        if not thread.is_alive():
            fresh_loop.close()  # type: ignore[attr-defined]

    def _submit_pending(self, events: object) -> Callable[[PendingEvent], None]:
        def submit(pending: PendingEvent) -> None:
            self._pending[id(pending.payload)] = pending
            events.put(pending.payload)  # type: ignore[attr-defined]

        return submit

    def _drain(self, events, finished, thread, client) -> Iterator[dict | None]:
        import queue as queue_module
        import time

        connected_once = False
        deadline = time.monotonic() + self._handshake_timeout_seconds
        while True:
            try:
                item = events.get(timeout=self._poll_seconds)
            except queue_module.Empty:
                if not thread.is_alive():
                    # start() 已经返回或抛错（握手失败走这条）。
                    self.last_close_reason = "connection_thread_exited"
                    break
                connected_once, lost = _connection_liveness(client, connected_once)
                if not connected_once and time.monotonic() > deadline:
                    # 截止时间内没连上：判连接失败，交回 supervisor 走退避重连。
                    # 不加这条，一条卡在 endpoint 上的连接会永远产出心跳，
                    # 而 supervisor 会一直以为它是健康的。
                    self.last_close_reason = "handshake_timeout"
                    break
                if lost:
                    # 连接中途静默死亡：SDK 在 auto_reconnect=False 下把接收循环的
                    # 异常抛在一个没人 await 的 task 里，异常被吞、start() 永不返回、
                    # 线程一直活着。唯一可观察的信号是 SDK 在抛之前先把 _conn 置回
                    # None；不检测它的话，进程看起来健康但再也收不到任何用户消息。
                    self.last_close_reason = "connection_lost_silently"
                    break
                yield None  # 空闲心跳：把控制权交回 supervisor
                continue
            if item is finished:
                break
            yield item


def _connection_liveness(client: object, connected_once: bool) -> tuple[bool, bool]:
    """返回 ``(是否曾经连上, 是否已经掉线)``。

    读的是 SDK 的私有属性 ``_conn``，因为 1.7.1 没有提供任何公开的连接状态。
    **降级方式是刻意选的**：如果上游改名，``getattr`` 恒得 ``None``，
    ``connected_once`` 永远为假，于是本函数永远报「没掉线」——退回到没有断线检测的
    行为（与本次修复前一致），而不是反过来误报掉线、把进程带进重连风暴。
    """
    conn = getattr(client, "_conn", None)
    if conn is not None:
        return True, False
    return connected_once, connected_once


def _real_sleep(seconds: float, should_stop: Callable[[], bool]) -> None:
    """分片睡眠，随时可被停机信号打断。

    一次性 ``time.sleep(60)`` 会把停机拖到退避结束——验收实测 SIGTERM 之后 45 秒
    进程仍未退出，而配置的停机超时是 20 秒。
    """
    import time

    deadline = time.monotonic() + seconds
    while not should_stop():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.2))


def _log_audit(action: str, /, **fields: object) -> None:
    logger.info("%s %s", action, fields)
