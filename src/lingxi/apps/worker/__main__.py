"""``python -m lingxi.apps.worker`` 的启动点。

按[代码框架「二、三层之间的 import 规则」](../../../../docs/技术设计/代码框架.md)，
每个进程一个子包、以 ``python -m lingxi.apps.<name>`` 启动。逻辑仍然全部在
``cli.main`` 里，这样入口可以被单测直接调用，不必每次都开子进程。

**这里有且只有一件不能下放给 ``cli.main`` 的事**（报告 R6-D2）：把生产数据库连接串
从 ``os.environ`` 里摘掉，让 Claude CLI 与它的 MCP 子进程不再继承它。那是一个
**进程级**副作用，而 ``main()`` 是一个会被单测在同一个解释器里反复调用的普通函数
——放在 ``main()`` 里，跑完几条队列模式的启动用例之后，同一个进程里所有真库用例的
DSN 就都没了（2026-09-02 CI 实测：40 个 ``setUpClass`` KeyError）。完整推导见
``cli.detach_process_environment`` 的文档字符串。

顺序是硬约束：**先摘、再进 main**。``detach_process_environment()`` 返回的快照是
摘除之前的完整副本，``main()`` 用它读全部配置（含 DSN），因此本进程照常工作，而
此后任何被 fork 出去的子进程都拿不到那个值。
"""

from __future__ import annotations

from .cli import detach_process_environment, main

if __name__ == "__main__":
    configuration, detached = detach_process_environment()
    raise SystemExit(main(env=configuration, detached_env_vars=detached))
