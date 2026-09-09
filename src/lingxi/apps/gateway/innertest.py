"""确认卡只由 gateway 的既有飞书凭据发送。"""

from lingxi.core.admin.followup_consumer import FollowupResult


def confirmation_handler(config, *, store, audit, callback):
    """没有动态配置时不运行新卡片业务，旧阶段保留明确待处理结果。"""
    if not config.innertest_scope:
        return lambda _item: FollowupResult("retry_wait", "handler_unavailable")
    from lingxi.adapters.feishu_admin_card import _create_card
    from lingxi.adapters.feishu_user_card import FeishuUserCards
    from lingxi.adapters.innertest_confirmation_card import InnertestConfirmationCard
    from lingxi.apps.innertest import build_innertest_service

    sender = FeishuUserCards(
        base_url=config.feishu_base_url, app_id=config.app_id, app_secret=str(config.app_secret)
    )
    return InnertestConfirmationCard(
        service=build_innertest_service(config, audit),
        store=store,
        create_card=lambda payload: _create_card(callback._confirm_cards._client, payload),
        send_card=sender.send_card,
    )
