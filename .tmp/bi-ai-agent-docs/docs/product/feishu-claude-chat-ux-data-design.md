# 数据设计：飞书 Claude Code 对话体验治理

更新时间：2026-07-23

## 现有表

当前 Codex Chat 相关表：

- `codex_chat_users`
- `codex_chat_access_requests`
- `codex_chat_conversations`
- `codex_chat_sessions`
- `codex_chat_messages`
- `codex_chat_memory`
- `codex_chat_runs`
- `codex_chat_stream_updates`
- `codex_chat_confirmations`

本轮优先复用现有表，避免为标题和 guardrail 过度建模。

`codex_chat_memory` 本轮暂不作为恢复状态的主存储。原因：本轮要恢复的是 run 级“是否已把结果交付给用户”的事实，应落在 run/artifact 记录上；memory 可在后续做长期摘要增强。

## 变更一：`codex_chat_conversations.title`

现状：

- 创建时固定为 `Codex 会话`。
- 用户在话题列表和历史记录中缺少业务主题。

目标：

- 保存当前 conversation 的业务主题摘要。
- 初始值由第一条用户消息生成。
- 后续只在更高质量摘要出现时更新，避免每轮都被最新问题覆盖。

建议规则：

```text
conversation.title = "尼日利亚近 7 天充值"
```

展示层拼品牌：

```text
BI Plus: 尼日利亚近 7 天充值
```

不需要新增列。

## 变更二：`codex_chat_runs` 增加完成质量字段

建议新增列：

| 列名 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `completion_quality` | `VARCHAR(64)` | `unknown` | 完成质量，取值见下方统一枚举 |
| `result_kind` | `VARCHAR(64)` | `chat` | 任务类型，取值见下方统一枚举 |

用途：

- 防止空回复被误判为成功。
- 支持后续报表统计“模型完成但用户未得到结果”的次数。
- 支持用户说“继续/没结果”时找最近未成功交付 run。

SQLite migration 采用 `_ensure_sqlite_schema()` 兼容补列。

### 统一枚举

`run.status` 继续使用现有状态：

| 值 | 说明 |
|---|---|
| `queued` | 已入队 |
| `running` | 正在执行 |
| `needs_confirmation` | 需要用户选择或补充信息 |
| `completed` | 已完成，且必须有用户可见结果、artifact、兜底文本或明确失败说明 |
| `failed` | 执行失败 |

`completion_quality` 是完成质量的唯一事实源：

| 值 | 说明 |
|---|---|
| `unknown` | 存量数据或未评估 |
| `ok` | 有正常用户可见文本结果 |
| `needs_user_choice` | 需要用户选择公司/指标/时间 |
| `incomplete_query` | 已确认范围或调用查询，但未交付数据结果 |
| `empty_text_fallback` | 历史兼容值：原始最终文本为空且曾用兜底文案恢复；Claude 新运行不再写入此值 |
| `artifact_delivered` | 用户可见 artifact 已成功交付 |
| `artifact_failed` | artifact 生成或上传失败，已给用户可读说明 |
| `card_fallback_sent` | 非文件交付结果的 CardKit 更新/关闭失败，已用普通消息补发；文件交付保留其 delivery quality，卡片故障记在 stream update |
| `file_skill_rejected` | 受控文件技能已被选择但调用未获接受，已明确告知未发送 |
| `file_delivery_failed` | 受控交付命令返回失败或缺少结构化结果，未确认已发送 |
| `file_delivery_repeated` | 检测到重复受控交付调用，交付状态不再判为成功且禁止自动重放 |
| `file_delivery_succeeded` | 唯一一次受控交付命令成功返回；真实环境仍需从飞书回读可访问结果 |
| `duplicate_file_delivery` | runner 观察到不同 tool id 的第二次受控交付，立即停止并关闭会话等待对账 |

文件/文档交付按产品决策属于低风险操作，不要求逐次批准，也不以手写自然语言规则作为运行时前置授权。意图识别由模型、system prompt 与 Skill 负责并通过 eval 验证；这里的稳定数据合同只记录结构化技能/交付事件与回执，不能用来声称模型对某句自然语言的理解必然正确。
| `empty_model_response` | 至多一次无副作用恢复后仍无最终文本；未确认用户可见结果 |
| `result_event_missing` | Claude 完成协议缺少最终 result 事件；未确认最终结果 |
| `tool_event_invalid` | tool use/result 的 ID 或名称缺失、冲突或孤立；副作用未知并 fail closed |
| `tool_result_missing` | 已观察到 tool use 但没有匹配 tool result；副作用未知并 fail closed |
| `empty_after_side_effect` | 有副作用工具完成后没有最终文本；禁止自动重放，等待对账 |
| `result_delivery_failed` | CardKit 与普通文本均未确认送达；禁止把模型结果记作用户可见成功 |

上述完成协议失败质量值必须同时满足 `run.status='failed'` 且
`failure_code=completion_quality`。它们都表示“没有已确认的最终用户可见结果”；即使流式草稿可能曾
短暂出现，也不得据此记为 `ok` 或自动重放。只有严格匹配且已知只读的工具活动允许一次空结果恢复。

`result_kind` 与 `completion_quality` 正交，表示这轮任务类型：

| 值 | 说明 |
|---|---|
| `chat` | 普通对话 |
| `bi_query` | BI 数据查询 |
| `artifact` | 图片、文档、文件等产物 |
| `file_delivery` | Claude 受控文件技能/命令的结构化调用与回执状态 |
| `clarification` | 追问/选择 |
| `failure` | 用户可读失败说明 |

是否已有用户可见结果由 `completion_quality` 派生，不新增 `user_visible_result` 列，避免冗余不一致。

## 变更三：新增 `codex_chat_artifacts`

需要跟踪图片、飞书文档、文件等用户可见交付物。

```sql
CREATE TABLE codex_chat_artifacts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    artifact_type VARCHAR(64) NOT NULL,
    title VARCHAR(256) NOT NULL DEFAULT '',
    local_path VARCHAR(1024) NOT NULL DEFAULT '',
    feishu_link VARCHAR(1024) NOT NULL DEFAULT '',
    file_token VARCHAR(256) NOT NULL DEFAULT '',
    image_key VARCHAR(256) NOT NULL DEFAULT '',
    delivery_channel VARCHAR(64) NOT NULL DEFAULT '',
    visibility VARCHAR(32) NOT NULL DEFAULT 'internal',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    error_message TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY(run_id) REFERENCES codex_chat_runs(id),
    FOREIGN KEY(conversation_id) REFERENCES codex_chat_conversations(id)
);
```

`codex_chat_artifacts` 也应在 `app/models/codex_chat.py` 中新增 ORM model；`Base.metadata.create_all()` 负责新库建表，`app/db.py::_ensure_sqlite_schema()` 负责存量 SQLite 的兼容 `CREATE TABLE IF NOT EXISTS` 和补列。

字段说明：

| 字段 | 说明 |
|---|---|
| `artifact_type` | `image` / `feishu_doc` / `file` / `markdown_internal` |
| `local_path` | 仅内部排查用，不直接展示给用户 |
| `feishu_link` | 用户可打开链接 |
| `file_token` | 飞书文件 token |
| `image_key` | 飞书图片 key |
| `delivery_channel` | `card_image` / `feishu_doc_link` / `feishu_file` |
| `visibility` | `user_visible` / `internal` |
| `status` | `pending` / `delivered` / `failed` |

隐私约束：

- 不记录 token、密码、私钥。
- `local_path` 不进入用户消息。
- `local_path` 不保存一次性交付区绝对路径；staged path 只能由固定根、peer UID 与 delivery ID 在进程内派生。
- 如路径可能包含敏感用户名，用户消息只展示文件标题。

## 变更四：`codex_chat_stream_updates`

现有表已保存：

- `run_id`
- `card_id`
- `feishu_message_id`
- `element_id`
- `sequence`
- `status`
- `last_error`
- `last_content_length`

本轮复用它判断 CardKit 是否成功关闭。

建议：

- CardKit 最终更新失败时，`status=failed`。
- 普通文本补发成功时，run 的 `completion_quality=card_fallback_sent`。

## 变更五：一次性交付 envelope

本次不新增数据库表或列。受控交付区是短生命周期的跨身份读取桥，不是用户文件仓库，也不是交付状态事实源。

每次请求只在进程内持有以下 envelope：

| 字段 | 来源 | 约束 |
|---|---|---|
| `delivery_id` | file sender 生成的 UUID | 客户端不能指定；只允许安全字符；单次消费 |
| `actor_uid` | Unix socket `SO_PEERCRED` | 不能从 JSON 覆盖 |
| `execution_username` | 系统账号按 `actor_uid` 解析 | 必须是受管用户且与 home 一致 |
| `source_path` | 客户端请求 | 只交给受信任 helper 校验；不写 DB、日志或用户消息 |
| `source_label` | helper 从已验证源文件生成 | 只保留安全 basename；不能含目录、用户名或控制字符 |
| `size` | helper 对已打开 source FD 执行 `fstat` | 必须与 staged FD 一致且不超过上限 |
| `staged_name` | 服务派生 | 固定为 `<delivery_id>.ready/.sending`，不含源文件名 |
| `recipient_open_id` | peer UID 对应的唯一 active DB 绑定 | 不能由请求、helper 输出或 staged metadata 提供 |

状态只允许：

```text
requested -> staged -> claimed -> terminal
                     \-> rejected
```

- `staged`：helper 已把同一已验证 source FD 完整复制并原子发布为 `.ready`。
- `claimed`：file sender 已把 `.ready` 原子改名为 `.sending`；此后同一 `delivery_id` 不得再次消费。
- `terminal`：飞书成功、明确失败或送达状态未知；均不自动重发并清理 staged copy。
- `rejected`：源文件、身份、绑定或 staged metadata 校验失败；不得调用飞书上传。

进程崩溃遗留的 `.ready/.sending` 由有界清理流程删除，不补发。持久审计继续只记录稳定结果码、文件类型/大小和必要的安全 label；不得记录 source/staged 绝对路径、文件内容、其他用户身份或可用于重新发送的 payload。

## 恢复查询

用户输入“继续”“你没有给我结果”时，服务层查询：

```sql
SELECT *
FROM codex_chat_runs
WHERE conversation_id = :conversation_id
ORDER BY id DESC
LIMIT 5;
```

优先恢复：

1. `completion_quality IN ('empty_text_fallback', 'incomplete_query', 'needs_user_choice', 'artifact_failed', 'empty_model_response', 'result_event_missing', 'tool_event_invalid', 'tool_result_missing', 'empty_after_side_effect', 'result_delivery_failed', 'duplicate_file_delivery')`
2. `status IN ('completed', 'failed', 'needs_confirmation')` 且有可恢复上下文
3. 最近一条 `result_kind IN ('bi_query', 'artifact', 'clarification')`

## 数据迁移

迁移位置：

- `app/db.py::_ensure_sqlite_schema()`

迁移策略：

1. 检查 `codex_chat_runs` 是否存在新列。
2. 不存在则 `ALTER TABLE ADD COLUMN`。
3. 检查 `codex_chat_artifacts` 是否存在。
4. 不存在则创建。
5. 存量 run 默认：
   - `completion_quality='unknown'`
   - `result_kind='chat'`

不对历史数据做复杂 backfill，避免误判。

## 报表口径

后续可统计：

- 空回复次数；
- incomplete query 次数；
- artifact 上传失败次数；
- 通过 fallback 恢复次数；
- 用户说“没结果”后是否补发成功。

这些指标用于 G4 “纠错与信任闭环”。
