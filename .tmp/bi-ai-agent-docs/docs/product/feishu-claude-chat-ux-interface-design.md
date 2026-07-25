# 接口设计：飞书 Claude Code 对话体验治理

更新时间：2026-07-23

## 一、内部结果完成接口

### `finalize_run_result`

位置建议：`app/services/codex_chat/service.py` 或新模块 `app/services/codex_chat/result_guard.py`

```python
def finalize_run_result(
    *,
    user_text: str,
    full_text: str,
    final_text: str,
    runner_events: list[dict],
    artifacts: list[dict],
    elapsed_seconds: int,
) -> FinalizedResult:
    ...
```

输出：

```python
@dataclass
class FinalizedResult:
    visible_text: str
    completion_status: str  # completed | needs_confirmation | failed
    completion_quality: str # 取值见 data design 的统一 completion_quality 枚举
    summary: str
    should_close_stream: bool
```

规则：

- `visible_text` 为空时，生成兜底文案，不允许空 completed。
- BI 查询 incomplete 时，`completion_status` 不能是普通 completed。
- 如果已有成功 artifact，可允许文本较短，但仍需说明 artifact 是什么。

## 二、Runner 工具事件接口（BI 守卫前置必做）

现有 `on_event` 继续保留：

```python
on_event({"type": "assistant_text", "text": "..."})
on_event({"type": "heartbeat", "elapsed_seconds": 12})
on_event({"type": "stage", "text": "正在查询数据"})
```

必须新增事件：

```python
on_event({
    "type": "tool_use",
    "tool_name": "mcp__bi-metric__query_metric",
    "input": {...},
})

on_event({
    "type": "tool_result",
    "tool_name": "mcp__bi-metric__query_metric",
    "summary": {...},
})
```

这些事件不是可选增强，而是 P0 查询结果守卫的输入条件。没有 tool trace，就无法可靠判断“Claude 是否已经调用 `query_metric` 但最终没有把结果展示给用户”。

`result_kind=bi_query` 的派生规则：

- 充分条件：tool trace 中出现 `mcp__bi-metric__query_metric`。
- 兜底条件：用户输入有明显 BI 查询意图但无工具调用，此时应生成澄清或失败说明，不能空完成。
- 关键词只做兜底，不与 `query_metric` 做 AND 条件。

Claude stream-json 最小解析要求：

- assistant event 的 `content[]` 中，`type=="tool_use"` 时记录 `name`、`id` 和安全摘要后的 `input`。
- user event 的 `content[]` 中，`type=="tool_result"` 时记录对应 `tool_use_id`、是否有文本结果、是否看起来有数据行。
- 不落原始大结果、不记录 token 或密钥。

一期可先不全量落库原始 tool result，但必须记录：

- tool name；
- 是否调用过 `query_metric`；
- 查询参数摘要；
- 是否有结果行。

## 三、卡片标题接口

### `build_chat_card_title`

位置建议：`app/services/codex_chat/titles.py`

```python
def build_chat_card_title(
    user_text: str,
    *,
    brand: str = "BI Plus",
    provider: str = "",
    max_chars: int = 26,
) -> str:
    ...
```

输出示例：

- `BI Plus: 尼日利亚近 7 天充值`
- `BI Plus: 乌干达收入趋势`
- `BI Plus: 生成充值分析报告`

约束：

- 不输出 `Codex:` 或 `Codex：`。
- provider 仅用于日志或调试，不进入普通用户标题。
- 超长标题截断。

实现约束：

- `build_chat_card_title` 取代 `service.py` 里的 `_short_card_title`，避免两套标题生成规则并存。

## 四、话题标题 / 话题预览接口

飞书话题标题更新能力需要先确认 Lark API 是否支持当前机器人权限。接口设计先定义内部抽象：

```python
class TopicTitlePort(Protocol):
    def update_topic_title(self, *, chat_id: str, thread_id: str, title: str) -> None:
        ...
```

一期如果 API/权限暂不可用：

- 仍先改卡片标题；
- 在 DB 保存 `conversation.title`；
- 让卡片正文开头先出现业务摘要或结果，避免飞书话题列表预览只显示“已完成 · 用时 xx 秒”；
- 话题标题更新列入验收待办，不影响 P0 查询修复。

## 五、多公司选择接口

候选来源必须明确。优先顺序：

1. MCP 维度检索工具返回的 company 候选，例如 `search_dimension(dimension_type="company", keyword="尼日利亚")`。
2. Claude tool trace 中安全摘要后的 company 候选。
3. 如果没有结构化候选，只能文本追问，不展示按钮。

### 文本降级版

当匹配多个公司且不能可靠按钮交互时：

```text
我找到 2 个可能的公司，请回复编号：
1. Nigeria Corp.（尼日利亚）
2. Nigeria Solar Corp.（尼日利亚新能源）
3. 两个都查
```

用户回复 `1` / `2` / `两个都查` 后继续原任务。

继续方式：

- 新建一条 run，使用同一个 Claude session `--resume <session_id>`。
- 用户选择被转写成明确指令，例如：`继续上一轮查询，company_id=13，指标和时间范围沿用上一轮。`
- run 必须属于当前 conversation 和当前 open_id。

### 卡片按钮版

按钮 value：

```json
{
  "action": "codex_chat_select_company",
  "conversation_id": 12,
  "run_id": 68,
  "selection": "company_id:13"
}
```

要求：

- value 不放敏感信息。
- run 必须属于当前用户和当前 conversation。
- 过期或不匹配时提示重新发起查询。

## 六、Artifact 交付接口

### 收件人输入

```python
@dataclass
class ArtifactRecipient:
    codex_chat_user_id: int
    feishu_open_id: str
    drive_folder_token: str = ""
```

`drive_folder_token` 可通过 `CodexChatUser.feishu_open_id -> User.open_id` 查找。当前按单租户假设处理；多租户放量前必须同时带 `tenant_key` 限定，避免同一 open_id 在不同租户误匹配。找不到时必须降级为可说明的飞书消息/文件交付，不能输出服务器路径。

`ArtifactRecipient` 只用于服务内部已经完成身份解析后的调用；Unix socket 文件请求不得接收该结构。文件请求的收件人只能由 `SO_PEERCRED peer UID -> execution_username -> 唯一 active DB 绑定` 产生。

本接口的 `local_path` 不表示调用方可以直接读取任意本地文件。实现必须：

- 由 root-owned 最小 staging helper 对真实源路径执行 Unix 用户 / home 边界校验，并从同一已验证 FD 复制到一次性交付区；
- 禁止读取 `.env`、私钥、token 文件、软链或越界文件；客户端复制出的 spool 文件不能替代真实源校验；
- 记录失败原因并给用户可读提示。

### 数据模型输入

```python
@dataclass
class ArtifactRequest:
    run_id: int
    artifact_type: str  # image | feishu_doc | file | markdown_internal
    local_path: str
    title: str
    user_visible: bool
    preferred_delivery: str  # card_image | feishu_doc_link | feishu_file
```

### 交付输出

```python
@dataclass
class ArtifactDeliveryResult:
    ok: bool
    artifact_type: str
    delivery_channel: str
    feishu_link: str = ""
    file_token: str = ""
    image_key: str = ""
    user_message: str = ""
    error: str = ""
```

### 私有文件 socket 请求

用户侧命令保持一个源文件参数：

```bash
send-feishu-file --file <source_path>
```

socket JSON 只允许：

```json
{"path":"<source_path>"}
```

请求不得包含或覆盖 `uid`、`username`、`home`、`delivery_id`、`staged_path`、`open_id`、`chat_id` 或其他收件人字段。file sender 必须先读取 `SO_PEERCRED`，确认受管 Linux 身份和唯一 active DB 绑定，再生成 `delivery_id`；任何身份/绑定失败都发生在 staging 和飞书调用之前。

### 受信任 staging helper

file sender 以解析出的 username 调用固定命令：

```text
sudo -n /usr/local/sbin/bi-ai-privileged stage-delivery <username>
```

stdin 只包含：

```json
{"source_path":"<requested path>","delivery_id":"<service generated UUID>"}
```

helper 不接受目标目录、目标文件名、收件人、任意 UID、任意命令或环境覆盖。它必须：

1. 确认 username 是 `/home/biai-agent/users/<username>` 下的受管非特权账号；helper 从系统账号解析 UID，file sender 在消费前再次解析并要求它仍等于 socket peer UID，UID 不能来自原始 socket JSON。
2. 以目标用户的精确 UID/GID/supplementary groups 打开 source，证明该用户本来就可读；root 权限不能把用户不可读文件变成可发送文件。
3. 对同一 source FD 完成 home 内路径、所有路径组件无 symlink、敏感文件名/后缀、普通文件、owner/可读性、单文件大小和 inode 稳定性校验。
4. 从该 FD 复制到固定 `/var/lib/bi-ai-agent/file-delivery/<uid>/`，目标先写服务生成 delivery ID 对应的临时文件，固定为 `root:bi-ai-service 0640`，完成 `fsync` 后原子 rename 为 `<delivery_id>.ready`。
5. stdout 只返回结构化安全摘要：`ok/code/delivery_id/file_name/size`；不得返回 source/staged 绝对路径或文件内容。

固定交付根为 `root:bi-ai-service 0750`，仅让 file sender 所属 service group 在启动时列举并清理过期 `.ready/.sending/.tmp`；numeric UID 子目录保持 `0770`，payload 保持 `0640`。该权限只作用于服务 staging 区，普通业务用户不属于 service group，用户 home 与工作目录权限不得改变。

helper 返回成功后，file sender 从固定根和 peer UID 派生 staged path，复核目录与文件 owner/group/mode、普通文件、link count、size 和 delivery ID，再把 `.ready` 原子认领为 `.sending`。实际调用飞书 API 前，必须再次解析同一 peer UID 的唯一 active DB 绑定，并要求当前收件人 `open_id` 与 staging 前快照一致；任一失配、disabled 或非唯一都清理 `.sending` 且不得发送。飞书 API 只能读取 `.sending` FD；不能回读用户 home，也不能接受 helper/请求给出的其他路径。

### 一次性与清理结果

| 结果 | 行为 |
|---|---|
| staging 前校验失败 | 返回既有 `file_not_allowed` / `sensitive_file` / `symlink_not_allowed` / `not_regular_file` / `file_too_large` 等稳定码；不创建 payload、不调用飞书 |
| staged metadata 不匹配 | `staged_file_invalid`，隔离并清理；不调用飞书 |
| `.ready` 不存在或已被认领 | `delivery_not_available`；不自动重建或重发 |
| 飞书明确成功 | 返回原有成功码，清理 `.sending` |
| 飞书明确失败或状态未知 | 返回失败码并明确未确认送达，清理 `.sending`；禁止自动重发 |
| 进程崩溃遗留 | 下次启动只做有界清理，不消费、不补发 |

file sender unit 继续使用 `bi-ai-service`，但为上述固定 helper 调用必须让实际进程 `NoNewPrivs: 0`。所有会隐式强制 no-new-privileges 的冲突项必须关闭或移除；其余 systemd hardening 和固定 `ReadWritePaths` 保持。验收以 `/proc/<MainPID>/status` 和一次无敏感内容的 helper probe 为准，不只看 `systemctl show`。

## 七、文档交付接口

CLI 归属：

- `send-feishu-file` 属于 BI Plus Plugin 仓库的用户侧命令。
- 本仓库负责后端 socket handler、飞书 Drive/消息发送和交付结果记录。
- 设计变更需要同步 plugin 仓库的 skill/CLI 参数与本仓库后端契约。

Plugin 调用后端时，新增或调整参数：

```bash
send-feishu-file --file report.md --as feishu_doc --hide-source
```

语义：

- `--as feishu_doc`：把 Markdown 转飞书文档。
- `--hide-source`：不向用户发送原 MD 文件；drive 模式下这应与现有 `.md -> docx/doc` 行为对齐，message fallback 也必须遵守。
- 返回消息必须包含飞书文档链接。

若飞书文档权限缺失：

- 当前实现不自动转换或发送原 Markdown；返回“飞书文档生成未确认，未发送 MD 原文件”，并要求先核对飞书避免重复发送；
- 未来若新增 `.docx` / `.pdf` 安全降级，必须有独立转换产物和可回读 delivery receipt，不能把源 Markdown 伪装成降级成功；
- 不发送服务器路径。

## 八、图片交付接口

Plugin 或 Claude 生成图片后，调用：

```bash
send-feishu-file --file chart.png --as image
```

语义：

- 优先作为飞书图片或文件发送。
- 用户消息中不出现本地路径。
- 如果图片上传失败，保留错误并给用户可读提示。

后端要求：

- PNG/SVG 作为普通文件发送时，`_file_type_for` 应继续返回飞书文件消息可接受的 `stream`，不要返回不存在的 `image` file_type。
- 如果走 CardKit 内联，需要新增飞书 image 上传接口并返回 `image_key`，同时新增 CardKit 图片元素。
- 如果走文件消息，返回 `file_token` 或可打开链接。

## 九、错误回复接口

统一错误文案：

| 错误码 | 文案 |
|---|---|
| `empty_final_text` | `刚才没有生成可见结果，我会重新补发上一次查询结果。` |
| `incomplete_query_result` | `我只确认了查询范围，还没有把结果展示出来。请稍等，我继续补充结果。` |
| `needs_company_selection` | `我找到了多个公司，请先选择要查询的对象。` |
| `artifact_upload_failed` | `文件已生成，但上传飞书失败，请稍后重试。` |
| `staging_failed` | `文件未进入受控交付区，本次没有发送。` |
| `delivery_state_unknown` | `文件是否送达暂时无法确认，请先核对飞书；系统不会自动重发。` |
| `card_update_failed` | `卡片更新失败，下面用普通消息补发完整回复。` |

现有兜底文案中如果含 `Codex`，本轮需统一改为 `BI Plus`。

## 十、测试接口

新增测试建议：

- `tests/test_codex_chat_result_guard.py`
- `tests/test_codex_chat_titles.py`
- `tests/test_feishu_file_sender_artifacts.py`
- `tests/test_feishu_file_sender.py`
- `tests/test_bi_plus_send_feishu_file_client.py`
- `tests/test_bi_plus_file_sender_daemon.py`
- `tests/test_production_baseline.py`
- 扩展 `tests/test_codex_chat_service.py`

测试只 mock 外部边界：

- 飞书 API；
- Claude runner；
- 文件系统；
- 时间。

不 mock `result_guard` 内部判断。
