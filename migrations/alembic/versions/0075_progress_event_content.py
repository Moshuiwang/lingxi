"""``task_delivery_event`` CHECK 放宽：``progress`` 事件允许携带受限长度的语义化
进度内容。

Revision ID: 0075_progress_event_content
Revises: 0071_daily_report_watermark
Create Date: 2026-08-27

来源：Issue #328 E-Q 批次 opus 审查 R1（真库实测发现的红线缺陷，编排者裁定走
放宽方案，不是回退功能）。语义化进度（Issue #321 方向 C，产品负责人 2026-08-27
裁定）在 ``apps/worker/service.py`` 把 ``"querying:N"``/``"composing"`` 这两种
固定形状之一写进 ``progress`` 事件的 ``content`` 字段，撞上本迁移之前 ``0059``
定的 CHECK（``event_type IN ('safely_releasable_answer','terminal') OR content
IS NULL``——只有这两类事件允许带正文，``progress`` 不在其中）：真库实测
**100% CheckViolation**，且被 ``apps/worker/service.py::_append_event`` 的通用
``except Exception`` 吞成一条 ``logger.error``，真实环境卡片因此完全不动、
体验比改动前更差（用户此前至少能看到「正在处理」的默认文案，现在因为异常
一直往上抛到写入函数收口，节流状态却已经推进，后续更新还会被节流吞掉）。

**为什么是放宽 CHECK 而不是回退 Issue #321 方向 C 的功能**：产品负责人已经
裁定语义化进度是既定功能（2026-08-27，#321 评论 5434086490），真库红线是实现
缺陷（迁移没跟上应用层新增的写入形状），不是产品要求本身有问题。

**放宽的范围刻意收紧，不是敞开口子**：新 CHECK 只多允许 ``progress`` 事件带
``content``，且额外加了一条 ``char_length(content) <= 32`` 的长度上限——
``progress`` 的 ``content`` 只可能是 ``card_stream.encode_progress_action`` 产出
的两种固定形状之一（``"composing"``，9 字节；``"querying:" + 位数不多的计数``），
是 worker 侧内部生成的短令牌，绝不是用户输入、模型输出或任何自由文本；32 是
留了充裕余量的上限，不是精确贴着已知最长值算出来的，写进 CHECK 是为了防未来
误用把这条口子当成"progress 可以带任意长度正文"（应用层的同一条契约同步写进
``src/lingxi/core/delivery/ports.py`` 的 ``PROGRESS_CONTENT_MAX_LENGTH``/
``CONTENT_BEARING_EVENT_TYPES``，供写入方在真正落库之前先自查一遍，见该文件
新增的 ``assert_content_allowed``）。``safely_releasable_answer``/``terminal``
两类既有内容承载类型的约束不变（仍然不限长度，那是用户可见的问数结果正文，
篇幅由业务内容决定）。

**体量核算（这次放宽会不会显著抬高 ``task_delivery_event`` 的行数）**：
``apps/worker/service.py`` 里 progress 写入受两道节流约束——事件驱动最短间隔
``_PROGRESS_MIN_UPDATE_INTERVAL_SECONDS=5`` 秒、兜底计时最长间隔
``_PROGRESS_FALLBACK_SECONDS=30`` 秒——因此单个任务无论跑多久，真正落库的
progress 行最密也是每 5 秒一条；对照 ``core/execution/guardrails`` 现行的单任务
墙钟上限量级（约十分钟级别，600 秒），一个任务最多产生约 600/5 = 120 条
progress 行，每行 ``content`` 至多 32 字节——这是一次性、有界的增量，不是无界
增长。``0059`` 文件头部登记的"``task_delivery_event`` 没有任何物理删除路径，
低敏事实一旦写入永久保留、不受九十天上限约束"这一既有留白**现状不变**：本迁移
只放宽 CHECK 约束的取值范围，不改动清理路径、不新增列、不改变任何行的生命周期
——该留白仍然只留给它原本登记的后续加固 Story，不在本次修复里处理，这里的
体量核算只是确认放宽本身不会让那条已知留白的严重程度发生数量级变化。

``downgrade()`` 收窄回原 CHECK：若届时已有 ``progress`` 行带 ``content``（放宽
生效期间产生的语义化进度记录），收窄会如实失败——与 ``0058``/``0059`` 的既有
先例一致，不做静默数据修复。
"""

from __future__ import annotations

from alembic import op

revision: str = "0075_progress_event_content"
down_revision: str | None = "0071_daily_report_watermark"
branch_labels: str | None = None
depends_on: str | None = None


# 被替换的这条 CHECK 是 0059 建表时未命名的表级约束，Postgres 按声明顺序自动
# 分配名字 `task_delivery_event_check2`（同一张表另外两条未命名表级 CHECK 依次
# 拿到 `_check`/`_check1`，列级 CHECK 各自按列名命名，互不冲突）——名字已经在
# 一次性隔离容器里对真实 0059 建表 DDL 实测确认（`pg_get_constraintdef` 回读
# 结果为 `CHECK (((event_type = ANY (ARRAY['safely_releasable_answer'::text,
# 'terminal'::text])) OR (content IS NULL)))`，与本迁移要替换的目标逐字匹配），
# 不是按声明顺序数出来的猜测。
_UPGRADE_SQL = r"""
ALTER TABLE task_delivery_event DROP CONSTRAINT task_delivery_event_check2;
ALTER TABLE task_delivery_event ADD CONSTRAINT task_delivery_event_check2
    CHECK (
        content IS NULL
        OR event_type IN ('safely_releasable_answer', 'terminal')
        OR (event_type = 'progress' AND char_length(content) <= 32)
    );
"""


_DOWNGRADE_SQL = r"""
ALTER TABLE task_delivery_event DROP CONSTRAINT IF EXISTS task_delivery_event_check2;
ALTER TABLE task_delivery_event ADD CONSTRAINT task_delivery_event_check2
    CHECK (event_type IN ('safely_releasable_answer', 'terminal') OR content IS NULL);
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
