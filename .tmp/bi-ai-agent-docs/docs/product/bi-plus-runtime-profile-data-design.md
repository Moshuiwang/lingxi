# BI Plus 运行环境切换数据设计

## 1. 数据域

本功能不新增业务域表；新增安全技术表 `admin_bootstrap_challenges`。隔离单位是 SQLite 数据库文件：正式与测试各自拥有完整 schema、用户、管理员、审批、权限、会话和审计数据。

| profile | 数据库文件 | 初始数据 |
|---|---|---|
| `bi-plus-production` | `/var/lib/bi-ai-agent/app.db` | 保留现有生产数据 |
| `bi-plus-test-bot` | `/var/lib/bi-ai-agent/profiles/bi-plus-test-bot/app.db` | 新建空 schema，不复制生产数据 |

## 2. 管理员事实源

管理员身份使用当前数据库的 `codex_chat_users`：

- `feishu_open_id`：当前飞书应用下观察到的用户身份；
- `entitlement_status`：必须是 `active`；
- `role`：必须是 `owner` 或 `admin`。

环境变量 `LARK_ADMIN_OPEN_IDS` 被移除，不参与授权。生产库与测试库可以有不同管理员 open_id。

## 3. 初始化

测试库由应用 schema 初始化能力创建；初始化工具只接受已经存在的固定测试库，父目录与数据库分别为 `bi-ai-service:bi-ai-service 0700/0600`，且都不可是 symlink。SQLite 写事务始终以 `bi-ai-service` 的精确有效 UID/GID 和 supplementary groups 执行，root 不创建数据库或 sidecar。初始化完成时管理员为空是合法状态；这意味着测试机器人尚无管理能力，而不是回退到生产管理员。

首次管理员使用数据库内的一次性 bootstrap challenge：已验证私聊事件写入候选 open_id、challenge hash、过期时间和消费状态；明文仅回复该用户。root 工具从 stdin 接收 challenge，并通过非敏感参数指定一个已由脚本验证存在的 Linux 用户名。事务内验证 profile、时效与未消费状态后，同步把候选提升为 active owner，并写入 `entitlement_username`、`execution_username`、当前默认 Claude `provider/model/reasoning_effort/cli_path`、固定 `cwd` 和递增后的 `config_version`。明文 challenge 和 open_id 不进入 shell 历史、进程参数、普通日志或 env profile。

Linux 运行身份绑定只写当前测试库的 `codex_chat_users`，不读取生产数据库。它不复制聊天、session、MCP token、BI 权限或审计数据，也不创建/修改 Linux 用户。事务内必须确认没有另一条 `CodexChatUser`（包括 pending/disabled）占用同一 `execution_username` 或 `entitlement_username`；冲突返回 `runtime_identity_already_bound`。username、Home、workspace、默认 Agent profile、冲突或其他业务校验失败时，整个事务不开始或回滚，challenge 保持未消费。若 rollback 或底层 commit 返回后无法确认事务状态，结果必须是 `bootstrap_state_unknown`，由只读回读判定，不能自动重试。

`admin_bootstrap_challenges` 字段为：自增 `id`、`profile_key`、`candidate_open_id`、全局唯一 `challenge_hash`、`expires_at`、可空 `consumed_at`、`created_at`。表只保存 hash，不保存明文。challenge 过期后不可消费；首位 owner 建立成功时，同一 profile 的所有其他未消费 challenge 在同一事务中标记为已消费，之后不再保留可用初始化入口。过期/已消费记录保留作最小审计证据，后续清理由独立数据保留策略负责，不在切换流程中自动删除。

## 4. 不同步原则

- 不自动同步用户、管理员、授权、token、对话或审计。
- 不以相同姓名、邮箱或生产 open_id 推断测试身份。
- 若未来需要测试数据，应通过独立、脱敏、可审计的 fixture 流程，不扩展本功能。

## 5. 完整性与备份

- 切换到目标前运行 SQLite integrity check 和 schema/version 校验。
- 离开生产环境前遵循既有 SQLite backup 门禁。
- 测试数据库备份与生产备份必须位于可区分的 profile 目录，禁止覆盖。
- 版本账本检查必须指向当前 profile 的固定数据库，不再硬编码生产路径。
- 未激活测试库在预置阶段由当前 release 的 `check_app_version.py --claim-version` 绑定精确 VERSION 与 commit；错误 commit 或版本复用必须拒绝。claim 保持既有 `claimed` 状态，首次真实激活健康通过后再按既有部署语义标记。
