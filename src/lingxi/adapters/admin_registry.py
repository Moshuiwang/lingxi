"""管理员角色登记表（``admin_registry``，迁移 ``0067``）的唯一 PostgreSQL 落点。

三个职责：

1. :class:`PostgresAdminRegistryLookup` —— 供 :class:`lingxi.core.admin.router.
   AdminCommandRouter` 注入的默认拒绝判定端口，**每次调用都是一条新查询**，不持有
   连接、不缓存结果（"角色收回后新请求立即拒绝"这条不变量的数据库侧兑现）。
2. :class:`PostgresAdminQueries` —— 只读查询命令组的查询实现，只读 `app_user`/
   `inbound_event`/`local_permission_override`（后者只为 `user_status()` 回显
   「当前生效本地覆盖」段，#319 S-P-1b 卡 B）/`onboarding_failure`（`trace_lookup()`
   回显失败原因，Issue #337 迁移 `0077`），不提供任意 SQL 拼接入口。
3. :func:`seed_admin_registry_entry` —— 唯一的写路径，供
   ``apps/admin_bootstrap`` 一次性种子命令调用；幂等（同一 open_id 已有 active
   条目时不重复插入），本 Story 之外没有任何其它写入口。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.adapters.postgres_local_permission import PostgresLocalPermissionOverrideStore
from lingxi.adapters.postgres_onboarding_failure import fetch_failure_reason
from lingxi.core.admin.registry import (
    AdminRegistryEntry,
    AdminRegistrySeedConflict,
    AdminRole,
)
from lingxi.core.admin.views import (
    AdminEventView,
    AdminTraceView,
    AdminUserStatusView,
    LocalPermissionOverrideView,
)
from lingxi.core.ids import new_id


#: 从一行 ``SELECT feishu_open_id, label, permission_admin_granted,
#: ops_admin_granted, super_admin_granted, entry_status FROM admin_registry ...``
#: 构造 :class:`AdminRegistryEntry`——列的顺序与取值形状是本模块与调用方之间的唯一
#: 契约。导出（不加下划线前缀）供 ``adapters/postgres_pending_action.py`` 复用：
#: 那个模块的 ``confirm()`` 需要在**自己的连接、自己的事务**里对这张表加
#: ``FOR SHARE`` 重新查一次（避免独立注入的登记表查询端口带来的 TOCTOU 窗口，外部
#: 审查交叉裁定，codex P1-4），SQL 本身必须留在调用方以便携带那个连接特有的锁子句，
#: 但"行怎么解析成值对象"这段逻辑不必因此复制第二份。
def admin_registry_entry_from_row(row: tuple) -> AdminRegistryEntry:
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

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def active_entry(self, *, open_id: str) -> AdminRegistryEntry | None:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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


class PostgresAdminQueries:
    """``AdminQueries`` 端口的真实实现：只读 ``app_user``/``inbound_event``/
    ``local_permission_override``（#319 S-P-1b 卡 B：``/admin user`` 新增
    「当前生效本地覆盖」段，是 ``/admin revoke_permission`` 的 UX 前置）。"""

    def __init__(
        self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
    ) -> None:
        self._dsn = dsn
        self._timeouts = timeouts
        # 复用已有的读路径（``effective_entries``），不在本类里重新拼一遍同样的
        # SQL——见 ``core/admin/pending_action.py`` 与卡 B 设计卡「数据经
        # PostgresLocalPermissionOverrideStore.effective_entries（已有）」。
        self._local_overrides = PostgresLocalPermissionOverrideStore(dsn, timeouts=timeouts)

    def user_status(self, *, identifier: str) -> AdminUserStatusView | None:
        # 登记式收敛（#371-8，2026-08-28 复审订正——下面这段原始评估被 Trace #373
        # H1 批终修复包 opus 审查 P2-9 证明是伪二选一，订正见后半段）：
        # `PostgresAppUserStore`（postgres_identity.py）没有一个现成方法能一次原子
        # 查询出本方法需要的 (id, provisioning_state, account_state,
        # permission_version, updated_at) 五列——`get_by_open_id` 按 open_id 查但
        # 不带 account_state/updated_at，`read_status` 带 account_state 但按内部
        # user_id 查、且不返回 updated_at。拆成"先 get_by_open_id 拿 id，再
        # read_status 查状态"两次查询会丢掉单条 SELECT 的原子快照、多一次往返；
        # 给 core 的 UserProvisioningStatus 加 updated_at 会改动 onboarding_runner
        # 等其它消费方共享的形状。
        #
        # 订正：上面只权衡了"改 PostgresAppUserStore 的两种方式"，漏掉了第三条
        # 路——`apps/trace/__init__.py::_fetch_user` 存在逐字节相同的这条 SELECT
        # （同一张表、同一组列、同一个 WHERE），抽一个两边共用的只读 helper 本可
        # 行为等价地消掉这处重复，原「两条路径都不是……因此保留」的结论是伪二选一。
        # 本轮仍不抽取：`apps/trace` 的内联 SQL 刚在同一批里按产品负责人裁定登记为
        # 受控只读 CLI 例外（`docs/技术设计/代码框架.md` §二「在案例外」，
        # 2026-08-28，Issue #371）——这时候抽公共 helper 会让 trace 侧不再是"内联
        # SQL"，部分推翻刚落的登记，在同一批修复包里自相矛盾，因此本轮接受这一处
        # 重复。复议条件：下一次拆分批一并评估抽取两边共用的只读 helper，并同步
        # 修订 `docs/技术设计/代码框架.md` §二对应的例外条目（届时如果真的抽取，
        # trace 那条例外要么撤销，要么改写为"仅其余 3 条内联 SELECT"）。
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, provisioning_state, account_state, permission_version, updated_at
                  FROM app_user
                 WHERE feishu_open_id = %s
                """,
                (identifier,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        user_id, provisioning_state, account_state, permission_version, updated_at = row
        # 独立的一次读（``effective_entries`` 自己开连接）：只读查询不需要与上面
        # 这次 ``app_user`` 读共享事务，见该方法文档"供 S-P-3 聚合复用的读路径"。
        local_overrides = tuple(
            LocalPermissionOverrideView(
                override_id=stored.id,
                direction=stored.entry.direction.value,
                company_id=stored.entry.company_id,
                metric_name=stored.entry.metric_name,
                reason=stored.entry.reason,
                created_at=_isoformat(stored.entry.created_at),
            )
            for stored in self._local_overrides.effective_entries(user_id=user_id)
        )
        return AdminUserStatusView(
            identifier=identifier,
            provisioning_state=provisioning_state,
            account_state=account_state,
            permission_version=permission_version,
            updated_at=_isoformat(updated_at),
            local_overrides=local_overrides,
            # 「银河来源」展示（#439 B 档）本轮登记为跟进项，不在本 Story 内计算，
            # 见本卡交付报告"登记项"一节：现算一次银河来源需要花名册快照 + 银河
            # 快照 + core/permission 聚合层，会把这些目前只在 scheduler 进程使用
            # 的依赖拉进 gateway 进程的运行时 import 闭包（本机 `check_installed_
            # package.py` 已经如实拦下这一改动），是一次需要单独评估的依赖边界
            # 决策，不是本卡"最小改动"范围内能够顺手做的事。``None`` 与
            # ``router._render_galaxy_source``/``management_card._galaxy_source_
            # markdown`` 的既有"不可用"分支天然衔接，管理员看到的是诚实的"银河
            # 来源不可用"，不是编造的数据。
            galaxy_source=None,
        )

    def recent_events(
        self, *, identifier: str | None, window_hours: int, limit: int
    ) -> Sequence[AdminEventView]:
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """``/admin trace <追溯号>`` 的查询实现（Issue #337）。

        「按 trace_id 查 inbound_event」这条 SELECT 与 ``apps/trace/__init__.py::
        _fetch_events`` 的既有查询逐字节同型（同一张表、同一组列、同一个
        WHERE）——与本类 :meth:`user_status` 文档字符串登记的既有重复同一性质：
        ``apps/trace`` 的内联 SQL 已经在 2026-08-28（Issue #371）被产品负责人
        登记为「受控只读 CLI」的架构例外，本轮不重新抽取共用 helper（会推翻同一批
        刚落的登记，见该例外的复议条件）。本方法只负责"给定追溯号，查一次
        ``inbound_event``"，不 import ``apps.trace``——``core``/``adapters`` 层
        不依赖 ``apps`` 模块（代码框架第二/三节）。

        「开通状态」复用既有的 :meth:`user_status`（同一个已装配的适配器实例，
        不重新拼一遍 ``app_user`` 查询）；「失败原因」复用
        ``adapters/postgres_onboarding_failure.fetch_failure_reason``（写入方
        :class:`~lingxi.adapters.postgres_onboarding_failure.
        PostgresFailureReasonRecorder` 是同一张表的唯一写入口）。

        取「最近一条」事件的 ``event_type``/``handled_as``/``onboarding_
        dispatched_at`` 展示：同一个 trace_id 结构上通常只对应一条入站事件
        （首次开通首聊），取最近一条只是为了在理论上出现多条时不崩溃，不是
        本命令承诺聚合展示全部历史事件——完整时间线仍然是 ``/admin audit``
        与 ``python -m lingxi.apps.trace`` 的职责。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        )

    def resolve_identifier(self, *, identifier: str) -> str:
        """把邮箱形态的标识反查成 open_id（#439 A 档）；见 ``core/admin/router.
        AdminQueries.resolve_identifier`` 的完整契约文档。

        判据是"是否含 ``@``"——不是邮箱形态（不含 ``@``）时不发起任何查询，直接
        原样返回，既是既有多数调用（open_id 本来就不含 ``@``）的零成本路径，也
        避免把一次明显不是邮箱的输入误当邮箱去查。

        ``app_user.email`` 没有唯一约束（迁移基线只对 ``feishu_open_id`` 建
        UNIQUE），零命中或多命中都是"反查失败"，与 ``resolve_identifier`` 的
        既有契约一致：原样返回输入，交给下游按既有"未找到"语义处理。
        """

        if "@" not in identifier:
            return identifier
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """把中文别名反查成真正的指标 ID（#439 A 档）；见 ``core/admin/router.
        AdminQueries.resolve_metric_name`` 的完整契约文档。

        每次调用现读别名表（``adapters/admin_metric_alias_map_file.py``，见该
        模块文档"与 company_function_metric_map_file.py 的关键差异：现读，不
        缓存"）——管理命令面低频，现读成本可忽略，换来编辑别名表立即生效。
        """

        from lingxi.adapters.admin_metric_alias_map_file import load_admin_metric_alias_map

        aliases = load_admin_metric_alias_map()
        return aliases.get(metric_token, metric_token)

    def user_label(self, *, open_id: str) -> str:
        """``core.admin.display_names.AdminDisplayNames.user_label`` 真实实现
        （Trace #469 S-1）：把 open_id 翻译成「姓名（邮箱）」。``app_user.
        display_name`` 是建档时从飞书通讯录读到的官方姓名，不是花名册/银河
        导入的姓名列（数据库设计「姓名列只能用于内部诊断」约束的是后者，本方法
        不涉及）。查无此用户，或姓名邮箱均为空，返回通用占位——绝不把入参
        ``open_id`` 原样拼进返回值（合同"管理员可见文案零 ou_"）。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """``AdminDisplayNames.company_label`` 真实实现：按**当前有效银河批次**
        查 ``galaxy_country.name_cn``（``boss_company_id`` 连接，与
        ``core/permission/galaxy_scope.py`` 同一连接键取舍）。没有有效批次，或
        查无中文名，原样返回 ``company_id``——公司编号是业务代码，不是需要隐藏
        的内部系统标识，允许兜底展示。"""

        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return company_id
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT name_cn FROM galaxy_country WHERE batch_id = %s AND boss_company_id = %s LIMIT 1",
                (batch_id, company_id),
            )
            row = cursor.fetchone()
        name_cn = (row[0] or "").strip() if row is not None else ""
        return f"{name_cn}（{company_id}）" if name_cn else company_id

    def metric_label(self, *, metric_id: str) -> str:
        """``AdminDisplayNames.metric_label`` 真实实现：反查
        ``config/admin_metric_alias_map.toml``（别名 → 真实指标 ID）得到中文
        别名。与 :meth:`resolve_metric_name`（输入侧，中文别名 → 真实 ID）同一
        份文件、方向相反——每次调用现读，不缓存，同一姿态（该文件模块文档）。
        多个别名映射到同一个真实 ID 时任取一个（配置写入方职责，非本方法关注
        的正确性问题）；查无别名原样返回 ``metric_id``。"""

        from lingxi.adapters.admin_metric_alias_map_file import load_admin_metric_alias_map

        aliases = load_admin_metric_alias_map()
        for alias, real_id in aliases.items():
            if real_id == metric_id:
                return alias
        return metric_id

    def company_labels(self, *, company_ids: Sequence[str]) -> Mapping[str, str]:
        """``AdminDisplayNames.company_labels`` 真实实现（Trace #469 修复包 B，
        B-7：连接风暴收敛）。

        修复前：管理卡渲染每翻译一个公司编号就调用一次 :meth:`company_label`，
        该方法每次都新建两条连接（一次 ``PostgresGalaxyImportStore.
        current_batch_id()``、一次 ``galaxy_country`` 查询）——公司目录当前
        43 个编号，一张管理卡因此打开约 90 条连接（审查实测坐实）。

        修复后：整批编号只建**两条**连接——一次取当前批次号，一次用
        ``boss_company_id = ANY(%s)`` 一条 SQL 拿回全部命中的中文名，不随
        ``company_ids`` 的长度线性增长。查无有效批次或查无中文名的编号在
        返回映射里原样是该编号本身，与 :meth:`company_label` 的兜底语义
        完全一致——调用方用同一个 ``dict.get(id, id)`` 姿势消费即可。
        """

        if not company_ids:
            return {}
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return {company_id: company_id for company_id in company_ids}
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """``AdminDisplayNames.metric_labels`` 真实实现（Trace #469 修复包 B，
        B-7）：与 :meth:`metric_label` 同一份映射文件，只读**一次**（``load_
        admin_metric_alias_map`` 本身已经现读不缓存，批量只是把"文件读取"这
        一步从"每个指标各读一次"收敛成"整批读一次"），与逐个调用
        :meth:`metric_label` 结果逐项相同（含"多个别名映射到同一个真实 ID 时
        任取一个"的既有 tie-break：按别名表迭代顺序，先出现的别名生效）。
        """

        if not metric_ids:
            return {}
        from lingxi.adapters.admin_metric_alias_map_file import load_admin_metric_alias_map

        aliases = load_admin_metric_alias_map()
        alias_by_metric_id: dict[str, str] = {}
        for alias, real_id in aliases.items():
            alias_by_metric_id.setdefault(real_id, alias)
        return {
            metric_id: alias_by_metric_id.get(metric_id, metric_id) for metric_id in metric_ids
        }

    def resolve_override_id(
        self, *, open_id: str, company_id: str, metric_name: str
    ) -> str | None:
        """按「open_id + 公司 + 指标」反查当前生效的本地覆盖行 override_id（#439 A
        档，revoke 新参数形状）；见 ``core/admin/router.AdminQueries.
        resolve_override_id`` 的完整契约文档。

        先按 ``feishu_open_id`` 查出内部 ``app_user.id``（``local_permission_
        override.user_id`` 的口径，迁移 ``0072``），再复用既有的
        :meth:`~lingxi.adapters.postgres_local_permission.
        PostgresLocalPermissionOverrideStore.effective_entries` 取该用户全部
        当前生效覆盖，按 ``company_id``/``metric_name`` 过滤——不新写一条按三键
        联合查询的 SQL，理由与 ``core/admin/views.LocalPermissionOverrideView``
        文档"数据经……effective_entries（已有）"一致：复用已经过测试、已经正确
        处理 ``entry_status='active'`` 过滤的读路径，不重新拼一遍等价查询。

        目标用户不存在（``feishu_open_id`` 查无）、零命中或多命中（同一
        公司+指标理论上可能同时有一条生效授权与一条生效抑制，见迁移 ``0072``
        的唯一索引按 ``direction`` 再分）均返回 ``None``——不猜测该收回哪一条。
        """

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
    """幂等地登记一条管理员条目。三类角色固定合并授予，不接受角色子集。

    **不再接受 ``roles`` 入参**（opus 批量审查 P2）：PM 2026-08-24 终裁「三类角色
    合并授予」不该是调用方"记得传 `ALL_ADMIN_ROLES`"的自觉，而应该是这个函数
    结构上唯一能做到的事——本函数只负责"从零到一"的首次播种，播种的对象要么是
    三类角色齐全的管理员，要么不存在，没有第三种"部分角色"的中间态可以通过这个
    入口写出来。与迁移 ``0067`` 新增的 ``CHECK`` 是同一条终裁在两层的编码。

    ``ON CONFLICT`` 的推断目标精确匹配迁移 ``0067`` 的部分唯一索引
    ``admin_registry_active_identity_idx``：同一 ``feishu_open_id`` 已存在一条
    ``entry_status='active'`` 的行时本次不插入、不覆盖。**但"没插入"不能直接当成
    "幂等成功"**（opus 批量审查 P2）：那一行有可能是别的原因写进去的、字段并不是
    这次意图播种的内容（例如 label 不同，或——理论上——三类角色不全，尽管迁移
    ``0067`` 的 ``CHECK`` 已经在数据库层面挡住这种写入）。因此这里必须回读那一行、
    逐字段核验，核验不通过时抛 :class:`~lingxi.core.admin.registry.
    AdminRegistrySeedConflict`，调用方（``apps/admin_bootstrap``）据此非零退出并
    说明哪些字段不一致，而不是把一次真正的不一致误报成一次安静的成功。

    返回 ``True``：本次真的新插入了一行。返回 ``False``：已存在的行逐字段核验
    通过，是真正的幂等成功。

    这是本表**唯一**的写入口：不提供任何更新已有行的路径。调用方（
    ``apps/admin_bootstrap``）负责取得不含真实值的 ``feishu_open_id``（读取
    ``feishu_delegated_subject`` 登记表，见 Issue #137 同一识别机制），本函数本身
    不关心它从哪里来，也不做任何格式假设之外的校验。
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

        # 没插入：读回那一条已存在的 active 行，逐字段核验是不是真的"幂等成功"。
        # 查询条件精确匹配触发冲突的那个部分唯一索引（feishu_open_id +
        # entry_status='active'）——同一个索引保证这里至多读回一行，不需要
        # LIMIT，也不会因为历史撤销行而读错对象。
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

    if existing is None:
        # 结构上不应该发生：ON CONFLICT 命中即说明上面那一刻这一行存在，而本表
        # 唯一的写入口就是本函数本身，两条语句之间没有任何删除路径。响亮失败，
        # 好过把"读不到"悄悄当成某种默认结论。
        raise AdminRegistrySeedConflict(mismatched_fields=("row_disappeared_between_statements",))

    # 不再核对 entry_status：上面那条 SELECT 已经用 `entry_status = 'active'`
    # 过滤，读到行就意味着它是 'active'，再比一次是永远为真的死分支。
    existing_label, permission_granted, ops_granted, super_granted = existing
    mismatched: list[str] = []
    if existing_label != normalized_label:
        mismatched.append("label")
    if not (permission_granted and ops_granted and super_granted):
        mismatched.append("roles")
    if mismatched:
        raise AdminRegistrySeedConflict(mismatched_fields=tuple(mismatched))
    return False
