# 全员 Codex 默认 Agent 数据设计

> 日期：2026-07-11
> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。
> 使用边界：下文仅保留历史数据设计，不是当前生产部署、验收或排障指引。
> 关联 PRD：`codex-default-agent-prd.md`

## 1. 数据原则

- 数据库只保存 Agent 配置与审计元数据，不保存 ChatGPT auth 或 MCP token 明文。
- 当前默认配置与 session 实际快照分开。
- 管理员修改必须单事务完成配置更新、版本递增、session 关闭和审计落库。
- SQLite 兼容迁移复用 `_ensure_sqlite_schema()`；新表由 ORM `create_all` 创建。

## 2. `codex_chat_users` 扩展

保留现有 `provider/cli_path/cwd`，新增：

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `model` | varchar(64) | NOT NULL | 飞书默认模型，默认 `gpt-5.6-terra` |
| `reasoning_effort` | varchar(16) | NOT NULL | 默认 `medium` |
| `config_version` | integer | NOT NULL, default 1 | 每次管理员有效变更 +1 |
| `config_source` | varchar(32) | NOT NULL | `system_default/admin_override/reconciled/needs_review` |
| `config_updated_by` | varchar(64) | nullable | 管理员 open_id；系统写入为空 |
| `config_updated_at` | datetime | NOT NULL | 最近配置更新时间 |

约束由 service 层和数据库兼容检查共同保证：

- `provider in ('codex','claude')`；
- Codex 第一期开启 `reasoning_effort in ('medium','high','xhigh')`；
- provider/model/reasoning 必须命中同一个受控 profile；
- `cli_path` 不能由飞书命令修改。

存量迁移必须按旧 `provider` 回填 provider 对应 model/reasoning：Claude 行回填当前 Claude 默认，Codex 行回填 Terra/Medium。无法证明来源的旧行保持原 provider、标记 `needs_review`，在人工确认前不得自动改为 Codex。所有 provider 变化必须先出现在 reconcile dry-run 并由 `--approve-provider-changes` 显式批准。

## 3. `codex_chat_sessions` 扩展

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `reasoning_effort` | varchar(16) | nullable | session 创建时快照 |
| `config_version` | integer | NOT NULL, default 1 | 对应用户配置版本 |
| `closed_reason` | varchar(32) | nullable | `config_changed/user_new/failed/admin_reset` |

已有 `provider/model/cwd/cli_path` 继续作为快照。恢复规则：仅当 session 为 active，且 provider/model/reasoning/config_version 全部匹配当前目标配置时允许 resume。

## 4. `codex_chat_runs` 扩展

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `provider` | varchar(32) | 历史兼容 nullable；新 run 必填 | 实际运行 Agent |
| `model` | varchar(64) | 历史兼容 nullable；新 run 必填 | 实际模型 |
| `reasoning_effort` | varchar(16) | 历史兼容 nullable；新 run 必填 | 实际推理深度 |
| `config_version` | integer | 历史兼容 nullable；新 run 必填 | 实际配置版本 |
| `input_tokens` | integer | nullable | provider 报告总输入 |
| `cached_input_tokens` | integer | nullable | 缓存输入；Claude 映射 cache read |
| `output_tokens` | integer | nullable | 总输出，包含 provider 定义的 reasoning 子集 |
| `reasoning_output_tokens` | integer | nullable | Codex 可用时记录的细分 |
| `mcp_call_count` | integer | NOT NULL default 0 | MCP 工具调用次数 |
| `failure_code` | varchar(64) | nullable | 稳定错误分类，不存秘密 |

token 字段用于体验、成本和故障分析；不同 provider 口径不同，跨 provider 比较时必须带 provider/model，不只比较 token 数量。

## 5. 新表 `agent_config_audits`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer PK | 主键 |
| `user_id` | integer FK | 目标用户 |
| `actor_open_id` | varchar(64) | 操作者 |
| `source_chat_id` | varchar(128) | 审批群 chat_id |
| `old_provider` | varchar(32) | 原 Agent |
| `old_model` | varchar(64) | 原模型 |
| `old_reasoning_effort` | varchar(16) | 原推理深度 |
| `new_provider` | varchar(32) | 新 Agent |
| `new_model` | varchar(64) | 新模型 |
| `new_reasoning_effort` | varchar(16) | 新推理深度 |
| `old_config_version` | integer | 原版本 |
| `new_config_version` | integer | 新版本 |
| `status` | varchar(16) | `applied/rejected` |
| `reason_code` | varchar(64) | `ok/invalid_model/run_in_progress/...` |
| `created_at` | datetime | 时间 |

拒绝的管理操作也记录审计，但不得保存用户输入中的未知环境变量、token 或完整命令行。成功路径使用一个 `BEGIN IMMEDIATE` 事务提交配置、session 和 applied audit；拒绝路径在成功回滚后使用独立短事务写 rejected audit。若任一审计写入失败，管理操作 fail closed，不得应用配置。

## 6. 新表 `codex_provision_runs`

记录存量 reconcile、新用户开户和 auth 同步的脱敏结果：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer PK | 主键 |
| `batch_id` | varchar(64) | 一次批次 |
| `user_id` | integer nullable | 目标用户 |
| `operation` | varchar(32) | `new_user/reconcile/auth_sync/restore/smoke` |
| `status` | varchar(16) | `running/succeeded/skipped/failed` |
| `step` | varchar(64) | 失败/完成步骤 |
| `reason_code` | varchar(64) | 稳定原因码 |
| `details_json` | text | 仅允许脱敏字段，如版本、路径是否存在、文件 mode |
| `started_at/finished_at` | datetime | 时间 |

`details_json` 明确禁止 auth/token/header/env 值。

## 7. 非数据库配置

| 数据 | 位置 | 权限/说明 |
|---|---|---|
| ChatGPT auth 权威副本 | `/home/biai-agent/.codex-auth/auth.json` | root-only |
| 用户 auth 副本 | `~/.codex/auth.json` | `440 root:<user-group>`；用户可读/可通过父目录替换的风险已接受 |
| Codex 本地配置 | `~/.codex/config.toml` | 用户本地环境；飞书 runner 不信任，使用 DB profile + 受控启动参数 |
| Auth generation manifest | `/home/biai-agent/.codex-auth/manifest.json` | root-only；candidate/current/previous/用户 installed generation，不含 token |
| Codex 业务上下文 | `~/bi-agent-work/AGENTS.md` | 受控模板派生；权限策略由实现阶段验证 |
| MCP token | `~/.mcp.json` | 现有 `440 root:<user-group>`，不复制到 Codex 配置 |
| 模型 allowlist | `config/agent_profiles.yaml` | 无秘密，进入 Git |

## 8. 配置 Profile

建议 `config/agent_profiles.yaml`：

```yaml
defaults:
  provider: codex
  model: gpt-5.6-terra
  reasoning_effort: medium
providers:
  codex:
    cli_path: /usr/local/lib/bi-agent-audit/codex/codex-0.144.1
    models:
      gpt-5.6-terra: [medium, high, xhigh]
      gpt-5.6-sol: [medium, high, xhigh]
      gpt-5.6-luna: [medium, high, xhigh]
  claude:
    cli_path: /usr/bin/claude
    models:
      deepseek-v4-pro[1m]: [medium, high, xhigh, max]
```

文件是批准列表，不是模型自动发现缓存。升级模型必须先验证 CLI 可用性，再通过 PR 更新。

## 9. 迁移与回填

1. 加 nullable/带 default 列。
2. 为所有行回填 `config_version=1` 和时间。
3. 按旧 provider 回填兼容 profile；未知来源标 `needs_review`，不自动改 provider。
4. 扩展 session/run 快照列。
5. 创建审计与 provision run 表。
6. 关闭 provider/profile 不兼容的 legacy active session；启动自检拒绝 active 用户的空或不兼容 provider/model/reasoning/cwd/cli_path。

迁移执行必须有 schema version/ledger：维护窗口先备份 SQLite、运行 `integrity_check`，记录每步开始/完成；中断后可判定重试或从备份恢复。部署包需要说明新 schema 对旧二进制是否兼容；不兼容时禁止代码回滚而不回滚 DB。

迁移可重复运行。任何一行非法都必须被列为失败，不允许运行时静默降级到 Codex 或 Claude。

## 10. 数据保留与隐私

- 配置审计长期保留，遵循现有基础操作日志策略。
- provision 运行明细建议保留 90 天。
- ChatGPT/MCP token 永不进入这些表。
- 飞书 open_id 和用户名只用于现有业务身份映射，不新增外部共享。
