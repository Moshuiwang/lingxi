# 审批后 MCP 验证等待态数据设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/post-approval-mcp-smoke-prd.md`

## 1. 数据原则

- 审批记录仍是授权事实源，不新增第二套授权状态。
- 复验状态只描述“审批后验证体验”，不代表 MCP 权限事实源。
- 任何日志、数据库、飞书消息都不得保存 token、私钥或 `.env` 内容。
- 复验状态必须持久化，避免服务重启后丢失“系统会自动复验”的承诺。

## 2. 复用现有数据

| 数据 | 用途 |
|------|------|
| `approval_records` | 识别审批记录、申请人、公司、职能、审批状态 |
| `users` | 判断用户是否已 approved / disabled |
| `mcp_tokens` | 只判断是否存在 token，不输出 token |
| `user_functions` | 展示当前已有权限和本次新增权限 |
| `audit_logs` | 可记录审批后验证摘要，不记录敏感内容 |

## 3. 建议新增字段

在 `approval_records` 补充窄字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `mcp_smoke_status` | text nullable | `not_required` / `pending_sync` / `passed` / `needs_manual_check` |
| `mcp_smoke_attempts` | integer | 已执行复验次数 |
| `mcp_smoke_next_check_at` | datetime nullable | 下一次自动复验时间 |
| `mcp_smoke_last_checked_at` | datetime nullable | 最近一次复验时间 |
| `mcp_smoke_last_summary` | text nullable | 脱敏摘要，给审批群读 |

进程内短任务只能作为加速，不能替代这些字段。

## 4. 脱敏摘要

`mcp_smoke_last_summary` 只允许保存：

- 验证阶段：第一次 / 第二次 / 最终。
- 结果：等待同步 / 通过 / 需人工处理。
- 用户可见影响：是否建议用户开始使用。
- 下一步：自动复验或人工检查。

不得保存：

- token 明文。
- Authorization header。
- `.mcp.json` 内容。
- 私钥、ZIP 口令、`.env` 内容。

## 5. 状态转换

| 当前状态 | 事件 | 新状态 |
|----------|------|--------|
| null | 审批通过且权限发布完成 | `pending_sync` |
| `pending_sync` | 任一复验通过 | `passed` |
| `pending_sync` | 最终复验仍失败 | `needs_manual_check` |
| `pending_sync` | 用户被禁用或审批记录撤销 | `not_required` |

`passed` 和 `needs_manual_check` 是终态，后续手工重跑 smoke 不应覆盖历史，除非显式重新开始验证。

## 6. 补偿扫描

服务启动或定时任务应扫描：

- `mcp_smoke_status='pending_sync'`
- `mcp_smoke_next_check_at <= now`

扫描后继续复验。超过最终复验窗口仍未通过的记录必须转为 `needs_manual_check` 并通知审批群。

## 7. 保留策略

审批后 smoke 状态随审批记录保留。不新增长期明细表。若未来要分析 MCP 同步耗时，再另建统计表。
