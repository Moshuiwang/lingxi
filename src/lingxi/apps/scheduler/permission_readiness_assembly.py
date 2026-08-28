"""权限就绪确认 + 变化通知这一面的装配：:func:`_build_readiness_follow_up`。

从 :mod:`lingxi.apps.scheduler.assembly`（Issue #350 拆分）搬出，单独成文件而不是并入
:mod:`lingxi.apps.scheduler.permission_publish`——那个文件承载 ``PermissionPublishDuty``
自身，而 ``tests/test_permission_publish_duty.py::NonBlockingTest`` 对它做**全文件级**
的否定断言（AST 剥离文档字符串后扫描源码，禁止出现 ``sleep``/``wait(`` 等等待类词汇，
证明发布职责的一轮 tick 绝不可能被阻塞）。本函数装配的
:class:`~lingxi.apps.scheduler.permission_publish.ReadinessFollowUp` 里
``PermissionNoticeDispatcher`` 的退避是**合法、必要**的 ``sleep=stop.wait``
（送达重试的可中止等待，与那条否定断言要挡的"一轮 tick 被阻塞"是两件事），
但会被那个全文件扫描连坐命中——因此单独放一个文件，不与 ``PermissionPublishDuty``
共享同一份源码。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from lingxi.core.permission.mcp_readiness import ReadinessSchedule

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.permission_publish import ReadinessFollowUp

logger = logging.getLogger(__name__)


def _build_readiness_follow_up(
    config: SchedulerConfig,
    *,
    audit: AuditSink,
    stop: threading.Event,
) -> ReadinessFollowUp | None:
    """装配「就绪确认 + 变化通知」这一面；前置不齐就**不装配**并留下**恰一条**审计。

    **两个前置各自只关掉自己那一块**（二级审查 N6）：

    1. **MCP 令牌主密钥**（``LINGXI_MCP_TOKEN_ENCRYPT_KEY``）——**整面的前置**。它不只是
       解密令牌用：就绪判定记录（``mcp_sync_check``）的读写口在同一个存储类上，而"这条
       变化通知过了"这个**唯一水位**就落在那张表里。没有它，连撤权通知都没有"只发一次"
       的载体，只能整面不装配。
    2. **问数 MCP 端点**（``LINGXI_QUERY_MCP_ENDPOINT``）——**只关掉探针**。撤权通知不
       依赖探针（权限文本为空的那一路本来就不发探针），因此端点没配时这一面照常装配，
       只是把 ``probe=None`` 交给 :class:`ReadinessTicker`：需要探针的那一路本轮不推进、
       不落任何记录，端点配好后从库里的进度原样继续。装一个指向空地址的假探针则相反，
       会让每条确认以技术失败耗满预算再转运维——把"还没接线"伪装成"接线了但一直失败"。

    **探针超时与就绪节奏用同一个数**：``ReadinessSchedule(probe_timeout_seconds=…)`` 与
    ``QueryMcpProbe(timeout_seconds=…)`` 都取 ``config.query_mcp_timeout_seconds``。
    两边不一致时，就绪那一侧算出来的"结论最晚什么时候落地"就是假的，因此这里在装配后
    立刻断言相等——装配层的错配不该等到生产才暴露。

    **探针的 ``metrics_reader`` 显式注入为已验证的
    :func:`~lingxi.adapters.query_mcp_probe.content_text_metrics_reader`**（Issue #253）：
    2026-08-19 对真实问数 MCP 的第一次实测（``docs/参考证据/问数MCP-list_metrics真实响应形状.md``）
    发现返回里没有 ``structuredContent``，指标挂在 ``result.content[0].text`` 的一段
    JSON 字符串里；``QueryMcpProbe`` 默认的 :func:`~lingxi.adapters.query_mcp_probe.default_metrics_reader`
    只认前者，因此不注入的话就绪探针在真实 MCP 上会**永远**技术失败。
    ``default_metrics_reader`` 本身不改——保留它作为"真实形状还没实测时"的收窄兜底，
    这里只是**装配层按证据放宽**，而不是放宽默认值本身。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（`V-花名册-29` 的同一条纪律；它还是一把主密钥）。
        audit.record(
            "permission_readiness.not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，MCP 就绪确认与权限变化通知不装配；权限发布照常运行", MASTER_KEY_ENV
        )
        return None

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore, token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
    from lingxi.core.permission.mcp_readiness import ReadinessTicker
    from lingxi.core.permission.notification import PermissionNoticeDispatcher

    tokens = PostgresMcpTokenStore(
        config.postgres_dsn,
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
        timeouts=config.postgres_timeouts,
    )
    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    probe = None
    if config.query_mcp_endpoint:
        probe = QueryMcpProbe(
            endpoint=config.query_mcp_endpoint,
            token_provider=token_cipher_provider(tokens),
            timeout_seconds=config.query_mcp_timeout_seconds,
            # 已验证的 reader（Issue #253 / L4a）：真实 MCP 的 list_metrics 返回没有
            # structuredContent，见本函数文档与 docs/参考证据/问数MCP-list_metrics真实响应形状.md。
            metrics_reader=content_text_metrics_reader,
        )
        if probe.timeout_seconds != schedule.probe_timeout_seconds:  # pragma: no cover - 装配自证
            raise RuntimeError(
                "探针传输超时必须与就绪节奏的单次超时一致，否则收口上界是假的"
            )
    else:
        # **恰一条**审计：只关掉探针，撤权通知照常。
        audit.record(
            "permission_readiness.probe_not_wired",
            reason="missing_environment_variable",
            variable="LINGXI_QUERY_MCP_ENDPOINT",
        )
        logger.warning(
            "未配置 LINGXI_QUERY_MCP_ENDPOINT，MCP 就绪探针不装配；"
            "撤权通知与权限发布照常运行，已发布的授权待端点配好后继续确认"
        )
    return ReadinessFollowUp(
        ticker=ReadinessTicker(
            probe=probe,
            store=tokens,
            audit=audit,
            clock=lambda: datetime.now(timezone.utc),
            schedule=schedule,
        ),
        checks=tokens,
        notices=PermissionNoticeDispatcher(
            sender=FeishuUserMessages(
                base_url=config.feishu_base_url,
                app_id=config.feishu_app_id,
                app_secret=config.feishu_app_secret,
            ),
            audit=audit,
            # 退避用 `stop.wait` 而不是 `time.sleep`：SIGTERM 能立刻打断它
            # （同 `CredentialRotationLoop._save_with_retry`）。
            sleep=stop.wait,
        ),
    )
