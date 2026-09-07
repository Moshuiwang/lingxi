"""权威决定链前两段（读取来源 + 检查完整性）的判据，以及两条重算链的横向对照。

三个入口共用 ``core/permission/decision_chain.py`` 这一份实现之后，"读不出来一律抛"
"补行失败只留痕、重读失败照抛""补齐真的补进了本次的决定"这三条判据必须只剩一份，
且各入口的留痕事件名不得被抹平。本文件分两层：

1. **共用实现自己的判据**——直接驱动 ``LocalOverrideDecisionSource``，不经过任何入口；
2. **两条重算链的横向对照**——同一份输入喂给每日重算与定向重算，比对补齐调用、发布
   内容与各自的留痕事件名。首聊开通不参加第 2 层：它按设计不接补齐口（建新行，
   「全部」组的补齐只发生在两条重算链上），这一点由本文件的否定断言单独钉住。

读失败在三个入口上的横向对照在 ``tests/test_local_override_read_failure_parity.py``，
本文件不重复。
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import UTC, datetime

import test_permission_refresh_duty as refresh
import test_targeted_permission_recompute as targeted

from lingxi.core.permission.decision_chain import (
    AllScopeBackfill,
    LocalOverrideDecisionSource,
)
from lingxi.core.permission.local_override import LocalOverrideReadError

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
USER = refresh.USER_ONE
SOURCE_ROOT = pathlib.Path(__file__).parents[1] / "src" / "lingxi"


class _Recorder:
    """三个回调各记一份调用，替代入口自己的留痕出口。"""

    def __init__(self) -> None:
        self.read_failed: list[tuple[str, str]] = []
        self.expand_failed: list[tuple[str, str]] = []
        self.expanded: list[tuple[str, int]] = []

    def on_read_failed(self, user_id: str, error: Exception) -> None:
        self.read_failed.append((user_id, type(error).__name__))

    def on_expand_failed(self, user_id: str, error: Exception) -> None:
        self.expand_failed.append((user_id, type(error).__name__))

    def on_expanded(self, user_id: str, added: int) -> None:
        self.expanded.append((user_id, added))


def build_source(
    *, reader=None, expander=None, metric_translation_map=None
) -> tuple[LocalOverrideDecisionSource, _Recorder]:
    recorder = _Recorder()
    backfill = (
        None
        if expander is None
        else AllScopeBackfill(
            expander=expander,
            on_failed=recorder.on_expand_failed,
            on_succeeded=recorder.on_expanded,
        )
    )
    source = LocalOverrideDecisionSource(
        reader=reader,
        on_read_failed=recorder.on_read_failed,
        metric_translation_map=(
            refresh.METRIC_TRANSLATION_MAP
            if metric_translation_map is None
            else metric_translation_map
        ),
        clock=lambda: NOW,
        backfill=backfill,
    )
    return source, recorder


class NotWiredTests(unittest.TestCase):
    """装配层没接这条来源：静默按"没有本地源"处理，一个回调都不触发。"""

    def test_an_unwired_source_resolves_to_none_without_any_callback(self) -> None:
        source, recorder = build_source()

        self.assertIsNone(source.resolve(USER))
        self.assertEqual((recorder.read_failed, recorder.expanded), ([], []))


class ReadStageTests(unittest.TestCase):
    """读取来源这一段：合法空集与不可读必须分得开。

    变异锚点：把 :meth:`LocalOverrideDecisionSource.read_entries` 的 ``except`` 改成
    ``return ()`` → ``test_an_unreadable_source_raises_and_never_degrades_to_empty``
    由绿转红（不再抛，读失败坍缩成"这个人没有本地覆盖"）。
    """

    def test_a_readable_empty_set_resolves_to_an_empty_result_not_none(self) -> None:
        reader = refresh.FakeLocalOverrides({USER: ()})
        source, recorder = build_source(reader=reader)

        resolved = source.resolve(USER)

        self.assertIsNotNone(resolved, "合法空集不是「未装配」：合并要拿到一个真实结果")
        assert resolved is not None
        self.assertEqual((resolved.grants, resolved.suppressions), (frozenset(), frozenset()))
        self.assertEqual(recorder.read_failed, [])

    def test_an_unreadable_source_raises_and_never_degrades_to_empty(self) -> None:
        reader = refresh.FakeLocalOverrides(fail_for={USER})
        source, recorder = build_source(reader=reader)

        with self.assertRaises(LocalOverrideReadError):
            source.resolve(USER)
        self.assertEqual(recorder.read_failed, [(USER, "RuntimeError")], "读失败必须留痕一次")

    def test_a_readable_grant_reaches_the_resolved_result(self) -> None:
        reader = refresh.FakeLocalOverrides({USER: (refresh._override_entry(),)})
        source, _ = build_source(reader=reader)

        resolved = source.resolve(USER)

        assert resolved is not None
        self.assertEqual(len(resolved.grants), 1)


class CompletenessStageTests(unittest.TestCase):
    """检查完整性这一段：缺才补、补失败只留痕、重读失败照抛。

    变异锚点：把 :meth:`LocalOverrideDecisionSource._complete_all_scope` 末尾的重读改回
    ``return entries``、或把 ``added_total == 0`` 的提前返回加回去 →
    ``test_a_successful_backfill_is_reread_into_this_round`` 与
    ``test_a_zero_row_backfill_still_rereads_so_a_revocation_wins_this_round`` 变红。
    """

    def _all_scope_reader(self) -> refresh.FakeLocalOverrides:
        return refresh.FakeLocalOverrides(
            {USER: (refresh._all_scope_entry(metric_name=refresh.METRIC_NAME),)}
        )

    def test_without_a_backfill_the_entries_pass_through_untouched(self) -> None:
        reader = self._all_scope_reader()
        source, recorder = build_source(reader=reader)

        resolved = source.resolve(USER)

        assert resolved is not None
        self.assertEqual(len(reader.calls), 1, "不接补齐口就只读一次")
        self.assertEqual(recorder.expanded, [])

    def test_only_the_missing_metrics_are_requested(self) -> None:
        reader = self._all_scope_reader()
        expander = refresh.FakeLegacyAllScope(overrides=reader)
        source, recorder = build_source(reader=reader, expander=expander)

        source.resolve(USER)

        self.assertEqual(len(expander.calls), 1)
        self.assertEqual(expander.calls[0]["metrics"], (refresh.METRIC_NAME_TWO,))
        self.assertEqual(recorder.expanded, [(USER, 1)])

    def test_a_successful_backfill_is_reread_into_this_round(self) -> None:
        reader = self._all_scope_reader()
        expander = refresh.FakeLegacyAllScope(overrides=reader)
        source, _ = build_source(reader=reader, expander=expander)

        resolved = source.resolve(USER)

        assert resolved is not None
        self.assertEqual(
            sorted(metric for _company, metric in resolved.grants),
            sorted({refresh.METRIC_NAME, refresh.METRIC_NAME_TWO}),
            "补进去的那条指标必须出现在本次的计算结果里",
        )
        self.assertEqual(len(reader.calls), 2, "补成功之后必须重读一次")

    def test_a_complete_group_never_calls_the_expander(self) -> None:
        reader = refresh.FakeLocalOverrides(
            {
                USER: (
                    refresh._all_scope_entry(metric_name=refresh.METRIC_NAME),
                    refresh._all_scope_entry(metric_name=refresh.METRIC_NAME_TWO),
                )
            }
        )
        expander = refresh.FakeLegacyAllScope(overrides=reader)
        source, recorder = build_source(reader=reader, expander=expander)

        source.resolve(USER)

        self.assertEqual((expander.calls, recorder.expanded), ([], []))
        self.assertEqual(len(reader.calls), 1, "没有缺项就不重读")

    def test_a_backfill_failure_only_leaves_a_trace_and_keeps_computing(self) -> None:
        """补行失败与重读失败刻意不同：这一次的决定并不因为补不进去而缺内容。

        补行失败仍然重读——判据是"这一轮有没有缺项"，不是"补行成没成功"：补不进去
        的那一刻，这个组照样可能已经被撤销了。
        """

        reader = self._all_scope_reader()
        expander = refresh.FakeLegacyAllScope(error=RuntimeError("注入的补行失败"))
        source, recorder = build_source(reader=reader, expander=expander)

        resolved = source.resolve(USER)

        assert resolved is not None
        self.assertEqual(recorder.expand_failed, [(USER, "RuntimeError")])
        self.assertEqual(recorder.expanded, [], "失败的那一组不登记新增行数")
        self.assertEqual(
            sorted(metric for _company, metric in resolved.grants), [refresh.METRIC_NAME]
        )
        self.assertEqual(len(reader.calls), 2, "有缺项就必重读，补行成败不影响这一步")

    def test_a_zero_row_backfill_still_rereads_so_a_revocation_wins_this_round(self) -> None:
        """补行口报告"一行都没新增"最常见的成因就是整组刚被撤销：**必须重读**。

        这条用例此前钉的是"零新增不重读"，那正是让撤销掉的指标被过时结论重新发布
        出去的那半步。这里的补行口在报告 0 的同时把组撤掉（真实交错的形状），重读
        之后本轮算出空集——撤销当轮生效，不用等下一轮。
        """

        reader = self._all_scope_reader()

        class _RevokedWhileExpanding:
            """报告零新增，并在同一时刻把这个组从来源里撤掉。"""

            def __init__(self) -> None:
                self.calls: list[str] = []

            def expand_all_scope_group(self, *, user_id, group_id, metrics, now) -> int:
                self.calls.append(user_id)
                reader._entries[user_id] = ()
                return 0

        expander = _RevokedWhileExpanding()
        source, recorder = build_source(reader=reader, expander=expander)

        resolved = source.resolve(USER)

        self.assertEqual(expander.calls, [USER])
        self.assertEqual(recorder.expanded, [(USER, 0)])
        self.assertEqual(len(reader.calls), 2, "零新增也必须重读，否则拿的是撤销前的事实")
        assert resolved is not None
        self.assertEqual(resolved.grants, frozenset(), "本轮就算出撤销之后的事实")

    def test_a_reread_failure_after_a_successful_backfill_raises(self) -> None:
        reader = refresh.FakeLocalOverrides(
            {USER: (refresh._all_scope_entry(metric_name=refresh.METRIC_NAME),)},
            fail_after_calls=1,
        )
        expander = refresh.FakeLegacyAllScope(overrides=reader)
        source, recorder = build_source(reader=reader, expander=expander)

        with self.assertRaises(LocalOverrideReadError):
            source.resolve(USER)
        self.assertEqual(recorder.expanded, [(USER, 1)], "前提：补行这一步确实成功了")
        self.assertEqual(recorder.read_failed, [(USER, "RuntimeError")], "重读失败与首读同姿态")


class TwoRecomputeChainsAgreeTests(unittest.TestCase):
    """同一份输入喂给每日重算与定向重算：补齐调用与发布内容必须逐字一致。

    变异锚点：把任一入口的 ``backfill=`` 接线去掉（传 ``None``）→ 本类由绿转红。
    """

    def _inputs(self):
        overrides = refresh.FakeLocalOverrides(
            {USER: (refresh._all_scope_entry(metric_name=refresh.METRIC_NAME),)}
        )
        return overrides, refresh.FakeLegacyAllScope(overrides=overrides)

    def _daily(self):
        overrides, expander = self._inputs()
        duty, parts = refresh.build_duty(
            identities=(refresh.identity(),), local_overrides=overrides, legacy_all_scope=expander
        )
        duty.run_once()
        return expander, parts

    def _targeted(self):
        overrides, expander = self._inputs()
        recompute, parts = targeted.build_recompute(
            identities=(refresh.identity(),),
            published_users={USER},
            local_overrides=overrides,
            legacy_all_scope=expander,
        )
        recompute.recompute_and_publish(user_id=USER)
        return expander, parts

    def test_both_chains_ask_the_expander_for_exactly_the_same_missing_metrics(self) -> None:
        daily_expander, _ = self._daily()
        targeted_expander, _ = self._targeted()

        self.assertEqual(len(daily_expander.calls), 1)
        self.assertEqual(
            [call["metrics"] for call in daily_expander.calls],
            [call["metrics"] for call in targeted_expander.calls],
        )

    def test_both_chains_publish_the_backfilled_metric(self) -> None:
        _, daily_parts = self._daily()
        _, targeted_parts = self._targeted()

        daily_row = daily_parts["decisions"].calls[0]["row"]
        targeted_row = targeted_parts["decisions"].calls[0]["row"]
        for label, row in (("每日重算", daily_row), ("定向重算", targeted_row)):
            with self.subTest(entry=label):
                self.assertIn(refresh.METRIC_NAME_TWO, row.permissions, "补进去的指标要发出去")

    def test_each_chain_keeps_its_own_trace_event_names(self) -> None:
        """共用判据不等于共用词汇：运维要能一眼看出是哪条链补的行。"""

        _, daily_parts = self._daily()
        _, targeted_parts = self._targeted()

        self.assertEqual(
            daily_parts["audit"].fields_for("permission_refresh.legacy_all_scope_refreshed"),
            [{"user": USER, "added": 1}],
        )
        self.assertEqual(
            [
                fields
                for name, fields in targeted_parts["audit"].records
                if name == "permission_targeted_recompute.legacy_all_scope_refreshed"
            ],
            [{"user": USER, "added": 1}],
        )


class MergeCallSiteDisciplineTest(unittest.TestCase):
    """每一处合并调用都必须**显式**声明是哪一种通配。

    ``full_access_wildcard`` 曾经的默认值 ``True`` 正是一次真实漏接的根因。签名侧的
    必填纪律钉在 ``tests/test_permission_merge_sources.py``；这里钉的是另一半——四个
    调用点里**没有任何一个**靠默认值。变异锚点：把任一调用点的
    ``full_access_wildcard=`` 参数删掉，本用例由绿转红。
    """

    def test_every_call_site_passes_the_flag_by_keyword(self) -> None:
        call_sites: list[tuple[str, int, bool]] = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name != "merge_permission_sources":
                    continue
                passed = any(word.arg == "full_access_wildcard" for word in node.keywords)
                call_sites.append((str(path.relative_to(SOURCE_ROOT)), node.lineno, passed))

        self.assertGreaterEqual(len(call_sites), 4, "扫描面变空就等于这条断言恒真")
        self.assertEqual([site for site in call_sites if not site[2]], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
