"""首次开通链上每一步的实现，以及全部注入口的持有者。

本模块住在 ``core/``：不 import 任何适配器、不发请求、不连数据库、不读时钟。外部世界全部
以协议注入，因此这些步骤在没有网络也没有数据库的机器上就能被完整断言。

编排次序住在 :class:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner`；这里
只回答"某一步怎么做、它的失败去向是哪一类终态"。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from lingxi.core.conversation.ports import OnboardingState
from lingxi.core.identity.first_contact import (
    EmploymentStatus,
    FirstContactOutcome,
    decide_first_contact,
    locate_by_open_id,
)
from lingxi.core.identity.legacy_permission_import import (
    import_legacy_permissions,
    translate_galaxy,
)
from lingxi.core.identity.onboarding_config import (
    OnboardingActions,
    OnboardingPolicy,
    OnboardingRecords,
    OnboardingRuntime,
    OnboardingSources,
)
from lingxi.core.identity.onboarding_support import draft_from_member, roster_row_for
from lingxi.core.identity.onboarding_terminal import (
    KEY_COMPLETED,
    KEY_DELEGATED_SUBJECT,
    KEY_SUSPENDED,
    KEY_SYNC_TIMEOUT,
    STATE_ACTIVE,
    OnboardingChainError,
    _ChainAborted,
    _internal,
    _not_authorized,
    _Terminal,
)
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember
from lingxi.core.identity.provisioning import ProvisioningRejection, ProvisioningRequest
from lingxi.core.identity.stock_token_source import ADOPTABLE, DECRYPT_FAILED, StockTokenLookup
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.local_override import ResolvedLocalOverrides, resolve_local_overrides
from lingxi.core.permission.mcp_readiness import ReadinessBinding, ReadinessOutcome
from lingxi.core.permission.merge_sources import (
    REASON_LOCAL_OVERRIDE_READ_FAILED,
    merge_permission_sources,
)
from lingxi.core.permission.notification import describe_scope
from lingxi.core.permission.publish import PermissionGrantBlockedByAccountState
from lingxi.core.permission.publish_row import (
    ADMIN_FULL_ACCESS_FUNCTION,
    STATUS_APPROVED,
    aggregate_permission,
    build_translated_publish_row,
    parse_permissions,
    serialize_permissions,
)

logger = logging.getLogger("lingxi.core.identity.onboarding_runner")

_UTC = UTC

#: 权限发布意图的原因码。与每日重算的原因码分开，让审计能一眼看出「这一版是开通排的
#: 还是重算排的」。
FIRST_ONBOARDING_REASON = "first_onboarding"


class OnboardingSteps:
    """持有全部注入口，并实现开通链上的每一步。"""

    def __init__(
        self,
        *,
        sources: OnboardingSources,
        actions: OnboardingActions,
        records: OnboardingRecords,
        policy: OnboardingPolicy,
        runtime: OnboardingRuntime,
    ) -> None:
        """把五组注入口摊平成实例字段，链上各步直接按名字取用。"""
        self._directory = sources.directory
        self._employment = sources.employment
        self._roster = sources.roster
        self._galaxy = sources.galaxy
        self._email_bindings = sources.email_bindings
        self._local_overrides = sources.local_overrides
        self._stock_tokens = sources.stock_tokens
        self._legacy_importer = sources.legacy_importer

        self._provisioning = actions.provisioning
        self._users = actions.users
        self._environment = actions.environment
        self._tokens = actions.tokens
        self._decisions = actions.decisions
        self._readiness = actions.readiness
        self._notifier = actions.notifier
        self._position_grants = actions.position_grants

        self._ledger = records.ledger
        self._audit = records.audit
        self._failure_reasons = records.failure_reasons
        self._onboarding_failed = records.onboarding_failed

        self._role_function_map = policy.role_function_map
        self._metric_translation_map = policy.metric_translation_map or {}
        self._innertest_roster_gate = policy.innertest_roster_gate
        self._delegated_subject = policy.delegated_subject
        self._publish_allowed = policy.publish_allowed
        self._publish_wait_seconds = float(policy.publish_wait_seconds)
        self._notify_attempts = policy.notify_attempts

        self._submit = runtime.submit
        self._sleep = runtime.sleep
        self._clock = runtime.clock or (lambda: datetime.now(_UTC))
        self._should_stop = runtime.should_stop or (lambda: False)

        self._lock = threading.Lock()
        self._running: dict[str, str] = {}
        #: 已经因为「通知没送到」释放过一次认领的事件。**每条事件只放回一次**：释放让
        #: 下一轮把整条链重跑一遍，而链上有可能等满十五分钟的就绪确认；无上界地放回会让
        #: 一次外部平台长时间不可用把执行器永久占满。第二次仍然送不到就记账收口，并留
        #: 一条 ``failed`` 后缀的响亮审计。
        self._released_for_notify: set[str] = set()

    # ---- 1. 身份定位（Epic B） ------------------------------------------

    def _locate(self, open_id: str) -> _Terminal | SnapshotMember:
        try:
            lookup = self._directory.lookup(open_id)
        except Exception as error:  # noqa: BLE001 - 读不到组织资料是本侧故障
            raise OnboardingChainError(f"directory_read_failed_{type(error).__name__}") from error
        availability = getattr(lookup, "availability", None)
        if not isinstance(availability, DirectoryAvailability):
            raise OnboardingChainError("directory_availability_unreadable")
        members = tuple(getattr(lookup, "members", ()) or ())
        location = locate_by_open_id(open_id, members)

        employment: EmploymentStatus | None = None
        if (
            availability is DirectoryAvailability.AVAILABLE
            and location.member is not None
            and location.member.tenant_key
        ):
            # **在职状态只能实时回读**（`V-开通-07`）：可见范围不做在职过滤，710 人实测
            # 中含 5 名冻结、1 名未加入。读取失败是本侧故障，不是"不在职"。
            try:
                employment = self._employment.status(
                    tenant_key=location.member.tenant_key, open_id=open_id
                )
            except Exception as error:  # noqa: BLE001
                raise OnboardingChainError(
                    f"employment_read_failed_{type(error).__name__}"
                ) from error

        try:
            delegated_subject_open_id = self._delegated_subject()
        except Exception as error:  # noqa: BLE001 - 读不到登记不等于"没有专用主体"
            # 失败开放会让专用授权账号落回普通员工路径并被建档（`V-身份-02` 的反面）。
            raise OnboardingChainError(
                f"delegated_subject_read_failed_{type(error).__name__}"
            ) from error

        decision = decide_first_contact(
            open_id=open_id,
            location=location,
            employment=employment,
            directory=availability,
            delegated_subject_open_id=delegated_subject_open_id,
        )
        if decision.outcome is FirstContactOutcome.RECORD_READY:
            assert location.member is not None  # RECORD_READY 蕴含定位成功
            return location.member
        if decision.outcome is FirstContactOutcome.DELEGATED_SUBJECT_IGNORED:
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED,
                KEY_DELEGATED_SUBJECT,
                reason="delegated_subject",
            )
        if decision.outcome is FirstContactOutcome.DIRECTORY_UNAVAILABLE:
            # 组织资料不可用时"定位不到"不是事实，只是我们暂时看不见 → 本侧故障。
            return _internal("directory_unavailable")
        reason = decision.failure_reason.value if decision.failure_reason else "not_located"
        return _not_authorized(reason)

    # ---- 2. 花名册 + 3. 银河唯一匹配 ------------------------------------

    def _match(
        self, member: SnapshotMember, *, trace_id: str
    ) -> _Terminal | tuple[ProvisioningRequest, Any]:
        roster_rows = self._roster.rows()
        if roster_rows is None:
            # 花名册快照根本不存在：这是**我们**缺一份数据，不是这个人没有权限。归成
            # "无可用银河权限"会把用户引去银河申请一个他其实已经有的权限。
            return _internal("roster_snapshot_missing")
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            return _internal("galaxy_batch_missing")

        match = match_galaxy_account(member.user_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            # 零条、多条、双键冲突、花名册重复、资料不完整全部走同一个用户出口，内部
            # 原因码仍然互不合并（`V-开通-17`）。
            return _not_authorized(match.reason)

        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )
        self._audit.record("onboarding.aggregated", trace_id=trace_id, **aggregate.audit_facts())
        # **不再在这里直接判定"无可用银河权限"**（PM 2026-08-29 裁定，Issue #419，
        # 消 `V-权限-15` 此前登记的已知限制）：零银河权限的用户如果存在本地授权
        # （管理员兜底赋权），仍应当继续开通并发布本地授权集合，与每日重算
        # （`permission_refresh.py::_refresh_user`）的语义保持一致。这里还没有
        # `app_user.id`（本地覆盖条目的键，迁移 `0072`；本方法发生在 `_provision`
        # 之前），查不了本地覆盖，因此真正的"这个人到底有没有可发布内容"判定推迟
        # 到建档、账号状态复核之后（`_run` 新增的 `_reject_zero_galaxy_without_
        # local_grant` 调用点），不在这里提前拒绝——但也不为一个注定被拒绝的人
        # （既无银河也无本地授权）签发令牌、建用户环境，见该方法文档。

        if aggregate.granted and not self._publish_allowed():
            # **翻译层不可用：一条发布意图都不排**（与 Issue #227 在每日重算那一侧的
            # 整轮判据同一条纪律，授权与撤权都不例外）。停在这里而不是继续：
            #
            # - 不是"没有银河权限"——这个人明明有，说成没有会把他引去银河申请一个他
            #   已经有的权限；
            # - 不是"MCP 同步超时"——那条说的是外部同步慢，而这里是我们自己缺一份内容；
            # - 也不能"先建档建环境、发布那步以后再补"：合同要求成功以发布 + 就绪确认
            #   为前提，半开的用户会一直停在 mcp_syncing 而没有任何人会来收拾。
            #
            # 因此按本侧故障收口（`LX-ONBOARD-001`，已转交管理员），且**在建档之前**，
            # 不留下任何半成品。**这道闸门的适用范围没有变**（Issue #419「既有出口
            # 闸门全部保持」）：只保护"银河内容需要翻译才能安全发布"这件事——零银河
            # 权限的用户没有银河内容需要翻译，本地授权本身已经是精确指标名，与改动前
            # "零银河用户结构上从不到达这道检查"逐字节一致（该检查此前挂在
            # `aggregate.granted` 的早退之后，零银河用户从未走到过这里）。
            self._audit.record("onboarding.publish_gate_closed", trace_id=trace_id)
            return _internal("permission_translation_unavailable")

        roster_row = roster_row_for(member.user_id, roster_rows)
        request = ProvisioningRequest.from_roster_row(draft_from_member(member), roster_row)
        return request, aggregate

    # ---- 4. 建档（#89 写侧） --------------------------------------------

    def _provision(self, request: ProvisioningRequest) -> _Terminal | str:
        result = self._provisioning.provision(request)
        if result.provisioned:
            assert result.app_user_id is not None  # ProvisioningResult 的不变式
            return result.app_user_id
        rejection = result.rejection
        if rejection is ProvisioningRejection.DELEGATED_SUBJECT:
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED,
                KEY_DELEGATED_SUBJECT,
                reason="delegated_subject",
            )
        if rejection is not None and rejection.is_storage_fault:
            # **库把工号吞了**不是"你没有银河权限"：后者会把用户引去银河申请一个他其实
            # 已经有的权限（接口设计 §8.1）。
            return _internal(rejection.value)
        return _not_authorized(rejection.value if rejection else "provision_rejected")

    # ---- 5. 续行前复核 ---------------------------------------------------

    def _recheck_still_provisionable(
        self, user_id: str, *, aggregate: Any, trace_id: str
    ) -> _Terminal | None:
        """``already_provisioned`` 不等于「这个人现在还该被开通」（接口设计 §8.1）。"""

        status = self._users.read_status(user_id)
        if status is None:
            # 刚刚建档成功却读不回来：库侧不一致，绝不当成"可以继续"。
            raise OnboardingChainError("user_row_disappeared")
        if status.account_state != "enabled":
            self._audit.record(
                "onboarding.halted_account_state",
                user=user_id,
                account_state=status.account_state,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED, KEY_SUSPENDED, reason="account_not_enabled"
            )
        if status.provisioning_state == STATE_ACTIVE:
            # 已经开通完了（重新认领的窗口里被另一条链跑完，或上一轮跑完但通知没送到）。
            # **不重复建环境、不重复发布**（`V-开通-14`）——但**照常通知**：这条路径正是
            # "上一次结论没送到、被重新认领"的收敛出口，不通知就等于把它烧掉。重复推送由
            # 绑定事件的去重键挡住（同一条事件的两次执行用同一个键）。
            #
            # 范围**不重新聚合一次外部权限**，用本轮已经算出来的那一份：它与即将发布
            # （或已经发布）的那一版是同一个来源，不会凭空编出一个用户没有的范围。
            self._audit.record("onboarding.already_active", user=user_id, trace_id=trace_id)
            return self._completed(serialize_permissions(aggregate))
        return None

    # ---- 6. 令牌 + 用户环境 ---------------------------------------------

    def _lookup_stock_token(self, email: str | None) -> StockTokenLookup | None:
        """按邮箱查一次存量令牌源（#281 改道）；``None``＝源未装配，原样走签新路径。
        rc25 S-1 起提前到零银河判定之前：同一结果供差集导入与 `_issue_token` 共用。"""

        if self._stock_tokens is None:
            return None
        try:
            return self._stock_tokens.lookup(email)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(
                f"stock_token_lookup_failed_{type(error).__name__}"
            ) from error

    def _import_legacy_permissions(
        self,
        user_id: str,
        lookup: StockTokenLookup,
        aggregate: Any,
        galaxy_map: Mapping[str, Sequence[str]],
        *,
        open_id: str,
        trace_id: str,
    ) -> None:
        """存量用户首聊差集导入（rc25 S-1）：见 ``legacy_permission_import.import_legacy_permissions``。"""

        assert self._legacy_importer is not None  # 构造期不变式：源装了导入口必装
        import_legacy_permissions(
            importer=self._legacy_importer,
            audit=self._audit,
            metric_translation_map=self._metric_translation_map,
            now=self._clock(),
            user_id=user_id,
            permissions_text=lookup.permissions,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
            galaxy_map=galaxy_map,
            open_id=open_id,
            trace_id=trace_id,
        )

    def _issue_token(self, user_id: str, lookup: StockTokenLookup | None) -> Any:
        """签发或采纳该用户的问数 MCP 访问令牌（adopt-or-issue，#281 改道裁定）。
        ``lookup`` 为 ``None``（源未装配）＝原签发路径，与接入前逐字节一致；无行 / 有行
        无密文退回签新；有行含密文则采纳；**解密失败响亮失败、绝不退回签新**（签新会让
        用户环境令牌与正式表错位，造成真实 MCP 认证失败）。"""

        if lookup is not None:
            if lookup.state == ADOPTABLE:
                return self._adopt_token(user_id, lookup)
            if lookup.state == DECRYPT_FAILED:
                self._audit.record("onboarding.stock_token_decrypt_failed", user=user_id)
                raise OnboardingChainError("stock_token_decrypt_failed")
            self._audit.record("onboarding.stock_token_absent", user=user_id, state=lookup.state)
        try:
            return self._tokens.issue_token(user_id)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(f"token_issue_failed_{type(error).__name__}") from error

    def _adopt_token(self, user_id: str, lookup: StockTokenLookup) -> Any:
        try:
            adopted = self._tokens.adopt_token(user_id, lookup.secret)
        except Exception as error:  # noqa: BLE001
            raise OnboardingChainError(f"token_adopt_failed_{type(error).__name__}") from error
        # 权限面由银河同步权威决定，不由本步裁量——这里只审计标注，不改变采纳与否
        # （#281 改道裁定第四条）。旧行的 ``permissions`` 由 `_import_legacy_permissions`
        # 作为管理员本地授权导入（rc25 S-1），本步仍只管令牌。
        approved = not lookup.status or lookup.status == STATUS_APPROVED
        self._audit.record(
            "onboarding.stock_token_adopted"
            if adopted.created
            else "onboarding.stock_token_existing_kept",
            user=user_id,
            status_approved=approved,
        )
        return adopted

    def _create_environment(self, user_id: str, issued: Any) -> None:
        """创建用户环境并把该用户的 MCP Bearer 落进它的 ``.mcp.json``。

        **明文只在这一次调用里存在**：``reveal()`` 的返回值不进日志、不进审计、不进异常
        （``adapters/mcp_token_cipher.py`` 的同一条纪律）。
        """

        try:
            self._environment.ensure(user_id=user_id, mcp_token=issued.reveal())
        except Exception as error:  # noqa: BLE001
            # **透传实现给的错误码**（它已经脱敏，只有 errno 符号名）：`ENOENT`（卷没挂）
            # 与 `EACCES`（权限不对）是两种完全不同的运维动作，把它们一起压成
            # `UserEnvironmentError` 等于让排查从头再来一遍。
            detail = getattr(error, "code", None) or type(error).__name__
            raise OnboardingChainError(f"user_environment_failed_{detail}") from error

    # ---- 7. 权限发布 ------------------------------------------------------

    def _translate_galaxy(
        self, user_id: str, aggregate: Any
    ) -> dict[str, tuple[str, ...]] | _Terminal:
        """银河聚合 → 「公司 → 指标名」，只算一次供导入与发布共用（rc25 S-1）；实现见
        ``core/identity/legacy_permission_import.translate_galaxy``。"""

        return translate_galaxy(
            audit=self._audit,
            metric_translation_map=self._metric_translation_map,
            user_id=user_id,
            aggregate=aggregate,
        )

    def _publish(
        self,
        user_id: str,
        request: ProvisioningRequest,
        aggregate: Any,
        issued: Any,
        *,
        galaxy_map: Mapping[str, Sequence[str]],
        trace_id: str,
    ) -> _Terminal | tuple[int, str]:
        if not request.email:
            # 发布行的 record_key/email 两列都来自邮箱；纯工号匹配成功但花名册没有邮箱时
            # 没有"这一行是谁的"的答案。归确定性业务失败，不是本侧故障。
            return _not_authorized("archived_identity_incomplete")
        # 本地权限覆盖合并（S-P-3 #319），接线点在这里而非更早的 `_match`——本地覆盖的 `user_id` 是内部 `app_user.id`，聚合时还没有它。
        # `galaxy_map` 由 `_run` 经 `_translate_galaxy` 算好传入（rc25 S-1）：已翻译的指标名（`{公司: (指标名, …)}`），与 `permission_refresh.py::_refresh_user` 同一条路径产出；零银河用户则是恒为空的 `{}`。
        local = self._resolve_local_overrides(user_id)
        # 通配角 v2（Issue #440）：`all_companies=True` 有两个互相独立的成因
        # （`scope.all_countries` 或持有 `ADMIN_FULL_ACCESS_FUNCTION`），只有后者
        # 是「真全指标通配」——`merge_permission_sources` 自己不猜测，调用方必须
        # 显式声明（见该函数「通配角 v2」文档）。零银河分支 `aggregate.functions`
        # 恒为空元组，`in` 判据天然为 False，参数在 `galaxy={}` 时本就无效果。
        merged = merge_permission_sources(
            galaxy=galaxy_map,
            local=local,
            full_access_wildcard=ADMIN_FULL_ACCESS_FUNCTION in aggregate.functions,
        )
        for reason in merged.skipped_reasons:  # 通配角 v1：见 merge_sources.py「通配角」一节
            self._audit.record("onboarding.local_override_skipped", user=user_id, reason=reason)
        if merged.unrepresentable_companies:
            # 本地「全部」组下某公司被抑制到空，读侧回退制无法表示（merge_sources.py
            # 「本地 "*" 组」一节）：fail-closed，不发布、不撤权，交管理员先撤组再抑制。
            self._audit.record(
                "onboarding.publish_gate_closed",
                user=user_id,
                reason="suppression_on_all_scope_unrepresentable",
            )
            return _internal("suppression_on_all_scope_unrepresentable")
        if not merged.permissions:
            if not aggregate.granted:
                # 防御性分支，理论上不会发生：`_reject_zero_galaxy_without_local_
                # grant` 已经用同一个合并函数确认过非空结果，两次调用之间只隔着
                # 令牌签发/环境创建（都不写本地覆盖表），除非管理员恰好在这个极短
                # 窗口收回了授权（TOCTOU）。归回"无可用银河权限"而不是下面的
                # `fully_suppressed_by_local_override`——原因不同：不是本地抑制
                # 把银河给的压光到零，是银河本来就没给，且这一刻本地授权也没了。
                return _not_authorized(aggregate.reason)
            # 红线-2：galaxy_map 非空（`_match` 已确保 granted），但本地抑制把合并
            # 结果压光到空字典。onboarding 是首次建行，空内容的新建行对 MCP 无意义，
            # 归类为确定性业务失败（同 `_match` 的 `not granted → _not_authorized`），
            # 不落到 `build_translated_publish_row` 对空输入的 `ValueError`。
            return _not_authorized("fully_suppressed_by_local_override")
        row = build_translated_publish_row(
            company_metrics=merged.permissions,
            email=request.email,
            display_name=request.identity.display_name,
            decided_at=self._clock(),
            token_cipher=issued.token_cipher,
        )
        try:
            decision = self._decisions.record_decision(
                user_id=user_id,
                row=row,
                reason=FIRST_ONBOARDING_REASON,
                # Issue #483：首次开通同样是一份**需要账号有效**的授权。`_recheck_
                # still_provisionable` 已经复核过一次账号状态，但那次复核到这里之间
                # 隔着令牌签发与用户环境创建——管理员恰好在这个窗口停用账号是真实
                # 形状，只有落决定那把行锁里的复检能真正把它串起来。
                require_enabled_account=True,
                decided_at=self._clock(),
            )
        except PermissionGrantBlockedByAccountState as blocked:
            # **必须收敛到既有的停用终态，不能变成通用内部故障**：模块文档
            # 「``account_state != 'enabled'`` → 停止开通，不建环境、不发权限，用户按
            # 「账号已停用」告知」说的正是这一种，用户看到的仍是「你的 BI Plus 账号
            # 当前已停用」。审计动作与 `_recheck_still_provisionable`/`_confirm` 两处
            # 复核逐字相同——运维按同一个动作名检索"这条开通链因为账号状态停下了"，
            # 不需要区分是哪一次复核抓到的。事务整体回滚：版本没推进、意图没入队。
            self._audit.record(
                "onboarding.halted_account_state",
                user=user_id,
                account_state=blocked.account_state,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.NOT_AUTHORIZED, KEY_SUSPENDED, reason="account_not_enabled"
            )
        version = int(decision.permission_version)
        # ``UNCHANGED`` 也要等：那一条意图可能还停在 ``pending``（重入、或每日重算刚排出
        # 来还没被消费）。跳过等待会让"发布面根本没跑"表现成十五分钟的 MCP 同步超时——
        # 一个本侧故障被说成了外部同步慢，运维会去查错的地方。``_await_published`` 对
        # 已经 ``published`` 的意图第一次回读就返回，因此这里不引入任何多余等待。
        waited = self._await_published(decision.outbox_id)
        if waited is not None:
            return waited
        return version, row.permissions

    def _resolve_local_overrides(self, user_id: str) -> ResolvedLocalOverrides | None:
        """读该用户当前生效的本地覆盖条目。``None``（未装配/读取失败）时对
        ``merge_permission_sources`` 恒等；读取失败额外响亮记一条
        ``onboarding.local_override_skipped``（``reason=local_override_read_failed``），
        异常不冒泡——一次开通不因本地覆盖读取失败而整链失败。"""

        if self._local_overrides is None:
            return None
        try:
            entries = tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception as error:  # noqa: BLE001 - 本地源读取失败只降级，不整链失败
            self._audit.record(
                "onboarding.local_override_skipped",
                user=user_id,
                reason=REASON_LOCAL_OVERRIDE_READ_FAILED,
            )
            logger.error(
                "本地权限覆盖读取失败，本次开通跳过本地源 user=%s error=%s",
                user_id,
                type(error).__name__,
            )
            return None
        return resolve_local_overrides(user_id=user_id, entries=entries)

    def _await_published(self, outbox_id: str) -> _Terminal | None:
        """等发布意图真的被写出去并逐字段读回一致。

        发布的**唯一**执行者是 ``lingxi-scheduler`` 的发布消费职责（单一写入负责人）。
        因此这里只**观察**意图的状态，不自己去写外部表格。

        等不到既不是"没有权限"，也不是"MCP 同步超时"——十五分钟那条终态说的是 MCP 侧
        同步，而这里是我们自己的发布面还没把行写出去，属本侧故障。
        """

        waited = 0.0
        step = 1.0
        while True:
            intent = self._decisions.load(outbox_id)
            status = getattr(intent, "status", None) if intent is not None else None
            if status == "published":
                return None
            if intent is None:
                # 意图查不到：**本轮根本没有排出这一条**（例如翻译层整轮判据把它挡住），
                # 与"排了但发布失败"是两件事。两者的用户出口相同（本侧故障），但原因码
                # 必须可分辨——前者要去看内容配置，后者要去看外部表格调用。
                return _internal("publish_intent_missing")
            if status in ("failed", "superseded"):
                # ``superseded``：本链排的这一版已经被更新的一版取代（撤权或重算）。
                # 那一版自己会被发布并确认，但**本次开通**不能据此宣告成功。
                return _internal(f"publish_{status}")
            if self._should_stop():
                # 停机不是"发布没完成"：那一版意图仍然有效，下一轮重跑会等到它。
                raise _ChainAborted()
            if waited >= self._publish_wait_seconds:
                return _internal("publish_not_completed")
            self._sleep(step)
            waited += step

    # ---- 8. MCP 就绪确认 + 9. active ------------------------------------

    def _confirm(
        self, *, user_id: str, permission_version: int, permissions: str, trace_id: str
    ) -> _Terminal:
        session = self._readiness.confirm(
            ReadinessBinding(user_id=user_id, permission_version=permission_version),
            permissions=permissions,
        )
        outcome = getattr(session, "outcome", None)
        if outcome is ReadinessOutcome.READY:
            # **只有到这里才写 active**：产品合同要求成功提示在环境创建、权限发布与当前
            # 用户 MCP 确认全部完成之后才发（`V-开通-11`）。
            #
            # **再复核一次**：从建档后那次复核到这里最长隔了十七分钟（发布等待 +
            # 就绪预算），管理员在这段时间里停用账号是真实形状。只复核一次等于用十七
            # 分钟前的事实宣告"开通完成"，还会把一个已经被停用的人写成 ``active``。
            status = self._users.read_status(user_id)
            if status is None or status.account_state != "enabled":
                self._audit.record(
                    "onboarding.halted_account_state",
                    user=user_id,
                    account_state=status.account_state if status else "missing",
                    trace_id=trace_id,
                )
                return _Terminal(
                    OnboardingState.NOT_AUTHORIZED, KEY_SUSPENDED, reason="account_not_enabled"
                )
            # **推进结果不能忽略**：条件更新影响 0 行意味着这个人当前状态不允许被推到
            # ``active``（被停用、或已经被另一条路径改写）。忽略返回值就会在库里还是
            # ``mcp_syncing`` 的情况下告诉用户"开通完成"，而他下一条消息仍然会被拒。
            if not self._users.advance_provisioning_state(user_id, to=STATE_ACTIVE):
                self._audit.record(
                    "onboarding.state_advance_refused_failed",
                    user=user_id,
                    provisioning_state=status.provisioning_state,
                    trace_id=trace_id,
                )
                return _internal("state_advance_refused")
            return self._completed(permissions)
        if outcome is ReadinessOutcome.TIMED_OUT:
            # 十五分钟预算耗尽：专用的等待类终态，**不与 LX-ONBOARD-001 混淆**
            # （`V-开通-13`）。状态留在 mcp_syncing，问数照常被拒（`V-开通-05`）。
            self._audit.record(
                "onboarding.sync_timeout",
                user=user_id,
                version=permission_version,
                trace_id=trace_id,
            )
            return _Terminal(
                OnboardingState.SYNC_TIMEOUT, KEY_SYNC_TIMEOUT, reason="mcp_sync_timeout"
            )
        if outcome is ReadinessOutcome.NO_PERMISSION:
            # 走到这一步的人一定有非空权限（聚合已经 granted），因此这条只可能来自本侧
            # 不一致，不是"去银河申请权限"的业务结论。
            return _internal("readiness_no_permission_after_grant")
        return _internal(
            f"readiness_{outcome.value if isinstance(outcome, ReadinessOutcome) else 'unknown'}"
        )

    def _completed(self, permissions: str) -> _Terminal:
        """成功文案必须报出**实际**公司与职能范围（产品合同「开通成功后」）。"""

        company, function = describe_scope(parse_permissions(permissions))
        return _Terminal(
            OnboardingState.COMPLETED,
            KEY_COMPLETED,
            values=(("company_name", company), ("function_name", function)),
        )
