# GitHub Issue AI 施工 Agent 接口设计

> 版本：0.1
> 日期：2026-06-14
> 状态：接口方案初稿
> 首个接入项目：BIAIAgent

---

## 一、接口范围

本文定义 Issue AI Agent 与外部系统和项目 adapter 的接口。

外部系统：

- GitHub
- 飞书
- AI 执行器
- 项目本地仓库
- CI/CD 系统

第一版不提供面向普通用户的 Web UI。产品经理主要通过飞书卡片交互。

---

## 二、调度入口

### 2.1 定时扫描命令

```bash
issue-agent scan --project biai-agent
```

本仓库第一版入口：

```bash
.venv/bin/python scripts/run_issue_agent.py scan --project biai-agent
```

行为：

1. 读取项目配置。
2. 拉取 GitHub open issues。
3. 同步 issue 镜像。
4. 对未处理或状态变化的 issue 启动分诊。
5. 对 `ai-ready` issue 启动施工。

第一版默认 dry-run，只做分诊和本地记录。任何会写 GitHub 评论、标签或发送飞书施工审批卡的命令都必须指定 issue，避免全量 open issue 被批量打扰。

推荐扩展入口：

```bash
.venv/bin/python scripts/run_issue_agent.py scan --project biai-agent
.venv/bin/python scripts/run_issue_agent.py write-smoke --project biai-agent --issue 46
.venv/bin/python scripts/run_issue_agent.py build-ready --project biai-agent --limit 1
.venv/bin/python scripts/run_issue_agent.py process-issue --project biai-agent --issue 46 --write
```

| 命令 | 用户可见结果 | 安全边界 |
|------|--------------|----------|
| `scan` | 无 GitHub/飞书可见结果，只输出扫描摘要和本地记录 | 不调用 Codex，不改代码，不发审批卡 |
| `write-smoke` | 指定 issue 上出现 GitHub 评论，产品经理收到飞书施工审批卡 | 只能处理 `--issue` 指定 issue |
| `build-ready` | 对已审批 `ai-ready` issue 创建 ready PR 和 PR 合并审批卡 | 默认 `--limit 1`，一次只施工一个 issue |
| `process-issue` | 用于人工补跑单个 issue 的分诊或施工 | 仍遵守标签、审批和锁 |
| `merge-approved` | 审批回调或补偿命令触发合并检查 | 不部署，等待人工受控发布 |

`--project` 是 canonical selector。项目 adapter 负责解析 repo、默认分支、本地仓库路径和审批人；第一版可以从环境变量初始化 adapter，但 CLI 不再把 `--repo` 作为主入口。

timer 入口：

```bash
.venv/bin/python scripts/run_issue_agent.py tick --project biai-agent
```

`tick` 读取 `ISSUE_AGENT_TIMER_MODE`：

- `observe`：只执行 `scan` dry-run。
- `active`：执行 `scan` dry-run，然后执行 `build-ready --limit 1`。

`active` 不会发全量施工审批卡；它只领取已经被产品经理批准并同时满足 `ai-ready` 标签和本地审批记录的 issue。

退出码：

| 退出码 | 含义 |
|--------|------|
| 0 | 扫描完成 |
| 1 | 配置错误 |
| 2 | GitHub 访问失败 |
| 3 | 飞书访问失败 |
| 4 | 分诊或 review AI 执行失败 |
| 5 | 本地仓库状态异常 |
| 6 | 施工阶段 Codex 执行失败 |
| 7 | PR 创建失败 |
| 8 | 合并门禁失败 |
| 9 | 部署结果失败 |
| 10 | smoke 失败 |

失败映射：

| run 状态 | error_code | 退出码 |
|----------|------------|--------|
| `BUILD_FAILED` | `CODEX_FAILED` / `TEST_FAILED` | 6 |
| `PR_CREATE_FAILED` | `PR_CREATE_FAILED` | 7 |
| `MERGE_BLOCKED` | `MERGE_CI_NOT_GREEN` / `MERGE_CONFLICT` / `PR_HEAD_CHANGED` | 8 |
| `DEPLOY_FAILED` | `DEPLOY_FAILED` | 9 |
| `SMOKE_FAILED` | `SMOKE_FAILED` | 10 |

### 2.2 处理单个 issue

```bash
issue-agent process-issue --project biai-agent --issue 123
```

用于人工补跑或排查。它仍然遵守标签、审批和锁规则。

### 2.3 EC2 systemd 入口

`issue-agent.service` 第一版为 oneshot：

```ini
[Service]
Type=oneshot
User=wangzhipeng
WorkingDirectory=/home/wangzhipeng/projects/bi-ai-agent
Environment=HOME=/home/wangzhipeng
Environment=USER=wangzhipeng
Environment=LOGNAME=wangzhipeng
EnvironmentFile=/home/wangzhipeng/.config/biai-agent/issue-agent.env
Environment=ISSUE_AGENT_TIMER_MODE=observe
ExecStart=/home/wangzhipeng/projects/bi-ai-agent/.venv/bin/python scripts/run_issue_agent.py tick --project biai-agent
```

敏感环境变量只放 environment file，不写入 git：

- `GITHUB_TOKEN`
- `ISSUE_AGENT_REPO`
- `ISSUE_AGENT_APPROVER_OPEN_IDS`
- 飞书 app 配置
- AI provider token

timer 每 10 分钟触发，默认 `observe`，只用于观察扫描范围。替代发布流程完成上线后，才允许把 `ISSUE_AGENT_TIMER_MODE` 改为 `active`；旧部署审批已停用。

`issue-agent.env` 必须归 `wangzhipeng` 所有，权限 `0600`。需要做指定 issue 写入验证时，不改 timer，人工执行 `write-smoke --project biai-agent --issue 46`。

active 施工最长可运行 60 分钟；systemd oneshot 运行期间不会并发启动下一次 tick，长施工期间跳过的扫描属于预期。

---

## 三、GitHub 接口

### 3.1 读取 issue

输入：

```json
{
  "owner": "repo-owner",
  "repo": "repo-name",
  "state": "open",
  "labels": []
}
```

输出：

```json
{
  "issues": [
    {
      "id": 1,
      "number": 123,
      "title": "issue title",
      "body": "issue body",
      "labels": ["product-ok"],
      "html_url": "https://github.com/org/repo/issues/123",
      "updated_at": "2026-06-14T10:00:00Z"
    }
  ]
}
```

### 3.2 评论 issue

用途：

- 分诊后说明缺什么。
- 施工开始时声明认领。
- PR 创建后回填链接。
- 失败时说明原因。

评论口径：

```markdown
AI 分诊结果：
- 类型：bug / feature / docs / ops
- 当前结论：可以施工 / 缺信息 / 不建议自动施工
- 缺少条件：product-ok, tech-ok
- 下一步：等待产品经理在飞书确认
```

### 3.3 打标签

允许写入：

- `ai-blocked`
- `ai-in-progress`
- `ai-pr-ready`
- `ai-approved-to-merge`
- `ai-done`
- `ai-failed`
- `ai-paused`
- `ai-needs-human`
- `ai-ready`

约束：

- `ai-ready` 只能由飞书 `允许施工` 回调触发。
- `ai-approved-to-merge` 只能由飞书 `批准合并` 回调触发。

### 3.4 创建 PR

PR 标题：

```text
[AI] <issue title>
```

PR 正文必须包含：

```markdown
## 用户体验变化

## 改动范围

## 测试结果

## 风险和边界

## 关联 Issue

Closes #123
```

默认直接创建 ready PR，但仍需飞书 PR 合并审批才可合并。只有需求不清、风险异常或需要人工补充说明时，才允许降级为 draft。

### 3.5 合并 PR

触发条件：

- 飞书 `批准合并` 已通过。
- PR open。
- PR 当前 head commit 与审批卡片记录的 `head_sha` 一致。
- CI 通过。
- 分支保护满足。
- 没有 merge conflict。

合并策略由项目配置决定：

- squash
- merge commit
- rebase

BIAIAgent 第一版建议使用当前仓库既有策略，不在 Agent 中硬编码。

---

## 四、飞书接口

### 4.1 分诊审批卡片

触发：AI 认为 issue 信息足够，建议进入施工。

卡片字段：

```json
{
  "type": "triage_approval",
  "project_key": "biai-agent",
  "issue_number": 123,
  "issue_title": "issue title",
  "issue_url": "https://github.com/org/repo/issues/123",
  "issue_type": "feature",
  "required_labels": ["product-ok", "tech-ok"],
  "missing_labels": [],
  "risk_level": "medium",
  "recommendation": "建议进入 AI 施工"
}
```

按钮：

| 按钮 | action |
|------|--------|
| 允许施工 | `approve_ai_ready` |
| 退回补充信息 | `request_more_info` |
| 暂停 | `pause_issue` |
| 查看 Issue | link |

### 4.2 PR 合并审批卡片

触发：AI 创建 PR 并完成测试/review。

卡片字段：

```json
{
  "type": "merge_approval",
  "project_key": "biai-agent",
  "issue_number": 123,
  "pr_number": 45,
  "pr_url": "https://github.com/org/repo/pull/45",
  "head_sha": "abc123",
  "user_visible_change": "用户会看到...",
  "test_summary": "pytest passed",
  "risk_summary": "不涉及权限/MCP/生产数据",
  "recommendation": "建议批准合并"
}
```

按钮：

| 按钮 | action |
|------|--------|
| 批准合并 | `approve_merge` |
| 退回修改 | `request_changes` |
| 暂停 | `pause_issue` |
| 查看 PR | link |

### 4.3 卡片回调

输入：

```json
{
  "action": "approve_merge",
  "project_key": "biai-agent",
  "issue_number": 123,
  "run_id": 789,
  "head_sha": "abc123",
  "user": {
    "open_id": "ou_xxx",
    "name": "王志鹏"
  },
  "note": "同意合并"
}
```

处理规则：

1. 校验 `open_id` 是否在允许审批人列表。
2. 校验 run 状态是否匹配当前 action。
3. 对 `approve_merge` 校验回调 `head_sha` 与当前 PR head commit 一致。
4. 写入审批记录。
5. 更新 GitHub 标签。
6. 更新卡片状态。
7. 异步触发下一步。

非授权用户点击按钮时：

- 不改变 run 状态。
- 卡片或 toast 提示“当前账号无审批权限”。
- 写 audit log。

---

## 五、AI 执行器接口

### 5.1 分诊输入

```json
{
  "project": {
    "project_key": "biai-agent",
    "rules": {}
  },
  "issue": {
    "number": 123,
    "title": "issue title",
    "body": "issue body",
    "comments": [],
    "labels": []
  },
  "repo_context": {
    "docs_index": "docs/README.md",
    "project": "https://github.com/orgs/startimes-bi/projects/2",
    "risk_rules": ["MCP", "权限", "部署"]
  }
}
```

### 5.2 分诊输出

```json
{
  "issue_type": "feature",
  "user_goal": "用户想要...",
  "acceptance_clear": true,
  "risk_level": "medium",
  "required_labels": ["product-ok", "tech-ok"],
  "missing_labels": [],
  "recommendation": "request_ai_ready_approval",
  "github_comment": "AI 分诊结果...",
  "feishu_summary": "建议进入施工..."
}
```

输出必须是结构化 JSON。不能只返回自然语言。

### 5.3 施工输入

```json
{
  "project_key": "biai-agent",
  "issue_number": 123,
  "branch": "codex/issue-123-title",
  "worktree_path": "/tmp/issue-agent/biai-agent/123",
  "triage_result": {},
  "approval": {
    "type": "ai_ready",
    "approved_by": "ou_xxx",
    "approved_at": "2026-06-14T10:00:00Z"
  }
}
```

### 5.4 施工输出

```json
{
  "status": "pr_ready",
  "branch": "codex/issue-123-title",
  "commit_sha": "abc123",
  "pr_number": 45,
  "test_summary": "pytest passed",
  "review_summary": "no blocking findings",
  "user_visible_change": "用户会看到...",
  "risk_summary": "不涉及权限/MCP/生产数据"
}
```

### 5.5 Codex 执行命令

施工 Agent 调用 Codex 时必须带上受控提示：

```bash
codex exec -C /tmp/issue-agent/biai-agent/46 --sandbox workspace-write -m <model> -c 'model_reasoning_effort="xhigh"' '<prompt>'
```

prompt 必须包含：

- 先读 `AGENTS.md` 和 Issue Agent 文档。
- 只处理当前 issue。
- 不处理其它 issue、评论或顺手发现的问题。
- 不扩展到 issue 未明确要求的重构、架构调整、体验改造或清理工作。
- 不读取、打印、提交 `.env`、token、secret。
- 不 push main。
- 按 TDD 写测试和最少代码。
- 不提交、不 push、不创建 PR；外层 Issue Agent 负责统一提交和创建 ready PR。
- 完成后输出用户体验变化、测试结果、风险边界、PR 正文草稿。

Codex 执行失败时：

- run 进入 `BUILD_FAILED` 或 `FAILED`。
- GitHub issue 评论失败摘要。
- 飞书卡片更新为需要人工处理。
- 不创建 PR；如果已创建 PR，必须在 PR/issue 上说明当前失败状态。

---

## 六、项目 Adapter 接口

每个项目提供配置，而不是修改平台核心。

示例：

```yaml
project_key: biai-agent
repo:
  owner: example
  name: BIAIAgent
  default_branch: main
workspace:
  local_repo_path: /home/wangzhipeng/projects/bi-ai-agent
  branch_prefix: codex/
policy:
  allowed_approvers:
    - "<product_manager_open_id>"
  high_risk_terms:
    - MCP
    - 权限
    - 密钥
    - 生产数据
    - 数据库
    - 部署
commands:
  test:
    - "pytest"
docs:
  index: docs/README.md
  project: https://github.com/orgs/startimes-bi/projects/2
merge:
  require_feishu_approval: true
deploy:
  mode: existing_cd_feishu_card
```

第一版的 BIAIAgent adapter 可以先由脚本参数和环境变量提供。第二个项目接入前，需要收口成项目配置文件或配置表，避免每个项目复制脚本。

Adapter 必须回答用户体验层面的四个问题：

- 这个项目的 issue 满足什么条件才允许 AI 施工。
- AI 改完后跑哪些测试，结果如何展示给产品经理。
- 合并后由谁确认部署，确认入口在哪里。
- 上线后用什么 smoke 口径告诉产品经理“现在用户能看到什么”。

### 6.1 BIAIAgent 第一版 adapter

BIAIAgent adapter 第一版至少提供：

```yaml
commands:
  test:
    - ".venv/bin/python -m pytest tests/test_issue_agent.py"
  smoke:
    - ".venv/bin/python scripts/run_issue_agent.py scan --project biai-agent"
build:
  codex_model: "gpt-5.5"
  reasoning_effort: "xhigh"
  worktree_root: "/tmp/issue-agent/biai-agent"
  max_concurrent_builds: 1
pull_request:
  draft: false
  title_prefix: "[AI]"
merge:
  require_feishu_approval: true
  method: "squash"
deploy:
  mode: existing_cd_feishu_card
```

实际测试命令可以随 issue 范围收窄，但 PR 和飞书卡片必须说明跑了哪些测试、哪些没有跑。

并发口径：

- `lock_key` 是 issue 级并发锁，防止同一个 issue 重复施工。
- `max_concurrent_builds` 是项目级全局施工上限。
- `build-ready --limit 1` 是单次命令最多领取几个 `READY` issue；BIAIAgent 第一版固定为 1。

---

## 七、错误接口

统一错误结构：

```json
{
  "error_code": "MERGE_CI_NOT_GREEN",
  "message": "CI 未通过，不能合并",
  "recoverable": true,
  "next_action": "等待 CI 通过或人工处理"
}
```

常见错误码：

| 错误码 | 含义 |
|--------|------|
| `ISSUE_NOT_READY` | issue 不满足施工条件 |
| `APPROVER_NOT_ALLOWED` | 点击人没有审批权限 |
| `WORKTREE_DIRTY` | 工作区不干净 |
| `TEST_FAILED` | 测试失败 |
| `REVIEW_BLOCKING_FINDING` | review 有阻断问题 |
| `PR_HEAD_CHANGED` | 审批后 PR head commit 变化 |
| `MERGE_CI_NOT_GREEN` | CI 未通过 |
| `MERGE_CONFLICT` | 合并冲突 |
| `DEPLOY_FAILED` | 部署失败 |
| `SMOKE_FAILED` | smoke 失败 |
| `CODEX_FAILED` | Codex 执行失败或超时 |
| `PR_CREATE_FAILED` | PR 创建失败 |
| `CONFIG_MISSING` | 必要环境变量或项目配置缺失 |

错误必须同步到 GitHub 或飞书，避免 issue 静默卡住。
