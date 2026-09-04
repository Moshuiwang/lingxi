"""问数 MCP 服务名：全仓库唯一一份。

放在 ``core/`` 的公共小模块里是[代码框架「三、横切约定」]
(../../../docs/技术设计/代码框架.md)的要求：这类横切事实只允许存在一份，各模块不自造，
``core/`` 结构性地不能 import ``adapters/``/``apps/``/外部 SDK，从构造上保证它零依赖。

## 为什么单独成一个模块，不留在 ``adapters/user_environment.py``

``adapters/user_environment.py``（写用户 ``.mcp.json`` 的那一侧）顶部 import 了
首次开通编排链，传递拉入约十二个模块；``apps/worker/config.py`` 只需要这一个
字符串常量核对只读工具白名单前缀，却会因此把整条编排链拖进 worker 这条热路径
的 import 闭包。单独放进零依赖模块后，两侧各自 import 它，互不牵连。
"""

from __future__ import annotations

#: 写进每个用户 ``.mcp.json`` 的 MCP 服务名——单一事实来源，``adapters/
#: user_environment.py`` 用它写入配置段，``apps/worker/config.py`` 的只读
#: 工具白名单要求每一项都以 ``mcp__{QUERY_MCP_SERVER_NAME}__`` 开头并在装配期
#: 断言两者一致——两侧各自维护一份字符串会悄悄分叉，导致真实工具调用被无声
#: 拒绝。改这个值必须同时确认 worker 白名单的装配期断言仍然通过。
QUERY_MCP_SERVER_NAME = "query"
