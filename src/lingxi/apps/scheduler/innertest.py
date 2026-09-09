"""scheduler 内受限管理入口与唯一后台阶段装配。"""

from lingxi.core.admin.followup_consumer import FollowupConsumer
from lingxi.core.admin.followup_renewal import FollowupLeaseKeeper
from lingxi.core.ids import new_id


def wire_innertest(config, *, loop, duties, audit):
    """显式配置才启动入口，生命周期与数据库预算复用同一进程 Owner。"""
    if not config.innertest_scope:
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
        probe=_build_probe(config),
    )
    consumer = FollowupConsumer(
        store=store,
        consumer_kind="scheduler",
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
    )
    listener.start()
    for component in (consumer, listener, FollowupLeaseKeeper([consumer], audit=audit)):
        loop.register_background(component)
    consumer.start()


def _build_probe(config):
    """逐用户加密令牌读取，不新建服务账号或 Agent 会话。"""
    from lingxi.adapters.query_mcp_probe import QueryMcpProbe, content_text_metrics_reader
    from lingxi.apps.scheduler.onboarding import _build_user_mcp_tokens

    return QueryMcpProbe(
        endpoint=config.mcp_probe_endpoint,
        token_provider=_build_user_mcp_tokens(config).query_token_provider(),
        metrics_reader=content_text_metrics_reader,
        timeout_seconds=5,
    )
