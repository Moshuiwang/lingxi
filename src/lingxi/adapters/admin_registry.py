"""管理员角色登记表（``admin_registry``，迁移 ``0067``）的唯一 PostgreSQL 落点。

三个职责：

1. :class:`PostgresAdminRegistryLookup` —— 供 :class:`lingxi.core.admin.router.
   AdminCommandRouter` 注入的默认拒绝判定端口，**每次调用都是一条新查询**，不持有
   连接、不缓存结果（"角色收回后新请求立即拒绝"这条不变量的数据库侧兑现）。
2. :class:`PostgresAdminQueries` —— 只读查询命令组的查询实现，只读 `app_user`/
   `inbound_event`/`local_permission_override`（后者只为 `user_status()` 回显
   「当前生效本地覆盖」段）/`onboarding_failure`（`trace_lookup()` 回显失败
   原因），不提供任意 SQL 拼接入口。
3. :func:`seed_admin_registry_entry` —— 唯一的写路径，供
   ``apps/admin_bootstrap`` 一次性种子命令调用；幂等（同一 open_id 已有 active
   条目时不重复插入），本 Story 之外没有任何其它写入口。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_onboarding_failure import fetch_failure_reason
from lingxi.core.admin.registry import (
    AdminRegistryEntry,
    AdminRegistrySeedConflictError,
    AdminRole,
)
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    GalaxySourceSummary,
    LocalPermissionOverrideView,
)
from lingxi.core.ids import new_id


def admin_registry_entry_from_row(row: tuple) -> AdminRegistryEntry:
    """从一行 ``SELECT feishu_open_id, label, ..., entry_status`` 构造 :class:`AdminRegistryEntry`。

    列的顺序与取值形状是本模块与调用方之间的唯一契约。导出（不加下划线
    前缀）供 ``adapters/postgres_pending_action.py`` 复用：那个模块的
    ``confirm()`` 需要在**自己的连接、自己的事务**里对这张表加 ``FOR SHARE``
    重新查一次（避免独立注入的登记表查询端口带来的 TOCTOU 窗口），SQL 本身
    必须留在调用方以便携带那个连接特有的锁子句，但"行怎么解析成值对象"这段
    逻辑不必因此复制第二份。
    """
    found_open_id, label, permission_granted, ops_granted, super_granted, entry_status = row
    roles: set[AdminRole] = set()
    if permission_granted:
        roles.add(AdminRole.PERMISSION_ADMIN)
    if ops_granted:
        roles.add(AdminRole.OPS_ADMIN)
    if super_granted:
        roles.add(AdminRole.SUPER_ADMIN)
    return AdminRegistryEntry(
        feishu_open_id=found_open_id,
        label=label,
        roles=frozenset(roles),
        entry_status=entry_status,
    )


class PostgresAdminRegistryLookup:
    """``AdminRegistryLookup`` 端口的真实实现。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记录连接参数；不在构造期建立任何连接。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None:
        """查一条 active 登记；每次调用都是新查询，不缓存。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT feishu_open_id, label, permission_admin_granted,
                       ops_admin_granted, super_admin_granted, entry_status
                  FROM admin_registry
                 WHERE feishu_open_id = %s AND entry_status = 'active'
                """,
                (open_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return admin_registry_entry_from_row(row)

    def active_entries(self) -> tuple[AdminRegistryEntry, ...]:
        """列出全部 active 登记，按 open_id 排序。

        供受控运行脚本回答"把预检卡发给哪位管理员"。**不判定授权**：条目是不是
        一位真正的管理员仍由 ``core.admin.registry.is_authorized_admin`` 决定，
        这里只负责把行取回来，避免同一条默认拒绝谓词出现第二份实现。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT feishu_open_id, label, permission_admin_granted,
                       ops_admin_granted, super_admin_granted, entry_status
                  FROM admin_registry
                 WHERE entry_status = 'active'
                 ORDER BY feishu_open_id
                """
            )
            rows = cursor.fetchall()
        return tuple(admin_registry_entry_from_row(row) for row in rows)


class PostgresAdminQueries:
    """``AdminQueries`` 端口的真实实现：只读 ``app_user``/``inbound_event``/``local_permission_override``。

    ``local_permission_override`` 只为 ``user_status()`` 回显「当前生效本地
    覆盖」段，是 ``/admin revoke_permission`` 的 UX 前置；不提供任意 SQL
    拼接入口。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记录连接参数与复用的本地覆盖读路径。"""
        self._dsn = dsn
        self._timeouts = timeouts
        # 复用已有的读路径（``effective_entries``），不在本类里重新拼一遍同样的
        # SQL——见 ``core/admin/pending_action.py`` 与卡 B 设计卡「数据经
        # PostgresLocalPermissionOverrideStore.effective_entries（已有）」。
        self._local_overrides = PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts)

    def user_status(self, *, identifier: str) -> AdminUserStatusView | None:
        """按 open_id 查一条状态视图；查无返回 ``None``。

        这条 SELECT 与 ``apps/trace/__init__.py::_fetch_user`` 逐字节同型
        （同一张表、同一组列、同一个 WHERE）；`apps/trace` 的内联 SQL 已登记为
        受控只读 CLI 的架构例外（代码框架§二「在案例外」），本方法不因此抽取
        共用 helper，避免推翻同批刚落的登记。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT id, feishu_user_id, provisioning_state, account_state,
                       permission_version, updated_at
                  FROM app_user
                 WHERE feishu_open_id = %s
                """,
                (identifier,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        (
            user_id,
            feishu_user_id,
            provisioning_state,
            account_state,
            permission_version,
            updated_at,
        ) = row
        return AdminUserStatusView(
            identifier=identifier,
            provisioning_state=provisioning_state,
            account_state=account_state,
            permission_version=permission_version,
            updated_at=_isoformat(updated_at),
            local_overrides=self._local_override_views(user_id=user_id),
            # 只读现算银河来源摘要；这条路径不参与权限判定，也不改变任何数据。
            # 读不到任一快照/映射时返回明确的不可用摘要，不能把不可用解释成无权限。
            galaxy_source=self._galaxy_source_summary(feishu_user_id=feishu_user_id),
        )

    def _local_override_views(self, *, user_id: str) -> tuple[LocalPermissionOverrideView, ...]:
        """独立的一次读（``effective_entries`` 自己开连接），不与 ``app_user`` 共享事务。"""
        return tuple(
            LocalPermissionOverrideView(
                override_id=stored.id,
                direction=stored.entry.direction.value,
                company_id=stored.entry.company_id,
                metric_name=stored.entry.metric_name,
                reason=stored.entry.reason,
                created_at=_isoformat(stored.entry.created_at),
                position_name=stored.entry.position_name,
                company_scope=stored.entry.company_scope,
                group_id=stored.entry.permission_group_id,
            )
            for stored in self._local_overrides.effective_entries(user_id=user_id)
        )

    def _galaxy_source_summary(self, *, feishu_user_id: str | None) -> GalaxySourceSummary:
        """按当前持久快照现算管理员卡的银河来源摘要。

        该摘要是只读展示数据，权限发布仍只认 scheduler 的同一套聚合管线。快照缺失、
        映射损坏或身份无法唯一匹配时全部 fail-closed 为“暂时读不到”，而不是把
        不确定状态渲染成“没有权限”。异常不带人员字段进入日志，也不向调用方冒泡。
        """
        if not isinstance(feishu_user_id, str) or not feishu_user_id.strip():
            return GalaxySourceSummary(granted=False, reason="roster_snapshot_unavailable")
        try:
            from lingxi.adapters.postgres_galaxy_snapshot import PostgresGalaxySnapshotReader
            from lingxi.adapters.postgres_roster_snapshot import PostgresRosterSnapshotStore
            from lingxi.adapters.role_function_map_file import load_role_function_map
            from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
            from lingxi.core.permission.publish_row import aggregate_permission

            roster = PostgresRosterSnapshotStore(self._dsn, timeouts=self._timeouts).load()
            if roster is None:
                return GalaxySourceSummary(granted=False, reason="roster_snapshot_unavailable")
            galaxy = PostgresGalaxySnapshotReader(self._dsn, timeouts=self._timeouts).load_current()
            if galaxy is None:
                return GalaxySourceSummary(granted=False, reason="galaxy_snapshot_unavailable")
            role_map = load_role_function_map()
            if not role_map:
                return GalaxySourceSummary(granted=False, reason="role_function_map_unavailable")
            match = match_galaxy_account(feishu_user_id, roster.rows, galaxy.user_rows)
            if match.state != MATCHED or not match.galaxy_user_id:
                return GalaxySourceSummary(granted=False, reason=match.reason)
            aggregate = aggregate_permission(
                galaxy_user_id=match.galaxy_user_id,
                user_role_rows=galaxy.role_rows(match.galaxy_user_id),
                datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
                country_rows=galaxy.country_rows,
                role_function_map=role_map,
            )
            return GalaxySourceSummary(
                granted=aggregate.granted,
                reason=aggregate.reason,
                companies=aggregate.companies,
                functions=aggregate.functions,
                all_companies=aggregate.all_companies,
            )
        except Exception:  # display-only source must fail closed
            return GalaxySourceSummary(granted=False, reason="galaxy_snapshot_unavailable")

    def recent_events(
        self, *, identifier: str | None, window_hours: int, limit: int
    ) -> Sequence[AdminEventView]:
        """按可选 identifier 与时间窗查最近若干条入站事件。"""
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            if identifier:
                cursor.execute(
                    """
                    SELECT received_at, event_type, handled_as, trace_id
                      FROM inbound_event
                     WHERE user_open_id = %s
                       AND received_at >= now() - make_interval(hours => %s)
                     ORDER BY received_at DESC
                     LIMIT %s
                    """,
                    (identifier, window_hours, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT received_at, event_type, handled_as, trace_id
                      FROM inbound_event
                     WHERE received_at >= now() - make_interval(hours => %s)
                     ORDER BY received_at DESC
                     LIMIT %s
                    """,
                    (window_hours, limit),
                )
            rows = cursor.fetchall()
        return tuple(
            AdminEventView(
                received_at=_isoformat(received_at),
                event_type=event_type,
                handled_as=handled_as,
                trace_id=trace_id,
            )
            for received_at, event_type, handled_as, trace_id in rows
        )

    def trace_lookup(self, *, trace_id: str) -> AdminTraceView | None:
        """``/admin trace <追溯号>`` 的查询实现；同一性质的 SQL 重复见 :meth:`user_status`。

        「开通状态」复用 :meth:`user_status`；「失败原因」复用
        ``postgres_onboarding_failure.fetch_failure_reason``；「任务/文档
        投递收口结果」见 :meth:`_trace_task`。取"最近一条"事件展示——同一
        trace_id 结构上通常只对应一条，取最近一条只为理论多条时不崩溃，
        不聚合全部历史。已知取舍：两个 JOIN 键都没有索引，是可接受的顺序
        扫描——本命令是管理员手工低频操作，不划算为它新建索引。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT received_at, event_type, handled_as, user_open_id,
                       onboarding_dispatched_at
                  FROM inbound_event
                 WHERE trace_id = %s
                 ORDER BY received_at
                """,
                (trace_id,),
            )
            rows = cursor.fetchall()
        if not rows:
            return None

        event_count = len(rows)
        first_received_at = _isoformat(rows[0][0])
        _, last_event_type, last_handled_as, last_open_id, last_dispatched_at = rows[-1]

        status = self.user_status(identifier=last_open_id) if last_open_id else None
        failure = fetch_failure_reason(self._dsn, trace_id=trace_id, timeouts=self._timeouts)
        task = self._trace_task(trace_id=trace_id)

        return AdminTraceView(
            trace_id=trace_id,
            event_count=event_count,
            first_received_at=first_received_at,
            last_event_type=last_event_type,
            last_handled_as=last_handled_as,
            dispatched=last_dispatched_at is not None,
            provisioning_state=status.provisioning_state if status is not None else None,
            account_state=status.account_state if status is not None else None,
            failure_reason=failure.failure_reason if failure is not None else None,
            failure_event_type=failure.event_type if failure is not None else None,
            failure_occurred_at=failure.occurred_at if failure is not None else None,
            task_status=task[0] if task is not None else None,
            task_error_kind=task[1] if task is not None else None,
            task_failure_code=task[2] if task is not None else None,
            task_failure_signature=task[3] if task is not None else None,
            task_ended_at=_isoformat(task[4]) if task is not None and task[4] is not None else None,
            document_delivery_status=task[5] if task is not None else None,
            document_delivery_last_error=task[6] if task is not None else None,
            document_body_degraded_reason=task[7] if task is not None else None,
        )

    def _trace_task(
        self, *, trace_id: str
    ) -> (
        tuple[
            str,
            str | None,
            str | None,
            str | None,
            object,
            str | None,
            str | None,
            str | None,
        ]
        | None
    ):
        """按追溯号取这条入站事件派生的**最近一个**任务与文档投递收口结果。

        查不到返回 ``None``——这条追溯号没有派生任何任务是完全正常的情形
        （管理命令、未开通用户、重复投递事件都不入队），不是错误，调用方据此
        在回显里省掉整段而不是显示一堆空值。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT task.status, task.error_kind, task.failure_code,
                       task.failure_signature, task.ended_at,
                       delivery.status, delivery.last_error,
                       delivery.body_degraded_reason
                  FROM task
                  JOIN inbound_event
                    ON inbound_event.feishu_event_id = task.inbound_event_id
                  LEFT JOIN task_document_delivery_request AS delivery
                    ON delivery.task_id = task.id
                 WHERE inbound_event.trace_id = %s
                 ORDER BY task.created_at DESC, delivery.created_at DESC NULLS LAST
                 LIMIT 1
                """,
                (trace_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
        )

    def resolve_identifier(self, *, identifier: str) -> str:
        """把邮箱形态的标识反查成 open_id；查询失败与"零命中/多命中"都原样返回输入。

        判据是"是否含 ``@``"：不是邮箱形态时不发起任何查询，直接原样返回，
        既是零成本路径，也避免把明显不是邮箱的输入误当邮箱去查。迁移 ``0085``
        给规范化邮箱加了部分唯一索引后"多命中"在全链迁移库上结构性不可能，
        但分支仍然保留——它是旧库与索引被删场景下唯一的防线，猜错一条的
        后果是把管理动作落到另一个人身上。比较是**逐字相等**，不做归一化。
        """
        if "@" not in identifier:
            return identifier
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT feishu_open_id FROM app_user"
                " WHERE email = %s AND feishu_open_id IS NOT NULL",
                (identifier,),
            )
            rows = cursor.fetchall()
        if len(rows) == 1:
            return rows[0][0]
        return identifier

    def resolve_metric_name(self, *, metric_token: str) -> str:
        """把中文别名反查成真正的指标 ID。

        每次调用现读别名表（管理命令面低频，现读成本可忽略，换来编辑别名表
        立即生效，见 ``admin_metric_alias_map_file`` 模块文档）。
        """
        from lingxi.adapters.admin_metric_alias_map_file import load_admin_metric_alias_map

        aliases = load_admin_metric_alias_map()
        return aliases.get(metric_token, metric_token)

    def user_label(self, *, open_id: str) -> str:
        """把 open_id 翻译成「姓名（邮箱）」；``display_name`` 是建档时从飞书通讯录读到的官方姓名。

        查无此用户，或姓名邮箱均为空，返回通用占位——绝不把入参 ``open_id``
        原样拼进返回值（合同"管理员可见文案零 ou_"）。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT display_name, email FROM app_user WHERE feishu_open_id = %s",
                (open_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return "该用户"
        display_name = (row[0] or "").strip()
        email = (row[1] or "").strip()
        if display_name and email:
            return f"{display_name}（{email}）"
        return display_name or email or "该用户"

    def company_label(self, *, company_id: str) -> str:
        """按**当前有效银河批次**查 ``galaxy_country.name_cn`` 翻译公司编号。

        没有有效批次，或查无中文名，原样返回 ``company_id``——公司编号是业务
        代码，不是需要隐藏的内部系统标识，允许兜底展示。
        """
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return company_id
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT name_cn FROM galaxy_country WHERE batch_id = %s AND boss_company_id = %s LIMIT 1",
                (batch_id, company_id),
            )
            row = cursor.fetchone()
        name_cn = (row[0] or "").strip() if row is not None else ""
        return f"{name_cn}（{company_id}）" if name_cn else company_id

    def metric_label(self, *, metric_id: str) -> str:
        """反查别名表把真实指标 ID 翻译成中文别名；与 :meth:`resolve_metric_name` 方向相反。

        每次调用现读，不缓存，同一姿态。多个别名映射到同一个真实 ID 时任取
        一个（配置写入方职责）；查无别名原样返回 ``metric_id``。
        """
        from lingxi.adapters.admin_metric_alias_map_file import load_admin_metric_alias_map

        aliases = load_admin_metric_alias_map()
        for alias, real_id in aliases.items():
            if real_id == metric_id:
                return alias
        return metric_id

    def company_labels(self, *, company_ids: Sequence[str]) -> Mapping[str, str]:
        """批量翻译公司编号，整批只建两条连接，不随数量线性增长连接数。

        逐个调用 :meth:`company_label` 会让一张管理卡打开与编号数成正比的
        连接；这里改成一次取当前批次号、一次用 ``ANY(%s)`` 拿回全部命中的
        中文名。查无有效批次或查无中文名的编号原样是该编号本身，与
        :meth:`company_label` 的兜底语义完全一致。
        """
        if not company_ids:
            return {}
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return {company_id: company_id for company_id in company_ids}
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT boss_company_id, name_cn FROM galaxy_country"
                " WHERE batch_id = %s AND boss_company_id = ANY(%s)",
                (batch_id, list(company_ids)),
            )
            rows = cursor.fetchall()
        name_cn_by_id = {row[0]: (row[1] or "").strip() for row in rows}
        return {
            company_id: (
                f"{name_cn_by_id[company_id]}（{company_id}）"
                if name_cn_by_id.get(company_id)
                else company_id
            )
            for company_id in company_ids
        }

    def metric_labels(self, *, metric_ids: Sequence[str]) -> Mapping[str, str]:
        """批量翻译指标 ID，与 :meth:`metric_label` 同一份映射文件，只读一次。

        批量只是把"文件读取"这一步从"每个指标各读一次"收敛成"整批读一次"，
        结果与逐个调用 :meth:`metric_label` 逐项相同（含"多个别名映射到同一
        个真实 ID 时任取一个"的 tie-break：按别名表迭代顺序，先出现的生效）。
        """
        if not metric_ids:
            return {}
        from lingxi.adapters.admin_metric_alias_map_file import load_admin_metric_alias_map

        aliases = load_admin_metric_alias_map()
        alias_by_metric_id: dict[str, str] = {}
        for alias, real_id in aliases.items():
            alias_by_metric_id.setdefault(real_id, alias)
        return {metric_id: alias_by_metric_id.get(metric_id, metric_id) for metric_id in metric_ids}

    def resolve_override_id(self, *, open_id: str, company_id: str, metric_name: str) -> str | None:
        """按「open_id + 公司 + 指标」反查当前生效的本地覆盖行 override_id。

        先查出内部 ``app_user.id``，再复用既有的
        :meth:`~lingxi.adapters.postgres_local_permission.
        PostgresLocalPermissionOverrideStore.effective_entries` 取该用户全部
        当前生效覆盖并过滤，不新写一条按三键联合查询的 SQL——复用已测试过、
        已正确处理 ``entry_status='active'`` 的读路径。目标用户不存在、零
        命中或多命中（同一公司+指标可能同时有一条生效授权与一条生效抑制）
        均返回 ``None``——不猜测该收回哪一条。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT id FROM app_user WHERE feishu_open_id = %s",
                (open_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        user_id = row[0]

        matches = [
            stored.id
            for stored in self._local_overrides.effective_entries(user_id=user_id)
            if stored.entry.company_id == company_id and stored.entry.metric_name == metric_name
        ]
        if len(matches) == 1:
            return matches[0]
        return None


def _isoformat(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def seed_admin_registry_entry(
    dsn: str,
    *,
    feishu_open_id: str,
    label: str,
    timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
) -> bool:
    """幂等地登记一条管理员条目。三类角色固定合并授予，不接受角色子集入参。

    只负责"从零到一"的首次播种：播种对象要么是三类角色齐全的管理员，要么
    不存在，没有"部分角色"的中间态（与迁移 ``0067`` 的 ``CHECK`` 同一约束
    两层编码）。``ON CONFLICT`` 命中时不插入不覆盖，但"没插入"不能直接当
    "幂等成功"——须回读逐字段核验，不通过抛 :class:`~lingxi.core.admin.
    registry.AdminRegistrySeedConflictError`。返回 ``True`` 表示新插入；``False``
    表示既有行核验通过。这是本表**唯一**的写入口。
    """
    if not feishu_open_id or not feishu_open_id.strip():
        raise ValueError("feishu_open_id 不能为空")
    if not label or not label.strip():
        raise ValueError("label 不能为空")

    normalized_open_id = feishu_open_id.strip()
    normalized_label = label.strip()

    row_id = new_id("adm")
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO admin_registry
                (id, feishu_open_id, label, permission_admin_granted,
                 ops_admin_granted, super_admin_granted, entry_status)
            VALUES (%s, %s, %s, TRUE, TRUE, TRUE, 'active')
            ON CONFLICT (feishu_open_id) WHERE entry_status = 'active' DO NOTHING
            RETURNING id
            """,
            (row_id, normalized_open_id, normalized_label),
        )
        if cursor.fetchone() is not None:
            return True

        # 没插入：读回那一条已存在的 active 行核验；查询条件精确匹配触发
        # 冲突的那个部分唯一索引，因此至多读回一行，不需要 LIMIT。
        cursor.execute(
            """
            SELECT label, permission_admin_granted, ops_admin_granted,
                   super_admin_granted
              FROM admin_registry
             WHERE feishu_open_id = %s AND entry_status = 'active'
            """,
            (normalized_open_id,),
        )
        existing = cursor.fetchone()

    _verify_seeded_row(existing, normalized_label=normalized_label)
    return False


def _verify_seeded_row(existing: tuple | None, *, normalized_label: str) -> None:
    """核验"没插入"时回读的既有行是否真的是幂等成功；不通过抛冲突错误。"""
    if existing is None:
        # 结构上不应该发生：ON CONFLICT 命中即说明上面那一刻这一行存在，而本表
        # 唯一的写入口就是本函数本身，两条语句之间没有任何删除路径。响亮失败，
        # 好过把"读不到"悄悄当成某种默认结论。
        raise AdminRegistrySeedConflictError(
            mismatched_fields=("row_disappeared_between_statements",)
        )

    # 不再核对 entry_status：上面那条 SELECT 已经用 `entry_status = 'active'`
    # 过滤，读到行就意味着它是 'active'，再比一次是永远为真的死分支。
    existing_label, permission_granted, ops_granted, super_granted = existing
    mismatched: list[str] = []
    if existing_label != normalized_label:
        mismatched.append("label")
    if not (permission_granted and ops_granted and super_granted):
        mismatched.append("roles")
    if mismatched:
        raise AdminRegistrySeedConflictError(mismatched_fields=tuple(mismatched))
