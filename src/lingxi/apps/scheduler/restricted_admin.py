"""受限通道服务装配：扩员三工具沿用既有服务，只读与准备工具组合既有查询口与命令路由。

只做构造与注入：读取口、阶段表与映射路径都来自固定配置，不从客户端接收任何参数。
"""


def build_restricted_service(config, audit, *, binding, slots):
    """扩员服务仍是身份权威；只读查询与准备工具共享 listener 的数据库预算槽。"""
    from lingxi.adapters.admin_registry import PostgresAdminQueries
    from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
    from lingxi.adapters.restricted_admin_prepare import build_restricted_prepare
    from lingxi.adapters.restricted_admin_queries import (
        RestrictedAdminQueries,
        RestrictedChannelService,
    )
    from lingxi.apps.innertest import build_innertest_service

    dsn = str(config.postgres_dsn)
    admin_queries = PostgresAdminQueries(dsn, timeouts=config.postgres_timeouts)
    followups = PostgresFollowupStore(dsn, timeouts=config.postgres_timeouts, db_slots=slots)
    readonly = RestrictedAdminQueries(
        dsn,
        queries=admin_queries,
        followups=followups,
        metric_map_path=config.metric_map_path,
        timeouts=config.postgres_timeouts,
    )
    return RestrictedChannelService(
        innertest=build_innertest_service(config, audit, binding=binding),
        queries=readonly,
        audit=audit,
        prepare=build_restricted_prepare(
            dsn,
            audit=audit,
            metric_map_path=config.metric_map_path,
            timeouts=config.postgres_timeouts,
            queries=admin_queries,
            readonly=readonly,
            store=followups,
        ),
    )
