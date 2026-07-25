# Codex 私聊机器人接口设计

> 日期：2026-06-13
> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。下文不是当前生产部署、验收或排障指引。
> 关联文档：`docs/product/codex-private-chat-prd.md`、`docs/architecture/codex-private-chat-architecture.md`

## 一、接口范围

这里的接口包括三类：

- 飞书事件入口。
- 模块内部服务接口。
- 飞书 CardKit 与 Codex CLI 外部调用接口。

第一版不新增面向外部用户的 HTTP API。现有 FastAPI `/health` 保留。

## 二、飞书消息事件入口

### 用途

接收现有飞书长连接消息事件，判断是否进入 Codex 私聊。

### 入参

| 字段 | 来源 | 用途 |
|---|---|---|
| `chat_type` | 飞书事件 | 正式 Codex 只处理 `p2p`；群聊不新增 Codex 触发 |
| `chat_id` | 飞书事件 | 记录私聊来源 |
| `message_id` | 飞书事件 | 幂等去重 |
| `root_id` | 飞书事件 | 仅在 `thread_id` 存在时作为话题别名辅助；单独出现不触发话题 |
| `parent_id` | 飞书事件 | 仅用于排障或未来扩展；单独出现不触发话题 |
| `thread_id` | 飞书事件 | 作为飞书原生话题的唯一触发字段和主 scope |
| `sender.open_id` | 飞书事件 | 权限判断 |
| `sender.tenant_key` | 飞书事件 | 保留上下文 |
| `content.text` | 飞书事件 | 用户消息或 `/new` |

### 出参

事件 handler 不直接返回用户内容，只快速 ACK 飞书事件。

### 成功行为

- 王志鹏普通私聊：提交 Codex 私聊后台任务。
- 王志鹏私聊中的飞书原生话题：提交 Codex 话题后台任务。
- 其他用户或其他场景：继续原 BI AI Agent 路由。

### 失败行为

- 字段缺失：不进入 Codex 私聊，走原路由或忽略。
- 重复 message_id：不重复执行。

### 用户可见影响

王志鹏看到 Codex 回复；在飞书话题中提问时，回复回到同一个话题；其他用户体验不变。

## 三、身份与路由接口

```text
should_route_to_codex_chat(event) -> RouteDecision
```

### 用途

判断当前消息是否进入 Codex 私聊模块。

### 入参

| 字段 | 说明 |
|---|---|
| `open_id` | 发送人 open_id |
| `chat_type` | 是否私聊；群聊不进入 Codex |
| `text` | 用户文本 |
| `message_context` | 飞书消息上下文，包含 message/root/parent/thread/chat |

### 出参

| 字段 | 说明 |
|---|---|
| `matched` | 是否进入 Codex 私聊 |
| `reason` | `entitled_user` / `not_entitled_user` / `not_private_chat` / `application_hidden` |
| `command` | `/new` 或 `message` |
| `reply_target` | `private` 或 `feishu_thread` |
| `application_visible` | 是否允许展示申请入口，第一版固定 `false` |

### 成功行为

只要用户在 Codex 私聊权利表中为 `active` 且 `chat_type=p2p`，进入 Codex 私聊。第一版权利表中只有王志鹏。

如果 `chat_type!=p2p`，即使消息里有 `@Bot`，也不进入 Codex。当前审批群/测试群命令路由保持原样。

### 失败行为

返回不命中，不抛给用户。

### 用户可见影响

无权限用户继续原机器人路由，不看到新模块提示。

`application_visible` 是由 `CODEX_CHAT_APPLICATION_ENABLED` 推导出的返回值，不落库。

## 四、隐藏申请入口接口

```text
get_codex_chat_application_entry(open_id) -> ApplicationEntryDecision
```

### 用途

为未来开放申请预留接口。第一版必须返回隐藏，不向用户展示任何 Codex 私聊申请入口。

### 入参

| 字段 | 说明 |
|---|---|
| `open_id` | 飞书用户 open_id |

### 出参

| 字段 | 说明 |
|---|---|
| `visible` | 第一版固定 `false` |
| `reason` | `feature_hidden` / `already_entitled` / `can_apply` |
| `entry_text` | 未来入口文案，第一版为空 |

### 成功行为

第一版始终隐藏入口。

### 失败行为

如果配置读取失败，按隐藏处理。

### 用户可见影响

当前没有任何用户看到申请入口。未来打开申请开关后，普通用户才会看到或触发申请路径。

`visible` 是由 `CODEX_CHAT_APPLICATION_ENABLED` 推导出的返回值，不落库。

## 五、消息处理内部接口

```text
handle_codex_message(open_id, message_context, text) -> None
```

### 用途

处理王志鹏的一条私聊消息，包含普通私聊主窗口和人类主动打开的飞书原生话题。

### 入参

| 字段 | 说明 |
|---|---|
| `open_id` | 用户身份 |
| `message_context.chat_id` | 私聊 chat |
| `message_context.chat_type` | 飞书 chat 类型，本能力只接受 `p2p` |
| `message_context.message_id` | 飞书消息 ID，幂等 |
| `message_context.root_id` | 飞书话题根消息，可空 |
| `message_context.parent_id` | 飞书父消息，可空 |
| `message_context.thread_id` | 飞书原生话题 ID，可空 |
| `text` | 用户输入 |

### 出参

无同步出参；结果通过飞书消息可见。

### 成功行为

- 普通私聊 `/new`：创建新会话并回复。
- 飞书话题里的 `/new`：不重置会话，回复“话题里不用 /new；请新开一个飞书话题开始新上下文。”。
- 普通消息：定位会话、创建 run、启动流式卡片、调用 Codex。
- 话题消息：用 `feishu_thread` scope 定位会话，并回复到同一个话题。

### 失败行为

发送可理解的失败说明，并落库 run 状态。

### 用户可见影响

用户要么看到流式回答，要么看到明确失败提示。

## 六、会话创建/查询接口

```text
get_or_create_active_conversation(open_id, scope) -> Conversation
create_new_conversation(open_id, scope) -> Conversation
bind_codex_session(conversation_id, session_id) -> None
```

### 用途

管理飞书私聊 / 飞书话题与 Codex session 的绑定。

### 入参

| 字段 | 说明 |
|---|---|
| `open_id` | 用户 |
| `chat_id` | 私聊 chat |
| `scope_type` | `private_chat` 或 `feishu_thread` |
| `scope_id` | 私聊为 `chat_id`；话题为 `thread_id` |
| `scope_aliases` | 话题查找时用 `thread_id` 和同事件携带的 `root_id` 命中已存在会话的 `scope_id` 或持久化别名集合；单独 `root_id/parent_id` 不触发话题 |
| `conversation_id` | 后端会话 ID |
| `session_id` | Codex/Cortex session/thread ID |

### 出参

Conversation 对象，包含当前 active 状态和绑定 session。

### 成功行为

普通私聊和飞书话题消息都能定位到一个明确 conversation。

### 失败行为

数据库不可用时，用户看到“会话服务暂不可用”。

### 用户可见影响

用户不需要看见 session_id；只感知普通私聊连续、同一话题连续、不同话题隔离。

## 七、Codex/Cortex 调用接口

```text
run_codex_message(conversation, user_text, on_event) -> CodexRunResult
```

### 用途

调用 Codex/Cortex CLI，并把输出事件逐步交给上层。

### 入参

| 字段 | 说明 |
|---|---|
| `conversation` | 包含明确 session_id |
| `user_text` | 用户输入 |
| `on_event` | 输出事件回调 |

### 出参

| 字段 | 说明 |
|---|---|
| `status` | `completed` / `failed` / `timeout` / `needs_confirmation` |
| `session_id` | 明确 Codex session |
| `final_text` | 最终回答 |
| `error_message` | 失败原因摘要 |

### 成功行为

- 有 session_id：使用明确 session resume。
- 无 session_id：创建新 session，并保存。
- 输出过程中持续触发 `on_event`。

### 失败行为

- CLI 不存在：run 失败。
- resume 指定 session 失败：新建 session 或提示用户。
- 超时：终止 run。

### 用户可见影响

用户看到流式过程；失败时看到可读说明。

## 八、流式输出更新接口

```text
create_stream_card(reply_target, title, initial_state) -> StreamTarget
update_stream_text(stream_target, state_text, answer_text, sequence) -> None
close_stream_card(stream_target, summary) -> None
```

### 用途

封装飞书 CardKit v1 流式卡片。

### 入参

| 字段 | 说明 |
|---|---|
| `reply_target.kind` | `private` 或 `feishu_thread` |
| `reply_target.open_id` | 私聊接收用户 |
| `reply_target.message_id` | 话题回复目标消息 ID，可空 |
| `reply_target.reply_in_thread` | 话题场景固定 `true` |
| `title` | 卡片标题 |
| `state_text` | 当前短状态，例如“正在处理 · 18 秒” |
| `answer_text` | 当前完整回答文本 |
| `sequence` | 严格递增序号 |
| `summary` | 结束后的聊天列表摘要 |

### 出参

| 字段 | 说明 |
|---|---|
| `card_id` | CardKit 卡片实体 ID |
| `message_id` | 飞书消息 ID |
| `element_id` | 被更新的 markdown 元素 ID |

### 成功行为

用户看到打字机效果、短状态计时和明确完成态。

### 失败行为

- 创建失败：发送普通文本兜底。
- 更新失败：停止流式，最终普通文本兜底。
- 关闭失败：后台重试。
- 话题内 CardKit 流式不可用：同话题普通文本兜底。

### 用户可见影响

主路径是原生流式卡片；兜底是普通文本。话题场景兜底也必须回到同一飞书话题。

## 九、消息反应接口

```text
add_reaction(message_id, emoji_type) -> None
```

### 用途

让用户知道消息已被 Bot 接住。该能力是体验增强，不是主流程依赖。

### 失败行为

- 添加反应失败时记录 warning。
- 不打断 Codex run。
- 不向用户发送额外错误提示，避免干扰主回复。
- 话题场景中，反应失败同样不影响主流程；如果需要发送后续提示，提示必须回到同一个话题。

## 十、危险操作确认接口

```text
request_dangerous_action_confirmation(run_id, action_summary) -> Confirmation
handle_confirmation_action(action_id, operator_open_id, decision) -> None
```

### 用途

对危险命令做二次确认。

该接口只有在 runner 能提供“执行前 pending approval / tool-call 事件”时才可调用。不能对已经执行完的命令补发确认卡片。

### 入参

| 字段 | 说明 |
|---|---|
| `run_id` | 当前执行 |
| `action_summary` | 用户可读的危险操作摘要 |
| `operator_open_id` | 点击确认的人 |
| `decision` | `confirm` / `cancel` |

### 出参

Confirmation 对象或处理结果。

### 成功行为

只有王志鹏点击确认才继续。

### 失败行为

非本人点击无效；过期确认无效。

### 用户可见影响

用户看到确认卡片，而不是 Codex 直接执行危险动作。

如果当前 runner 无法执行前拦截危险操作，用户应看到明确失败提示：

```text
这次操作需要二次确认，但当前 Codex 执行通道无法安全暂停。已停止本次执行。
```

## 十一、健康检查接口

```text
check_codex_chat_health() -> HealthReport
```

### 用途

给运维或调试确认能力是否可用。

### 入参

无。

### 出参

| 字段 | 说明 |
|---|---|
| `codex_cli_available` | Codex 是否存在 |
| `cardkit_available` | CardKit 权限是否可用 |
| `db_available` | 表是否可用 |
| `allowed_user_configured` | 王志鹏 open_id 是否配置 |
| `application_entry_visible` | 申请入口是否开启，第一版应为 `false` |

### 成功行为

返回各项状态，不打印密钥。

### 失败行为

返回 failed 项。

### 用户可见影响

无直接影响；用于上线前检查。

## 十二、管理/调试接口

第一版建议只做 CLI，不开放飞书命令：

```text
scripts/codex_chat_admin.py list-conversations
scripts/codex_chat_admin.py show-run <run_id>
scripts/codex_chat_admin.py close-stale-streams
scripts/codex_chat_admin.py list-entitlements
```

### 用途

排查会话、run、卡片未关闭等问题。

### 用户可见影响

无；仅管理员排障。
