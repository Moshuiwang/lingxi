"""``python -m lingxi.apps.reauthorize`` 的一次性重授权入口。

命令行参数在这里显式取出并传进 ``main``：``main`` 本身不读 ``sys.argv``，
避免程序内调用时把宿主进程的参数误解释成一次首次建立。
"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
