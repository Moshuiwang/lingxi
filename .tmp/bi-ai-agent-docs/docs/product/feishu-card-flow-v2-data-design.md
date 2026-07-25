# 飞书卡片流程 V2 数据设计

> 日期：2026-06-08
> 状态：已随 V2 施工落地，未新增复杂数据模型；待真实飞书 live run / 人工点击复验
> 依据：`docs/feishu-card-flow-v2.md`、`docs/feishu-card-flow-v2-architecture.md`
> 范围：用户申请、审批、权限落库、取消授权、权限发布失败记录。

## 一、设计目标

V2 尽量复用现有 SQLite 表，不新增复杂数据模型。

目标只有三件事：

- 申请能记录“一个公司 + 多个职能”。
- 权限事实源保持“用户 + 公司 + 单个职能”。
- 发布失败时不能让管理员或用户误以为权限已经完整生效或完整收回。

## 二、现有表是否够用

| 表 | V2 用法 | 是否需要新表 |
|---|---|---|
| `users` | 用户身份、状态、当前姓名邮箱 | 不需要 |
| `approval_records` | 每次申请快照，`functions_json` 已支持职能多选 | 不需要 |
| `user_functions` | 当前真实权限，唯一粒度是 `user_id + company + function` | 不需要 |
| `user_grants` | 旧兼容授权表，继续由现有逻辑维护 | 不需要 |
| `audit_logs` | 记录申请、审批、取消授权、发布失败等操作摘要 | 不需要 |
| `companies` / `physical_companies` | 继续保留给历史数据、管理后台、对账和 access_all 逻辑 | 不需要 |
| `function_metrics` | 继续作为权限发布展开到指标口径的依据 | 不需要 |

V2 不建议一开始新增专门的 `card_sessions`、`revoke_sessions` 或 `notification_records`。003 到 004 的中间选择继续通过飞书卡片 `form_value` 传递，不保存服务端会话状态。

## 三、用户与申请数据

### 3.1 `users`

继续使用现有状态：

```text
pending / approved / rejected / disabled / offboarded
```

V2 入口判断依赖这些状态，但不能只看 `users.status`。停用 / 离职用户优先提示账号不可用；其它用户只要存在待审申请，就按“已有申请正在审批”处理。

施工要求：

- 已通过用户再次申请时，不把 `users.status` 降回 `pending`。
- 停用 / 离职用户优先提示账号不可用，不能通过旧 001 提交新申请。
- 已通过用户的 001 展示当前 `users.name` 和 `users.email`，不可编辑。

### 3.2 `approval_records`

继续作为每次申请快照。

字段使用：

| 字段 | V2 用法 |
|---|---|
| `user_id` | 申请人 |
| `name_snapshot` / `email_snapshot` | 本次申请时的姓名邮箱快照 |
| `company` | 本次申请公司，单选 |
| `functions_json` | 本次申请职能，多选 JSON list |
| `notes` | 用户说明 |
| `status` | `pending / approved / rejected`；V2 主链不再主动写 `expired` |
| `approval_msg_id` | 审批群 002 消息 ID，用于处理后置灰或二次点击拦截 |
| SSH / provisioning 相关字段 | 沿用现有首次开户和 SSH 交付记录 |

V2 不删除 `expired` 枚举兼容旧数据，但新主链不主动生成 `expired`。

### 3.3 待审申请唯一口径

V2 用户体验要求：只要用户有任何待审申请，就不再发新的 001，也不允许旧 001 再提交。

后台查询口径：

```text
approval_records.user_id = 当前用户
AND approval_records.status = 'pending'
```

不要只按公司查 pending。否则用户申请 A 公司待审时，还可能通过旧卡提交 B 公司，和 V2 口径冲突。

V2 不做主动超时关闭。若待审申请长期无人处理，管理员通过审批群文字命令人工关闭，关闭后按退回流程通知用户重新申请。

管理员人工关闭 pending 申请时，只改这条 `approval_records.status = rejected` 并写入关闭原因。若用户本身已经是 approved 用户的补充申请被关闭，不能把 `users.status` 降级成 rejected；用户仍保留已通过身份和既有权限。

## 四、权限事实源

### 4.1 `user_functions`

`user_functions` 是 V2 的当前权限事实源。

唯一粒度：

```text
user_id + company + function
```

这已经由现有唯一约束 `uq_user_function` 表达。

审批通过时：

- 一次申请只有一个公司。
- 职能可以多选。
- 多个职能逐条展开写入 `user_functions`。
- 已存在的 `user_id + company + function` 跳过，不重复写。

示例：

```text
申请：Rwanda + [运营, 财务]

落库：
user_id=1, company=Rwanda, function=运营
user_id=1, company=Rwanda, function=财务
```

### 4.2 `ALL_FUNCTION`

现有代码支持 `全部` 职能，但 V2 第一版不默认开放给用户申请。

V2 规则：

- 001 只能提交 `permission_options.yaml` 中配置的职能。
- 如果产品要开放 `全部`，必须显式把 `全部` 加入 `permission_options.yaml`。
- 如果审批通过 `全部`，同公司下普通职能行应清理，只保留 `全部`，避免 003 同时出现 `全部` 和普通职能造成误解。
- 对历史数据中已经存在的 `全部`，003 取消授权下拉显示为该公司下的 `全部`。

## 五、配置数据

新增：

```text
config/permission_options.yaml
```

它是 001 公司/职能下拉和后台提交校验的唯一来源。

该文件只保存“可选项身份”，不保存用户权限事实：

```yaml
companies:
  - label: Rwanda
    value: Rwanda

functions:
  - label: 运营
    value: 运营
```

数据关系：

- `permission_options.yaml` 决定用户可以申请什么。
- `user_functions` 记录用户已经拥有什么。
- `function_metrics` 决定权限发布时每个公司/职能对应哪些指标。
- `companies` / `physical_companies` 继续用于历史兼容、管理后台、access_all 或对账，不再作为 001 实时下拉来源。

施工要求：

- `permission_options.yaml` 的 `value` 必须能和 `function_metrics.company/function` 对上。
- 配置里有、`function_metrics` 里没有的组合，允许申请但权限发布时可能失败；失败必须进入文字通知和审计。
- 配置里没有的公司或职能，不允许通过卡片提交。

## 六、取消授权数据

003 的下拉选项直接来自 `user_functions`。

每个选项是一条真实存在的权限：

```text
user_id + company + function
```

004 确认时通过 `form_value` 带回这三个值：

```json
{
  "revoke_target": "{\"user_id\":123,\"company\":\"Rwanda\",\"function\":\"运营\"}"
}
```

确认取消时必须重新查 `user_functions`：

- 仍存在：允许取消。
- 不存在：提示管理员“这项权限已经不存在，无需重复取消”。

取消授权继续调用 `user_service.revoke_authorization()`，不要绕过它直接删表。

## 七、发布失败的数据口径

### 7.1 审批通过后发布失败

审批通过后可能出现：

```text
approval_records.status = approved
user_functions 已写入
permission_publish 失败
```

V2 口径：

- 不回滚审批。
- 不删除已写入的 `user_functions`。
- 用户文字通知不能说“新权限已实时生效”，应说明权限发布正在同步中，通常 15 分钟内生效。
- 管理或审批群文字结果必须显示“权限发布失败，系统会由 timer 兜底重试；如需立即处理，可执行 `@Bot 重试权限发布`”。
- 记录一条 `audit_logs`，`action` 建议为 `permission_publish`，`result` 为 `failed`，`detail` 写申请记录、用户、公司、职能和简短错误。
- 现有 `scripts/sync_permission_bitable.py` 会由 systemd timer 以 ≤15 分钟周期重试；管理员也可使用 `@Bot 重试权限发布` 立即重新发布当前数据库权限事实。
- 手动重试和 timer 必须复用同一套 `publish_permissions_now()` / `sync_permissions` 逻辑；成功结果展示 `created / updated / deleted`。
- 如果现有同步入口没有全局锁，施工时补一把跨进程文件锁，例如同一个 lockfile / `flock`。timer 脚本和 Bot 手动重试都必须抢这把锁，避免两个独立进程同时全量同步。

### 7.2 取消授权后发布失败

取消授权可能出现：

```text
user_functions 已删除
permission_publish 失败
```

V2 口径：

- 管理员文字结果必须说明：数据库已删除，权限发布失败但系统会由 timer 兜底重试；如需立即处理，可执行 `@Bot 重试权限发布`。
- 用户文字通知不能说“权限已经实时回收”，应说明“权限取消已记录，系统正在同步，通常 15 分钟内生效”。
- 记录一条 `audit_logs`，`action` 建议为 `permission_publish`，`result` 为 `failed`，`detail` 写被取消用户、公司、职能和简短错误。
- 第一版不保存待补发通知队列，因此 timer 或手动重试成功后不会自动补发第二条用户通知。
- 手动重试和 timer 的并发口径同上：复用现有发布函数，并避免同一时刻跑两次全量同步。

### 7.3 是否需要新增失败队列表

第一版不新增失败队列表。

原因：

- 当前 V2 先追求流程收敛和可维护。
- 失败可以通过 `audit_logs` 和管理员文字结果发现。
- 现有 systemd timer 已经会周期性重试全量发布。
- 管理员可以用 `@Bot 重试权限发布` 手动立即重试全量发布。
- 如果后续需要自动重试，再新增 `permission_publish_jobs` 或类似队列表。

## 八、审计记录

V2 至少保留以下审计动作：

| 动作 | 建议 action | result |
|---|---|---|
| 用户提交申请 | `apply` | `success` / `failed` |
| 审批通过 | `approve` | `success` |
| 审批退回 | `reject` | `success` |
| 取消授权 | `revoke` | `success` / `failed` |
| 权限发布 | `permission_publish` | `success` / `failed` |
| 手动重试权限发布 | `permission_publish_retry` | `success` / `failed` |
| 管理员关闭待审申请 | `admin_close_pending` | `success` / `failed` |
| 旧卡迁移兜底 | `legacy_card_action` | `blocked` |

`detail` 只写摘要，不写敏感内容或完整飞书消息。

## 九、迁移与清理

V2 施工不要求数据库迁移。

需要做的迁移类动作：

1. 停用 V2 主链里的审批主动超时任务。
2. 保留 `expired` 旧状态兼容，不主动生成新 `expired`。
3. 上线前检查当前 `pending` 申请，避免旧审批卡和新审批卡并存导致体验混乱。
4. 提供管理员文字命令关闭长期待审申请，不恢复自动超时。
5. 提供管理员文字命令重试权限发布，不新增失败队列表。
6. 对旧卡 action 保留兜底，不静默失败。

待 V2 稳定后，再考虑：

- 清理旧无按钮结果卡相关逻辑。
- 清理旧管理员多步收权向导。
- 如果确认长期不再需要，单独评估是否删除 `expired` 相关代码和测试。

## 十、验收标准

数据层通过以下结果即认为 V2 可施工：

1. 不新增核心业务表也能支撑 4 张卡。
2. 待审申请按用户级别拦截，不按公司级别拦截。
3. 多职能申请会逐条写入 `user_functions`。
4. `user_functions` 不产生重复权限。
5. 003 只列出当前真实存在的 `user_functions`。
6. 004 确认前会重新校验权限仍存在。
7. 审批发布失败不把新权限说成实时已生效，而是说明正在同步。
8. 取消发布失败不把权限说成实时已回收，而是说明取消已记录、正在同步。
9. 长期待审申请有管理员人工关闭出口。
10. 权限发布失败有管理员手动重试出口。
11. 所有失败都有审计摘要或管理员可见文字结果。
