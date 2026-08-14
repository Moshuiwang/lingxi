"""进程活性心跳文件（Issue #153）。

healthcheck 命令（``python -m lingxi.apps.healthcheck``）据此判断"进程活着但主
循环已经停止"——只探测依赖（数据库）可达，测不出主循环挂死；只探测 PID 存活，
测不出消费停止。两者都要，这个文件补上第二种。

三个常驻进程（scheduler/gateway/worker 队列模式）各自在主循环的每一轮里调用
``touch_liveness(role)`` 一次；healthcheck 命令用同一个 ``role`` 读取
``read_liveness_age_seconds`` 并与阈值比较。**只用于容器内自我健康检查**，不是
跨容器可观测——healthcheck 与被检查的进程运行在同一个容器（`docker exec` 语义），
共享同一个文件系统命名空间，因此不需要网络或共享卷。

文件写在容器的 ``/tmp``（三个进程的 compose 定义都已经给了可写的 tmpfs），
随容器重启自然清空——这正是我们想要的：不需要跨重启持久化"上一次活着是什么
时候"，重启后进程会在下一轮循环里很快重新写出一条新记录。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

DEFAULT_DIRECTORY = Path("/tmp")
FILENAME_TEMPLATE = "lingxi-{role}-liveness"


def liveness_path(role: str, *, directory: Path | None = None) -> Path:
    base = directory
    if base is None:
        override = os.environ.get("LINGXI_LIVENESS_DIR", "").strip()
        base = Path(override) if override else DEFAULT_DIRECTORY
    return base / FILENAME_TEMPLATE.format(role=role)


def touch_liveness(
    role: str, *, directory: Path | None = None, clock: Callable[[], float] = time.time
) -> None:
    """写入当前时间戳。写失败不抛异常——活性文件写不进去不能带走主循环；
    healthcheck 会因为文件缺失或过期如实变红，这本身就是我们想要的诚实失败。
    """

    path = liveness_path(role, directory=directory)
    try:
        path.write_text(repr(clock()), encoding="utf-8")
    except OSError:
        pass


def read_liveness_age_seconds(
    role: str, *, directory: Path | None = None, now: Callable[[], float] = time.time
) -> float | None:
    """返回活性文件距今的秒数；文件不存在或内容不可解析时返回 ``None``。"""

    path = liveness_path(role, directory=directory)
    try:
        written_at = float(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return max(0.0, now() - written_at)
