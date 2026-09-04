"""``innertest_content_capture`` 的九十天到期删除（对抗审查 2026-09-02 C-7）。

这张表（迁移 ``0069``）保存的是用户问题原文、模型回答原文与工具调用详情，是全仓库
内容密度最高的一张表。迁移里 ``expires_at`` 触发器（固定 ``created_at + 2160 小时``）
与到期扫描索引都建好了，却**没有任何调用方**：九十天上限只存在于一个没人读的列里。
缺的不是机制，是职责——与 ``mcp_sync_check``/``onboarding_completion_notice`` 当年
是同一个形状的缺陷，处置也照它们（``apps/scheduler/retention.py``）。

**为什么单独一个模块，而不是挂在**
:mod:`lingxi.adapters.postgres_content_capture` **上**：那个模块是写入侧，构造入参
要 ``ContentCaptureRecord``，而那个类顺着 ``core/innertest_content_capture.py`` 会把
整个 ``core.execution``（工具判定 + 审计脱敏）拉进 import 闭包。删除侧的唯一调用方
是 scheduler，它没有任何理由为了删几行而背上 worker 的执行层。删除只需要连接串。

**为什么不进迁移 ``0054`` 的受限清理函数**：那条分工（删除权限只存在于无登录角色
``lingxi_retention_owner`` 上、适配器一条 DELETE 都不写）适用于函数已经覆盖的两张
父表；本表与 ``mcp_sync_check``/``onboarding_completion_notice`` 一样走应用层语句，
理由见 ``apps/scheduler/retention.py`` 的 ``PermissionRetentionSweepDuty`` 文档。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

logger = logging.getLogger(__name__)


class PostgresContentCaptureRetention:
    """``innertest_content_capture`` 到期行的唯一删除入口。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def purge_expired(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """删除过了九十天上限的采集记录，返回删除条数（C-7）。

        **删整行而不是擦某一列**：这张表的三个内容列（用户问题原文、模型回答原文、
        工具调用详情）合起来就是它存在的全部理由，擦光等于留下一具空壳；表本身
        也只在 stage 内测轮写入，没有"行还在但内容没了"这种需要保留的运行事实。
        与 ``mcp_sync_check`` 的 :meth:`purge_expired_checks` 同一形态。

        到期判据只有 ``expires_at <= now``，**一个条件都不多加**：清理职责能扩大
        删除面的唯一方式就是"顺手多删一点"，而多删掉的东西没有恢复路径。
        ``expires_at`` 由迁移 ``0069`` 的触发器固定为 ``created_at + 2160 小时``，
        调用方写什么都会被覆盖，因此这里读到的到期时间不可能被伪造成更早。

        小批量 + 每轮一次（不循环到删空）：一次调用就是一个事务，没有半批状态；
        积压交给下一轮，两条都是幂等的。
        """

        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("到期判定时间必须带时区")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                cursor = connection.cursor()
                cursor.execute(
                    """
                    DELETE FROM innertest_content_capture
                     WHERE id IN (
                           SELECT id FROM innertest_content_capture
                            WHERE expires_at <= %s
                            ORDER BY expires_at
                            LIMIT %s
                     )
                    """,
                    (moment, limit),
                )
                purged = cursor.rowcount
        if purged:
            # 只记条数，不取任何行内容（这张表的每一行都是用户问答原文）。
            logger.info("内测轮采集记录已到期删除 条数=%s", purged)
        return purged
