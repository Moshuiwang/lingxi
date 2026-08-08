"""``lingxi-worker`` 的受控执行入口（Issue #37 / #90）。

它回答的是一个此前没有答案的问题：**只读屏障真的被装上了吗？** Issue #29 交付的
`ToolGateway`、`TurnAudit` 与 `build_hook_matchers` 在仓库里没有任何调用方，而
"组件存在 + 单测通过 ≠ 屏障在生效"。本入口执行**一个** Agent SDK 回合，把判定、
hook 回调、工具回执、最终正文和终止结果全部汇入同一个回合审计，并把脱敏后的结果
以 JSON 打到 stdout。

**turn 模式**仍是受控验证用的单回合执行器；**queue 模式**接入 PostgreSQL 任务领取、
会话续用、心跳/停止与失败收口。真实飞书 CardKit/文本 transport 由调用方注入，
真实端到端仍属于 E4 的 L4a 验收，不在本模块伪造平台协议。

用法::

    LINGXI_WORKER_QUESTION=... LINGXI_WORKER_READONLY_TOOL=mcp__... \\
        python -m lingxi.apps.worker
"""
