# GitHub Issue AI 施工 Agent 数据设计

> 版本：0.1
> 日期：2026-06-14
> 状态：数据方案初稿
> 首个接入项目：BIAIAgent

---

## 一、设计目标

数据层要记录从 issue 被扫描到 PR 合并、部署验收的完整过程。它服务三个目标：

- 产品经理能知道每个 issue 当前卡在哪。
- 系统能避免重复施工和并发冲突。
- 出问题时能回溯是谁批准、AI 做了什么、结果是什么。

第一版可以使用 SQLite。后续多项目、多团队、多审批人后再迁移到 PostgreSQL。

第一版 MVP 要支持一次性完整闭环，但用数据状态限制真实动作：

- dry-run 只记录扫描和分诊摘要，不产生 GitHub/飞书外部写入。
- write smoke 必须记录指定 issue、外部写入结果和审批卡 message_id。
- build run 必须记录 worktree、branch、Codex 命令摘要、测试结果和 PR。
- merge run 必须记录审批时的 head_sha、合并前校验结果和 merge commit。
- deploy run 第一版只镜像现有 CD 结果，不由 Issue Agent 自己发起生产部署。

---

## 二、核心实体

```text
agent_projects
agent_issues
agent_runs
agent_triage_results
agent_approvals
agent_pull_requests
agent_test_runs
agent_review_runs
agent_deploy_runs
agent_audit_logs
```

---

## 三、表设计

### 3.1 agent_projects

记录接入项目。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| project_key | text | 项目唯一标识，如 `biai-agent` |
| repo_owner | text | GitHub owner |
| repo_name | text | GitHub repo |
| default_branch | text | 默认分支 |
| local_repo_path | text | EC2 本地仓库路径 |
| branch_prefix | text | AI 分支前缀 |
| schedule_minutes | integer | 扫描周期，第一版默认 10 |
| enabled | boolean | 是否启用 |
| config_json | text | 项目 adapter 配置 JSON |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

`config_json` 保存项目差异配置，例如测试命令、高风险标签、审批人、部署方式。

### 3.2 agent_issues

记录 GitHub issue 的镜像状态。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| project_id | integer | 所属项目 |
| github_issue_id | integer | GitHub issue ID |
| issue_number | integer | GitHub issue number |
| title | text | 标题快照 |
| html_url | text | 链接 |
| state | text | open / closed |
| labels_json | text | 标签快照 |
| last_seen_at | datetime | 最近扫描时间 |
| last_processed_at | datetime | 最近处理时间 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

唯一约束：

```text
project_id + issue_number
```

### 3.3 agent_runs

一次 issue 自动处理流程。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| project_id | integer | 项目 |
| issue_id | integer | issue |
| run_type | text | 第一版固定为 issue_lifecycle；后续可拆 triage / build / merge / deploy 子 run |
| status | text | 当前状态 |
| lock_key | text | 并发锁 |
| base_branch | text | 基准分支 |
| work_branch | text | 工作分支 |
| worktree_path | text | 临时工作区路径 |
| started_at | datetime | 开始时间 |
| finished_at | datetime | 结束时间 |
| error_code | text | 失败码 |
| error_message | text | 失败说明 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

第一版使用一条 issue lifecycle run 贯穿分诊、施工、PR、合并和部署镜像。`status` 表达当前阶段，`lock_key` 从施工审批发起到合并或失败期间保持，防止重复施工。后续多项目平台化时，可以再拆成 phase 子 run。

常用状态：

- `DISCOVERED`
- `TRIAGING`
- `BLOCKED`
- `WAITING_FOR_AI_READY_APPROVAL`
- `READY`
- `BUILDING`
- `REVIEWING`
- `PR_READY`
- `WAITING_FOR_MERGE_APPROVAL`
- `MERGE_CHECKING`
- `MERGED`
- `DEPLOYED`
- `VERIFIED`
- `BUILD_FAILED`
- `PR_CREATE_FAILED`
- `MERGE_BLOCKED`
- `DEPLOY_FAILED`
- `SMOKE_FAILED`
- `FAILED`
- `CANCELLED`

状态口径：

- `READY` 只表示允许施工，不表示已经开始改代码。
- `BUILDING` 表示 Codex 或本地施工正在运行。
- `PR_READY` 表示 PR 已创建，但还没有合并授权。
- `WAITING_FOR_MERGE_APPROVAL` 表示产品经理需要看飞书 PR 合并审批卡。
- `MERGED` 表示代码已合并，后续由现有 CD 继续处理部署确认。
- 任何失败状态都必须释放 `lock_key`，否则下一轮无法补偿或人工处理。

### 3.4 agent_triage_results

记录分诊结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| run_id | integer | 所属 run |
| issue_type | text | bug / feature / docs / ops / security / unknown |
| user_goal | text | 用户目标摘要 |
| acceptance_clear | boolean | 验收标准是否清楚 |
| risk_level | text | low / medium / high / blocked |
| required_labels_json | text | 建议需要的前置标签 |
| missing_labels_json | text | 当前缺少的前置标签 |
| blocked_reason | text | 阻断原因 |
| ai_recommendation | text | AI 建议 |
| github_comment_id | integer | 自动评论 ID |
| feishu_message_id | text | 飞书卡片消息 ID |
| created_at | datetime | 创建时间 |

### 3.5 agent_approvals

记录飞书审批动作。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| run_id | integer | 所属 run |
| approval_type | text | ai_ready / merge / deploy |
| status | text | pending / approved / rejected / paused / expired |
| approver_open_id | text | 审批人 open_id |
| approver_name | text | 审批人名称快照 |
| feishu_message_id | text | 卡片消息 ID |
| feishu_action_id | text | 按钮 action ID |
| approved_head_sha | text | 批准合并时卡片记录的 PR head commit |
| decision_note | text | 审批留言 |
| requested_at | datetime | 发起时间 |
| decided_at | datetime | 决定时间 |
| created_at | datetime | 创建时间 |

第一版规则：

- `ai_ready` 和 `merge` 必须由产品经理批准。
- `deploy` 不记录已停用 CD 卡片回调；替代发布入口确定后再定义部署结果镜像。

### 3.6 agent_pull_requests

记录 AI 创建的 PR。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| run_id | integer | 所属 run |
| github_pr_id | integer | GitHub PR ID |
| pr_number | integer | PR number |
| title | text | 标题 |
| html_url | text | 链接 |
| head_branch | text | 源分支 |
| head_sha | text | 当前 PR head commit |
| base_branch | text | 目标分支 |
| status | text | draft / open / merged / closed |
| merge_commit_sha | text | 合并 commit |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

### 3.7 agent_test_runs

记录测试结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| run_id | integer | 所属 run |
| command | text | 测试命令 |
| status | text | passed / failed / skipped |
| summary | text | 摘要 |
| log_path | text | 本地日志路径 |
| started_at | datetime | 开始时间 |
| finished_at | datetime | 结束时间 |

日志中不得包含密钥、token 或生产敏感数据。

第一版至少要记录：

- 命令是否真的执行。
- 成功/失败/跳过。
- 给产品经理看的摘要。
- 本地日志路径。

如果测试因为缺环境跳过，`status` 必须是 `skipped`，摘要写明原因，不能记为 `passed`。

### 3.8 agent_review_runs

记录 AI review。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| run_id | integer | 所属 run |
| reviewer | text | codex / claude / other |
| model | text | 模型名称或别名 |
| depth | text | review 深度 |
| target | text | docs / diff / files |
| status | text | passed / findings / failed / skipped |
| report_path | text | review 报告路径 |
| summary | text | 摘要 |
| created_at | datetime | 创建时间 |

如果 reviewer 不可用，必须记录 `skipped` 或 `failed`，不能记录为 `passed`。

Issue Agent 自身施工流程的 review 规则：

- 文档包 review：Claude Opus + xhigh，至少 1 次、最多 2 次。
- 代码 diff review：Claude Opus + xhigh，至少 1 次、最多 2 次。
- 如果 Claude review 发现问题，执行 agent 可以修复确认有效的问题。
- 如果 Claude review 不可用或连续失败，应改用 Codex / ChatGPT 5.5 + xhigh 做同等范围 review；文档和 PR 统一称为 `gpt-5.5`。
- review 结论必须作为假设处理，最终是否修复由执行 agent 判断。

### 3.9 agent_deploy_runs

记录部署和 smoke 结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| run_id | integer | 所属 run |
| deploy_system | text | CD 系统 |
| status | text | pending / approved / deployed / failed |
| deploy_ref | text | 部署版本或 commit |
| smoke_status | text | passed / failed / skipped |
| smoke_summary | text | smoke 摘要 |
| started_at | datetime | 开始时间 |
| finished_at | datetime | 结束时间 |

第一版部署记录不替代现有 CD 系统。它只记录：

- PR 合并后是否进入现有 CD 飞书确认。
- 产品经理是否已在 CD 卡片批准部署。
- 部署结果和 smoke 摘要。

Issue Agent 不直接写生产部署命令。

PR 合并后如果暂时没有 CD 结果，run 可以停留在 `MERGED`，但必须记录最近一次等待提醒时间；超过配置阈值仍无 CD 结果时，系统要补充 GitHub/飞书提醒，避免静默停滞。

### 3.10 agent_audit_logs

记录关键事件。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | integer | 主键 |
| project_id | integer | 项目 |
| issue_id | integer | issue |
| run_id | integer | run |
| event_type | text | 事件类型 |
| actor_type | text | ai / human / system |
| actor_id | text | actor 标识 |
| summary | text | 简短说明 |
| payload_json | text | 结构化详情 |
| created_at | datetime | 创建时间 |

---

## 四、关键约束

### 4.1 并发约束

同一个 issue 只能有一个 active run：

```text
unique active lock: project_id + issue_number
```

如果已有 active run，下一轮扫描只能刷新状态，不能重复施工。

### 4.2 审批约束

第一版只允许配置中的产品经理审批：

```text
approver_open_id in project.config.allowed_approvers
```

不在名单内的按钮点击只更新卡片提示，不改变状态。

### 4.3 标签约束

`ai-ready` 只能由审批动作产生。AI 分诊不能直接写入最终门禁。

### 4.4 敏感数据约束

以下内容不得进入数据库明文：

- GitHub token
- 飞书 app secret
- AI API key
- 生产数据库凭证
- MCP token
- SSH 私钥

数据库只保存引用、状态、摘要和脱敏后的日志路径。

### 4.5 用户可见状态约束

每个非终态 run 都必须能回答：

- 当前卡在哪。
- 是否需要产品经理点击按钮。
- 点按钮后会发生什么。
- 失败后谁处理。

如果 run 进入失败、暂停、退回或人工处理状态，必须至少在 GitHub issue 或飞书卡片之一留下用户可见说明。正式 `--write` 模式下两边都要尽量同步。

---

## 五、BIAIAgent 初始配置数据

建议配置：

```json
{
  "project_key": "biai-agent",
  "default_branch": "main",
  "schedule_minutes": 10,
  "branch_prefix": "codex/",
  "allowed_approvers": ["<product_manager_open_id>"],
  "precondition_labels": [
    "product-ok",
    "design-ok",
    "tech-ok",
    "data-ok",
    "security-ok",
    "ops-ok"
  ],
  "gate_labels": [
    "ai-ready",
    "ai-blocked",
    "ai-in-progress",
    "ai-pr-ready",
    "ai-approved-to-merge",
    "ai-done",
    "ai-failed",
    "ai-paused",
    "ai-needs-human"
  ],
  "high_risk_terms": [
    "飞书",
    "MCP",
    "权限",
    "密钥",
    "生产数据",
    "数据库",
    "部署",
    "开户"
  ]
}
```

真实 open_id、token、secret 不写入文档和 git。

### 5.1 多项目复用约束

当前表结构虽然落在 BIAIAgent 数据库里，但字段必须保持多项目语义：

- 所有 issue、run、审批和 PR 记录都必须归属到 `project_key` 对应的项目。
- 不在表结构里写死 BIAIAgent 的 repo、审批人、测试命令或部署方式。
- BIAIAgent 的默认高风险词只作为首个项目配置，不作为全局默认规则。
- `allowed_approvers` 第一版只有产品经理一个人，后续必须能扩展到多角色。
- GitHub token、飞书 secret、AI provider token 只从环境变量或受保护配置读取，不进入数据库和日志。

第二个项目接入前，应增加项目配置加载机制，让 adapter/config 成为事实源，而不是依赖脚本参数散落传入。

### 5.2 Issue #46 初始运行记录

issue #46 首次真实闭环试跑时，建议形成以下记录：

| 阶段 | 记录 |
|------|------|
| dry-run | `agent_runs` 记录扫描和分诊摘要，不写 GitHub/飞书 |
| write-smoke | `agent_triage_results.github_comment_id`、`agent_approvals.feishu_message_id` |
| ai-ready | `agent_approvals.status=approved`，run 进入 `READY` |
| build | `work_branch=codex/issue-46-*`，`worktree_path=/tmp/issue-agent/biai-agent/46` |
| test/review | `agent_test_runs`、`agent_review_runs` |
| PR | `agent_pull_requests.status=open/merged/closed`，记录 head_sha |
| merge approval | `agent_approvals.approval_type=merge`，记录 approved_head_sha |
| merged/deploy | `agent_pull_requests.merge_commit_sha`，`agent_deploy_runs` 镜像现有 CD 状态 |
