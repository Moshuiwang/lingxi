"""按**当前有效批次**读回银河权限快照（Issue [#156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-03a）。

导入侧（:mod:`lingxi.adapters.galaxy_import`）把一份银河导出整批写进五张 ``galaxy_*``
表并维持「当前有效批次唯一」；解释侧（:mod:`lingxi.core.permission.galaxy_scope`、
:mod:`lingxi.core.permission.publish_row`）是纯函数，只接收行、不读库。**两者之间原本
没有任何东西**——每日权限重算要拿到「当前这一批的行」就缺这一层，本模块补上它。

## 三条边界

1. **只读**。本模块一个写语句都没有，也不建批次、不改批次状态：「哪一批有效」是导入层
   的职责（`V-银河-06`），这里只**调用**它的 :meth:`~lingxi.adapters.galaxy_import.
   PostgresGalaxyImportStore.current_batch_id`，不复制那条 SQL——「未过期的最近一个
   ``complete``」是一条产品规则，两处各写一份迟早会分叉。
2. **没有有效批次就如实说不可用**（返回 ``None``），不回落到"最近一批"、不猜。银河快照
   过期或从未导入时，唯一安全的结论是"现在算不出任何人的权限"，而不是拿一份可能已经
   失效的权限去发布。
3. **表名只有一处**：全部取自 :data:`lingxi.core.permission.galaxy_export.TARGET_TABLES`，
   与导入器写入时用的是同一份常量。

## 取哪些列

只取解释层真正会读的列（纪律同 :mod:`lingxi.adapters.postgres_roster_audit`：
「取的列就是要用的列」）：

- ``galaxy_user``：``user_id`` / ``user_name`` / ``email``——账号匹配的三个键。
  **刻意不取 ``nick_name``**：它是中文姓名，:func:`~lingxi.core.permission.
  account_match.match_galaxy_account` 只把它放进 ``advisory`` 供人工核对，而每日重算
  从不读那个字段。少取一列就是少一份可识别数据进内存与进程日志的可能。
- ``galaxy_user_role``：``user_id`` / ``role_name``——角色名映射的输入。``role_id``
  与 ``source_user_name`` 都不参与聚合（后者的值实测是中文姓名，列名具有误导性）。
- ``galaxy_user_datacountry``：``user_id`` / ``datacountry_id``——公司范围的连接键。
- ``galaxy_country``：``country_key`` / ``name`` / ``name_cn`` / ``boss_company_id``
  ——:func:`~lingxi.core.permission.galaxy_scope.resolve_company_scope` 读的正是这四列
  （``name``/``name_cn`` 用于校验「全非」哨兵行的形态）。

## 为什么按 ``user_id`` 预先分组

两张关联表（角色、数据国家）在读回时按 ``user_id`` 分组成索引，聚合时只把该账号那一份
交给纯函数。这**不是把判定搬进适配器**：纯函数仍然按 ``user_id`` 再过滤一次，因此分组
只可能**少给**行、不可能多给——而少给的方向是失败关闭（少一个角色 / 少一个国家，
最坏是无权限），多给才是越权。真实批次量级（2026-08-06 导入实测：user 331 /
user_role 1574 / role_menu 3334 / user_datacountry 6517 / country 58）下不分组也跑得完，
分组是为了让「按人取行」的代价与在职员工人数无关。

``galaxy_user`` 与 ``galaxy_country`` **不分组、整批交出**：前者是匹配层判断"命中几条"
的依据，任何提前收窄都可能把"命中多条 → 不发权限"悄悄变成"唯一命中 → 发权限"，方向
正好是越权；后者的通配展开本来就要看全表（含没有 ``country_key`` 的行与哨兵行）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.permission.galaxy_export import TARGET_TABLES

logger = logging.getLogger(__name__)

#: 四条读取语句。列顺序即下面构造行映射时的键顺序，改一处必须改另一处。
USER_COLUMNS: tuple[str, ...] = ("user_id", "user_name", "email")
USER_ROLE_COLUMNS: tuple[str, ...] = ("user_id", "role_name")
USER_DATACOUNTRY_COLUMNS: tuple[str, ...] = ("user_id", "datacountry_id")
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


def _rows(cursor: Any, statement: str, batch_id: str, columns: Sequence[str]) -> tuple[dict[str, Any], ...]:
    cursor.execute(statement, (batch_id,))
    return tuple(dict(zip(columns, row)) for row in cursor.fetchall())


def _group_by_user(rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Mapping[str, Any], ...]]:
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

    def load_current(self) -> GalaxyPermissionSnapshot | None:
        """读回当前有效批次的四类行；**没有有效批次时返回 ``None``**。

        ``None`` 是一个明确的"现在算不出权限"，调用方据此整轮失败关闭。它与"批次里
        一行都没有"不同——后者会返回一份空快照，而空快照会让每个人都聚合成无权限，
        那是数据事实，不是不可用。

        **读完之后重新问一次「当前有效批次是哪一批」，答案必须仍是刚才那一批**。四条
        语句在 ``READ COMMITTED`` 下各取一次数据库快照，读取期间库里可能发生三件事，
        这一次重调把三条腿一起盖住：

        - **一次新导入完成**：旧批次转 ``superseded``、新批次成为当前有效。此时手里
          这份行是**上一批**的——拿它去发布，等于用刚刚被取代的权限覆盖新权限，而外部
          发布表没有版本号，谁也发现不了；
        - **批次过期**：九十天上限到了，它不再是当前有效批次；
        - **保留清理级联删除**：按 ``expires_at`` 删批次时子表随外键 ``CASCADE`` 一起
          消失，读到的是一份**残缺**快照。它不会让谁多拿权限（少行只会少算范围，聚合层
          再往下就是失败关闭），但会让一批在职员工的权限被算成"没有"。

        判据刻意是**重调 :meth:`current_batch_id` 并比对 id**，而不是在这里复写一遍
        「``complete`` 且未过期」：那条谓词是 `V-银河-06` 的规则，复写一份就有了第二处
        口径，早晚分叉——而分叉的表现是"复核通过了、用的却不是当前那一批"。

        **这道复核不保证「发布出去的一定是最新一批」，也不试图保证**：复核之后到逐人
        ``record_decision`` 之间仍有窗口，一次恰好在此时完成的新导入要等下一天那一轮
        才生效。这与"新批次在当日轮跑完之后才完成"是同一件事——**日频刷新的固有时延**，
        不是缺陷。这道复核要挡的是更严重的那一种：读到一半换了批次，于是发布的内容
        **自身不自洽**（一部分来自 A、一部分来自 B）。
        """

        batch_id = self.current_batch_id()
        if batch_id is None:
            logger.warning("没有当前有效的银河批次，本次读取不可用")
            return None

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            user_rows = _rows(cursor, _select("user", USER_COLUMNS), batch_id, USER_COLUMNS)
            role_rows = _rows(cursor, _select("user_role", USER_ROLE_COLUMNS), batch_id, USER_ROLE_COLUMNS)
            datacountry_rows = _rows(
                cursor,
                _select("sys_user_datacountry", USER_DATACOUNTRY_COLUMNS),
                batch_id,
                USER_DATACOUNTRY_COLUMNS,
            )
            country_rows = _rows(cursor, _select("sys_country", COUNTRY_COLUMNS), batch_id, COUNTRY_COLUMNS)

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
    """``core.permission.notification.CompanyNameResolver`` 的真实实现（结构性
    实现，不继承）：按当前有效银河批次查 ``galaxy_country.name_cn``（Trace
    #469 S-1 TOP-8）。

    与 ``adapters/admin_registry.PostgresAdminQueries.company_label`` 是同一份
    查询姿势（当前批次 + ``boss_company_id`` 精确匹配），**独立各自维护，不
    共享实现**——两处调用面不同（一处是管理员命令面展示，一处是普通用户权限
    变化通知），与本仓库既有的"各自独立声明接口"惯例一致。只查
    ``galaxy_country`` 一张表，不用 :meth:`PostgresGalaxySnapshotReader.
    load_current`——那个方法额外读 ``galaxy_user``/``galaxy_user_role``/
    ``galaxy_user_datacountry`` 三张表，本类每次调用只需要一次"给定编号查
    中文名"的轻量读取，没有理由为此多付三张表的读取成本（``PermissionPublishDuty``
    是高频 tick，代价差异会被放大）。
    """

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def name_for(self, *, company_id: str) -> str | None:
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return None
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
        """``CompanyNameResolver.names_for`` 真实实现（Trace #469 修复包 B，
        B-7：连接风暴收敛）：与 ``adapters/admin_registry.PostgresAdminQueries.
        company_labels`` 同一份批量查询姿势，独立各自维护（同上一条注释理由）。

        修复前：``describe_scope`` 对权限文档里每一个公司编号各调用一次
        :meth:`name_for`，每次都新建两条连接——公司位较多的权限文档会重演
        ``core/admin`` 侧同一条连接风暴（审查交叉裁定的同构问题）。修复后：
        整批编号只建两条连接。查无中文名的编号在返回映射里是 ``None``（与
        :meth:`name_for` 的既有语义一致，不是空字符串），空输入返回空映射、
        不发起任何查询。
        """

        if not company_ids:
            return {}
        from lingxi.adapters.galaxy_import import PostgresGalaxyImportStore

        batch_id = PostgresGalaxyImportStore(self._dsn, timeouts=self._timeouts).current_batch_id()
        if batch_id is None:
            return {company_id: None for company_id in company_ids}
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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
