# JumpServer 登录迁移数据设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/jumpserver-access-migration-prd.md`

## 1. 数据原则

- JumpServer 登录状态不是 BI 数据权限事实源。
- 数据权限继续由 SQLite 授权、多维表格发布、MCP 服务端校验决定。
- 第一版不保存 JumpServer 密码、MFA、私钥或 API token。
- 若 JumpServer API 未接入，只记录“待运维开通”的脱敏状态。

## 2. 复用现有字段

| 数据 | 用途 |
|------|------|
| `users.email` | 与 LDAP 或 JumpServer 用户映射的候选字段 |
| `users.linux_username` | EC2 目标账号 |
| `approval_records` | 识别是否申请高级工作台 |
| `ssh_delivery_status` | 旧语义为 SSH 交付，需要迁移为工作台准备状态或保留兼容 |
| `audit_logs` | 记录待办生成、状态变更和回收动作 |

## 3. 建议新增字段

如果实现需要落库，可在用户或审批记录上新增窄字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `workspace_access_status` | text | `not_requested` / `preparing` / `ready` / `failed` |
| `jumpserver_access_status` | text | `not_required` / `pending_ops` / `granted` / `failed` / `revoked` |
| `jumpserver_asset_hint` | text nullable | 目标资产显示名或运维待确认信息 |
| `jumpserver_requested_at` | datetime nullable | 待办生成时间 |
| `jumpserver_confirmed_at` | datetime nullable | 授权确认时间 |

如不想扩表，第一版也可以把状态写入 `audit_logs` 和审批群消息，不回写长期字段。

## 4. 状态含义

| 状态 | 用户/管理员含义 |
|------|----------------|
| `not_requested` | 用户没有申请高级工作台 |
| `preparing` | EC2 工作区和 MCP 工作区正在准备 |
| `ready` | 高级工作台环境准备好 |
| `not_required` | 用户未申请高级工作台或不需要 JumpServer 权限 |
| `pending_ops` | 等待运维开通 JumpServer 资产权限 |
| `granted` | JumpServer 权限已确认 |
| `failed` | 工作台准备或 JumpServer 授权失败，需要管理员处理 |
| `revoked` | 用户禁用后 JumpServer 权限应已回收 |

## 5. 审计记录

建议 audit event：

- `workspace_ready`
- `jumpserver_access_requested`
- `jumpserver_access_confirmed`
- `jumpserver_access_revoke_requested`
- `jumpserver_access_revoked`

审计内容只包含邮箱、Linux 用户名、资产提示、状态，不包含凭证。

## 6. 开放问题

实现前需要运维确认：

- JumpServer 授权用 LDAP 用户名、邮箱还是其他 ID。
- BI AI Agent EC2 在 JumpServer 中的资产名称。
- 是否有 API。
- 若无 API，待办发审批群还是专门运维群。
