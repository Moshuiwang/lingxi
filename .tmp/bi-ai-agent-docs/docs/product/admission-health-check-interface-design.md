# BI Agent 每小时准入巡检接口设计

## 1. 命令行入口

```bash
.venv/bin/python scripts/check_admission_health.py [options]
```

参数：

| 参数 | 说明 |
|------|------|
| `--metric-snapshot <path>` | MCP `list_metrics` 结果 JSON 文件 |
| `--dry-run` | 只输出结果，不推飞书 |
| `--notify` | 有变化/异常时推送审批群 |
| `--force-notify` | 即使无变化也推送摘要，便于验收 |
| `--json` | 输出机器可读 JSON |
| `--work-hours-only` | 只在北京时间工作时段运行 |

默认行为：

- 读取环境变量指定的快照路径。
- 工作时段内运行。
- 只有有变化或异常时推送。

## 2. 环境变量

| 变量 | 说明 |
|------|------|
| `ADMISSION_HEALTH_METRIC_SNAPSHOT` | MCP 指标快照 JSON 路径 |
| `ADMISSION_HEALTH_APPROVAL_CHAT_ID` | 审批群 chat_id |
| `ADMISSION_HEALTH_ENABLED` | 是否启用自动巡检，默认启用 |
| `ADMISSION_HEALTH_TZ` | 默认 `Asia/Shanghai` |
| `ADMISSION_HEALTH_START_HOUR` | 默认 `9` |
| `ADMISSION_HEALTH_END_HOUR` | 默认 `19` |
| `ADMISSION_HEALTH_REMINDER_HOURS` | 未处理项重复提醒间隔，默认 `4` |
| `ADMISSION_HEALTH_MAX_SNAPSHOT_AGE_HOURS` | MCP 指标快照最大年龄，默认 `2` |

## 3. MCP 指标快照输入

支持两种格式。

### 3.1 MCP 原始列表

```json
[
  {
    "metric_id": "sub_recharge_count",
    "name": "充值用户数",
    "description": "...",
    "parameters": ["start_date", "end_date", "company_ids"],
    "dimensions": ["date", "company"]
  }
]
```

如果原始 MCP 输出没有 `parameters` 或 `dimensions`，巡检只比较名称和描述，不声明参数/维度无变化。

### 3.2 规范化列表

```json
{
  "source": "mcpmonall:list_metrics",
  "generated_at": "2026-06-12T10:00:00+08:00",
  "metrics": [
    {
      "metric_key": "sub_recharge_count",
      "name_cn": "充值用户数",
      "name_en": "Subscription Recharge Users",
      "description": "...",
      "dimensions": ["date", "company"]
    }
  ]
}
```

脚本需要把 `metric_id` / `metric_key` 统一成 `metric_key`。

## 4. JSON 输出

```json
{
  "status": "success",
  "notified": true,
  "metric_findings": [
    {
      "type": "unassigned_metric",
      "metric_key": "sub_recharge_money",
      "title": "充值金额",
      "user_impact": "已有运营/财务用户不会看到该指标",
      "suggested_action": "确认绑定到运营、财务、全部，或暂不开放"
    }
  ],
  "account_findings": [
    {
      "type": "approved_user_without_token",
      "email": "user@example.com",
      "user_impact": "用户无法通过 MCP 鉴权查数",
      "suggested_action": "补签 MCP token 并重新发布权限"
    }
  ]
}
```

## 5. 飞书审批群消息

### 5.1 指标待分配消息

```text
发现 2 个 BI 指标待分配职责

1. sub_recharge_money / 充值金额
   状态：未绑定任何普通职责
   用户影响：已有运营/财务用户不会自动看到
   建议动作：确认开放给运营、财务、全部，或暂不开放

2. sub_deduction_money / 扣费金额
   状态：未绑定任何普通职责
   用户影响：普通用户不可见
   建议动作：确认职责归属
```

### 5.2 账号健康异常消息

```text
BI Agent 账号准入异常 2 项

1. user@example.com 已批准但没有 MCP token
   用户影响：登录后无法查数
   建议动作：补签 token 并重新发布权限

2. ops@example.com 权限展开为空
   用户影响：list_metrics 为空
   建议动作：确认公司/职责是否已绑定指标
```

## 6. 后续交互接口

第一版可以只提醒，不在卡片里直接修改权限。

后续可扩展审批群命令：

```text
绑定指标 sub_recharge_money 到 运营
忽略指标 sub_test_metric 原因 测试指标不开放
查看未分配指标
```

这些命令必须走管理员权限校验，并在绑定后触发权限发布。

## 7. systemd 入口

建议先部署快照生产服务：

```ini
[Unit]
Description=BI Agent metric snapshot capture

[Service]
Type=oneshot
WorkingDirectory=/home/wangzhipeng/projects/bi-ai-agent
EnvironmentFile=/home/wangzhipeng/projects/bi-ai-agent/.env
ExecStart=/home/wangzhipeng/projects/bi-ai-agent/.venv/bin/python scripts/capture_metric_snapshot.py --output ${ADMISSION_HEALTH_METRIC_SNAPSHOT}
```

再部署准入巡检消费服务：

```ini
[Unit]
Description=BI Agent admission health check

[Service]
Type=oneshot
WorkingDirectory=/home/wangzhipeng/projects/bi-ai-agent
EnvironmentFile=/home/wangzhipeng/projects/bi-ai-agent/.env
ExecStart=/home/wangzhipeng/projects/bi-ai-agent/.venv/bin/python scripts/check_admission_health.py --notify --work-hours-only
```

两个服务均使用小时级 timer：

```ini
[Timer]
OnCalendar=Mon..Fri *:00:00
Persistent=true
```

脚本内部再按北京时间工作时段判断，避免服务器时区差异造成误发。
