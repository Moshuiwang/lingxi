"""`scripts/ops/import_local_permission_override.py` 的纯逻辑与假实现用例（Issue #441）。

与 `tests/test_size_ratchet_check.py` 同一惯例——`scripts/` 下的文件用
`importlib.util.spec_from_file_location` 按路径加载，不依赖它出现在
`PYTHONPATH` 上；脚本内部对 `lingxi.*` 的 import 由测试运行时的
`PYTHONPATH=src` 解析，与如何加载脚本本身无关。

本文件只测**零 I/O 的纯逻辑**（差集计算、用户级判定编排、CSV 解析、幂等短路
查询的判断逻辑）；真正的数据库写入路径（`apply_grant`）只有真库能证伪，见
`tests/test_import_local_permission_override_postgres.py`。
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCRIPT = Path(__file__).parents[1] / "scripts" / "ops" / "import_local_permission_override.py"


def _load_script():
    module_name = "import_local_permission_override_under_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 脚本内部用了 `from __future__ import annotations`，`@dataclass` 因此需要
    # 在 `sys.modules[cls.__module__]` 里查到本模块才能解析延迟求值的类型注解
    # （标准库 `dataclasses._is_type` 的既有行为）——先登记再执行，否则字段类型
    # 里任何一个非内建名字都会在 `exec_module` 这一步就地炸掉。
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()


# ---------------------------------------------------------------------------
# 一、差集计算（compute_company_diff）
# ---------------------------------------------------------------------------


class CompanyDiffTests(unittest.TestCase):
    def test_legacy_only_metric_is_in_the_diff(self) -> None:
        diff = TOOL.compute_company_diff({"1011": ("日活", "收入")}, {"1011": ("日活",)})

        self.assertEqual(diff, {"1011": ("收入",)})

    def test_a_company_fully_covered_by_galaxy_is_dropped_not_written_empty(self) -> None:
        diff = TOOL.compute_company_diff({"1011": ("日活",)}, {"1011": ("日活",)})

        self.assertEqual(diff, {}, "差集为空的公司键必须被丢弃，不写空列表")

    def test_a_company_the_legacy_side_never_had_does_not_appear(self) -> None:
        diff = TOOL.compute_company_diff({"1011": ("日活",)}, {"1099": ("收入",)})

        self.assertEqual(diff, {"1011": ("日活",)}, "银河多出的公司不倒灌进差集")

    def test_values_are_sorted_and_deduplicated(self) -> None:
        diff = TOOL.compute_company_diff({"1011": ("收入", "日活", "收入")}, {})

        self.assertEqual(diff["1011"], ("收入", "日活"))

    def test_wildcard_galaxy_current_makes_the_whole_diff_empty(self) -> None:
        """通配（513 后台管理员）已经覆盖旧表可能给出的任何具体公司权限——变异
        锚点：临时删掉这条短路分支后，本用例必须变红（会算出一个多余的 1011 键）。
        """

        diff = TOOL.compute_company_diff(
            {"1011": ("日活",)}, {TOOL.ALL_COMPANIES_KEY: ("全部指标",)}
        )

        self.assertEqual(diff, {})

    def test_empty_legacy_produces_empty_diff(self) -> None:
        self.assertEqual(TOOL.compute_company_diff({}, {"1011": ("日活",)}), {})


# ---------------------------------------------------------------------------
# 二、resolve_galaxy_current：匹配 + 聚合 + 翻译，注入假银河快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeGalaxySnapshot:
    user_rows: tuple[Mapping[str, Any], ...]
    country_rows: tuple[Mapping[str, Any], ...]
    _role_rows_by_user: Mapping[str, tuple[Mapping[str, Any], ...]]
    _datacountry_rows_by_user: Mapping[str, tuple[Mapping[str, Any], ...]]

    def role_rows(self, galaxy_user_id: Any) -> tuple[Mapping[str, Any], ...]:
        return self._role_rows_by_user.get(str(galaxy_user_id), ())

    def datacountry_rows(self, galaxy_user_id: Any) -> tuple[Mapping[str, Any], ...]:
        return self._datacountry_rows_by_user.get(str(galaxy_user_id), ())


ROLE_FUNCTION_MAP = {"A运营": "运营"}
METRIC_TRANSLATION_MAP = {"BC-甲": {"运营": ("日活",)}}

GRANTED_USER = TOOL.AppUserRecord(
    user_id="usr_1",
    employee_no="10001",
    email="zhang.san@example.com",
    feishu_open_id="ou_zhang",
    display_name="张三",
    account_state="enabled",
)


def _granted_galaxy_snapshot() -> _FakeGalaxySnapshot:
    return _FakeGalaxySnapshot(
        user_rows=({"user_id": "G-10001", "user_name": "10001", "email": GRANTED_USER.email},),
        country_rows=({"country_key": "101", "name": "ALPHA", "name_cn": "甲国", "boss_company_id": "BC-甲"},),
        _role_rows_by_user={"G-10001": ({"user_id": "G-10001", "role_name": "A运营"},)},
        _datacountry_rows_by_user={"G-10001": ({"user_id": "G-10001", "datacountry_id": "101"},)},
    )


class ResolveGalaxyCurrentTests(unittest.TestCase):
    def test_matched_and_granted_returns_translated_metrics(self) -> None:
        result, reason = TOOL.resolve_galaxy_current(
            app_user=GRANTED_USER,
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
        )

        self.assertIsNone(reason)
        self.assertEqual(result, {"BC-甲": ("日活",)})

    def test_unmatched_account_is_skipped_with_a_reason(self) -> None:
        empty_galaxy = _FakeGalaxySnapshot(
            user_rows=(), country_rows=(), _role_rows_by_user={}, _datacountry_rows_by_user={}
        )

        result, reason = TOOL.resolve_galaxy_current(
            app_user=GRANTED_USER,
            galaxy=empty_galaxy,
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
        )

        self.assertIsNone(result)
        self.assertTrue(reason.startswith(TOOL.REASON_GALAXY_ACCOUNT_PREFIX), reason)

    def test_zero_galaxy_permission_returns_empty_mapping_not_a_skip(self) -> None:
        """银河判定"无可用权限"不是跳过——空映射对差集计算恒等，旧表内容全部
        计入差集（`V-权限-15` 零银河兜底同一口径）。"""

        no_role_galaxy = _FakeGalaxySnapshot(
            user_rows=({"user_id": "G-10001", "user_name": "10001", "email": GRANTED_USER.email},),
            country_rows=(),
            _role_rows_by_user={},
            _datacountry_rows_by_user={},
        )

        result, reason = TOOL.resolve_galaxy_current(
            app_user=GRANTED_USER,
            galaxy=no_role_galaxy,
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
        )

        self.assertIsNone(reason)
        self.assertEqual(result, {})

    def test_uncovered_translation_is_skipped_not_guessed(self) -> None:
        result, reason = TOOL.resolve_galaxy_current(
            app_user=GRANTED_USER,
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map={},
        )

        self.assertIsNone(result)
        self.assertEqual(reason, TOOL.REASON_TRANSLATION_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 三、plan_import：整体编排，注入假 lookup_user
# ---------------------------------------------------------------------------


class PlanImportTests(unittest.TestCase):
    def test_a_matched_user_with_a_diff_produces_a_planned_grant(self) -> None:
        legacy = {GRANTED_USER.email: {"BC-甲": ("日活", "存量指标")}}

        plan = TOOL.plan_import(
            legacy=legacy,
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=GRANTED_USER),
        )

        self.assertEqual(len(plan.grants), 1)
        grant = plan.grants[0]
        self.assertEqual(grant.email, GRANTED_USER.email)
        self.assertEqual(grant.user_id, GRANTED_USER.user_id)
        self.assertEqual(grant.feishu_open_id, GRANTED_USER.feishu_open_id)
        self.assertEqual(grant.company_id, "BC-甲")
        self.assertEqual(grant.metric_name, "存量指标")
        self.assertEqual(plan.skipped, ())

    def test_a_diff_that_is_fully_covered_by_galaxy_produces_no_grant(self) -> None:
        legacy = {GRANTED_USER.email: {"BC-甲": ("日活",)}}

        plan = TOOL.plan_import(
            legacy=legacy,
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=GRANTED_USER),
        )

        self.assertEqual(plan.grants, ())
        self.assertEqual(plan.skipped, ())

    def test_an_ambiguous_app_user_is_skipped(self) -> None:
        plan = TOOL.plan_import(
            legacy={"dup@example.com": {"1011": ("日活",)}},
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=None, ambiguous=True),
        )

        self.assertEqual(plan.grants, ())
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(plan.skipped[0].reason, TOOL.REASON_APP_USER_AMBIGUOUS)

    def test_an_unknown_email_is_skipped(self) -> None:
        plan = TOOL.plan_import(
            legacy={"nobody@example.com": {"1011": ("日活",)}},
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=None),
        )

        self.assertEqual(plan.grants, ())
        self.assertEqual(plan.skipped[0].reason, TOOL.REASON_APP_USER_NOT_FOUND)

    def test_a_disabled_account_is_skipped_without_touching_galaxy(self) -> None:
        disabled_user = TOOL.AppUserRecord(
            user_id="usr_2",
            employee_no="10002",
            email="li.si@example.com",
            feishu_open_id="ou_li",
            display_name="李四",
            account_state="suspended",
        )

        class _ExplodingGalaxy:
            user_rows = ()
            country_rows = ()

            def role_rows(self, galaxy_user_id: Any) -> tuple:  # pragma: no cover - 调用即失败
                raise AssertionError("停用账号不该走到银河匹配这一步")

            def datacountry_rows(self, galaxy_user_id: Any) -> tuple:  # pragma: no cover
                raise AssertionError("停用账号不该走到银河匹配这一步")

        plan = TOOL.plan_import(
            legacy={disabled_user.email: {"1011": ("日活",)}},
            galaxy=_ExplodingGalaxy(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=disabled_user),
        )

        self.assertEqual(plan.grants, ())
        self.assertEqual(plan.skipped[0].reason, TOOL.REASON_ACCOUNT_NOT_ENABLED)

    def test_plan_is_deterministically_ordered_by_email_company_metric(self) -> None:
        user_b = TOOL.AppUserRecord(
            user_id="usr_b", employee_no="", email="b@example.com",
            feishu_open_id="ou_b", display_name="乙", account_state="enabled",
        )
        user_a = TOOL.AppUserRecord(
            user_id="usr_a", employee_no="", email="a@example.com",
            feishu_open_id="ou_a", display_name="甲", account_state="enabled",
        )
        legacy = {
            user_b.email: {"1099": ("z指标", "a指标")},
            user_a.email: {"1011": ("日活",)},
        }
        empty_galaxy = _FakeGalaxySnapshot(
            user_rows=(), country_rows=(), _role_rows_by_user={}, _datacountry_rows_by_user={}
        )
        records = {user_a.email: user_a, user_b.email: user_b}

        plan = TOOL.plan_import(
            legacy=legacy,
            galaxy=empty_galaxy,
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=records[email]),
        )

        # 两个账号在这份夹具下都命中 `not_found`（没有布进任何银河用户行），因此
        # 两人都被跳过、零发布计划——这条用例只钉排序契约，不依赖具体产出内容，
        # 换一份两人都能匹配成功的夹具来钉排序会让本用例同时依赖匹配细节。
        self.assertEqual(plan.grants, ())
        self.assertEqual([item.email for item in plan.skipped], [user_a.email, user_b.email])


# ---------------------------------------------------------------------------
# 四、load_legacy_export：CSV 解析
# ---------------------------------------------------------------------------


class LoadLegacyExportTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_a_well_formed_export_parses(self) -> None:
        path = self._write('email,permissions\nZhang.San@Example.com,"{""1011"": [""日活""]}"\n')

        result = TOOL.load_legacy_export(path)

        self.assertEqual(result, {"zhang.san@example.com": {"1011": ("日活",)}})

    def test_missing_required_columns_is_rejected(self) -> None:
        path = self._write("email,other\na@example.com,x\n")

        with self.assertRaises(ValueError):
            TOOL.load_legacy_export(path)

    def test_a_duplicate_email_is_rejected_not_silently_merged(self) -> None:
        path = self._write(
            'email,permissions\n'
            'a@example.com,"{""1011"": [""日活""]}"\n'
            'a@example.com,"{""1011"": [""收入""]}"\n'
        )

        with self.assertRaises(ValueError):
            TOOL.load_legacy_export(path)

    def test_a_blank_email_is_rejected(self) -> None:
        path = self._write('email,permissions\n,"{""1011"": [""日活""]}"\n')

        with self.assertRaises(ValueError):
            TOOL.load_legacy_export(path)

    def test_malformed_permissions_json_is_rejected(self) -> None:
        path = self._write("email,permissions\na@example.com,not-json\n")

        with self.assertRaises(ValueError):
            TOOL.load_legacy_export(path)


# ---------------------------------------------------------------------------
# 五、幂等短路查询：_existing_active_grant 用假游标钉住 SQL 形状与判断逻辑
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, *, hit: bool) -> None:
        self._hit = hit
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return (1,) if self._hit else None


class ExistingActiveGrantTests(unittest.TestCase):
    def test_a_hit_reports_present(self) -> None:
        cursor = _FakeCursor(hit=True)

        self.assertTrue(
            TOOL._existing_active_grant(cursor, user_id="usr_1", company_id="1011", metric_name="日活")
        )

    def test_no_hit_reports_absent(self) -> None:
        cursor = _FakeCursor(hit=False)

        self.assertFalse(
            TOOL._existing_active_grant(cursor, user_id="usr_1", company_id="1011", metric_name="日活")
        )

    def test_the_query_filters_on_grant_direction_only(self) -> None:
        """变异锚点：把 `DIRECTION_GRANT` 悄悄换成 `'suppress'` 必须让这条用例
        变红——幂等短路查询如果查错极性，会把一条同键的本地抑制误判成"已经
        导入过"而跳过一笔真正该导入的授权。"""

        cursor = _FakeCursor(hit=False)
        TOOL._existing_active_grant(cursor, user_id="usr_1", company_id="1011", metric_name="日活")

        _, params = cursor.executed[0]
        self.assertIn(TOOL.DIRECTION_GRANT, params)


# ---------------------------------------------------------------------------
# 六、dry-run 输出形状：_print_plan 只是格式化，不碰数据库
# ---------------------------------------------------------------------------


class PrintPlanTests(unittest.TestCase):
    def test_output_lists_every_planned_grant_and_every_skip_reason(self) -> None:
        plan = TOOL.ImportPlan(
            grants=(
                TOOL.PlannedGrant(
                    email="a@example.com", user_id="usr_a", feishu_open_id="ou_a",
                    company_id="1011", metric_name="日活",
                ),
            ),
            skipped=(TOOL.SkippedUser(email="b@example.com", reason=TOOL.REASON_APP_USER_NOT_FOUND),),
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            TOOL._print_plan(plan)
        output = buffer.getvalue()

        self.assertIn("a@example.com", output)
        self.assertIn("1011", output)
        self.assertIn("日活", output)
        self.assertIn("b@example.com", output)
        self.assertIn(TOOL.REASON_APP_USER_NOT_FOUND, output)

    def test_a_plan_with_no_grants_says_so_without_a_grant_line(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            TOOL._print_plan(TOOL.ImportPlan(grants=(), skipped=()))
        output = buffer.getvalue()

        self.assertIn("0", output)
        self.assertNotIn("+ user=", output)


if __name__ == "__main__":
    unittest.main()
