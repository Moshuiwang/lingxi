# 用户机器人入口路由表接口方案

> 日期：2026-06-10
> 状态：方案
> 范围：飞书私聊消息事件、审批群/测试群 @Bot 消息事件、内部入口调用、用户可见回复。

## 一、接口边界

这里的“接口”不是新增 HTTP API，而是飞书消息事件和内部入口函数之间的交互口径。

本期不新增外部接口，不改变 MCP 接口，不改变审批卡片 action。

## 二、飞书私聊消息输入

触发条件：

```text
chat_type = p2p
message_type = text
```

入口字段：

| 字段 | 用途 |
|---|---|
| `open_id` | 识别用户 |
| `tenant_key` | 后续卡片回调兜底使用 |
| `text` | 私聊文本，用于路由匹配 |

## 三、私聊路由输入输出

内部入口：

```text
handle_private_message(open_id, tenant_key, text, submit_fn) -> bool
```

目标行为：

| 输入文本 | 返回 | 入口动作 | 用户可见结果 |
|---|---|---|---|
| `申请权限` | `True` | 提交申请入口任务 | 收到申请卡片或状态文字 |
| ` 申请权限 ` | `True` | 提交申请入口任务 | 收到申请卡片或状态文字 |
| 其他文本 | `True` | 发送私聊引导文案 | 收到“可以直接说：申请权限” |

说明：未命中也返回 `True`，表示这条私聊消息已被机器人处理，避免上层误判为无人处理。

## 四、命中后的服务调用

命中 `申请权限`：

```text
_send_application_entry(open_id, tenant_key)
  -> application_flow.build_entry_response(db, open_id, tenant_key)
```

服务返回与入口动作保持现状：

| 服务返回 | 入口动作 |
|---|---|
| `card_001` | 私聊发送申请卡片 |
| `text` | 私聊发送状态文字 |
| `text_then_card_001` | 先发状态文字，再发申请卡片 |

## 五、未命中回复

未命中时调用：

```text
send_text_message(open_id, guide_text, token)
```

建议回复：

```text
我现在可以帮你开通 BI 权限。你可以直接说：申请权限
```

未命中不调用 `application_flow`，不写业务数据，不发审批群消息。

## 六、审批群/测试群消息输入

触发条件：

```text
chat_type != p2p
chat_id in {审批群, 测试群}
has_mention = true
```

这里的 `has_mention` 必须表示“明确 @ 了本 Bot”。现有“消息中存在任意 @”不能直接作为 help 兜底条件；否则审批群里 @ 其他人也会触发命令列表。

该判断应在消息解析层完成：解析被 @ 对象的 open_id，和本 Bot 的 open_id 比对；如果 mention id 是其他用户或 `all`，则 `has_mention=False`，并且不能触发 help。

两种 mention 输入都要支持：

| 形态 | 判断方式 |
|---|---|
| `<at user_id="ou_xxx">...</at>` | 文本里直接带 open_id，可直接和 Bot open_id 比对 |
| `@_user_N` | 文本里没有 open_id，必须用 `msg.mentions` 中对应 key 的 `id.open_id` 比对 |

本 Bot 的 open_id 来源：

| 来源 | 规则 |
|---|---|
| `LARK_BOT_OPEN_ID` | 优先使用，适合生产显式配置 |
| `/bot/v3/info` | 未配置时用 tenant token 获取并缓存 |
| `LARK_APP_ID` / `get_app_id()` | 禁止使用；它不是 Bot open_id |

如果 Bot open_id 无法解析，服务应 fail-fast 或给出明确错误，不能让审批群命令进入“全部不响应”的静默状态。

入口字段：

| 字段 | 用途 |
|---|---|
| `open_id` | 识别操作人 |
| `chat_id` | 判断是审批群还是测试群 |
| `stripped_text` | 去掉 @Bot 后的文本，用于命令匹配 |
| `mentions` | 飞书消息 mention 数组，用于把 `@_user_N` 映射到 open_id；XML 形态仍可直接从文本取 open_id |
| `has_mention` | 控制群里只有明确 @Bot 才响应，不能把 @ 其他人当成命中 |
| `tenant_key` | 测试命令复用现有上下文 |

内部入口签名保持现状，但 `has_mention` 的语义收紧为“明确 @ 了本 Bot”。`start_bot.py` 需要从消息事件里提取 `msg.mentions`，交给解析层参与计算：

```text
handle_group_message(open_id, chat_id, stripped_text, has_mention, submit_fn, tenant_key="", post_approval_fn=None) -> bool
```

## 七、审批群/测试群路由输入输出

| 输入文本 | 群 | 返回 | 入口动作 | 用户可见结果 |
|---|---|---|---|---|
| `@Bot 取消授权` | 审批群/测试群 | `True` | 进入取消授权流程 | 收到取消授权选择卡片或错误文字 |
| `@Bot 关闭申请 ...` | 审批群/测试群 | `True` | 关闭待审申请 | 群里收到处理结果 |
| `@Bot 重试权限发布` | 审批群/测试群 | `True` | 触发权限发布重试 | 群里收到处理结果 |
| `@Bot 列出用户` 等中文管理命令 | 审批群/测试群 | `True` | 进入 admin command flow | 群里收到处理结果 |
| `@Bot 测试申请 ...` 等测试命令 | 测试群 | `True` | 进入测试命令 flow | 测试群收到处理结果 |
| `@Bot`、`@Bot 帮助`、`@Bot help`、其他未命中文本 | 审批群 | `True` | 发送审批群 help 文案 | 收到审批群命令列表 |
| `@Bot`、`@Bot 帮助`、`@Bot help`、其他未命中文本 | 测试群 | `True` | 发送测试群 help 文案 | 收到审批群命令列表 + 测试命令列表 |
| 未 @Bot 的群消息 | 审批群/测试群 | `False` | 不处理 | 不响应 |
| @ 其他人或 @所有人 | 审批群/测试群 | `False` | 不处理 | 不响应 |
| 任意 @Bot | 其他群 | 不调用该入口 | 不处理 | 不响应 |

说明：审批群/测试群未命中也返回 `True`，表示入口已完成处理；当前上层不依赖该返回值，但测试可用它确认路由结果。

## 八、群聊 help 回复

未命中时调用：

```text
send_text_to_chat(chat_id, help_text, token)
```

审批群 help 只列生产可用命令；测试群 help 在审批群命令后追加测试命令。`@Bot 帮助` / `@Bot help` 通过兜底 help 入口处理，不需要新增业务命令 handler。

测试群 help 必须用和测试命令执行相同的判定条件。当前实现口径是 `_is_test_chat(chat_id)`，只认显式配置的 `LARK_TEST_CHAT_ID`，不使用 `get_test_chat_id()` 的审批群 fallback。这样可以保证 help 中推荐的测试命令确实能在该群执行。

## 九、群聊接口不变

以下群聊入口不在本次接口变更范围内：

| 群聊输入 | 原流程 |
|---|---|
| `@Bot 取消授权` | 进入取消授权选择 |
| `@Bot 关闭申请 <用户> <原因>` | 关闭待审申请 |
| `@Bot 重试权限发布` | 管理员立即触发权限发布 |
| 测试群 E2E 命令 | 仅测试群生效 |

## 十、错误处理

| 场景 | 处理 |
|---|---|
| 文本为空 | 视为未命中，发送引导文案 |
| 文本不是 `申请权限` | 视为未命中，发送引导文案 |
| 审批群/测试群只 @Bot 不带文字 | 发送对应群 help 文案 |
| 审批群/测试群 @Bot 但命令未支持 | 发送对应群 help 文案 |
| 审批群/测试群 @ 其他人或 @所有人 | 不响应 |
| XML 形态 @Bot | 能识别并进入路由 |
| Placeholder 形态但没有 `mentions` 映射 | 不能认定为 @Bot，避免误响应 |
| 其他群 @Bot | 不响应 |
| 飞书发送失败 | 沿用现有发送失败日志与异常处理 |
| 申请流程内部返回文字 | 原样发送给用户 |
