"""权限发布意图的消费：写入当前权限多维表格并**逐字段读回核对**（纯编排）。

发布意图怎么产生见 :mod:`lingxi.adapters.postgres_permission_publish`，一行长什么样
见 :mod:`lingxi.core.permission.publish_row`。本模块只回答一句话：**拿到一条发布
意图之后做什么，以及做完之后这条意图算成功还是失败**。

七个互不合并的结果：``published``（唯一可当作已发布的终态）、``superseded``（版本已被
取代，零外部调用）、``conflict``（口径冲突或命中多行）、``invalid``（快照本身不可用）、
``mismatch``/``rejected``/``uncertain``（读回不一致、外部明确拒绝、结果不明，均可
重试，只有"完整返回+业务错误码非0"才算 ``rejected``）；本通道的"结果不明"可以自动
重试（不像投递），因为每次发布前先查后写。
并发边界：同一用户同一时刻只有一条发布在途；旧版本不覆盖新版本；一轮最多认领一次；
崩溃留下的行由 ``reclaim_stale`` 放回 ``pending``，重入安全。外部表格与 outbox 都以
Protocol 注入，本模块不发送告警，断言可在无网络无数据库时跑完。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, NamedTuple, Protocol

from lingxi.core.permission.publish_row import (
    TOKEN_CIPHER_FIELD,
    PublishRow,
    compare_readback,
    readback_text,
)

logger = logging.getLogger(__name__)

#: 同一条发布意图最多尝试多少次。到顶之后转 ``failed`` 等人来看，而不是无限重试。
#: 取 5 是与 outbox 每轮消费节奏配套的经验值，靠 :meth:`PermissionPublishExecutor.
#: run_once` 的本轮排除兑现——少了本轮排除，一条失败回 pending 的意图会在同一轮内
#: 被立刻重新认领，"重试"就不会跨过一次调度间隔。
DEFAULT_MAX_ATTEMPTS = 5

# outbox 的状态取值，与迁移 ``0064`` 的 CHECK 逐字对应。写在这里而不是散落字面量：
# ``core`` 不 import 适配器，但"这条意图接下来是什么状态"是判定，必须能在纯单测里证伪。
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"


class PublishOutcome(Enum):
    """一次发布尝试的结果。七态互不合并，语义见模块文档。"""

    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    INVALID = "invalid"
    MISMATCH = "mismatch"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class PublishFailureKind(Enum):
    """失败的两类语义，纪律与 ``adapters/feishu_roster_bitable.RosterFailureKind`` 相同。

    区分是给告警与排障用的：明确失败要人去改配置或权限，结果不明通常下一轮就好了。
    把结果不明说成明确失败，会让一次网络抖动被当成"发布表权限被回收"；反过来则会让真正
    被拒绝的写入每天安静地重试下去。
    """

    DEFINITE = "definite"
    INDETERMINATE = "indeterminate"


class PermissionTableError(RuntimeError):
    """发布表调用失败。``code`` 供程序判断，消息里不含凭据、Base 标识或人员资料。

    定义在 ``core`` 而不是适配器里：执行器要按 ``definite`` 分流，而 ``core`` 不能
    import ``adapters``。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        """记录失败分类码；``definite`` 未显式给出时按 ``feishu_code_`` 前缀猜测。"""
        super().__init__(f"权限发布表调用失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


class PermissionDecisionTransientFailureError(RuntimeError):
    """一次权限决定命中数据库瞬时故障（死锁、锁等待超时等操作性错误）。

    事务已整体回滚，``app_user.permission_version`` 与 ``publish_outbox`` 均未变化，
    调用方可以安全重试。定义在这个纯类型模块而不是 adapters 层，是为了让调用方在
    不引入 psycopg 依赖链的情况下拿到这个类型去写 ``except``；``classification``
    只是抛出方 psycopg 异常的类名，仅用于审计记录，不参与控制流判断。
    """

    def __init__(self, classification: str) -> None:
        """记录抛出方 psycopg 异常的类名，仅用于审计。"""
        super().__init__(f"权限决定命中数据库瞬时故障，事务已回滚，可重试：{classification}")
        self.classification = classification


#: ``app_user.account_state`` 里**唯一**允许被排出非空授权的取值。
#:
#: 刻意写成正向白名单常量，不是拒绝列表：数据库 CHECK 今天有四个取值，两种写法逐行
#: 等价，但将来新增第五个状态时，拒绝列表会静默放行它，白名单会默认拒绝。
ACCOUNT_STATE_ENABLED = "enabled"


class PermissionGrantBlockedByAccountStateError(RuntimeError):
    """一次**需要账号有效**的权限决定，在落决定的行锁里发现账号已不是 ``enabled``。

    **它不是故障，是正确结果**：事务整体回滚，管理员的「停用」承诺在这里被兑现——
    任何一条会把非空授权排给已停用（或删除中/已删除）账号的路径都必须在这里停下。
    三个调用方各自把它翻译成自己的用户可见收口，不得静默吞掉：每日批授权记专属原因码
    且不计入失败数（被挡是正确结果）；定向重算记可分辨的跳过码；首次开通复用既有的
    停用终态。``account_state`` 只是四个固定字面量之一，不是人员资料，可以进审计。
    """

    def __init__(self, account_state: str) -> None:
        """记录导致拒绝的账号状态字面量。"""
        super().__init__(f"账号状态不允许排出非空授权，事务已整体回滚：{account_state}")
        self.account_state = account_state


class ExistingPermissionRow(NamedTuple):
    """发布表里已经存在的一行：外部记录标识 + 原始字段。

    ``fields`` 保持**原始形态**（可能是字符串、数字或对象），归一交给
    :func:`lingxi.core.permission.publish_row.readback_text`——判定层只认它一份口径。
    """

    record_id: str
    fields: Mapping[str, Any]

    @property
    def record_key(self) -> str:
        """这一行当前的 ``record_key``（归一口径同 :func:`readback_text`）。"""
        return readback_text(self.fields.get("record_key"))

    @property
    def email(self) -> str:
        """这一行当前的 ``email``（归一口径同 :func:`readback_text`）。"""
        return readback_text(self.fields.get("email"))

    @property
    def token_cipher(self) -> str:
        """这一行当前的令牌密文（空串 = 那一列是空的）。**纯空白等同于空**。

        归一走同一个 :func:`readback_text` 再 ``strip()``——一个 ``"   "`` 会让裸真值
        判断认为"密文还在"，于是收敛成发布完成，而那一行对问数 MCP 一样无效（合法
        密文恒为 88 个 base64 字符，绝不含空白）。
        """
        return readback_text(self.fields.get("token_cipher")).strip()

    def matches_key(self, record_key: str) -> bool:
        """这一行的 ``record_key`` 是不是我们要写的那一个（**大小写不敏感**）。

        我们的 ``record_key`` 是规范化邮箱（小写），外部表格里的既有值可能保留了原始
        大小写；按大小写不敏感比较能正确匹配到同一个人，且更新时写入的是规范化后的
        值，同一口径下的大小写因此收敛。这里只核 ``record_key``，不核 ``email``——
        ``record_key`` 才是消费方的主键，``email`` 是它的普通列。
        """
        return self.record_key.strip().casefold() == record_key.strip().casefold()

    @property
    def permissions(self) -> str:
        """这一行当前的 ``permissions`` 单元格文本（归一口径同 :func:`readback_text`）。"""
        return readback_text(self.fields.get("permissions"))

    def content_fields(self, row: PublishRow) -> dict[str, str]:
        """按 ``row.content_fields`` 的键集读回本行对应值，归一走同一个 :func:`readback_text`。"""
        return {name: readback_text(self.fields.get(name)) for name in row.content_fields}

    def content_matches(self, row: PublishRow) -> bool:
        """内容是否与待写行**逐字段相同**（不看 ``updated_at``）。"""
        return self.content_fields(row) == row.content_fields


class PermissionTableTransport(Protocol):
    """当前权限多维表格的可注入写读回面。

    四个方法都可能抛 :class:`PermissionTableError`；其余异常一律原样上抛。
    实现见 ``adapters/feishu_permission_bitable.py``。
    """

    def find_rows(self, *, record_key: str, email: str) -> Sequence[ExistingPermissionRow]:
        """返回 ``record_key`` **或** ``email`` 命中的全部行。

        两个键一次查完：发布执行器要同时回答"该更新哪一行"和"这个人是不是已经以别的
        ``record_key`` 口径存在了"，分两次查会让第二个问题变成一次可选的额外调用，
        而可选的安全检查早晚会被跳过。
        """
        ...

    def create_row(self, fields: Mapping[str, str]) -> str:
        """新建一行，返回外部记录标识。"""
        ...

    def update_row(self, record_id: str, fields: Mapping[str, str]) -> None:
        """按外部记录标识更新给定字段；**未列出的字段不得被改动**。"""
        ...

    def read_row(self, record_id: str) -> Mapping[str, Any]:
        """按外部记录标识读回整行原始字段，供逐字段核对。"""
        ...


@dataclass(frozen=True)
class ClaimedPublish:
    """一条已经被认领（``publishing``）的发布意图。

    ``current_permission_version`` 是**认领那一刻**该用户在数据库里的权限版本，与
    outbox 行一起取回：旧版本不覆盖新版本这条规则因此是纯判定，可以在没有数据库的
    机器上证伪。
    """

    outbox_id: str
    user_id: str
    permission_version: int
    payload: Mapping[str, Any]
    attempts: int = 1
    current_permission_version: int | None = None
    #: 这条意图**自己建过**的那一行（首次认领、以及从未成功创建时都是 ``None``）。
    #: 回答别处答不出的问题："这一行是不是我们建的"——**不是**
    #: ``publish_outbox.external_record_id``（那一列是审计语义，既有行更新失败也会
    #: 写它，拿它当出身用会误伤既有行）。出身只由 ``create_row`` 明确返回记录标识
    #: 这一种事实设置。
    created_record_id: str | None = None


@dataclass(frozen=True)
class PublishAttempt:
    """一次发布尝试的完整结果，可直接进审计。

    **不含任何人员资料值**：只有内部标识、外部记录标识、结果分类、错误码和不一致的
    **字段名**（纪律同 ``core/identity/roster_audit.PersonDiff``）。
    """

    outcome: PublishOutcome
    outbox_id: str
    user_id: str
    permission_version: int
    attempts: int = 1
    # "create" / "update" / "unchanged" / "none"：这次尝试对外部表格做了哪种动作
    # （``unchanged``＝既有行内容逐字段相同，零外部写入）。
    action: str = "none"
    external_record_id: str | None = None
    mismatch_fields: tuple[str, ...] = ()
    error_code: str | None = None
    failure_kind: PublishFailureKind | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        """校验 published/mismatch 两种结论各自的字段形状不变量。"""
        if self.outcome is PublishOutcome.PUBLISHED:
            if self.mismatch_fields:
                raise ValueError("逐字段读回不一致时不得判为发布完成")
            if not self.external_record_id:
                raise ValueError("发布完成必须带外部记录标识")
        if self.outcome is PublishOutcome.MISMATCH and not self.mismatch_fields:
            raise ValueError("读回不一致必须列出不一致的字段名")

    @property
    def published(self) -> bool:
        """这一版权限是否已经**被证明**写进了发布表。下游唯一可以据以继续的信号。"""
        return self.outcome is PublishOutcome.PUBLISHED

    @property
    def retryable(self) -> bool:
        """再试一次**有可能**成功。

        ``conflict`` / ``invalid`` 不在其中：前者要人先决定 ``record_key`` 口径，后者
        说明快照本身坏了，重试只会以同样的方式再失败一次。
        """
        return self.outcome in (
            PublishOutcome.MISMATCH,
            PublishOutcome.REJECTED,
            PublishOutcome.UNCERTAIN,
        )

    @property
    def needs_alert(self) -> bool:
        """要不要惊动人。``published`` 与 ``superseded`` 都是正常收敛，不告警。"""
        return self.outcome not in (PublishOutcome.PUBLISHED, PublishOutcome.SUPERSEDED)

    @property
    def alert_kind(self) -> str:
        """告警分类字符串，形如 ``permission_publish_mismatch``。"""
        return f"permission_publish_{self.outcome.value}"

    def next_status(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> str:
        """这条意图接下来应当是什么状态。

        可重试且次数没用完 → 回 ``pending`` 等下一轮；其余一律终态。这是纯函数，
        因此"重试到底会不会停"能被断言。"等下一轮"到底多久由
        :meth:`PermissionPublishExecutor.run_once` 的本轮排除保证。
        """
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts 必须是正整数")
        if self.outcome is PublishOutcome.PUBLISHED:
            return STATUS_PUBLISHED
        if self.outcome is PublishOutcome.SUPERSEDED:
            return STATUS_SUPERSEDED
        if self.retryable and self.attempts < max_attempts:
            return STATUS_PENDING
        return STATUS_FAILED

    def audit_facts(self) -> dict[str, Any]:
        """这次尝试可安全写进审计事实的字段。"""
        return {
            "outcome": self.outcome.value,
            "outbox_id": self.outbox_id,
            "user_id": self.user_id,
            "permission_version": self.permission_version,
            "attempts": self.attempts,
            "action": self.action,
            "external_record_id": self.external_record_id,
            "mismatch_fields": list(self.mismatch_fields),
            "error_code": self.error_code,
            "failure_kind": self.failure_kind.value if self.failure_kind is not None else None,
            "detail": self.detail,
        }


def _failure(
    claim: ClaimedPublish,
    error: PermissionTableError,
    *,
    action: str,
    external_record_id: str | None,
) -> PublishAttempt:
    """把一次外部调用失败结算成尝试结果，保留明确失败 / 结果不明的分界。"""
    kind = PublishFailureKind.DEFINITE if error.definite else PublishFailureKind.INDETERMINATE
    outcome = PublishOutcome.REJECTED if error.definite else PublishOutcome.UNCERTAIN
    return PublishAttempt(
        outcome=outcome,
        outbox_id=claim.outbox_id,
        user_id=claim.user_id,
        permission_version=claim.permission_version,
        attempts=claim.attempts,
        action=action,
        external_record_id=external_record_id,
        error_code=error.code,
        failure_kind=kind,
    )


def _conflict(claim: ClaimedPublish, code: str) -> PublishAttempt:
    return PublishAttempt(
        outcome=PublishOutcome.CONFLICT,
        outbox_id=claim.outbox_id,
        user_id=claim.user_id,
        permission_version=claim.permission_version,
        attempts=claim.attempts,
        error_code=code,
        failure_kind=PublishFailureKind.DEFINITE,
    )


def _parse_claim_row(claim: ClaimedPublish) -> PublishRow | PublishAttempt:
    """把 outbox payload 解析成待写行；解析失败直接结算成 ``invalid``。"""
    try:
        return PublishRow.from_fields(claim.payload)
    except (ValueError, TypeError) as error:
        return PublishAttempt(
            outcome=PublishOutcome.INVALID,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            error_code="invalid_payload",
            failure_kind=PublishFailureKind.DEFINITE,
            # 只记异常类型：异常正文里可能带上快照里的字段值。
            detail=type(error).__name__,
        )


def _check_superseded(claim: ClaimedPublish) -> PublishAttempt | None:
    """版本已被取代（或用户已删除）时提前收口，一次外部调用都不发。"""
    current = claim.current_permission_version
    if current is None or current > claim.permission_version:
        return PublishAttempt(
            outcome=PublishOutcome.SUPERSEDED,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            detail="user_missing" if current is None else f"current={current}",
        )
    return None


class _RowLookup(NamedTuple):
    """一次 ``find_rows`` 查找的结果：命中的行 + 由此推出的动作类型。"""

    matches: tuple[ExistingPermissionRow, ...]
    action: str
    record_id: str | None
    existing_cipher: str


def _locate_existing_row(
    claim: ClaimedPublish, row: PublishRow, *, transport: PermissionTableTransport
) -> _RowLookup | PublishAttempt:
    """按 ``record_key``/``email`` 查找既有行，判定冲突与动作类型（新建/更新）。

    同一个人已存在另一种 ``record_key`` 口径、或命中多行时失败关闭（既不更新——会
    改写业务侧的键，也不新建——会造出第二行权限），等人决定口径。
    """
    try:
        matches = tuple(transport.find_rows(record_key=row.record_key, email=row.email))
    except PermissionTableError as error:
        return _failure(claim, error, action="none", external_record_id=None)

    if len(matches) > 1:
        return _conflict(claim, "multiple_rows")
    if matches and not matches[0].matches_key(row.record_key):
        return _conflict(claim, "record_key_mismatch")

    action = "update" if matches else "create"
    record_id = matches[0].record_id if matches else None
    existing_cipher = matches[0].token_cipher if matches else ""
    return _RowLookup(
        matches=matches, action=action, record_id=record_id, existing_cipher=existing_cipher
    )


def _check_cipher_rewrite(
    claim: ClaimedPublish, row: PublishRow, lookup: _RowLookup
) -> PublishAttempt | None:
    """**这一行是我们自己建的，密文却不是我们写进去的那一份**：判 ``mismatch``。

    判据是 ``created_record_id``（出身）而不是 ``external_record_id``（审计）：后者
    在既有行更新失败时也会被写上，用它会把既有行的一次更新重试判成永久冲突；既有
    行的出身永远是 ``None``，因此走不到这里，不会误伤。保证边界刻意不扩大：新权限
    版本的新意图、以及创建结果不明时，出身均为 ``None``，识别不到改写——那两条路径
    上"改写者"与"旧系统合法密文"不可区分，猜错方向会把合法旧记录打成永久失败，
    最终由就绪探针兜底（带错误密文的行永远探不成功，超时转运维）。
    """
    matches = lookup.matches
    if (
        matches
        and lookup.existing_cipher
        and row.token_cipher
        and claim.created_record_id == matches[0].record_id
        and lookup.existing_cipher != row.token_cipher
    ):
        return PublishAttempt(
            outcome=PublishOutcome.MISMATCH,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            action=lookup.action,
            external_record_id=lookup.record_id,
            mismatch_fields=("token_cipher",),
            error_code="readback_mismatch",
            failure_kind=PublishFailureKind.DEFINITE,
        )
    return None


def _select_expected_fields(
    claim: ClaimedPublish, row: PublishRow, lookup: _RowLookup
) -> dict[str, str] | PublishAttempt:
    """按动作与既有密文状态选出待写字段集（`V-权限-11` 的落点）。

    新建（或既有密文为空、需要补上空洞）时写七列；既有密文非空时写六列，那一列
    既不被清空也不被覆盖——它可能是旧系统签发的，我们不知道明文。两边都没有密文
    时抛 ``ValueError``，本函数把它转译成 ``invalid``：静默少写一列的结果是"发布
    成功了，但这个人永远问不了数"，一个我们自己都发现不了的假成功。
    """
    try:
        if not lookup.matches:
            return row.create_fields
        if lookup.existing_cipher:
            return row.fields
        return row.create_fields
    except ValueError as error:
        return PublishAttempt(
            outcome=PublishOutcome.INVALID,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            action=lookup.action,
            external_record_id=lookup.record_id,
            error_code="missing_token_cipher",
            failure_kind=PublishFailureKind.DEFINITE,
            detail=type(error).__name__,
        )


def _check_unchanged(
    claim: ClaimedPublish, row: PublishRow, lookup: _RowLookup
) -> PublishAttempt | None:
    """**不变不回写**：既有行内容与待写行逐字段相同且密文仍在时零外部写入。

    判据用的是 ``find_rows`` 刚读回的这一行，不是 outbox 里的上一版快照：决定层的
    "不变"管的是"要不要排意图"，这里管的是"要不要碰外部表"。密文空洞补写与自建行
    密文改写守卫都不走这条短路，`V-权限-11` 不变。
    """
    if lookup.matches and lookup.existing_cipher and lookup.matches[0].content_matches(row):
        return PublishAttempt(
            outcome=PublishOutcome.PUBLISHED,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            action="unchanged",
            external_record_id=lookup.record_id,
        )
    return None


def _write_and_verify(
    claim: ClaimedPublish,
    lookup: _RowLookup,
    expected: dict[str, str],
    *,
    transport: PermissionTableTransport,
) -> PublishAttempt:
    """写入（新建或更新）并**逐字段读回核对**，核对通过才判 ``published``。

    写入接口返回成功只证明请求被受理，证明不了收下的内容与我们决定发布的是同一份；
    读回按字符串值比对，比对范围与本次实际写出去的字段集同一份。更新路径没有提交
    ``token_cipher``，但仍必须确认那一列不是空——"发布完成"断言的是"这一行现在对
    MCP 有效"，一行被平台清空密文的更新不能悄悄收敛成 ``published``。
    """
    record_id = lookup.record_id
    try:
        if lookup.matches:
            transport.update_row(lookup.matches[0].record_id, expected)
        else:
            record_id = transport.create_row(expected)
        actual = transport.read_row(record_id or "")
    except PermissionTableError as error:
        return _failure(claim, error, action=lookup.action, external_record_id=record_id)

    mismatch = compare_readback(expected, actual)
    if not mismatch and TOKEN_CIPHER_FIELD not in expected:
        # strip() 与 ExistingPermissionRow.token_cipher 同一口径：纯空白不算数。
        if not readback_text(actual.get(TOKEN_CIPHER_FIELD)).strip():
            mismatch = (TOKEN_CIPHER_FIELD,)
    if mismatch:
        return PublishAttempt(
            outcome=PublishOutcome.MISMATCH,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            action=lookup.action,
            external_record_id=record_id,
            mismatch_fields=mismatch,
            error_code="readback_mismatch",
            failure_kind=PublishFailureKind.DEFINITE,
        )
    return PublishAttempt(
        outcome=PublishOutcome.PUBLISHED,
        outbox_id=claim.outbox_id,
        user_id=claim.user_id,
        permission_version=claim.permission_version,
        attempts=claim.attempts,
        action=lookup.action,
        external_record_id=record_id,
    )


def publish_claim(claim: ClaimedPublish, *, transport: PermissionTableTransport) -> PublishAttempt:
    """执行一条发布意图：查找 → 新建或更新 → **逐字段读回核对**。

    判定次序是刻意的：先判版本再动手（旧版本一次外部调用都不发）；先查后写（幂等 +
    防止同一个人建第二行）；字段集按动作与既有密文状态分（见
    :func:`_select_expected_fields`）；写完必须读回（见 :func:`_write_and_verify`）。
    未预期的异常一律不捕获、原样上抛：把它们也吞成"结果不明"会让真正的缺陷伪装成
    外部异常，每一轮安静地重试下去。各步骤的详细论证见对应辅助函数的文档。
    """
    row = _parse_claim_row(claim)
    if isinstance(row, PublishAttempt):
        return row

    superseded = _check_superseded(claim)
    if superseded is not None:
        return superseded

    lookup = _locate_existing_row(claim, row, transport=transport)
    if isinstance(lookup, PublishAttempt):
        return lookup

    rewrite_guard = _check_cipher_rewrite(claim, row, lookup)
    if rewrite_guard is not None:
        return rewrite_guard

    expected = _select_expected_fields(claim, row, lookup)
    if isinstance(expected, PublishAttempt):
        return expected

    unchanged = _check_unchanged(claim, row, lookup)
    if unchanged is not None:
        return unchanged

    return _write_and_verify(claim, lookup, expected, transport=transport)


class PermissionPublishStore(Protocol):
    """发布意图 outbox 的最小消费面（可注入）。

    实现见 ``adapters/postgres_permission_publish.py``。
    """

    def claim_next(self, *, exclude: Sequence[str] = ()) -> ClaimedPublish | None:
        """认领一条待发布意图，排除 ``exclude`` 里的 ``outbox_id``；没有则返回 ``None``。"""
        ...

    def complete(self, attempt: PublishAttempt, *, status: str) -> None:
        """把一次尝试结果落库，并把该条意图收口到 ``status``。"""
        ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class PermissionPublishExecutor:
    """把「认领一条意图 → 发布 → 记账 → 需要时告警」串起来。只编排注入的接口，不做 I/O。

    形状与 ``core/identity/roster_snapshot.RosterSnapshotUpdater`` 一致（编排放
    ``core``，真正的外部调用在注入进来的对象里）。

    **``complete`` 失败不吞**：先留一条审计说明这次记账没成功，再原样上抛。那条意图会
    停在 ``publishing``，由 ``reclaim_stale`` 放回 ``pending`` 后重入——重入安全，因为
    发布本身幂等（先查后写）。吞掉它会让"外部已经写了、库里还是 pending"永久停在那里。
    """

    name = "权限发布"

    def __init__(
        self,
        *,
        store: PermissionPublishStore,
        transport: PermissionTableTransport,
        audit: _AuditSink,
        on_alert: Callable[[PublishAttempt], None] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """装配执行器；``max_attempts`` 必须是正整数，非法值当场拒绝。"""
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts 必须是正整数")
        self._store = store
        self._transport = transport
        self._audit = audit
        self._on_alert = on_alert
        self._max_attempts = max_attempts

    def run_once(
        self, *, limit: int = 50, exclude: Sequence[str] = ()
    ) -> tuple[PublishAttempt, ...]:
        """消费至多 ``limit`` 条待发布意图，返回逐条结果。

        ``limit`` 是**单轮预算**，不是重试上限（重试上限见
        :meth:`PublishAttempt.next_status`）。**本轮认领过的意图本轮不再认领**：认领
        按 ``(created_at, id)`` 取最老一条，失败只改状态不改 ``created_at``，没有本轮
        排除的话同一条意图会在几十毫秒内被重复认领、把重试预算在一轮内烧完。默认空
        元组时本方法自己就是一轮；真实调度职责逐条检查停止信号时用
        ``run_once(limit=1)`` × N，累积清单由调用方传下来。当前部署单副本、发布消费
        是单一写入负责人，进程内作用域因此不构成并发问题。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是正整数")
        attempts: list[PublishAttempt] = []
        # 保序而不是 set：这一串会进 SQL 参数，顺序稳定的语句更容易在日志与用例里比对。
        claimed: list[str] = [str(item) for item in exclude]
        for _ in range(limit):
            claim = self._store.claim_next(exclude=tuple(claimed))
            if claim is None:
                break
            claimed.append(claim.outbox_id)
            attempts.append(self._run_claim(claim))
        return tuple(attempts)

    def _run_claim(self, claim: ClaimedPublish) -> PublishAttempt:
        attempt = publish_claim(claim, transport=self._transport)
        status = attempt.next_status(max_attempts=self._max_attempts)
        try:
            self._store.complete(attempt, status=status)
        except Exception as error:
            self._audit.record(
                "permission_publish.complete_failed",
                # 只记异常类型：异常正文可能带上被写入的字段值。
                error=type(error).__name__,
                status=status,
                **attempt.audit_facts(),
            )
            raise
        self._audit.record(
            f"permission_publish.{attempt.outcome.value}", status=status, **attempt.audit_facts()
        )
        if attempt.needs_alert:
            logger.warning(
                "权限发布未完成 outcome=%s outbox=%s attempts=%s error=%s fields=%s",
                attempt.outcome.value,
                attempt.outbox_id,
                attempt.attempts,
                attempt.error_code,
                ",".join(attempt.mismatch_fields),
            )
            if self._on_alert is not None:
                self._on_alert(attempt)
        return attempt


#: 向后兼容别名：两个异常类已改名以满足异常类命名规则（须以 Error 结尾），跨模块
#: 引用未同步改名前继续可用，全仓统一改名后再清理。
PermissionDecisionTransientFailure = PermissionDecisionTransientFailureError
PermissionGrantBlockedByAccountState = PermissionGrantBlockedByAccountStateError


__all__ = [
    "ACCOUNT_STATE_ENABLED",
    "DEFAULT_MAX_ATTEMPTS",
    "ClaimedPublish",
    "ExistingPermissionRow",
    "PermissionDecisionTransientFailureError",
    "PermissionGrantBlockedByAccountStateError",
    "PermissionPublishExecutor",
    "PermissionPublishStore",
    "PermissionTableError",
    "PermissionTableTransport",
    "PublishAttempt",
    "PublishFailureKind",
    "PublishOutcome",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_PUBLISHED",
    "STATUS_SUPERSEDED",
    "publish_claim",
]
