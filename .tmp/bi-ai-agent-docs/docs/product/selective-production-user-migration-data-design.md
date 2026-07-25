# 选择性生产用户迁移数据设计

每个迁移用户以 Linux username 为选择键，关联 `users`、唯一 `mcp_tokens`、`codex_chat_users` 及 `drive_folder_token`。MCP Token 仅是连接身份，安全写入生产 `.mcp.json`；不迁 `user_functions`、`user_grants` 或任何权限发布状态。

目标库重新分配本地主键，保持用户、连接 token 与 Agent 配置关联一致。chat 相关历史、审批和审计不写入目标库。

迁移报告只包含总人数、用户名哈希或本地私有清单中的状态码，以及 PASS/FAIL；不得输出 token、open_id、邮件、聊天内容或权限明细。
