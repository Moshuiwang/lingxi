"""``adapters/delegated_subject_lookup.py`` 的真库断言（需要真实 PostgreSQL 16）。

这个模块是从 ``adapters/delegated_credentials.py`` 拆出来的单一只读函数（opus
批量审查 P1 修复，专用主体结构性出口前置 A3）：``registered_delegated_subject_
open_id`` 本身此前没有独立的单元测试（只被 scheduler 装配/`admin_bootstrap` CLI
用注入的假实现间接覆盖），拆分之后单独补一份真库断言，确认提取过程没有改变
任何行为——SQL 语句与查询条件与拆分前逐字节相同。

不测 ``adapters/delegated_credentials.py`` 的重新导出是否还能用——那只是一条
``from ... import ...`` 语句，Python import 系统本身保证正确性，不需要额外测试；
`test_identity_postgres_records.py`/`test_scheduler_process.py` 等既有测试仍然
经那个模块路径调用其余功能，本身就是这条重新导出没有失效的证据。
"""

from __future__ import annotations

import os
import unittest

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

from lingxi.adapters.delegated_subject_lookup import (
    DELEGATED_PURPOSE,
    registered_delegated_subject_open_id,
)
from lingxi.adapters.postgres import connect

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，专用授权主体标识读取的真库断言未验证（需真实 PostgreSQL 16）"
    if not DSN
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，专用授权主体标识读取的真库断言未验证"
)


@unittest.skipUnless(DSN and psycopg_available(), SKIP_REASON)
class RegisteredDelegatedSubjectOpenIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = DSN
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        reset_production_rows(self._dsn)

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def test_no_row_returns_none(self) -> None:
        self.assertIsNone(registered_delegated_subject_open_id(self._dsn))

    def test_a_registered_subject_is_returned(self) -> None:
        self.execute(
            "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES (%s, %s)",
            (DELEGATED_PURPOSE, "ou_registered_subject"),
        )

        self.assertEqual(
            registered_delegated_subject_open_id(self._dsn), "ou_registered_subject"
        )

    def test_a_different_purpose_is_rejected_by_the_table_check(self) -> None:
        """诚实记录：`WHERE purpose = %s` 这个过滤条件今天在真库上测不出"读到了别的
        purpose 却被正确过滤掉"这条分支——``purpose`` 列本身有 ``CHECK (purpose =
        'org_directory_sync')``（迁移 ``006``），结构上这张表此刻只能有这一个
        purpose 值。过滤条件仍然是正确、必要的代码：防的是未来这条 CHECK 被放宽
        之后本函数不会读错行，不是当前就有别的 purpose 数据存在。"""

        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES (%s, %s)",
                ("employee_token", "ou_unrelated_purpose"),
            )

    def test_a_blank_subject_open_id_is_rejected_by_the_table_check(self) -> None:
        """诚实记录：函数自己的"空白值当 None 处理"分支在真库上此刻打不到——
        `feishu_delegated_subject` 的 CHECK（``NULLIF(BTRIM(subject_open_id), '')
        IS NOT NULL``，迁移 ``006``）已经在 INSERT 这一步就拒绝空白值，函数里那
        条判断是应用层的第二道防线，不是当前唯一防线。"""

        with self.assertRaises(Exception):
            self.execute(
                "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES (%s, %s)",
                (DELEGATED_PURPOSE, "   "),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
