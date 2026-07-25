# 全员 Codex 默认 Agent 接口设计

> 日期：2026-07-11
> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。
> 使用边界：下文仅保留历史接口设计，不是当前生产部署、验收或排障指引。
> 关联 PRD：`codex-default-agent-prd.md`

## 1. Agent Profile 接口

```python
resolve_agent_profile(provider: str, model: str, reasoning_effort: str) -> AgentProfile
```

成功：返回固定 `cli_path`、模型和 reasoning。失败：抛稳定的 `unknown_provider/unsupported_model/unsupported_reasoning`，不做 fallback。

```python
default_agent_profile() -> AgentProfile
```

必须返回 `codex/gpt-5.6-terra/medium`。

## 2. 用户配置服务

```python
get_user_agent_config(db, *, user_ref: str) -> UserAgentConfig
```

`user_ref` 支持现有管理员命令已支持的邮箱/Linux 用户名精确解析；多匹配必须要求管理员选择，禁止模糊命中后直接修改。

```python
update_user_agent_config(
    db,
    *,
    user_id: int,
    provider: str,
    model: str,
    reasoning_effort: str,
    actor_open_id: str,
    source_chat_id: str,
    expected_config_version: int,
    auth_context: AdminAuthContext,
) -> AgentConfigChangeResult
```

原子行为：

1. service 校验不可伪造的 `AdminAuthContext`（审批群 ID、管理员 open_id、事件来源/签名验证结果）；入口检查只是快速拒绝，不是唯一授权边界。
2. 校验完整 profile。
3. `BEGIN IMMEDIATE` 后重新检查目标用户没有 running run。
4. 重新读取当前配置；SQLite busy 直接失败。
5. 以 `WHERE config_version=:expected` CAS 更新并递增版本；0 行视为并发冲突。
6. 关闭 active session，`closed_reason=config_changed`。
7. 写 applied audit 并 commit。

成功事务包含配置、session 和 applied audit，任一步失败全部 rollback。非法输入、running run、busy/CAS 冲突在 rollback 后用独立短事务写 rejected audit；拒绝审计写失败则返回 `audit_failed` 并保持配置不变。run admission 使用同一事务协议，避免检查后新 run 插入。

```python
reset_user_agent_config(...) -> AgentConfigChangeResult
```

等价于更新为系统默认 profile。

## 3. 审批群中文命令

沿用现有 `admin_command_flow`：

```text
@Bot 查看Agent 用户=wangzp
@Bot 调整Agent 用户=wangzp agent=codex 模型=gpt-5.6-terra 推理=medium
@Bot 调整Agent 用户=wangzp agent=claude 模型=deepseek-v4-pro[1m] 推理=high
@Bot 恢复Agent默认 用户=wangzp
```

成功回复示例：

```text
已更新用户 wangzp 的默认 Agent
原配置：claude / deepseek-v4-pro[1m] / high
新配置：codex / gpt-5.6-terra / medium
配置版本：7 → 8
会话：旧 active session 已关闭；下一条消息创建新会话
```

失败回复必须说明业务原因，例如“该模型不支持 xhigh”“用户当前有请求运行中”；不得返回 traceback、token、auth 状态明文或完整文件路径。

## 4. Runner Factory

```python
build_runner(*, user: CodexChatUser, session: CodexChatSession | None) -> RunnerPort
```

- 新 session：使用当前用户配置。
- resume：使用 session 快照；快照与当前 config version 不同则返回 `new_session_required`。
- Codex runner 不直接执行 binary；它只调用固定 managed launcher，并把 username/model/reasoning/config version/session/prompt 作为严格 JSON stdin envelope。launcher 再从 DB 复核 profile，并显式传 `-m <model>` 和 `-c model_reasoning_effort="<effort>"`；resume 只能使用明确 session id，禁止 `--last`。
- Claude runner 显式传 provider 对应 model 与 `--effort`；同时保留当前 CLI 已验证的 `CLAUDE_CODE_EFFORT_LEVEL` 子进程环境映射，二者必须一致。受控认证文件只允许静态的 URL/token export（可含空行和整行注释），拒绝 `source`、变量展开及额外键；runner 解析一次后通过目标用户 helper 的 stdin 传入最小环境，凭据不进入 argv，provider 输出在解析和持久化前脱敏。若目标 Claude CLI/模型不支持请求档位则切换前拒绝，不静默忽略。
- 目标业务 OS 用户、home、cwd 和 `CODEX_HOME` 必须由 launcher 确定，不能从飞书文本传入。

## 5. Codex Launcher

内部命令接口：

```text
bi-agent-codex-run --user-id <validated-internal-id> --prompt-stdin
```

launcher 不接受透传的任意 Codex 参数。模型、reasoning、session id 和工作目录通过结构化受控输入传入并再次命中 allowlist；用户 prompt 只走 stdin。launcher：

1. 从内部 user id 查询固定 Linux 用户映射，不由输入拼接用户名或路径。
2. 校验目标业务用户已批准、Linux 账号存在且未禁用。
3. 校验该用户 auth、`config.toml`、`.mcp.json` 的 owner/mode；拒绝 symlink、路径穿越和检查后替换。
4. 以目标用户读取本人 `.mcp.json`，解析 Bearer token并设置当次 `BI_MCP_BEARER_TOKEN`。
5. 从空白环境构造最小 allowlist，固定 `HOME/USER/LOGNAME/CODEX_HOME`；显式固定 shell 环境敏感变量 scrub 策略。
6. 拒绝所有 CLI `-c/--config/--cd/--add-dir/sandbox/approval/MCP/provider/CODEX_HOME` 覆盖，将 supplementary groups 设置为配置批准的精确集合；其中包含现有 BI Plus 文件发送 socket 所需组。
7. 校验固定 binary 和路径 owner/mode，并应用 Codex sandbox/受控工作目录。
8. 使用目标业务用户 uid/gid 执行固定 Codex binary。
9. stdout 只返回 Codex JSONL；所有输出路径执行结构化脱敏，regex 仅作末道防线。

高级工作台另提供当前用户模式：

```text
bi-agent-codex <普通 Codex CLI 参数>
```

登录 profile 可把 `codex` 入口指向该 wrapper。它不 sudo、不接受目标用户名，只以当前 uid 读取当前 home 的 `.mcp.json`、注入 `BI_MCP_BEARER_TOKEN` 后执行固定真实 Codex binary；允许用户正常使用交互式 CLI 参数。飞书 managed 模式与用户交互模式必须是两个明确代码路径，不能让飞书调用任意参数模式。

返回码：

| code | 含义 |
|---:|---|
| 0 | 完成 |
| 20 | 用户环境缺失 |
| 21 | 统一 auth 不可用 |
| 22 | MCP 配置/token 缺失 |
| 23 | 配置权限不安全 |
| 24 | 用户已禁用 |
| 25 | auth generation 不可用或未安装 |
| 26 | 全局 Codex run fuse 已关闭 |

## 6. Codex Runner 结果

```python
AgentRunResult(
    status: Literal["completed", "failed", "timeout"],
    session_id: str,
    final_text: str,
    token_usage: TokenUsage | None,
    mcp_call_count: int,
    failure_code: str | None,
)
```

`TokenUsage` 至少支持 `input_tokens/cached_input_tokens/output_tokens/reasoning_output_tokens`。`reasoning_output_tokens` 是 output 的细分，不重复计费。

两个 provider 返回同一结果字段集合，并把实际 provider、usage、MCP call count 和 failure code 保存到 run。稳定失败分类至少包含 `auth_unavailable`、`mcp_unavailable`、`timeout`、`cli_unavailable`、`cli_exit_error`、`heartbeat_error`；失败不得自动改用另一个 Agent。Claude 的 reasoning 必须通过已验证的 `CLAUDE_CODE_EFFORT_LEVEL` 子进程环境显式传入，不支持的档位在启动 CLI 前拒绝。

## 7. 开户接口扩展

```python
render_codex_config(*, mcp_url: str, profile: AgentProfile) -> str
render_agents_md(global_md: str, country_md: str, business_md: str) -> str
```

纯函数不接收 token。

```python
setup_codex(home_dir, uid, gid, *, profile, auth_source) -> CodexSetupResult
```

由 root I/O 层调用，为业务用户创建独立 `CODEX_HOME`、配置、统一 auth 副本和工作区 `AGENTS.md`。任何步骤失败返回稳定 step/reason；不打印文件内容。

自动开户复用现有 `create_user.py` root 入口的 `--install-codex-auth` 模式，在同一 root 进程内调用 `AuthGenerationStore.rollout_user`；不为 Bot 新增第二条任意 auth-sync sudo 能力。该调用受 manifest 锁保护且只安装 `current` generation，generation 非空前 provisioning 不得返回成功。

`create_user.py --restore` 增加 Codex 派生文件恢复，但默认不覆盖管理员数据库 profile；渲染时使用数据库当前 profile 或显式参数，不能无条件恢复系统默认。

## 8. 存量 Reconcile CLI

```text
sudo .venv/bin/python scripts/reconcile_codex_users.py --dry-run
sudo .venv/bin/python scripts/reconcile_codex_users.py --username wangzp --apply
sudo .venv/bin/python scripts/reconcile_codex_users.py --all-approved --apply --json
sudo .venv/bin/python scripts/reconcile_codex_users.py --all-approved --apply --approve-provider-changes --json
```

默认必须是 `--dry-run`；`--apply` 才允许写。输出：

```json
{
  "batch_id": "...",
  "summary": {"ready": 10, "skipped": 2, "failed": 1},
  "users": [
    {"username": "wangzp", "status": "ready", "checks": ["auth", "config", "mcp", "db"]}
  ]
}
```

输出不得包含 token、header、auth JSON、环境变量值或 session id。

## 9. Auth 同步 CLI

```text
sudo .venv/bin/python scripts/sync_codex_auth.py --check
sudo .venv/bin/python scripts/sync_codex_auth.py --username wangzp --apply
sudo .venv/bin/python scripts/sync_codex_auth.py --all-approved --apply
```

`--check` 只报告权威副本 generation、固定 Codex 版本、权限、canary 状态和用户副本是否一致。`--apply` 必须先通过 canary；保留 previous generation，失败时可原子回滚并触发全局熔断。哈希仅用于本机比较，不在飞书展示。

manifest 操作接口必须覆盖 `stage-candidate/canary/promote/rollout/rollback/retire`。每个用户记录 installed generation；partial rollout 和崩溃可恢复，新开户只取 current。launcher 每次执行检查 run fuse、current/previous 允许状态和用户 installed generation。

## 10. Smoke 接口

```text
sudo .venv/bin/python scripts/check_codex_user.py --username wangzp --static
sudo .venv/bin/python scripts/check_codex_user.py --username wangzp --mcp-list-metrics
```

`--static` 不联网；`--mcp-list-metrics` 只读调用 MCP，输出连接状态和可见 metric key，不输出 token。Codex 扩批只要求我方对目标用户执行本人 token、授权内成功和典型越权拒绝的最小回归，不要求 MCP 团队新增确认。分类：`ready/auth_failed/mcp_auth_failed/mcp_permission_denied/tool_unavailable/timeout`。

## 11. 飞书私聊路由接口

现有 `handle_feishu_private_message` 对外形状保持不变。内部先取得 active 用户和有效 Agent 配置，再构造 runner。配置无效时返回明确配置错误，不再像当前未知 provider 那样静默降级为 Codex。

## 12. 幂等与并发

- `message_id` 继续作为消息幂等键。
- reconcile 对同一用户重复执行结果一致。
- Agent 配置更新和 run admission 使用 `BEGIN IMMEDIATE + expected config_version CAS`；明确处理 busy、CAS conflict 和事务崩溃。
- running run 期间拒绝配置变更，并发创建 run 不得越过事务检查。
- auth 同步使用原子 rename，避免 Codex 读到半文件。

DB 并发实现要求：使用新 Session/Connection，`BEGIN IMMEDIATE` 必须是首条语句，显式 busy timeout；两个独立连接测试 run admission 与配置更新竞争。拒绝审计最多重试固定次数，仍 busy 时返回管理员可见 `audit_failed`。

## 13. 禁用与回收接口

```python
revoke_user_agent_access(db, *, user_id: int, reason: str, auth_context: AdminAuthContext) -> RevocationResult
```

顺序为 entitlement/run fuse → 终止并关闭 run/session → Linux 登录锁定 → 删除服务器 auth 副本 → MCP disabled 发布 → 审计。部分失败写补偿状态并由巡检重试。疑似共享 auth 泄露调用全局 generation rotate，而不是只删除单用户文件。

## 14. 全局控制接口

三个控制面互相独立：`CODEX_RUN_FUSE`、`NEW_USER_DEFAULT_AGENT`、`CODEX_EXISTING_ROLLOUT`。现有 `CODEX_CHAT_ENABLED` 不表示回切 Claude。审批群/运维 CLI 必须能查询状态、关闭 run fuse、把新用户默认恢复 Claude、对存量用户执行受审计批量回滚。

另设只用于数据库禁用失败等紧急场景的 `AGENT_ROUTE_FUSE`，它同时阻断 Claude/Codex 新请求。该 fuse 必须在状态接口可见，只能由显式 `close-route/open-route` 管理操作改变；修改其他三个控制面不得隐式重开它。每次显式关闭或恢复必须审计操作者、稳定 reason code 和旧值→新值；reason code 仅接受 `operator_action`、`incident_detected`、`incident_resolved`、`security_incident`、`rollback` 枚举值，其他输入统一审计为 `redacted`，避免误存凭证。审计提交失败时不得返回成功，并应恢复原值，恢复失败则返回路由状态不确定的错误供人工处置。
