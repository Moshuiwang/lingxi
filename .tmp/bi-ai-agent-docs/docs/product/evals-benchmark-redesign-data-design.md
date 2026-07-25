# Evals Benchmark 数据设计

更新时间：2026-06-18

## 一、原则

评测数据分三层：

| 层级 | 是否提交 | 说明 |
|------|----------|------|
| 候选题包 | 可提交 | AI 或模板生成，人工尚未确认 |
| 正式题库 | 可提交 | 人工确认后进入 benchmark |
| 原始运行报告 | 不提交 | 每次模型运行的完整回答、工具轨迹、成本 |

候选题和正式题库不得包含密钥、token、真实私密配置或完整内部路径。

## 二、Case Schema

正式 case 继续放在 `evals/cases/*.json`，从单题结构扩展为场景结构。

兼容旧字段：

| 字段 | 说明 |
|------|------|
| `id` | 全局唯一 |
| `name` | 场景名 |
| `question` | 单轮场景首问，兼容旧 runner |
| `pass_criteria` | 人工或 judge 评估时的通过标准 |
| `fail_criteria` | 失败标准 |

新增字段：

| 字段 | 说明 |
|------|------|
| `review_status` | `draft` / `approved` / `rejected` |
| `profile` | `simple` / `standard` / `deep` |
| `metric_keys` | 覆盖的指标 |
| `prompt_strategies` | 适用 prompt 策略 |
| `score_rubric` | 100 分制权重和扣分规则 |
| `expected_answer` | 真实标准答案或取数计划 |
| `conversation` | 多轮场景脚本 |
| `judge_notes` | 给人工或未来 judge 的判卷提示 |

兼容规则：

- 所有 case 必须保留非空 `question`。
- 多轮 case 的 `question` 必须等于 `conversation` 里的第一条 `role=user` 文本。
- `conversation` 至少包含一条 `role=user`。
- 没有 `conversation` 的旧 case 视为单轮场景。
- 正式 case 最多包含 3 条 `role=user` 轮次。

`conversation` 支持的 role：

| role | 必填字段 | 是否发送给模型 | 说明 |
|------|----------|----------------|------|
| `user` | `text` | 是 | 评测脚本中的用户输入 |
| `expected_agent` | `expect` | 否 | 期待模型在上一轮后的行为，用于评分 |

本期不支持运行时动态生成用户补充回答。

## 三、多轮场景

多轮场景使用 `conversation` 表达。

```json
{
  "conversation": [
    {"role": "user", "text": "看看最近充值情况怎么样"},
    {"role": "expected_agent", "expect": "追问时间、公司和充值口径"},
    {"role": "user", "text": "查卢旺达 2026 年 4 月 6 日到 4 月 12 日充值用户数"}
  ]
}
```

runner 实际执行时只把 `role=user` 的轮次发送给模型；`expected_agent` 用于评分。

## 四、标准答案

标准答案由真实工具结果和指标字典口径共同构成。

| 字段 | 说明 |
|------|------|
| `source` | `mcp_query_metric` / `metric_dictionary` / `manual` |
| `metric_key` | 指标 |
| `date_range` | 固定日期 |
| `company_ids` | 固定公司 |
| `group_by` | 分组 |
| `filters` | 过滤条件 |
| `fixture` | 可选，指向固定答案文件 |
| `notes` | 人工确认口径 |
| `verification_status` | `ready` / `needs_fixture` / `blocked_by_mcp` |

AI 不得自行编写真实数字。AI 只能生成标准答案取数计划和评分建议；真实数字来自固定账号、固定日期、固定 MCP 环境下的查询结果。

真实业务数字提交策略：

- `evals/reports/` 的完整原始回答和明细默认不提交。
- 正式题库可提交标准答案摘要，但只能保存足够判卷的最小数字，不保存大明细。
- 金额、收入类、税率和汇率 fixture 默认先走人工确认；若产品或数据认为敏感，则只提交哈希/摘要和取数计划，真实数字保留在本地报告或飞书确认记录。
- 任何 full-access 账号、token、MCP URL 密钥或 provider key 都不得进入题库或 fixture。

## 五、评分结果

每个 case 运行后生成：

| 字段 | 说明 |
|------|------|
| `score_total` | 总分 |
| `score_breakdown` | 五个维度分数 |
| `score_source` | `suggested` / `human` / `judge_agent` / `mixed` |
| `status` | `PASS` / `PARTIAL` / `FAIL` / `ERROR` / `PENDING_EVAL` |
| `judge_suggestion` | 自动打分建议，可为空 |
| `human_score` | 人工确认分，可为空 |
| `failure_tags` | 标准失败标签，见下表 |

失败标签：

| 标签 | 是否强制 FAIL | 说明 |
|------|---------------|------|
| `wrong_number` | 是 | 关键数字错误 |
| `wrong_metric` | 是 | 指标替代、口径错误或错误聚合 |
| `unsafe` | 是 | 越权、泄密或错误解释权限问题 |
| `no_convergence` | 是 | 3 个用户轮次内没有收敛到可用答案或正确拒绝 |
| `over_refusal` | 否 | 可以回答却过度保守拒绝或反复确认 |
| `cost_inefficient` | 否 | 明显不合理地消耗 token、工具调用或大上下文 |
| `tool_misuse` | 否 | 使用了不合适的 MCP 工具或非预期本地工具 |
| `blocked_by_mcp` | 否 | 当前 MCP 暂不支持标准答案取数 |

## 六、矩阵运行结果

矩阵维度：

- provider
- model
- prompt_strategy
- effort
- profile
- case 文件

报告必须聚合：

- 平均分。
- PASS/PARTIAL/FAIL/ERROR 数。
- PENDING_EVAL 数；不纳入正式 pass_rate 分母。
- score_source 分布；混合来源行标为 `mixed`。
- 总成本和单题均价。
- 平均耗时。
- 平均轮数。
- 平均工具调用数。
- 失败类型分布。

## 七、人工闸门状态

候选题包放在 `evals/review_queue/`，文件名建议：

```text
<date>-<profile>-<metric-scope>.json
<date>-<profile>-<metric-scope>.md
```

确认后由命令写入正式题库，并把 `review_status` 改为 `approved`。

本期不新增数据库表；所有评测状态通过 JSON 文件和报告产物管理。
