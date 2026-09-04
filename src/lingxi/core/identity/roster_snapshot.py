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

**告警只产出事实，不发送**：本模块交付可断言的告警事实与一个 ``on_alert`` 注入点，
:class:`RosterSnapshotUpdater` 在需要时把它调起来；不注入就只落审计，不静默丢弃。
S-B-04 把这个注入点接上了 scheduler 的结构化告警日志，而**面向管理员的那一份提醒走
每日日报**（`V-花名册-47` 的裁定原文就是「按日报告警提醒、不自动删」）——`core/alerting.py`
的状态机只认心跳、任务滞留与发送连续失败三类信号，快照超龄是一件每日节奏的数据新鲜度
事实，塞进那三类里会让阈值、去重与恢复计时三套语义同时失真。

**S-B-04 新增的第三段**（:class:`DailyRosterSource`）：把「读一轮 → 判定 → 替换或保旧 →
交出这一轮用于比对的行」串成日报真正要用的那条链，并结算快照超龄事实。
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
        action=SnapshotAction.KEEP_PREVIOUS
        if previous is not None
        else SnapshotAction.NO_SNAPSHOT_YET,
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

    **调用方**是 :class:`DailyRosterSource`（S-B-04 起），后者由 ``apps/scheduler`` 的
    花名册审计日报职责每天驱动一次。真实花名册读取所需的专用主体凭据自 2026-08-09 起
    未落盘（Issue #52 的 G-READ 判定），因此**当前部署下该职责仍然不注册**——不注册的
    理由现在是配置缺项并留有审计，而不是装配里写死的 ``None``。
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


#: 快照超龄阈值的默认值（`V-花名册-47`）。
#:
#: **为什么是 48 小时。** 快照由每日一轮的日报职责刷新，正常情况下每天换一份新的，
#: 因此「一天没换」几乎总是可以自愈的一次性事件——源头临时读不到、一次发布窗口、
#: 一次网络抖动，下一轮就好了；为它每天打扰管理员会让这条提醒很快被忽略。**连续两天
#: 没换新**才说明源头是真的读不到了，而那正是需要人去看配置与权限的时候。
#:
#: 下界不能低于 24 小时：日报一天只跑一轮，24 小时以内的阈值会把「今天这一轮还没轮到」
#: 报成异常。上界也不宜远大于 48 小时：比对基线在无人知晓的情况下持续变旧，恰恰是
#: 「花名册资料比对」这件事最不该有的失效形态——它会安静地一直报「没有差异」。
#:
#: 可由部署覆盖（``LINGXI_ROSTER_SNAPSHOT_STALE_AFTER_HOURS``），因为「多久算旧」
#: 取决于花名册本身的维护节奏，那是部署事实而不是产品规则。
DEFAULT_SNAPSHOT_STALE_AFTER = timedelta(hours=48)


@dataclass(frozen=True)
class RosterSnapshotStatus:
    """一轮取用之后，快照本身处于什么状态。**不含任何花名册字段值**（`V-花名册-46`）。

    这个对象有两个消费者，因此它既要能进审计，也要能进日报正文：产品负责人 2026-08-08
    的 D2 裁定要求日报写明「快照时间与同步状态」，而管理员据以判断「今天这份日报可不可信」
    的全部依据就在这里——快照多旧、本轮读取成不成功、失败原因是哪一类。
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
        """快照是否已经超龄（`V-花名册-47`）。没有快照时为 ``False``——那是更严重的
        另一类事实（:attr:`available`），把两者混成同一个布尔值会让日报只说得出一句话。"""

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

    只编排注入的可调用对象，不做 I/O，形状与 :class:`RosterSnapshotUpdater` 一致
    （真正的读取与读库在注入进来的东西里）。它回答的是一个此前没有归属的问题：
    **日报到底拿哪一份行去比对**。

    答案分两种，且都不是「本轮读到什么就比什么」：

    - 本轮可以替换（``COMPLETE``）：比对用**本轮读到的行**，快照同时被换成这一份；
    - 本轮不可替换（空源 / 不完整 / 失败）：比对用**库里那一份上一次成功的快照**，
      并把保旧原因带进日报（产品负责人 2026-08-07 决定：空源、失败或半轮读取继续
      保留上一份有效快照）。半轮读到的行一行都拿不到——读取层在 `FAILED` 时把
      ``rows`` 清空正是为了这一刻。

    **完全没有快照时不比对**（`V-花名册-48`）：库里从未有过快照、而本轮又读不成功时，
    行集是空的，拿它去比对会把全体已开通用户报成「花名册查无此人」。这里如实交出
    ``available=False``，由日报侧改发告警。

    ``RosterSnapshotInconsistent``（回读到的元信息与行对不上，一次并发替换恰好落在
    两条语句之间）**不在这里捕获**：那是一个响亮的、下一轮重试就会消失的信号，
    由职责层的失败隔离承接（`V-花名册-17`），吞掉它会让「比对用的行少了一大截」
    表现为一切正常。
    """

    def __init__(
        self,
        *,
        read_round: Callable[[], Any],
        updater: RosterSnapshotUpdater,
        load_snapshot: Callable[[], Any | None],
        stale_after: timedelta = DEFAULT_SNAPSHOT_STALE_AFTER,
    ) -> None:
        if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
            raise ValueError("快照超龄阈值必须是正的时间长度")
        self._read_round = read_round
        self._updater = updater
        self._load_snapshot = load_snapshot
        self._stale_after = stale_after

    @property
    def stale_after(self) -> timedelta:
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
