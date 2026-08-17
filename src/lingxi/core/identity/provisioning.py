"""身份匹配成功之后的**写侧建档服务合同**（纯逻辑，无 I/O）。

本模块回答一件事：正式 `OnboardingRunner`（[Issue #65](https://github.com/Moshuiwang/lingxi/issues/65)
的编排层，实现属 Epic D）在「身份唯一、花名册唯一、银河权限有效」之后，**用什么调用面
把这个人写进 `app_user`**，以及它能从返回值里分辨出什么。判定规则本身不在这里：
定位与建档前提在 :mod:`lingxi.core.identity.first_contact`，银河账号匹配在
:mod:`lingxi.core.permission.account_match`，本模块只负责「已经确定要建档」之后的写入面。

实现住在 :class:`lingxi.adapters.postgres_identity.PostgresAppUserStore`；这里是
`core/`，因此只有数据形状、原因分类和 :class:`IdentityProvisioning` 注入口。

## 三个结果与三个拒绝原因

| 结果 | 含义 | runner 应当怎么读 |
|---|---|---|
| `CREATED` | 本次调用新建了 `app_user` 行 | 继续后续开通步骤 |
| `ALREADY_PROVISIONED` | 同一 `feishu_open_id` 已有档，本次幂等返回同一条 | 与 `CREATED` 等价地继续，不得当成失败 |
| `REJECTED` | 约束防线拒绝，库里**零行残留** | 按 `rejection` 分流，见下表 |

| `rejection` | 触发点 | 属于 |
|---|---|---|
| `INCOMPLETE_IDENTITY` | 身份六字段「全有或全无」CHECK（`migrations/008`）拒绝，或请求本身没有 `feishu_open_id` | 确定性业务失败：走 PR #105 冻结的「无可用银河权限」终态 |
| `DELEGATED_SUBJECT` | `app_user_no_delegated_subject` 触发器拒绝（专用授权账号不建档，`V-身份-02`） | 确定性业务失败：走既有「专用主体忽略」出口，不回普通员工路径 |
| `STORAGE_INTEGRITY` | 写入-回读不一致（工号 / 邮箱被静默丢弃或改写，`V-开通-15`） | **存储侧故障，不是业务结论**：走 `LX-ONBOARD-001` 内部故障出口 |

把 `STORAGE_INTEGRITY` 混进前两类，用户会看到「没有银河权限」而事实是我们的库把工号
吞了——这是一条会误导用户去银河申请的错误结论，因此
:attr:`ProvisioningRejection.is_storage_fault` 把这条分界写成可判定的属性，而不是靠
runner 记得区分。

## 幂等：同一 `open_id` 重复建档返回「已存在」，不报错

`OnboardingRunner.start` 的合同要求按 `event_id` / `open_id` 幂等，而
`core.conversation.onboarding_recovery` 的对账扫描会把「已认领、没确认交接」的孤儿
事件**再交接一次**，崩溃点还可能落在「编排已经跑了一半」之后（见
`core/conversation/ports.py` 的 `OnboardingRunner` 文档）。如果重复建档报错，重入路径
会把一次**已经成功**的建档判成内部故障：用户拿到 `LX-ONBOARD-001`，库里其实已经有档，
而且没有任何东西会再来收拾它——正是 Issue #65 轻审 P2-2 要避免的悬空形状。

因此幂等语义是「返回已存在」，并且：

- 不回退 `provisioning_state`：已经推进到发布或同步中的用户不会因为重入被打回 `matching`；
- 不触碰 `permission_record_id` / `permission_version`：匹配确认前不占位、确认后不被重入抹掉（`V-开通-01`）；
- 会按新快照刷新身份资料与花名册字段（沿用既有 `record_identity` 语义），因为重入时
  runner 手里的资料只会更新、不会更旧。

## 花名册字段存的是**花名册原值**，不是匹配用的归一值

`app_user.employee_no` / `email` 落库的用途是[数据库设计第三节](../../../../docs/技术设计/数据库设计.md)
说的「匹配时刻的存档」，与姓名一起构成 [Issue #52](https://github.com/Moshuiwang/lingxi/issues/52)
每日日报的比对基线，而 :func:`lingxi.core.identity.roster_audit.compare_roster` 做的是
**去空白后的精确字符串比较**。

`AccountMatch.matched_email` 是 `normalize_email` 小写归一之后的比较值（见
`core/permission/account_match.py`）。把它当存档写进去，任何邮箱含大写字母的用户都会
**天天**出现在管理群日报里，而资料其实一个字都没变。所以 :meth:`ProvisioningRequest.from_roster_row`
只从花名册行取原值（仅去首尾空白），银河匹配结果对本合同的输入**没有贡献**。

同理，请求里没有、也不会有任何权限字段：`permission_record_id` 在
:class:`lingxi.core.identity.first_contact.IdentityRecordDraft` 上是恒为 `None` 的常量字段，
「先占位再回填」在类型上就做不到（`V-开通-01`）。
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

        runner 据此分流：`True` 走内部故障码 `LX-ONBOARD-001`，`False` 走 PR #105
        冻结的确定性失败终态。把两者合并会让一次「库把工号吞了」显示成「你没有银河
        权限」，把用户引到银河去申请一个他其实已经有的权限。
        """

        return self is ProvisioningRejection.STORAGE_INTEGRITY


def _archive_text(value: object) -> str | None:
    """花名册存档值的归一：只去首尾空白，**不小写、不截断、不补零**。

    空白与缺失都归一为 `None` 而不是空串：空串会被写进库，而
    `roster_audit.compare_roster` 把「存档为空」读作「没有基线、不比对」，
    两者在日报里是同一个结论，但库里留空串会让「有没有存档过」这件事无法回读。
    """

    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


def missing_identity_fields(draft: IdentityRecordDraft) -> tuple[str, ...]:
    """按 `migrations/008` 的空白口径（`NULLIF(BTRIM(x), '')`）列出为空的身份字段名。

    **这不是那条 CHECK 的替代品**，是它的翻译器：写侧路径照常把语句发给数据库，
    由数据库拒绝；本函数只在拒绝之后把「为什么被拒」翻译成 runner 能分辨的字段名。
    因此它返回的是**字段名**，从不返回字段值——诊断信息不搬运身份原值。

    唯一的例外见 :meth:`ProvisioningRequest.blocking_gap`：`feishu_open_id` 为空时
    数据库**故意不拒绝**（六字段全空是账号删除完成后的合法形态），那一格只能由写侧
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
    ) -> "ProvisioningRequest":
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
        return dataclasses.replace(
            self.identity, employee_no=self.employee_no, email=self.email
        )


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
    def created(cls, app_user_id: str) -> "ProvisioningResult":
        return cls(ProvisioningOutcome.CREATED, app_user_id)

    @classmethod
    def already_provisioned(cls, app_user_id: str) -> "ProvisioningResult":
        return cls(ProvisioningOutcome.ALREADY_PROVISIONED, app_user_id)

    @classmethod
    def rejected(
        cls, rejection: ProvisioningRejection, *, missing_fields: tuple[str, ...] = ()
    ) -> "ProvisioningResult":
        return cls(
            ProvisioningOutcome.REJECTED, None, rejection, missing_fields
        )


class IdentityProvisioning(Protocol):
    """建档服务的注入口：runner 只依赖这个签名，真实实现住在 `adapters/`。

    **事务边界（本合同承诺什么、不承诺什么）**

    - 承诺：一次 :meth:`provision` 在**一个数据库事务**内完成 `app_user` 的完整写入；
      任何拒绝（CHECK、触发器、写入-回读不一致）都整条回滚，库里不留半行，
      也不破坏该 `open_id` 已有的档案。
    - 承诺：返回时事务**已提交**。建档结果对后续步骤、对账扫描与 #52 日报立即可见。
    - 不承诺：跨系统原子性。用户环境创建、权限发布 outbox、MCP 同步确认、飞书回复都在
      本合同之外，它们的补偿与重入属 `OnboardingRunner`（Issue #65 / Epic D）。
    - **不接收调用方事务对象**，与 `enqueue_publish` / `audit.record` 的「必须传 tx」
      刚好相反（[接口设计八](../../../../docs/技术设计/接口设计.md#八领域服务接口)）：
      那两个要的是「审计与状态变更同事务」，而建档是编排的第一步，后面全是跨系统的
      长动作。把建档挂在调用方事务里有两个后果——一次开通会把数据库连接和行锁按分钟级
      占住；更要紧的是「建档已经成功」在崩溃后不可判定，重入既读不到它、又要重新建，
      而幂等恰恰依赖它已经落地。
    """

    def provision(self, request: ProvisioningRequest) -> ProvisioningResult: ...
