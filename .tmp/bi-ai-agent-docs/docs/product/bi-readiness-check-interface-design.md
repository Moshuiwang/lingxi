# BI Agent 统一巡检接口设计

> 日期：2026-06-26
> 状态：待实现
> 对应 PRD：`docs/product/bi-readiness-check-prd.md`

## 1. 命令行入口

建议新增统一入口：

```bash
.venv/bin/python scripts/check_bi_readiness.py [options]
```

参数：

| 参数 | 说明 |
|------|------|
| `--dry-run` | 只输出结果，不推飞书 |
| `--notify` | 有异常或达到通知条件时推飞书 |
| `--force-notify` | 即使无异常也推送完整摘要 |
| `--json` | 输出机器可读 JSON |
| `--work-hours-only` | 只在北京时间工作时段运行 |
| `--skip-catalog` | 跳过指标目录阶段，仅用于排障 |
| `--skip-admission` | 跳过准入阶段，仅用于排障 |
| `--skip-freshness` | 跳过数据新鲜度阶段，仅用于排障 |
| `--metric-snapshot <path>` | 复用或指定指标目录快照 |
| `--freshness-lookback-days <n>` | 数据新鲜度日指标查询窗口，默认 14 |

默认自动巡检不应跳过任何阶段。

## 2. 环境变量

| 变量 | 说明 |
|------|------|
| `BI_READINESS_ENABLED` | 是否启用统一巡检，默认启用 |
| `BI_READINESS_CHAT_ID` | 统一巡检消息目标群；缺省可复用审批群 |
| `BI_READINESS_TZ` | 默认 `Asia/Shanghai` |
| `BI_READINESS_START_HOUR` | 默认 `9` |
| `BI_READINESS_END_HOUR` | 默认 `19` |
| `BI_READINESS_MAX_DAILY_LAG_DAYS` | 日指标默认最大延迟，默认 `2` |
| `BI_READINESS_LOOKBACK_DAYS` | 日指标查询窗口，默认 `14` |
| `BI_READINESS_FORCE_NOTIFY_OK` | 是否定时发送正常摘要，默认否 |
| `ADMISSION_HEALTH_METRIC_SNAPSHOT` | 兼容旧指标目录快照路径 |
| `ADMISSION_HEALTH_MAX_SNAPSHOT_AGE_HOURS` | 兼容旧快照过期阈值 |

环境变量不得包含 token 明文。MCP 凭证继续由监控账号自己的 `.mcp.json` 管理。

## 3. MCP 调用接口

### 3.1 指标目录

使用全量监控账号调用：

```text
list_metrics
```

输出规范化为：

```json
{
  "source": "mcpmonall:list_metrics",
  "generated_at": "2026-06-26T09:00:00+08:00",
  "metrics": [
    {
      "metric_key": "sub_recharge_count",
      "name_cn": "充值用户数",
      "name_en": "Recharge User Count"
    }
  ]
}
```

### 3.2 数据新鲜度

日指标查询建议：

```json
{
  "metric_id": "sub_recharge_count",
  "group_by": ["date"],
  "filters": {
    "start_date": "20260613",
    "end_date": "20260626"
  }
}
```

判断方式：

- 找到返回行中最新的 `date`。
- 若窗口内无行，状态为 `unknown` 或 `failed`，取决于该指标是否应该每日有数据。
- 不在客户端保存完整数据行，只记录最新日期和行数。
- 实现前必须用当前生产 MCP 的 `filters` schema 复核请求体，不得复用旧版顶层日期参数。

月指标查询建议：

- 优先使用 MCP 对该指标支持的时间字段。
- 若 MCP 只返回最新月份，记录 `latest_month`。
- 若 MCP schema 不明确，第一版可把该指标标为 `unknown`，提示需要补充周期配置。

## 4. JSON 输出

```json
{
  "status": "failed",
  "checked_at": "2026-06-26T09:05:00+08:00",
  "catalog": {
    "status": "ok",
    "metric_count": 7,
    "findings": []
  },
  "admission": {
    "status": "ok",
    "findings": []
  },
  "freshness": {
    "status": "failed",
    "metrics": [
      {
        "metric_key": "sub_recharge_count",
        "name": "充值用户数",
        "period_type": "daily",
        "latest_period": "2026-06-24",
        "lag_days": 2,
        "status": "failed"
      }
    ]
  },
  "message": "BI Agent 巡检发现数据新鲜度异常"
}
```

## 5. 飞书消息接口

第一版使用文本消息，不做卡片。

原因：

- 巡检重点是可读摘要和行动建议。
- 卡片会引入状态更新、按钮权限和过期处理，超过本期文档目标。
- 后续如果需要“确认已知悉”“创建处理任务”，再单独做卡片。

### 5.1 异常消息模板

```text
BI Agent 巡检发现异常

数据新鲜度：异常
5 个日指标最新只到 2026-06-24，已落后 2 天：
- 新增用户数
- 充值用户数
- 充值金额
- 扣费用户数
- 扣费金额

权限准入：正常
指标目录：正常，当前 7 个指标

用户影响：
用户查询 2026-06-25 或 2026-06-26 的日指标时，可能查不到数据或得到空结果。

建议动作：
联系数据/MCP 侧确认 ETL 或上游同步；对外说明当前日指标最新到 2026-06-24。
```

### 5.2 正常强制摘要

```text
BI Agent 巡检无异常

指标目录：正常，当前 7 个指标
权限准入：正常
数据新鲜度：正常
- 日指标最新到 2026-06-24
- 月指标最新到 2026-06

巡检时间：2026-06-26 09:05
```

## 6. systemd 接口

目标服务：

```ini
[Unit]
Description=BI Agent unified readiness check

[Service]
Type=oneshot
User=wangzhipeng
Group=wangzhipeng
WorkingDirectory=/home/wangzhipeng/projects/bi-ai-agent
ExecStart=/home/wangzhipeng/projects/bi-ai-agent/.venv/bin/python scripts/check_bi_readiness.py --notify --work-hours-only
```

目标 timer：

```ini
[Unit]
Description=Run BI Agent unified readiness check hourly

[Timer]
OnCalendar=*:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

部署时应避免和旧 `bi-admission-health-check.timer` 同一时间重复发消息。灰度期可让统一巡检只 `--dry-run` 或发到测试群。

## 7. 手工排障命令

```bash
.venv/bin/python scripts/check_bi_readiness.py --dry-run --json
.venv/bin/python scripts/check_bi_readiness.py --dry-run --skip-freshness
.venv/bin/python scripts/check_bi_readiness.py --dry-run --force-notify
```

排障输出必须脱敏，不展示 token、`.env`、私钥或完整业务明细。

## 8. 兼容策略

- 旧 `scripts/check_admission_health.py` 保留至少一个版本周期。
- 旧 `scripts/capture_metric_snapshot.py` 可继续作为统一巡检内部实现的一部分。
- 旧环境变量继续支持，但新文档以 `BI_READINESS_*` 为统一命名。
- 旧 timer 停用前，必须确认统一巡检已覆盖旧异常消息。
