"""scheduler 内受限管理入口与唯一后台阶段装配。

装配与否在启动日志里各留一条：没有这条日志，运维只能靠猜这个职责是不是起来了，
而「没配所以没起」与「配了但起失败」在外部看起来是同一个样子。
"""

import logging

from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)


def wire_innertest(config, *, loop, duties, audit):
    """显式配置才启动入口，生命周期与数据库预算复用同一进程 Owner。"""
    if not config.innertest_scope:
        logger.info(
            "未配置 LINGXI_INNERTEST_SCOPE：不注册受限管理入口，"
            "内测扩员的管理命令与后台阶段在本进程内均不可用（不阻止启动）"
        )
        return
    from lingxi.adapters.innertest_binding import load_binding
    from lingxi.adapters.innertest_handlers import InnertestFollowupHandlers
    from lingxi.adapters.innertest_runner import SharedOnboardingRunner
    from lingxi.adapters.innertest_socket import InnertestSocketListener
    from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
    from lingxi.apps.innertest import build_innertest_service

    if not config.innertest_binding_path or not config.innertest_socket_path:
        raise ValueError("内测管理入口缺少受保护绑定或 socket 配置")
    binding = load_binding(config.innertest_binding_path)
    service = build_innertest_service(config, audit, binding=binding)
    owners = [d for d in duties if hasattr(d, "onboarding_runner")]
    runners = [d.onboarding_runner for d in owners]
    if len(runners) != 1:
        raise ValueError("内测后台开通入口未就绪")
    store = PostgresFollowupStore(
        str(config.postgres_dsn), timeouts=config.postgres_timeouts, db_slots=loop.followup_db_slots
    )
    handlers = InnertestFollowupHandlers(
        store=store,
        runner=SharedOnboardingRunner(
            runner=runners[0],
            executor=owners[0].onboarding_executor,
            should_stop=lambda: loop.stopping,
        ),
        probe=_build_probe(
            config, db_slots=loop.followup_db_slots, should_stop=lambda: loop.stopping
        ),
    )
    consumer = FollowupConsumer(
        store=store,
        consumer_kind="scheduler",
        stop=loop.stop_event,
        owner=new_id("run"),
        handlers={
            s: handlers.handle for s in ("innertest_preprovision", "innertest_readiness_check")
        },
        audit=audit,
    )
    listener = InnertestSocketListener(
        path=config.innertest_socket_path,
        service=service,
        db_slots=loop.followup_db_slots,
        socket_gid=binding.socket_gid,
        stop=loop.stop_event,
    )
    listener.start()
    for component in (consumer, listener, FollowupLeaseKeeper([consumer], audit=audit)):
        loop.register_background(component)
    consumer.start()
    logger.info(
        "已注册受限管理入口：scope=%s socket=%s binding=%s",
        config.innertest_scope,
        config.innertest_socket_path,
        config.innertest_binding_path,
    )


def _build_probe(config, *, db_slots=None, should_stop=None):
    """沿用逐用户加密令牌读取，传输与执行均受本入口五秒预算约束。"""
    from lingxi.adapters.innertest_probe import InnertestMcpTokens
    from lingxi.adapters.mcp_token_cipher import McpTokenCipher
    from lingxi.adapters.postgres_mcp_token import token_cipher_provider
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
    from lingxi.apps.scheduler.onboarding import HardDeadlineProbe

    tokens = InnertestMcpTokens(
        config.postgres_dsn,
        cipher=McpTokenCipher(config.mcp_token_encrypt_key),
        timeouts=config.postgres_timeouts,
        db_slots=db_slots,
        should_stop=should_stop,
    )
    probe = QueryMcpProbe(
        endpoint=config.query_mcp_endpoint,
        token_provider=token_cipher_provider(tokens),
        metrics_reader=content_text_metrics_reader,
        timeout_seconds=5,
    )
    return HardDeadlineProbe(probe=probe, timeout_seconds=5)
