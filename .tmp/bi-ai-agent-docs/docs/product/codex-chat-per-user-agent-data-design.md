# 数据设计：Codex Chat 多 Agent 支持

> 状态：历史归档。产品已决定不再提供 Codex；当前飞书查询只保留 Claude 路径（2026-07-22）。下文不是当前生产部署、验收或排障指引。

## 变更表：`codex_chat_users`

### 新增列

| 列名 | 类型 | 可空 | 说明 |
|------|------|------|------|
| `provider` | `VARCHAR(32)` | NOT NULL, DEFAULT `'codex'` | Agent 类型：`codex` 或 `claude` |
| `cli_path` | `VARCHAR(512)` | NOT NULL | AI Agent 可执行文件绝对路径 |
| `cwd` | `VARCHAR(512)` | NOT NULL | Agent 启动工作目录绝对路径 |

**设计决策：三列均为 NOT NULL。** 不使用 NULL + 运行时 fallback 机制，避免新用户静默继承全局默认值，也使配置意图明确可审计。Migration 时对存量行回填明确值。

### 完整表结构（变更后）

```sql
CREATE TABLE codex_chat_users (
    id                   INTEGER      NOT NULL PRIMARY KEY,
    feishu_open_id       VARCHAR(64)  NOT NULL UNIQUE,
    feishu_name          VARCHAR(128) NOT NULL,
    entitlement_username VARCHAR(64)  NOT NULL,
    execution_username   VARCHAR(64)  NOT NULL,
    role                 VARCHAR(32)  NOT NULL,
    entitlement_status   VARCHAR(32)  NOT NULL,
    entitlement_source   VARCHAR(64)  NOT NULL,
    provider             VARCHAR(32)  NOT NULL DEFAULT 'codex',  -- 新增
    cli_path             VARCHAR(512) NOT NULL,                   -- 新增
    cwd                  VARCHAR(512) NOT NULL,                   -- 新增
    created_at           DATETIME     NOT NULL,
    updated_at           DATETIME     NOT NULL
);
```

### ORM Model 同步

`app/models/codex_chat.py` 的 `CodexChatUser` 类需同步加三个 `mapped_column`：

```python
provider: Mapped[str] = mapped_column(String(32), nullable=False, default="codex")
cli_path: Mapped[str] = mapped_column(String(512), nullable=False)
cwd: Mapped[str] = mapped_column(String(512), nullable=False)
```

### Migration

**复用现有 `_ensure_sqlite_schema()` 模式**（`app/db.py`），用 `PRAGMA table_info` 检测列是否存在再 `ALTER TABLE ADD COLUMN`。

SQLite 支持 `ADD COLUMN ... NOT NULL DEFAULT '...'`（带 DEFAULT 时允许 NOT NULL），因此可一步到位：

```sql
-- 加列（带 DEFAULT，SQLite 支持）
ALTER TABLE codex_chat_users ADD COLUMN provider VARCHAR(32) NOT NULL DEFAULT 'codex';
ALTER TABLE codex_chat_users ADD COLUMN cli_path VARCHAR(512) NOT NULL DEFAULT '';
ALTER TABLE codex_chat_users ADD COLUMN cwd      VARCHAR(512) NOT NULL DEFAULT '';

-- 回填存量行（DEFAULT '' 只是占位，必须显式回填实际值）
UPDATE codex_chat_users
SET provider = 'codex',
    cli_path = '/home/wangzhipeng/.local/bin/codex',
    cwd      = '/home/wangzhipeng/projects/bi-ai-agent'
WHERE feishu_open_id = 'ou_ecec4e4ba5716773a58a14789fb623ae';
-- 注意：回填必须覆盖所有存量行（含 entitlement_status != 'active' 的 pending 行）

-- 启动自检（在 _ensure_sqlite_schema 或 startup 里加）：
-- 扫 cli_path='' 或 cwd='' 的行并 LOG ERROR，避免静默黑洞
```

## 目标数据状态

| feishu_name | feishu_open_id | execution_username | provider | cli_path | cwd |
|---|---|---|---|---|---|
| 四达文档会议助手 | `ou_ecec4e4ba5716773a58a14789fb623ae` | wangzhipeng | codex | `/home/wangzhipeng/.local/bin/codex` | `/home/wangzhipeng/projects/bi-ai-agent` |
| 王志鹏 | `ou_58f29ff5f96f8527d007437111207742` | wangzp | claude | `/usr/bin/claude` | `/home/biai-agent/users/wangzp/bi-agent-work` |

## 安全约束

- `cli_path` 和 `cwd` **只允许管理员直接写 DB**，审批流程不得让申请用户自填这两列
- `cli_path` 写入前管理员需确认路径在 EC2 上存在且可执行
- `provider` 取值仅限 `codex` / `claude`，应用层做枚举校验

## 不涉及的表

- `codex_chat_conversations`、`codex_chat_sessions`、`codex_chat_runs`：不变，已通过 `user_id` 实现会话数据隔离
- 其余所有表：不变
