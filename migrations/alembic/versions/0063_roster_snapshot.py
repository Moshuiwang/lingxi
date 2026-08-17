"""花名册持久快照载体：最近一份成功、完整、非空的读取结果。

Revision ID: 0063_roster_snapshot
Revises: 0062_onboarding_dispatch_ledger
Create Date: 2026-08-17

Issue [#52](https://github.com/Moshuiwang/lingxi/issues/52) 的 S-B-02。产品负责人
2026-08-08 的 D2 裁定**推翻了此前的「零新表」定案**：每日花名册比对需要一个跨进程
重启与常规发布都还在的持久载体，「源头返回空 / 读取失败 / 超时 / 半轮」一律保留上一份
有效快照而不是覆盖它。没有这张表时，「上一份」只存在于进程内存里——重启就没了，而
花名册那天恰好读空的话，全体已开通用户会被一次性报成「移除」。

**两张表而不是一张**：快照的元信息（读取时间、完整性事实）与行是一对多，且替换时
必须整体换掉。行表通过 ``ON DELETE CASCADE`` 挂在元信息上，删元信息那一行就等于
删掉整份快照，替换因此可以在**同一个事务**里做完，中途失败不留半份
（`V-花名册-42`）。

**``singleton`` 列是「最多一份快照」的数据库级保证**（`V-花名册-45`）。它只能取
``TRUE``（``CHECK``）且带 ``UNIQUE``，于是第二条快照行在数据库层就插不进去。这条约束
不是洁癖：本表存的是全体在册人员的姓名 / 工号 / 邮箱，一旦允许多份并存，「保留最近
一份」就退化成应用代码的自觉，而积累下来的历史版本没有任何用途、却每一份都是一份
可识别人员数据。用约束而不是靠调用方记得先删，理由同数据库设计「一、设计原则」第 1 条。

**行表不去重、不加人员 ID 唯一约束**：同一人员 ID 多行是花名册的实测常态
（2026-08-05 受控读取：1206 行 / 1177 个不同人员 ID），去重会把歧义变成静默的错误
选择（`V-花名册-06`）。``row_index`` 保留读取次序，让重复行可以原样回读。人员 ID 上
只建**局部**索引且跳过空值：没有人员 ID 的行与任何用户都对不上，把它们放进索引只会
让索引替一批永远查不到的行付出维护成本。

**不建 ``app_user`` 外键**：花名册是全公司在册人员表，绝大多数行不是 Lingxi 用户。
外键会把「花名册里有这个人」错误地表达成「Lingxi 认识这个人」，也会让快照替换被
`app_user` 的行删除顺序绑架。花名册**不是权限权威**（产品合同「外部平台边界」），
本表同样只是一份下游资料副本。

**保留期已由产品负责人裁定（2026-08-17）：始终保留最近一份，豁免九十天规则。**
因此本表**刻意没有** ``expires_at``、没有到期触发器，也不在 ``0054`` 的受限清理函数
覆盖范围内——这不是遗漏。裁定的三句话：

1. **始终保留最近一份**：它是每日比对的花名册侧基线，删掉等于让比对失去基线，
   属于「继续提供服务所需的当前状态」（数据库设计[「当前状态与历史内容分开」]
   (../../../docs/技术设计/数据库设计.md#当前状态与历史内容分开)）；
2. **替换即删旧**：靠 ``singleton`` 唯一约束与 ``ON DELETE CASCADE``，库里永远只有
   一份，不积累历史版本——这正是豁免九十天上限的前提，本表不会「越攒越多」；
3. **超龄不自动删，改为按日报告警提醒**：源头长期读不到导致快照变旧时，管理员要
   知道「基线在变旧」，而不是某天发现基线被悄悄删了。告警事实已由本 Story 的
   ``core/identity/roster_snapshot.py`` 产出（含上一份快照的读取时间与年龄），
   **接线到日报 / 告警状态机属 S-B-04**。

**不授任何表权限**：与 ``0059``/``0061`` 同型，本 revision 不写 ``GRANT``——四个数据库
角色的表级授权当前只在 ``0054`` 里针对它自己那两张表出现，运行时进程也尚未以这些
角色连库（当前能力已登记）。这里凭空授权会造出一份与实际连接身份对不上的授权面。

``downgrade()`` 真实可执行：两张表都是本 revision 新建的，按依赖反序整体删除，不存在
需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0063_roster_snapshot"
down_revision: str | None = "0062_onboarding_dispatch_ledger"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE roster_snapshot (
    id                        TEXT        PRIMARY KEY,           -- ULID, rsn_*
    -- 只能是 TRUE 且唯一：任何时刻最多一份快照，理由见文件头部。
    singleton                 BOOLEAN     NOT NULL DEFAULT TRUE
        CHECK (singleton) UNIQUE,
    -- 整轮读完的时刻（UTC）。「快照有多旧」是保旧告警要报给管理员的核心事实。
    captured_at               TIMESTAMPTZ NOT NULL,
    -- 这一份被装进库的时刻，与 captured_at 分开：读取时间来自读取方，装载时间来自库。
    installed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 只有 COMPLETE（整轮读完、完整性通过、非空）才允许成为快照，因此行数恒为正。
    row_count                 INTEGER     NOT NULL CHECK (row_count > 0),
    pages_read                INTEGER     NOT NULL CHECK (pages_read > 0),
    -- 源头自报总数。飞书是否一定返回 total 未经回源确认，允许缺失；缺失时
    -- total_matches_rows 为 NULL，表示「没法判定」而不是「判定为不一致」。
    reported_total            INTEGER     CHECK (reported_total IS NULL OR reported_total >= 0),
    total_matches_rows        BOOLEAN,
    rows_without_personnel_id INTEGER     NOT NULL DEFAULT 0
        CHECK (rows_without_personnel_id >= 0),
    -- 完整性事实只落**计数**，键是列名或键字段名，值是数字：整张元信息表里
    -- 没有任何一个人员资料值（`V-花名册-46`，与 `V-花名册-33` 同口径）。
    blank_column_rows         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    duplicate_keys            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    duplicate_rows            JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE roster_snapshot_row (
    snapshot_id  TEXT    NOT NULL REFERENCES roster_snapshot(id) ON DELETE CASCADE,
    -- 读取次序。重复行原样保留，靠它区分，不做任何去重（`V-花名册-06`）。
    row_index    INTEGER NOT NULL CHECK (row_index >= 0),
    -- 五列与 RosterRow 逐一对应；缺字段落空串而不是 NULL，与读取层的归一口径一致，
    -- 让「花名册这个字段是空的」在比对时不必再区分 NULL 与空串两种空。
    personnel_id TEXT    NOT NULL,
    email        TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    employee_no  TEXT    NOT NULL,
    record_id    TEXT    NOT NULL,
    PRIMARY KEY (snapshot_id, row_index)
);

-- 局部索引：没有人员 ID 的行与任何用户都对不上，不进索引。
CREATE INDEX roster_snapshot_row_personnel_idx
    ON roster_snapshot_row (personnel_id)
 WHERE personnel_id <> '';
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS roster_snapshot_row;
DROP TABLE IF EXISTS roster_snapshot;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062 同型：不走 ``op.execute()``，避免空参数集
    触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
