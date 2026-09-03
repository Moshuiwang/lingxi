"""首次开通链的两道**失败关闭闸**：它们不产出内容，只决定「这个人此刻还该不该
继续被开通」，命中就返回一个 :class:`~lingxi.core.identity.onboarding_terminal._Terminal`。

两道闸都排在 ``AutoOnboardingRunner._run`` 的**公共段**，且都早于令牌签发/采纳与
权限发布意图：邮箱闸在**建档之前**（它只需要 ``feishu_open_id``），零银河闸在建档
之后（它需要 ``app_user.id`` 去查本地授权）。放在公共段而不是某条入口分支里，是
因为 ``_run`` 会被两条入口复用：收到首聊消息的正式开通，以及 Issue #541 的「预开通」
（系统触发、无入站消息）。挂在分支上的闸对第二条路径等于不存在。

## 为什么这两道闸住在同一个模块

它们形状相同（``(...) -> _Terminal | None``、只读、命中即失败关闭、各自记一条审计），
但去向不同：

| 闸 | 命中含义 | 用户出口 |
|---|---|---|
| :func:`reject_zero_galaxy_without_local_grant` | 这个人确实没有可用权限 | 冻结的「无可用银河权限」（确定性业务失败） |
| :func:`reject_email_bound_to_another_person` | 我们这边的数据不对，不是他没权限 | 冻结的 ``LX-ONBOARD-001``（本侧故障）＋管理员告警 |

前者是从 ``onboarding_runner.py`` **纯移动**过来的（rc25 S-2a；同 Trace #358 S-H-1
的先例：只搬定义，不改判定），搬家的直接原因是 ``onboarding_runner.py`` 当时是
1499 行、体量棘轮阈值是 1500 行（``scripts/ci/check_size_ratchet.py``），新增的第二道闸
放不进去；把两道同形状的闸放在一起，比把其中一道压成没有注释的三行更可读。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from lingxi.core.identity.onboarding_ports import EmailBindingSource, _AuditSink
from lingxi.core.identity.onboarding_terminal import _Terminal, _internal, _not_authorized
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
    """**同一个邮箱已经绑给另一个人时失败关闭**（rc25 S-2a，对抗审查 X-1）。

    ## 挡的是什么

    正式表 ``user_company_permissions`` 的行键 ``record_key`` 是**规范化邮箱**
    （``core/permission/publish_row.py``），而开通链在签发令牌之前会**按邮箱**查
    正式表存量行，单行且可解密就把那份密文**采纳**成本用户的问数令牌
    （``AutoOnboardingRunner._issue_token``）。``app_user.email`` 在 rc25 S-2a 之前
    没有任何唯一性约束，于是当花名册里两名**不同员工**填了同一个邮箱时：

    1. A 先开通 → 签发令牌 T、写出正式表行 R（``record_key`` = 该邮箱）；
    2. B 后开通 → 按同一邮箱查到 R、把 **A 的**令牌密文采纳进 B 的 ``.mcp.json``；
    3. B 的发布意图以同一个 ``record_key`` 把 R 的 ``permissions`` 覆写成 B 的范围。

    结果是两个人以**同一个身份**查数，可见范围由最后一次发布者决定；两人的权限
    变化通知与就绪探针也互相干扰。触发条件是上游数据质量（共享邮箱 / 邮箱填错），
    不是用户可控输入。

    ## 判据为什么是 ``feishu_open_id`` 而不是 ``user_id``

    因为这道闸排在**建档之前**：那时本人的 ``app_user.id`` 还不存在，能拿来回答
    "这一行是不是我自己"的只有 ``feishu_open_id``——它是基线里 ``app_user`` 唯一
    那个 UNIQUE 列，也是建档的幂等键。命中的行里只要有一行的 ``feishu_open_id``
    不等于当前这个人（含空值），就是"这个邮箱已经绑给别人"。

    **为什么必须在建档之前**：迁移 ``0085`` 之后，第二个人的建档 upsert 会被那条
    部分唯一索引直接拒绝，链在 ``_provision`` 就以一条泛化的 ``storage_integrity``
    结束——用户结论一样是 ``LX-ONBOARD-001``，但排障的人只看到"库把工号吞了"，
    看不到真正的原因；而且那一刻已经为一个注定失败的人尝试过写入。放在建档之前，
    两件事同时成立：原因码是有名字的 ``email_already_bound``，且 ``app_user``
    零新增行。

    ## 为什么走 ``LX-ONBOARD-001`` 而不是「无权限」

    这是**我们这边的数据不对**，不是"这个人没有银河权限"。走「无权限」出口会把
    用户引去银河申请一个他其实已经有的权限（接口设计 §8.1 的同一条纪律），而真正
    需要做事的人——管理员——什么也不会知道。走本侧故障家族则同时得到两件事：用户
    看到的是冻结的 ``LX-ONBOARD-001``（"已转交管理员处理"），管理群拿到一条带
    ``reason=email_already_bound`` 的告警（``AutoOnboardingRunner._notify_admin_of_
    failure`` 在 ``INTERNAL_ERROR`` 终态上的既有触发点，本函数不另建通道）。

    ## 与数据库唯一索引的分工

    迁移 ``0085`` 给 ``app_user`` 建了 ``lower(btrim(email))`` 上的**部分唯一索引**，
    它才是"两个人不可能同时绑同一个邮箱"的结构性保证——包括本函数读完之后、建档写入
    之前的那一小段并发窗口。本函数是它的**纵深**：在写入之前就读到冲突，给出一条
    有名字的原因码和一条可检索的审计。两者缺一都会退化——只有索引时诊断信息丢失、
    且每次冲突都要先尝试一次注定失败的写入；只有本函数时并发两条链仍可能各自读到
    "无冲突"。

    ## 空邮箱

    规范化后为空（``None`` / 空串 / 纯空白）时**直接放行**：没有邮箱就没有
    ``record_key``，采纳与发布两条链路都不成立（``build_publish_row`` 对空邮箱直接
    ``ValueError``），也与迁移 ``0085`` 那条索引的 ``WHERE btrim(email) <> ''`` 同口径
    ——不进索引的值同样不该被本函数当成冲突。
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
    """零银河权限用户提前查一次**本地授权**：合并结果非空（管理员兜底赋权）
    → 返回 ``None``，放行继续正常链路（令牌签发、用户环境、``_publish`` 会再
    做一次同样的合并并真正结算发布行——见 ``_publish`` 里的对应分支，这里
    重复一次查询换来的是"不为一个注定被拒绝的人签发令牌、建环境"，取舍见
    下）；合并结果仍为空 → 返回"无可用银河权限"的确定性业务失败终态，
    **在这里**拒绝——早于 ``_issue_token``/``_create_environment``，不为一个
    注定被拒绝的人签发问数 MCP 令牌、不创建带凭据的用户环境。

    **为什么在这里而不是 ``_publish``**：``_publish`` 需要已签发的令牌
    （``issued.token_cipher`` 要写进发布行），因此结构上只能排在令牌签发**之后**；
    零银河用户里绝大多数既无银河也无本地授权（``aggregate.granted`` 为假的
    用户里，只有极少数会恰好也被管理员发过本地授权），把最终判定放在这里能
    让大多数人在签发令牌/建环境之前就了结，换来的代价是"确实有本地授权兜底
    的那一小撮人"会被查两次本地覆盖（一次这里、一次 ``_publish``）——两次都是
    只读查询，且第二次结果理论上应当与这次一致（除非管理员在这两步之间的
    极短窗口收回了授权，那种情况下 ``_publish`` 会用它自己重新算出的结果，
    不会用这次的陈旧结论）。

    **不翻译**：``aggregate.granted`` 为假时 ``aggregate.companies``/``functions``
    恒为空（``PermissionAggregate.__post_init__`` 的不变式），``translate_
    company_functions`` 对空输入直接拒绝（那是"参数缺失"，不是"没有内容"，
    见其自身校验），因此银河这一侧对合并的贡献直接是 ``{}``，不经过翻译层。
    这也是这条分支不受 ``publish_allowed`` 闸门约束的理由——那道闸只保护"银河
    内容需要翻译才能安全发布"这件事，零银河用户没有银河内容，与改动前"零银河
    用户结构上从不到达 ``publish_allowed`` 检查"逐字节一致（见 ``_match``）。
    """

    local = resolve_local_overrides(user_id)
    # ``full_access_wildcard`` 现在是必填关键字参数（Trace #445 结构性防复发：
    # 默认值曾是一次真实漏接的根因）——这条分支 ``galaxy`` 恒为空字典，取值
    # 对结果没有作用面，仍必须显式传参。
    merged = merge_permission_sources(galaxy={}, local=local, full_access_wildcard=True)
    for reason in merged.skipped_reasons:  # 通配角 v1 结构上不会出现（galaxy 恒为空）
        audit.record("onboarding.local_override_skipped", user=user_id, reason=reason)
    if merged.permissions:
        return None
    audit.record(
        "onboarding.no_local_grant_after_zero_galaxy", user=user_id, trace_id=trace_id
    )
    return _not_authorized(aggregate.reason)
