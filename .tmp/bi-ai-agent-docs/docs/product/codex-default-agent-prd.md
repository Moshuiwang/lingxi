# 全员 Codex 默认 Agent PRD

> 日期：2026-07-11
> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。
> 使用边界：下文仅保留历史产品决策，不是当前生产部署、验收或排障指引。
> Owner：X1 用户接入体验 / M6 运维与开户
> 关联：`codex-default-agent-data-design.md`、`codex-default-agent-interface-design.md`、`../architecture/codex-default-agent-architecture.md`

## 1. 背景与问题

当前飞书私聊运行时已经支持按用户选择 `codex` 或 `claude`，但开户默认仍是 Claude，Codex 仅在少量账号上手工配置。Codex 的统一 ChatGPT 认证、逐用户 MCP 注入、全量存量用户补齐、默认模型和审批群管理接口尚未形成正式产品能力。

公司只有一个可用于 Codex 的 ChatGPT 账号，因此本方案采用“统一认证、逐用户运行与授权隔离”：所有用户共享同一个 ChatGPT 身份，但不得共享 Codex 会话、工作目录、MCP token 或用户级配置。

## 2. 产品目标

1. 所有当前已批准用户可被幂等补齐 Codex；未来批准的新用户自动具备 Codex。
2. 飞书私聊默认使用 `Codex + gpt-5.6-terra + medium`。
3. Codex 默认连接 `bi-metric` MCP，并且每个用户只使用自己的 MCP token。
4. Claude 开户已有的适用能力在 Codex 侧得到等价覆盖。
5. 审批群管理员可按用户调整默认 Agent、模型和推理深度，所有变更可验证、可审计、可回滚。
6. 保留 Claude 作为可选 Agent，不强制删除现有 Claude 环境或历史会话。

## 3. 非目标

- 不采购或创建第二个 ChatGPT 账号。
- 不把 MCP token、ChatGPT token 写入 SQLite、Git、日志、飞书消息或用户可编辑配置。
- 不让普通用户自行修改飞书默认 Agent、模型或推理深度。
- 不开放任意 CLI 路径、任意模型字符串或任意环境变量注入。
- 不在本阶段改变 MCP 服务端权限模型，也不宣称已完成正式多租户隔离验收。
- 不迁移或合并 Claude 与 Codex 的历史 session；切换 Agent 后新建 session。
- 不在文档阶段实现代码、部署或修改线上用户。

## 4. 用户与角色

| 角色 | 能力 |
|---|---|
| 已批准普通用户 | 默认通过飞书使用 Codex；MCP 查询受本人 token 权限限制 |
| 高级工作台用户 | 飞书默认使用 Codex，也可在自己的 Linux 工作目录直接使用 Codex CLI 或现有 Claude |
| 审批群管理员 | 查询和调整指定用户的 Agent、模型、推理深度；查看脱敏验证结果 |
| 系统运维 | 安装/升级 Codex、维护统一 ChatGPT 认证、执行存量 reconcile、处理认证轮换 |

管理员身份沿用现有审批群管理员判定，不新建第二套管理员名单。

但现有“只要来自审批群/测试群就视为管理员”的宽松判定不能用于本接口。实现必须落到明确管理员 open_id allowlist/角色事实源，并同时校验事件来自正式审批群；测试群成员不自动获得生产配置修改权。

## 5. 核心产品规则

### 5.1 默认配置

| 配置 | 默认值 |
|---|---|
| Agent | `codex` |
| Model | `gpt-5.6-terra` |
| Reasoning effort | `medium` |
| MCP | `bi-metric`，默认启用 |
| 工作目录 | `/home/biai-agent/users/<username>/bi-agent-work` |
| 执行身份 | 对应用户的 Linux 账号 |

默认值必须由应用配置与数据库显式落值共同约束，不能依赖 CLI 当时的隐式默认。

### 5.2 统一 Auth 与用户隔离

- ChatGPT 认证只有一个受控权威副本，由运维账号维护。
- 统一认证以受控副本投放到每个业务用户独立的 `CODEX_HOME`；禁止多人共享完整 `CODEX_HOME`、state 或 session。
- Codex 进程必须以目标业务用户 OS 身份运行，home、cwd、`CODEX_HOME` 和 session 相互独立；不能仅靠 `cwd` 模拟隔离。
- 认证轮换必须可批量同步和脱敏验证；单用户失败不得泄露凭据或影响其他用户配置。

已接受风险：拥有高级工作台登录权限的内部用户技术上可以读取并复制自己收到的共享 `auth.json`。产品确认当前公司内部阶段优先保证直接 CLI 使用体验，接受这一风险，不作为本期 blocker。控制措施是限定内部已审批用户、独立用户目录、禁止把 auth 写入 Git/DB/日志、统一轮换/全局吊销、离职禁用时删除用户副本并锁定账号。未来若风险等级提高，再迁移到不可导出凭据代理或个人认证；该升级不改变每用户 MCP token 和会话隔离原则。

### 5.3 每用户 MCP

- `.mcp.json` 继续是用户 MCP URL 与 token 的受控事实载体，保持 `root:<用户主组> 440`。
- Codex `config.toml` 只保存 MCP URL 和 `bearer_token_env_var` 名称，不保存 token 明文。
- 受控 Codex launcher 在目标用户进程内读取其 `.mcp.json`，只把该 token 注入当次子进程环境。
- 高级工作台直接使用 Codex 时，通过用户侧 `bi-agent-codex` wrapper（登录环境中提供 `codex` 入口）读取自己的 `.mcp.json` 并注入同一环境变量；不得要求用户手工 export token。
- 不允许从其他用户目录复制 token，不允许使用统一 MCP token。
- MCP 缺失、token 无效或权限拒绝必须明确失败；不得退回无认证 MCP 或管理员 token。

### 5.4 飞书默认路由

- 已批准且 `codex_chat_users.entitlement_status=active` 的私聊消息进入其配置的默认 Agent。
- 新批准用户默认配置为 Codex/Terra/Medium。
- 现有用户经 reconcile 后默认迁移为 Codex/Terra/Medium；管理员明确配置为 Claude 的用户不被后续 reconcile 覆盖。
- 群聊不新增普通问答入口；审批群继续只处理受控管理命令。
- `/new`、飞书话题隔离、流式卡片、完成态、结果 guardrail 和文件交付继续复用现有私聊能力。

### 5.5 管理员调整

审批群提供以下受控操作：

1. 查询用户当前 Agent 配置。
2. 设置 `agent=codex|claude`。
3. 设置与 Agent 匹配的模型。
4. 设置与 Agent 匹配的推理深度。
5. 恢复系统默认 `codex/gpt-5.6-terra/medium`。

变更规则：

- 必须一次校验完整目标配置，任何字段非法则整次不落库。
- Codex 模型和推理深度基于已批准 allowlist；初始至少允许本机已验证的 Terra/Sol/Luna 与其支持档位。
- `low`、`max`、`ultra` 不作为系统默认，但管理员是否可选由 allowlist 明确决定；第一期管理接口只开放 `medium/high/xhigh`。
- Claude 模型使用 Claude provider 自己的 allowlist，不接受 Codex 模型名。
- 配置成功后归档该用户所有 active Agent session；下一条消息使用新配置创建新 session。
- 回复管理员“原值 → 新值、操作者、验证状态”，不显示 token、CLI auth 或内部 session id。

## 6. Claude 能力对齐矩阵

| Claude 当前开户/运行能力 | Codex 处理 | 说明 |
|---|---|---|
| Linux 用户、主组、`terminal-only`、独立 home | 复用 | Codex 必须以该 OS 用户执行 |
| `bi-agent-work` 与登录后自动进入 | 复用 | 统一作为 CLI/飞书工作目录 |
| 三层 `CLAUDE.md` 上下文 | 等价生成 `AGENTS.md` | 两者由同一模板语义源派生，避免内容漂移 |
| `/etc/claude-code/managed-settings.json` 企业规则 | 以受控 Agent 指令模板 + root 管理配置实现 | 不假设 Codex 支持 Claude 专用键 |
| `~/.claude/settings.json` 模型配置 | `~/.codex/config.toml` | 显式写 Terra/Medium、MCP server 和安全配置 |
| DeepSeek shell auth | 不适用 | 仅 Claude provider 使用；Codex 使用统一 ChatGPT auth |
| `.mcp.json` per-user token | 复用事实源 | Codex launcher 转为 bearer env，不复制 token |
| `enabledMcpjsonServers` allowlist | Codex MCP 固定注册 `bi-metric` | 未批准的 MCP 不进入受控飞书运行时 |
| BI Plus Claude Plugin 安装 | 不直接移植 | 只要求 Codex 具备等价 BI 上下文、MCP 查询与文件交付；未来有 Codex Plugin 时另立项 |
| `create_user.py --restore` | 扩展为同时恢复 Codex 派生配置 | 不碰 MCP token、SSH、历史 session |
| Claude+MCP smoke | 增加 Codex+MCP smoke | 验证连接、身份和授权范围，不打印 token |
| 用户可改 Claude 模型 | 飞书默认配置仅管理员可改 | 高级工作台本地 CLI 个性化不改变飞书默认配置 |

## 7. 关键用户流程

### 7.1 新用户审批通过

1. 现有审批流签发用户 MCP token。
2. 开户创建 Linux 用户、工作目录、Claude 环境和 `.mcp.json`。
3. 同一开户事务继续安装 Codex CLI 用户入口、独立 `CODEX_HOME`、统一 auth 副本、`AGENTS.md` 和 `config.toml`。
4. 激活 `codex_chat_users`，显式写入 Codex/Terra/Medium。
5. 执行脱敏静态检查；MCP 权限发布后执行只读 smoke。
6. 成功后飞书私聊默认由 Codex 回答；任何关键步骤失败则标记待修复，不谎报开通完成。

### 7.2 存量用户补齐

1. 枚举数据库中已批准且未禁用的用户，而不是只枚举 home 目录。
2. 预检 Linux 用户、home、`.mcp.json`、MCP token 记录和审批状态。
3. 幂等创建/修复 Codex 配置并同步统一 auth。
4. 对没有管理员显式覆盖的用户写入默认 Codex/Terra/Medium。
5. 按用户输出脱敏结果：`ready/skipped/failed` 与原因。
6. 任一用户失败不回滚其他已成功用户，但批次最终状态为 partial failure。

### 7.3 管理员切换 Agent

1. 管理员在审批群查询用户。
2. 系统展示当前配置与允许选项。
3. 管理员提交目标 Agent、模型、推理深度。
4. 系统校验 CLI、模型组合、用户环境和 MCP 配置。
5. 单事务更新配置、递增版本、归档 active session、写审计。
6. 下一条飞书消息创建新 Agent session。

## 8. 失败与降级原则

| 场景 | 行为 |
|---|---|
| 统一 ChatGPT auth 缺失/过期 | 全局熔断 Codex 新 run；通知审批群，不自动切 Claude |
| 用户 MCP token 缺失 | 禁止发起 BI 查询；返回配置异常并通知管理员 |
| Codex CLI 缺失或版本不符 | 该用户标记失败；不宣称完成 |
| 模型/推理组合不支持 | 管理操作拒绝，保留旧配置 |
| Agent 配置变更时仍有运行中请求 | 拒绝变更或等待请求结束；第一期采用拒绝并提示重试 |
| Codex 执行失败 | 保留会话证据并给用户明确失败；不静默切换 Claude |
| MCP 权限拒绝 | 作为正确安全结果呈现，不用管理员 token 重试 |
| 存量批处理部分失败 | 成功用户保留，失败用户列入重试清单，批次不标全成功 |
| 用户禁用/离职 | 先停飞书 Agent 和运行中会话，再锁登录、删服务器 auth 副本、禁 MCP；部分失败进入补偿重试 |
| 疑似共享 auth 泄露 | 全局熔断 Codex，新建 auth generation，canary 后分批轮换；不能只删单用户文件 |

## 9. 验收标准

1. 随机选择至少一个存量用户和一个新建测试用户，飞书首问均由 Codex/Terra/Medium 处理。
2. 两名用户同时查询 MCP 时，服务端观察到各自 token 身份，授权结果不串用户。
3. Codex 进程的 OS uid、home、cwd、`CODEX_HOME` 均属于目标业务用户，不以 Bot 管理员身份运行。
4. 所有用户共享同一 ChatGPT 账号，但 Codex session/state 不共享。
5. 高级工作台用户直接执行 `codex` 时无需再次登录或手工配置 MCP，且只能使用自己的 token。
6. 管理员能把单个用户切换到 Claude，再切回 Codex；每次切换后的首问创建新 session。
7. 非管理员在审批群执行调整命令被拒绝且不产生配置变更。
8. 非法模型、非法 reasoning、未知 Agent、运行中切换均失败且旧配置保持不变。
9. 日志、SQLite、飞书消息和测试输出中均不出现 ChatGPT token 或 MCP token。
10. `create_user --restore` 能恢复 Codex 派生配置但不改 auth、MCP token 和 SSH。
11. Codex+MCP smoke 能区分连接成功、权限拒绝、认证失败和工具不可用。

## 10. 发布闸门

- 代码必须走 PR 合入 `main`，无密钥测试 CI 通过后人工合并；不启用自动合并或自动部署。
- 先测试账号、再 `wangzp`、再小批存量用户、最后全量。
- 必须保留 Claude 回切能力和逐用户回滚命令。
- 本项目不要求 MCP 团队为 Codex 接入新增开发、确认或签字；当前 MCP 能力作为既有可用依赖。任何仍使用该历史路径的验证都必须确认每个用户携带自己的 token、典型授权内查询成功、典型越权查询被拒，并保留证据。MCP 门禁的风险接受口径遵守 `../mcp/mcp-permission-boundary.md`，不伪造未执行回归结果。
