"""身份匹配成功之后的**写侧建档服务合同**（纯逻辑，无 I/O）。

回答一件事：正式 ``OnboardingRunner`` 在「身份唯一、花名册唯一、银河权限有效」
之后，用什么调用面把这个人写进 ``app_user``。判定规则不在这里：定位与建档前提
在 first_contact.py，银河账号匹配在 account_match.py。三态结果：``CREATED``
（新建）、``ALREADY_PROVISIONED``（幂等返回、与 ``CREATED`` 等价地继续）、
``REJECTED``（约束防线拒绝，库里零行残留，按 ``rejection`` 分流：
``INCOMPLETE_IDENTITY``/``DELEGATED_SUBJECT`` 是业务失败，``STORAGE_INTEGRITY``
是存储侧故障要走内部故障出口）。

`OnboardingRunner.start` 的合同要求按 `event_id` / `open_id` 幂等，而
重复建档因此**返回已存在、不报错**：不回退状态、不触碰权限字段，但按新快照
刷新身份与花名册字段；"已存在"不等于"现在还该被开通"，仍须复核当前状态。
**花名册字段存原值**：工号/邮箱不做归一，银河匹配结果对输入没有贡献。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from lingxi.core.identity.first_contact import IdentityRecordDraft

# 身份六字段。顺序即报错时的字段名输出顺序，用元组而不是集合，输出与
# `PYTHONHASHSEED` 无关。这张表必须与 `migrations/008_create_app_user.sql` 的
# 「全有或全无」CHECK 逐字段一致——它不是那条约束的替代品，见 `missing_identity_fields`。
IDENTITY_REQUIRED_FIELDS: tuple[str, ...] = (
    "feishu_open_id",
    "feishu_user_id",
    "feishu_union_id",
    "display_name",
    "department",
    "tenant_key",
)

# 花名册存档字段。**只有两个**：工号（匹配银河的主键）与邮箱（辅键）。
# 姓名的存档来自身份链的 `display_name`，不从花名册行取。
ROSTER_ARCHIVE_FIELDS: tuple[str, ...] = ("employee_no", "email")

# PostgreSQL SQLSTATE。写在这里而不是在 adapters 里散落字面量：分类规则要能在没有
# 数据库的机器上被证伪（`core/` 纯逻辑），adapters 只负责把异常的 sqlstate 递进来。
SQLSTATE_CHECK_VIOLATION = "23514"
SQLSTATE_RAISE_EXCEPTION = "P0001"

# `migrations/008` 里专用主体触发器抛出的原文。`app_user` 写路径上今天只有这一条
# `RAISE EXCEPTION`，但「今天只有一条」不是可以省掉判断的理由：不带标记就把所有
# P0001 当成专用主体，将来任何新触发器的拒绝都会被误报成「这是专用授权账号」。
# 该常量与两条迁移血统的一致性由 `tests/test_identity_provisioning_contract.py` 守。
DELEGATED_SUBJECT_REJECTION_MARKER = "专用授权账号不能被建成用户记录"


@dataclass(frozen=True)
class UserProvisioningStatus:
    """建档之后回读的**准入判据**：这个人此刻还该不该被继续开通。

    住在这里而不是编排层，是因为它是上面「`already_provisioned` 不等于还该开通」那一段
    的另一半：本合同说"写侧不做这个判断"，这个形状就是它把判断交出去时用的信封。放进
    编排层会让 `adapters/postgres_identity.py` 为了实现读回而 import 整条权限链——一个
    只想写 `app_user` 的进程不该因此把银河聚合、发布行与就绪状态机拖进自己的依赖闭包。

    只有三个字段，且**都不是身份资料**：账号状态、开通状态、权限版本。
    """

    account_state: str
    provisioning_state: str
    permission_version: int


class ProvisioningOutcome(str, Enum):
    """一次建档调用的结果。三态，不合并。"""

    CREATED = "created"
    ALREADY_PROVISIONED = "already_provisioned"
    REJECTED = "rejected"


class ProvisioningRejection(str, Enum):
    """建档被拒的原因；互不合并，语义见模块文档的表。"""

    INCOMPLETE_IDENTITY = "incomplete_identity"
    DELEGATED_SUBJECT = "delegated_subject"
    STORAGE_INTEGRITY = "storage_integrity"

    @property
    def is_storage_fault(self) -> bool:
        """这条拒绝是**存储侧故障**而不是「这个人不该建档」的业务结论。

        runner 据此分流：``True`` 走内部故障码 ``LX-ONBOARD-001``，``False`` 走
        冻结的确定性失败终态。把两者合并会让一次「库把工号吞了」显示成「你没有银河
        权限」，把用户引到银河去申请一个他其实已经有的权限。
        """
        return self is ProvisioningRejection.STORAGE_INTEGRITY


def _archive_text(value: object) -> str | None:
    """花名册存档值的归一：只去首尾空白，**不小写、不截断、不补零**。

    空白与缺失都归一为 ``None`` 而不是空串：空串会被写进库，而库里留空串会让
    「有没有存档过」这件事无法回读。与数据库口径不完全等价：``str.strip()``
    剥掉全部 Unicode 空白，是数据库 ``BTRIM(x)``（默认只剥半角空格）的超集，
    这个方向是失败关闭的——判空更严只会让写侧更早拒绝，不会放行数据库认为
    有值的字段。
    """
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


def missing_identity_fields(draft: IdentityRecordDraft) -> tuple[str, ...]:
    """按数据库的空白语义列出为空的身份字段名，口径差异见 :func:`_archive_text`。

    **这不是那条 CHECK 的替代品**，是它的翻译器：写侧路径照常把语句发给数据库，
    由数据库拒绝；本函数只在拒绝之后把「为什么被拒」翻译成 runner 能分辨的字段名，
    返回的是字段名，从不返回字段值。唯一的例外见 :meth:`ProvisioningRequest.
    blocking_gap`：``feishu_open_id`` 为空时数据库故意不拒绝，那一格只能由写侧
    自己守。
    """
    return tuple(
        field
        for field in IDENTITY_REQUIRED_FIELDS
        if _archive_text(getattr(draft, field, None)) is None
    )


def classify_write_failure(
    *,
    sqlstate: str | None,
    message: str,
    missing_fields: tuple[str, ...] = (),
) -> ProvisioningRejection | None:
    """把数据库的拒绝翻译成建档原因；**不认识的一律返回 `None`**。

    返回 `None` 的约定是「调用方必须把原异常原样抛出」，不是「当作成功」也不是
    「归到某个兜底原因」：一条我们没预料到的约束拒绝如果被归成
    `INCOMPLETE_IDENTITY`，用户会得到一个确定性的「无可用银河权限」，而真实原因
    再也没人看得见。
    """
    if sqlstate == SQLSTATE_RAISE_EXCEPTION and DELEGATED_SUBJECT_REJECTION_MARKER in message:
        return ProvisioningRejection.DELEGATED_SUBJECT
    if sqlstate == SQLSTATE_CHECK_VIOLATION and missing_fields:
        return ProvisioningRejection.INCOMPLETE_IDENTITY
    return None


@dataclass(frozen=True)
class ProvisioningRequest:
    """runner 交给建档服务的最小充分输入。

    只有两部分：判定层已经确认过的身份草稿，和花名册**原值**的工号 / 邮箱。
    没有第三部分——尤其没有银河匹配结果：`AccountMatch` 决定「要不要建档」，
    但它的任何字段都不进 `app_user`（权限字段见模块文档最后一段）。
    """

    identity: IdentityRecordDraft
    employee_no: str | None = None
    email: str | None = None

    @classmethod
    def from_roster_row(
        cls, identity: IdentityRecordDraft, row: Mapping[str, object] | None = None
    ) -> ProvisioningRequest:
        """从判定层草稿 + 一行花名册记录组装请求。

        `row` 接受 `adapters.feishu_roster_bitable.RosterRow`（它实现了 `get`）或任何
        同键名映射；`None` 表示这次没有花名册行（工号 / 邮箱留空，建档不以它们为前提）。
        取的是**原值**，理由见模块文档「花名册字段存的是花名册原值」。
        """
        if row is None:
            return cls(identity=identity)
        return cls(
            identity=identity,
            employee_no=_archive_text(row.get("employee_no")),
            email=_archive_text(row.get("email")),
        )

    @property
    def missing_identity_fields(self) -> tuple[str, ...]:
        """本请求的身份草稿里，哪些必填字段为空。"""
        return missing_identity_fields(self.identity)

    @property
    def blocking_gap(self) -> tuple[str, ...]:
        """写侧必须**自己**拦下的那一格：`feishu_open_id` 为空。

        数据库的「全有或全无」CHECK 对六字段全空是放行的（那是账号删除完成后的合法
        形态），而 `ON CONFLICT (feishu_open_id)` 对 `NULL` 也不去重。所以一个空
        `open_id` 的请求既不会被约束拒绝、又永远不幂等：它会一次一行地堆出无法回读的
        垃圾档案。这一格不是重复数据库的判断，是补上数据库故意不管的那一格。

        其余字段的残缺**照常交给数据库拒绝**，写侧不抢在前面短路——那条 CHECK 是
        「任何代码路径都绕不过去」的那一道，只有真的走一遍才证明得了它还在。
        """
        if _archive_text(self.identity.feishu_open_id) is None:
            return self.missing_identity_fields
        return ()

    def to_draft(self) -> IdentityRecordDraft:
        """合成真正写进 `app_user` 的草稿：身份字段来自判定层，花名册字段来自本请求。

        判定层的草稿从不携带工号 / 邮箱（`decide_first_contact` 恒不填）。若调用方
        自行构造了带花名册字段的草稿却没有同时填进请求，这里**直接失败**而不是静默
        丢值：工号是匹配银河的主键，静默丢掉它等于把这个人退化成纯邮箱匹配。
        """
        for field in ROSTER_ARCHIVE_FIELDS:
            if getattr(self.identity, field) is not None and getattr(self, field) is None:
                raise ValueError(f"建档请求的花名册字段 {field} 只在草稿上有值，会被静默丢弃")
        return dataclasses.replace(self.identity, employee_no=self.employee_no, email=self.email)


@dataclass(frozen=True)
class ProvisioningResult:
    """一次建档调用的返回值。

    不变式由 `__post_init__` 强制，而不是靠调用方自觉：成功一定带 `app_user_id`，
    拒绝一定带原因且**一定不带** `app_user_id`。「拒绝了但顺手给个用户标识」在类型
    层面就构造不出来——那种返回值会让 runner 拿着一个并不存在的档案往下走。
    """

    outcome: ProvisioningOutcome
    app_user_id: str | None = None
    rejection: ProvisioningRejection | None = None
    # 被拒时为空的身份字段名，按 :data:`IDENTITY_REQUIRED_FIELDS` 的固定次序。
    # **只有字段名，没有字段值**（与 `roster_audit.PersonDiff` 同一条纪律）。
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验成功/拒绝两种状态各自的字段形状不变量。"""
        if self.outcome is ProvisioningOutcome.REJECTED:
            if self.rejection is None:
                raise ValueError("建档拒绝必须带可分辨的原因")
            if self.app_user_id is not None:
                raise ValueError("建档拒绝不得返回用户标识：拒绝时库里零行残留")
        else:
            if self.rejection is not None or self.missing_fields:
                raise ValueError("建档成功不得同时携带拒绝原因")
            if not self.app_user_id:
                raise ValueError("建档成功必须返回用户标识")

    @property
    def provisioned(self) -> bool:
        """建档这一步是否已经成立（新建或早已存在）。runner 据此继续后续步骤。"""
        return self.outcome is not ProvisioningOutcome.REJECTED

    @classmethod
    def created(cls, app_user_id: str) -> ProvisioningResult:
        """构造一次「新建成功」的结果。"""
        return cls(ProvisioningOutcome.CREATED, app_user_id)

    @classmethod
    def already_provisioned(cls, app_user_id: str) -> ProvisioningResult:
        """构造一次「幂等返回已有档案」的结果。"""
        return cls(ProvisioningOutcome.ALREADY_PROVISIONED, app_user_id)

    @classmethod
    def rejected(
        cls, rejection: ProvisioningRejection, *, missing_fields: tuple[str, ...] = ()
    ) -> ProvisioningResult:
        """构造一次「建档被拒」的结果。"""
        return cls(ProvisioningOutcome.REJECTED, None, rejection, missing_fields)


class IdentityProvisioning(Protocol):
    """建档服务的注入口：runner 只依赖这个签名，真实实现住在 ``adapters/``。

    事务边界：一次 :meth:`provision` 在一个数据库事务内完成 ``app_user`` 的
    完整写入并提交（任何拒绝整条回滚，不留半行）；不承诺跨系统原子性，用户
    环境创建、权限发布、MCP 同步确认、飞书回复都在本合同之外。**不接收调用方
    事务对象**（与 ``enqueue_publish``/``audit.record`` 的"必须传 tx"相反）：
    建档是编排的第一步，把它挂在调用方事务里会把数据库连接和行锁按分钟级
    占住，且"建档已经成功"在崩溃后不可判定，而幂等恰恰依赖它已经落地。
    """

    def provision(self, request: ProvisioningRequest) -> ProvisioningResult:
        """建档；成功（新建或已存在）返回带用户标识的结果，否则返回拒绝原因。"""
        ...
