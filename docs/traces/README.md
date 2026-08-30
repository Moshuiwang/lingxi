# Trace 执行载体（docs/traces/）

长期执行计划（Execution Trace）的**合同与执行状态**以本目录下受版本控制的文件为唯一正文；对应的 GitHub `[tracking]` Issue 降级为瘦指针与产品负责人裁定界面。方法论正文仍在 GitHub [#147](https://github.com/Moshuiwang/lingxi/issues/147)；Codex 与 Claude Code 共用 [Lingxi Trace 执行适配](../../.agents/skills/lingxi-trace/SKILL.md)，本目录只承载按该方法生成的具体 Trace。

## 准入

满足任一条件的工作需要建 Trace 目录：多 Epic / 多批次 / 顺序依赖 / 多编排者接力 / 高风险（部署、权限、数据）。单 Issue 可完成的工作不建 Trace，照常走 Issue。历史 Trace（#46～#304）不迁移，其留痕仍在原 Issue。

## 结构（每 Trace 一个子目录：`<issue号>-<中文短名>/`）

| 文件 | 内容 | 谁改、怎么改 |
| --- | --- | --- |
| `合同.md` | 六段式开工合同（目标 / 授权终点 / 不做清单 / 验收标准 / 成本预算 / 主线声明）+ Epic 与批次结构 + 完成定义 | 建立与修订一律走 PR；**PR 合并即产品负责人批准**。未写进合同的授权不存在 |
| `任务表.md` | 全部 Step 的复选框状态表，**状态即文件当前值** | 编排者随执行直接 commit 更新（无需 PR）；行格式 `- [x] S-X-N 一句话（关键指针）` |
| `验收.md` | 逐 Epic 可观察完成标准 + 证据等级目标/现值 + 关联[验收矩阵](../技术设计/验收矩阵.md) `V-*` 行 | 编排者随收口 commit 更新；证据等级口径见[验证与门禁](../技术设计/验证与门禁.md) |

## 与 Issue / 评论的分工（现行做法不变）

- `[tracking]` Issue 正文 = 瘦指针：目标一句话 + 三件套链接 + 当前阶段一行 + 原正文折叠留档。
- 产品负责人裁定评论、编排者交接评论、批次收口评论**仍发在该 Issue**；收口评论附任务表更新 commit 链接。
- 缺陷、工作项、决策留痕照常走各自 Issue；产品事实照常写 `docs/` 正文。
- Trace 只记录职责与当前运行映射：Codex 用新顶层 Task、Claude Code 用 tmux 命名会话。两种适配器共用同一三件套、Step ID、证据和 GitHub 留痕，禁止维护两套合同。
