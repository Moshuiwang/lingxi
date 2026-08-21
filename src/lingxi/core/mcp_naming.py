"""问数 MCP 服务名：全仓库唯一一份（独立审查，分支 fix/291-280-user-experience 收尾）。

放在 ``core/`` 的公共小模块里是[代码框架「三、横切约定」]
(../../../docs/技术设计/代码框架.md)的要求：这类横切事实只允许存在一份，各模块不自造。
``core/`` 结构性地不能 import ``adapters/``、``apps/`` 或任何外部 SDK
（``scripts/ci/check_core_layering.py`` 强制），因此把它放在这里从构造上保证它是一个
真正零依赖的常量，不会把无关的依赖链带给只需要这一个字符串的调用方。

## 为什么单独成一个模块，不留在 ``adapters/user_environment.py``

这个常量此前定义在 ``adapters/user_environment.py``（写用户 ``.mcp.json`` 的那一侧）。
``apps/worker/config.py`` 的只读工具白名单装配期断言需要同一个值来核对前缀（Issue #291
根因 #1：两侧一旦不同步，用户的每一次真实工具调用都会在 ``PreToolUse`` 被无声拒绝），
此前只能 ``from lingxi.adapters.user_environment import QUERY_MCP_SERVER_NAME``。但
``adapters/user_environment.py`` 顶部 import 了 ``core/identity/onboarding_runner.py``
（首次开通编排，传递拉入身份匹配、花名册、银河、建档等约十二个模块）——``apps/worker/
config.py`` 只是想要一个字符串常量，却因此把整条开通编排链一起拖进了 worker 的 import
闭包。worker 是处理每一次真实用户提问的热路径进程，这条闭包与它的职责毫无关系，只会
拉长启动时间、扩大它的依赖面、让"改一行开通编排代码"意外触发 worker 的重新验证范围。
把常量单独放进这个零依赖模块之后，``adapters/user_environment.py`` 与 ``apps/worker/
config.py`` 各自 import 它，互不牵连对方原有的依赖闭包。
"""

from __future__ import annotations

#: 写进每个用户 ``.mcp.json`` 的 MCP 服务名——单一事实来源。
#: ``adapters/user_environment.py`` 用它作为写入配置的服务名段；``apps/worker/config.py``
#: 的只读工具白名单要求每一项都以 ``mcp__{QUERY_MCP_SERVER_NAME}__`` 开头，并在装配期
#: （``load_config``）断言两者一致。2026-08-21 的真实事故正是两侧各自维护一份字符串、
#: 悄悄分叉的后果——白名单前缀写的是遗留全进程配置的服务名 ``bi-metric``，这里早已改成
#: ``query``，两处没人同步，用户的每一次真实工具调用因此在 ``PreToolUse`` 被无声拒绝。
#: 改这个值必须同时确认 worker 白名单的装配期断言仍然通过，不能只改一处。
QUERY_MCP_SERVER_NAME = "query"
