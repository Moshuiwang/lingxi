# BI Plus 运行环境切换接口设计

## 1. 环境变量合同

新增必填：

```text
BI_AI_RUNTIME_PROFILE=bi-plus-production|bi-plus-test-bot
```

`DATABASE_URL` 必须与 profile 固定映射匹配。机器人 App ID 的 SHA-256 必须与 `/etc/bi-ai-agent/profile-bindings.json` 中该 profile 的独立指纹匹配。机器人 App Secret、审批群、云盘和多维表格等应用维度配置仍属于各自 profile。禁止配置 `LARK_ADMIN_OPEN_IDS`。

profile 文件只允许以下严格语法：每个非注释行必须是单行 `UPPER_SNAKE_CASE=value`；值不得包含空白、引号、`#`、反斜杠、控制字符；不接受 `export`、续行、行尾注释或重复 key。注释必须整行以 `#` 开头。

## 2. 健康接口

`GET /health` 成功响应增加：

```json
{
  "status": "ok",
  "service": "bi-ai-agent",
  "version": "<VERSION>",
  "runtime_profile": "bi-plus-production",
  "runtime_label": "PRODUCTION"
}
```

测试环境返回 `bi-plus-test-bot` / `TEST-BOT`。接口不返回路径、App ID、chat ID、open_id 或任何配置值。

## 3. 进程启动日志

三个生产入口启动时记录同一条非敏感事实：`runtime_profile=<key> runtime_label=<label>`。profile 缺失、未知或数据库不匹配时进程在连接数据库或调用飞书前退出，错误只包含稳定 reason code。

## 4. 运维命令

受控 helper 接口：

```text
bi-ai-environment status
bi-ai-environment preflight <profile>
bi-ai-environment switch <authorization-id> <profile>
```

- `status` 只输出当前 key/label及 PASS/FAIL。
- `preflight` 只输出检查项和稳定错误码，不输出配置值。
- `switch` 只接受两个固定 profile，且必须有短时效 `environment_switch`、`service_stop`、`service_restart` 授权。

## 5. 稳定结果码

至少覆盖：`unknown_profile`、`profile_metadata_invalid`、`profile_syntax_invalid`、`profile_missing_key`、`profile_duplicate_key`、`profile_forbidden_key`、`database_mapping_mismatch`、`database_integrity_failed`、`switch_busy`、`profile_requires_release_1_25`、`target_health_failed_rolled_back`、`rollback_failed_services_stopped`、`rollback_failed_state_unknown`。

## 6. 管理员接口

所有管理入口和卡片回调调用数据库权限服务 `is_active_admin(db, open_id)`；不再接收 env 白名单参数。审批群 chat ID 只能限制来源，不能直接授权。

首次测试管理员的 root 工具接口为：

```text
bootstrap_test_admin.py --linux-username <existing-user>
```

工具必须以 root 运行，从标准输入接收一次性 challenge，不接收 open_id 参数。Linux 用户名是非敏感显式参数，只接受 `[a-z][a-z0-9_-]{2,31}` 且拒绝保护账户；passwd UID 必须非零，Home 必须精确为 `/home/biai-agent/users/<username>`。脚本通过目录 FD + `O_NOFOLLOW` 打开 Home 与 `bi-agent-work`，两者必须由该 UID 拥有、是 `0700` 的真实目录，并在 consume/flush 后、数据库 commit 前复核同一 inode。固定测试数据库必须已经存在；父目录与文件分别为 `bi-ai-service:bi-ai-service 0700/0600` 的真实目录/普通文件。SQLite 使用 `mode=rw`，完整事务与关闭过程均在 `bi-ai-service` 的精确有效 UID/GID 和 supplementary groups 下执行。challenge 由测试机器人在已验证私聊事件中创建，数据库只保存 hash；工具只输出稳定结果码。

成功返回 `owner_activated`。至少稳定区分：`challenge_invalid`、`challenge_expired`、`wrong_profile`、`active_admin_exists`、`linux_identity_invalid`、`database_identity_invalid`、`runtime_identity_required`、`runtime_identity_already_bound`、`bootstrap_failed_rolled_back`、`bootstrap_state_unknown`。Linux 或数据库身份校验失败必须在 commit 前 rollback，不得留下部分 owner；rollback 或 commit 结果无法确认时统一返回 `bootstrap_state_unknown`，后续必须只读回读，不得自动重试。
