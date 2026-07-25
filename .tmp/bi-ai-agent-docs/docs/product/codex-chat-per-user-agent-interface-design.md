# 接口设计：Codex Chat 多 Agent 支持 + 体验优化

> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。下文不是当前生产部署、验收或排障指引。

## 一、多 Agent 支持

### 对外接口（无变化）
无 HTTP API 或飞书 Webhook 变更。

---

### 新增：`ClaudeCodeRunner`

**位置**：`app/services/codex_chat/claude_runner.py`

实现与 `CodexExecRunner` 相同的 `RunnerPort` 协议（`run(*, session_id, user_text, on_event) -> dict`）。

**命令构造：**
```python
args = [
    self.cli_path,
    "--print",
    "--output-format", "stream-json",
    "--verbose",
    "--dangerously-skip-permissions",   # 非交互模式必须；上线前须补 OS 用户隔离（见架构文档）
    "--model", self.model,              # 必须显式传，避免依赖本地默认配置
]
if session_id:
    args.extend(["--resume", session_id])
args.append(user_text)
# 必须用 list 形式传给 Popen，禁止拼 shell 字符串（防止 user_text 含特殊字符注入）
```

`model` 作为 `ClaudeCodeRunner.__init__` 的参数，默认值可从 env var `CODEX_CHAT_CLAUDE_MODEL` 读取，写入 `codex_chat_users.cli_path` 旁边的配置（或后续加 `model` 列）。

**事件解析（已修正，基于实测）：**

> Claude Code stream-json 默认模式下，`assistant` 事件里的 `content[]` 是**完整内容块快照，不是增量 delta**。一次响应可能产生多个 assistant 事件（thinking 块 + text 块），每块是该块全量文本。

```python
# init：取 session_id（最先出现）
if event.get("type") == "system" and event.get("subtype") == "init":
    observed_session_id = event.get("session_id") or observed_session_id

# assistant：全量文本块（过滤 thinking，只取 text 块）
if event.get("type") == "assistant":
    for item in event.get("message", {}).get("content", []):
        if item.get("type") == "text" and item.get("text"):
            # 用 _append_text 去重，不能累加（全量快照重复 append 会重复刷屏）
            final_text = _append_text(final_text, item["text"])
            on_event({"type": "assistant_text", "text": item["text"]})
        # type=="thinking" 块：跳过，不发给用户

# result：权威 token 来源（assistant 事件的 usage 是中间快照，不可累加）
if event.get("type") == "result":
    if not event.get("is_error"):
        final_text = event.get("result") or final_text
    token_usage = _extract_claude_token_usage(event.get("usage") or {})
```

**token 用量字段映射（`_extract_claude_token_usage`）：**
```python
# 只从 result 事件提取，不处理 assistant 事件里的中间快照
{
    "input_tokens": usage.get("input_tokens"),
    "output_tokens": usage.get("output_tokens"),
    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
}
```

**heartbeat 必须实现：**
```python
# 与 CodexExecRunner 相同，复用 _read_lines_with_timeout + on_idle
# Claude Code --print 遇阻塞会挂死，heartbeat 是唯一 stale-run 检测手段
line_iter = _read_lines_with_timeout(proc, fd, self.timeout_seconds,
    on_idle=lambda elapsed: on_event({"type": "heartbeat", "elapsed_seconds": elapsed}))
```

---

### 新增：`deps_from_user(user, token_fn)`

**位置**：`app/services/codex_chat/feishu_runtime.py`

```python
def deps_from_user(user: CodexChatUser, token_fn: Callable[[], str]) -> CodexChatDeps:
    if user.provider == "claude":
        runner = ClaudeCodeRunner(cli_path=user.cli_path, cwd=user.cwd)
    else:
        runner = CodexExecRunner(cli_path=user.cli_path, cwd=user.cwd)
    return CodexChatDeps(
        messenger=FeishuCodexMessenger(token_fn),
        streamer=FeishuCardKitStreamer(token_fn),
        runner=runner,
        entitlement_username=user.entitlement_username,
        execution_username=user.execution_username,
        codex_cwd=user.cwd,
        codex_cli_path=user.cli_path,
    )
# 注意：token_fn 是延迟求值闭包，不要在构建 deps 时固化 token（会过期）
```

---

### 修改：`handle_feishu_private_message`

```python
def handle_feishu_private_message(db, *, open_id, chat_id, message_id, text, token_fn, ...):
    if not should_try_codex(open_id):
        return False
    user = codex_chat_service.ensure_entitled_user(db, open_id)
    if user is None:
        return False
    return codex_chat_service.handle_private_message(
        db, open_id=open_id, ..., deps=deps_from_user(user, token_fn)
    )
```

---

### 修改：`ensure_entitled_user` 签名

```python
# 之后（仅查 DB，去掉 deps 参数和 seed 逻辑）
def ensure_entitled_user(db: Session, open_id: str) -> CodexChatUser | None:
    user = db.query(CodexChatUser).filter_by(feishu_open_id=open_id).first()
    return user if user and user.entitlement_status == "active" else None
```

---

### 管理操作

无 API，管理员直接写 DB：

```sql
-- 新增 Claude Code 用户（王志鹏）
INSERT INTO codex_chat_users
  (feishu_open_id, feishu_name, entitlement_username, execution_username,
   role, entitlement_status, entitlement_source, provider, cli_path, cwd)
VALUES
  ('ou_58f29ff5f96f8527d007437111207742', '王志鹏', 'wangzp', 'wangzp',
   'user', 'active', 'manual',
   'claude', '/usr/bin/claude', '/home/biai-agent/users/wangzp/bi-agent-work');
```

---

## 二、状态行下沉

> **注意**：Opus review 指出 `service.py` 的 `_render_stream_text` 当前已经是 body 内布局（状态 + `\n\n` + 正文），标题走 `_short_card_title`。**实现前先核对现状**，实际改动可能远小于预期。

改动集中在 `app/feishu/cardkit.py`：将"正在思考…"/"已完成·用时Xs"状态文字从卡片 `header.title` 移到 `body` 末尾（Markdown 分隔线 + 状态行），`header.title` 只保留用户提问摘要。

---

## 三、审批/权限流程消息合并

### 进度卡片设计约束（Opus review 补充）

- 飞书 CardKit 更新元素内容（`PUT /cardkit/v1/cards/{card_id}/elements/{element_id}/content`）必须携带**单调递增的 `sequence`** 和幂等 `uuid`，否则乱序更新被丢弃或覆盖错乱
- 进度卡跨多次 service 调用（审批→开户→MCP→邮件），`sequence` 不能存进程内变量，**必须持久化**（类似 `CodexChatStreamUpdate.sequence` 列）
- `create_progress_card` 返回值需同时包含 `card_id`（更新用）和 `message_id`（群内定位用）

### 新增：进度卡片 API（`app/feishu/cardkit.py`）

```python
def create_progress_card(open_id: str, title: str, steps: list[str], token: str) -> dict:
    """
    发送初始进度卡片。
    返回：{"card_id": "...", "message_id": "...", "element_id": "..."}
    """

def update_progress_step(
    card_id: str, element_id: str, sequence: int,
    step_index: int, status: str, detail: str, token: str
) -> None:
    """
    更新某步骤状态（running / done / failed）。
    sequence 必须单调递增，由调用方从持久化存储读取并递增后传入。
    """

def complete_progress_card(
    card_id: str, element_id: str, sequence: int, token: str
) -> None:
    """标记整张卡片完成。"""

def fail_progress_card(
    card_id: str, element_id: str, sequence: int,
    step_index: int, error: str, token: str
) -> None:
    """标记某步骤失败，卡片显示失败原因。"""
```

### 修改：provisioning service

```python
# 开始时（sequence 从 DB 读，初始为 0）
card = create_progress_card(user_open_id, "正在为您开通账号", steps=[...], token=token)
# 持久化 card_id, element_id, message_id, sequence=0 到 DB

# 每步完成时（从 DB 读 sequence，+1 后写回）
seq = db_read_and_increment_sequence(card_id)
update_progress_step(card["card_id"], card["element_id"], seq, step_index=0, status="done", detail="...", token=token)

# 全部完成时
seq = db_read_and_increment_sequence(card_id)
complete_progress_card(card["card_id"], card["element_id"], seq, token=token)
```
