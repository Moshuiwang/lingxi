"""预开通（[Issue #541](https://github.com/Moshuiwang/lingxi/issues/541)，rc25 S-8a）：
产品负责人事先给的名单里的人，在与 BI Plus 发生**任何一次对话之前**就完成身份定位、
建档、令牌准备、权限合成与发布，于是他第一次发消息时**没有开通等待**，直接得到问数结果。

本模块只放**预开通独有**的东西，其余一律走既有开通链：

1. 「系统触发」这条入口（:func:`run_system_onboarding`，经
   ``AutoOnboardingRunner.start_system`` 暴露）与正式首聊入口**唯一**的结构性差异——
   没有 ``inbound_event`` 行，因此没有认领账本可记、也没有一条"用户正在等"的通知要送
   （:func:`system_event_id`、:data:`NULL_DISPATCH_LEDGER`、:func:`deliver_silently`）；
2. 名单给的是**邮箱**，而飞书侧的定位键是 ``open_id``：邮箱 → 花名册 ``personnel_id``
   → 组织快照成员 → ``open_id`` 的提前定位（:func:`locate_by_email`、:func:`plan_preprovision`）；
3. 预开通完成时静默、**首聊时**才补的那一句提示的挂起动作（:func:`deliver_silently`）。
   那一句的文案键是 ``lingxi.config.content.KEY_PREPROVISIONED_FIRST_CHAT``——键名住在
   内容目录那一侧（消费点在 ``core/conversation/pipeline.py`` 的 ``ACTIVE`` 分支），
   本模块不从 ``core/identity`` 这一侧再定义一份，也是为了不把整条身份链拖进 gateway
   与 worker 的 import 闭包。

## 入口的三处与首聊不同的形状（rc25 S-8b 的 ops 批量入口按这个调）

``start_system(*, email, trace_id, origin, initiated_by_open_id, preprovision_grant)``：

- **按邮箱进，不按 open_id 进**：名单给的是邮箱，定位在
  :func:`locate_by_email` 里做一次，落在开通链**自己**这一侧。调用方（脚本）用
  :func:`plan_preprovision` 出 dry-run 清单时用的是同一个函数，因此"清单上写的是谁"
  与"真正被开通的是谁"不可能分叉——两处各写一份定位才是分叉的来源。
- **同步返回终态**，不是 ``start()`` 那样立刻返回 ``started``：批量脚本要逐人拿到
  结论才能出清单、才能"逐人失败关闭、不阻塞其他人"。链因此跑在**调用方线程**上，
  不进那个专属线程池——脚本本来就是一次性进程，池只会让它多一层等待。进程内
  "同一个人只跑一条链"的去重仍然共用编排自己的那把锁。
- **预授权随链落库**：``preprovision_grant`` 挂在与 rc25 S-1 存量差集导入**同一个
  挂点**——``_run`` 里存量导入之后、零银河判定之前。次序是硬的：名单本身带了新权限
  的人，如果先判零权限就会被整批拒绝（与 S-1 把差集导入挂在零银河判定之前同一条
  理由）。``initiated_by_open_id`` 一路带到落库口，写进合成 ``pending_action`` 的
  责任人栏——**不写死占位身份**：审计栏目里一个无法追溯的假身份比没有审计更糟。

## 为什么共用 ``AutoOnboardingRunner._run``，而不是另写一条预开通链

``_run`` 的公共段上排着两道失败关闭闸与整条发布链：内测名单闸、**同邮箱是否已绑给
另一个人**（rc25 S-2a 对抗审查 X-1，:mod:`lingxi.core.identity.onboarding_guards`）、
零银河兜底、``publish_allowed`` 发布闸、``full_access_wildcard`` 必填参数。第二条链
意味着这五处判定各有第二份实现，而 X-1 的整个立论就是"同邮箱两人共用同一把问数令牌
与同一行正式表权限"——一条绕过它的第二入口会把 S-2a 的根治**直接作废**。因此系统触发
只加一个入口（``AutoOnboardingRunner.start_system``），判定与写入**逐字节**复用
``_run``；本模块提供的三个协作者是那条链上仅有的三处差异，全部是**去掉**动作
（不记账、不发消息），没有一处是新的判定。

## 两条停滞收口在系统触发路径下会同时失效（本模块修的接缝）

分水岭（``advance(provisioning)``）之后的失败，此前有两条收口路径，而它们在系统触发
路径下**同时**不成立：

- 「当场收口」``AutoOnboardingRunner._abort_if_stalled`` 的触发判据是通知**确认送达**
  （外部审查 P2-1 修复：通知彻底失败的人不能被提前判死，要留给四十五分钟兜底）。
  预开通静默不发通知 ⇒ ``delivered`` 恒为假 ⇒ 当场收口**永不触发**。
- 「四十五分钟停摆兜底」``StalledProvisioningDuty`` 的候选查询原本 **INNER** 关联
  ``inbound_event``。系统触发没有事件行 ⇒ 这个人**结构上**永远捞不到。

结果是这个人永久停在 ``provisioning``/``mcp_syncing``，而他一旦发消息，pipeline 会照发
「正在完成…请稍候」——**与 Issue #282 修复前的形状逐字相同**。两处各修一半：

- 本模块的 :func:`deliver_silently` 对系统触发返回 ``True``（"按送达处理"）。P2-1 的
  立论是"用户一条终态都没收到，却已经被收口"，而预开通**本来就不发**任何终态通知，
  没有任何人在等它——那条立论在这条路径上不成立，把它照搬过来只会让人卡死；
- ``adapters/postgres_stalled_provisioning.py`` 的候选查询改成 ``LEFT JOIN LATERAL``，
  无事件行时租约起点取 ``app_user.provisioning_started_at``（**不是** ``updated_at``：
  那一列会被任何无关更新刷新，租约会永远不到期）。

## 邮箱命中多个人员 ID 一律跳过、不猜（产品负责人 2026-09-02 裁定，硬规则）

花名册实测 1223 行里有 **86 组同邮箱对应多个 ``personnel_id``**，其中 3 组 / 7 人按
"同一邮箱下出现多个不同工号"判据是**真的不同人**（W0-6b 实测）。"哪一个还在职"在系统
里判不了（读它需要全系统单消费者的专用授权令牌）；唯一可判定的收敛（与组织快照求交）
只覆盖 59.9% 的花名册人员，**不构成证明**。

而认错人的后果在 rc25 S-2a 的部分唯一索引（迁移 ``0085``）之后是**不可自愈**的：错的
那一行 ``app_user`` 会**永久占住这个邮箱**，真正的那个人首聊时会撞上"同邮箱已绑定他人"
被内部错误拒绝，而仓库里**没有改绑动作**（账号删除流程是唯一出口）。因此
:func:`locate_by_email` 在邮箱命中多个人员 ID 时**一律跳过**，出具体原因码
:data:`SKIP_EMAIL_MULTIPLE_PERSONNEL`，交给失败清单——哪怕其中只有一个在组织快照里。
被跳过的人由产品负责人换一种方式给（直接给 open_id 或工号），或者干脆等他自己首聊走
正常链路：**正常链路从 open_id 出发，天然不会认错人。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence

from lingxi.core.conversation.ports import OnboardingResult, OnboardingState
from lingxi.core.identity.onboarding_terminal import (
    KEY_COMPLETED,
    OnboardingChainError,
    _internal,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.permission.account_match import normalize_email

#: 系统触发（预开通）合成事件标识的前缀。**只**用作通知去重键与审计字段，
#: **不落任何表**——尤其不往 ``inbound_event`` 插假行：那张表是入站审计与对账的基线
#: （``/admin trace`` 与未开通首聊交接对账都读它），插一行假事件会让对账把它当成真实
#: 首聊重新认领，等于自己给自己制造一条重复开通链。
SYSTEM_EVENT_PREFIX = "preprovision:"

#: ``onboarding.result`` 审计里的来源标识，用来分辨"首聊时开通"与"首聊前预开通"。
ORIGIN_PREPROVISION = "preprovision"
ORIGIN_FIRST_CHAT = "first_chat"


def system_event_id(trace_id: str) -> str:
    """系统触发这一次的合成事件标识。见 :data:`SYSTEM_EVENT_PREFIX`。"""

    return f"{SYSTEM_EVENT_PREFIX}{trace_id}"


def is_system_trigger(event_id: str) -> bool:
    """这次开通是不是系统触发（预开通）的。

    判据落在合成事件标识上而不是一个额外的入口参数：``event_id`` 已经贯穿
    ``start`` → 执行线程 → ``_execute`` → ``_notify`` 的每一层，用它判定不需要在这条
    链的四个方法上各加一个参数，也就不存在"某一层忘了往下传"的漏接面。
    """

    return str(event_id or "").startswith(SYSTEM_EVENT_PREFIX)


def origin_of(event_id: str) -> str:
    """审计用的来源标识。"""

    return ORIGIN_PREPROVISION if is_system_trigger(event_id) else ORIGIN_FIRST_CHAT


class _NullDispatchLedger:
    """系统触发的认领账本：两个方法都是 no-op。

    ``DispatchLedger`` 的两个方法都以 ``inbound_event`` 那一行为对象——记账是
    ``onboarding_dispatched_at``、放回是把它清成 ``NULL``。系统触发**没有那一行**，
    两件事都没有对象，因此这里是真正意义上的"无事可做"，不是把失败吞掉。

    **不插假事件行**（见 :data:`SYSTEM_EVENT_PREFIX`）。
    """

    def mark_onboarding_dispatched(self, *, event_id: str) -> None:
        return None

    def release_onboarding_claim(self, *, event_id: str, claim_token: Any = None) -> None:
        return None


#: 进程内共享的单例：本类无状态。
NULL_DISPATCH_LEDGER = _NullDispatchLedger()


class _NoticeArmer(Protocol):
    def mark_preprovision_notice_pending(self, *, open_id: str) -> bool: ...


def deliver_silently(*, key: str, open_id: str, users: _NoticeArmer) -> bool:
    """预开通的"投递"：**不发任何消息**，返回 ``True``（按送达处理）。

    两件事同时成立，缺一不可：

    1. **静默**（产品负责人裁定 4）。预开通是我们替用户做的事，不是他发起的事；在他
       没有任何上下文的时候推一条「开通完成」，他能做的只有困惑。链上两处 ``_notify``
       （中途的「正在同步」与终态）都经过这里，因此静默是结构性的，不靠调用点自觉。
    2. **按送达处理**，好让「当场收口」照常触发——立论见模块文档「两条停滞收口在系统
       触发路径下会同时失效」。

    终态是「开通完成」时，把那句话**挂起**到该用户首聊时再补
    （文案键 ``lingxi.config.content.KEY_PREPROVISIONED_FIRST_CHAT``）。挂起是否真的落下由存储层再判一次
    "这个人此前从没跟我们说过话"，见 ``UserStateStore.mark_preprovision_notice_pending``
    ——同一份名单重跑不会重新挂起（幂等），已经在聊的人也不会被补一句他不需要的话。
    """

    if key != KEY_COMPLETED:
        return True
    users.mark_preprovision_notice_pending(open_id=open_id)
    return True


# ---------------------------------------------------------------------------
# 邮箱 → 飞书身份的提前定位
# ---------------------------------------------------------------------------

#: 逐人跳过的原因码。**互不合并**：产品负责人拿着清单要能分辨"名单写错了邮箱"
#: （前两个）与"这个人我们这边看不见"（后三个），两类的下一步动作完全不同。
SKIP_EMAIL_BLANK = "email_blank"
SKIP_EMAIL_NOT_IN_ROSTER = "email_not_in_roster"
SKIP_EMAIL_MULTIPLE_PERSONNEL = "email_multiple_personnel"
SKIP_DIRECTORY_UNAVAILABLE = "directory_unavailable"
SKIP_PERSONNEL_NOT_IN_DIRECTORY = "personnel_not_in_directory"
SKIP_DIRECTORY_MULTIPLE_MEMBERS = "directory_multiple_members"
#: 花名册快照本身读不出来（``RosterSource.rows()`` 返回 ``None``）。与"这个邮箱不在
#: 花名册里"分开：前者是我们暂时看不见，后者是名单写错了，下一步动作完全不同。
SKIP_ROSTER_UNAVAILABLE = "roster_unavailable"


@dataclass(frozen=True)
class PreprovisionTarget:
    """一个可以进开通链的名单条目：邮箱已经唯一定位到一名飞书成员。"""

    email: str
    personnel_id: str
    member: SnapshotMember

    @property
    def open_id(self) -> str:
        return self.member.open_id


@dataclass(frozen=True)
class PreprovisionSkip:
    """一个被**失败关闭**跳过的名单条目。``reason`` 取上面六个原因码之一。"""

    email: str
    reason: str

    def as_result(self) -> OnboardingResult:
        """跳过也要有一个可被脚本消费的结论。

        状态用 ``NOT_AUTHORIZED``：这一类失败的意思是"我们无法唯一地认定这个邮箱是
        谁"，因此**不能**给他开通——与"这个人没有可用银河权限"同属确定性业务失败，
        不是本侧故障（走 ``INTERNAL_ERROR`` 会触发管理群告警，而名单写错一个邮箱不是
        需要半夜叫人的事）。``messages`` 恒为空：预开通失败**没有任何用户可见出口**，
        逐人原因只报告给产品负责人（脚本清单，rc25 S-8b）。
        """

        return OnboardingResult(state=OnboardingState.NOT_AUTHORIZED, failure_reason=self.reason)


class DirectoryByUserId(Protocol):
    """按飞书 ``user_id``（＝花名册「人员ID」）回读组织快照成员的只读口。

    真实实现是 ``adapters/postgres_identity.PostgresOrgSnapshotStore.lookup_by_user_id``。
    正式首聊路径的方向与此**相反**（``open_id`` → 成员 → ``user_id`` → 花名册 → 银河），
    组织快照适配器此前只有按 ``open_id`` 查这一条 SQL；预开通是唯一需要反向走的路径。
    """

    def lookup_by_user_id(self, user_id: str) -> Any: ...


def locate_by_email(
    email: str,
    *,
    roster_rows: Sequence[Mapping[str, Any]],
    directory: DirectoryByUserId,
) -> PreprovisionTarget | PreprovisionSkip:
    """邮箱 → 花名册 ``personnel_id`` → 组织快照成员。**任一环节非唯一即跳过。**

    这一类失败是**预开通独有**的：正式首聊路径从 ``open_id`` 出发，天然唯一，根本
    走不到这里。跳过的硬规则与它的不可自愈后果见模块文档最后一节。

    ``roster_rows`` 是花名册快照的全量行（``RosterSource.rows()`` 本来就返回全部行，
    不需要新查询）；邮箱按 ``account_match.normalize_email`` 的同一口径归一，避免
    "名单里大写、花名册里小写"这种纯格式差异被当成查无此人。
    """

    needle = normalize_email(email)
    if not needle:
        return PreprovisionSkip(email=email, reason=SKIP_EMAIL_BLANK)
    personnel_ids = sorted(
        {
            str(row.get("personnel_id", "") or "").strip()
            for row in roster_rows
            if normalize_email(row.get("email")) == needle
            and str(row.get("personnel_id", "") or "").strip()
        }
    )
    if not personnel_ids:
        return PreprovisionSkip(email=email, reason=SKIP_EMAIL_NOT_IN_ROSTER)
    if len(personnel_ids) > 1:
        # **一律跳过、不猜**（产品负责人 2026-09-02 裁定 6）：认错人会让错的那一行
        # 永久占住这个邮箱（迁移 0085 的部分唯一索引），真人首聊被拒且**不可自愈**。
        return PreprovisionSkip(email=email, reason=SKIP_EMAIL_MULTIPLE_PERSONNEL)

    lookup = directory.lookup_by_user_id(personnel_ids[0])
    if getattr(lookup, "availability", None) is not DirectoryAvailability.AVAILABLE:
        # 组织资料不可用 / 已过九十天上限时"查不到"不是事实，只是我们暂时看不见——
        # 与 ``AutoOnboardingRunner._locate`` 同一条纪律，不当成"这个人不存在"。
        return PreprovisionSkip(email=email, reason=SKIP_DIRECTORY_UNAVAILABLE)
    members = tuple(getattr(lookup, "members", ()) or ())
    if not members:
        return PreprovisionSkip(email=email, reason=SKIP_PERSONNEL_NOT_IN_DIRECTORY)
    if len(members) > 1:
        return PreprovisionSkip(email=email, reason=SKIP_DIRECTORY_MULTIPLE_MEMBERS)
    return PreprovisionTarget(
        email=email, personnel_id=personnel_ids[0], member=members[0]
    )


def plan_preprovision(
    emails: Iterable[str],
    *,
    roster_rows: Sequence[Mapping[str, Any]],
    directory: DirectoryByUserId,
) -> tuple[tuple[PreprovisionTarget, ...], tuple[PreprovisionSkip, ...]]:
    """把一份名单逐条定位成「可执行」与「跳过」两张清单，**保持名单顺序**。

    同一个邮箱在名单里出现多次只处理一次（后一次静默丢弃）：那是名单本身的笔误，
    不是两个人；对同一个人跑两次开通链没有任何新结果，只会多一条重复的审计。

    **逐人不阻塞**：本函数只做定位，一条都不会抛异常带走其余人；真正的开通由调用方
    （``scripts/ops`` 的预开通入口，rc25 S-8b）逐人 ``try/except`` 调用
    ``AutoOnboardingRunner.start_system``。
    """

    targets: list[PreprovisionTarget] = []
    skips: list[PreprovisionSkip] = []
    seen: set[str] = set()
    for raw in emails:
        normalized = normalize_email(raw)
        if normalized and normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        located = locate_by_email(raw, roster_rows=roster_rows, directory=directory)
        if isinstance(located, PreprovisionTarget):
            targets.append(located)
        else:
            skips.append(located)
    return tuple(targets), tuple(skips)


# ---------------------------------------------------------------------------
# 系统触发入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreprovisionGrant:
    """随预开通链一起落库的那一笔预授权，以及**谁**要为它负责。

    ``plan`` 的具体形态由落库口定义（rc25 S-8b 的
    ``PostgresLocalPermissionOverrideStore.import_position_grant``，形状是「职位 + 公司
    范围」展开出来的公司×指标计划）。本模块**刻意不去认识它**：开通链在这条路上只做
    两件事——把它交给落库口、并保证落库发生在零银河判定**之前**。多认识一层就会在
    ``core/identity`` 里长出第二份权限展开逻辑。

    ``initiated_by_open_id`` 是产品负责人本人的 open_id，写进合成 ``pending_action``
    的责任人栏。**没有默认值**：审计栏目里一个无法追溯的假身份比没有审计更糟。
    """

    plan: Any
    initiated_by_open_id: str


class PositionGrantImporter(Protocol):
    """预授权的落库口（rc25 S-8b 的 ``PostgresLocalPermissionOverrideStore.
    import_position_grant``）。形状照 ``LegacyPermissionImporter.import_plan``：每用户
    一事务——合成一条已终态的 ``pending_action``（``local_permission_override.
    pending_action_id`` 是结构性 NOT NULL 外键，迁移 ``0072``「没有确认卡不能写入」）
    与全部 ``local_permission_override`` 行原子落库，撞唯一索引降级为 ``already_present``。

    任何异常原样上抛，由 ``AutoOnboardingRunner`` 按本侧故障 fail-closed（外部表零写入）
    ——与 S-1 差集导入同一条纪律：一笔没落下去的预授权绝不能让这个人带着"少了权限"的
    范围被发布出去。
    """

    def import_position_grant(
        self,
        *,
        user_id: str,
        target_open_id: str,
        grant: Any,
        now: datetime,
        initiated_by_open_id: str,
    ) -> Any: ...


def import_preprovision_grant(
    importer: PositionGrantImporter | None,
    grant: PreprovisionGrant,
    *,
    user_id: str,
    open_id: str,
    now: datetime,
    audit: Any,
    trace_id: str,
) -> None:
    """把这一笔预授权落库。**落库口没装配就整链失败关闭**，绝不静默跳过。

    静默跳过的用户可见后果是：这个人被开通了、令牌签了、正式表也写了，但**范围里没有
    名单答应给他的那部分权限**——他能问数，只是问不出该问的东西，而当天没有任何东西
    会报警。这与 rc25 S-1 那次"装了存量令牌源、没装差集导入口"的真实漏接是同一个形状，
    修法也照抄：宁可响亮失败。
    """

    if importer is None:
        raise OnboardingChainError("preprovision_grant_importer_not_wired")
    report = importer.import_position_grant(
        user_id=user_id,
        target_open_id=open_id,
        grant=grant.plan,
        now=now,
        initiated_by_open_id=grant.initiated_by_open_id,
    )
    # 审计只记计数类事实，不记权限内容、邮箱或姓名（与本链其余审计同一条纪律）。
    audit.record("onboarding.preprovision_grant_imported", user=user_id, trace_id=trace_id, report=str(type(report).__name__))


class _SystemOnboardingHost(Protocol):
    """:func:`run_system_onboarding` 用到的编排内部面，显式写出来而不是隐式 duck-type。

    只有 ``AutoOnboardingRunner`` 实现它。之所以让入口住在本模块而不是编排类里：
    ``onboarding_runner.py`` 贴着体量棘轮阈值（1500 行），而这一段全部是**预开通独有**
    的形状——首聊路径一行都不走它。
    """

    _roster: Any
    _directory: Any
    _lock: Any
    _running: dict[str, str]

    def _execute(self, *, event_id: str, open_id: str, trace_id: str, claim_token: Any = None, grant: Any = None) -> Any: ...

    def _release(self, open_id: str, event_id: str) -> None: ...


def run_system_onboarding(
    runner: _SystemOnboardingHost,
    *,
    email: str,
    trace_id: str,
    origin: str = ORIGIN_PREPROVISION,
    initiated_by_open_id: str,
    preprovision_grant: Any | None = None,
) -> OnboardingResult:
    """系统触发一次开通，**同步返回终态**。形状与三处差异见模块文档「入口的三处形状」。

    ``origin`` 目前只接受 :data:`ORIGIN_PREPROVISION`：v1 只有"预开通"这一个系统来源，
    而这个值同时决定合成事件标识的前缀（:func:`system_event_id`），也就是链上"要不要
    静默、要不要记账"的判据。收下一个不认识的来源再继续跑，等于让调用方随手把一条链
    变成"不静默但也没有账本"的第三种形态——**失败关闭**，不猜。

    花名册读不出来（``rows()`` 返回 ``None``）时同样跳过而不是当成"查无此人"：那是
    我们暂时看不见，不是这个邮箱不存在，两者的下一步动作完全不同。
    """

    if origin != ORIGIN_PREPROVISION:
        raise ValueError("系统触发目前只有预开通这一个来源")
    if not str(initiated_by_open_id or "").strip():
        raise ValueError("预开通必须带上责任人 open_id：审计里不接受占位身份")
    rows = runner._roster.rows()
    if rows is None:
        return PreprovisionSkip(email=email, reason=SKIP_ROSTER_UNAVAILABLE).as_result()
    located = locate_by_email(email, roster_rows=rows, directory=runner._directory)
    if isinstance(located, PreprovisionSkip):
        return located.as_result()

    event_id = system_event_id(trace_id)
    grant = (
        None if preprovision_grant is None
        else PreprovisionGrant(plan=preprovision_grant, initiated_by_open_id=initiated_by_open_id)
    )
    with runner._lock:
        # 与 ``start()`` 共用同一把锁和同一个登记表：一个人同时被脚本和自己的首聊触发
        # 是真实形状（名单里的人恰好在这一刻发了第一条消息），两条链必须互斥。
        if runner._running.setdefault(located.open_id, event_id) != event_id:
            return OnboardingResult(state=OnboardingState.STARTED, failure_reason="already_running")
    try:
        terminal = runner._execute(
            event_id=event_id, open_id=located.open_id, trace_id=trace_id, grant=grant
        )
    finally:
        runner._release(located.open_id, event_id)
    if terminal is None:
        # 停机信号落在链中途：``_execute`` 已经如实记了审计、什么也没收口。
        terminal = _internal("aborted_while_stopping")
    return terminal.as_result(trace_id=trace_id)


__all__ = [
    "DirectoryByUserId",
    "NULL_DISPATCH_LEDGER",
    "ORIGIN_FIRST_CHAT",
    "ORIGIN_PREPROVISION",
    "PositionGrantImporter",
    "PreprovisionGrant",
    "PreprovisionSkip",
    "PreprovisionTarget",
    "SKIP_DIRECTORY_MULTIPLE_MEMBERS",
    "SKIP_DIRECTORY_UNAVAILABLE",
    "SKIP_EMAIL_BLANK",
    "SKIP_EMAIL_MULTIPLE_PERSONNEL",
    "SKIP_EMAIL_NOT_IN_ROSTER",
    "SKIP_PERSONNEL_NOT_IN_DIRECTORY",
    "SKIP_ROSTER_UNAVAILABLE",
    "SYSTEM_EVENT_PREFIX",
    "deliver_silently",
    "import_preprovision_grant",
    "is_system_trigger",
    "locate_by_email",
    "origin_of",
    "plan_preprovision",
    "run_system_onboarding",
    "system_event_id",
]
