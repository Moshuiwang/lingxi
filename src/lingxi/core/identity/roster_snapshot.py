"""花名册持久快照的替换判定与保旧告警事实。

每日花名册比对需要一份跨进程重启与常规发布都还在的持久快照，而「源头返回空 /
读取失败 / 超时 / 半轮」一律**保留上一份有效快照**，绝不覆盖。本模块回答两个
判定：这一轮能不能替换快照——门槛只有一个，读取结果的 ``status`` 是
``COMPLETE``；不能替换时管理员该被告知什么——按原因分四类告警事实，并区分
「保留了上一份」与「还没有任何快照」（后者根本没有基线，说"保留了上一份"是假话）。

**为什么门槛不能写成「rows 非空」**（`V-花名册-41`，PR #208 二级审查钉入的合同条款）：
读取层的 ``INCOMPLETE`` 刻意保留 rows（行确实读到了，只是可信度没有），以行数为
门槛会让"读到了但不可信"的一轮顶掉一份好快照。
**本模块不 import 任何适配器**：读取结果按结构取用，四个状态字面量显式登记——
读取层将来多一个状态时响亮失败，不默默归到"保旧"。告警只产出事实、不发送；
:class:`DailyRosterSource` 把「读一轮 → 判定 → 替换或保旧」串成日报真正要用的链。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

_UTC = UTC

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
        """校验读取时间带时区、行数为正整数；把时间归一到 UTC。"""
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


def _read_facts(outcome: Any) -> tuple[int, int, Any, Any]:
    """从读取结果里取出行数、翻页数与失败码/失败分类四项事实。"""
    integrity = getattr(outcome, "integrity", None)
    row_count = int(getattr(integrity, "row_count", 0) or 0)
    pages_read = int(getattr(integrity, "pages_read", 0) or 0)
    failure = getattr(outcome, "failure", None)
    failure_code = getattr(failure, "code", None)
    failure_kind = getattr(getattr(failure, "kind", None), "value", None)
    return row_count, pages_read, failure_code, failure_kind


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
    row_count, pages_read, failure_code, failure_kind = _read_facts(outcome)
    previous_age = None if previous is None else (moment - previous.captured_at).total_seconds()
    previous_kwargs = {
        "previous_captured_at": previous.captured_at if previous else None,
        "previous_row_count": previous.row_count if previous else None,
        "previous_age_seconds": previous_age,
    }

    # **唯一门槛**：状态是 COMPLETE。不看 rows 是否非空——理由见模块说明。
    # 两个判据都要过：``complete_nonempty`` 是读取层给出的结论，``status`` 挡住
    # "属性说可以、状态说不行"的不自洽对象。任一为假都不替换，方向永远偏向保旧。
    if bool(getattr(outcome, "complete_nonempty", False)) and status == STATUS_COMPLETE:
        rows = getattr(outcome, "rows", ())
        if not rows:
            # 读取层的契约是"COMPLETE 恒非空"；真出现这种对象说明读取层被改坏了，
            # 写一份零行快照会静默清空比对基线，因此响亮失败而不是当成空源。
            raise ValueError("状态为 COMPLETE 的读取结果不含任何行，拒绝写入空快照")
        return SnapshotDecision(
            action=SnapshotAction.INSTALL if previous is None else SnapshotAction.REPLACE,
            status=status,
            row_count=row_count,
            pages_read=pages_read,
            **previous_kwargs,
        )

    return SnapshotDecision(
        action=SnapshotAction.KEEP_PREVIOUS
        if previous is not None
        else SnapshotAction.NO_SNAPSHOT_YET,
        status=status,
        alert=_classify(outcome, status),
        failure_code=failure_code,
        failure_kind=failure_kind,
        row_count=row_count,
        pages_read=pages_read,
        **previous_kwargs,
    )


class RosterSnapshotStore(Protocol):
    """持久快照载体的最小接口（可注入面）。

    实现见 ``adapters/postgres_roster_snapshot.py``；判定层只依赖这三个方法，
    因此全部断言可以在没有数据库的机器上跑完。
    """

    def load_facts(self) -> StoredSnapshotFacts | None:
        """读库里当前那份快照的元信息；从未有过快照时返回 ``None``。"""
        ...

    def replace(self, rows: Sequence[Any], integrity: Any, *, captured_at: datetime) -> str:
        """整体替换持久快照，返回新快照的标识。"""
        ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


class RosterSnapshotUpdater:
    """把「读一轮 → 判定 → 替换或保旧 → 留痕」串起来。只编排注入的接口，不做 I/O。

    形状与 ``core/alerting.py`` 的 ``AlertDispatcher`` 一致（编排放 ``core``，真正的
    外部调用在注入进来的对象里）。调用方是 :class:`DailyRosterSource`，由
    ``apps/scheduler`` 的花名册审计日报职责每天驱动一次；真实花名册读取所需的专用
    主体凭据尚未落盘时，当前部署下该职责不注册——理由是配置缺项并留有审计，不是
    装配里写死的 ``None``。
    """

    name = "花名册快照"

    def __init__(
        self,
        *,
        store: RosterSnapshotStore,
        audit: _AuditSink,
        on_alert: Callable[[SnapshotDecision], None] | None = None,
    ) -> None:
        """装配快照更新器；存储、审计与告警回调均由调用方注入。"""
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


#: 快照超龄阈值的默认值（`V-花名册-47`）。取 48 小时：一天没换几乎总是可以自愈的
#: 一次性事件，每天都报会被很快忽略，连续两天没换新才说明源头真的读不到了。下界
#: 不低于 24 小时（日报一天只跑一轮），上界不远大于 48 小时（比对基线不能无人知晓
#: 地持续变旧）。可由部署覆盖（``LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS``），
#: 因为"多久算旧"取决于花名册本身的维护节奏，是部署事实而不是产品规则。
DEFAULT_SNAPSHOT_STALE_AFTER = timedelta(hours=48)


@dataclass(frozen=True)
class RosterSnapshotStatus:
    """一轮取用之后，快照本身处于什么状态。**不含任何花名册字段值**（`V-花名册-46`）。

    这个对象有两个消费者，因此它既要能进审计，也要能进日报正文——日报须写明「快照
    时间与同步状态」，管理员据以判断「今天这份日报可不可信」的全部依据就在这里：
    快照多旧、本轮读取成不成功、失败原因是哪一类。
    """

    # `SnapshotAction` 的字面量：本轮对持久快照做了什么。
    action: str
    # `RosterReadStatus` 的字面量：本轮读取的结论。
    read_status: str
    # 超龄阈值，秒。放进状态对象而不是让渲染层去问配置：日报要说明「超过多久算旧」，
    # 而那个数字必须与真正做判定的那个是同一个。
    stale_after_seconds: float
    alert: str | None = None
    failure_code: str | None = None
    failure_kind: str | None = None
    # **本轮实际用于比对的那份快照**的读取时间。没有任何可用快照时为 ``None``。
    captured_at: datetime | None = None
    row_count: int = 0
    age_seconds: float | None = None

    @property
    def available(self) -> bool:
        """有没有一份可用于比对的快照。

        ``False`` 时**绝不能拿空行去比对**：比对集来自 `app_user`，花名册侧一行都没有
        会把全体已开通用户报成「移除」（`V-花名册-48`）。
        """
        return self.captured_at is not None

    @property
    def refreshed(self) -> bool:
        """本轮是否真的换上了新快照。"""
        return self.action in (SnapshotAction.INSTALL.value, SnapshotAction.REPLACE.value)

    @property
    def stale(self) -> bool:
        """快照是否已经超龄（`V-花名册-47`）。

        没有快照时为 ``False``——那是更严重的另一类事实（:attr:`available`），
        把两者混成同一个布尔值会让日报只说得出一句话。
        """
        return self.age_seconds is not None and self.age_seconds > self.stale_after_seconds

    @property
    def needs_attention(self) -> bool:
        """这一天是否**即使没有任何资料差异也必须发日报**。

        空差异日本来不发（`V-花名册-25`：没有待办却每天发一条「今天没事」，管理群很快
        会学会忽略它）。但「快照超龄」与「没有快照」这两种情形下，「没有差异」这句话
        本身就是不可信的——正是这一条让沉默成为最危险的输出。
        """
        return not self.available or self.stale

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实。姓名、工号、邮箱、人员 ID 一个都不在这里。"""
        return {
            "snapshot_action": self.action,
            "snapshot_read_status": self.read_status,
            "snapshot_alert": self.alert,
            "snapshot_failure_code": self.failure_code,
            "snapshot_rows": self.row_count,
            "snapshot_captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "snapshot_age_seconds": self.age_seconds,
            "snapshot_stale": self.stale,
            "snapshot_available": self.available,
        }


@dataclass(frozen=True)
class RosterRound:
    """一轮花名册取用的结果：用于比对的行 + 快照状态。

    两者必须一起交出去。只给行，日报就说不清「这些行是今天读到的还是三天前那份」；
    只给状态，比对就没有输入。
    """

    rows: tuple[Any, ...]
    snapshot: RosterSnapshotStatus


class DailyRosterSource:
    """每日一轮的花名册取用：**读一轮 → 更新持久快照 → 交出比对用的行与快照状态**。

    只编排注入的可调用对象，不做 I/O。答案分两种，都不是「本轮读到什么就比什么」：
    可替换（``COMPLETE``）时比对用**本轮读到的行**；不可替换（空源/不完整/失败）
    时比对用**库里上一次成功的快照**并把保旧原因带进日报。**完全没有快照时不
    比对**（`V-花名册-48`）：如实交出 ``available=False``，由日报侧改发告警，不拿
    空行比对（会把全体已开通用户报成"查无此人"）。``RosterSnapshotInconsistentError``
    **不在这里捕获**：那是并发替换窗口内的响亮信号，由职责层的失败隔离承接。
    """

    def __init__(
        self,
        *,
        read_round: Callable[[], Any],
        updater: RosterSnapshotUpdater,
        load_snapshot: Callable[[], Any | None],
        stale_after: timedelta = DEFAULT_SNAPSHOT_STALE_AFTER,
    ) -> None:
        """装配日报数据源；``stale_after`` 必须是正的时间长度。"""
        if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
            raise ValueError("快照超龄阈值必须是正的时间长度")
        self._read_round = read_round
        self._updater = updater
        self._load_snapshot = load_snapshot
        self._stale_after = stale_after

    @property
    def stale_after(self) -> timedelta:
        """当前生效的超龄阈值。"""
        return self._stale_after

    def current(self, *, now: datetime) -> RosterRound:
        """跑一轮读取与快照更新，交出这一轮用于比对的行与快照状态。"""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("本轮读取时间必须带时区")
        moment = now.astimezone(_UTC)

        outcome = self._read_round()
        decision = self._updater.apply(outcome, now=moment)

        if decision.should_replace:
            # 刚写进去的就是本轮读到的那一份，不必再回读一次库：多读一次既慢，
            # 又给并发替换多开一个可以读到别人那一份的窗口。
            rows = tuple(getattr(outcome, "rows", ()))
            return RosterRound(
                rows, self._status(decision, captured_at=moment, row_count=len(rows), now=moment)
            )

        stored = self._load_snapshot()
        if stored is None:
            # 库里一份都没有：`decision.action` 已经是 `NO_SNAPSHOT_YET`，这里也可能是
            # 「刚刚被并发删掉」。两种情况对日报是同一件事：没有基线，不能比对。
            return RosterRound(
                (), self._status(decision, captured_at=None, row_count=0, now=moment)
            )

        facts = stored.facts
        return RosterRound(
            tuple(stored.rows),
            self._status(
                decision, captured_at=facts.captured_at, row_count=facts.row_count, now=moment
            ),
        )

    def _status(
        self,
        decision: SnapshotDecision,
        *,
        captured_at: datetime | None,
        row_count: int,
        now: datetime,
    ) -> RosterSnapshotStatus:
        age = None if captured_at is None else (now - captured_at).total_seconds()
        return RosterSnapshotStatus(
            action=decision.action.value,
            read_status=decision.status,
            stale_after_seconds=self._stale_after.total_seconds(),
            alert=decision.alert.value if decision.alert is not None else None,
            failure_code=decision.failure_code,
            failure_kind=decision.failure_kind,
            captured_at=captured_at,
            row_count=row_count,
            age_seconds=age,
        )
