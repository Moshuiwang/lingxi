# BI Agent 统一巡检数据设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/bi-readiness-check-prd.md`

## 1. 设计原则

- SQLite 继续保存巡检运行结果和提醒去重状态。
- MCP 是业务数据查询边界；本项目不直接连接生产数据源。
- 统一巡检结果是运维/审批可见状态，不是 MCP 验权事实源。
- 数据新鲜度只记录指标级元信息，不保存业务明细数据。
- 日志和数据库不得保存 token、密钥、用户私钥或 `.env` 内容。

## 2. 复用现有表

| 表 | 用途 |
|----|------|
| `metric_catalog_snapshots` | 记录 MCP 指标目录快照 |
| `metric_assignment_status` | 记录指标是否已分配职责 |
| `admission_health_runs` | 记录现有准入巡检结果 |
| `users` | 账号状态检查 |
| `mcp_tokens` | 只判断是否存在 token，不读取或输出 token |
| `user_functions` | 用户授权公司和职能 |
| `function_metrics` | 职能可见指标映射 |
| 飞书多维表格权限发布副本 | 只读核对 MCP 侧可见权限副本 |

## 3. 建议新增表：`bi_readiness_runs`

记录一次统一巡检的总结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | integer PK | 自增 |
| `started_at` | datetime | 巡检开始时间 |
| `finished_at` | datetime nullable | 巡检结束时间 |
| `status` | text | `ok` / `warning` / `failed` / `unknown` |
| `catalog_status` | text | 指标目录阶段状态 |
| `admission_status` | text | 准入阶段状态 |
| `freshness_status` | text | 数据新鲜度阶段状态 |
| `metric_count` | integer nullable | 当前指标数量 |
| `latest_daily_date` | text nullable | 日指标整体最新日期，如 `2026-06-24` |
| `latest_month` | text nullable | 月指标整体最新月份，如 `2026-06` |
| `summary_json` | text | 统一摘要 JSON |
| `notified` | boolean | 是否已发送飞书通知 |
| `error` | text nullable | 总体失败原因 |
| `created_at` | datetime | 创建时间 |

`summary_json` 只保存状态摘要，不保存业务数据行。

## 4. 建议新增表：`metric_freshness_observations`

记录每次巡检中每个指标的新鲜度观察结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | integer PK | 自增 |
| `run_id` | integer | 关联 `bi_readiness_runs.id` |
| `metric_key` | text | MCP 指标 key |
| `metric_name` | text nullable | 中文名 |
| `period_type` | text | `daily` / `monthly` / `unknown` |
| `lookback_start` | text nullable | 查询窗口开始日期或月份 |
| `lookback_end` | text nullable | 查询窗口结束日期或月份 |
| `latest_period` | text nullable | 最新有数据日期或月份 |
| `lag_days` | integer nullable | 日指标延迟天数 |
| `status` | text | `ok` / `warning` / `failed` / `unknown` |
| `row_count` | integer nullable | MCP 返回行数，仅记录数量 |
| `error` | text nullable | 单指标查询失败原因 |
| `created_at` | datetime | 创建时间 |

不保存 `query_metric` 返回的完整数据行。若未来需要排障，可只保存脱敏后的样例和行数，但默认不做。

## 5. 指标周期配置

第一版建议用代码常量或 YAML 配置维护指标周期：

```yaml
metrics:
  sub_new_count:
    period_type: daily
    max_lag_days: 2
  sub_recharge_count:
    period_type: daily
    max_lag_days: 2
  exchange_rate:
    period_type: monthly
    max_month_lag: 0
```

配置字段：

| 字段 | 说明 |
|------|------|
| `period_type` | `daily` / `monthly` |
| `max_lag_days` | 日指标报警阈值，`lag_days >= max_lag_days` 时按配置进入 warning 或 failed |
| `max_month_lag` | 月指标允许落后月份数 |
| `lookback_days` | 日指标查询窗口，默认 14 天 |
| `severity` | 超阈值后默认 `warning` 或 `failed` |

未知指标处理：

- 默认 `period_type=unknown`。
- 进入 `warning`，提示需要为新指标补充周期配置。
- 不阻塞其他已知指标的新鲜度判断。

## 6. 统一摘要 JSON

示例：

```json
{
  "status": "failed",
  "checked_at": "2026-06-26T09:05:00+08:00",
  "catalog": {
    "status": "ok",
    "metric_count": 7,
    "new_metrics": [],
    "removed_metrics": []
  },
  "admission": {
    "status": "ok",
    "finding_count": 0
  },
  "freshness": {
    "status": "failed",
    "latest_daily_date": "2026-06-24",
    "latest_month": "2026-06",
    "stale_metrics": [
      "sub_new_count",
      "sub_recharge_count",
      "sub_recharge_money",
      "sub_deduction_count",
      "sub_deduction_money"
    ]
  }
}
```

## 7. 提醒去重

统一巡检应避免重复刷屏。

建议去重 key：

| 问题类型 | 去重 key |
|----------|----------|
| 指标目录变化 | `catalog:<type>:<metric_key>` |
| 准入异常 | `admission:<type>:<user_or_metric>` |
| 数据新鲜度异常 | `freshness:<metric_key>:<latest_period>` |
| 巡检自身失败 | `system:<stage>:<error_code>` |

第一版可以复用现有 admission health 的提醒时间字段，并在 `bi_readiness_runs.summary_json` 中记录上次通知状态。数据新鲜度异常如果 `latest_period` 长时间不变，仍应按固定节奏重新提醒，例如每日一次，避免长期断更只提醒一次后静默。若实现复杂，再新增 `readiness_finding_notifications` 表。

## 8. 数据保留

建议：

- `bi_readiness_runs` 保留 90 天。
- `metric_freshness_observations` 保留 90 天。
- `metric_catalog_snapshots` 可继续保留现有策略；如数据增长明显，可按 180 天清理。

本期文档只定义保留建议，不实现清理任务。

## 9. 迁移策略

实施时：

1. 新增表，不迁移旧记录。
2. 统一巡检第一次运行时建立 baseline。
3. 旧 `admission_health_runs` 继续保留，作为旧巡检历史。
4. 停旧 timer 不删除旧表。

回滚时：

- 停用 `bi-readiness-check.timer`。
- 重新启用旧 `bi-metric-snapshot.timer` 和 `bi-admission-health-check.timer`。
- 新表保留，不影响旧逻辑。
