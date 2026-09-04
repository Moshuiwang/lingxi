"""权限就绪确认 + 变化通知这一面的装配：:func:`_build_readiness_follow_up`。

单独成文件而不是并入 :mod:`lingxi.apps.scheduler.permission_publish`——那个文件
承载 ``PermissionPublishDuty`` 自身，而 ``tests/test_permission_publish_duty.py::
NonBlockingTest`` 对它做全文件级的否定断言（AST 剥离文档字符串后扫描源码，禁止
出现 ``sleep``/``wait(`` 等等待类词汇，证明发布职责的一轮 tick 绝不可能被阻塞）。
本函数装配的 :class:`~lingxi.apps.scheduler.permission_publish.ReadinessFollowUp`
里 ``PermissionNoticeDispatcher`` 的退避是合法、必要的 ``sleep=stop.wait``（送达
重试的可中止等待，与那条否定断言要挡的"一轮 tick 被阻塞"是两件事），但会被那个
全文件扫描连坐命中——因此单独放一个文件，不与 ``PermissionPublishDuty`` 共享
同一份源码。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from lingxi.apps.scheduler.audit import AuditSink
from lingxi.apps.scheduler.config import SchedulerConfig
from lingxi.apps.scheduler.permission_publish import ReadinessFollowUp
from lingxi.core.permission.mcp_readiness import ReadinessSchedule

logger = logging.getLogger(__name__)


def _build_readiness_follow_up(
    config: SchedulerConfig,
    *,
    audit: AuditSink,
    stop: threading.Event,
) -> ReadinessFollowUp | None:
    """装配「就绪确认 + 变化通知」这一面；前置不齐就**不装配**并留下**恰一条**审计。

    两个前置各自只关掉自己那一块：**MCP 令牌主密钥**是整面的前置——就绪判定
    记录与"这条变化通知过了"的唯一水位都落在同一张表；**问数 MCP 端点**只关掉
    探针，撤权通知本来就不依赖探针，端点没配时这一面照常装配，只是把
    ``probe=None`` 交给 :class:`ReadinessTicker`。**不装一个指向空地址的假
    探针**：那会让每条确认以技术失败耗满预算再转运维，把"还没接线"伪装成
    "接线了但一直失败"。
    """

    if not config.mcp_token_encrypt_key:
        from lingxi.adapters.mcp_token_cipher import MASTER_KEY_ENV

        # 只报变量名，不回显任何值（它还是一把主密钥）。
        audit.record(
            "permission_readiness.not_wired",
            reason="missing_environment_variable",
            variable=MASTER_KEY_ENV,
        )
        logger.warning(
            "未配置 %s，MCP 就绪确认与权限变化通知不装配；权限发布照常运行", MASTER_KEY_ENV
        )
        return None

    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import PostgresMcpTokenStore
    from lingxi.core.permission.mcp_readiness import ReadinessTicker

    tokens = PostgresMcpTokenStore(
        config.postgres_dsn,
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
        timeouts=config.postgres_timeouts,
    )
    schedule, probe = _build_readiness_probe(config, audit=audit, tokens=tokens)

    return ReadinessFollowUp(
        ticker=ReadinessTicker(
            probe=probe,
            store=tokens,
            audit=audit,
            clock=lambda: datetime.now(UTC),
            schedule=schedule,
        ),
        checks=tokens,
        notices=_build_notice_dispatcher(config, audit=audit, stop=stop),
    )


def _build_readiness_probe(
    config: SchedulerConfig, *, audit: AuditSink, tokens: object
) -> tuple[ReadinessSchedule, object | None]:
    """装配就绪探针；缺问数 MCP 端点时只留 ``probe=None`` 并记**恰一条**审计。

    ``ReadinessSchedule`` 与 ``QueryMcpProbe`` 的超时必须取同一个数，这里装配后
    立刻断言相等——两边不一致时"结论最晚什么时候落地"就是假的，错配不该等到
    生产才暴露。``metrics_reader`` 显式注入
    :func:`~lingxi.adapters.query_mcp_probe.content_text_metrics_reader`：真实
    问数 MCP 的返回里没有 ``structuredContent``，指标挂在
    ``result.content[0].text`` 的一段 JSON 字符串里，默认的 reader 认不出这个
    形状，不注入的话探针在真实 MCP 上会永远技术失败。
    """

    schedule = ReadinessSchedule(probe_timeout_seconds=config.query_mcp_timeout_seconds)
    if not config.query_mcp_endpoint:
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
        return schedule, None

    from lingxi.adapters.postgres_mcp_token import token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader

    probe = QueryMcpProbe(
        endpoint=config.query_mcp_endpoint,
        token_provider=token_cipher_provider(tokens),
        timeout_seconds=config.query_mcp_timeout_seconds,
        # 已验证的 reader：真实 MCP 的 list_metrics 返回没有 structuredContent，
        # 见本函数文档与 docs/参考证据/问数MCP-list_metrics真实响应形状.md。
        metrics_reader=content_text_metrics_reader,
    )
    if probe.timeout_seconds != schedule.probe_timeout_seconds:  # pragma: no cover - 装配自证
        raise RuntimeError("探针传输超时必须与就绪节奏的单次超时一致，否则收口上界是假的")
    return schedule, probe


def _build_notice_dispatcher(config: SchedulerConfig, *, audit: AuditSink, stop: threading.Event):
    """装配权限变化通知的发送口。

    公司位展示改经 ``galaxy_country.name_cn`` 展示中文名（而不是裸
    ``boss_company_id`` 编号）：解析口内部按当前有效批次现读，没有批次或查无
    对应编号时按既有行为原样展示编号，不阻塞通知发送。退避用 ``stop.wait`` 而
    不是 ``time.sleep``：SIGTERM 能立刻打断它。
    """

    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.adapters.postgres_galaxy_snapshot import PostgresCompanyNames
    from lingxi.core.permission.notification import PermissionNoticeDispatcher

    return PermissionNoticeDispatcher(
        sender=FeishuUserMessages(
            base_url=config.feishu_base_url,
            app_id=config.feishu_app_id,
            app_secret=config.feishu_app_secret,
        ),
        audit=audit,
        company_names=PostgresCompanyNames(config.postgres_dsn, timeouts=config.postgres_timeouts),
        sleep=stop.wait,
    )
