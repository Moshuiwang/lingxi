"""``lingxi-worker`` 的受控执行入口。

它回答一个此前没有答案的问题：只读屏障真的被装上了吗？`ToolGateway`、
`TurnAudit` 与 `build_hook_matchers` 交付时在仓库里没有任何调用方，组件存在、
单测通过不等于屏障在生效。本入口执行**一个** Agent SDK 回合，把判定、hook
回调、工具回执、最终正文和终止结果全部汇入同一个回合审计，脱敏后以 JSON
打到 stdout。

**turn 模式**是受控验证用的单回合执行器；**queue 模式**接入 PostgreSQL 任务
领取、会话续用、心跳/停止与失败收口。真实飞书 transport 由调用方注入，不在
本模块伪造平台协议。用法见 ``LINGXI_WORKER_QUESTION``/
``LINGXI_WORKER_READONLY_TOOLS`` 等环境变量（见 :mod:`config`）。
"""
