"""真库用例之间的隔离契约：上一条用例写进去的行，下一条用例必须看不见（Issue #234）。

#234 为了速度改了清场机制——从「每条用例 TRUNCATE 当前全部生产表」改成「只清这一轮
真的被写过的那几张表，能 DELETE 的就 DELETE」。**改的是隔离机制本身**，所以隔离强度
必须有一条会变红的用例守着；否则下一次有人再想省一点时间时，没有任何东西会告诉他
哪里踩了线。为了快而让用例之间互相看见对方的数据，是把一个性能问题换成一个正确性问题。

**为什么这些用例与执行顺序无关。** 类里每条用例都在 `tearDown` 里故意往库里塞脏数据，
又都在用例体开头断言库是干净的。只要类里有两条以上用例，就一定有一条是在另一条之后
开始的——那一条开头的断言，就是「隔离机制真的把上一条的写入清掉了」这句话本身。
不依赖 unittest 按方法名排序，也不依赖谁先谁后。

脏数据故意挑三张形态不同的表，让「清得掉」这件事不只在最容易的那种表上成立：

* `app_user` —— 被 7 张表通过外键引用，清它必须先清子表（或者靠 `TRUNCATE … CASCADE`）；
* `galaxy_import_batch` —— 0054 给它装了 BEFORE DELETE 触发器
  `lingxi_reject_premature_delete()`，**未到期的行谁来 DELETE 都被拒绝**，而用例刚插进去的
  批次 2160 小时后才到期。它是「清行不能一律改用 DELETE」的活证据，下面有一条用例
  直接把这个拒绝跑出来；
* `inbound_event` —— 没有任何表引用它的叶子表，证明脏表判定不依赖外键图。
"""

from __future__ import annotations

import os
import unittest

from postgres_schema import (
    ensure_production_schema,
    production_tables,
    production_tables_with_rows,
    reset_production_rows,
)

from lingxi.adapters.postgres import connect

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_REASON = "跳过：未设置 LINGXI_POSTGRES_DSN，真库用例之间的隔离契约未验证"

# 标记行按主键点名查，不靠「表里有没有行」这种会被别的模块顺带满足的弱条件。
_MARKERS = (
    ("app_user", "id", "usr-iso-contract"),
    ("galaxy_import_batch", "id", "gib-iso-contract"),
    ("inbound_event", "feishu_event_id", "evt-iso-contract"),
)

_WRITE_APP_USER, _WRITE_IMPORT_BATCH, _WRITE_INBOUND_EVENT = (
    # 一律 `ON CONFLICT DO NOTHING`：同一条用例里可能先手工写一遍、`tearDown` 再写一遍，
    # 脏数据本身不该因为重复写入而抛异常——那样红的是用例自己的写法，不是隔离。
    """INSERT INTO app_user
           (id, feishu_open_id, feishu_user_id, feishu_union_id,
            display_name, department, tenant_key, provisioning_state)
       VALUES ('usr-iso-contract', 'ou-iso-contract', 'u-iso-contract', 'un-iso-contract',
               '隔离契约', '数据部', 'tk-iso-contract', 'active')
       ON CONFLICT DO NOTHING""",
    """INSERT INTO galaxy_import_batch (id, source_label, source_digest, status)
       VALUES ('gib-iso-contract', '隔离契约', 'digest-iso-contract', 'complete')
       ON CONFLICT DO NOTHING""",
    """INSERT INTO inbound_event (feishu_event_id, event_type, trace_id, expires_at)
       VALUES ('evt-iso-contract', 'im.message.receive_v1',
               '01J00000000000000000ISO234', now())
       ON CONFLICT DO NOTHING""",
)
_DIRTY_WRITES = (_WRITE_APP_USER, _WRITE_IMPORT_BATCH, _WRITE_INBOUND_EVENT)


@unittest.skipUnless(DSN, SKIP_REASON)
class RealDatabaseIsolationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    @classmethod
    def tearDownClass(cls) -> None:
        # 最后一条用例的 tearDown 也会留下脏数据。这里把它收掉：本模块的脏数据是
        # 给自己用的证据，不该漏给按模块名排在后面的真库模块。
        reset_production_rows(cls._dsn)

    def setUp(self) -> None:
        # **这一行就是被验的隔离机制。** 变异复验时注释掉它，后跑的用例必须变红。
        reset_production_rows(self._dsn)

    def tearDown(self) -> None:
        # 每条用例收尾都留下脏数据，因此谁跑在后面谁就在证明隔离。
        self._execute(*_DIRTY_WRITES)

    # -- 小工具 ---------------------------------------------------------

    def _execute(self, *statements: str) -> None:
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def _marker_rows(self) -> list[tuple[str, int]]:
        """三条标记行各还剩几行。干净的库上应当全是 0。"""

        counts: list[tuple[str, int]] = []
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for table, key, value in _MARKERS:
                cursor.execute(f'SELECT count(*) FROM public."{table}" WHERE {key} = %s', (value,))
                counts.append((table, cursor.fetchone()[0]))
        return counts

    def _scan_every_table_for_rows(self) -> tuple[str, ...]:
        """逐表各发一条 EXISTS 慢慢扫一遍——它是「到底哪些表有行」的独立裁判。

        故意不复用清行自己那条合并语句：用被验对象去验被验对象，等于什么都没验。
        """

        found: list[str] = []
        tables = production_tables(self._dsn)
        with connect(self._dsn) as connection, connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f'SELECT EXISTS (SELECT 1 FROM public."{table}")')
                if cursor.fetchone()[0]:
                    found.append(table)
        return tuple(sorted(found))

    def _assert_starts_clean(self) -> None:
        self.assertEqual(
            self._marker_rows(),
            [(table, 0) for table, _, _ in _MARKERS],
            "上一条用例在 tearDown 里写下的行还看得见：用例之间的隔离已经破了",
        )

    # -- 契约 -----------------------------------------------------------

    def test_a_case_never_sees_rows_written_by_the_previous_case(self) -> None:
        self._assert_starts_clean()
        self.assertEqual(
            self._scan_every_table_for_rows(),
            (),
            "清场之后还有生产表存着行",
        )

    def test_the_probe_that_drives_the_reset_agrees_with_a_table_by_table_scan(self) -> None:
        """清行依据的那条合并判定，必须与逐表独立扫描给出同一个集合。

        清行只清判定说「有行」的表。判定一旦漏掉一张，那张表就会带着上一条用例的行活到
        下一条用例——而失败会出现在某个完全无关的模块里，没人查得到这里。让它在这里直接变红。
        """

        self._assert_starts_clean()
        self._execute(*_DIRTY_WRITES)

        scanned = self._scan_every_table_for_rows()
        probed = production_tables_with_rows(self._dsn)

        self.assertEqual(
            scanned,
            ("app_user", "galaxy_import_batch", "inbound_event"),
            "本用例自己写的三张表没被扫到，说明写入根本没生效，后面的比较没有意义",
        )
        self.assertEqual(probed, scanned, "清行判定与逐表扫描不一致：清行会漏表")

    def test_reset_only_touches_the_tables_that_were_actually_written(self) -> None:
        """成本与「这一轮弄脏了几张表」成正比，而不是与「库里一共有几张表」成正比。

        回到「每轮清全部生产表」的那一刻，这条断言就变红——它是 #234 那个性能性质的
        机制守卫，不是一句「实测快了所以应该没问题」。
        """

        self._assert_starts_clean()
        all_tables = production_tables(self._dsn)
        self._execute(_WRITE_INBOUND_EVENT)  # 只弄脏 inbound_event 这一张叶子表

        cleaned = reset_production_rows(self._dsn)

        self.assertEqual(
            cleaned,
            ("inbound_event",),
            "清行碰的表超出了这一轮真正被写过的那张",
        )
        self.assertGreater(
            len(all_tables),
            len(cleaned) + 1,
            f"库里只有 {len(all_tables)} 张生产表，这条断言撑不起「清行范围远小于全表」",
        )

    def test_rows_that_delete_refuses_to_remove_are_still_cleaned(self) -> None:
        """未到期的保留内容 DELETE 不掉，但清场必须照样把它清干净。

        这条同时是「为什么清行不能一律改用 DELETE」的证据：先把数据库那句拒绝真的跑出来，
        再证明清场没有被它挡住。哪天有人把 TRUNCATE 那条分支删掉，这条用例会变红。
        """

        self._assert_starts_clean()
        self._execute(_WRITE_IMPORT_BATCH)

        with self.assertRaises(Exception) as rejected:
            self._execute('DELETE FROM public."galaxy_import_batch"')
        self.assertIn("拒绝删除未到期的", str(rejected.exception))

        cleaned = reset_production_rows(self._dsn)

        self.assertEqual(cleaned, ("galaxy_import_batch",))
        self.assertEqual(self._scan_every_table_for_rows(), (), "DELETE 清不掉的行被清场漏下了")
