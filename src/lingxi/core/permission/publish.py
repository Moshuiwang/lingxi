"""权限发布意图的消费：写入当前权限多维表格并**逐字段读回核对**（纯编排）。

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-01 后半段。
发布意图怎么产生、怎么与权限决定同事务落库，见
:mod:`lingxi.adapters.postgres_permission_publish`；一行长什么样、字段怎么序列化，见
:mod:`lingxi.core.permission.publish_row`。本模块只回答一句话：**拿到一条发布意图之后
做什么，以及做完之后这条意图算成功还是失败**。

本模块住在 ``core/``，因此不 import 任何适配器、不发请求、不连数据库：外部表格与 outbox
都以 Protocol 注入（:class:`PermissionTableTransport` / :class:`PermissionPublishStore`），
全部断言可以在没有网络也没有数据库的机器上跑完。

## 六个互不合并的结果

| 结果 | 发生了什么 | outbox 去向 |
|---|---|---|
| ``published`` | 写入成功，且逐字段读回与预期**完全一致** | ``published`` |
| ``superseded`` | 这条意图的权限版本已被更新版本取代 | ``superseded``，**不发生任何外部调用** |
| ``conflict`` | 同一个人已存在 ``record_key`` 口径不同的行，或命中多行 | ``failed``，不重试 |
| ``invalid`` | 内容快照本身不可用（缺字段 / 被人改过库） | ``failed``，不重试 |
| ``mismatch`` | 写入调用成功，但读回有字段对不上 | 重试；attempts 用尽转 ``failed`` |
| ``rejected`` | 外部**明确拒绝**（完整响应 + 业务错误码非 0） | 重试；attempts 用尽转 ``failed`` |
| ``uncertain`` | **结果不明**（传输异常、超时、响应形状不对、缺可回读标识） | 重试；attempts 用尽转 ``failed`` |

**只有 ``published`` 允许把这一版权限当作已发布。** ``mismatch`` 尤其不行：写入接口
返回成功、读回却对不上，说明平台收下的与我们决定发布的不是同一份内容，此时宣告发布完成
等于让下游（MCP 就绪确认、开通成功提示）建立在一份我们从没验证过的权限上。

## 明确失败与结果不明的分界，以及**为什么这里可以重试**

分类纪律沿用 ``adapters/feishu_delivery.py`` 的白名单口径：只有「HTTP/RPC 完整返回 +
业务错误码明确非 0」才算 ``rejected``，其余一切（含"响应成功但缺可回读标识"）都是
``uncertain``；未预期的异常一律不捕获、原样上抛。

但**动作**与投递相反：投递的"结果不明"绝不自动重发（重发会让用户收到第二条消息），
本通道的"结果不明"**可以**自动重试。差别在幂等的载体不同——每次发布前先按
``record_key`` / ``email`` 查一次，命中就更新、不命中才新建，因此"上一次到底写没写成功"
不影响结果：写成功了就更新回同样的值，没写成功就补上。同一条意图重复执行收敛到**同一行、
同一份内容**，不产生第二行、也不产生第二次"交付"。

## 并发边界

- **同一用户同一时刻只有一条发布在途**：由 ``claim_next`` 的实现保证（见
  ``adapters/postgres_permission_publish.py`` 的 ``FOR UPDATE SKIP LOCKED`` +
  「该用户不存在**更早的非终态**兄弟行」条件）。少了这条，两个消费者可以同时拿到同一
  用户的 v1 与 v2，v1 的写入落后到 v2 之后就会把旧权限盖回去——用户侧表现为**已经
  收回的权限被静默恢复**。判据取「更早的非终态」而不是「有没有 ``publishing``」，是
  因为后者在 ``READ COMMITTED`` 下看不见尚未提交的认领；理由全文在该方法的文档。
- **旧版本不覆盖新版本**：认领时一并取回 ``app_user.permission_version``，比它旧的意图
  直接判 ``superseded``，**一次外部调用都不发**。
- **重启安全**：崩溃留下的 ``publishing`` 行由 ``reclaim_stale`` 放回 ``pending``；因为
  发布本身幂等（先查后写），重入不会写出第二行。
- 本 Story **不装配任何常驻消费者**：真实调用方是 Epic D 的 ``OnboardingRunner`` 与每日
  权限刷新职责。当前进程闭包里没有它，与 ``core/identity/provisioning.py`` 同一姿态。

## 告警

本模块**只产出可断言的结果对象**，不发送告警：``on_alert`` 是注入点，形状与
``core/identity/roster_snapshot.RosterSnapshotUpdater`` 一致（回调收到整个
:class:`PublishAttempt`）。接到 ``core/alerting.py`` 的状态机属装配层（Epic D）——
那套状态机当前只认心跳、任务滞留与飞书发送连续失败三类信号，权限发布失败塞进这三类
会让阈值、去重与恢复计时三套语义同时失真，需要先决定它是新增一类还是复用一类。
不注入时只落审计，不静默丢弃。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, NamedTuple, Protocol

from lingxi.core.permission.publish_row import PublishRow, compare_readback, readback_text

logger = logging.getLogger(__name__)

#: 同一条发布意图最多尝试多少次。到顶之后转 ``failed`` 等人来看，而不是无限重试。
#: 取 5 是与 outbox 的每轮消费节奏配套的经验值，不是外部规范；调整它不改变任何语义。
DEFAULT_MAX_ATTEMPTS = 5

# outbox 的状态取值，与迁移 ``0064`` 的 CHECK 逐字对应。写在这里而不是散落字面量：
# ``core`` 不 import 适配器，但"这条意图接下来是什么状态"是判定，必须能在纯单测里证伪。
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"


class PublishOutcome(Enum):
    """一次发布尝试的结果。七态互不合并，语义见模块文档的表。"""

    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    INVALID = "invalid"
    MISMATCH = "mismatch"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class PublishFailureKind(Enum):
    """失败的两类语义，纪律与 ``adapters/feishu_roster_bitable.RosterFailureKind`` 相同。

    区分是给**告警与排障**用的：明确失败要人去改配置或权限，结果不明通常下一轮就好了。
    把结果不明说成明确失败，会让一次网络抖动被当成"发布表权限被回收"；反过来则会让真正
    被拒绝的写入每天安静地重试下去。
    """

    DEFINITE = "definite"
    INDETERMINATE = "indeterminate"


class PermissionTableError(RuntimeError):
    """发布表调用失败。``code`` 供程序判断，消息里不含凭据、Base 标识或人员资料。

    定义在 ``core`` 而不是适配器里：执行器要按 ``definite`` 分流，而 ``core`` 不能
    import ``adapters``。方向与 ``core/identity/roster_snapshot.StoredSnapshotFacts``
    一致——适配器反过来 import 本模块。
    """

    def __init__(self, code: str, *, definite: bool | None = None) -> None:
        super().__init__(f"权限发布表调用失败：{code}")
        self.code = code
        self.definite = definite if definite is not None else code.startswith("feishu_code_")


class ExistingPermissionRow(NamedTuple):
    """发布表里已经存在的一行：外部记录标识 + 原始字段。

    ``fields`` 保持**原始形态**（可能是字符串、数字或对象），归一交给
    :func:`lingxi.core.permission.publish_row.readback_text`——判定层只认它一份口径。
    """

    record_id: str
    fields: Mapping[str, Any]

    @property
    def record_key(self) -> str:
        return readback_text(self.fields.get("record_key"))

    @property
    def email(self) -> str:
        return readback_text(self.fields.get("email"))

    def matches_key(self, record_key: str) -> bool:
        """这一行的 ``record_key`` 是不是我们要写的那一个（**大小写不敏感**）。

        我们的 ``record_key`` 是规范化邮箱（小写），而外部表格里的既有值可能保留了
        原始大小写。按字节严格相等会把「同一个人、同一种口径、只是大小写不同」判成
        口径冲突，于是一个本该更新的行永远走不通；按大小写不敏感比较则会正确地更新
        它——更新时写入的是规范化后的值，同一口径下的大小写因此收敛，不改变这一行
        指向谁。

        **口径登记（二级独立审查 P3-3，知情接受）：这里只核 ``record_key``，不核
        ``email``。** 一行命中之后不再要求它的 ``email`` 也等于我们要写的邮箱——理由
        是 ``record_key`` 才是消费方的主键，``email`` 是它的普通列；两列在既有 26 行
        里同源（2026-08-17 回源核对），但**权威的是键**。若某天出现「``record_key``
        对得上、``email`` 却是另一个人」的行，那属于外部表自身的数据损坏，多加一道
        比对既救不了它，反而会把一次本该成功的更新变成永久 CONFLICT。查找侧仍然按
        ``record_key`` **或** ``email`` 两个键取候选，因此这种行不会被漏掉，只是由
        「多行命中」或「键口径不同」那两条分支去失败关闭。
        """

        return self.record_key.strip().casefold() == record_key.strip().casefold()


class PermissionTableTransport(Protocol):
    """当前权限多维表格的可注入写读回面。

    四个方法都可能抛 :class:`PermissionTableError`；其余异常一律原样上抛。
    实现见 ``adapters/feishu_permission_bitable.py``。
    """

    def find_rows(self, *, record_key: str, email: str) -> Sequence[ExistingPermissionRow]:
        """返回 ``record_key`` **或** ``email`` 命中的全部行。

        两个键一次查完，是因为发布执行器要同时回答两个问题：「该更新哪一行」和
        「这个人是不是已经以别的 ``record_key`` 口径存在了」。分两次查会让第二个问题
        变成一次可选的额外调用，而可选的安全检查早晚会被跳过。
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
    # "create" / "update" / "none"：这次尝试对外部表格做了哪种动作。
    action: str = "none"
    external_record_id: str | None = None
    mismatch_fields: tuple[str, ...] = ()
    error_code: str | None = None
    failure_kind: PublishFailureKind | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
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
        说明快照本身坏了，重试只会以同样的方式再失败一次，还会把真实原因埋进重试噪音里。
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
        因此"重试到底会不会停"能被断言，而不是靠读一遍消费循环去推断。
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


def _failure(claim: ClaimedPublish, error: PermissionTableError, *, action: str,
             external_record_id: str | None) -> PublishAttempt:
    """把一次外部调用失败结算成尝试结果，保留明确失败 / 结果不明的分界。"""

    kind = PublishFailureKind.DEFINITE if error.definite else PublishFailureKind.INDETERMINATE
    outcome = (
        PublishOutcome.REJECTED if error.definite else PublishOutcome.UNCERTAIN
    )
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


def publish_claim(
    claim: ClaimedPublish, *, transport: PermissionTableTransport
) -> PublishAttempt:
    """执行一条发布意图：查找 → 新建或更新 → **逐字段读回核对**。

    判定次序是刻意的：

    1. **先判版本再动手**。旧版本的意图一次外部调用都不发——「不覆盖新权限」必须在
       写之前成立，写完再回滚已经晚了。
    2. **先查后写**。查找同时服务幂等（命中就更新）与安全（同一个人不建第二行）。
    3. **写完必须读回**。写入接口返回成功只证明请求被受理，证明不了收下的内容与我们
       决定发布的是同一份；读回按**字符串值**比对（G-BIT 移交的实现约束，见
       :func:`lingxi.core.permission.publish_row.readback_text`）。

    未预期的异常一律不捕获、原样上抛（纪律同 ``adapters/feishu_delivery.py``）：把它们
    也吞成"结果不明"会让真正的缺陷伪装成外部异常，每一轮安静地重试下去。
    """

    try:
        row = PublishRow.from_fields(claim.payload)
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

    current = claim.current_permission_version
    if current is None or current > claim.permission_version:
        # `None` = 认领时读不到该用户的权限版本（用户已被删除，行随 CASCADE 消失）。
        # 两种情况都不该再写外部表格，也都不是失败——这一版权限已经没有意义了。
        return PublishAttempt(
            outcome=PublishOutcome.SUPERSEDED,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            detail="user_missing" if current is None else f"current={current}",
        )

    try:
        matches = tuple(transport.find_rows(record_key=row.record_key, email=row.email))
    except PermissionTableError as error:
        return _failure(claim, error, action="none", external_record_id=None)

    if len(matches) > 1:
        return _conflict(claim, "multiple_rows")
    if matches and not matches[0].matches_key(row.record_key):
        # 同一个人已经以另一种 record_key 口径存在。既不更新（会改写业务侧的键），
        # 也不新建（会造出同一个人的第二行权限）——失败关闭，等人决定口径。
        return _conflict(claim, "record_key_mismatch")

    action = "update" if matches else "create"
    record_id = matches[0].record_id if matches else None
    try:
        if matches:
            transport.update_row(matches[0].record_id, row.fields)
        else:
            record_id = transport.create_row(row.fields)
        actual = transport.read_row(record_id or "")
    except PermissionTableError as error:
        return _failure(claim, error, action=action, external_record_id=record_id)

    mismatch = compare_readback(row.fields, actual)
    if mismatch:
        return PublishAttempt(
            outcome=PublishOutcome.MISMATCH,
            outbox_id=claim.outbox_id,
            user_id=claim.user_id,
            permission_version=claim.permission_version,
            attempts=claim.attempts,
            action=action,
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
        action=action,
        external_record_id=record_id,
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


class PermissionPublishStore(Protocol):
    """发布意图 outbox 的最小消费面（可注入）。实现见
    ``adapters/postgres_permission_publish.py``。"""

    def claim_next(self) -> ClaimedPublish | None: ...

    def complete(self, attempt: PublishAttempt, *, status: str) -> None: ...


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
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts 必须是正整数")
        self._store = store
        self._transport = transport
        self._audit = audit
        self._on_alert = on_alert
        self._max_attempts = max_attempts

    def run_once(self, *, limit: int = 50) -> tuple[PublishAttempt, ...]:
        """消费至多 ``limit`` 条待发布意图，返回逐条结果。

        ``limit`` 是**单轮预算**，不是重试上限：它挡的是"一轮把整张表刷完"占住外部
        接口配额；重试上限是 :meth:`PublishAttempt.next_status` 里的 ``max_attempts``。
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是正整数")
        attempts: list[PublishAttempt] = []
        for _ in range(limit):
            claim = self._store.claim_next()
            if claim is None:
                break
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


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "ClaimedPublish",
    "ExistingPermissionRow",
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
