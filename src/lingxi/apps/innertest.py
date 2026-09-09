"""两进程共享内测配置装配，不从客户端选择环境或身份。"""

from types import SimpleNamespace


def build_roster_gate(config):
    """兼容安装可先部署，明确 scope 后每次读同一数据库权威。"""
    from lingxi.adapters.postgres_innertest_roster import PostgresInnertestRoster
    from lingxi.core.identity.innertest_roster_gate import build_innertest_roster_gate

    if not config.innertest_scope:
        return build_innertest_roster_gate(config.innertest_roster_open_ids)
    return PostgresInnertestRoster(
        str(config.postgres_dsn),
        scope=config.innertest_scope,
        legacy=config.innertest_roster_open_ids,
    )


def build_innertest_service(config, audit, *, binding=None):
    """固定服务装配，gateway 仅需绑定编号，不持 peer 认证入口。"""
    from lingxi.adapters.postgres_innertest import PostgresInnertestService
    from lingxi.adapters.postgres_innertest_locator import locate_transaction_email

    if not config.innertest_scope or not config.innertest_binding_id:
        raise ValueError("内测管理入口缺少环境或绑定配置")
    if binding is None:
        binding = SimpleNamespace(binding_id=config.innertest_binding_id, peer_uid=None)
    if binding.binding_id != config.innertest_binding_id:
        raise ValueError("内测管理绑定配置不一致")
    return PostgresInnertestService(
        str(config.postgres_dsn),
        scope=config.innertest_scope,
        binding=binding,
        locator=locate_transaction_email,
        audit=audit,
    )


def wrap_pending_actions(config, store, audit):
    """扩员确认沿用 gateway 的同一个已验证卡片回调入口。"""
    if not config.innertest_scope:
        return store
    from lingxi.adapters.postgres_innertest_confirmation import InnertestPendingActions

    return InnertestPendingActions(store, build_innertest_service(config, audit))
