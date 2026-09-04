"""进程活性心跳文件。

healthcheck 命令据此判断"进程活着但主循环已经停止"——只探测依赖（数据库）
可达，测不出主循环挂死；只探测 PID 存活，测不出消费停止。两者都要，这个
文件补上第二种。

三个常驻进程各自在主循环的每一轮里调用 ``touch_liveness(role)`` 一次；
healthcheck 命令用同一个 ``role`` 读取 ``read_liveness_age_seconds`` 并与
阈值比较。只用于容器内自我健康检查，不是跨容器可观测——两者运行在同一个
容器，共享同一个文件系统命名空间，不需要网络或共享卷。

文件写在容器的 ``/tmp``，随容器重启自然清空——不需要跨重启持久化"上一次
活着是什么时候"，重启后进程会在下一轮循环里很快重新写出一条新记录。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_DIRECTORY = Path("/tmp")
FILENAME_TEMPLATE = "lingxi-{role}-liveness"


def liveness_path(role: str, *, directory: Path | None = None) -> Path:
    """算出该角色的活性文件路径；`directory` 缺省时按环境变量或默认目录取。"""
    base = directory
    if base is None:
        override = os.environ.get("LINGXI_LIVENESS_DIR", "").strip()
        base = Path(override) if override else DEFAULT_DIRECTORY
    return base / FILENAME_TEMPLATE.format(role=role)


def touch_liveness(
    role: str, *, directory: Path | None = None, clock: Callable[[], float] = time.time
) -> None:
    """写入当前时间戳。写失败不抛异常——活性文件写不进去不能带走主循环；healthcheck 会因为文件缺失或过期如实变红，这本身就是我们想要的诚实失败。"""
    path = liveness_path(role, directory=directory)
    try:
        path.write_text(repr(clock()), encoding="utf-8")
    except OSError:
        pass


def read_liveness_age_seconds(
    role: str, *, directory: Path | None = None, now: Callable[[], float] = time.time
) -> float | None:
    """返回活性文件距今的秒数；文件不存在或内容不可解析时返回 ``None``。

    时钟回拨（NTP 校时、容器迁移、手工调时钟）会让 ``now() - written_at``
    算出负数：不钳到 ``0.0``，因为那等于把回拨窗口内的任何一次探测都说成
    "心跳刚刚写入"，而调用方拿"年龄很小"当"近期已真实验证过"的证据，回拨
    越久缓存反而越显新鲜。负差值意味着这份时间戳不可信，因此返回 ``None``
    ——与"从未写过心跳"同一个信号，调用方据此退回真实探测。
    """
    path = liveness_path(role, directory=directory)
    try:
        written_at = float(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    age = now() - written_at
    if age < 0:
        return None
    return age
