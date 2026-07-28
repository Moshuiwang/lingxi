# Claude 执行链路的已验证事实与管道投递风险

> 来源：前身项目 [startimes-bi/bi-ai-agent#122](https://github.com/startimes-bi/bi-ai-agent/issues/122) 的验收记录、根因调研与生产复现，以及 Claude Agent SDK 默认 transport 的公开源码。
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

## 尚未验证：Lingxi 选定的 Agent SDK 是否继承该缺陷

Lingxi 的执行层选型是 Python Claude Agent SDK。其默认 transport（`_internal/transport/subprocess_cli.py`）的公开源码显示，它同样以子进程方式拉起 `claude` CLI，并把 stdout 配置为管道：

```python
cmd = [self._cli_path, "--output-format", "stream-json", "--verbose"]
cmd.extend(["--input-format", "stream-json"])
self._process = await anyio.open_process(cmd, stdin=PIPE, stdout=PIPE, ...)
```

即「改用 Agent SDK 而不是裸 CLI」并不改变触发条件——非 TTY 管道 stdout 是同一个。

**但存在一处未确认的差异，不得当作已知**：前身项目与上游复现用的都是 `-p` 一次性 print 模式，而 Agent SDK 用的是 `--input-format stream-json` 的双向流式模式。这两种模式是否共享同一刷新缺陷，目前没有证据，也不推测。这正是 Lingxi 开工门禁第一条要回答的问题。

## 若缺陷成立，可选的恢复方向

按可靠性排序，供门禁失败时评估，均未在 Lingxi 验证：

1. **官方支持的补投**：`-p --resume <session-id> --output-format json` 向已有会话追加一次「只重述上次最终答案、禁用工具与 MCP」的请求，按结构化返回投递。这是官方文档写明的脚本消费接口，不依赖内部文件格式；代价是产生一次新的模型调用，不保证逐字一致。
2. **伪终端包裹**：用 `script -qefc` 之类使 CLI 认为 stdout 连接的是 TTY，绕开该检测路径。需要评估终端控制字符带来的解析复杂度。
3. **不建议**：直接解析本地会话 JSONL 转录。官方明确声明该格式是内部实现、跨版本不保证兼容并劝阻直接解析；且 Lingxi 的 worker 按用户 uid 启动子进程、以文件权限作为隔离边界，读取他人家目录下的转录需要跨 Unix 用户读权限——前身项目正是在同类跨用户边界上出过生产故障。

## 安全边界

无论采用哪种恢复方向，都不得对已产生工具副作用的回合自动重放：「CLI 完成但没有终止事件」这一状态本身无法区分「什么都没发生」和「副作用已经真实发生」。仅在本回合完全没有观察到任何工具调用时，才允许一次有界恢复。
