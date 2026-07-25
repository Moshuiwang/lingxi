# BI AI Agent 文档索引

仓库文档只保存当前有效的产品合同、架构边界、协议规范、运维手册和可执行测试说明。
计划、优先级、owner、状态、依赖和验收证据统一维护在
[GitHub Project](https://github.com/orgs/startimes-bi/projects/2) 与关联 Issues 中。

## 核心入口

- [产品合同](product/requirements.md)
- [架构总览](architecture/architecture.md)
- [架构决策](architecture/architecture-decisions.md)
- [工程实践](architecture/engineering-practices.md)
- [运维手册](ops/runbook.md)
- [Live Run 清单](ops/live-run-checklist.md)
- [MCP 权限边界](mcp/mcp-permission-boundary.md)
- [文档治理契约](project/document-governance.md)

## 产品规范

[统一产品需求（唯一产品视角入口）](product/requirements.md) 描述当前产品面向各角色的完整体验、边界、已知缺口及专题资料边界。以下内容是各专题的设计或验收参考，不能单独作为当前产品承诺；具体状态仍以 GitHub Project 和关联 Issue 为准。

- [用户准入申请](product/prd-onboarding-flow.md)
- [用户权限变更](product/user-permission-change-flow.md)
- [飞书卡片流程](product/feishu-card-flow-v2.md)
- [默认飞书聊天准入](product/default-feishu-chat-access-prd.md)
- [审批后 MCP 验证](product/post-approval-mcp-smoke-prd.md)
- [JumpServer 接入](product/jumpserver-access-migration-prd.md)
- [统一 BI 巡检](product/bi-readiness-check-prd.md)
- [准入健康巡检](product/admission-health-check-prd.md)
- [指标分配闭环](product/metric-assignment-closure-prd.md)
- [私聊路由](product/private-chat-routing-prd.md)
- [飞书 Claude 对话体验](product/feishu-claude-chat-ux-prd.md)
- [飞书 Claude 对话体验数据设计](product/feishu-claude-chat-ux-data-design.md)
- [飞书 Claude 对话体验接口设计](product/feishu-claude-chat-ux-interface-design.md)
- [飞书云盘目录规范](product/feishu-drive-folder-spec.md)
- [话题标题规则](product/feishu-topic-title-standard-light.md)
- [选择性生产用户迁移](product/selective-production-user-migration-prd.md)
- [BI Plus 正式/测试机器人环境切换](product/bi-plus-runtime-profile-prd.md)
- [BI Plus 环境切换数据设计](product/bi-plus-runtime-profile-data-design.md)
- [BI Plus 环境切换接口设计](product/bi-plus-runtime-profile-interface-design.md)

## 架构

- [飞书审批群命令](architecture/feishu-admin-command-architecture.md)
- [准入健康巡检](architecture/admission-health-check-architecture.md)
- [统一 BI 巡检](architecture/bi-readiness-check-architecture.md)
- [默认飞书聊天准入](architecture/default-feishu-chat-access-architecture.md)
- [审批后 MCP 验证](architecture/post-approval-mcp-smoke-architecture.md)
- [JumpServer 接入](architecture/jumpserver-access-migration-architecture.md)
- [指标分配闭环](architecture/metric-assignment-closure-architecture.md)
- [私聊路由](architecture/private-chat-routing-architecture.md)
- [飞书 Claude 对话体验](architecture/feishu-claude-chat-ux-architecture.md)
- [E2E 测试账号 Runner](architecture/e2e-test-account-runner-architecture.md)
- [Evals Benchmark](architecture/evals-benchmark-redesign-architecture.md)

- 历史归档（不适用于当前生产）：`architecture/codex-chat-per-user-agent-architecture.md`、`architecture/codex-private-chat-architecture.md`、`architecture/codex-default-agent-architecture.md`
- [选择性生产用户迁移](architecture/selective-production-user-migration-architecture.md)
- [BI Plus 正式/测试机器人环境切换](architecture/bi-plus-runtime-profile-architecture.md)

## MCP 规范

- [MCP 能力说明](mcp/mcp-overview.md)
- [权限隔离边界](mcp/mcp-permission-boundary.md)
- [访问令牌加密](mcp/mcp-encryption-spec.md)
- [指标权限结构](mcp/metric-permission-design.md)
- [指标字典](mcp/metrics-dictionary.md)
- [查询结果契约](mcp/query-result-contract.md)

## 运维

- [系统 Runbook](ops/runbook.md)
- [新生产服务器最小安全基线](ops/production-bootstrap.md)
- [Live Run 清单](ops/live-run-checklist.md)
- [MCP 地址切换](ops/mcp-endpoint-switch-runbook.md)
- [版本机制](ops/versioning.md)
- [SQLite 备份与隔离恢复](ops/sqlite-backup-restore.md)
- [选择性生产用户迁移测试](testing/selective-production-user-migration-test-plan.md)
- [BI Plus 环境切换测试](testing/bi-plus-runtime-profile-test-plan.md)
- [代码仓库边界](ops/repository-host-migration.md)

## 测试与评估

- [飞书 Claude 对话体验测试计划](testing/feishu-claude-chat-ux-test-plan.md)
- [E2E 自动化架构](testing/e2e-test-automation-architecture.md)
- [审批群命令 Live Run](testing/feishu-admin-command-live-run.md)
- [Evals Benchmark 手册](testing/evals-benchmark-manual.md)
- [系统提示词](testing/system-prompt.md)
- [最小系统提示词](testing/system-prompt-minimal.md)
- [Evals 子工程入口](../evals/README.md)

专题测试计划只用于解释当前可执行测试的边界，不承担项目状态管理。项目是否需要执行、何时执行及验收结果，以 GitHub Project 和关联 Issue 为准。

## 历史 Codex 文档

产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径。以下文档仅保留历史决策和演进记录，不得作为生产部署、验收或排障前提：

- 产品与数据/接口设计：`product/codex-default-agent-*.md`、`product/codex-chat-per-user-agent-*.md`、`product/codex-private-chat-*.md`
- 架构设计：`architecture/codex-default-agent-architecture.md`、`architecture/codex-chat-per-user-agent-architecture.md`、`architecture/codex-private-chat-architecture.md`
- 历史测试方案：`testing/codex-private-chat-e2e.md`
