"""按当前有效批次读回银河权限快照。

导入侧（``adapters.galaxy_import``）把一份银河导出整批写进五张 ``galaxy_*`` 表并
维持"当前有效批次唯一"；解释侧（``core.permission`` 的纯函数）只接收行、不读库。
本模块补上两者之间缺的一层：把"当前这一批的行"取出来。

**三条边界**：只读，不建批次也不改批次状态（哪一批有效是导入层的职责
`V-银河-06`，这里只调用它的判定、不复制那条 SQL）；没有有效批次就如实返回
``None``，不回落到"最近一批"、不猜；表名只有一处，全部取自
``core.permission.galaxy_export.TARGET_TABLES``，与导入器写入时同一份常量。

只取解释层真正会读的列，见各 ``*_COLUMNS`` 常量旁的说明；两张关联表按
``user_id`` 预先分组后再交给聚合，见 :func:`_group_by_user`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.permission.galaxy_export import TARGET_TABLES

logger = logging.getLogger(__name__)

#: 四条读取语句的列清单，只取解释层真正会读的列。列顺序即下面构造行映射时的键
#: 顺序，改一处必须改另一处。刻意不取 ``galaxy_user.nick_name``（中文姓名，只
#: 供人工核对的 advisory，聚合从不读它）；不取 ``galaxy_user_role.role_id``/
#: ``source_user_name``（后者实测是中文姓名，列名具有误导性，不参与聚合）。
USER_COLUMNS: tuple[str, ...] = ("user_id", "user_name", "email")
USER_ROLE_COLUMNS: tuple[str, ...] = ("user_id", "role_name")
#: 公司范围的连接键。
USER_DATACOUNTRY_COLUMNS: tuple[str, ...] = ("user_id", "datacountry_id")
#: ``core.permission.galaxy_scope.resolve_company_scope`` 读的正是这四列
#: （``name``/``name_cn`` 还用于校验"全非"哨兵行的形态）。
COUNTRY_COLUMNS: tuple[str, ...] = ("country_key", "name", "name_cn", "boss_company_id")


def _select(source_table: str, columns: Sequence[str]) -> str:
    """按源表名与列名拼一条整批读取语句。

    表名来自 :data:`TARGET_TABLES`（与导入器同一份常量），列名是本模块的字面量；
    两者都不来自外部输入，因此这里的字符串拼接不构成注入面——参数只有 ``batch_id``
    一个，且走占位符。
    """
    return f"SELECT {', '.join(columns)} FROM {TARGET_TABLES[source_table]} WHERE batch_id = %s"


def _text(value: Any) -> str:
    """分组键的归一：``NULL`` 与空白归空串，与解释层 ``_required_text`` 同口径。"""
    if value is None:
        return ""
    return str(value).strip()


def _rows(
    cursor: Any, statement: str, batch_id: str, columns: Sequence[str]
) -> tuple[dict[str, Any], ...]:
    cursor.execute(statement, (batch_id,))
    return tuple(dict(zip(columns, row)) for row in cursor.fetchall())


def _group_by_user(rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Mapping[str, Any], ...]]:
    """按 ``user_id`` 预先分组，供聚合时只把该账号那一份交给纯函数。

    这不是把判定搬进适配器：纯函数仍会按 ``user_id`` 再过滤一次，分组只可能
    少给行、不可能多给——少给的方向是失败关闭（少一个角色/国家最坏是无权限，
    多给才是越权）。``galaxy_user``/``galaxy_country`` 不参与分组、整批交出：
    前者是匹配层判断命中条数的依据，提前收窄可能把"命中多条→不发权限"变成
    "唯一命中→发权限"；后者的通配展开本身要看全表（含没有 ``country_key``
    的行与哨兵行）。
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        key = _text(row.get("user_id"))
        if not key:
            # 没有账号标识的行连不到任何人。导入层的必填校验本来就挡着它，
            # 这里丢弃只是让分组键不出现空串这一个"看起来像所有人"的桶。
            continue
        grouped.setdefault(key, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


@dataclass(frozen=True)
class GalaxyPermissionSnapshot:
    """当前有效批次里，聚合权限所需的全部行。

    构造出来就是自洽的一份：四类行同属 :attr:`batch_id` 那一批，读取期间批次仍然有效
    （读完复核过）。**没有任何解释**——「这些角色对应什么职能」「这些国家对应哪些公司」
    一律由 :mod:`lingxi.core.permission` 的纯函数回答。
    """

    batch_id: str
    user_rows: tuple[Mapping[str, Any], ...]
    country_rows: tuple[Mapping[str, Any], ...]
    role_rows_by_user: Mapping[str, tuple[Mapping[str, Any], ...]]
    datacountry_rows_by_user: Mapping[str, tuple[Mapping[str, Any], ...]]

    def role_rows(self, galaxy_user_id: Any) -> tuple[Mapping[str, Any], ...]:
        """该银河账号的角色行；没有就是空元组（该账号一个角色都没有）。"""
        return self.role_rows_by_user.get(_text(galaxy_user_id), ())

    def datacountry_rows(self, galaxy_user_id: Any) -> tuple[Mapping[str, Any], ...]:
        """该银河账号的数据国家授权行；没有就是空元组。"""
        return self.datacountry_rows_by_user.get(_text(galaxy_user_id), ())

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实：**只有批次标识与计数**。

        批次标识是一个内部生成的随机串（``gib_…``），不含人员数据；四个计数同理。
        任何一行的字段值都不在这里（纪律同 `V-银河-13`）。
        """
        return {
            "batch": self.batch_id,
            "galaxy_users": len(self.user_rows),
            "galaxy_countries": len(self.country_rows),
            "galaxy_users_with_roles": len(self.role_rows_by_user),
            "galaxy_users_with_countries": len(self.datacountry_rows_by_user),
        }


class PostgresGalaxySnapshotReader:
    """当前有效批次的只读读取口。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def current_batch_id(self) -> str | None:
        """当前有效批次标识；没有新鲜批次时返回 ``None``。

        **委托给导入层**而不是自己写一条 SQL：「未过期的最近一个 ``complete``」是
        `V-银河-06` 的规则，全仓库只允许有一处实现。延迟 import 是本仓库既有约定
        （``src/lingxi/`` 里没有模块级第三方 import；导入层构造时要 ``psycopg``）。
        """
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        return PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()

    def _read_batch_rows(
        self, batch_id: str
    ) -> tuple[
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
    ]:
        """读一批的四类行：账号、角色、数据国家授权、国家。

        供 :meth:`load_current` 在批次校验前后各调一次 :meth:`current_batch_id`。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            user_rows = _rows(cursor, _select("user", USER_COLUMNS), batch_id, USER_COLUMNS)
            role_rows = _rows(
                cursor, _select("user_role", USER_ROLE_COLUMNS), batch_id, USER_ROLE_COLUMNS
            )
            datacountry_rows = _rows(
                cursor,
                _select("sys_user_datacountry", USER_DATACOUNTRY_COLUMNS),
                batch_id,
                USER_DATACOUNTRY_COLUMNS,
            )
            country_rows = _rows(
                cursor, _select("sys_country", COUNTRY_COLUMNS), batch_id, COUNTRY_COLUMNS
            )
        return user_rows, role_rows, datacountry_rows, country_rows

    def load_current(self) -> GalaxyPermissionSnapshot | None:
        """读回当前有效批次的四类行；**没有有效批次时返回 ``None``**。

        ``None`` 表示"现在算不出权限"，与"批次里一行都没有"（会返回一份空快照，
        是数据事实）不同。四条查询各自在 READ COMMITTED 下取一次快照，读完后
        **重新问一次当前有效批次是哪一批**：不一致就整体判不可用——可能是新导入
        完成覆盖了旧批次、批次过期，或保留清理级联删掉了子表行；避免发布内容
        一部分来自旧批次、一部分来自新批次。这道核对不保证"发布的一定是最新
        一批"（核对之后到写入之间仍有窗口，交给下一轮日频刷新兜底），只保证
        "这一轮读到的四类行来自同一批"。
        """
        batch_id = self.current_batch_id()
        if batch_id is None:
            logger.warning("没有当前有效的银河批次，本次读取不可用")
            return None

        user_rows, role_rows, datacountry_rows, country_rows = self._read_batch_rows(batch_id)

        confirmed = self.current_batch_id()
        if confirmed != batch_id:
            logger.warning(
                "银河当前有效批次在读取期间改变，本次读取不可用 selected=%s now=%s",
                batch_id,
                confirmed,
            )
            return None

        snapshot = GalaxyPermissionSnapshot(
            batch_id=batch_id,
            user_rows=user_rows,
            country_rows=country_rows,
            role_rows_by_user=_group_by_user(role_rows),
            datacountry_rows_by_user=_group_by_user(datacountry_rows),
        )
        # 只记批次标识与计数：任何一行的字段值都不进日志（`V-银河-13`）。
        logger.info(
            "银河权限快照已读回 batch=%s 账号=%s 角色行=%s 国家授权行=%s 国家=%s",
            batch_id,
            len(user_rows),
            len(role_rows),
            len(datacountry_rows),
            len(country_rows),
        )
        return snapshot


class PostgresCompanyNames:
    """``core.permission.notification.CompanyNameResolver`` 的真实实现（结构性实现，不继承）。

    按当前有效银河批次查 ``galaxy_country.name_cn``，与
    ``adapters/admin_registry.PostgresAdminQueries.company_label`` 是同一份查询
    姿势，独立各自维护、不共享实现（调用面不同：一处是管理员命令面展示，一处是
    权限变化通知）。只查 ``galaxy_country`` 一张表，不用
    :meth:`PostgresGalaxySnapshotReader.load_current`——那个方法额外读三张关联
    表，本类每次调用只需要一次轻量读取，没有理由多付那三张表的读取成本
    （``PermissionPublishDuty`` 是高频 tick，代价差异会被放大）。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        """记下 DSN 与超时配置；不在构造时连接数据库。"""
        self._dsn = dsn
        self._timeouts = timeouts

    def name_for(self, *, company_id: str) -> str | None:
        """按单个公司编号查中文名；查无该批次或该编号时返回 ``None``。"""
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return None
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT name_cn FROM galaxy_country WHERE batch_id = %s AND boss_company_id = %s LIMIT 1",
                (batch_id, company_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        name_cn = (row[0] or "").strip()
        return name_cn or None

    def names_for(self, *, company_ids: Sequence[str]) -> Mapping[str, str | None]:
        """``CompanyNameResolver.names_for`` 真实实现。

        与 ``adapters/admin_registry.PostgresAdminQueries.company_labels`` 同一份
        批量查询姿势，独立各自维护（同上一条注释理由）。逐个编号各调一次
        :meth:`name_for` 会为权限文档里每个公司编号新建两条连接，公司位较多时
        重演 ``core/admin`` 侧同一种连接风暴；这里整批编号只建两条连接。查无
        中文名的编号在返回映射里是 ``None``（与 :meth:`name_for` 语义一致，
        不是空字符串），空输入返回空映射、不发起任何查询。
        """
        if not company_ids:
            return {}
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return {company_id: None for company_id in company_ids}
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
        name_cn_by_id = {row[0]: ((row[1] or "").strip() or None) for row in rows}
        return {company_id: name_cn_by_id.get(company_id) for company_id in company_ids}


__all__ = [
    "GalaxyPermissionSnapshot",
    "PostgresCompanyNames",
    "PostgresGalaxySnapshotReader",
]
