# Codex 私聊机器人数据库设计

> 日期：2026-06-13
> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。下文不是当前生产部署、验收或排障指引。
> 关联文档：`docs/product/codex-private-chat-prd.md`

## 一、设计目标

数据库要支撑四件事：

- 不串会话：飞书私聊 / 飞书原生话题与 Codex session 明确绑定。
- 可恢复：后端重启后仍能找到当前活跃会话和未完成 run。
- 可排障：能看到每次用户输入、Codex 执行、流式卡片更新状态。
- 可扩展：同一用户可以同时拥有普通私聊会话和多个飞书话题会话。

第一版只支持王志鹏一个用户，且不展示申请入口。但表结构保留“权利状态”和“申请记录”，方便未来开放给所有用户申请。

## 二、users

### 表用途

记录拥有或曾申请 Codex 私聊权利的人。第一版只有王志鹏为可用状态。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `feishu_open_id` | text | 飞书 open_id |
| `feishu_name` | text | 飞书姓名 |
| `entitlement_username` | text | 权利归属账号，例如 `wangzp` |
| `execution_username` | text | 实际执行 Codex 的 Linux 账号，例如 `wangzhipeng` |
| `role` | text | `owner` / `user` |
| `entitlement_status` | text | `active` / `pending` / `rejected` / `disabled` |
| `entitlement_source` | text | `seed_allowlist` / `approved_application` / `manual_grant` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

### 主键

`id`

### 关键索引

- `idx_codex_users_open_id(feishu_open_id)`
- `idx_codex_users_entitlement_status(entitlement_status)`

### 示例数据

```text
id=1
feishu_open_id=ou_58f29ff5f96f8527d007437111207742
feishu_name=王志鹏
entitlement_username=wangzp
execution_username=wangzhipeng
role=owner
entitlement_status=active
entitlement_source=seed_allowlist
```

### 第一版是否需要

需要。虽然现有项目已有用户表，但 Codex 私聊模块应保持可复用，避免直接依赖 BI 权限用户模型。第一版它承担隐藏白名单；未来它承担申请审批通过后的权利判断。

实现时可以命名为 `codex_chat_users`，避免和现有 `users` 冲突。

## 三、access_requests

### 表用途

预留未来“申请 Codex 私聊权利”的记录。第一版不展示申请入口，因此正常情况下没有新申请记录。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `feishu_open_id` | text | 申请人 open_id |
| `feishu_name` | text | 申请人姓名 |
| `linux_username` | text | 申请使用的 Linux 账号 |
| `execution_username` | text | 审批后实际执行账号，第一版未来开放时由管理员决定 |
| `reason` | text | 申请理由 |
| `status` | text | `pending` / `approved` / `rejected` / `cancelled` |
| `approval_message_id` | text | 未来审批卡片消息 ID |
| `reviewer_open_id` | text | 审批人 |
| `review_note` | text | 审批意见 |
| `created_at` | datetime | 创建时间 |
| `reviewed_at` | datetime | 审批时间 |

### 主键

`id`

### 关键索引

- `idx_codex_access_requests_open_id(feishu_open_id)`
- `idx_codex_access_requests_status(status)`

### 示例数据

```text
feishu_open_id=ou_future_user
linux_username=zhangsan
reason=需要在飞书里使用 Codex 处理项目任务
status=pending
```

### 第一版是否需要

表结构建议预留，但第一版不写入。原因是产品已确认当前隐藏申请入口，未来会开放给用户申请。

## 四、conversations

### 表用途

记录飞书私聊和飞书原生话题会话。本次 PR 后，一个用户可以有一个普通私聊 active conversation，也可以在多个飞书话题里各有一个 active conversation。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `user_id` | integer | 关联 users |
| `feishu_chat_id` | text | 私聊 chat_id |
| `scope_type` | text | `private_chat` / `feishu_thread` |
| `scope_id` | text | 私聊等于 chat_id；话题等于 thread_id |
| `scope_aliases` | text | JSON 数组，保存同一飞书话题观察到的 thread_id/root_id；parent_id 不入别名 |
| `title` | text | 会话标题，可由首条消息生成 |
| `status` | text | `active` / `archived` / `failed` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

### 主键

`id`

### 关键索引

- `idx_codex_conversations_user_status(user_id, status)`
- `idx_codex_conversations_scope(scope_type, scope_id)`
- `uq_codex_one_active_conversation_per_scope(user_id, scope_type, scope_id) WHERE status='active'`

### 示例数据

```text
id=100
user_id=1
feishu_chat_id=oc_xxx
scope_type=private_chat
scope_id=oc_xxx
title=默认会话
status=active
```

飞书话题示例：

```text
id=101
user_id=1
feishu_chat_id=oc_xxx
scope_type=feishu_thread
scope_id=omt_xxx 或 om_root_xxx
title=飞书话题会话
status=active
```

### 第一版是否需要

需要。它是飞书入口和 Codex session 的业务边界。

必须加部分唯一索引，保证每个用户在同一个 scope 内只有一个 active conversation。否则同一话题并发时可能出现两个当前会话。

历史私聊会话不需要迁移语义。既有 `private_chat` 数据继续按 `chat_id` 作为 scope 使用；本次只调整唯一索引口径，允许新增 `feishu_thread` active conversation。

只有消息带 `thread_id` 时才创建或查找 `feishu_thread` conversation。业务查找时用 `{thread_id, root_id}` 命中已有 active conversation 的 `scope_id` 或 `scope_aliases`；命中后把本次新看到的 `thread_id/root_id` 合并进 `scope_aliases`。单独 `root_id/parent_id` 按普通私聊处理，避免 Bot 通过 `reply_in_thread=true` 主动创建话题。数据库唯一索引提供兜底约束，但不能替代这个查找规则。

## 五、codex_sessions

### 表用途

保存后端 conversation 与 Codex/Cortex session 的明确绑定。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `conversation_id` | integer | 关联 conversations |
| `provider` | text | `codex` / `cortex` |
| `session_id` | text | Codex thread/session ID |
| `cwd` | text | Codex 工作目录 |
| `cli_path` | text | CLI 路径 |
| `model` | text | 可选，模型名 |
| `status` | text | `active` / `closed` / `failed` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

### 主键

`id`

### 关键索引

- `idx_codex_sessions_conversation(conversation_id)`
- `idx_codex_sessions_session_id(session_id)`

### 示例数据

```text
conversation_id=100
provider=codex
session_id=019ebc8e-4433-7061-bbbc-f629d6e592cb
cwd=/home/wangzhipeng/projects/bi-ai-agent
cli_path=/home/wangzhipeng/.local/bin/codex
status=active
```

### 第一版是否需要

必须需要。它保证不使用 `resume --last`。

## 六、messages

### 表用途

保存用户消息和机器人最终回复，便于排障和未来摘要。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `conversation_id` | integer | 会话 |
| `run_id` | integer | 可空，关联 runs |
| `role` | text | `user` / `assistant` / `system` |
| `feishu_message_id` | text | 飞书消息 ID |
| `content` | text | 消息正文 |
| `content_format` | text | `text` / `markdown` / `json` |
| `created_at` | datetime | 创建时间 |

### 主键

`id`

### 关键索引

- `idx_codex_messages_conversation_created(conversation_id, created_at)`
- `idx_codex_messages_feishu_message(feishu_message_id)`

### 示例数据

```text
role=user
feishu_message_id=om_user_1
content=帮我看一下当前项目状态
```

### 第一版是否需要

需要。Codex 自身有上下文，但后端也要有可追踪记录，尤其用于失败恢复。

本次不新增 `root_id` / `parent_id` / `thread_id` 三列到 messages。飞书话题归属以 `codex_chat_conversations.scope_type/scope_id` 为准；如后续排障需要更细粒度证据，再补窄列。

## 七、conversation_memory

### 表用途

保存会话摘要和长期上下文。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `conversation_id` | integer | 会话 |
| `memory_type` | text | `summary` / `preference` / `system_note` |
| `content` | text | 摘要内容 |
| `source_run_id` | integer | 来源 run |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

### 主键

`id`

### 关键索引

- `idx_codex_memory_conversation_type(conversation_id, memory_type)`

### 示例数据

```text
conversation_id=100
memory_type=summary
content=用户正在调研飞书 CardKit 流式卡片与 Codex 私聊集成。
```

### 第一版是否需要

建议保留表，但第一版可以不自动生成摘要。原因是 Codex session 自身承担主上下文，摘要可以作为未来会话压缩和迁移能力。

## 八、runs

### 表用途

记录每次调用 Codex/Cortex 的执行过程。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `conversation_id` | integer | 会话 |
| `codex_session_id` | integer | 关联 codex_sessions |
| `user_message_id` | integer | 关联用户消息 |
| `status` | text | `queued` / `running` / `needs_confirmation` / `completed` / `failed` / `timeout` |
| `started_at` | datetime | 开始时间 |
| `finished_at` | datetime | 结束时间 |
| `pid` | integer | CLI 进程 ID，可空 |
| `exit_code` | integer | CLI 退出码 |
| `final_text` | text | 最终回答 |
| `error_message` | text | 错误摘要 |
| `dangerous_action_summary` | text | 待确认操作摘要 |

### 主键

`id`

### 关键索引

- `idx_codex_runs_conversation_status(conversation_id, status)`
- `idx_codex_runs_started(started_at)`

### 示例数据

```text
conversation_id=100
status=completed
final_text=已确认，CardKit 流式卡片可行。
```

### 第一版是否需要

必须需要。它是执行状态、失败恢复和危险操作确认的核心记录。

## 九、stream_updates

### 表用途

记录流式卡片的创建、更新和关闭状态。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `run_id` | integer | 关联 runs |
| `card_id` | text | CardKit card_id |
| `feishu_message_id` | text | 飞书消息 ID |
| `element_id` | text | markdown 元素 ID |
| `sequence` | integer | 当前更新序号 |
| `last_content_length` | integer | 最后一次文本长度 |
| `status` | text | `created` / `streaming` / `closed` / `failed` |
| `last_error` | text | 最近错误 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

### 主键

`id`

### 关键索引

- `idx_codex_stream_run(run_id)`
- `idx_codex_stream_status(status)`
- `idx_codex_stream_card(card_id)`

### 示例数据

```text
run_id=200
card_id=7650544048899214522
feishu_message_id=om_x100b6df75a4174a0c1bcd2bce9fe74d
element_id=codex_stream
sequence=4
status=closed
```

### 第一版是否需要

需要。CardKit 流式输出是第一版核心体验，必须能知道哪张卡片没关闭、哪次更新失败。

话题内如果 CardKit 流式不可用，`status=failed` 并记录 `last_error`，最终回复通过同话题普通文本兜底。

## 十、confirmations

### 表用途

记录危险操作二次确认。

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键 |
| `run_id` | integer | 关联 runs |
| `action_token` | text | 卡片按钮携带的确认 token |
| `action_summary` | text | 用户可读摘要 |
| `status` | text | `pending` / `confirmed` / `cancelled` / `expired` |
| `requested_at` | datetime | 发起时间 |
| `decided_at` | datetime | 点击时间 |
| `operator_open_id` | text | 点击人 |

### 主键

`id`

### 关键索引

- `idx_codex_confirmations_run(run_id)`
- `idx_codex_confirmations_token(action_token)`
- `idx_codex_confirmations_status(status)`

### 示例数据

```text
run_id=201
action_summary=准备删除目录 /tmp/example
status=pending
```

### 第一版是否需要

需要。用户已确认危险操作必须二次确认，用表记录可以避免重复点击、过期点击和非本人点击。

## 十一、表命名建议

为避免和现有 BI AI Agent 表冲突，落地时建议全部加前缀：

```text
codex_chat_users
codex_chat_access_requests
codex_chat_conversations
codex_chat_sessions
codex_chat_messages
codex_chat_memory
codex_chat_runs
codex_chat_stream_updates
codex_chat_confirmations
```

这样未来迁移到其他项目时，可以整组迁移，不影响 BI 权限主链。

## 十二、SQLite 迁移口径

本项目没有独立迁移框架时，按现有 SQLite 兼容补列风格处理：

1. 新库：SQLAlchemy `create_all()` 直接创建最新表结构和索引。
2. 旧库：启动时检查 `codex_chat_conversations` 索引。
3. 如果存在旧的 `uq_codex_one_active_conversation_per_user`，删除后创建 `uq_codex_one_active_conversation_per_scope`。
4. 补 `codex_chat_conversations.scope_aliases` 窄列，默认空字符串或 `[]`。
5. 本次不新增 messages / stream_updates 字段。
6. 历史 `private_chat` 会话不改数据；它们继续以 `scope_type=private_chat, scope_id=chat_id` 生效。
