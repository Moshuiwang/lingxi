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
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lingxi.core.admin.registry import ALL_ADMIN_ROLES, AdminRegistryEntry, AdminRole

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

    def test_all_companies_key_constant_matches_publish_row(self) -> None:
        """本工具的通配拒绝/跳过分支全都判 :data:`TOOL.ALL_COMPANIES_KEY`——
        钉住它确实是 `publish_row` 的同一个 "*" 字面量，不是本文件另起的
        独立字符串（否则两边悄悄漂移，判断形同虚设）。"""

        self.assertEqual(TOOL.ALL_COMPANIES_KEY, "*")

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
        country_rows=(
            {"country_key": "101", "name": "ALPHA", "name_cn": "甲国", "boss_company_id": "BC-甲"},
        ),
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

    def test_a_company_outside_the_translation_mapping_is_still_planned(self) -> None:
        """**只拒值、不拒键**（rc25 S-2d 与 S-1 裁定一致）：映射里根本没有 "9999"
        这家公司，差集照样产出这条 grant——「本地是本地的」，问数 MCP 认识 40–43
        这类映射外公司。

        变异存活证据：若把 P-2 的值校验误写成"公司键必须在映射内"（不论落在
        `load_legacy_export` 还是这一层），本用例立刻从 1 条 grant 变红成 0 条。
        """

        legacy = {GRANTED_USER.email: {"9999": ("存量指标",)}}

        plan = TOOL.plan_import(
            legacy=legacy,
            galaxy=_granted_galaxy_snapshot(),
            role_function_map=ROLE_FUNCTION_MAP,
            metric_translation_map=METRIC_TRANSLATION_MAP,
            lookup_user=lambda email: TOOL.UserLookup(record=GRANTED_USER),
        )

        self.assertEqual(len(plan.grants), 1)
        self.assertEqual(plan.grants[0].company_id, "9999")
        self.assertEqual(plan.grants[0].metric_name, "存量指标")
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

    def test_a_wildcard_galaxy_current_is_registered_as_skipped_not_silent(self) -> None:
        """rc21 修复包 B（P1+P2+P3 之 b）：银河侧命中通配时，改动前直接调用
        `compute_company_diff` 拿到空字典、不产出任何 grant 也不产出任何
        skip——dry-run 清单上完全看不出这个用户发生过什么。现在必须显式登记
        进 `plan.skipped`，原因码是 `REASON_WILDCARD_GALAXY_CURRENT`。

        变异存活证据：把 `plan_import` 里 `ALL_COMPANIES_KEY in galaxy_current`
        这条分支删掉（退回直接调用 `compute_company_diff`），本用例的
        `plan.skipped` 断言会从"恰好一条 wildcard_galaxy_current"变红成
        "空元组"——差集本身仍然正确（恒空），但可见性回归静默。

        用「角色即全公司」B 口径特例（``ADMIN_FULL_ACCESS_FUNCTION``）触发
        通配——不搭建「全非」datacountry 哨兵行，两条路径殊途同归地产出同一种
        ``galaxy_current == {ALL_COMPANIES_KEY: (...)}`` 形状（见
        ``publish_row.aggregate_permission`` 模块文档「角色即全公司」一节）。
        """

        admin_galaxy = _FakeGalaxySnapshot(
            user_rows=({"user_id": "G-10001", "user_name": "10001", "email": GRANTED_USER.email},),
            country_rows=(
                {
                    "country_key": "101",
                    "name": "ALPHA",
                    "name_cn": "甲国",
                    "boss_company_id": "BC-甲",
                },
            ),
            _role_rows_by_user={"G-10001": ({"user_id": "G-10001", "role_name": "管理员角色"},)},
            _datacountry_rows_by_user={
                "G-10001": ({"user_id": "G-10001", "datacountry_id": "101"},)
            },
        )
        legacy = {GRANTED_USER.email: {"BC-甲": ("旧表独有指标",)}}

        plan = TOOL.plan_import(
            legacy=legacy,
            galaxy=admin_galaxy,
            role_function_map={"管理员角色": "后台管理员"},
            metric_translation_map={"*": {"后台管理员": ("全部指标",)}},
            lookup_user=lambda email: TOOL.UserLookup(record=GRANTED_USER),
        )

        self.assertEqual(plan.grants, (), "通配下差集本身仍然恒为空，不产出任何 grant")
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(plan.skipped[0].email, GRANTED_USER.email)
        self.assertEqual(plan.skipped[0].reason, TOOL.REASON_WILDCARD_GALAXY_CURRENT)

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
            user_id="usr_b",
            employee_no="",
            email="b@example.com",
            feishu_open_id="ou_b",
            display_name="乙",
            account_state="enabled",
        )
        user_a = TOOL.AppUserRecord(
            user_id="usr_a",
            employee_no="",
            email="a@example.com",
            feishu_open_id="ou_a",
            display_name="甲",
            account_state="enabled",
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
            "email,permissions\n"
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

    def test_a_wildcard_permissions_key_rejects_the_whole_export(self) -> None:
        """rc21 修复包 B（P1+P2+P3 之 a）：旧表任意一行的 ``permissions`` 出现
        ``"*"``（``ALL_COMPANIES_KEY``）键，整份导出拒绝——不是这一行单独跳过。

        复现：``{"*": [...]}`` 这样的一行若被当作普通 ``company_id="*"``
        写进 ``local_permission_override``，会让该用户凭一条本地授权行跨公司
        越权（读侧 ``lookup_metrics`` 的"*"回退制）。

        变异存活证据：把 `load_legacy_export` 里 `ALL_COMPANIES_KEY in
        permissions` 这条判据删掉，本用例会从抛出 `ValueError` 变红成
        `load_legacy_export` 正常返回一份包含 "*" 键的结果。
        """

        path = self._write(
            "email,permissions\n"
            'a@example.com,"{""1011"": [""日活""]}"\n'
            'wildcard@example.com,"{""*"": [""全部指标""]}"\n'
        )

        with self.assertRaises(ValueError) as raised:
            TOOL.load_legacy_export(path)

        self.assertIn("#263", str(raised.exception))

    def test_a_wildcard_permissions_key_alongside_specific_companies_is_still_rejected(
        self,
    ) -> None:
        """通配键与具体公司键混在同一行也一样整体拒绝——不是"只挡纯通配行"。"""

        path = self._write(
            'email,permissions\na@example.com,"{""1011"": [""日活""], ""*"": [""全部指标""]}"\n'
        )

        with self.assertRaises(ValueError):
            TOOL.load_legacy_export(path)

    # ---- rc25 S-2d（对抗审查 P-2）：只判键不判值曾让 "*" 值直通 ------------

    def test_a_wildcard_metric_value_rejects_the_whole_export(self) -> None:
        """``{"1011": ["*"]}``——通配在**值**里。改动前只判了键，这一行直通落库，
        读侧 ``lookup_metrics`` 的回退制会让它等于"1011 全部指标（含未来新增）"，
        且此后对单个指标的抑制减不掉它。

        变异存活证据：把 `load_legacy_export` 里 `classify_legacy_permissions` 那段
        `shape != SHAPE_SPECIFIC` 的判据删掉，本用例会从抛出 `ValueError` 变红成
        `load_legacy_export` 正常返回一份含 `("*",)` 值的结果。
        """

        path = self._write(
            "email,permissions\n"
            'ok@example.com,"{""1011"": [""日活""]}"\n'
            'star@example.com,"{""1011"": [""*""]}"\n'
        )

        with self.assertRaises(ValueError) as raised:
            TOOL.load_legacy_export(path)

        self.assertIn("star@example.com", str(raised.exception))

    def test_a_metric_value_that_merely_contains_a_star_is_also_rejected(self) -> None:
        """``"日活*"``：``"*"`` 混进名字里同样整份拒绝（卫生判据，不是形状判据）。"""

        path = self._write('email,permissions\na@example.com,"{""1011"": [""日活*""]}"\n')

        with self.assertRaises(ValueError):
            TOOL.load_legacy_export(path)

    def test_a_blank_metric_value_rejects_the_whole_export(self) -> None:
        path = self._write(
            "email,permissions\n"
            'ok@example.com,"{""1011"": [""日活""]}"\n'
            'blank@example.com,"{""1011"": [""   ""]}"\n'
        )

        with self.assertRaises(ValueError) as raised:
            TOOL.load_legacy_export(path)

        self.assertIn("blank@example.com", str(raised.exception))

    def test_a_newline_inside_a_metric_value_rejects_the_whole_export(self) -> None:
        """指标名里夹一个换行——CSV 引号内的换行是合法字段内容，解析得出来，
        但一个名字里带换行的指标不可能匹配映射里的任何东西，只可能是导出坏了
        或有人在藏东西：整份拒绝，不"尽力解析"。"""

        path = self._write('email,permissions\nnl@example.com,"{""1011"": [""日\\n活""]}"\n')

        with self.assertRaises(ValueError) as raised:
            TOOL.load_legacy_export(path)

        self.assertIn("nl@example.com", str(raised.exception))

    def test_a_company_key_outside_the_mapping_is_still_accepted(self) -> None:
        """**只拒值、不拒键**：映射外公司（40–43 这类）照常解析通过——「本地是
        本地的」是 PM 2026-09-02 对 rc25 S-1 的明示裁定，生产上的首聊路径依赖它。

        变异存活证据：若把值校验误写成"公司键必须在映射/目录内"，本用例立刻变红。
        """

        path = self._write('email,permissions\na@example.com,"{""9999"": [""日活""]}"\n')

        self.assertEqual(TOOL.load_legacy_export(path), {"a@example.com": {"9999": ("日活",)}})


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
            TOOL._existing_active_grant(
                cursor, user_id="usr_1", company_id="1011", metric_name="日活"
            )
        )

    def test_no_hit_reports_absent(self) -> None:
        cursor = _FakeCursor(hit=False)

        self.assertFalse(
            TOOL._existing_active_grant(
                cursor, user_id="usr_1", company_id="1011", metric_name="日活"
            )
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
                    email="a@example.com",
                    user_id="usr_a",
                    feishu_open_id="ou_a",
                    company_id="1011",
                    metric_name="日活",
                ),
            ),
            skipped=(
                TOOL.SkippedUser(email="b@example.com", reason=TOOL.REASON_APP_USER_NOT_FOUND),
            ),
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


# ---------------------------------------------------------------------------
# 七、main() 的两道前置闸门（rc25 S-2d，对抗审查 P-2 / P-8）：
#     ① --initiated-by 必须是生效的已登记管理员；② 导出的**值**必须干净。
#     两道闸都必须在**任何写入之前**拒绝——退出码非 0，`apply_grant` 一次都不许被调用。
# ---------------------------------------------------------------------------


class _FakeAdminRegistryLookup:
    """假 ``admin_registry`` 读侧：只认一个 open_id，其余一律查无（返回 ``None``）。

    条目的 ``entry_status``/``roles`` 可调，用来验证判定确实走的是
    ``core/admin/registry.is_authorized_admin``（active + 三类角色全真），而不是
    "查到一行就算管理员"。
    """

    def __init__(
        self,
        *,
        authorized_open_id: str | None,
        entry_status: str = "active",
        roles: frozenset = ALL_ADMIN_ROLES,
        error: Exception | None = None,
    ) -> None:
        self._authorized_open_id = authorized_open_id
        self._entry_status = entry_status
        self._roles = roles
        self._error = error
        self.asked: list[str] = []

    def active_entry(self, *, open_id: str) -> Any:
        self.asked.append(open_id)
        if self._error is not None:
            raise self._error
        if open_id != self._authorized_open_id:
            return None
        return AdminRegistryEntry(
            feishu_open_id=open_id,
            label="测试管理员",
            roles=self._roles,
            entry_status=self._entry_status,
        )


class MainGateTests(unittest.TestCase):
    ADMIN_OPEN_ID = "ou_registered_admin"
    GOOD_CSV = 'email,permissions\na@example.com,"{""1011"": [""日活""]}"\n'
    STAR_VALUE_CSV = 'email,permissions\nstar@example.com,"{""1011"": [""*""]}"\n'
    BLANK_VALUE_CSV = 'email,permissions\nblank@example.com,"{""1011"": [""   ""]}"\n'
    NEWLINE_VALUE_CSV = 'email,permissions\nnl@example.com,"{""1011"": [""日\\n活""]}"\n'

    def setUp(self) -> None:
        self.applied: list[Any] = []
        self._install_lookup(_FakeAdminRegistryLookup(authorized_open_id=self.ADMIN_OPEN_ID))

        original_apply = TOOL.apply_grant

        def _forbidden_apply(*args: Any, **kwargs: Any) -> bool:
            # 闸门用例里一行都不该落库：记下来让断言看得见，同时立刻炸掉，
            # 避免"记了一笔却继续跑下去"把一次真正的回归伪装成通过。
            self.applied.append((args, kwargs))
            raise AssertionError("前置闸门之后不该有任何写入")

        TOOL.apply_grant = _forbidden_apply
        self.addCleanup(setattr, TOOL, "apply_grant", original_apply)

    def _install_lookup(self, lookup: _FakeAdminRegistryLookup) -> None:
        original = TOOL.resolve_admin_registry_lookup
        TOOL.resolve_admin_registry_lookup = lambda dsn: lookup
        self.addCleanup(setattr, TOOL, "resolve_admin_registry_lookup", original)
        self.lookup = lookup

    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def _run_apply(self, csv_text: str, *, initiated_by: str | None = None) -> tuple[int, str]:
        """按最危险的姿势跑一次：``--apply``（真正写入的那一档）。闸门必须在这一档
        下也整份拒绝，不能只在 dry-run 下报警。"""

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = TOOL.main(
                [
                    str(self._write(csv_text)),
                    "--initiated-by",
                    initiated_by if initiated_by is not None else self.ADMIN_OPEN_ID,
                    "--dsn",
                    "postgresql://unused/unused",
                    "--apply",
                ]
            )
        return code, err.getvalue()

    def test_an_abbreviated_flag_is_rejected_with_zero_side_effects(self) -> None:
        """rc25 修复包 F5：``allow_abbrev=False``——``--ap`` 不得被解析成
        ``--apply``（与 scripts/ops/preprovision.py 同一条纪律：手滑半个词不能
        等于授权真实写入）。缩写被拒必须退出码非 0 且零写入。"""

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                TOOL.main(
                    [
                        str(self._write(self.GOOD_CSV)),
                        "--initiated-by",
                        self.ADMIN_OPEN_ID,
                        "--dsn",
                        "postgresql://unused/unused",
                        "--ap",
                    ]
                )

        self.assertNotEqual(caught.exception.code, 0)
        self.assertEqual(self.applied, [], "缩写被拒＝零写入")

    # ---- P-2：值不干净 → 整份拒绝、退出码非 0、零写入 ----------------------

    def test_a_wildcard_metric_value_is_rejected_before_any_write(self) -> None:
        code, stderr = self._run_apply(self.STAR_VALUE_CSV)

        self.assertNotEqual(code, 0)
        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [], "整份拒绝＝零写入，不是跳过这一条继续导入")
        self.assertIn("star@example.com", stderr)

    def test_a_blank_metric_value_is_rejected_before_any_write(self) -> None:
        code, stderr = self._run_apply(self.BLANK_VALUE_CSV)

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])
        self.assertIn("blank@example.com", stderr)

    def test_a_newline_metric_value_is_rejected_before_any_write(self) -> None:
        code, stderr = self._run_apply(self.NEWLINE_VALUE_CSV)

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])
        self.assertIn("nl@example.com", stderr)

    # ---- P-8：--initiated-by 必须是生效的已登记管理员 ----------------------

    def test_an_unregistered_initiated_by_is_rejected_with_zero_writes(self) -> None:
        """责任人不是已登记管理员：连那份（本身完全合格的）导出都不该被处理。

        变异存活证据：把 `main()` 里 `initiated_by_is_registered_admin` 那段闸门
        删掉，本用例会从 `code == 2` 变红——`main()` 会继续往下走去连库。
        """

        code, stderr = self._run_apply(self.GOOD_CSV, initiated_by="ou_stranger")

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.lookup.asked, ["ou_stranger"], "闸门必须真的去问登记表")
        self.assertIn("已登记管理员", stderr)

    def test_a_revoked_registration_is_not_an_admin(self) -> None:
        """判定复用 `is_authorized_admin`：查到一行不等于是管理员，撤销过的不算。"""

        self._install_lookup(
            _FakeAdminRegistryLookup(authorized_open_id=self.ADMIN_OPEN_ID, entry_status="revoked")
        )

        code, _ = self._run_apply(self.GOOD_CSV)

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])

    def test_a_partially_granted_registration_is_not_an_admin(self) -> None:
        """三类角色没有全部授予同样不算——本仓库没有"部分权限的管理员"这个概念。"""

        self._install_lookup(
            _FakeAdminRegistryLookup(
                authorized_open_id=self.ADMIN_OPEN_ID,
                roles=frozenset({AdminRole.PERMISSION_ADMIN}),
            )
        )

        code, _ = self._run_apply(self.GOOD_CSV)

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])

    def test_a_registry_read_failure_fails_closed(self) -> None:
        """登记表读不出来 → 拒绝。查不到答案不等于答案是"是"。"""

        self._install_lookup(
            _FakeAdminRegistryLookup(
                authorized_open_id=self.ADMIN_OPEN_ID,
                error=RuntimeError("dsn=postgresql://user:pw@host/db 连接失败"),
            )
        )

        code, stderr = self._run_apply(self.GOOD_CSV)

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])
        self.assertIn("管理员登记表不可读", stderr)
        self.assertIn("RuntimeError", stderr)
        self.assertNotIn("postgresql://", stderr, "只登记异常类型名，不把异常正文抄进输出")

    def test_a_registered_admin_passes_the_gate_and_the_export_check_still_runs(self) -> None:
        """正向对照：闸门不是"一律拒绝"。已登记管理员放行后，拒绝理由变成导出
        本身不合格那一条——证明这次运行确实越过了 P-8 那道闸。"""

        code, stderr = self._run_apply("email,other\na@example.com,x\n")

        self.assertEqual(code, 2)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.lookup.asked, [self.ADMIN_OPEN_ID])
        self.assertIn("旧表导出读取失败", stderr)
        self.assertNotIn("已登记管理员", stderr)


if __name__ == "__main__":
    unittest.main()
