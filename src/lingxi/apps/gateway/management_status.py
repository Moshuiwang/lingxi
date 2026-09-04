"""管理卡上「这次操作现在怎么样了」那一行**给管理员看的话**。

从 ``apps/gateway/__init__.py`` 拆出来的一层纯判定：入参只有「数据库里的机器
状态」和「本次刚读回的账号状态」，出参只有一句中文，不碰 CardKit、数据库或
任何凭据——判据必须能不组装 gateway 就被用例直接钉死。

``management_card_context.dispatch_status`` 只有四个机器态，不能原样回显给
管理员。``incomplete`` 不能对**已停用**用户一律翻译成「将在次日批处理修正」
——发权每日批遍历要求账号 ``enabled``，停用用户永远进不了遍历集合，等不到
「修正」，翻译成这句话就是假承诺。

判据落在**本次刚读回的** ``AdminUserStatusView.account_state`` 上，不落在
不落库的人类文案上：视觉恢复 scanner 按持久水位重画时只能拿到那四个机器态，
账号恢复 ``enabled`` 之后，同一张卡再刷新就自动回到通用文案，不需要缓存失效。
"""

from __future__ import annotations

from typing import Any

from lingxi.config.content import default_content_catalog
from lingxi.core.permission.targeted_recompute import SKIP_ACCOUNT_NOT_ENABLED

#: 管理卡对**已停用**用户做本地权限动作时的如实回执。走版本化内容目录，改文案必须
#: 递增 ``config/content.toml`` 的 ``[meta] version`` 并刷新 ``content.lock.toml``。
MANAGEMENT_ACCOUNT_NOT_ENABLED_KEY = "permission.management_account_not_enabled"

#: 账号状态正常时 ``incomplete`` 的通用兜底——这句对他们成立：日批确实会补齐。
GENERIC_INCOMPLETE_TEXT = "权限下发未完成，将在次日批处理修正"

#: 「已记录、正在下发」这句**瞬时**状态行的唯一字面量——曾经在
#: ``apps/gateway/__init__.py`` 另抄了两份导致改一处漏两处，现在三处都引用这一个。
PUBLISHING_STATUS_TEXT = "操作已记录，权限正在下发"

#: 与 ``core/permission/publish.ACCOUNT_STATE_ENABLED`` 同一判据的本地字面量；这里只做
#: 展示分支，不做任何授权判定，因此不引入那条模块的 import 闭包。
_ACCOUNT_STATE_ENABLED = "enabled"

#: ``dispatch_status`` 里属于机器态、绝不能原样给管理员看的取值。
_MACHINE_DISPATCH_STATUS = frozenset({"incomplete", "publishing"})


def account_not_enabled_text() -> str:
    """那句真话。内容目录本身失败关闭，这里不再兜第二层默认值。"""
    return default_content_catalog().text(MANAGEMENT_ACCOUNT_NOT_ENABLED_KEY).text


def is_account_not_enabled(status: Any) -> bool:
    """当前读到的账号状态是否**不是** ``enabled``。

    ``status`` 是 ``core/admin/views.AdminUserStatusView``；旧测试替身可能没有
    ``account_state`` 字段，读不到时按"没有额外信息"处理（返回 ``False``），保持与本
    判据加入之前逐字节一致的渲染。
    """
    account_state = getattr(status, "account_state", None)
    return isinstance(account_state, str) and account_state != _ACCOUNT_STATE_ENABLED


def skipped_recompute_status_message(outcome: Any) -> str | None:
    """一次判 ``SKIPPED`` 的定向重算该给管理员看什么。

    返回 ``None`` 表示"没有专门的话要说"——调用方沿用原来那句失败文案，行为逐字节
    不变。目前只有 ``account_not_enabled`` 有专属真话：其余跳过原因（快照缺失、匹配
    失败……）确实可能被次日批处理纠正，原文案对它们成立。
    """
    if getattr(outcome, "reason", None) != SKIP_ACCOUNT_NOT_ENABLED:
        return None
    return account_not_enabled_text()


def incomplete_status_text(*, status: Any, dispatch_status: str | None) -> str:
    """``incomplete`` 这一态最终显示成哪句话。

    ``dispatch_status`` 已经是人类文案时原样透传（即时路径已经算好了要说什么）；只剩
    机器态可用时（视觉恢复 scanner 重画）才回落——**并且先看账号状态**，停用用户拿到
    的是真话而不是那句永远不会兑现的承诺。
    """
    if dispatch_status and dispatch_status not in _MACHINE_DISPATCH_STATUS:
        return dispatch_status
    if is_account_not_enabled(status):
        return account_not_enabled_text()
    return GENERIC_INCOMPLETE_TEXT


def _claims_publishing(
    *, state: str, dispatch_status: str | None, status_message: str | None
) -> bool:
    """这一次要显示的话，是不是「已记录、正在下发」那句承诺？

    三个入参任意一个指向"正在下发"都算：``status_message`` 是即时路径已经算好的人类
    文案（它就等于 :data:`PUBLISHING_STATUS_TEXT`），``state``/``dispatch_status`` 是
    视觉恢复 scanner 重画时唯一可用的机器态。**只认这一句**——「已生效」「已取消」
    「未完成」各有自己的判据，不在这里顺手一起改写。
    """
    if status_message == PUBLISHING_STATUS_TEXT:
        return True
    if status_message:
        return False
    return state in {"submitted", "dispatching"} or dispatch_status in (
        "publishing",
        PUBLISHING_STATUS_TEXT,
    )


def rendered_dispatch_status(
    *, status: Any, state: str, dispatch_status: str | None, status_message: str | None
) -> str | None:
    """管理卡「当前状态」那一行最终显示的话。

    数据库里的 ``state``/``dispatch_status`` 都是机器状态，不能原样回显给管理员；所有
    管理卡可见结果都在这里映射成产品术语，调用方（``_GatewayManagementCardRefresher``）
    不再自己拼任何文案。``status_message`` 是即时路径已经算好的那句话，优先级最高。
    """
    if is_account_not_enabled(status) and _claims_publishing(
        state=state, dispatch_status=dispatch_status, status_message=status_message
    ):
        # 终态卡说的是真话，但**瞬时**这一行如果还在说「权限正在下发」，对一个
        # 已停用的目标就是不成立的承诺（发布层在 app_user 行锁里已挡住非
        # enabled 账号的非空授权）。这里让瞬时与终态说同一句话，避免管理员先
        # 看到一句不成立的承诺、隔一会儿才被终态纠正。
        return account_not_enabled_text()
    if status_message:
        return status_message
    if state in {"submitted", "dispatching"} or dispatch_status == "publishing":
        return PUBLISHING_STATUS_TEXT
    if state == "effective" or dispatch_status == "effective":
        return "已生效"
    if state == "incomplete" or dispatch_status == "incomplete":
        return incomplete_status_text(status=status, dispatch_status=dispatch_status)
    if state == "closed":
        return "已取消"
    return dispatch_status


__all__ = [
    "rendered_dispatch_status",
    "GENERIC_INCOMPLETE_TEXT",
    "PUBLISHING_STATUS_TEXT",
    "MANAGEMENT_ACCOUNT_NOT_ENABLED_KEY",
    "account_not_enabled_text",
    "incomplete_status_text",
    "is_account_not_enabled",
    "skipped_recompute_status_message",
]
