"""逐用户的权限决策树：从存档身份与两份快照算到一次权限决定的提交，两个入口共用一份。

每日批与管理员即时动作此前各持一份同型决策树，靠人工对齐；判定原语（匹配、聚合、
翻译、合并、发布行结算）本来只有一份，重复的是把它们串起来的顺序与出口。收拢之后
入口之间只剩三处**显式参数**：花名册行与银河快照由入口自己读好传入（每日批在整轮
前置里判「花名册今天更新过」，即时动作只要求快照存在——判据留在入口，本模块不认识
日期）；本地覆盖来源由入口装配（含「全部」组补行与读失败留痕）；令牌密文读取口可以
缺席（即时动作所在进程没有主密钥，发布行不带密文，由之后的发布执行器失败关闭）。

**入口特有的东西不进来**：身份从哪里查、留痕事件叫什么名字、报告怎么计数、通知谁，
全部留在各自入口。本模块只回答「这个人这次落到哪条出口、写了什么」——出口用
:class:`DecisionBranch` 与原始事实表达，不定义任何一侧的原因码词汇。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.decision_chain import LocalOverrideDecisionSource
from lingxi.core.permission.local_override import LocalOverrideReadError
from lingxi.core.permission.merge_sources import merge_permission_sources
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombinationError,
    translate_company_functions,
)
from lingxi.core.permission.publish import PermissionGrantBlockedByAccountStateError
from lingxi.core.permission.publish_row import (
    ADMIN_FULL_ACCESS_FUNCTION,
    aggregate_permission,
    build_revocation_row,
    build_translated_publish_row,
)


class Decision(Protocol):
    """``record_decision`` 的回执：有没有真的排出新意图，以及同事务清掉的已送达正文数。"""

    enqueued: bool
    cleared_events: int


class DecisionStore(Protocol):
    """权限决定的落库口。

    版本推进、幂等与账号状态复检全部由它在自己持有的行锁里承担；本模块不读、不写、
    不比较版本，也不自己判断「这次权限有没有变化」。``require_enabled_account`` 是
    必填关键字参数：授权侧传 ``True``、撤权侧传 ``False``，传反在实现侧运行期就写不出来。
    """

    def record_decision(
        self,
        *,
        user_id: str,
        row: Any,
        reason: str,
        require_enabled_account: bool,
        decided_at: datetime,
        clear_delivered_content: bool = False,
    ) -> Decision:
        """落一次权限决定。"""
        ...


class PublishHistory(Protocol):
    """「这个人在发布链上有没有留下过足迹」的只读口。

    只有一个只回答"有没有"的方法：撤权那一路不需要发布队列的写侧，把它摆进来等于让
    "顺手改一下那条意图"在类型上变得可写。在途的旧意图同样算足迹。
    """

    def has_publish_footprint(self, user_id: str) -> bool:
        """发布成功过，或当前还有意图在途。"""
        ...


class TokenCipherReader(Protocol):
    """令牌**密文**的只读口。

    刻意只声明读取一个方法：签发口不在这个协议里，因此"重算时顺手签一份令牌"这件事
    在类型上就写不出来——本决策树一次都不签发，取不到密文就留空，由发布执行器失败关闭。
    """

    def token_cipher(self, user_id: str) -> str | None:
        """这个人已经登记的密文；没有就 ``None``。"""
        ...


@dataclass(frozen=True)
class DecisionPorts:
    """决策树要落决定、查足迹、读密文的三个端口。

    ``token_ciphers`` 为 ``None`` 表示这个入口所在的进程没有读取密文的钥匙：发布行
    不带密文照常落决定，这个人若在发布链上还没有足迹，就通过回调让入口留一条可分辨
    的痕迹——真正的失败关闭发生在之后独立一轮的发布执行器，运维不会自然地把两处联系起来。
    """

    decisions: DecisionStore
    publish_history: PublishHistory
    token_ciphers: TokenCipherReader | None


class DecisionBranch(str, Enum):
    """决策树的出口。入口按自己的词汇把出口翻成原因码、计数与留痕，本模块不替它们命名。"""

    MISSING_PERSONNEL_ID = "missing_personnel_id"
    MATCH_FAILED = "match_failed"
    ARCHIVE_INCOMPLETE = "archive_incomplete"
    TRANSLATION_UNCOVERED = "translation_uncovered"
    LOCAL_OVERRIDE_READ_FAILED = "local_override_read_failed"
    SUPPRESSION_UNREPRESENTABLE = "suppression_unrepresentable"
    REVOKE_WITHOUT_FOOTPRINT = "revoke_without_footprint"
    REVOKED = "revoked"
    GRANT_BLOCKED = "grant_blocked"
    PUBLISHED = "published"


@dataclass(frozen=True)
class UserDecision:
    """一次逐用户决策的结论：落到哪条出口，以及入口翻译它所需的原始事实。

    ``zero_galaxy_reason`` 非空表示银河这一侧判「无可用权限」、走的是本地授权兜底
    分支；两条撤权出口据此区分"银河本来就没给"与"银河给了、被本地抑制清空"。
    """

    branch: DecisionBranch
    match_reason: str | None = None
    zero_galaxy_reason: str | None = None
    mapping_is_empty: bool = False
    account_state: str | None = None
    enqueued: bool = False
    cleared_events: int = 0
    published_without_cipher: bool = False


class UserPermissionDecisionTree:
    """匹配 → 聚合 → 翻译 → 合并本地覆盖 → 发布或撤权，任何"不发布"的出口都显式返回。

    匹配失败只跳过、不撤权：它说的是"我们认不出这个人是谁"，不是"银河说他没有权限"。
    据一次花名册歧义或数据陈旧去清空一个人的权限，方向与花名册那一侧「查无此人仅
    提示、不做任何自动处置」的既定口径正好相反。
    """

    def __init__(
        self,
        *,
        ports: DecisionPorts,
        role_function_map: Mapping[str, str],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        local_override_source: LocalOverrideDecisionSource,
        publish_reason: str,
        revoke_reason: str,
        on_local_override_skipped: Callable[[str, str], None],
        on_publish_without_cipher: Callable[[str], None] | None = None,
    ) -> None:
        """接线端口、两份映射、本地覆盖来源、两个决定原因码与入口自己的留痕回调。"""
        self._ports = ports
        self._role_function_map = role_function_map
        self._metric_translation_map = metric_translation_map
        self._local_override_source = local_override_source
        self._publish_reason = publish_reason
        self._revoke_reason = revoke_reason
        self._on_local_override_skipped = on_local_override_skipped
        self._on_publish_without_cipher = on_publish_without_cipher

    def decide(
        self,
        identity: ArchivedIdentity,
        roster_rows: Sequence[Any],
        galaxy: Any,
        *,
        now: datetime,
    ) -> UserDecision:
        """对一个已开通用户走完整条决策树；``now`` 是这次决定的时刻。

        存档不全在聚合之后才判：两种发布行都需要邮箱和姓名，任何合并结果都救不了一个
        存档不全的人，但入口要知道这个人是不是零银河，才能按自己的口径计数。
        """
        if not identity.personnel_id:
            # 建档合同要求人员 ID 必填，但存档里真的没有时，匹配层会直接抛错。
            # 在这里归类成"输入不完整"，而不是让它冒充一次技术故障。
            return UserDecision(DecisionBranch.MISSING_PERSONNEL_ID)
        match = match_galaxy_account(identity.personnel_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            return UserDecision(DecisionBranch.MATCH_FAILED, match_reason=match.reason)
        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )
        zero_galaxy_reason = None if aggregate.granted else aggregate.reason
        if not identity.email or not identity.display_name:
            return UserDecision(
                DecisionBranch.ARCHIVE_INCOMPLETE, zero_galaxy_reason=zero_galaxy_reason
            )
        if aggregate.granted:
            translated = self._translate(aggregate)
            if isinstance(translated, UserDecision):
                return translated
            company_metrics = translated
        else:
            # 零银河分支不翻译：银河的公司与职能恒为空，对合并的贡献直接是空字典
            # （翻译对空输入会直接拒绝，那是「参数缺失」不是「没有内容」）。
            company_metrics = {}
        return self._merge_and_settle(identity, aggregate, company_metrics, zero_galaxy_reason, now)

    def revoke(self, identity: ArchivedIdentity, *, now: datetime) -> UserDecision:
        """不看银河、直接走撤权出口：没有发布足迹就跳过，有就落一次空权限决定。

        撤权是**保行、清空权限内容**：发布表那一行留着，权限写成空对象，状态与令牌密文
        都不碰。「在途也算足迹」是必需的：昨天排的授权意图还堵在待发布、今天这个人被撤权
        时若跳过，等发布面消费积压时已经被收回的范围会被写进外部表。
        """
        return self._revoke(identity, zero_galaxy_reason=None, now=now)

    def _translate(self, aggregate: Any) -> Mapping[str, Sequence[str]] | UserDecision:
        """把「公司 + 职能」翻成指标名；未覆盖就走跳过出口——不发布，也不撤权。

        映射整体为空与个别组合未覆盖按真实取值分类而不是硬编码：入口的整轮判据通常已
        保证走到这里时映射非空，但这条逐用户判据的正确性不依赖那条外部不变量。
        """
        try:
            return translate_company_functions(
                companies=aggregate.companies,
                functions=aggregate.functions,
                all_companies=aggregate.all_companies,
                mapping=self._metric_translation_map,
            )
        except UncoveredPermissionCombinationError as error:
            return UserDecision(
                DecisionBranch.TRANSLATION_UNCOVERED, mapping_is_empty=error.mapping_is_empty
            )

    def _merge_and_settle(
        self,
        identity: ArchivedIdentity,
        aggregate: Any,
        company_metrics: Mapping[str, Sequence[str]],
        zero_galaxy_reason: str | None,
        now: datetime,
    ) -> UserDecision:
        """真实权限 =（银河 ∪ 本地授权）− 本地抑制，之后决定发布还是撤权。

        本地覆盖读不出来 ≠ 这个人没有本地覆盖：合并对"没有本地源"恒等，照常算下去会
        产出一份少了本地补授、也少了本地抑制的完整权限决定——一次数据库抖动因此变成
        一次冒充完整结果的越权/欠权发布；零银河分支上它更是直接决定"发布还是撤权"。
        合并结果被抑制压光到空时走撤权出口：银河那一侧原本是有效授权，本地行政性地收回
        到零，语义等同于撤权；不这么做的话空字典会让渲染函数抛错，被记成不可分辨的通用失败。
        """
        try:
            local = self._local_override_source.resolve(identity.app_user_id)
        except LocalOverrideReadError:
            return UserDecision(
                DecisionBranch.LOCAL_OVERRIDE_READ_FAILED, zero_galaxy_reason=zero_galaxy_reason
            )
        # 通配全指标有两个互相独立的成因（范围覆盖全部国家，或持有全量访问职能），只有
        # 后者是真的全指标通配——合并函数自己不猜，调用方必须显式声明。零银河分支的
        # 职能恒为空元组，取值对结果没有作用面，仍按同一条规则显式传参。
        merged = merge_permission_sources(
            galaxy=company_metrics,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:
            self._on_local_override_skipped(identity.app_user_id, reason)
        if merged.unrepresentable_companies:
            return UserDecision(
                DecisionBranch.SUPPRESSION_UNREPRESENTABLE, zero_galaxy_reason=zero_galaxy_reason
            )
        if not merged.permissions:
            return self._revoke(identity, zero_galaxy_reason=zero_galaxy_reason, now=now)
        return self._publish(identity, merged.permissions, now)

    def _revoke(
        self, identity: ArchivedIdentity, *, zero_galaxy_reason: str | None, now: datetime
    ) -> UserDecision:
        """撤权两出口：从无发布足迹 → 跳过（不为他新建一行空权限）；否则落撤权决定。

        撤权**任何账号状态都必须放行**：挡住撤权＝停用彻底失效，是方向相反、后果最严重
        的那种错误——强制撤权的服务对象本来就是已停用用户。是否真的排出新意图由落决定
        的内容比对决定，因此第二天仍然无权限时判"无变化"，撤权不会每天重发一次。
        """
        user_id = identity.app_user_id
        if not self._ports.publish_history.has_publish_footprint(user_id):
            return UserDecision(
                DecisionBranch.REVOKE_WITHOUT_FOOTPRINT, zero_galaxy_reason=zero_galaxy_reason
            )
        decision = self._ports.decisions.record_decision(
            user_id=user_id,
            row=build_revocation_row(
                email=identity.email, display_name=identity.display_name, decided_at=now
            ),
            reason=self._revoke_reason,
            require_enabled_account=False,
            decided_at=now,
            clear_delivered_content=True,
        )
        return UserDecision(
            DecisionBranch.REVOKED,
            zero_galaxy_reason=zero_galaxy_reason,
            enqueued=decision.enqueued,
            cleared_events=decision.cleared_events,
        )

    def _publish(
        self,
        identity: ArchivedIdentity,
        company_metrics: Mapping[str, Sequence[str]],
        now: datetime,
    ) -> UserDecision:
        """结算并落一次授权发布决定，银河授权与零银河本地兜底两条路径殊途同归。

        这是一份**需要账号有效**的授权：基线读取到轮到这个人之间，管理员可能刚把他停用
        并排空了权限——判据必须落在落决定那把已经持有的行锁里，而不是这里先查一次账号
        状态（那只会把窗口缩小）。被挡是正确结果，不是故障：事务整体回滚，一个字节都没写。
        """
        user_id = identity.app_user_id
        without_cipher = False
        token_cipher = None
        if self._ports.token_ciphers is None:
            without_cipher = not self._ports.publish_history.has_publish_footprint(user_id)
            if without_cipher and self._on_publish_without_cipher is not None:
                self._on_publish_without_cipher(user_id)
        else:
            token_cipher = self._ports.token_ciphers.token_cipher(user_id)
        row = build_translated_publish_row(
            company_metrics=company_metrics,
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
            token_cipher=token_cipher,
        )
        try:
            decision = self._ports.decisions.record_decision(
                user_id=user_id,
                row=row,
                reason=self._publish_reason,
                require_enabled_account=True,
                decided_at=now,
                clear_delivered_content=True,
            )
        except PermissionGrantBlockedByAccountStateError as blocked:
            return UserDecision(DecisionBranch.GRANT_BLOCKED, account_state=blocked.account_state)
        return UserDecision(
            DecisionBranch.PUBLISHED,
            enqueued=decision.enqueued,
            cleared_events=decision.cleared_events,
            published_without_cipher=without_cipher,
        )


__all__ = [
    "Decision",
    "DecisionBranch",
    "DecisionPorts",
    "DecisionStore",
    "PublishHistory",
    "TokenCipherReader",
    "UserDecision",
    "UserPermissionDecisionTree",
]
