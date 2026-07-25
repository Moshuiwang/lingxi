# BI Agent 指标分配闭环数据设计

## 1. 目标

指标分配闭环需要把“巡检发现的未分配指标”变成“审批群可处理的准入配置变更”。数据设计要支持：

- 查询当前指标和职能绑定状态。
- 把未分配指标绑定到指定职能。
- 记录谁做了分配、分配了什么、权限发布是否成功。
- 重复点击或重复命令不产生重复映射。

## 2. 现有事实源

| 数据 | 表/来源 | 说明 |
|------|---------|------|
| MCP 当前指标 | 指标快照 JSON / `metric_catalog_snapshots` | 由 `mcpmonall` 定时抓取 |
| 指标与职能绑定 | `function_metrics` | 公司 + 职能 + 指标 |
| 用户持有职能 | `user_functions` | 用户 + 公司 + 职能 |
| 用户权限发布副本 | 飞书多维表格 | MCP 读取的权限副本 |
| 巡检运行结果 | `admission_health_runs` | 记录本轮巡检状态和发现项 |
| 未分配指标状态 | `metric_assignment_status` | 巡检留痕与提醒节流；不作为分配是否生效的判断源 |

## 3. 绑定表

继续复用 `function_metrics`，不新增一套平行权限表。

### 3.1 `function_metrics`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | integer | 主键 |
| `company` | string | 逻辑公司名 |
| `function` | string | 职能，例如 `运营` |
| `metric_key` | string | MCP 指标 key |
| `created_at` | datetime | 创建时间 |

唯一约束：

```text
company + function + metric_key
```

这个唯一约束保证重复提交不会产生重复映射。

## 4. 审计表

当前仓库已有基础操作日志能力。指标分配应优先复用现有审计/操作日志表；如果现有字段不足，再新增结构化表。

### 4.1 推荐事件

事件名：

```text
metric_assignment
```

### 4.2 推荐字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `actor_open_id` | string | 操作者飞书 open_id |
| `actor_name` | string | 操作者名称，可为空 |
| `chat_id` | string | 操作发生的审批群 |
| `action` | string | `assign_metric_to_function` |
| `function` | string | 目标职能 |
| `metric_keys_json` | text | 本次提交的指标列表 |
| `companies_count` | integer | 本次影响的公司数量 |
| `created_count` | integer | 新增映射数量 |
| `existing_count` | integer | 已存在映射数量 |
| `publish_status` | string | `success` / `failed` / `skipped` |
| `publish_result_json` | text | 发布结果或失败原因 |
| `source` | string | `feishu_card` / `feishu_command` / `cli` |
| `request_id` | string | 飞书卡片/命令请求 ID |
| `created_at` | datetime | 操作时间 |

### 4.3 最小落库口径

如果暂不新增表，可以把上述字段写入现有操作日志的 `detail` JSON。最低要求是能追溯：

- 谁操作。
- 分给哪个职能。
- 分了哪些指标。
- 影响多少公司。
- 权限发布是否成功。

## 5. 查询视图口径

`@Bot 查看指标分配` 的文字回复需要合成两类数据：

1. 最新 MCP 指标快照。
2. `function_metrics` 中每个 `metric_key` 绑定过的普通职能集合。

输出字段：

| 字段 | 说明 |
|------|------|
| `metric_key` | 指标 key |
| `title` | 指标中文名或英文名 |
| `functions` | 已绑定普通职能列表 |
| `unassigned` | 是否未绑定任何普通职能 |

`全部` 不计入普通职能集合。

## 6. 分配写入口径

分配卡片提交时：

1. 读取当前已登记公司。
2. 对每个公司插入 `company + function + metric_key`。
3. 已存在的映射跳过。
4. 有新增映射时立即调用权限发布。
5. 写审计记录。

默认影响范围是所有已登记公司。后续可扩展公司范围选择，但第一版不做。

巡检消项以 `function_metrics` 实时重算为准。`metric_assignment_status` 只用于记录巡检看到的指标状态和提醒节流，不要求分配流程单独更新它。

## 7. 状态流转

```text
MCP 快照出现指标
  -> 巡检识别未绑定普通职能
  -> 审批群文字查询可见
  -> 管理员通过分配卡片提交
  -> 写入 function_metrics
  -> 发布权限副本
  -> 下一轮巡检按 function_metrics 重算后消项
```

## 8. 幂等规则

| 场景 | 结果 |
|------|------|
| 同一指标重复分配给同一职能 | 不新增映射，提示已存在 |
| 同一卡片重复提交 | 不重复写映射，审计可记录重复请求 |
| 权限发布失败后重试 | 不重复写映射，只重新发布权限 |
| 指标提交时已不在快照中 | 拒绝提交，提示快照已变化 |

## 9. 数据安全

- 不记录 MCP token。
- 不记录飞书 app secret。
- 不记录用户 SSH 私钥或密码。
- 审计只记录权限配置事实，不记录用户查询结果。
