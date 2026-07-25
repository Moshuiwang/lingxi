# 选择性生产用户迁移接口设计

CLI 接受：来源 SQLite snapshot、生产目标 SQLite、私有用户名清单、新式用户目录根、HTTPS MCP endpoint、authorization ID 和 dry-run/execute 模式。

dry-run 只生成脱敏计划并验证来源；execute 仅可调用固定 root helper。helper 验证有效的 `user_migration` authorization ID，并验证 endpoint 等于 root-owned endpoint 文件；先创建 nologin 身份，再写 token 配置，最后启用聊天状态。外部 provisioner 不接收 SSH 公钥或任意命令。

输出是结构化脱敏结果：计划人数、已验证人数、已迁移人数、失败码和是否可切流。任何失败返回非零退出码。
