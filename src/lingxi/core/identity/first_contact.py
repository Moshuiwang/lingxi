"""员工首次私聊时的定位与建档判定。

本模块只有规则，没有 I/O。调用次序由 ``apps`` 层组装：

1. 从最近一轮完成的组织快照里按 ``open_id`` 定位（:func:`locate_by_open_id`）；
2. 定位到之后**实时**读取该成员的在职状态（快照里没有、也不会有这个字段）；
3. 把定位结果、在职状态与组织资料可用性交给 :func:`decide_first_contact`。

为什么在职状态是"调用时的入参"而不是快照字段：可见范围不做在职过滤，710 人
实测中含 5 名冻结、1 名未加入；把 ``status`` 存下来会立刻产生"库里在职、飞书
已冻结"的陈旧窗口，而这个字段的唯一用途恰恰是拦截这种情况（断言 V-开通-07）。

判定顺序本身是产品行为，不能重排：专用授权账号的排除必须排在最前，
组织资料不可用的终态必须排在定位之前——组织资料不可用时"定位不到"不是事实，
只是我们暂时看不见。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from lingxi.config.content import ContentRenderError, default_content_catalog
from lingxi.core.identity.org_snapshot import DirectoryAvailability, SnapshotMember, index_members_by_open_id


class LocationOutcome(str, Enum):
    LOCATED = "located"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class LocationResult:
    outcome: LocationOutcome
    member: SnapshotMember | None
    candidate_count: int


def locate_by_open_id(open_id: object, members: Sequence[SnapshotMember]) -> LocationResult:
    """用完整 ``open_id`` 在快照成员里定位。

    只做首尾空白裁剪，**不做**大小写归一、前缀匹配或姓名回退：前缀在 710 人中
    有 57 组碰撞，姓名有重名且不假定语言，两者都不能用于比对。命中多条时返回
    ``AMBIGUOUS`` 而不是任选一条——"不猜测身份"是产品合同的硬要求。
    """

    if not isinstance(open_id, str):
        return LocationResult(LocationOutcome.NOT_FOUND, None, 0)
    needle = open_id.strip()
    if not needle:
        return LocationResult(LocationOutcome.NOT_FOUND, None, 0)
    candidates = index_members_by_open_id(members).get(needle, ())
    if not candidates:
        return LocationResult(LocationOutcome.NOT_FOUND, None, 0)
    if len(candidates) > 1:
        return LocationResult(LocationOutcome.AMBIGUOUS, None, len(candidates))
    return LocationResult(LocationOutcome.LOCATED, candidates[0], 1)


@dataclass(frozen=True)
class EmploymentStatus:
    """飞书成员详情 ``status`` 的判定投影。**只在一次判定中存在，绝不落库。**"""

    is_activated: bool
    is_exited: bool
    is_frozen: bool
    is_resigned: bool
    is_unjoin: bool

    _FLAGS = ("is_activated", "is_exited", "is_frozen", "is_resigned", "is_unjoin")

    @property
    def employed(self) -> bool:
        return self.is_activated and not (self.is_exited or self.is_frozen or self.is_resigned or self.is_unjoin)

    @classmethod
    def from_feishu(cls, payload: Any) -> "EmploymentStatus | None":
        """把飞书返回的 ``status`` 转成判定投影；任何字段缺失或非布尔即返回 ``None``。

        返回 ``None`` 表示**不可判定**，调用方必须拒绝开通，不得当作在职。默认成
        在职是这条链路最危险的错误：它会给一个已冻结的账号开通并发布权限。
        """

        if not isinstance(payload, Mapping):
            return None
        values: dict[str, bool] = {}
        for flag in cls._FLAGS:
            value = payload.get(flag)
            if not isinstance(value, bool):
                return None
            values[flag] = value
        return cls(**values)


class FirstContactOutcome(str, Enum):
    RECORD_READY = "record_ready"
    NOT_AUTHORIZED = "not_authorized"
    DELEGATED_SUBJECT_IGNORED = "delegated_subject_ignored"
    DIRECTORY_UNAVAILABLE = "directory_unavailable"


class FailureReason(str, Enum):
    """不开通的内部诊断原因；只用于审计和排障，不创建人工核对待办。"""

    NOT_LOCATED = "not_located"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    INCOMPLETE_PROFILE = "incomplete_profile"
    EMPLOYMENT_UNKNOWN = "employment_unknown"
    NOT_EMPLOYED = "not_employed"


@dataclass(frozen=True)
class IdentityRecordDraft:
    """建档时允许写入 ``app_user`` 的全部字段——**只有完成身份关联必需的那些**。

    ``permission_record_id`` 恒为 ``None``：权限匹配确认前不先占位再回填
    （断言 V-开通-01）。这里把它写成常量字段而不是"调用方记得别传"，是为了
    让"占位"这件事在类型上就做不到。

    没有在职状态字段，也没有 ``mobile`` / ``job_title`` / ``leader_id``：前者会
    产生陈旧窗口，后者是可选增强。工号与邮箱按《2026-08-05 花名册身份链与工号
    邮箱匹配》决策进入统一用户记录的保存范围（匹配银河的主/辅键），但由花名册
    读取步骤填充、不作为建档前提，因此是可选字段。
    """

    feishu_open_id: str
    feishu_user_id: str
    feishu_union_id: str
    display_name: str
    display_name_locale: str | None
    department: str | None
    tenant_key: str
    employee_no: str | None = None
    email: str | None = None
    provisioning_state: str = "matching"
    permission_record_id: None = None


@dataclass(frozen=True)
class FirstContactDecision:
    outcome: FirstContactOutcome
    #: **历史遗留字段，生产中没有任何消费方**（全仓只有
    #: ``core/permission/notification.py`` 读 ``content_key``；``onboarding_runner._locate``
    #: 只用 ``outcome``/``failure_reason``）。对于带必填占位（如追溯号 ``{reference}``）的
    #: 文案键，本字段留空——判定层拿不到 ``trace_id``（:func:`decide_first_contact` 是纯
    #: 函数，签名里没有它），也不该猜一个假值出来渲染。真正的正文由持有 ``trace_id`` 的
    #: 发送方按 ``content_key`` 渲染（见 Issue #280 联合设计 §7.1）。
    message: str
    content_key: str = ""
    content_version: str = ""
    draft: IdentityRecordDraft | None = None
    failure_reason: FailureReason | None = None

    @property
    def creates_record(self) -> bool:
        return self.draft is not None


_MESSAGE_KEYS: dict[FirstContactOutcome, str] = {
    FirstContactOutcome.RECORD_READY: "onboarding.checking",
    FirstContactOutcome.NOT_AUTHORIZED: "onboarding.not_authorized",
    FirstContactOutcome.DELEGATED_SUBJECT_IGNORED: "onboarding.delegated_subject",
    FirstContactOutcome.DIRECTORY_UNAVAILABLE: "onboarding.internal_error",
}

#: 带必填占位（追溯号 ``{reference}``）、不能在本模块即时渲染的文案键（Issue #280
#: §7.1）。``_decision`` 只确认键存在、取版本号，不渲染正文——渲染需要 ``trace_id``，
#: 而这条判定链路是纯函数，拿不到它。
_DEFERRED_RENDER_KEYS: frozenset[str] = frozenset({"onboarding.internal_error"})


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def decide_first_contact(
    *,
    open_id: object,
    location: LocationResult,
    employment: EmploymentStatus | None,
    directory: DirectoryAvailability,
    delegated_subject_open_id: object,
) -> FirstContactDecision:
    """首聊事件的最终判定。

    刻意**没有**目标租户参数：标识跨租户唯一，租户归属是从快照里查出来的结果，
    不是需要预先配置的输入（Issue #16 硬约束 1）。
    """

    incoming = open_id.strip() if isinstance(open_id, str) else ""
    delegated = delegated_subject_open_id.strip() if isinstance(delegated_subject_open_id, str) else ""

    # 1. 专用授权账号本身永远不建档，且这条判断必须先于任何其他分支——
    #    组织资料不可用、定位不到都不能让它落回普通员工路径（断言 V-身份-02）。
    if delegated and incoming == delegated:
        return _decision(FirstContactOutcome.DELEGATED_SUBJECT_IGNORED)

    # 2. 组织资料不可用或已过九十天上限时，"定位不到"不是事实，只是看不见。
    if directory is not DirectoryAvailability.AVAILABLE:
        return _decision(FirstContactOutcome.DIRECTORY_UNAVAILABLE)

    if location.outcome is LocationOutcome.NOT_FOUND:
        return _decision(FirstContactOutcome.NOT_AUTHORIZED, failure_reason=FailureReason.NOT_LOCATED)
    if location.outcome is LocationOutcome.AMBIGUOUS or location.member is None:
        return _decision(FirstContactOutcome.NOT_AUTHORIZED, failure_reason=FailureReason.AMBIGUOUS_IDENTITY)

    member = location.member

    # 3. 在职状态不可判定或明确非在职都按无可用权限结束。两者都不建档、不建待办。
    if employment is None:
        return _decision(FirstContactOutcome.NOT_AUTHORIZED, failure_reason=FailureReason.EMPLOYMENT_UNKNOWN)
    if not employment.employed:
        return _decision(FirstContactOutcome.NOT_AUTHORIZED, failure_reason=FailureReason.NOT_EMPLOYED)

    # 4. 必要资料缺失时不写半条记录（断言 V-开通-06），统一走无可用权限出口。
    #    部门仍是必要资料；把它当可选字段放行会建出 department 为空的半份档案。
    department = member.department_names[0].strip() if member.department_names and member.department_names[0] else ""
    if any(_blank(value) for value in (member.open_id, member.user_id, member.union_id, member.display_name, member.tenant_key)) or not department:
        return _decision(FirstContactOutcome.NOT_AUTHORIZED, failure_reason=FailureReason.INCOMPLETE_PROFILE)

    draft = IdentityRecordDraft(
        feishu_open_id=member.open_id.strip(),
        feishu_user_id=member.user_id.strip(),
        feishu_union_id=member.union_id.strip(),
        display_name=member.display_name.strip(),
        display_name_locale=member.display_name_locale,
        department=department,
        tenant_key=member.tenant_key.strip(),
    )
    return _decision(FirstContactOutcome.RECORD_READY, draft=draft)


def _decision(
    outcome: FirstContactOutcome,
    *,
    draft: IdentityRecordDraft | None = None,
    failure_reason: FailureReason | None = None,
) -> FirstContactDecision:
    catalog = default_content_catalog()
    key = _MESSAGE_KEYS[outcome]
    if key in _DEFERRED_RENDER_KEYS:
        # 不即时渲染：该键要求 `reference`（追溯号），而这层判定函数没有 trace_id
        # 可传。只确认键真的登记在目录里、取当前版本号——两者是审计真正消费的事实
        # （见 FirstContactDecision.message 字段文档）。
        if key not in catalog.text_keys():
            raise ContentRenderError("unregistered content key")
        return FirstContactDecision(
            outcome=outcome,
            message="",
            content_key=key,
            content_version=catalog.version,
            draft=draft,
            failure_reason=failure_reason,
        )
    content = catalog.text(key)
    return FirstContactDecision(
        outcome=outcome,
        message=content.text,
        content_key=content.key,
        content_version=content.version,
        draft=draft,
        failure_reason=failure_reason,
    )
