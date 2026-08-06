"""``python -m lingxi.apps.worker`` 的启动点。

按[代码框架「二、三层之间的 import 规则」](../../../../docs/技术设计/代码框架.md)，
每个进程一个子包、以 ``python -m lingxi.apps.<name>`` 启动。这里只做转发，逻辑在
``cli.main``，这样入口可以被单测直接调用，不必每次都开子进程。
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
