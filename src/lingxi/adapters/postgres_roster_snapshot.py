"""花名册持久快照的读写。

快照载体是数据库表（迁移 ``0063_roster_snapshot``），不是进程内存或本地文件：
scheduler 每一次重启、每一次发布都会换掉进程和容器，只有库里的那一份能活下来。
**替换是整体替换，且在同一个事务里完成**（`V-花名册-42`）：先删掉旧快照那一行
（子表按 ``ON DELETE CASCADE`` 一起消失），再写入新的元信息与全部行；中途任何一步
失败即整体回滚，不存在"删了旧的还没写完新的"中间态——分两次提交会留下一个真实
可达的窗口，那一刻的比对会把全体已开通用户报成「移除」。

**这里不判定"能不能替换"**：门槛在 ``core/identity/roster_snapshot.py``（只认
``status``/``complete_nonempty``），本模块只负责"要换就原子地换掉、要读就原样读
回来"。**行原样保留，不去重、不过滤、不排序**（`V-花名册-06`）：同一人员 ID 多行
是花名册的实测常态，``row_index`` 保住读取次序。**日志只记计数**（`V-花名册-46`）：
任何一个人的资料值进日志都等于绕过日报的展示口径。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# 行形态只保留一份定义：读取层产出 `RosterRow`，快照回读也还它同一个类型，比对层
# 因此不必区分"这行是刚读的还是从快照回来的"。在这里重新定义一个同字段的类，会让
# 两条路径的行形态在某次改字段时悄悄分叉（读取层给了工号、快照层还叫 work_no 这类）。
from lingxi.adapters.feishu_roster_bitable import RosterRow
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.roster_snapshot import StoredSnapshotFacts
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)

_UTC = UTC

# 元信息只有一行（``singleton`` 唯一约束），因此不需要 ORDER BY / LIMIT。
LOAD_FACTS_SQL = "SELECT id, captured_at, row_count FROM roster_snapshot"

LOAD_ROWS_SQL = """
SELECT personnel_id, email, name, employee_no, record_id
  FROM roster_snapshot_row
 WHERE snapshot_id = %s
 ORDER BY row_index
"""

# 整体替换的三条语句，按依赖顺序。`DELETE` 依赖外键的 CASCADE 删掉旧行；
# 库里最多一份快照，所以不带 WHERE 就是"删掉那一份"。
DELETE_SNAPSHOT_SQL = "DELETE FROM roster_snapshot"

INSERT_SNAPSHOT_SQL = """
INSERT INTO roster_snapshot
     (id, captured_at, row_count, pages_read, reported_total, total_matches_rows,
      rows_without_personnel_id, blank_column_rows, duplicate_keys, duplicate_rows)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

INSERT_ROW_SQL = """
INSERT INTO roster_snapshot_row
     (snapshot_id, row_index, personnel_id, email, name, employee_no, record_id)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class RosterSnapshotInconsistentError(RuntimeError):
    """回读到的元信息与行对不上。消息里只有两个数字，没有任何行内容。

    这是**并发窗口**的信号，不是数据损坏：替换本身是原子的，但回读的两条语句在
    READ COMMITTED 下各取一次数据库快照，一次并发替换恰好落在中间就会让两边错位。
    抛出而不是返回半态——一份"元信息说 1206 行、实际零行"的快照会被比对读成
    「全员移除」。
    """


@dataclass(frozen=True)
class StoredRosterSnapshot:
    """回读出来的完整快照：元信息 + 行。

    构造出来的实例一定自洽：``len(rows) == facts.row_count`` 由 :meth:`
    PostgresRosterSnapshotStore.load` 在返回前核对，对不上就抛
    :class:`RosterSnapshotInconsistentError`。
    """

    facts: StoredSnapshotFacts
    rows: tuple[RosterRow, ...] = ()


def _text(value: object) -> str:
    """归一为可比较文本。``NULL`` 与空白都归空串，与读取层的归一口径一致。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _counts(items: Any, *, key: str, value: str) -> dict[str, int]:
    """把完整性事实里的具名计数序列转成 ``{名称: 计数}``。只取数字，不取值。"""
    result: dict[str, int] = {}
    for item in items or ():
        name = getattr(item, key, None)
        number = getattr(item, value, None)
        if isinstance(name, str) and isinstance(number, int) and not isinstance(number, bool):
            result[name] = number
    return result


class PostgresRosterSnapshotStore:
    """持久快照的读写。构造时不连接数据库，每次调用自带连接（同 adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def load_facts(self) -> StoredSnapshotFacts | None:
        """取当前快照的元信息；**从未有过快照时返回 ``None``**。

        判定层靠这个 ``None`` 区分「保旧」与「还没有任何快照」（`V-花名册-44`），
        因此这里不能用"零行快照"之类的哨兵对象代替——那会让首轮读失败被报成保旧。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(LOAD_FACTS_SQL)
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredSnapshotFacts(
            snapshot_id=str(row[0]),
            captured_at=_as_utc(row[1]),
            row_count=int(row[2]),
        )

    def load(self) -> StoredRosterSnapshot | None:
        """取完整快照（元信息 + 全部行）；从未有过快照时返回 ``None``。

        两次查询在同一个事务里，但默认隔离级别 READ COMMITTED 下每条语句各取一次
        快照：并发的一次 :meth:`replace` 恰好落在两条语句之间时，回来的可能是
        「元信息说 1206 行、实际零行」这种半态，原样交出去会让比对把全体已开通用户
        报成「移除」。因此**回读后逐份核对行数**，对不上就抛
        :class:`RosterSnapshotInconsistentError`，不返回半态；重试通常就好（替换是原子
        的，下一次读会稳定落在替换后的那一份）。提高隔离级别到 ``REPEATABLE READ``
        也能消除这个窗口，但改动面超出本适配器，行数核对更便宜、更可断言。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(LOAD_FACTS_SQL)
                header = cursor.fetchone()
                if header is None:
                    return None
                cursor.execute(LOAD_ROWS_SQL, (header[0],))
                rows = cursor.fetchall()

        facts = StoredSnapshotFacts(
            snapshot_id=str(header[0]),
            captured_at=_as_utc(header[1]),
            row_count=int(header[2]),
        )
        if len(rows) != facts.row_count:
            # 只报两个数字，不报任何行内容。
            raise RosterSnapshotInconsistentError(
                f"花名册快照回读不一致：元信息 {facts.row_count} 行，实际回来 {len(rows)} 行"
            )
        logger.info("花名册快照已回读 行=%s", len(rows))
        return StoredRosterSnapshot(
            facts=facts,
            rows=tuple(
                RosterRow(
                    personnel_id=_text(row[0]),
                    email=_text(row[1]),
                    name=_text(row[2]),
                    employee_no=_text(row[3]),
                    record_id=_text(row[4]),
                )
                for row in rows
            ),
        )

    def replace(
        self,
        rows: Sequence[Any],
        integrity: Any,
        *,
        captured_at: datetime,
    ) -> str:
        """用这一轮的行整体替换快照，返回新快照的内部标识。

        **调用方必须已经判定过这一轮可以替换**（``status``/``complete_nonempty``）。
        这里只再挡一次"零行"：写一份零行快照会静默清空比对基线，而数据库的
        ``row_count > 0`` 约束也会拒绝它——先在这里响亮失败，错误信息才说得清是谁传的。
        """
        snapshot_id, moment, payload, meta_fields = _build_replace_payload(
            rows, integrity, captured_at=captured_at
        )

        with connect(self._dsn, timeouts=self._timeouts) as connection:
            # 一个事务：删旧、写元信息、写行。中途失败整体回滚，库里仍是替换前那一份。
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(DELETE_SNAPSHOT_SQL)
                cursor.execute(
                    INSERT_SNAPSHOT_SQL,
                    (snapshot_id, moment, len(payload), *meta_fields),
                )
                cursor.executemany(INSERT_ROW_SQL, payload)

        # 只记条数与页数：行内容一个字段都不进日志（`V-花名册-46`）。
        logger.info("花名册快照已整体替换 行=%s", len(payload))
        return snapshot_id


def _build_replace_payload(
    rows: Sequence[Any],
    integrity: Any,
    *,
    captured_at: datetime,
) -> tuple[str, datetime, list[tuple[Any, ...]], tuple[Any, ...]]:
    """校验入参并拼出 :meth:`PostgresRosterSnapshotStore.replace` 要写的行与元信息。

    返回的元信息字段顺序须与 ``INSERT_SNAPSHOT_SQL`` 里 ``captured_at``/
    ``row_count`` 之后的列一一对应，调用方按位置拼接。
    """
    if not rows:
        raise ValueError("拒绝写入零行花名册快照：空快照会清空比对基线")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("快照读取时间必须带时区")
    moment = captured_at.astimezone(_UTC)

    snapshot_id = new_id("rsn")
    payload = [
        (
            snapshot_id,
            index,
            _text(getattr(row, "personnel_id", "")),
            _text(getattr(row, "email", "")),
            _text(getattr(row, "name", "")),
            _text(getattr(row, "employee_no", "")),
            _text(getattr(row, "record_id", "")),
        )
        for index, row in enumerate(rows)
    ]

    duplicates = getattr(integrity, "duplicates", ())
    meta_fields = (
        int(getattr(integrity, "pages_read", 0) or 0),
        getattr(integrity, "reported_total", None),
        getattr(integrity, "total_matches_rows", None),
        int(getattr(integrity, "rows_without_personnel_id", 0) or 0),
        _jsonb(_counts(getattr(integrity, "blank_column_rows", ()), key="column", value="rows")),
        _jsonb(_counts(duplicates, key="field", value="keys")),
        _jsonb(_counts(duplicates, key="field", value="rows")),
    )
    return snapshot_id, moment, payload, meta_fields


def _jsonb(value: Any) -> Any:
    """把计数映射交给 psycopg 的 JSONB 适配。延迟导入：没有驱动的机器仍能 import 本模块。"""
    try:
        from psycopg.types.json import Jsonb
    except ModuleNotFoundError:  # pragma: no cover - 只有无驱动环境会走到
        return json.dumps(value, ensure_ascii=False)
    return Jsonb(value)


def _as_utc(value: Any) -> datetime:
    """库里的 ``TIMESTAMPTZ`` 回来时应当带时区；缺时区一律按 UTC 解读并记为异常输入。"""
    if not isinstance(value, datetime):
        raise ValueError("快照读取时间必须是时间戳")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)
