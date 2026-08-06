"""组织快照与统一用户记录的 PostgreSQL 存取。

吸收 `scripts/sync_feishu_org_snapshot.py`（受控验收脚本）已验证的两条模式：
先在内存里完成两条身份路径的完整性校验，再在**一个事务**里写入同一轮快照；
失败时只留一条 ``failed`` 批次，不留任何成员行。

校验规则本身在 :mod:`lingxi.core.identity.org_snapshot`，本模块只负责
"不通过就一行都不提交"这件事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lingxi.core.identity.first_contact import IdentityRecordDraft
from lingxi.core.identity.identifiers import redact_identifier
from lingxi.core.identity.org_snapshot import (
    DirectoryAvailability,
    SnapshotBatch,
    SnapshotIntegrityError,
    SnapshotMember,
    directory_availability,
    require_complete_batch,
    snapshot_expires_at,
)
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectoryLookup:
    """一次首聊定位所需的全部外部输入：资料可用性 + 候选成员。"""

    availability: DirectoryAvailability
    members: tuple[SnapshotMember, ...]


@dataclass(frozen=True)
class AppUserRecord:
    id: str
    provisioning_state: str
    permission_record_id: str | None
    created: bool


class PostgresOrgSnapshotStore:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn

    def commit_batch(
        self,
        batch: SnapshotBatch,
        *,
        source_app_id: str,
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> str:
        """校验通过才写入一整轮快照；不通过则抛错并只留一条失败批次。

        校验在事务之外先做完：**半轮快照比没有快照更危险**，它会让一部分在职
        员工"定位不到"，而失败原因在下游完全看不出来。
        """

        identifier = run_id or new_id("orgsync")
        moment = started_at or datetime.now(timezone.utc)
        try:
            report = require_complete_batch(batch)
        except SnapshotIntegrityError as error:
            self._record_failed_run(identifier, source_app_id, moment, error)
            raise

        with self._psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO feishu_org_sync_run
                             (id, source_app_id, status, started_at, completed_at, expires_at,
                              tenant_count, department_count, member_count, metadata)
                           VALUES (%s, %s, 'complete', %s, now(), %s, %s, %s, %s, %s)""",
                        (
                            identifier,
                            source_app_id,
                            moment,
                            snapshot_expires_at(moment),
                            report.tenant_count,
                            report.department_count,
                            report.member_count,
                            self._json({"integrity_checked": True, "credentials_saved": False}),
                        ),
                    )
                    cursor.executemany(
                        """INSERT INTO feishu_org_tenant_snapshot
                             (id, sync_run_id, tenant_key, visible_to_user_identity, member_count)
                           VALUES (%s, %s, %s, %s, %s)""",
                        [
                            (new_id("tenant"), identifier, scope.tenant_key, scope.visible_to_user_identity, len(scope.user_member_keys))
                            for scope in batch.tenants
                        ],
                    )
                    if batch.departments:
                        cursor.executemany(
                            """INSERT INTO feishu_org_department_snapshot
                                 (id, sync_run_id, tenant_key, department_key, name)
                               VALUES (%s, %s, %s, %s, %s)""",
                            [
                                (new_id("dept"), identifier, item.tenant_key, item.department_key, item.name)
                                for item in batch.departments
                            ],
                        )
                    cursor.executemany(
                        """INSERT INTO feishu_org_member_snapshot
                             (id, sync_run_id, tenant_key, member_key, open_id, user_id, union_id,
                              display_name, display_name_locale, department_names)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        [
                            (
                                new_id("member"),
                                identifier,
                                item.tenant_key,
                                item.member_key,
                                item.open_id,
                                item.user_id,
                                item.union_id,
                                item.display_name,
                                item.display_name_locale,
                                self._json(list(item.department_names)),
                            )
                            for item in batch.members
                        ],
                    )
                    # 旧批次让位，但只让**更早启动**的让位：较早启动、较晚完成的
                    # 批次不得反过来取代更新的数据（Codex 复查发现）。整个切换在
                    # advisory 锁内串行化，避免两轮同步交错留下双 complete。
                    cursor.execute("SELECT pg_advisory_xact_lock(4217002)")
                    cursor.execute(
                        """UPDATE feishu_org_sync_run SET status = 'superseded'
                            WHERE status = 'complete' AND id <> %(identifier)s
                              AND started_at <= (SELECT started_at FROM feishu_org_sync_run WHERE id = %(identifier)s)""",
                        {"identifier": identifier},
                    )
                    cursor.execute(
                        """UPDATE feishu_org_sync_run SET status = 'superseded'
                            WHERE id = %(identifier)s
                              AND EXISTS (
                                  SELECT 1 FROM feishu_org_sync_run other
                                   WHERE other.status = 'complete' AND other.id <> %(identifier)s
                                     AND other.started_at > feishu_org_sync_run.started_at
                              )""",
                        {"identifier": identifier},
                    )
        logger.info(
            "组织快照批次已提交 tenants=%s departments=%s members=%s",
            report.tenant_count,
            report.department_count,
            report.member_count,
        )
        return identifier

    def latest_complete_expiry(self) -> datetime | None:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT expires_at FROM feishu_org_sync_run WHERE status = 'complete' ORDER BY started_at DESC LIMIT 1"
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def lookup(self, open_id: str, *, now: datetime | None = None) -> DirectoryLookup:
        """按完整 ``open_id`` 在最近一轮完成快照里取候选成员。

        资料不可用或已过九十天上限时不返回任何候选：那时"查不到"不是事实，
        只是我们暂时看不见，判定必须走终态而不是"定位不到"。
        """

        moment = now or datetime.now(timezone.utc)
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, expires_at FROM feishu_org_sync_run WHERE status = 'complete' ORDER BY started_at DESC LIMIT 1"
            )
            run = cursor.fetchone()
            availability = directory_availability(run[1] if run else None, moment)
            if run is None or availability is not DirectoryAvailability.AVAILABLE:
                return DirectoryLookup(availability, ())
            cursor.execute(
                """SELECT tenant_key, member_key, open_id, user_id, union_id,
                          display_name, display_name_locale, department_names
                     FROM feishu_org_member_snapshot
                    WHERE sync_run_id = %s AND open_id = %s""",
                (run[0], open_id),
            )
            rows = cursor.fetchall()
        members = tuple(
            SnapshotMember(
                tenant_key=str(row[0]),
                member_key=str(row[1]),
                open_id=str(row[2]),
                user_id=str(row[3]),
                union_id=str(row[4]),
                display_name=str(row[5]),
                display_name_locale=row[6],
                department_names=tuple(str(name) for name in (row[7] or [])),
            )
            for row in rows
        )
        return DirectoryLookup(availability, members)

    def _record_failed_run(self, run_id: str, source_app_id: str, started_at: datetime, error: SnapshotIntegrityError) -> None:
        codes = ",".join(problem.value for problem in error.report.problems)[:120]
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO feishu_org_sync_run
                     (id, source_app_id, status, started_at, completed_at, expires_at, error_code)
                   VALUES (%s, %s, 'failed', %s, now(), %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (run_id, source_app_id, started_at, snapshot_expires_at(started_at), codes),
            )
        logger.warning("组织快照完整性校验未通过，未提交任何成员行 problems=%s", codes)

    def _json(self, value: Any) -> Any:
        from psycopg.types.json import Jsonb

        return Jsonb(value)


class PostgresAppUserStore:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self._dsn = dsn

    def record_identity(self, draft: IdentityRecordDraft) -> AppUserRecord:
        """按 ``feishu_open_id`` 建档或刷新资料。

        两条刻意不做的事：
        - **不写 ``permission_record_id`` / ``permission_version``**，连列都不出现在
          语句里，"先占位再回填"因此没有实现路径（断言 V-开通-01）；
        - 冲突时**不回写 ``provisioning_state``**，否则一个已经推进到发布或同步中的
          用户会因为再发一条消息被打回 ``matching``。
        """

        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app_user
                     (id, feishu_open_id, feishu_user_id, feishu_union_id, display_name,
                      display_name_locale, department, tenant_key, employee_no, email,
                      provisioning_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (feishu_open_id) DO UPDATE SET
                     feishu_user_id = EXCLUDED.feishu_user_id,
                     feishu_union_id = EXCLUDED.feishu_union_id,
                     display_name = EXCLUDED.display_name,
                     display_name_locale = EXCLUDED.display_name_locale,
                     department = EXCLUDED.department,
                     tenant_key = EXCLUDED.tenant_key,
                     -- 花名册字段的保留只对"同一个人"成立：feishu_user_id 变了
                     -- 说明账号复用换人（#34 方案 C 不拦截），旧人的工号/邮箱
                     -- 绝不能挂在新人身上——工号是匹配银河的主键，残留会把
                     -- 新人直接接到旧人的权限记录（独立复查发现）。
                     employee_no = CASE
                         WHEN app_user.feishu_user_id IS DISTINCT FROM EXCLUDED.feishu_user_id
                         THEN EXCLUDED.employee_no
                         ELSE COALESCE(EXCLUDED.employee_no, app_user.employee_no)
                     END,
                     email = CASE
                         WHEN app_user.feishu_user_id IS DISTINCT FROM EXCLUDED.feishu_user_id
                         THEN EXCLUDED.email
                         ELSE COALESCE(EXCLUDED.email, app_user.email)
                     END,
                     updated_at = now()
                RETURNING id, provisioning_state, permission_record_id, (xmax = 0) AS inserted""",
                (
                    new_id("usr"),
                    draft.feishu_open_id,
                    draft.feishu_user_id,
                    draft.feishu_union_id,
                    draft.display_name,
                    draft.display_name_locale,
                    draft.department,
                    draft.tenant_key,
                    draft.employee_no,
                    draft.email,
                    draft.provisioning_state,
                ),
            )
            row = cursor.fetchone()
        assert row is not None
        record = AppUserRecord(str(row[0]), str(row[1]), row[2], bool(row[3]))
        logger.info(
            "统一用户记录已写入 open_id=%s created=%s state=%s",
            redact_identifier(draft.feishu_open_id),
            record.created,
            record.provisioning_state,
        )
        return record

    def get_by_open_id(self, open_id: str) -> AppUserRecord | None:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, provisioning_state, permission_record_id FROM app_user WHERE feishu_open_id = %s",
                (open_id,),
            )
            row = cursor.fetchone()
        return None if row is None else AppUserRecord(str(row[0]), str(row[1]), row[2], False)

    def count(self) -> int:
        with self._psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM app_user")
            row = cursor.fetchone()
        return int(row[0]) if row else 0
