"""把一份银河导出写入 Lingxi 数据库（PostgreSQL）。

流程固定为四步，顺序不可调换：

1. **校验**——纯函数 `core.permission.galaxy_export.validate_export`；不通过就此
   结束，一行都不写，也不建批次。半批权限数据比没有数据更危险。
2. **检查已有数据**——同一份导出（`source_digest`）已有完成批次时直接返回既有批次，
   不产生第二份。
3. **写入**——批次与五张表在**同一个事务**里写完；任一步失败整批回滚。
4. **回读确认**——从库里数回来的行数与校验阶段的计数逐表比对，不一致即中止并回滚，
   批次不会被标成 `complete`。

写入的是导出的原始结构，不做解释：`全非` 哨兵、A 前缀角色映射、公司范围展开都在
解释层。本模块也不触碰 `app_user` 与任何身份切片的表。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.permission.galaxy_export import (
    SOURCE_TABLES,
    TABLE_SPECS,
    TARGET_TABLES,
    Issue,
    validate_export,
)

IMPORTED = "imported"
logger = logging.getLogger("lingxi.adapters.galaxy_import")

ALREADY_IMPORTED = "already_imported"
REJECTED = "rejected"

# 批次表里每张源表对应的行数列。
_ROW_COUNT_COLUMNS: Mapping[str, str] = {
    "user": "user_row_count",
    "user_role": "user_role_row_count",
    "role_menu": "role_menu_row_count",
    "sys_user_datacountry": "user_datacountry_row_count",
    "sys_country": "country_row_count",
}


class GalaxyImportVerificationError(RuntimeError):
    """回读确认失败：写进去的行数与校验阶段的计数对不上。"""


@dataclass(frozen=True)
class ImportResult:
    """一次银河导出落库的结果：结论、批次号、各表行数与问题清单。"""

    outcome: str
    batch_id: str | None
    row_counts: Mapping[str, int]
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _issue_payload(issues: Sequence[Issue]) -> list[dict[str, Any]]:
    """把校验结论转成可入库的结构信息；`detail` 本身已按脱敏纪律不含人员数据。"""
    return [
        {
            "table": issue.table,
            "rule": issue.rule,
            "detail": issue.detail,
            "rows": list(issue.row_numbers),
        }
        for issue in issues
    ]


class PostgresGalaxyImportStore:
    """银河导出的落库入口。构造时不连接数据库，每次调用自带事务。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """接入连接串与超时配置；不建连接。"""
        from psycopg.types.json import Json

        self._json = Json
        self._dsn = dsn
        self._timeouts = timeouts

    # ---- 查询 -------------------------------------------------------------

    def current_batch_id(self) -> str | None:
        """当前有效批次：最近一个**未过期**的 `complete` 批次。

        过期批次不算「当前有效」：清理流程可能尚未运行，把过期快照当有效
        会让下游继续使用可能已失效的权限与超期人员副本（合同九十天上限）。
        没有新鲜批次时如实返回 None，由调用方安全失败。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT id FROM galaxy_import_batch WHERE status = 'complete' AND expires_at > now() "
                "ORDER BY completed_at DESC, started_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def _batch_for_digest(self, cursor: Any, source_digest: str) -> tuple[str, str] | None:
        # 幂等判定不看 status='complete'：批次被更新导出取代（superseded）后，
        # 同一份旧导出再导一次会写出第二份全量并把权限快照回退（独立复查发现）。
        # 只有 failed 批次允许同摘要重来。
        cursor.execute(
            "SELECT id, status FROM galaxy_import_batch WHERE source_digest = %s AND status <> 'failed' LIMIT 1",
            (source_digest,),
        )
        row = cursor.fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    # ---- 写入 -------------------------------------------------------------

    def _insert_rows(
        self, cursor: Any, source_table: str, rows: Sequence[Mapping[str, Any]]
    ) -> int:
        """写入一张表的全部行，返回写入行数。子类可覆盖以注入故障（测试用）。"""
        if not rows:
            return 0
        target = TARGET_TABLES[source_table]
        columns = ["batch_id", *[column.canonical for column in TABLE_SPECS[source_table].columns]]
        placeholders = ", ".join(["%s"] * len(columns))
        statement = f"INSERT INTO {target} ({', '.join(columns)}) VALUES ({placeholders})"
        cursor.executemany(statement, [tuple(row[column] for column in columns) for row in rows])
        return len(rows)

    def _readback_count(self, cursor: Any, source_table: str, batch_id: str) -> int:
        cursor.execute(
            f"SELECT count(*) FROM {TARGET_TABLES[source_table]} WHERE batch_id = %s",
            (batch_id,),
        )
        return int(cursor.fetchone()[0])

    def _reuse_existing_batch(
        self,
        cursor: Any,
        source_digest: str,
        confirm_unchanged: bool,
        row_counts: Mapping[str, int],
        report: Any,
    ) -> ImportResult | None:
        """命中同摘要既有批次时该不该复用；返回 ``None`` 表示继续走新导入。"""
        existing_hit = self._batch_for_digest(cursor, source_digest)
        if existing_hit is not None and confirm_unchanged:
            # 真实的新一次导出恰与历史内容相同（连续刷新无变化、或权限
            # A→B→A）：仅当调用方显式确认「这是新导出」时才作为新批次
            # 落库并成为当前有效——摘要不是导出身份，默认路径仍然挡住
            # 误重导（防快照回退）。
            logger.info("同内容导出经显式确认，按新批次导入 previous=%s", existing_hit[0])
            existing_hit = None
        if existing_hit is None:
            return None
        existing, existing_status = existing_hit
        if existing_status == "superseded":
            logger.warning(
                "该导出对应的批次 %s 已被更新导出取代；不重导。若确需回滚到旧快照，须显式操作而非重复导入",
                existing,
            )
        return ImportResult(
            outcome=ALREADY_IMPORTED,
            batch_id=existing,
            row_counts=row_counts,
            warnings=report.warnings,
        )

    def _write_batch_rows(
        self,
        cursor: Any,
        new_batch_id: str,
        source_label: str,
        source_digest: str,
        report: Any,
    ) -> None:
        """插入批次头（``staging``）与五张源表的全部行。"""
        cursor.execute(
            "INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, metadata) "
            "VALUES (%s, %s, %s, 'staging', %s)",
            (
                new_batch_id,
                source_label,
                source_digest,
                self._json({"warnings": _issue_payload(report.warnings)}),
            ),
        )
        for source_table in SOURCE_TABLES:
            rows = [{"batch_id": new_batch_id, **dict(row)} for row in report.rows[source_table]]
            self._insert_rows(cursor, source_table, rows)

    def _readback_and_verify(
        self, cursor: Any, new_batch_id: str, row_counts: Mapping[str, int]
    ) -> dict[str, int]:
        """以库里数回来的行数为准核对写入；不一致就抛错回滚，不相信写入调用的返回值。"""
        readback = {
            source_table: self._readback_count(cursor, source_table, new_batch_id)
            for source_table in SOURCE_TABLES
        }
        mismatched = [name for name in SOURCE_TABLES if readback[name] != row_counts[name]]
        if mismatched:
            raise GalaxyImportVerificationError(
                "回读确认失败，已整批回滚："
                + "；".join(
                    f"{name} 期望 {row_counts[name]} 行、实际 {readback[name]} 行"
                    for name in mismatched
                )
            )
        return readback

    def _finalize_batch(self, cursor: Any, new_batch_id: str, readback: Mapping[str, int]) -> None:
        """旧的完成批次让位给新批次，保证「当前有效批次」唯一。"""
        cursor.execute(
            "UPDATE galaxy_import_batch SET status = 'superseded' "
            "WHERE status = 'complete' AND id <> %s",
            (new_batch_id,),
        )
        cursor.execute(
            "UPDATE galaxy_import_batch SET status = 'complete', completed_at = now(), "
            f"{', '.join(f'{column} = %s' for column in _ROW_COUNT_COLUMNS.values())} "
            "WHERE id = %s",
            (*[readback[name] for name in _ROW_COUNT_COLUMNS], new_batch_id),
        )

    def import_export(
        self,
        *,
        source_label: str,
        source_digest: str,
        tables: Mapping[str, Sequence[Mapping[str, Any]]],
        batch_id: str | None = None,
        confirm_unchanged: bool = False,
    ) -> ImportResult:
        """执行「校验 → 检查已有数据 → 写入 → 回读确认」。"""
        report = validate_export(tables)
        if not report.ok:
            return ImportResult(
                outcome=REJECTED,
                batch_id=None,
                row_counts=dict(report.row_counts),
                errors=report.errors,
                warnings=report.warnings,
            )

        new_batch_id = batch_id or f"gib_{secrets.token_urlsafe(16)}"
        row_counts = {name: len(report.rows[name]) for name in SOURCE_TABLES}

        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            # 人工 CLI 也可能并发执行：advisory 锁把整个导入串行化，
            # 免去「两个不同摘要交错留下双 complete」的窗口。
            cursor.execute("SELECT pg_advisory_xact_lock(4217001)")
            reused = self._reuse_existing_batch(
                cursor, source_digest, confirm_unchanged, row_counts, report
            )
            if reused is not None:
                return reused

            self._write_batch_rows(cursor, new_batch_id, source_label, source_digest, report)
            readback = self._readback_and_verify(cursor, new_batch_id, row_counts)
            self._finalize_batch(cursor, new_batch_id, readback)

        return ImportResult(
            outcome=IMPORTED,
            batch_id=new_batch_id,
            row_counts=readback,
            warnings=report.warnings,
        )
