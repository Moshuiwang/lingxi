"""gateway 两条后台循环的存活保障：心跳、看门狗，以及"线程死了必须有人知道"。

长连接主线程一直阻塞在 supervisor 里，它没有别的机会发现后台线程已经死亡；投递
循环自己的单轮异常隔离也保证不了"整条线程消失"。这里把这两件事挂在同一个已有时机
（每一轮心跳）上：不新增线程、不新增定时器，也不改变任何既有的停机语义。

为什么不能只靠健康检查兜底：活性文件过期只会让容器变成 unhealthy，而 plain Docker
Compose 不会因 unhealthy 重启容器——用户侧看到的就是"发了没反应"，没有任何告警。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from lingxi.apps.liveness import touch_liveness


def combined_heartbeat(
    alerting_duty: Any, liveness_role: str, *, watchdog: Callable[[], None] | None = None
) -> Callable[[], None]:
    """把"记进告警状态机"与"戳一下活性文件"合成一个心跳回调。

    两件事共用同一个触发时机——都是"这条循环这一轮还活着"的证据——但服务不同的
    消费者：告警状态机供跨进程重启也能观察到的阈值/去重判定，活性文件供同容器内的
    健康检查命令判断"主循环是否还在跳动"。

    Args:
        alerting_duty: 提供 ``heartbeat_callback`` 的告警职责对象。
        liveness_role: 活性文件的角色名，两条循环各用各的。
        watchdog: 搭在同一个时机上的第三件事——这条循环还活着的时候，顺手确认
            另一条也还活着。放在心跳之后调用，看门狗异常不连累真正的心跳工作。

    Returns:
        可以直接交给循环的心跳回调。
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
    """投递消费后台线程的实际入口：跑循环，并保证它一旦退出一定有人知道。

    消费者自己已经把单轮异常隔离掉，不会因为一次瞬时数据库错误退出；但"线程没了"
    这件事不能只由那一层保证——解释器级错误（``BaseException`` 一类）、或将来某次
    改动把隔离改漏，都会让这条线程无声消失。

    Args:
        consumer: 提供 ``run_forever`` 的消费者。
        stop: 停机信号，与主线程共享同一个。
        poll_interval_seconds: 空闲轮询间隔。
        heartbeat: 每轮心跳回调。
        on_tick: 每轮结束时的附加动作。
        on_dead: 线程非预期退出时的唯一上报出口。覆盖两条路径：异常退出，以及
            "没有收到停机信号却正常返回"。停机信号已置位时的正常返回是**预期**
            退出，不上报——那是优雅停机，不是故障。
    """
    try:
        consumer.run_forever(
            stop=stop,
            poll_interval_seconds=poll_interval_seconds,
            heartbeat=heartbeat,
            on_tick=on_tick,
        )
    except BaseException as error:
        # 原样抛回线程：Python 会打印完整栈，排查时那份栈是唯一能定位到具体语句
        # 的证据；告警只带异常类型，不带正文。
        on_dead(type(error).__name__)
        raise
    if not stop.is_set():
        on_dead("returned_without_stop")


def delivery_thread_watchdog(
    thread: threading.Thread, *, stop: threading.Event, on_dead: Callable[[str], None]
) -> Callable[[], None]:
    """给长连接主线程用的看门狗：确认后台投递线程还活着。

    长连接每一轮（含没有用户消息时的空闲心跳）都会调用一次心跳回调，这次检查就挂在
    那个时机上。只上报一次——线程死亡是不可逆状态，每轮重复上报只会刷屏；线程还没
    ``start()`` 时 ``ident`` 为 ``None``，那是"尚未开始"而不是"已经死亡"，不上报。

    Returns:
        可以挂进心跳的检查函数。
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


def combined_watchdog(*checks: Callable[[], None]) -> Callable[[], None]:
    """把多个看门狗检查合成一个：心跳回调只接受一个 ``watchdog`` 参数。

    这里只是顺序调用，不做额外的异常隔离：每个检查各自把"只上报一次"的状态封在
    自己的闭包里，而调用方已经把整个 ``watchdog`` 调用包在 try/except 里。

    Returns:
        依次跑完全部检查的单个函数。
    """

    def check() -> None:
        for one in checks:
            one()

    return check
