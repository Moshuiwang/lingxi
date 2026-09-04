"""gateway 两条后台循环的存活保障：心跳、看门狗、以及"线程死了必须有人知道"。

长连接主线程一直阻塞在 supervisor 里，它没有别的机会发现后台线程已经死亡；投递
线程自己的单轮异常隔离也保证不了"整条线程消失"。这里把两件事挂在同一个已有时机
（每一轮心跳）上：不新增线程、不新增定时器，也不改变任何既有的停机语义。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from lingxi.apps.liveness import touch_liveness

def _combined_heartbeat(
    alerting_duty: Any, liveness_role: str, *, watchdog: Callable[[], None] | None = None
) -> Callable[[], None]:
    """把"记进 AlertManager"与"戳一下活性文件"合成一个心跳回调（Issue #153）。

    两件事共用同一个触发时机——都是"这条循环这一轮还活着"的证据——但服务不同的
    消费者：``AlertManager`` 供跨进程重启也能观察到的阈值/去重状态机，活性文件
    供同容器内的 ``python -m lingxi.apps.healthcheck`` 判断"主循环是否还在跳动"
    （见 ``apps/liveness.py`` 模块说明）。

    ``watchdog``（Issue #191）是搭在同一个时机上的第三件事：**这条循环还活着的
    时候，顺手确认另一条循环也还活着**。放在心跳之后调用，一次看门狗异常不会
    连累前面两件真正的心跳工作。
    """

    beat = alerting_duty.heartbeat_callback(liveness_role)

    def combined() -> None:
        beat()
        touch_liveness(liveness_role)
        if watchdog is not None:
            watchdog()

    return combined

def run_delivery_loop(
    consumer: Any,
    *,
    stop: threading.Event,
    poll_interval_seconds: float,
    heartbeat: Callable[[], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    on_dead: Callable[[str], None],
) -> None:
    """投递消费后台线程的实际入口：跑循环，并保证它一旦退出一定有人知道（#191）。

    ``DeliveryConsumer.run_forever`` 自己已经把单轮异常隔离掉，不会因为一次瞬时
    数据库错误退出；但"线程没了"这件事不能只由那一层保证——解释器级错误
    （``MemoryError`` 一类 ``BaseException``）、或将来某次改动把隔离改漏，都会让
    这条线程无声消失。此前 ``main()`` 只在 ``supervisor.run()`` 返回后才 ``join``，
    于是"Gateway 照常收消息、却一条也投不出去"要等容器活性文件过期才被健康检查
    间接发现，而 plain Docker Compose 并不会因为 unhealthy 重启容器（Issue #191）。

    因此 ``on_dead`` 覆盖两条退出路径：异常退出（连 ``BaseException`` 一起）、以及
    "没有收到停机信号却正常返回"。停机信号已置位时的正常返回是**预期**退出，
    不上报——那是优雅停机，不是故障。
    """

    try:
        consumer.run_forever(
            stop=stop,
            poll_interval_seconds=poll_interval_seconds,
            heartbeat=heartbeat,
            on_tick=on_tick,
        )
    except BaseException as error:
        # 原样抛回线程：Python 会打印完整栈，排查时那份栈是唯一能定位到具体
        # 语句的证据；告警只带异常类型，不带正文。
        on_dead(type(error).__name__)
        raise
    if not stop.is_set():
        on_dead("returned_without_stop")

def delivery_thread_watchdog(
    thread: threading.Thread, *, stop: threading.Event, on_dead: Callable[[str], None]
) -> Callable[[], None]:
    """给长连接主线程用的看门狗（Issue #191）：确认投递线程还活着。

    ``main()`` 的主线程一直阻塞在 ``supervisor.run()`` 里直到停机，它没有别的机会
    发现后台线程已经死亡。长连接每一轮（含没有用户消息时的空闲心跳）都会调用一次
    心跳回调，把这次检查挂在同一个时机上：不新增线程、不新增定时器，也不改变任何
    既有的停机语义。

    只上报一次——线程死亡是不可逆状态，每一轮重复上报只会刷屏。线程还没 ``start()``
    时 ``ident`` 为 ``None``，那是"尚未开始"而不是"已经死亡"，不上报。
    """

    reported = [False]

    def check() -> None:
        if reported[0] or stop.is_set():
            return
        if thread.ident is None or thread.is_alive():
            return
        reported[0] = True
        on_dead("thread_not_alive")

    return check

def _combined_watchdog(*checks: Callable[[], None]) -> Callable[[], None]:
    """把多个看门狗检查合成一个（Issue #341 S-ES-3）：长连接主线程的心跳回调只
    接受一个 ``watchdog`` 参数，而现在最多有两条独立的后台线程（既有投递消费
    循环、新增的文档投递消费循环）各自需要被确认还活着。一次检查异常不能连累
    另一条的检查——`delivery_thread_watchdog` 返回的 ``check`` 各自已经把"只
    上报一次"的状态封装在自己的闭包里，这里只是顺序调用，不需要额外的异常隔离
    （`_combined_heartbeat` 的调用方已经把整个 ``watchdog`` 调用包在
    try/except 里）。
    """

    def check() -> None:
        for one in checks:
            one()

    return check
