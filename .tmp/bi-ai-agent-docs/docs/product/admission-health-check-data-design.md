# BI Agent 每小时准入巡检数据设计

## 1. 设计原则

- SQLite 继续是授权事实源。
- MCP 指标快照是外部事实的本地缓存，不直接代表授权。
- 新指标默认锁住，必须被绑定到职责后才进入普通用户权限。
- 巡检状态只用于去重提醒和追踪，不作为 MCP 验权依据。

## 2. 现有表复用

| 表 | 用途 |
|----|------|
| `users` | 判断用户状态：approved / disabled |
| `mcp_tokens` | 判断 approved 用户是否具备 MCP token |
| `user_functions` | 用户已获批的公司 + 职能 |
| `companies` | 逻辑公司到实体 company_id 的展开 |
| `function_metrics` | 公司 + 职能 到 metric_key 的授权映射 |
| 飞书多维表格权限发布副本 | 只读核对 approved 用户是否已发布、disabled 用户是否仍残留可用 permissions |

## 3. 新增表：`metric_catalog_snapshots`

记录每次 MCP 全量指标快照。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | integer PK | 自增 |
| `source` | text | 快照来源，例如 `mcpmonall:list_metrics` |
| `generated_at` | datetime | 快照采集时间 |
| `snapshot_json` | text | 规范化后的指标数组 JSON |
| `metric_count` | integer | 指标数量 |
| `content_hash` | text | 快照内容 hash，用于判断是否变化 |
| `created_at` | datetime | 创建时间 |

规范化指标对象建议字段：

```json
{
  "metric_key": "sub_recharge_money",
  "name_cn": "充值金额",
  "name_en": "Recharge Amount",
  "description": "待 MCP 返回为准",
  "parameters": ["start_date", "end_date", "company_ids"],
  "dimensions": ["date", "company"],
  "raw": {}
}
```

`raw` 可保留 MCP 原始字段，但不得包含 token、密钥或用户隐私。

## 4. 新增表：`metric_assignment_status`

记录每个指标的准入处理状态。

| 字段 | 类型 | 说明 |
|------|------|------|
| `metric_key` | text PK | MCP 指标 key |
| `status` | text | `unassigned` / `assigned` / `ignored` / `removed` |
| `first_seen_at` | datetime | 首次发现时间 |
| `last_seen_at` | datetime | 最近一次仍存在时间 |
| `last_notified_at` | datetime nullable | 最近一次推送审批群时间 |
| `note` | text nullable | 管理员备注 |
| `updated_at` | datetime | 更新时间 |

状态含义：

| 状态 | 含义 |
|------|------|
| `unassigned` | MCP 有该指标，但普通职能没有绑定 |
| `assigned` | 至少一个普通职能已绑定 |
| `ignored` | 业务明确暂不开放 |
| `removed` | MCP 当前不再返回 |

## 5. 新增表：`admission_health_runs`

记录每小时巡检结果，方便追踪和排障。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | integer PK | 自增 |
| `started_at` | datetime | 开始时间 |
| `finished_at` | datetime nullable | 结束时间 |
| `status` | text | `success` / `failed` |
| `metric_findings_json` | text | 指标类发现 |
| `account_findings_json` | text | 账号类发现 |
| `notified` | boolean | 是否推送审批群 |
| `error` | text nullable | 失败原因 |

## 6. 不新增的字段

不在 `users` 上新增“指标权限缓存”字段。用户可见指标继续通过 `user_functions + function_metrics` 展开得到，避免缓存和事实源不一致。

不在多维表格权限副本中新增巡检字段。多维表格继续只服务 MCP 验权。

## 7. 去重提醒

当前去重规则：

- 新指标首次出现：立即推送。
- 同一指标、同一种问题仍未处理：工作时段每 24 小时最多提醒一次。
- 账号健康异常：同一异常连续存在时按 `ADMISSION_HEALTH_REMINDER_HOURS` 节流，并合并到同一条摘要中。
- 高权限复核：每日最多推送一次；若高权限名单发生变化，下一轮工作时段立即推送。

## 8. 快照新鲜度

巡检消费方必须校验 MCP 指标快照的 `generated_at`：

- 默认超过 2 小时视为过期。
- 过期时本轮指标巡检失败，不更新 last snapshot，不输出“无变化”。
- 若允许推送，则向审批群发送“指标快照过期，巡检不可用”异常。
- 最大过期时长由 `ADMISSION_HEALTH_MAX_SNAPSHOT_AGE_HOURS` 配置。

## 9. 未分配职责判定

`function_metrics` 的事实粒度是 `(company, function, metric_key)`。本期“未分配职责”的判定是指标级：

- 若某个 `metric_key` 在 `function_metrics` 中没有任何普通职能绑定，则状态为 `unassigned`。
- `全部` 职能不算普通职能绑定，因为它只代表高权限用户自动覆盖，不代表普通运营/财务已获权。
- 若某个指标只绑定了部分公司/职能，本期不作为错误推送；后续如需要，可新增“公司级指标覆盖率”检查。

## 10. 迁移策略

新增表通过应用启动时的 SQLite 兼容补列/建表逻辑创建，或由巡检脚本首次运行前调用 `init_db()` 创建。

旧数据不需要迁移；首次运行时：

1. 读取当前 MCP 指标快照。
2. 建立 baseline。
3. 对未分配职责指标直接产生待处理提醒。
