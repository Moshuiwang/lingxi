"""花名册持久快照的替换判定与保旧告警事实（Issue #52 / S-B-02）。

产品负责人 2026-08-08 的 **D2 裁定**推翻了此前的「零新表」定案：每日花名册比对需要一份
跨进程重启与常规发布都还在的持久快照，而「源头返回空 / 读取失败 / 超时 / 半轮」一律
**保留上一份有效快照**，绝不覆盖。本模块回答的就是这一句话里的两个判定：

1. **这一轮能不能替换快照**——门槛只有一个：读取结果的 ``status`` 是 ``COMPLETE``
   （等价的只读属性 ``complete_nonempty``）。
2. **不能替换时，管理员该被告知什么**——按原因分成四类互不合并的告警事实，并且区分
   「保留了上一份」与「本来就还没有任何快照」。

**为什么门槛不能写成「rows 非空」**（`V-花名册-41`，PR #208 二级审查钉入的合同条款）：
读取层的 ``INCOMPLETE``（源头自报总数与累计行数对不上，或某个必需列整列取不到值）
**刻意保留 rows**——那些行确实读到了，只是可信度没有。以「rows 非空」为门槛会让这种
「读到了但不可信」的一轮直接顶掉上一份好快照，而这正是保旧要挡的形状；同理，一次
真实的空源（``EMPTY_SOURCE``）在「rows 非空」口径下会被判成"不替换"而看起来对了，
掩盖了判据本身是错的。判据必须是 ``status``，用例见 ``tests/test_roster_snapshot.py``
的否定面。

**为什么区分「保旧」与「还没有任何快照」**（`V-花名册-44`）：两者的管理员动作不同。
有旧快照时，比对照常能跑，只是基线在变旧，要报的是「快照已经 N 秒没更新」；从未有过
快照时，比对根本没有基线，报「保留了上一份」是**假话**——首轮就读失败的部署会被这句
话安抚过去，直到有人发现日报从来没发出来过。

**本模块不 import 任何适配器**（代码框架第二节第 1 条）：读取结果按**结构**取用
（``complete_nonempty`` / ``status.value`` / ``failure`` / ``integrity`` 的计数），
形态由 ``adapters/feishu_roster_bitable.py`` 的 :class:`RosterReadOutcome` 提供，
测试可以用同形状的假对象证伪。四个状态字面量在下方 :data:`_KNOWN_STATUSES` 里显式
登记：读取层将来多一个状态时，这里**响亮失败**而不是默默归到"保旧"——把一个没人分类
过的新状态静默当成"保旧"，等于让快照在无人知晓的情况下停更。

**告警只产出事实，不发送**：接线到告警状态机（``core/alerting.py`` 的 ``AlertingDuty``）
属 S-B-04 的范围。本模块交付的是可断言的告警事实与一个 ``on_alert`` 注入点，
:class:`RosterSnapshotUpdater` 在需要时把它调起来；不注入就只落审计，不静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

_UTC = timezone.utc

# 读取层四态的字面量。与 `adapters/feishu_roster_bitable.RosterReadStatus` 的 `value`
# 一一对应；这里重列一遍是本模块不 import adapters 的代价，也是"新状态必须被显式
# 分类"的落点（见模块说明）。
STATUS_COMPLETE = "complete"
STATUS_EMPTY_SOURCE = "empty_source"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"

_KNOWN_STATUSES = frozenset(
    {STATUS_COMPLETE, STATUS_EMPTY_SOURCE, STATUS_INCOMPLETE, STATUS_FAILED}
)

# 失败两分类的字面量，同样与 `RosterFailureKind` 的 `value` 对应。
FAILURE_DEFINITE = "definite"
FAILURE_INDETERMINATE = "indeterminate"


class SnapshotAction(Enum):
    """本轮对持久快照做了什么。四个取值互不重叠，覆盖全部可能。"""

    # 装入第一份快照：此前库里一份都没有。
    INSTALL = "install"
    # 用本轮结果整体替换已有快照。
    REPLACE = "replace"
    # 本轮不可信，保留已有的那一份。
    KEEP_PREVIOUS = "keep_previous"
    # 本轮不可信，而且此前就没有任何快照——**没有"旧"可保**。
    NO_SNAPSHOT_YET = "no_snapshot_yet"


class SnapshotAlertKind(Enum):
    """保旧告警的原因分类。四类互不合并（`V-花名册-43`）。

    合并任意两类都会让管理员失去可行动的信息：空源要去看花名册是不是被清空或换了
    视图；不完整要去看表结构是不是改了列名；明确失败要去查权限或配置；结果不明通常
    下一轮就好了，频繁出现才需要看网络。
    """

    # 整轮读完、没有任何失败，但零行。不是"全员离职"，是需要人看一眼的异常。
    EMPTY_SOURCE = "empty_source"
    # 整轮读完，但完整性判定不通过：行拿到了，可信度没有。
    INCOMPLETE = "incomplete"
    # 服务端完整返回并明确拒绝：要人去改配置或权限。
    FAILED_DEFINITE = "failed_definite"
    # 传输异常、超时、响应形状不对、游标停滞、翻页超上限：读到多少行是未知的。
    FAILED_INDETERMINATE = "failed_indeterminate"


@dataclass(frozen=True)
class StoredSnapshotFacts:
    """库里那份快照的元信息。**不含任何人员资料值**——它只用来回答"有没有、多旧、多大"。

    定义在 ``core`` 而不是适配器里：判定逻辑要用它，而 ``core`` 不能 import
    ``adapters``；持久化实现（``adapters/postgres_roster_snapshot.py``）反过来
    import 本模块，方向与 ``core/identity/roster_audit.ArchivedIdentity`` 一致。
    """

    snapshot_id: str
    captured_at: datetime
    row_count: int

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            # 时间一律 UTC 存储（接口设计「二、通用约定」）。naive 时间进来会让
            # "快照有多旧"在跨时区部署上算错，而算错的方向恰恰是"看起来更新"。
            raise ValueError("快照读取时间必须带时区")
        object.__setattr__(self, "captured_at", self.captured_at.astimezone(_UTC))
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int):
            raise ValueError("快照行数必须是整数")
        if self.row_count <= 0:
            # 只有 COMPLETE 能成为快照，而 COMPLETE 恒非空；零行快照说明写入路径
            # 绕过了门槛，宁可响亮失败也不要让它冒充一份"有效基线"。
            raise ValueError("快照行数必须为正")


@dataclass(frozen=True)
class SnapshotDecision:
    """一轮判定的完整结果：做什么、为什么、以及可直接进审计的事实。

    **整个对象不含任何花名册字段值**（`V-花名册-46`，与 `V-花名册-33` 同口径）：
    只有动作、状态、告警分类、错误码与计数。
    """

    action: SnapshotAction
    status: str
    alert: SnapshotAlertKind | None = None
    failure_code: str | None = None
    failure_kind: str | None = None
    row_count: int = 0
    pages_read: int = 0
    previous_captured_at: datetime | None = None
    previous_row_count: int | None = None
    # 上一份快照到本轮为止有多旧。没有上一份时为 ``None``，而不是 0——0 会被读成
    # "刚刚更新过"。
    previous_age_seconds: float | None = None

    @property
    def should_replace(self) -> bool:
        """是否应当写入 / 替换持久快照。"""

        return self.action in (SnapshotAction.INSTALL, SnapshotAction.REPLACE)

    @property
    def kept_previous(self) -> bool:
        """是否保留了一份已有快照。首轮无快照时为 ``False``（`V-花名册-44`）。"""

        return self.action is SnapshotAction.KEEP_PREVIOUS

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实。姓名、工号、邮箱、人员 ID 一个都不在这里。"""

        return {
            "action": self.action.value,
            "status": self.status,
            "alert": self.alert.value if self.alert is not None else None,
            "failure_code": self.failure_code,
            "failure_kind": self.failure_kind,
            "rows": self.row_count,
            "pages": self.pages_read,
            "kept_previous": self.kept_previous,
            "previous_captured_at": (
                self.previous_captured_at.isoformat() if self.previous_captured_at else None
            ),
            "previous_row_count": self.previous_row_count,
            "previous_age_seconds": self.previous_age_seconds,
        }


def _status_value(outcome: Any) -> str:
    """取读取结果的状态字面量。枚举与字符串都接受，其余一律响亮失败。"""

    status = getattr(outcome, "status", None)
    value = getattr(status, "value", status)
    if not isinstance(value, str) or value not in _KNOWN_STATUSES:
        raise ValueError(f"未知的花名册读取状态：{value!r}")
    return value


def _classify(outcome: Any, status: str) -> SnapshotAlertKind:
    """把"不能替换"的三种状态分成四类告警原因。"""

    if status == STATUS_EMPTY_SOURCE:
        return SnapshotAlertKind.EMPTY_SOURCE
    if status == STATUS_INCOMPLETE:
        return SnapshotAlertKind.INCOMPLETE
    failure = getattr(outcome, "failure", None)
    kind = getattr(getattr(failure, "kind", None), "value", None)
    if kind == FAILURE_DEFINITE:
        return SnapshotAlertKind.FAILED_DEFINITE
    # 结果不明是**兜底**方向而不是明确失败：把一次网络抖动说成"花名册权限被回收"
    # 会让管理员去改一个没坏的配置（读取层 RosterFailureKind 的同一条理由）。
    return SnapshotAlertKind.FAILED_INDETERMINATE


def decide_snapshot_update(
    outcome: Any,
    *,
    previous: StoredSnapshotFacts | None,
    now: datetime,
) -> SnapshotDecision:
    """判定这一轮该不该替换快照，并结算保旧告警事实。纯函数，无 I/O。

    ``previous`` 为 ``None`` 表示库里**从未有过**快照；``now`` 是本轮读取完成的时刻，
    由调用方注入（用它算上一份快照的年龄，也用作新快照的读取时间）。
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("本轮读取时间必须带时区")
    moment = now.astimezone(_UTC)

    status = _status_value(outcome)
    integrity = getattr(outcome, "integrity", None)
    row_count = int(getattr(integrity, "row_count", 0) or 0)
    pages_read = int(getattr(integrity, "pages_read", 0) or 0)
    failure = getattr(outcome, "failure", None)
    failure_code = getattr(failure, "code", None)
    failure_kind = getattr(getattr(failure, "kind", None), "value", None)

    previous_age: float | None = None
    if previous is not None:
        previous_age = (moment - previous.captured_at).total_seconds()

    # **唯一门槛**：状态是 COMPLETE。不看 rows 是否非空——理由见模块说明。
    # 两个判据都要过，是因为它们防的是不同的错：``complete_nonempty`` 是读取层给出的
    # 结论（读取层改判定时这里跟着变），``status`` 则挡住"属性说可以、状态说不行"的
    # 不自洽对象。任一为假都不替换，方向永远偏向保旧。
    if bool(getattr(outcome, "complete_nonempty", False)) and status == STATUS_COMPLETE:
        rows = getattr(outcome, "rows", ())
        if not rows:
            # 读取层的契约是"COMPLETE 恒非空"。真出现这种对象说明读取层被改坏了或
            # 调用方自造了一个不自洽的结果；写一份零行快照会静默清空比对基线，
            # 因此这里响亮失败而不是"顺手当成空源"。
            raise ValueError("状态为 COMPLETE 的读取结果不含任何行，拒绝写入空快照")
        return SnapshotDecision(
            action=SnapshotAction.INSTALL if previous is None else SnapshotAction.REPLACE,
            status=status,
            row_count=row_count,
            pages_read=pages_read,
            previous_captured_at=previous.captured_at if previous else None,
            previous_row_count=previous.row_count if previous else None,
            previous_age_seconds=previous_age,
        )

    return SnapshotDecision(
        action=SnapshotAction.KEEP_PREVIOUS if previous is not None else SnapshotAction.NO_SNAPSHOT_YET,
        status=status,
        alert=_classify(outcome, status),
        failure_code=failure_code,
        failure_kind=failure_kind,
        row_count=row_count,
        pages_read=pages_read,
        previous_captured_at=previous.captured_at if previous else None,
        previous_row_count=previous.row_count if previous else None,
        previous_age_seconds=previous_age,
    )


class RosterSnapshotStore(Protocol):
    """持久快照载体的最小接口（可注入面）。

    实现见 ``adapters/postgres_roster_snapshot.py``；判定层只依赖这三个方法，
    因此全部断言可以在没有数据库的机器上跑完。
    """

    def load_facts(self) -> StoredSnapshotFacts | None: ...

    def replace(self, rows: Sequence[Any], integrity: Any, *, captured_at: datetime) -> str: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class RosterSnapshotUpdater:
    """把「读一轮 → 判定 → 替换或保旧 → 留痕」串起来。只编排注入的接口，不做 I/O。

    形状与 ``core/alerting.py`` 的 ``AlertDispatcher`` 一致（编排放 ``core``，真正的
    外部调用在注入进来的对象里），因此 scheduler、以及将来任何需要刷新快照的入口，
    都装配同一份编排。

    **本类不注册成定时职责，也不改任何现有职责的行为**：真实花名册读取的凭据自
    2026-08-09 起未落盘（Issue #52 的 G-READ 判定），接线属 S-B-04 / S-B-05 的范围。
    这里交付的是可断言的判定与事实产出。
    """

    name = "花名册快照"

    def __init__(
        self,
        *,
        store: RosterSnapshotStore,
        audit: _AuditSink,
        on_alert: Callable[[SnapshotDecision], None] | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._on_alert = on_alert

    def apply(self, outcome: Any, *, now: datetime) -> SnapshotDecision:
        """按一轮读取结果更新快照，返回判定结果。

        ``now`` 既是判定用的时钟，也是新快照的读取时间：读取层不自带时间戳，
        "这份快照有多新"只能由完成读取的这一侧记。

        写入失败**不吞**（纪律同 ``adapters/feishu_roster_bitable.read_roster_snapshot``
        对未预期异常的处理）：先留一条审计说明这一轮没能替换，再原样上抛，由职责层
        隔离。吞掉它会让"快照其实一直没更新"表现为一切正常。
        """

        previous = self._store.load_facts()
        decision = decide_snapshot_update(outcome, previous=previous, now=now)

        if decision.should_replace:
            try:
                self._store.replace(outcome.rows, outcome.integrity, captured_at=now)
            except Exception as error:
                self._audit.record(
                    "roster_snapshot.replace_failed",
                    # 只记异常类型：异常正文可能带上被写入的行内容。
                    error=type(error).__name__,
                    **decision.audit_facts(),
                )
                raise
            self._audit.record("roster_snapshot.replaced", **decision.audit_facts())
            return decision

        self._audit.record("roster_snapshot.kept_previous", **decision.audit_facts())
        if self._on_alert is not None:
            self._on_alert(decision)
        return decision
