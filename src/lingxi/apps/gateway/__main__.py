"""``python -m lingxi.apps.gateway`` 的启动点。

带 ``if __name__`` 卫语句（与 ``apps/worker`` 同惯例，不同于 ``apps/scheduler``）：
没有它，任何 ``import lingxi.apps.gateway.__main__`` ——包括 CI 的制品完整性检查
——都会真的把长连接进程跑起来。
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    raise SystemExit(main())
