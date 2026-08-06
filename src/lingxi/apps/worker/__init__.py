"""``lingxi-worker`` 的最薄可执行入口（Issue #37）。

它回答的是一个此前没有答案的问题：**只读屏障真的被装上了吗？** Issue #29 交付的
`ToolGateway`、`TurnAudit` 与 `build_hook_matchers` 在仓库里没有任何调用方，而
"组件存在 + 单测通过 ≠ 屏障在生效"。本入口执行**一个** Agent SDK 回合，把判定、
hook 回调、工具回执、最终正文和终止结果全部汇入同一个回合审计，并把脱敏后的结果
以 JSON 打到 stdout。

**它现在是什么**：受控验证用的最薄执行器，由人或受控脚本直接运行。
**它现在不是什么**：飞书问数入口、任务队列消费者、生产 worker。没有任务领取、
没有并发、没有重试、没有交付、没有审计持久化——这些按 Issue #37 的停止线留给后续切片。

用法::

    LINGXI_WORKER_QUESTION=... LINGXI_WORKER_READONLY_TOOL=mcp__... \\
        python -m lingxi.apps.worker
"""
