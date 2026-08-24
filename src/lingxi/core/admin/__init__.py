"""管理能力：服务端管理员角色登记表判定、命令解析与命令面路由。

[Issue #95](https://github.com/Moshuiwang/lingxi/issues/95) S-M-01（2026-08-24 范围重定）
交付的确定性命令面——不是管理 MCP（管理 MCP 身份认证与绑定退场为未来入口，见
[决策记录](../../../docs/决策记录/2026-08-24-管理员职责集与银河外权限动作边界.md)）。

纯逻辑，不做网络或数据库 I/O：外部数据由调用方通过 ``router.py`` 声明的 ``Protocol``
注入（代码框架第二节「core/ 不 import adapters/」）。真实的 PostgreSQL 判定与查询实现
住在 ``adapters/admin_registry.py``。
"""

from __future__ import annotations
