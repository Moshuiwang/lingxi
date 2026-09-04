"""每日花名册审计的比对基线读取。**本模块只读，一个写语句都没有。**

「只读」在这里是产品约束，不是编码习惯：

- `employee_no` 是匹配银河权限记录的**主键**。把花名册当前值静默写回存档，会在下一次
  匹配时改变匹配结果，而日报恰恰是为了让人来判断这次变化该不该被接受
  （`V-花名册-14`，对应变异点 M-11）；
- 存档三字段是比对基线本身。写回等于每天把基线对齐成花名册现值，第二天差异归零——
  漂移不会消失，只会再也报不出来。

比对集的两条过滤都在 SQL 里，不在 Python 里（`V-花名册-10`、`V-花名册-11`）：

- `provisioning_state = 'active'`：只比对**已开通**用户。其余六种状态（`guest`、
  `matching`、`manual_review`、`provisioning`、`mcp_syncing`、`aborted`）的人还没有
  完成开通，他们的资料不属于「已承诺维护」的范围，出现在管理群里是数据范围外泄；
- `account_state NOT IN ('deleting', 'deleted')`：删除编排会按数据库设计 :144
  **清空**姓名等字段，不排除的话每一个被删除的用户都会稳定地报一条「姓名变化」。

飞书在职状态与花名册「账号状态」按设计 :150 **按需回读、不落库**，本模块因此不 SELECT、
更不写入任何状态快照列（`V-花名册-15`）。
"""

from __future__ import annotations

import logging

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.roster_audit import ArchivedIdentity

logger = logging.getLogger(__name__)

# 唯一的一条语句。放在模块常量里，是为了让「本模块只读」可以被一眼核对，也可以被
# 源码级断言核对（测试会检查本模块不出现 INSERT / UPDATE / DELETE / TRUNCATE）。
ACTIVE_BASELINE_SQL = """
SELECT id, feishu_user_id, display_name, employee_no, email
  FROM app_user
 WHERE provisioning_state = 'active'
   AND account_state NOT IN ('deleting', 'deleted')
 ORDER BY id
"""


def _text(value: object) -> str:
    """`NULL` 与空白归一为空串。存档为空的字段随后会被比对函数跳过。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


class PostgresRosterBaselineReader:
    """读取已开通用户的存档三字段。构造时不连接数据库，每次调用自带连接。"""

    def __init__(self, dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS) -> None:
        self._dsn = dsn
        self._timeouts = timeouts

    def load_active_baseline(self) -> tuple[ArchivedIdentity, ...]:
        """返回本轮比对集。只取五列：内部标识、人员 ID 与存档三字段。

        取的列就是要用的列——多取一列就是多一份可识别数据进内存，而它没有用途。
        """

        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(ACTIVE_BASELINE_SQL)
            rows = cursor.fetchall()

        baseline = tuple(
            ArchivedIdentity(
                app_user_id=str(row[0]),
                personnel_id=_text(row[1]),
                display_name=_text(row[2]),
                employee_no=_text(row[3]),
                email=_text(row[4]),
            )
            for row in rows
        )
        # 只记条数。依据是 `V-花名册-33`（审计与日志不含花名册字段值）——不是日报的
        # 展示口径，后者已被 2026-08-08 的 D2 裁定放宽到「受控管理群可含原值」，
        # 而日志会流向排障、CI 输出与工单，不在那个受控范围里。
        logger.info("花名册审计基线已读取 已开通用户=%s", len(baseline))
        return baseline
