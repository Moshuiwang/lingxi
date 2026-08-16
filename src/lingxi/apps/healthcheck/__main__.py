"""``python -m lingxi.apps.healthcheck`` 的入口。

带 ``if __name__`` 卫语句（与 ``apps/worker``、``apps/gateway`` 同惯例）：
没有它，任何 ``import lingxi.apps.healthcheck.__main__``——包括 CI 的
``check_installed_package.py`` 完整性检查——都会在 import 期间真的跑
``argparse``，因缺少 ``--role`` 而 ``SystemExit(2)``，把 import 变成崩溃。
"""

from __future__ import annotations

from lingxi.apps.healthcheck import run

if __name__ == "__main__":
    raise SystemExit(run())
