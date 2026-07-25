# 飞书卡片流程 V2 接口与事件设计

> 日期：2026-06-08
> 状态：已随 V2 施工落地，作为飞书事件 / action / service 调用口径；待真实飞书 live run / 人工点击复验
> 依据：`docs/feishu-card-flow-v2.md`、`docs/feishu-card-flow-v2-architecture.md`
> 范围：飞书消息路由、卡片 action、内部 flow service 输入输出、文字通知。

## 一、设计目标

这里的“接口”不是新增 HTTP API，而是把飞书事件和内部服务调用说清楚：

- 用户发什么消息才会收到卡片。
- 每张卡片有哪些 action。
- 每个 action 的 payload 长什么样。
- 入口层调用哪个 service。
- service 返回什么，让入口层发卡或发文字。

目标是让施工时不用猜 action 名、字段名和错误处理。

## 二、入口消息路由

### 2.1 私聊申请权限

触发条件：

```text
chat_type = p2p
text = 申请权限
```

处理：

```text
start_bot.py
  -> application_flow.build_entry_response(open_id, tenant_key)
```

返回类型：

| 返回 | 入口层动作 |
|---|---|
| `card_001` | 私聊发送 001 |
| `text` | 私聊发送文字 |
| `text_then_card_001` | 先发退回原因文字，再发 001 |

普通私聊不进入申请流程，不弹 001。

### 2.2 审批群取消授权

触发条件：

```text
chat_id = approval_chat_id
mention Bot
text = 取消授权
```

处理：

```text
start_bot.py
  -> revoke_flow.list_revoke_choices(actor_open_id, chat_id)
```

返回类型：

| 返回 | 入口层动作 |
|---|---|
| `card_003` | 审批群发送 003 |
| `text` | 审批群发送无权限、无可取消权限等文字 |

私聊不弹 003。非审批群不弹 003。

### 2.3 审批群关闭待审申请

触发条件：

```text
chat_id = approval_chat_id
mention Bot
text = 关闭申请 <用户邮箱或 open_id> <原因>
```

处理：

```text
start_bot.py
  -> pending_admin_flow.close_pending_application(actor_open_id, ident, reason)
```

返回类型：

| 返回 | 入口层动作 |
|---|---|
| `closed` | 审批群发送关闭结果文字，用户私聊收到退回式通知 |
| `not_found` | 审批群发送未找到待审申请 |
| `not_admin` | 审批群发送无权限 |
| `validation_error` | 审批群发送命令格式错误，例如缺少原因 |

这是人工兜底，不是自动超时，不新增卡片。

### 2.4 审批群重试权限发布

触发条件：

```text
chat_id = approval_chat_id
mention Bot
text = 重试权限发布
```

处理：

```text
start_bot.py
  -> permission_publish_flow.retry_permission_publish(actor_open_id)
```

返回类型：

| 返回 | 入口层动作 |
|---|---|
| `published` | 审批群发送重试成功结果 |
| `failed` | 审批群发送重试失败原因 |
| `not_admin` | 审批群发送无权限 |

现有 systemd timer 会周期性重试权限发布。该命令只用于管理员立即触发一次全量同步，不必等待下一次 timer；命令结果只通知管理员。

## 三、卡片 action 清单

| 卡片 | action_name | 类型 | 说明 |
|---|---|---|---|
| 001 | `v2_application_submit` | `form_submit` | 提交申请 |
| 001 | `v2_application_cancel` | button | 取消填写，旧卡不可操作 |
| 002 | `v2_approval_approve` | `form_submit` | 审批通过 |
| 002 | `v2_approval_reject` | `form_submit` | 审批退回 |
| 003 | `v2_revoke_select` | `form_submit` | 选择一条用户-公司-职能权限，进入 004 |
| 003 | `v2_revoke_cancel` | button | 取消本次取消授权流程，旧卡不可操作 |
| 004 | `v2_revoke_confirm` | button | 确认取消授权 |
| 004 | `v2_revoke_back` | button | 返回重选，重新生成 003 |
| 004 | `v2_revoke_cancel` | button | 取消本次取消授权流程，旧卡不可操作 |

旧 action 不复用，避免新旧流程混在一起。旧 action 统一走迁移兜底。

## 四、通用 action value

001、003、004 都需要 TTL。

通用字段：

```json
{
  "flow_version": "v2",
  "card_id": "001",
  "expires_at": 1780000000
}
```

处理规则：

- 所有 V2 action 先检查 `flow_version` 和 `card_id`。
- 001 / 003 / 004 先检查 `expires_at`。
- 002 不设置 TTL，不检查 `expires_at`。
- 不认识的旧 action 返回迁移兜底文字。

## 五、001 填写申请卡

### 5.1 表单字段

首次申请：

```json
{
  "name": "张三",
  "email": "zhangsan@startimes.com.cn",
  "company": "Rwanda",
  "functions": ["运营", "财务"],
  "notes": "需要看充值指标"
}
```

已通过用户再次申请：

```json
{
  "company": "Rwanda",
  "functions": ["运营", "财务"],
  "notes": "补充财务权限"
}
```

已通过用户的姓名和邮箱不让用户编辑，由后台从 `users` 读取。

### 5.2 `v2_application_submit`

入口层处理：

```text
start_bot.py
  -> application_flow.submit_from_card(open_id, tenant_key, form_value, action_value)
```

`application_flow` 返回：

| 返回 | 入口层动作 |
|---|---|
| `validation_error` | 私聊发送文字原因，001 保持或变不可操作由实现选简单方式 |
| `pending_exists` | 私聊发送已有申请正在审批 |
| `submitted` | 审批群发送 002，用户私聊发送“申请已提交，等待审批”，001 变不可操作 |

提交成功后的返回数据建议：

```json
{
  "type": "submitted",
  "approval_record_id": 123,
  "approval_card_context": {
    "applicant_name": "张三",
    "open_id": "ou_xxx",
    "tenant_key": "tenant_xxx",
    "email": "zhangsan@startimes.com.cn",
    "company": "Rwanda",
    "functions": ["运营", "财务"],
    "notes": "需要看充值指标",
    "current_auth": {
      "Uganda": ["运营"]
    }
  },
  "user_text": "申请已提交，等待审批"
}
```

### 5.3 `v2_application_cancel`

处理：

```text
start_bot.py
  -> update current card to non-operable cancelled state
```

不写数据库，不发审批群消息。

## 六、002 审批卡

### 6.1 卡片上下文

002 action value 必须包含：

```json
{
  "flow_version": "v2",
  "card_id": "002",
  "approval_record_id": 123
}
```

002 不设置 TTL。

### 6.2 审批表单字段

```json
{
  "reason": "同意 / 退回原因"
}
```

通过时 reason 可为空。退回时 reason 必填。

### 6.3 `v2_approval_approve`

入口层处理：

```text
start_bot.py
  -> approval_service.approve(record_id, reviewer_open_id)
  -> start post-approval orchestration
  -> approval_followup.build_result(...)
```

处理要求：

- 再次查询 `approval_records.status`。
- 只有 `pending` 可以通过。
- 已经通过或退回的 002 再次点击，只返回“该申请已处理”，不重复执行审批。
- 审批卡处理后必须变为不可操作。

返回给用户的文字要等后续程序执行完：

```json
{
  "approval": "approved",
  "company": "Rwanda",
  "functions": ["运营", "财务"],
  "account_provisioning": "completed | skipped | failed",
  "permission_db": "success",
  "permission_publish": "success | failed",
  "ssh_delivery": "completed | skipped | failed",
  "manual_required": true
}
```

权限发布失败时，用户文字不能说新权限已实时生效，只能说明权限已记录、正在同步，通常 15 分钟内生效。

### 6.4 `v2_approval_reject`

入口层处理：

```text
start_bot.py
  -> approval_service.reject(record_id, reviewer_open_id, reason)
```

处理要求：

- reason 必填。
- 只有 `pending` 可以退回。
- 已经处理过的 002 再次点击，只返回“该申请已处理”。
- 审批卡处理后必须变为不可操作。
- 用户收到退回原因，并提示私聊 Bot 发送“申请权限”重新申请。

## 七、003 取消授权选择卡

### 7.1 下拉选项

003 使用一个组合下拉框：

```text
姓名（邮箱） / 公司 / 职能
```

选项 value 建议用 JSON 字符串，或稳定分隔符编码。为了可读性，建议 JSON 字符串：

```json
{
  "user_id": 123,
  "company": "Rwanda",
  "function": "运营"
}
```

后台不能信任展示值，最终按 value 里的 `user_id/company/function` 重新查 `user_functions`。

### 7.2 `v2_revoke_select`

form_value：

```json
{
  "revoke_target": "{\"user_id\":123,\"company\":\"Rwanda\",\"function\":\"运营\"}"
}
```

入口层处理：

```text
start_bot.py
  -> revoke_flow.build_confirmation(actor_open_id, form_value, action_value)
```

返回：

| 返回 | 入口层动作 |
|---|---|
| `validation_error` | 审批群文字提示或卡片内提示 |
| `card_004` | 更新当前卡为 004 |

004 的 action value 必须继续携带同一份 revoke target。

### 7.3 `v2_revoke_cancel`

003 上点击取消：

```text
start_bot.py
  -> update current card to non-operable cancelled state
```

不写数据库。

## 八、004 取消授权确认卡

### 8.1 action value

```json
{
  "flow_version": "v2",
  "card_id": "004",
  "expires_at": 1780000000,
  "target": {
    "user_id": 123,
    "company": "Rwanda",
    "function": "运营"
  }
}
```

003 到 004 不保存服务端中间态。

### 8.2 `v2_revoke_confirm`

入口层处理：

```text
start_bot.py
  -> revoke_flow.confirm_revoke(actor_open_id, target)
```

返回：

| 返回 | 入口层动作 |
|---|---|
| `not_found` | 管理员文字提示权限已不存在 |
| `not_admin` | 管理员文字提示无权限 |
| `publish_failed` | 管理员文字提示数据库已删除但发布失败，系统会兜底重试；用户收到“取消已记录、正在同步” |
| `revoked` | 管理员文字结果 + 用户文字结果 |

成功返回建议：

```json
{
  "type": "revoked",
  "admin_text": "已取消张三 Rwanda 运营权限...",
  "user_open_id": "ou_xxx",
  "user_text": "你的 Rwanda / 运营 权限已取消...",
  "remaining_auth": {
    "Uganda": ["运营"]
  }
}
```

发布失败返回建议：

```json
{
  "type": "publish_failed",
  "admin_text": "数据库已删除，但权限发布失败，系统会在 15 分钟内重试；也可 @Bot 重试权限发布",
  "user_open_id": "ou_xxx",
  "user_text": "你的 Rwanda / 运营 权限取消已记录，系统正在同步，通常 15 分钟内生效。"
}
```

### 8.3 `v2_revoke_back`

处理：

```text
start_bot.py
  -> revoke_flow.list_revoke_choices(actor_open_id, approval_chat_id)
  -> update current card to new 003
```

旧 004 同时不可操作。

### 8.4 `v2_revoke_cancel`

004 上点击取消：

```text
start_bot.py
  -> update current card to non-operable cancelled state
```

不写数据库。

## 九、旧 action 迁移兜底

V2 上线后，旧卡可能还在用户飞书里。

旧 action 包括但不限于：

```text
submit_application
confirm_application
edit_application
request_more_auth
admin_revoke_start
admin_view_users
wizard_user_selected
wizard_scope_selected
wizard_revoke_confirm
revoke_confirm
revoke_cancel
wizard_cancel
```

处理规则：

- 不再继续旧流程。
- 不静默失败。
- 返回统一文字或卡片更新：

```text
该卡片已失效。请私聊 Bot 发送“申请权限”，或在审批群 @Bot 取消授权 重新开始。
```

如实现成本更低，也可以对旧 action 返回 toast，但用户必须看得到明确提示。

## 十、管理员关闭待审申请

### 10.1 命令格式

```text
@Bot 关闭申请 <用户邮箱或 open_id> <原因>
```

示例：

```text
@Bot 关闭申请 zhangsan@startimes.com.cn 审批长期未处理，请重新申请
```

### 10.2 服务调用

```text
start_bot.py
  -> pending_admin_flow.close_pending_application(actor_open_id, ident, reason)
```

处理要求：

- 只能在审批群触发。
- 触发人必须是管理员。
- reason 必填。
- 找到该用户最新一条 pending 申请。
- 将其按退回处理，写入 reason。
- 用户收到文字通知，提示重新发送“申请权限”。

返回建议：

```json
{
  "type": "closed",
  "approval_record_id": 123,
  "admin_text": "已关闭张三的待审申请，用户可重新申请。",
  "user_open_id": "ou_xxx",
  "user_text": "你的申请已被管理员关闭，原因：审批长期未处理，请重新申请。"
}
```

## 十一、管理员重试权限发布

### 11.1 命令格式

```text
@Bot 重试权限发布
```

### 11.2 服务调用

```text
start_bot.py
  -> permission_publish_flow.retry_permission_publish(actor_open_id)
```

处理要求：

- 只能在审批群触发。
- 触发人必须是管理员。
- 按当前数据库权限事实重新同步权限多维表格。
- 返回本次同步结果。
- 不自动补发用户通知。
- 该命令复用现有 `publish_permissions_now()`，不是一套新的发布逻辑。
- 如果 timer 正在同步，返回“发布任务正在执行，请稍后再试”，不要并发跑第二次。这个判断必须基于跨进程文件锁，例如同一个 lockfile / `flock`，不能只用 Bot 进程内锁。
- 成功结果展示 `publish_permissions_now()` 返回的 `created / updated / deleted`。

返回建议：

```json
{
  "type": "published",
  "admin_text": "权限发布重试成功：created=0 updated=1 deleted=1"
}
```

失败返回建议：

```json
{
  "type": "failed",
  "admin_text": "权限发布重试失败：{error}"
}
```

## 十二、文字通知接口

所有无按钮结果都用文字。

文案从现有 `config/messages.yaml` 扩展，不在代码里硬编码。

建议新增 key：

```yaml
v2:
  application:
    submitted: "申请已提交，等待审批。"
    pending_exists: "你已有申请正在审批，请等待审批结果。"
    disabled: "当前账号不可用，不能发起申请。"
  approval:
    approved_success: "你的申请已通过..."
    approved_publish_failed: "你的申请已通过，数据库已记录，权限正在同步中，通常 15 分钟内生效..."
    followup_status_line: "{label}：{status}"
    rejected: "你的申请被退回，原因：{reason}。如需重新申请，请私聊 Bot 发送“申请权限”。"
    closed_by_admin: "你的申请已被管理员关闭，原因：{reason}。如需重新申请，请私聊 Bot 发送“申请权限”。"
  revoke:
    admin_success: "已取消 {user} 的 {company} / {function} 权限..."
    admin_publish_failed: "数据库已删除，但权限发布失败，系统会在 15 分钟内重试；也可 @Bot 重试权限发布。"
    user_success: "你的 {company} / {function} 权限已取消..."
    user_syncing: "你的 {company} / {function} 权限取消已记录，系统正在同步，通常 15 分钟内生效。"
  permission_publish:
    retry_success: "权限发布重试成功：{detail}"
    retry_failed: "权限发布重试失败：{error}"
  legacy:
    card_expired: "该卡片已失效。请私聊 Bot 发送“申请权限”，或在审批群 @Bot 取消授权 重新开始。"
```

审批通过后的部分失败文案不为每种组合单独建 key。由 `approval_followup.py` 动态拼装逐项状态行，例如“开户：成功 / 权限发布：失败 / SSH 交付：成功”，避免文案组合爆炸。

## 十三、服务返回对象

第一版不需要复杂 class 层级。建议使用简单 dataclass 或 dict，但字段要稳定。

### 13.1 `application_flow`

```python
{
    "type": "card_001" | "text" | "text_then_card_001" | "submitted" | "pending_exists" | "validation_error",
    "text": "...",
    "card_context": {...},
    "approval_record_id": 123,
}
```

### 13.2 `revoke_flow`

```python
{
    "type": "card_003" | "card_004" | "revoked" | "publish_failed" | "not_found" | "not_admin" | "validation_error",
    "text": "...",
    "card_context": {...},
    "admin_text": "...",
    "user_open_id": "ou_xxx",
    "user_text": "...",
}
```

### 13.3 `approval_followup`

```python
{
    "manual_required": true,
    "user_text_key": "v2.approval.approved_publish_failed",
    "status": {
        "permission_db": "success",
        "permission_publish": "failed",
        "ssh_delivery": "completed"
    }
}
```

### 13.4 `pending_admin_flow`

```python
{
    "type": "closed" | "not_found" | "not_admin" | "validation_error",
    "admin_text": "...",
    "user_open_id": "ou_xxx",
    "user_text": "...",
}
```

### 13.5 `permission_publish_flow`

```python
{
    "type": "published" | "failed" | "not_admin",
    "admin_text": "...",
}
```

## 十四、验收标准

接口层通过以下结果即认为 V2 可施工：

1. 只有私聊“申请权限”进入 001。
2. 只有审批群 `@Bot 取消授权` 进入 003。
3. 审批群 `@Bot 关闭申请 ...` 可以人工关闭长期待审申请。
4. 审批群 `@Bot 重试权限发布` 可以立即触发一次全量发布，并复用现有发布逻辑。
5. 001 / 003 / 004 都带 `flow_version/card_id/expires_at`。
6. 002 不带 TTL，但带 `approval_record_id`。
7. 002 已处理后再次点击不会重复审批。
8. 003 选择值能完整带到 004，不需要服务端 session。
9. 004 确认前会重新校验管理员和权限存在性。
10. 旧 action 有明确兜底。
11. 权限发布失败时，用户通知说明正在同步，不误导为实时生效或实时回收。
