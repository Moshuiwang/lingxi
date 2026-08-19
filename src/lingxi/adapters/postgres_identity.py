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
from datetime import date, datetime, timezone
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
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
from lingxi.core.identity.provisioning import (
    ProvisioningRejection,
    ProvisioningRequest,
    ProvisioningResult,
    UserProvisioningStatus,
    classify_write_failure,
    missing_identity_fields,
)
from lingxi.core.ids import new_id

logger = logging.getLogger(__name__)

# 开通状态的推进次序（迁移 008 的 CHECK 是取值域，这里是**先后**）。只列首次开通链上
# 会经过的四格：`guest` 是建档默认值之前的形态，`manual_review` / `aborted` 不在自动
# 开通链上（产品负责人 2026-08-08 裁定：确定性失败统一无权限终态，不建人工核对）。
# 不在表里的状态一律拒绝推进，而不是当成"排在最前面"——后者会让一个拼错的状态名把
# 任何用户都推成 active。
_PROVISIONING_ORDER: dict[str, int] = {
    "guest": 0,
    "matching": 1,
    "provisioning": 2,
    "mcp_syncing": 3,
    "active": 4,
}


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
    employee_no: str | None = None
    email: str | None = None


class IdentityStorageIntegrityError(RuntimeError):
    """建档请求的花名册字段未按原值落库时拒绝继续。"""


class PostgresOrgSnapshotStore:
    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

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

        with connect(self._dsn, timeouts=self._timeouts) as connection:
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

    def has_complete_run_on(self, day: date) -> bool:
        """今天（UTC 日历日）是否已经有一轮 ``complete`` 批次（Issue #250 F8）。

        供 ``OrgSnapshotSyncDuty`` 把当日水位从纯内存变成对进程重启保持——查询
        本身不引入租约或领导权语义，只是读一次既有表。``started_at`` 显式转到
        UTC 再取日期，避免会话时区把"今天"判错。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM feishu_org_sync_run
                    WHERE status = 'complete'
                      AND (started_at AT TIME ZONE 'UTC')::date = %s
                    LIMIT 1""",
                (day,),
            )
            return cursor.fetchone() is not None

    def latest_complete_expiry(self) -> datetime | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def provision(self, request: ProvisioningRequest) -> ProvisioningResult:
        """Issue #89 写侧建档服务合同的 PostgreSQL 实现（`IdentityProvisioning`）。

        语义（幂等、结果与拒绝原因、事务边界）的正文在
        :mod:`lingxi.core.identity.provisioning`，本方法不重复叙述，只落实三件事：

        1. **不短路数据库的防线**：除了数据库故意不管的那一格（`blocking_gap`），
           请求照原样发给数据库，由「全有或全无」CHECK 与专用主体触发器拒绝，
           再把拒绝翻译成 :class:`ProvisioningRejection`；
        2. **不吞掉不认识的失败**：`classify_write_failure` 返回 `None` 时原样抛出，
           不归进任何兜底原因（否则一条未知约束会伪装成「无可用银河权限」）；
        3. **诊断不带身份原值**：日志与结果里只有字段名、原因码和脱敏后的 `open_id`。
        """

        # 只取异常基类，不碰 `psycopg` 顶层：正式代码里唯一的建连入口是
        # `lingxi.adapters.postgres.connect`，`check_db_timeouts.py` 会把任何对
        # `psycopg` / `psycopg.connection` 的导入判为绕过工厂。
        from psycopg.errors import Error as DriverError

        gap = request.blocking_gap
        if gap:
            # 数据库对「六字段全空」是放行的（账号删除完成后的合法形态），
            # `ON CONFLICT (feishu_open_id)` 对 NULL 也不去重：这一格只能写侧自己守。
            return self._rejected(
                request, ProvisioningRejection.INCOMPLETE_IDENTITY, missing_fields=gap
            )

        draft = request.to_draft()
        try:
            record = self.record_identity(draft)
        except IdentityStorageIntegrityError:
            return self._rejected(request, ProvisioningRejection.STORAGE_INTEGRITY)
        except DriverError as error:
            missing = missing_identity_fields(draft)
            rejection = classify_write_failure(
                sqlstate=error.sqlstate, message=str(error), missing_fields=missing
            )
            if rejection is None:
                raise
            return self._rejected(
                request,
                rejection,
                missing_fields=missing if rejection is ProvisioningRejection.INCOMPLETE_IDENTITY else (),
            )
        return (
            ProvisioningResult.created(record.id)
            if record.created
            else ProvisioningResult.already_provisioned(record.id)
        )

    def _rejected(
        self,
        request: ProvisioningRequest,
        rejection: ProvisioningRejection,
        *,
        missing_fields: tuple[str, ...] = (),
    ) -> ProvisioningResult:
        logger.warning(
            "建档被拒 open_id=%s reason=%s missing=%s storage_fault=%s",
            redact_identifier(request.identity.feishu_open_id),
            rejection.value,
            ",".join(missing_fields),
            rejection.is_storage_fault,
        )
        return ProvisioningResult.rejected(rejection, missing_fields=missing_fields)

    def record_identity(self, draft: IdentityRecordDraft) -> AppUserRecord:
        """按 ``feishu_open_id`` 建档或刷新资料。

        两条刻意不做的事：
        - **不写 ``permission_record_id`` / ``permission_version``**，连列都不出现在
          语句里，"先占位再回填"因此没有实现路径（断言 V-开通-01）；
        - 冲突时**不回写 ``provisioning_state``**，否则一个已经推进到发布或同步中的
          用户会因为再发一条消息被打回 ``matching``。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
                RETURNING id, provisioning_state, permission_record_id, employee_no, email,
                          (xmax = 0) AS inserted""",
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
            record = AppUserRecord(str(row[0]), str(row[1]), row[2], bool(row[5]), row[3], row[4])
            for field in ("employee_no", "email"):
                expected = getattr(draft, field)
                if expected is not None and getattr(record, field) != expected:
                    # 不能把模型里有、库里空（或被规范化改写）的身份键交给后续匹配。
                    # 异常发生在连接事务内，当前写入随事务回滚；日志和异常都不带字段值。
                    raise IdentityStorageIntegrityError(f"app_user {field} 持久化回读不一致")
        logger.info(
            "统一用户记录已写入 open_id=%s created=%s state=%s",
            redact_identifier(draft.feishu_open_id),
            record.created,
            record.provisioning_state,
        )
        return record

    def get_by_open_id(self, open_id: str) -> AppUserRecord | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, provisioning_state, permission_record_id, employee_no, email "
                "FROM app_user WHERE feishu_open_id = %s",
                (open_id,),
            )
            row = cursor.fetchone()
        return None if row is None else AppUserRecord(str(row[0]), str(row[1]), row[2], False, row[3], row[4])

    def read_status(self, user_id: str) -> UserProvisioningStatus | None:
        """回读该用户此刻的账号状态、开通状态与权限版本（Epic D / S-D-02）。

        首次开通编排在建档之后、创建用户环境与发布权限**之前**要用它复核一次——
        `already_provisioned` 不等于「这个人现在还该被开通」（[接口设计
        §8.1](../../../../docs/技术设计/接口设计.md)）。与 :meth:`get_by_open_id` 分开是
        因为那一个按飞书标识查、返回的是建档投影，这一个按内部标识查、返回的是**准入
        判据**；把两件事塞进同一个返回值会让调用方分不清自己拿到的是哪一份事实。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT account_state, provisioning_state, permission_version "
                "FROM app_user WHERE id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return UserProvisioningStatus(str(row[0]), str(row[1]), int(row[2]))

    def advance_provisioning_state(self, user_id: str, *, to: str) -> bool:
        """把 ``provisioning_state`` 往前推一格；**只前进不回退**（`V-开通-04`）。

        条件写在 SQL 的 ``WHERE`` 里，不在 Python 里先读后写：两条并发的开通链（对账
        重交接撞上用户的新消息）会各自读到同一个旧状态，于是后到的那一条可以把
        ``active`` 写回 ``provisioning``——一个已经开通完的用户因此重新变成"开通中"，
        问数被拒。放进条件更新之后，这种回退在数据库层面就发生不了。

        返回是否真的改了行。``False`` 有两种含义（目标状态不在推进表里、或当前状态已经
        不比目标靠前），两者对调用方是同一件事：**不需要推进**，因此不合并成异常。
        """

        if to not in _PROVISIONING_ORDER:
            raise ValueError("不认识的开通状态，拒绝推进")
        allowed = tuple(state for state, rank in _PROVISIONING_ORDER.items() if rank < _PROVISIONING_ORDER[to])
        if not allowed:  # pragma: no cover - 推进表的第一格不是任何一次推进的目标
            return False
        # 推到 ``active`` 还要求账号此刻是启用的：从建档后那次复核到就绪最长隔十七分钟
        # （发布等待 + 就绪预算），管理员在这段时间里停用账号是真实形状。写在 ``WHERE``
        # 里而不是在 Python 里先读后写——先读后写之间还有一个窗口，而这一步的后果是把
        # 一个已经被停用的人标成"开通完成"。
        guard = " AND account_state = 'enabled'" if to == "active" else ""
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app_user SET provisioning_state = %s, updated_at = now() "
                "WHERE id = %s AND provisioning_state = ANY(%s)" + guard,
                (to, user_id, list(allowed)),
            )
            changed = cursor.rowcount
        if changed:
            logger.info("开通状态推进 user=%s state=%s", user_id, to)
        return bool(changed)

    def count(self) -> int:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM app_user")
            row = cursor.fetchone()
        return int(row[0]) if row else 0
