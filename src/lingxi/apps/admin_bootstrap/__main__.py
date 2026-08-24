"""``python -m lingxi.apps.admin_bootstrap`` 的入口。

带 ``if __name__`` 卫语句（与 ``apps/healthcheck``、``apps/worker``、``apps/gateway``、
``apps/trace`` 同惯例）：没有它，任何 ``import lingxi.apps.admin_bootstrap.__main__``
——包括 CI 的 ``check_installed_package.py`` 完整性检查——都会在 import 期间真的执行
一次种子播种流程。
"""

from __future__ import annotations

from lingxi.apps.admin_bootstrap import run

if __name__ == "__main__":
    raise SystemExit(run())
