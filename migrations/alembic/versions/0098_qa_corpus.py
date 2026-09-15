"""问答留存语料 ``qa_corpus`` 与窄读取角色 ``qa_corpus_reader``：结构性不到期、只追加。

Revision ID: 0098_qa_corpus
Revises: 0097_operation_audit

## 为什么

产品合同「数据保留与删除」第三条例外：为 Agent 自演化设立的问答留存语料通道，对每一次
真实问数保留用户问题原文、用户实收的安全回答正文与模型原文回答（两者都存）以及产生它的
工具调用详情，**不设保留上限、明文落库**；只写入为此设立的独立正式表，不复用内测采集
通道（``innertest_content_capture``，默认关闭、九十天到期、轮次结束即清，姿态全反），
也不并入执行层审计。表名刻意不带 ``innertest_`` 前缀。

## 形态：``qa_corpus``

- 每次真实问数一行，``task_id`` 唯一——重领任务不双写（写入侧 ``ON CONFLICT DO NOTHING``）。
- **不建 ``task`` 外键**：任务行九十天只擦 ``prompt`` 不删整行，语料不得随任务清理；
  外键只会把两种生命周期绑在一起。
- ``user_id`` 引用 ``app_user`` 并 ``ON DELETE CASCADE``：用户删除时语料随删。这是合同
  「裁定前该通道不豁免删除规则」的结构落法，是**裁定前默认**；若产品负责人裁定改为
  「不可逆脱敏后继续留存」，需新迁移改外键动作并放开 ``UPDATE`` 拒绝。
- 四段正文各带脱敏命中计数：``question_content`` / ``answer_delivered``（写进投递事件的
  那一份实收正文）/ ``answer_model_raw``（最后一条助手消息整段，未经出口安全投影）/
  ``tool_calls``（形状同 ``0069``：``tool_use_id`` / ``tool_name`` / ``tool_input`` /
  ``result_summary``，结果摘要 4000 字节截断并标 ``truncated``）。凭据形状在应用层按
  ``core/content_redaction.py`` 过滤后计数，表不再做第二次脱敏。
- 终态与来源版本列只收固定码：``terminal_kind`` 与投递事件同一取值域，``user_result``
  是固定码形状，``failure_code`` 只限长度。
- **没有 ``expires_at``**：任何清理职责在这张表上无键可扫，「不到期」是结构性的。
  ``BEFORE UPDATE`` 一律拒绝（同 ``operation_audit``），不建 DELETE 触发器——容量与用户
  删除两项裁定后再定限权面。

## 索引与 ``pg_trgm``

按人 ``(user_id, created_at DESC)``、时间窗 ``(created_at DESC, id)``；关键词检索用
``pg_trgm`` 的 GIN 索引，只建在 ``question_content`` 与 ``answer_delivered`` 上（模型原文
不作检索目标）。托管方没有中文分词扩展，``to_tsvector('simple')`` 把无空格中文整串当一个
词元、子串检索失效；trgm 直接加速 ``ILIKE '%词%'``（少于三个字符退化为顺序扫描，
``%`` / ``_`` 须转义）。``pg_trgm`` 是可信扩展，非超级用户可装。

扩展装进 ``extensions`` schema（不存在则建），不装进 ``public``：托管方本就把扩展放在
那里；本仓的真库用例底座会把 ``public`` 下的函数逐个 DROP 重建，扩展成员函数不能被单独
删除，装进 ``public`` 会让每次重建结构都失败；``public`` 的结构 dump 也因此不含扩展对象。
扩展已经装在别的 schema 时不动它——``upgrade()`` 查 ``pg_extension`` 取实际 schema 再拼
``gin_trgm_ops`` 的限定名。``downgrade()`` **不** ``DROP EXTENSION`` 也不删 schema：
它可能早已存在、可能被别的对象共用。

## ``qa_corpus_reader``

合同写明「只有为此设立的窄读取角色可读」。独立登记表而不是给 ``admin_registry`` 加第四列
（那张表有「active ⇒ 三类全真」的 CHECK 与全集判定，改动爆炸半径大）。``label`` 是角色化
标签不是姓名；同一 ``feishu_open_id`` 同一时刻至多一条 ``active``（部分唯一索引，形状同
``admin_registry``）。授予与撤销的命令、读取与导出的审计接入是后续工作项。

## 受控清理 SQL 姿态

「不到期」不等于「不清理」：本表**不自动删除**，唯一的自动删除是用户删除随删（外键
CASCADE）。容量只告警不删（阈值与告警由后续工作项定义）。其余清理只走登记在这里的受控
SQL（与 ``0069`` 文件头、``migrations/README.md``「运维紧急删除路径」同一姿态），执行前后
各留一行 ``operation_audit``（``corpus.purge``）：

.. code-block:: sql

    -- 先核对范围，再删；<start> / <end> 为 UTC。
    SELECT count(*) FROM qa_corpus WHERE created_at >= '<start>' AND created_at < '<end>';
    DELETE FROM qa_corpus WHERE created_at >= '<start>' AND created_at < '<end>';

## 回滚

两张表任一非空即拒绝降级（应用回退只切镜像不降库，同 ``0091`` / ``0092`` / ``0097``）；
都为空时删两张表与触发器函数，``public`` 完整逆转。
"""

from __future__ import annotations

import re

from alembic import op

revision: str = "0098_qa_corpus"
down_revision: str | None = "0097_operation_audit"
branch_labels: str | None = None
depends_on: str | None = None

#: 扩展落点：托管方的惯例 schema，本地与 CI 不存在时由本 revision 建。
_EXTENSION_SCHEMA = "extensions"
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

_TABLES_SQL = r"""
CREATE TABLE qa_corpus (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL UNIQUE,
 conversation_id TEXT NOT NULL,
 user_id TEXT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
 trace_id TEXT,
 task_created_at TIMESTAMPTZ,
 question_content TEXT NOT NULL,
 question_redaction_count INTEGER NOT NULL DEFAULT 0
  CHECK (question_redaction_count >= 0),
 answer_delivered TEXT NOT NULL,
 answer_delivered_redaction_count INTEGER NOT NULL DEFAULT 0
  CHECK (answer_delivered_redaction_count >= 0),
 answer_model_raw TEXT NOT NULL,
 answer_model_raw_redaction_count INTEGER NOT NULL DEFAULT 0
  CHECK (answer_model_raw_redaction_count >= 0),
 tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb
  CHECK (jsonb_typeof(tool_calls) = 'array'),
 tool_calls_redaction_count INTEGER NOT NULL DEFAULT 0
  CHECK (tool_calls_redaction_count >= 0),
 terminal_kind TEXT NOT NULL
  CHECK (terminal_kind IN ('success','failed','stopped','redacted_withheld','timeout')),
 user_result TEXT NOT NULL
  CHECK (user_result ~ '^[a-z][a-z0-9_]*$' AND length(user_result) <= 64),
 failure_code TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 128),
 output_safety_withheld BOOLEAN NOT NULL DEFAULT FALSE,
 worker_id TEXT NOT NULL,
 worker_version TEXT NOT NULL,
 target_worker_version TEXT NOT NULL,
 system_prompt_digest TEXT,
 model TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX qa_corpus_user_recent_idx
 ON qa_corpus (user_id, created_at DESC);
CREATE INDEX qa_corpus_recent_idx
 ON qa_corpus (created_at DESC, id);

CREATE FUNCTION qa_corpus_append_only() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RAISE EXCEPTION '问答留存语料只追加、不允许修改';
END;
$$;

CREATE TRIGGER qa_corpus_no_update
    BEFORE UPDATE ON qa_corpus
    FOR EACH ROW EXECUTE FUNCTION qa_corpus_append_only();

CREATE TABLE qa_corpus_reader (
 id TEXT PRIMARY KEY,
 feishu_open_id TEXT NOT NULL
  CHECK (NULLIF(BTRIM(feishu_open_id), '') IS NOT NULL),
 label TEXT NOT NULL
  CHECK (NULLIF(BTRIM(label), '') IS NOT NULL),
 entry_status TEXT NOT NULL DEFAULT 'active'
  CHECK (entry_status IN ('active', 'revoked')),
 granted_by TEXT NOT NULL
  CHECK (NULLIF(BTRIM(granted_by), '') IS NOT NULL),
 granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 revoked_at TIMESTAMPTZ,
 CHECK ((entry_status = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE INDEX qa_corpus_reader_open_id_idx
 ON qa_corpus_reader (feishu_open_id);
CREATE UNIQUE INDEX qa_corpus_reader_active_idx
 ON qa_corpus_reader (feishu_open_id) WHERE entry_status = 'active';
"""

_TRGM_INDEX_SQL = (
    "CREATE INDEX qa_corpus_question_trgm_idx"
    " ON qa_corpus USING gin (question_content {schema}.gin_trgm_ops)",
    "CREATE INDEX qa_corpus_answer_trgm_idx"
    " ON qa_corpus USING gin (answer_delivered {schema}.gin_trgm_ops)",
)

_TRGM_SCHEMA_SQL = (
    "SELECT n.nspname FROM pg_catalog.pg_extension e"
    " JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace"
    " WHERE e.extname = 'pg_trgm'"
)


def _ensure_trgm(cursor) -> str:
    """保证 ``pg_trgm`` 可用，返回它实际所在的 schema 名。

    已装在任何 schema 都原样沿用；没装时装进 ``extensions``（不存在则建）。schema 名只
    允许小写标识符形状——它要拼进索引定义，不能信任目录里读到的任意名字。
    """
    cursor.execute(_TRGM_SCHEMA_SQL)
    row = cursor.fetchone()
    if row is None:
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {_EXTENSION_SCHEMA}")
        cursor.execute(f"CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA {_EXTENSION_SCHEMA}")
        cursor.execute(_TRGM_SCHEMA_SQL)
        row = cursor.fetchone()
    schema = row[0] if row else ""
    if not _SCHEMA_NAME.match(schema):
        raise RuntimeError("pg_trgm 所在 schema 的名字不是小写标识符，拒绝拼进索引定义")
    return schema


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(_TABLES_SQL)
        schema = _ensure_trgm(cursor)
        for statement in _TRGM_INDEX_SQL:
            cursor.execute(statement.format(schema=schema))


def downgrade() -> None:
    # 语料与读取角色都是不可再生的记录；应用回退只切镜像，不降级数据库。
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM qa_corpus) "
        "OR EXISTS (SELECT 1 FROM qa_corpus_reader) THEN "
        "RAISE EXCEPTION 'qa corpus records require compatible recovery'; END IF; END $$"
    )
    op.execute("DROP TABLE qa_corpus_reader")
    op.execute("DROP TABLE qa_corpus")
    op.execute("DROP FUNCTION qa_corpus_append_only()")
