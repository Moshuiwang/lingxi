"""首次开通链的两道**失败关闭闸**。

它们不产出内容，只决定「这个人此刻还该不该继续被开通」，命中就返回一个
:class:`~lingxi.core.identity.onboarding_terminal._Terminal`。两道闸都排在 ``AutoOnboardingRunner._run`` 的**公共段**，且都早于令牌签发/采纳与
权限发布意图：邮箱闸在**建档之前**（它只需要 ``feishu_open_id``），零银河闸在建档
之后（它需要 ``app_user.id`` 去查本地授权）。放在公共段而不是某条入口分支里，是
因为 ``_run`` 会被两条入口复用：收到首聊消息的正式开通，以及系统触发的「预开通」
（无入站消息）。挂在分支上的闸对第二条路径等于不存在。

它们形状相同（``(...) -> _Terminal | None``、只读、命中即失败关闭、各自记一条审计），
但去向不同：:func:`reject_zero_galaxy_without_local_grant` 命中表示这个人确实没有
可用权限，出口是冻结的「无可用银河权限」（确定性业务失败）；
:func:`reject_email_bound_to_another_person` 命中表示我们这边的数据不对、不是他没
权限，出口是冻结的 ``LX-ONBOARD-001``（本侧故障）＋管理员告警。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from lingxi.core.identity.onboarding_ports import EmailBindingSource, _AuditSink
from lingxi.core.identity.onboarding_terminal import _internal, _not_authorized, _Terminal
from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.local_override import ResolvedLocalOverrides
from lingxi.core.permission.merge_sources import merge_permission_sources

logger = logging.getLogger(__name__)

#: 「同一规范化邮箱已经绑给另一个人」的失败原因码。走 ``LX-ONBOARD-001``
#: 家族（本侧故障），**不是**「无权限」——见 :func:`reject_email_bound_to_another_person`。
EMAIL_ALREADY_BOUND = "email_already_bound"


def reject_email_bound_to_another_person(
    open_id: str,
    email: str | None,
    *,
    bindings: EmailBindingSource,
    audit: _AuditSink,
    trace_id: str,
) -> _Terminal | None:
    """**同一个邮箱已经绑给另一个人时失败关闭**。

    正式表行键是规范化邮箱，开通链在签发令牌前按邮箱查存量行、采纳可解密的
    密文；两个不同员工共用同一邮箱时，后开通者会采纳前者的令牌、并把发布意图
    覆写成自己的范围——两人以同一身份查数。判据用 ``feishu_open_id``：这道闸排在
    建档之前，``app_user.id`` 还不存在。走 ``LX-ONBOARD-001``（本侧故障）而不是
    「无权限」：这是我们这边的数据不对，走「无权限」会把用户引去银河申请一个他
    已经有的权限。与数据库唯一索引是纵深防御：索引挡并发写入，这道闸提前给出
    有名字的原因码。规范化后为空邮箱直接放行——没有邮箱就没有 ``record_key``。
    """
    normalized = normalize_email(email)
    if not normalized:
        return None
    conflicting = tuple(
        sorted(
            {
                binding.user_id
                for binding in bindings.bindings_for_email(normalized)
                if (binding.feishu_open_id or "") != open_id
            }
        )
    )
    if not conflicting:
        return None
    # 审计只带冲突方的 ``user_id``：**不带邮箱、也不带 open_id**（两者都是身份资料
    # 值，与本文件其余审计同一条纪律）。需要还原是哪个邮箱时按这些 ``user_id``
    # 回查 ``app_user``；需要还原"谁触发的"时按 ``trace_id`` 回查审计链。
    audit.record(
        "onboarding.email_already_bound",
        conflicting_users=conflicting,
        trace_id=trace_id,
    )
    logger.error(
        "同一邮箱已绑定其它用户，开通失败关闭 trace=%s conflicting=%d", trace_id, len(conflicting)
    )
    return _internal(EMAIL_ALREADY_BOUND)


def reject_zero_galaxy_without_local_grant(
    user_id: str,
    aggregate: Any,
    *,
    resolve_local_overrides: Callable[[str], ResolvedLocalOverrides | None],
    audit: _AuditSink,
    trace_id: str,
) -> _Terminal | None:
    """零银河权限用户提前查一次**本地授权**，避免为注定被拒绝的人签发令牌、建环境。

    合并结果非空（管理员兜底赋权）→ 返回 ``None`` 放行，``_publish`` 会再做一次
    同样的合并并真正结算发布行；合并结果仍为空 → 在这里（早于
    ``_issue_token``/``_create_environment``）就返回"无可用银河权限"终态。不能放在
    ``_publish``：那里需要已签发的令牌，结构上排在令牌签发之后。**不翻译**：
    ``aggregate.granted`` 为假时 ``companies``/``functions`` 恒为空，银河这一侧
    贡献直接是 ``{}``，也因此不受 ``publish_allowed`` 闸门约束。
    """
    local = resolve_local_overrides(user_id)
    # ``full_access_wildcard`` 是必填关键字参数——这条分支 ``galaxy`` 恒为空
    # 字典，取值对结果没有作用面，仍必须显式传参（无默认值的结构性要求）。
    merged = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)
    for reason in merged.skipped_reasons:  # 通配角 v1 结构上不会出现（galaxy 恒为空）
        audit.record("onboarding.local_override_skipped", user=user_id, reason=reason)
    if merged.permissions:
        return None
    audit.record("onboarding.no_local_grant_after_zero_galaxy", user=user_id, trace_id=trace_id)
    return _not_authorized(aggregate.reason)
