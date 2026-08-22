"""``python -m lingxi.apps.trace`` 的入口。

带 ``if __name__`` 卫语句（与 ``apps/healthcheck``、``apps/worker``、``apps/gateway``
同惯例）：没有它，任何 ``import lingxi.apps.trace.__main__``——包括 CI 的
``check_installed_package.py`` 完整性检查——都会在 import 期间真的跑 ``argparse``，
因缺少位置参数 ``trace_id`` 而 ``SystemExit(2)``，把 import 变成崩溃。
"""

from __future__ import annotations

from lingxi.apps.trace import run

if __name__ == "__main__":
    raise SystemExit(run())
