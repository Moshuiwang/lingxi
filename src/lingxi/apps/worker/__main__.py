"""``python -m lingxi.apps.worker`` 的启动点。

每个进程一个子包、以 ``python -m lingxi.apps.<name>`` 启动。逻辑全部在
``cli.main`` 里，入口可以被单测直接调用，不必每次都开子进程。

**这里有且只有一件不能下放给 ``cli.main`` 的事**：把生产数据库连接串从
``os.environ`` 里摘掉，让 Claude CLI 与它的 MCP 子进程不再继承它。那是一个
**进程级**副作用，而 ``main()`` 会被单测反复调用——放进 ``main()`` 会导致跑完
几条队列模式用例后同进程里所有真库用例的 DSN 都没了，完整推导见
``cli.detach_process_environment`` 的文档字符串。

顺序是硬约束：**先摘、再进 main**。``detach_process_environment()`` 返回的快照是
摘除之前的完整副本，``main()`` 用它读全部配置（含 DSN），本进程因此照常工作，
被 fork 出去的子进程都拿不到那个值。
"""

from __future__ import annotations

from .cli import detach_process_environment, main

if __name__ == "__main__":
    configuration, detached = detach_process_environment()
    raise SystemExit(main(env=configuration, detached_env_vars=detached))
