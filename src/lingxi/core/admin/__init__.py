"""管理能力：服务端管理员角色登记表判定、命令解析与命令面路由。

交付的是确定性命令面，不是管理 MCP（管理 MCP 身份认证与绑定退场为未来入口，
取舍见[决策记录目录](../../../docs/决策记录/README.md)）。

纯逻辑，不做网络或数据库 I/O：外部数据由调用方通过 ``router.py`` 声明的 ``Protocol``
注入（代码框架第二节「core/ 不 import adapters/」）。真实的 PostgreSQL 判定与查询实现
住在 ``adapters/admin_registry.py``。
"""

from __future__ import annotations
