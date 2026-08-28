"""``onboarding_failure``：按追溯号落一行开通失败原因，供 ``/admin trace <追溯号>``
只读查询消费。

Revision ID: 0077_onboarding_failure_reason
Revises: 0075_progress_event_content
Create Date: 2026-08-28

[Issue #337](https://github.com/Moshuiwang/lingxi/issues/337)（Trace #373 H3 批
S-H3-1）：现状登记「管理员凭追溯号自助查询失败原因」只能靠检索 scheduler 容器日志
（`apps/scheduler/audit.py` 的 `StructuredLogAuditSink`），`python -m lingxi.apps.trace`
只读运维命令查得到入站事件/开通状态/发布意图/就绪探针，唯独查不了 `failure_reason`
——`core/identity/onboarding_runner.py` 模块文档「一条链失败中断时」与
`apps/trace/__init__.py` 模块文档「能回答什么、回答不了什么」都明确登记了这条缺口，
称其为「可选的后续迁移」。本迁移就是把它做掉。

## 合并次序（重要，交给编排者执行；本节 2026-08-28 订正）

**派工卡对链头的假设有误，已本地实测订正**：派工卡称"S-H3-3（用户记忆）同批占用
`0076_user_memory`（`down_revision="0075_progress_event_content"`）……本卡如需迁移
……`down_revision` 先写 `0075_progress_event_content`（当前链头）"——但
`scripts/ci/check_alembic_revisions.py` 对基线 SHA（`e86f1f6`，H3 批次分支基线）
实测：**当前链唯一头是 `0073_pending_action_perm_types`，不是 `0075`**。链的真实
拓扑（文件名数字不是合并顺序，只是历史编号）：`...0070 → 0071 → 0075 → 0074 → 0072
→ 0073`（头）——`0075` 早已有子节点（`0074`），不是叶子。若 S-H3-3 与本卡都按派工卡
原话把 `down_revision` 指向 `0075`，会同时产生三个头（`0073`/`0076`/`0077`），链检查
必然红。

本 revision 因此把 `down_revision` **改指向实测的真实当前头** `0073_pending_action_
perm_types`，保证本分支单独跑 `check_migration_chain.sh` 时链是绿的、单头。
**编排者收口时必须重新核实 S-H3-3（`0076_user_memory`）当时实际使用的
`down_revision`**：若它也被同一条错误假设影响而指向 `0075`，两条迁移都需要按
实际合并次序重新排链（谁先合并、后合并者的 `down_revision` 改指向先合并者的
revision id），不能假设本文件写下这段话时的排序就是最终排序。

## 为什么是一张新窄表，不是 S9 那张大而全的 ``audit_event``

`docs/技术设计/接口设计.md` 联合设计 §7.2「级 2」把 `failure_reason` 落库列为「可选的
后续迁移」，`audit_event` 表本身属于另一条独立的 S9 切片（结构化审计检索，范围远大于
「按追溯号查一次失败原因」）。本卡的判据只有一条：`/admin trace <追溯号>` 能凭
`trace_id` 查回 `failure_reason`。为此起一张大而全的审计表是过度设计——S9 真正立项时
如果它的形状能覆盖本表的查询需要，届时可以把本表的数据迁移过去并废弃它，但不该反过来
让一个 MVP 只读查询卡等一张远未排期的表。

## 数据来源：两个已有的审计写出点，`INSERT ... ON CONFLICT DO NOTHING`

两处调用方（详见 `core/identity/onboarding_ports.FailureReasonRecorder` 协议文档）：

- `core/identity/onboarding_runner.py::AutoOnboardingRunner._execute`——每一次开通链
  跑到终态、且 `terminal.reason` 非空（即真的是一次失败，不是成功完成）时，紧邻既有
  `self._audit.record("onboarding.result", ...)` 调用之后落一行，`event_type=
  'onboarding.result'`。覆盖面包含 `_KEYS_REQUIRING_REFERENCE`（`KEY_INTERNAL_ERROR`/
  `KEY_SYNC_TIMEOUT`）——这两个终态是**唯一**会把追溯号亮给用户看的两种措辞，管理员
  能拿到的每一个追溯号理论上都在这条写出点覆盖范围内。
- `apps/scheduler/stalled_provisioning.py::StalledProvisioningDuty._process_one`——链
  本身死掉（进程被强杀等，`onboarding.result` 从未被写出）、由停摆扫描职责租约到期
  收口时，紧邻既有 `self._audit.record("stalled_provisioning.aborted", ...)` 调用之后
  落一行，`event_type='stalled_provisioning.aborted'`，`failure_reason` 固定为该职责
  自己已经在用的字面量 `'stalled_lease_expired'`。

**「同事务」的实现取舍**：本仓库的适配器写入约定是每次调用各自 `with connect(...) as
connection` 独立开合，`onboarding.result`/`stalled_provisioning.aborted` 两个审计
写出点本身只是结构化日志调用（`_AuditSink.record`），紧邻它们的调用栈里没有一个
**已经打开、可供加入**的数据库事务可以字面意义上"共享"。本迁移把两处新增写出实现成
——紧邻既有审计调用、同步执行的**一条单语句 INSERT**（`adapters/
postgres_onboarding_failure.py::PostgresFailureReasonRecorder.record_failure`），
在这个仓库现有的写入模型下就是能做到的最强原子性；不是异步、不是"最终会补写"的旁路。
落库失败按本仓库既有的"次要写出最佳努力"纪律处理（同
`AutoOnboardingRunner._notify_admin_of_failure`）：不带走已经决定的终态或已完成的
收口，只记一条自己的失败审计。

**已知边界（如实登记，本卡不覆盖）**：`AutoOnboardingRunner.start()` 里两条**同步**
返回 `INTERNAL_ERROR`（因此同样带追溯号）的分支——`should_stop()` 为真时的
`"stopping"`、提交执行器失败时的`"executor_unavailable"`——从不经过 `_execute`，
因此从不触发 `onboarding.result` 审计，本迁移也就查不到这两种场景的 failure_reason。
两者结构上罕见（进程正在停机、或执行器队列已满/已停），且已经各自有独立审计事件名
（`onboarding.start_declined_while_stopping`/`onboarding.rejected_by_executor`）
可供检索容器日志兜底；留给下一次拆分批评估是否需要补第三个写出点。

## 幂等：``trace_id`` 主键 + ``ON CONFLICT DO NOTHING``

`trace_id` 是主键（唯一），两个写出点都用 `INSERT ... ON CONFLICT (trace_id) DO
NOTHING`——同一条链正常只会产生一次终态，若因为进程重启等原因让同一个 `trace_id`
被处理第二次，先落的那一行保持不变，不覆盖、不报错。**不存用户查询内容**：
`failure_reason` 是内部原因码字面量（如 `"directory_unavailable"`/
`"mcp_sync_timeout"`/`"stalled_lease_expired"`），不是自由文本，不含姓名、邮箱或
用户消息正文。

``downgrade()`` 是数据丢失操作：一旦部署环境写过失败原因，`DROP TABLE` 会把它们连同
表一起清空——与 `0067`/`0075` 等既有 revision 同一姿态，不做静默数据修复。
"""

from __future__ import annotations

from alembic import op

revision: str = "0077_onboarding_failure_reason"
#: 实测当前链头（见文件头部「合并次序」订正说明）——不是派工卡原话的 0075。
down_revision: str | None = "0073_pending_action_perm_types"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE onboarding_failure (
    -- ULID，与产生它的那一次开通/收口共用同一个 trace_id（core/ids.new_ulid()）。
    -- 主键即唯一：见文件头部「幂等」一节，两处写入方都用 ON CONFLICT DO NOTHING。
    trace_id          TEXT        PRIMARY KEY
        CHECK (NULLIF(BTRIM(trace_id), '') IS NOT NULL),

    -- 内部原因码字面量（如 'directory_unavailable'/'mcp_sync_timeout'/
    -- 'stalled_lease_expired'），不是自由文本、不含身份信息或用户消息正文。
    failure_reason    TEXT        NOT NULL
        CHECK (NULLIF(BTRIM(failure_reason), '') IS NOT NULL),

    -- 是哪一个审计写出点产生的这行——'onboarding.result'（编排层终态）或
    -- 'stalled_provisioning.aborted'（停摆扫描职责租约到期收口）。写死取值范围，
    -- 不接受任意字符串：这张表只服务这两个已知来源，见文件头部说明。
    event_type        TEXT        NOT NULL
        CHECK (event_type IN ('onboarding.result', 'stalled_provisioning.aborted')),

    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

#: **数据丢失操作**：见文件头部「downgrade」一节。
_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS onboarding_failure;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision（如 0067/0075）同型：不走 ``op.execute()``，避免空参数集
    触发插值模式。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
