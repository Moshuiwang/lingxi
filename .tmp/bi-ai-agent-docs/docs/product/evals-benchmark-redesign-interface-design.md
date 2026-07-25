# Evals Benchmark 接口设计

更新时间：2026-06-18

## 一、CLI

### 生成候选题

```bash
python3 evals/generate_cases.py \
  --profile deep \
  --metrics docs/mcp/metrics-dictionary.md \
  --output evals/review_queue
```

参数：

| 参数 | 说明 |
|------|------|
| `--profile` | `simple` / `standard` / `deep` |
| `--metrics` | 指标字典路径 |
| `--metric-key` | 可选，只为指定指标生成 |
| `--output` | 候选题包输出目录 |
| `--case-prefix` | ID 前缀，默认按 profile 生成 |
| `--validate-only` | 只校验指标字典解析和生成计划，不写候选题 |

输出：

- `*.json`：结构化候选题。
- `*.md`：适合发到飞书审批群的摘要。

### 人工闸门通过

```bash
python3 evals/approve_cases.py \
  evals/review_queue/20260618-deep-all.json \
  --output evals/cases/generated_deep.json
```

行为：

- 校验候选题结构。
- 过滤 `review_status=rejected`，这些题视为人工明确排除。
- 阻塞仍为 `review_status=draft` 或缺失审核状态的题。
- 写入正式题库时把 `review_status` 设为 `approved`。

### 运行 benchmark

```bash
python3 evals/run_benchmark.py \
  --phase generated_deep.json \
  --provider deepseek-pro \
  --prompt-strategy minimal \
  --effort max
```

候选题人工闸门前可做离线预检：

```bash
python3 evals/run_benchmark.py \
  --case-file evals/review_queue/20260618-deep-all.json \
  --include-draft \
  --validate-only
```

新增参数：

| 参数 | 说明 |
|------|------|
| `--case-file` | 直接指定候选题或正式题 JSON 文件；用于人工闸门前预检 |
| `--prompt-strategy` | `bare` / `minimal` / `full_context` |
| `--include-draft` | 默认不跑 draft case，显式打开后可试跑候选题 |
| `--validate-only` | 只校验 provider、prompt strategy、case schema 和题库状态，不调用模型 |

### 生成排名

```bash
python3 evals/score_report.py evals/reports/*_summary.json \
  --output evals/reports/ranking.md
```

候选题草稿做矩阵预检时：

```bash
python3 evals/run_matrix.py \
  --case-file evals/review_queue/20260618-deep-all.json \
  --include-draft \
  --smoke
```

输出：

- 质量排名。
- 性价比排名。
- 成本表。
- 失败类型分布。

## 二、飞书审批群口径

本期飞书只做通知和闸门，不做复杂编辑。

候选题消息内容：

```text
【Benchmark 候选题闸门】
profile=deep
指标数=7
候选场景数=...

请确认：
1. 题目是否像真实用户会问的话。
2. 评分标准是否能判断好坏。
3. 标准答案取数计划是否可信。

需要修改时，回 Codex 工作台改候选题文件。
```

报告消息内容：

```text
【Benchmark 评测报告】
phase=generated_deep.json
模型矩阵=DeepSeek Pro / Flash
Prompt=bare / minimal / full_context

质量排名：...
性价比排名：...
失败高发类型：...
报告路径：...
```

## 三、报告字段

排名报告至少包含：

| 字段 | 说明 |
|------|------|
| provider | provider 名称 |
| model | 模型名 |
| prompt_strategy | prompt 策略 |
| effort | 思考深度 |
| cases | case 数 |
| scored_cases | 已评分 case 数 |
| score_coverage | 已评分覆盖率 |
| avg_score | 平均分 |
| score_source | `suggested` / `human` / `judge_agent` / `mixed` |
| pass_rate | 通过率 |
| total_cost_usd | 总成本 |
| cost_per_case | 单 case 成本 |
| avg_duration_s | 平均耗时 |
| avg_turns | 平均轮数 |
| avg_tool_calls | 平均工具调用数 |
| failure_tags | 失败类型分布 |

当同一矩阵行里混合不同分数来源时，`score_source=mixed`，并展示来源占比。`PENDING_EVAL` 不纳入正式 `pass_rate` 分母，但必须计入总 case 数。

排名表必须展示 `scored_cases/cases`。当覆盖率不足时，质量和性价比排名只能作为预览，不能作为正式模型结论。端到端 `pass_rate` 包含 `ERROR`，因为超时和工具链失败同样影响用户是否能拿到答案。

## 四、兼容性

- 旧 `phase1.json` 没有 `conversation` 时仍按单轮执行。
- 新多轮 case 仍必须保留 `question`，并与第一条用户轮次一致。
- 旧 `status` 人工评估流程继续可用。
- 旧 baseline 工具继续保留，只是后续可读取 score 字段。
- 默认不强制 Feishu 环境变量，避免本地离线预检依赖飞书。
