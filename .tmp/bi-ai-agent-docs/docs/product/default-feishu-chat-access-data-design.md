# 默认飞书聊天准入数据设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/default-feishu-chat-access-prd.md`

## 1. 数据原则

- 飞书聊天是默认必开能力，不需要用户选择，也不需要单独审批状态。
- 高级工作台是本次申请的附加项，默认不开。
- 审批记录仍表示一次整体申请，不拆成两个独立审批单。
- 数据权限仍由现有用户、授权和 MCP token 体系承载。
- 后台 Linux / Claude / Codex 运行身份用于私聊 session、经验沉淀和审计，不等同于用户可登录的高级工作台。

## 2. 建议新增字段

若当前 `approval_records` 没有可表达附加项的字段，建议补窄字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `feishu_chat_requested` | boolean | 恒为 true，用于显示和兼容，不给用户选择 |
| `advanced_workspace_requested` | boolean | 用户是否额外申请高级数据分析工作台，默认 false |
| `advanced_workspace_reason` | text nullable | 用户选择高级工作台时填写的用途说明 |

如果不希望存储 `feishu_chat_requested`，也可在服务层视为默认 true，只持久化 `advanced_workspace_requested`。

## 3. 用户状态

现有 `users.status=approved` 代表用户基础 BI 权限已通过。审批通过后需要同步激活 `codex_chat_users.entitlement_status=active`，让飞书私聊按该用户的 `open_id` 放行，并用该用户的 Linux 运行身份保存 Claude/Codex session。

高级工作台和 JumpServer 状态采用 `docs/product/jumpserver-access-migration-data-design.md` 的拆分模型，避免把“工作台已准备”和“JumpServer 权限已开通”混在一起。

| 字段 | 含义 |
|------|------|
| `workspace_access_status` | 用户可见高级工作台登录入口是否已交付 |
| `jumpserver_access_status` | JumpServer 资产权限是否已开通或待运维处理 |

若本轮不落长期状态，至少要在审批记录和通知里保留“是否申请高级工作台”的可追踪信息。

## 4. 权限发布

无论是否申请高级工作台，只要审批通过，都应：

- 写入用户和授权；
- 签发或维护 MCP token；
- 发布多维表格权限；
- 创建或沿用该用户的后台 Linux / Claude / Codex 运行身份；
- 激活飞书私聊 entitlement；
- 允许用户在飞书私聊中继续查询。

只有申请高级工作台时，才触发登录材料交付和 JumpServer 相关状态。

## 5. 审计记录

建议记录：

- `feishu_chat_access_approved`
- `advanced_workspace_requested`
- `advanced_workspace_not_requested`
- `advanced_workspace_preparation_started`

审计里不保存 SSH 私钥、ZIP 口令、token 或 `.env` 内容。
