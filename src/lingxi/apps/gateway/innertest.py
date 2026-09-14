"""确认卡只由 gateway 的既有飞书凭据发送；持久发卡阶段按待确认动作类型分派到发卡器。"""


def confirmation_handler(config, *, store, audit, callback):
    """五种管理写动作的发卡器只依赖 gateway 已装配的端口；扩员发卡器沿用既有服务。"""
    from lingxi.adapters.admin_confirmation_card import (
        AdminConfirmationCard,
        ConfirmationCardDispatch,
    )
    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup
    from lingxi.adapters.feishu_admin_card import _create_card
    from lingxi.adapters.feishu_user_card import FeishuUserCards

    sender = FeishuUserCards(
        base_url=config.feishu_base_url, app_id=config.app_id, app_secret=str(config.app_secret)
    )

    def create_card(payload):
        return _create_card(callback._confirm_cards._client, payload)

    admin = AdminConfirmationCard(
        store=store,
        pending_actions=callback._pending_actions,
        registry=PostgresAdminRegistryLookup(
            str(config.postgres_dsn), timeouts=config.postgres_timeouts
        ),
        display_names=callback._display_names,
        create_card=create_card,
        send_card=sender.send_card,
    )
    innertest = _innertest_card(
        config, store=store, audit=audit, create_card=create_card, send_card=sender.send_card
    )
    return ConfirmationCardDispatch(
        pending_actions=callback._pending_actions, admin=admin, innertest=innertest
    )


def _innertest_card(config, *, store, audit, create_card, send_card):
    """没有动态配置时不运行扩员卡片业务，该阶段保留明确待处理结果。"""
    if not config.innertest_scope:
        return None
    from lingxi.adapters.innertest_confirmation_card import InnertestConfirmationCard
    from lingxi.apps.innertest import build_innertest_service

    return InnertestConfirmationCard(
        service=build_innertest_service(config, audit),
        store=store,
        create_card=create_card,
        send_card=send_card,
    )
