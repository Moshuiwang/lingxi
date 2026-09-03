"""`scripts/ops/preprovision.py` 的纯逻辑用例与预授权落库口的假 cursor 断言（Issue #541）。

加载方式同 `tests/test_import_local_permission_override.py`：`scripts/` 下的文件用
`importlib.util.spec_from_file_location` 按路径加载，脚本内部对 `lingxi.*` 的 import
由 `PYTHONPATH=src` 解析。

本文件覆盖 rc25 S-8b 的四条：`--dry-run` 零写入且只出计数；名单形态写错当场拒、零写入；
逐人失败关闭不阻塞其他人；每笔预授权合成的 `pending_action` 带 `reason='preprovision_2_0'`。
最后一条用**假 cursor** 直接断言落库口发出的 SQL 参数——它不需要真库，因此"这条 reason
被改掉了"会在 fast 档就变红，而不是要等真库门禁或 stage 演练。
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lingxi.adapters.postgres_local_permission import _apply_position_grant_locked
from lingxi.core.permission.position_override import (
    PREPROVISION_OVERRIDE_REASON,
    PREPROVISION_PENDING_ACTION_REASON,
    build_preprovision_grant_plan,
    expand_position_scope,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "ops" / "preprovision.py"


def _load_script():
    module_name = "preprovision_ops_under_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 先登记再执行：脚本用了 `from __future__ import annotations`，dataclass 解析
    # 延迟求值的字段注解时要在 sys.modules 里查到本模块（同 #441 的既有注释）。
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()

ROLE_MAP = {"A国家总经理": "CEO", "A国家财务总监": "财务"}
COMPANY_MAP = {
    "1011": {"CEO": ("sub_new_count", "exchange_rate"), "财务": ("vat_rate",)},
    "1012": {"CEO": ("sub_new_count",), "财务": ("vat_rate", "exchange_rate")},
}

ROLE_MAP_TOML = """
[meta]
source = "test"

[roles]
"A国家总经理" = "CEO"
"A国家财务总监" = "财务"
"""

COMPANY_MAP_TOML = """
[meta]
source = "test"

[companies."1011"]
"CEO" = ["sub_new_count", "exchange_rate"]
"财务" = ["vat_rate"]

[companies."1012"]
"CEO" = ["sub_new_count"]
"财务" = ["vat_rate", "exchange_rate"]
"""


def _plan(position_name: str = "A国家总经理", company_scope: str = "1011"):
    return build_preprovision_grant_plan(
        expand_position_scope(
            position_name=position_name,
            company_scope=company_scope,
            role_function_map=ROLE_MAP,
            company_function_metric_map=COMPANY_MAP,
            available_companies=tuple(COMPANY_MAP),
        )
    )


class RosterParsingTest(unittest.TestCase):
    """名单形态写错一律**整份拒绝**（当场拒、零写入），不逐人跳过。"""

    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_a_well_formed_roster_normalizes_the_email(self) -> None:
        rows = TOOL.load_roster(
            self._write("email,position,company_scope\n  Zhang.San@Example.com , A国家总经理 , 1011 \n")
        )
        self.assertEqual(
            [(row.email, row.position_name, row.company_scope) for row in rows],
            [("zhang.san@example.com", "A国家总经理", "1011")],
        )

    def test_a_wrong_header_is_rejected_outright(self) -> None:
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_roster(self._write("email,permissions\na@b.com,\"{}\"\n"))

    def test_a_blank_field_is_rejected_outright(self) -> None:
        with self.assertRaises(TOOL.RosterError):
            TOOL.load_roster(self._write("email,position,company_scope\na@b.com,,1011\n"))

    def test_the_same_email_twice_is_rejected_outright(self) -> None:
        """归一后同一个邮箱两行＝名单本身有歧义，不猜哪一行为准。"""

        with self.assertRaises(TOOL.RosterError):
            TOOL.load_roster(
                self._write(
                    "email,position,company_scope\n"
                    "a@b.com,A国家总经理,1011\n"
                    "A@B.com,A国家财务总监,1012\n"
                )
            )

    def test_an_unknown_position_is_rejected_outright(self) -> None:
        """职位写错**当场拒**——这正是选「职位＋公司范围」而不是「邮箱＋指标 JSON」的意义：
        名单上没有一列会静默生效的自由文本。"""

        rows = (TOOL.RosterRow(email="a@b.com", position_name="A国家运维总监", company_scope="1011"),)
        with self.assertRaises(TOOL.RosterError):
            TOOL.plan_preprovision(
                rows, role_function_map=ROLE_MAP, company_function_metric_map=COMPANY_MAP
            )

    def test_a_company_scope_outside_the_catalog_is_rejected_outright(self) -> None:
        rows = (TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="9999"),)
        with self.assertRaises(TOOL.RosterError):
            TOOL.plan_preprovision(
                rows, role_function_map=ROLE_MAP, company_function_metric_map=COMPANY_MAP
            )

    def test_one_bad_row_rejects_the_whole_roster(self) -> None:
        """一行写错就整份拒绝：不放行其余行、不产出部分结果。"""

        rows = (
            TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="1011"),
            TOOL.RosterRow(email="c@d.com", position_name="不存在的职位", company_scope="1011"),
        )
        with self.assertRaises(TOOL.RosterError):
            TOOL.plan_preprovision(
                rows, role_function_map=ROLE_MAP, company_function_metric_map=COMPANY_MAP
            )

    def test_all_companies_scope_expands_to_every_company(self) -> None:
        items = TOOL.plan_preprovision(
            (TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="全部"),),
            role_function_map=ROLE_MAP,
            company_function_metric_map=COMPANY_MAP,
        )
        self.assertEqual(items[0].plan.company_scope, "*")
        self.assertEqual(
            set(items[0].plan.pairs),
            {("1011", "sub_new_count"), ("1011", "exchange_rate"), ("1012", "sub_new_count")},
        )


class PlanCarriesTheAuditReasonTest(unittest.TestCase):
    def test_every_planned_grant_carries_the_preprovision_reason(self) -> None:
        """预授权计划带的是 ``preprovision_2_0``，不是存量差集导入的 ``legacy_import_2_0``。"""

        items = TOOL.plan_preprovision(
            (TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="1011"),),
            role_function_map=ROLE_MAP,
            company_function_metric_map=COMPANY_MAP,
        )
        self.assertEqual(items[0].plan.pending_action_reason, "preprovision_2_0")
        self.assertNotEqual(items[0].plan.pending_action_reason, "legacy_import_2_0")


class PerPersonFailureIsolationTest(unittest.TestCase):
    """裁定⑤：逐人失败关闭、不阻塞其他人。"""

    def test_one_persons_exception_does_not_stop_the_rest(self) -> None:
        items = TOOL.plan_preprovision(
            (
                TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="1011"),
                TOOL.RosterRow(email="c@d.com", position_name="A国家总经理", company_scope="1011"),
                TOOL.RosterRow(email="e@f.com", position_name="A国家财务总监", company_scope="1012"),
            ),
            role_function_map=ROLE_MAP,
            company_function_metric_map=COMPANY_MAP,
        )
        seen: list[str] = []

        class _Result:
            state = "completed"
            failure_reason = None

        def start_system(*, email: str, **_: Any) -> Any:
            seen.append(email)
            if email == "c@d.com":
                raise RuntimeError("身份定位炸了")
            return _Result()

        report = TOOL.run_preprovision(
            items, start_system=start_system, initiated_by_open_id="ou_admin", trace_id_factory=lambda: "trace"
        )

        self.assertEqual(seen, ["a@b.com", "c@d.com", "e@f.com"], "第二个人失败后必须继续跑第三个人")
        self.assertEqual((report.provisioned, report.failed), (2, 1))
        self.assertEqual(
            [item.outcome for item in report.outcomes],
            ["provisioned", "failed_RuntimeError", "provisioned"],
        )

    def test_failures_record_only_the_exception_type_never_its_text(self) -> None:
        items = TOOL.plan_preprovision(
            (TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="1011"),),
            role_function_map=ROLE_MAP,
            company_function_metric_map=COMPANY_MAP,
        )

        def start_system(**_: Any) -> Any:
            raise ValueError("zhang.san@example.com 定位失败")

        report = TOOL.run_preprovision(
            items, start_system=start_system, initiated_by_open_id="ou_admin", trace_id_factory=lambda: "trace"
        )
        rendered = io.StringIO()
        with redirect_stdout(rendered):
            TOOL.print_report(report)
        self.assertNotIn("zhang.san@example.com", rendered.getvalue())
        self.assertIn("failed_ValueError", rendered.getvalue())

    def test_a_non_completed_terminal_state_is_a_skip_not_a_success(self) -> None:
        items = TOOL.plan_preprovision(
            (TOOL.RosterRow(email="a@b.com", position_name="A国家总经理", company_scope="1011"),),
            role_function_map=ROLE_MAP,
            company_function_metric_map=COMPANY_MAP,
        )

        class _Result:
            state = "not_authorized"
            failure_reason = "email_multiple_personnel"

        report = TOOL.run_preprovision(
            items,
            start_system=lambda **_: _Result(),
            initiated_by_open_id="ou_admin",
            trace_id_factory=lambda: "trace",
        )
        self.assertEqual((report.provisioned, report.skipped, report.failed), (0, 1, 0))
        self.assertEqual(report.outcomes[0].reason, "email_multiple_personnel")


class CommandLineWritePolarityTest(unittest.TestCase):
    """写入极性：默认只出清单、零写入；`--apply` 才真正执行。"""

    def setUp(self) -> None:
        self._files: list[Path] = []
        self.addCleanup(lambda: [path.unlink(missing_ok=True) for path in self._files])
        self.started: list[dict[str, Any]] = []

        class _Result:
            state = "completed"
            failure_reason = None

        def start_system(**kwargs: Any) -> Any:
            self.started.append(kwargs)
            return _Result()

        self._patch(TOOL, "resolve_admin_registry_lookup", lambda dsn: object())
        self._patch(TOOL, "initiated_by_is_registered_admin", lambda lookup, open_id: True)
        # `resolve_start_system` 现在返回 (入口, 收尾) 两个可调用：收尾必须被调用一次
        # ——真实装配里 `build_loop` 已经 start() 了开通执行器的线程池（谁建谁清）。
        self.shutdowns: list[int] = []
        self._patch(
            TOOL,
            "resolve_start_system",
            lambda dsn: (start_system, lambda: self.shutdowns.append(1)),
        )

    def _patch(self, module: Any, name: str, value: Any) -> None:
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(lambda: setattr(module, name, original))

    def _write(self, text: str, suffix: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8", newline=""
        )
        handle.write(text)
        handle.close()
        path = Path(handle.name)
        self._files.append(path)
        return path

    def _argv(self, roster: str) -> list[str]:
        return [
            str(self._write(roster, ".csv")),
            "--initiated-by",
            "ou_admin",
            "--dsn",
            "postgresql://example/none",
            "--role-function-map",
            str(self._write(ROLE_MAP_TOML, ".toml")),
            "--company-function-metric-map",
            str(self._write(COMPANY_MAP_TOML, ".toml")),
        ]

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = TOOL.main(argv)
        return code, out.getvalue(), err.getvalue()

    ROSTER = (
        "email,position,company_scope\n"
        "a@b.com,A国家总经理,1011\n"
        "c@d.com,A国家财务总监,1012\n"
    )

    def test_the_default_run_only_prints_the_plan_and_writes_nothing(self) -> None:
        code, out, _ = self._run(self._argv(self.ROSTER))
        self.assertEqual(code, 0)
        self.assertEqual(self.started, [], "dry-run 必须一次都不调用开通入口")
        self.assertIn("名单 2 人", out)
        self.assertIn("a@b.com", out)
        self.assertIn("将获得 2 条公司×指标", out)
        self.assertIn("合计将预授权 4 条公司×指标", out)
        self.assertIn("默认只出清单，不执行任何一人", out)

    def test_the_dry_run_alias_also_writes_nothing(self) -> None:
        code, _, _ = self._run([*self._argv(self.ROSTER), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.started, [])

    def test_apply_and_dry_run_together_take_the_conservative_branch(self) -> None:
        code, out, _ = self._run([*self._argv(self.ROSTER), "--apply", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.started, [], "两个开关一起给出时按更保守的 --dry-run 处理")
        self.assertIn("按更保守的 --dry-run 处理", out)

    def test_apply_calls_the_system_entry_once_per_person_with_the_frozen_plan(self) -> None:
        code, out, _ = self._run([*self._argv(self.ROSTER), "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual([call["email"] for call in self.started], ["a@b.com", "c@d.com"])
        self.assertEqual({call["origin"] for call in self.started}, {"preprovision"})
        self.assertEqual({call["initiated_by_open_id"] for call in self.started}, {"ou_admin"})
        self.assertEqual(
            {call["preprovision_grant"].pending_action_reason for call in self.started},
            {"preprovision_2_0"},
        )
        self.assertIn("成功 2、跳过 0、失败 0", out)
        self.assertEqual(len(self.shutdowns), 1, "跑完必须停掉并 join 开通执行器线程池")

    def test_a_malformed_roster_is_rejected_before_anything_runs(self) -> None:
        """名单写错 → 退出码 2、开通入口一次都没被调用（零写入）。"""

        code, _, err = self._run(
            [*self._argv("email,position,company_scope\na@b.com,不存在的职位,1011\n"), "--apply"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.started, [])
        self.assertIn("名单不可用", err)

    def test_an_unregistered_initiator_is_rejected_before_anything_runs(self) -> None:
        self._patch(TOOL, "initiated_by_is_registered_admin", lambda lookup, open_id: False)
        code, _, err = self._run([*self._argv(self.ROSTER), "--apply"])
        self.assertEqual(code, 2)
        self.assertEqual(self.started, [])
        self.assertIn("已登记管理员", err)

    def test_the_help_text_states_the_scheduler_container_constraint(self) -> None:
        """硬约束必须出现在 ``--help`` 上，不能只写在源码文档里。"""

        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            TOOL.main(["--help"])
        self.assertIn("lingxi-scheduler", out.getvalue())


class _FakeCursor:
    """记录 SQL 与参数的假 cursor；只实现落库口用到的三个方法。"""

    def __init__(self, existing: list[tuple[str, str, str]] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._existing = existing or []
        self._pending: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.calls.append((" ".join(sql.split()), tuple(params)))
        if "FROM local_permission_override" in sql and sql.strip().upper().startswith("SELECT"):
            self._pending = list(self._existing)

    def fetchall(self) -> list[tuple]:
        rows, self._pending = self._pending, []
        return rows

    def fetchone(self) -> tuple | None:
        return None

    def statements(self, needle: str) -> list[tuple[str, tuple]]:
        return [call for call in self.calls if needle in call[0]]


class SyntheticPendingActionTest(unittest.TestCase):
    """`pending_action_id` 是结构性 NOT NULL 外键：不合成一条终态确认记录就一行都写不进去。

    这里用假 cursor 断言合成出来的那一条究竟带了什么——`reason='preprovision_2_0'`
    被改成别的值（例如复用 `legacy_import_2_0`）会当场变红。
    """

    def _apply(self, cursor: _FakeCursor):
        return _apply_position_grant_locked(
            cursor,
            user_id="usr_1",
            target_open_id="ou_target",
            plan=_plan(),
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
            initiated_by_open_id="ou_admin",
        )

    def test_the_synthetic_pending_action_carries_the_preprovision_reason(self) -> None:
        cursor = _FakeCursor()
        report = self._apply(cursor)
        self.assertEqual(report.imported, 2)
        inserts = cursor.statements("INSERT INTO pending_action")
        self.assertEqual(len(inserts), 1, "一笔预授权只合成一条 pending_action，全部行共用它")
        params = inserts[0][1]
        self.assertIn(PREPROVISION_PENDING_ACTION_REASON, params)
        self.assertEqual(PREPROVISION_PENDING_ACTION_REASON, "preprovision_2_0")
        self.assertNotIn("legacy_import_2_0", params)
        self.assertIn("local_permission_grant", params)
        self.assertIn("ou_admin", params)
        self.assertIn("'executed'", inserts[0][0])
        self.assertIn("FALSE", inserts[0][0])

    def test_every_row_shares_one_group_id_and_carries_the_position_and_scope(self) -> None:
        """整组撤销要成立，本笔的全部行必须共享同一个组 ID 并带上职位与公司范围。"""

        cursor = _FakeCursor()
        self._apply(cursor)
        rows = cursor.statements("INSERT INTO local_permission_override")
        self.assertEqual(len(rows), 2)
        groups = {row[1][-1] for row in rows}
        self.assertEqual(len(groups), 1)
        self.assertTrue(next(iter(groups)).startswith("lpg_"))
        for _sql, params in rows:
            self.assertIn("A国家总经理", params)
            self.assertIn("1011", params)
            self.assertIn(PREPROVISION_OVERRIDE_REASON, params)

    def test_an_already_active_key_is_not_written_again(self) -> None:
        cursor = _FakeCursor(
            existing=[("1011", "sub_new_count", "active"), ("1011", "exchange_rate", "active")]
        )
        report = self._apply(cursor)
        self.assertEqual((report.imported, report.already_present), (0, 2))
        self.assertEqual(cursor.statements("INSERT INTO pending_action"), [])

    def test_a_revoked_key_is_not_resurrected(self) -> None:
        """管理员撤销过的键不复活——重跑名单不会把它按新组重建。"""

        cursor = _FakeCursor(
            existing=[("1011", "sub_new_count", "revoked"), ("1011", "exchange_rate", "revoked")]
        )
        report = self._apply(cursor)
        self.assertEqual((report.imported, report.revoked_skipped), (0, 2))
        self.assertEqual(cursor.statements("INSERT INTO pending_action"), [])


if __name__ == "__main__":
    unittest.main()


class PreprovisionGrantSeamTests(unittest.TestCase):
    """预授权落库口的**接缝**：调用方与实现方的关键字必须逐字一致。

    这条用例存在的理由是一次真实的接缝缺陷：S-8a（调用方）写 ``grant=``、S-8b
    （实现方）写 ``plan=``，两边各自的测试都绿——因为调用方测的是假的 importer、
    实现方测的是直接调自己。真实链路第一次跑到这里才会 ``TypeError``，而那时
    已经在 stage 或生产。**与 rc25 S-1「装了存量令牌源、没装差集导入口」是同一
    形状**：装配缝隙不会被任何一侧的单元测试照到，只能由一条专门盯缝的用例守住。

    这里刻意用签名比对而不是「跑一次真链路」：真链路要真库、要装配、要凭据，
    成本高且容易被跳过；签名比对零依赖、必然执行，而它挡住的正是这次真实发生
    的那个错法。
    """

    def _keyword_names(self, function: object) -> tuple[str, ...]:
        import inspect

        return tuple(
            name
            for name, parameter in inspect.signature(function).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        )

    def test_the_real_store_and_the_chain_protocol_declare_the_same_keywords(self) -> None:
        from lingxi.adapters.postgres_local_permission import (
            PostgresLocalPermissionOverrideStore,
        )
        from lingxi.core.identity.preprovision import PositionGrantImporter

        self.assertEqual(
            self._keyword_names(PostgresLocalPermissionOverrideStore.import_position_grant),
            self._keyword_names(PositionGrantImporter.import_position_grant),
            "预授权落库口两侧的关键字漂移了：调用方按 Protocol 传参，实现方对不上就是运行时 TypeError，"
            "而两侧各自的单元测试都照不到。改任一侧都要同时改另一侧。",
        )

    def test_the_real_start_system_binds_the_kwargs_the_script_actually_passes(self) -> None:
        """脚本 → 开通链的另一半接缝：``run_preprovision`` 传的五个关键字必须能被
        ``AutoOnboardingRunner.start_system`` bind。上一条守的是"链 → 落库口"，这条
        守的是"脚本 → 链"——同一类缺陷（两侧单测都绿、真实链路才 ``TypeError``）在
        这条边上同样存在：脚本侧测的是假 ``start_system``，链侧测的是自己直接调。"""

        import inspect

        from lingxi.core.identity.onboarding_runner import AutoOnboardingRunner

        signature = inspect.signature(AutoOnboardingRunner.start_system)
        # 这五个关键字逐字抄自 scripts/ops/preprovision.py::run_preprovision 的调用点。
        signature.bind(
            object(),
            email="a@b.com",
            trace_id="trace",
            origin=TOOL.ORIGIN_PREPROVISION,
            initiated_by_open_id="ou_admin",
            preprovision_grant=object(),
        )

    def test_the_scripts_origin_is_the_chains_own_constant(self) -> None:
        """``origin`` 在链侧是失败关闭判据（不等于它就 ``ValueError``），且决定合成
        事件标识前缀＝「静默 / 记账」的判据。脚本必须用链的那个常量，不是同值字面量。"""

        from lingxi.core.identity.preprovision import ORIGIN_PREPROVISION

        self.assertIs(TOOL.ORIGIN_PREPROVISION, ORIGIN_PREPROVISION)

    def test_the_real_store_binds_the_kwargs_the_chain_actually_passes(self) -> None:
        import inspect

        from lingxi.adapters.postgres_local_permission import (
            PostgresLocalPermissionOverrideStore,
        )

        signature = inspect.signature(
            PostgresLocalPermissionOverrideStore.import_position_grant
        )
        # 这五个关键字逐字抄自真实调用点 core/identity/preprovision.py::import_preprovision_grant。
        signature.bind(
            object(),
            user_id="usr_seam",
            target_open_id="ou_seam",
            grant=object(),
            now=object(),
            initiated_by_open_id="ou_admin",
        )
