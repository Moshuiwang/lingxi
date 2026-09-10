"""``python -m lingxi.apps.innertest_roster`` 的入口。

带 ``if __name__`` 卫语句（与 ``apps/admin_bootstrap`` 同惯例）：没有它，任何
``import lingxi.apps.innertest_roster.__main__``——包括 CI 的
``check_installed_package.py`` 完整性检查——都会在 import 期间真的执行一次
受控导入命令。
"""

from __future__ import annotations

from lingxi.apps.innertest_roster import run

if __name__ == "__main__":
    raise SystemExit(run())
