# Claude 执行链路的已验证事实与管道投递风险

> 来源：前身项目 [startimes-bi/bi-ai-agent#122](https://github.com/startimes-bi/bi-ai-agent/issues/122) 的验收记录、根因调研与生产复现，Lingxi [Issue #23](https://github.com/Moshuiwang/lingxi/issues/23) 在 `biai-stage` 的受控验证，以及 Claude Agent SDK 默认 transport 的公开源码。
> 核对日期：2026-07-28。本文只保留对 Lingxi 执行层选型仍然有效的结论，不复制该 Issue 的过程讨论。

## 已验证成立：百炼兼容端点可以承载 Agent 会话

前身项目在真实百炼兼容端点上完成过一次可回读的协议层验收（该项目自身记为 L5）：

- 7 个有效回合，其中新建会话 4 次、`resume` 恢复会话 3 次；
- 每个回合都观察到会话标识、助手文本、终止 `result` 事件，以及 1 次只读 MCP 工具调用与 1 次对应回执；
- 无效 JSON 0 次、工具错误 0 次、终止事件缺失 0 次；
- 另有 1 批 10 轮因进程结束后未回传最终摘要，按其验收合同记为 UNKNOWN，未计入通过。

**对 Lingxi 的意义**：会话启动、会话恢复、MCP 调用与终止事件这四项能力在百炼兼容端点上已有可复验证据，不需要在 Lingxi 重跑。模型提供方不是这条链路的主要风险来源。

## 已验证不成立的归因：空结果不是百炼的问题

该 Issue 的标题把空结果归因于百炼，但其后续根因调研与生产复现推翻了这一归因：

- 2026-07-24 的一次生产失败中，run 以「终止事件缺失」结束，最终文本为空，用户侧卡片没有内容；
- 但同一 Claude 会话的本地转录在同一时间窗口内记录了完整的用户消息、一次工具调用及回执，以及一条 209 字的助手文本；
- 即模型已经生成答案并落盘，只是承载进程没有从 `stream-json` 通道收到该文本和终止事件。

因此这是**投递通道的缺陷，不是模型端点的缺陷**。把模型调用指向百炼只替换了提供方，不改变本地这段输出处理逻辑。

上游 `anthropics/claude-code` 有跨版本、多次独立报告的同类现象：`--print --output-format stream-json` 在 stdout 被重定向为管道（非 TTY）时，可能在会话已内部完成的情况下始终不把终止事件写到 stdout。最小复现形式是 `claude -p "hello" | cat` 无输出而 `claude -p "hello"` 正常，指向 CLI 对 stdout 是否为 TTY 的检测与刷新路径。这些报告未见官方修复确认。

## 已验证：Lingxi 选定的 Agent SDK 双向流式路径未复现该缺陷

Lingxi 的执行层选型是 Python Claude Agent SDK。其默认 transport（`_internal/transport/subprocess_cli.py`）的公开源码显示，它同样以子进程方式拉起 `claude` CLI，并把 stdout 配置为管道：

```python
cmd = [self._cli_path, "--output-format", "stream-json", "--verbose"]
cmd.extend(["--input-format", "stream-json"])
self._process = await anyio.open_process(cmd, stdin=PIPE, stdout=PIPE, ...)
```

即「改用 Agent SDK 而不是裸 CLI」并不改变非 TTY 管道 stdout 这一触发条件，但 Agent SDK 使用的双向流式模式与前身项目及上游复现的 `-p` 一次性模式不同。

2026-07-28，Lingxi 在 `biai-stage` 使用 Python Claude Agent SDK `0.2.128`、SDK 自带 Claude Code CLI `2.1.220`、真实百炼兼容端点和真实只读 `bi-metric` MCP 完成 [Issue #23](https://github.com/Moshuiwang/lingxi/issues/23) 受控验证：

- 先执行 1 个连通回合，再执行唯一一轮 39 个正式回合，未补跑或重跑；
- 40/40 回合均取得非空最终正文和唯一终止结果；
- 正式回合 39/39 的最终助手正文与终止结果长度、哈希一致，最长正文 13,049 字节；
- 覆盖独立新会话、三条各 6 回合的 `resume`、两条交替恢复会话、长回答和多次真实 MCP 调用；
- 未观察到正文为空、截断、跨会话串线或终止事件缺失。

因此，**Agent SDK 双向流式非 TTY 路径的正文与终止事件投递在该固定版本和端点组合下成立**。这不抹去前身项目 `-p` 模式的真实故障，也不证明所有未来版本或所有异常路径都不会回归；Lingxi 应保留终止事件缺失监控和禁止对已有副作用回合自动重放的安全边界。

同次验证还确认受控 Skill、`PreToolUse`、`PostToolUse` 和 `Stop` 可采集，但暴露了两个问题：`PostToolUseFailure` 全轮 0 次，以及允许的只读 MCP 工具配置没有阻止未明确禁用的 `CronCreate` 被执行。这两项由下一节的定向补测回答。

## 已验证：审计采集与只读边界的机制可行，但必须由执行层自己实现

2026-07-28 在同一环境执行 6 个回合的定向补测（`disallowed_tools` 完全为空，`PreToolUse` hook 为唯一屏障）：

- **`PreToolUse` 返回拒绝是执行前的真阻止**。3 次非白名单调用（`Bash`、`Write`、`CronCreate`）全部被拒且均无 `PostToolUse`；文件副作用探针确认目标文件未被创建，`CronCreate` 被拒后 `session_crons` 为空、无 cron 文件残留。被拒回合仍取得非空正文和恰好一次终止结果，同回合内白名单工具照常执行。
- **真实工具执行失败可以采集**。MCP 协议层 `isError=true` 与 MCP 进程调用中退出两种失败，2/2 触发 `PostToolUseFailure` 且 `PostToolUse` 为 0，回调带工具名、入参与错误文本。
- **权限拒绝完全静默**。拒绝后 `PostToolUseFailure`、`PermissionDenied`、`PermissionRequest`、`PostToolUse` 均为 0。同批注册了 SDK 类型未声明的事件名并确认 CLI 会透传（`PostToolBatch` 实际触发 7 次），因此 0 次不是「没注册上」。拒绝在 SDK 消息流中仍可采集：每次拒绝产生一个 `is_error=True` 的工具结果块。

由此得到两条对实现的硬约束：

1. **审计链必须合成**。`PostToolUseFailure` 的语义是「工具抛错」，不是「用户没拿到结果」——被 MCP 包成正常响应的业务失败（如「指标不存在」）不会触发它。要审计用户实际是否拿到结果，必须额外解析工具回执内容；要审计拒绝，必须在自己的 `PreToolUse` 里记账。**不需要为此改用 TypeScript SDK。**
2. **只读边界必须由执行层强制**：默认拒绝、只放行显式白名单，新增内置工具自动落入拒绝分支；不得依赖 `allowed_tools`、`disallowed_tools` 或提示词约束。

一项副产品结论：**「配置一个不可达的 MCP server」不能用来构造工具执行失败**——不可达的 server 在初始化时状态为 `failed`，其工具根本不出现在工具清单里，模型无法调用。要构造真实执行失败，必须用能正常握手注册、仅在被调用时失败的 server。

## 若以后回归，可选的恢复方向

按可靠性排序，供以后确认再次出现正文或终止事件丢失时评估，均未在 Lingxi 验证：

1. **官方支持的补投**：`-p --resume <session-id> --output-format json` 向已有会话追加一次「只重述上次最终答案、禁用工具与 MCP」的请求，按结构化返回投递。这是官方文档写明的脚本消费接口，不依赖内部文件格式；代价是产生一次新的模型调用，不保证逐字一致。
2. **伪终端包裹**：用 `script -qefc` 之类使 CLI 认为 stdout 连接的是 TTY，绕开该检测路径。需要评估终端控制字符带来的解析复杂度。
3. **不建议**：直接解析本地会话 JSONL 转录。官方明确声明该格式是内部实现、跨版本不保证兼容并劝阻直接解析；且 Lingxi 的 worker 按用户 uid 启动子进程、以文件权限作为隔离边界，读取他人家目录下的转录需要跨 Unix 用户读权限——前身项目正是在同类跨用户边界上出过生产故障。

## 安全边界

无论采用哪种恢复方向，都不得对已产生工具副作用的回合自动重放：「CLI 完成但没有终止事件」这一状态本身无法区分「什么都没发生」和「副作用已经真实发生」。仅在本回合完全没有观察到任何工具调用时，才允许一次有界恢复。
