"""夹具契约测试：确定性无权限身份夹具 + MCP 同步最小合法配置。

对应 ``scripts/acceptance_fixtures_identity.py``（Trace v12「受控触发夹具确定性
合同」清单①②，见 Issue #147）。本文件只做一件事：证明该模块登记的每条合成
夹具，喂给**与生产同一份**判定函数后，确实落在它自称的分支——不是本文件另起
一套判断逻辑去猜"应该"落在哪。

装配方式照抄既有先例 ``tests/test_acceptance_fixtures_contract.py``：``scripts/``
不是一个包，用 ``importlib.util.spec_from_file_location`` 按路径直接装载模块。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "acceptance_fixtures_identity.py"

from lingxi.adapters.role_function_map_file import load_role_function_map
from lingxi.core.permission.account_match import MATCHED, NOT_FOUND, match_galaxy_account
from lingxi.core.permission.mcp_readiness import ReadinessSchedule
from lingxi.core.permission.role_function import resolve_role_functions


def _load_script():
    spec = importlib.util.spec_from_file_location("acceptance_fixtures_identity_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NegativeIdentityFixturesMatchProductionParsingTests(unittest.TestCase):
    """每条夹具喂给 ``match_galaxy_account``（生产判定函数本体）必须落在其声明分支。"""

    def setUp(self) -> None:
        self.module = _load_script()

    def test_registry_is_not_a_dead_probe(self) -> None:
        self.assertEqual(len(self.module.NEGATIVE_IDENTITY_FIXTURES), 5)

    def test_each_fixture_reaches_its_declared_branch(self) -> None:
        for fixture in self.module.NEGATIVE_IDENTITY_FIXTURES:
            with self.subTest(name=fixture.name):
                result = match_galaxy_account(
                    fixture.feishu_user_id, fixture.roster_rows, fixture.galaxy_rows
                )
                self.assertEqual(result.state, fixture.expected_state)
                self.assertEqual(result.reason, fixture.expected_reason)
                self.assertIn(result.state, (MATCHED, NOT_FOUND))

    def test_four_of_five_are_not_found_and_one_is_matched(self) -> None:
        """零条/多条/双键冲突/资料不完整落 NOT_FOUND；无支持职能在账号层是 MATCHED
        （职能层才判定不支持），二者分工不能混淆。
        """

        states = [f.expected_state for f in self.module.NEGATIVE_IDENTITY_FIXTURES]
        self.assertEqual(states.count(NOT_FOUND), 4)
        self.assertEqual(states.count(MATCHED), 1)

    def test_unsupported_function_fixture_has_no_mapped_role(self) -> None:
        """UNSUPPORTED_FUNCTION 账号匹配成功后，喂给真实随包配置文件解析出的角色
        必须全部「未映射」——与生产完全同一份 ``galaxy_role_function_map.toml``，
        不是本文件另起一份猜测配置。
        """

        mapping = load_role_function_map()
        resolved = resolve_role_functions(
            self.module.UNSUPPORTED_FUNCTION_ROLE_NAMES, mapping
        )
        self.assertTrue(resolved, "夹具至少要提供一个角色名")
        for role_function in resolved:
            with self.subTest(role=role_function.role_name):
                self.assertFalse(
                    role_function.mapped,
                    f"{role_function.role_name} 在当前配置里意外被映射到了职能，"
                    "夹具已经不能代表「无支持职能」，需要换一个角色名或核对配置改动。",
                )

    def test_fixture_lookup_by_name_round_trips(self) -> None:
        for fixture in self.module.NEGATIVE_IDENTITY_FIXTURES:
            self.assertIs(self.module.negative_identity_fixture_by_name(fixture.name), fixture)

    def test_unknown_fixture_name_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.module.negative_identity_fixture_by_name("不存在的夹具名字")


class McpReadinessMinimumLegalScheduleTests(unittest.TestCase):
    """MCP 同步超时夹具：验收窗口不靠真的等十五分钟。"""

    def setUp(self) -> None:
        self.module = _load_script()

    def test_kwargs_construct_a_legal_schedule(self) -> None:
        schedule = ReadinessSchedule(**self.module.MCP_READINESS_MINIMUM_LEGAL_SCHEDULE_KWARGS)
        self.assertEqual(schedule.attempt_offsets(), (0, 1))
        self.assertEqual(schedule.max_attempts, 2)
        self.assertEqual(schedule.hard_deadline, timedelta(seconds=2))

    def test_schedule_is_strictly_smaller_than_contract_default(self) -> None:
        """最小合法配置必须真的「最小」——预算严格小于合同默认十五分钟，
        否则验收窗口会在这一步意外卡住十五分钟。
        """

        from lingxi.core.permission.mcp_readiness import CONTRACT_SCHEDULE

        minimum = ReadinessSchedule(**self.module.MCP_READINESS_MINIMUM_LEGAL_SCHEDULE_KWARGS)
        self.assertLess(minimum.budget, CONTRACT_SCHEDULE.budget)

    def test_two_attempts_land_at_expected_wallclock_offsets(self) -> None:
        """把配方接到真实的偏移计算函数上，钉住「两次尝试」这个验收现场的
        直觉描述确实等于代码行为，不是口头约定。
        """

        schedule = ReadinessSchedule(**self.module.MCP_READINESS_MINIMUM_LEGAL_SCHEDULE_KWARGS)
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        due_times = [started + timedelta(seconds=offset) for offset in schedule.attempt_offsets()]
        self.assertEqual(due_times, [started, started + timedelta(seconds=1)])


class CliSelfCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script()

    def test_cli_list_mentions_every_fixture_and_the_schedule(self) -> None:
        import io
        import unittest.mock as mock

        with mock.patch("sys.stdout", new=io.StringIO()) as captured:
            self.assertEqual(self.module.main([]), 0)
        output = captured.getvalue()
        for fixture in self.module.NEGATIVE_IDENTITY_FIXTURES:
            self.assertIn(fixture.expected_reason, output)
        self.assertIn("mcp-readiness-minimum-legal", output)


if __name__ == "__main__":
    unittest.main()
