"""随包发布的「公司 + 职能 → 指标名」翻译映射配置文件（Issue #227；外置与「后台管理员」
条目为 Issue #320）。

镜像 ``tests/test_galaxy_role_function.py`` 的 ``ShippedRoleFunctionMapFileTest``
写法。2026-08-19 产品负责人给出了七个职能各自对应的指标 ID，编排者按「同一职能全国
统一」机械展开进随包文件；2026-08-26 补充「后台管理员」全公司×全指标一条。本组断言
把**这些决定**钉住：任何人改动映射内容却不同步改这里，门禁就红。文件缺失或格式错误
则仍然按配置错误处理（不静默退化）；外置路径下同一条纪律同样成立，见
``ExternalPathOverrideTest``。
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from lingxi.adapters.company_function_metric_map_file import (
    default_company_function_metric_map_path,
    load_company_function_metric_map,
)
from lingxi.adapters.role_function_map_file import load_role_function_map

#: 产品负责人 2026-08-19 给出的「职能 → 指标 ID 列表」（2026-08-26 补充「后台管理员」，
#: Issue #320）。值是 ``metric_id``（权限发布表 permissions 列的权威结构，Issue #155），
#: 不是中文指标名。
_ALL_NINE_METRICS = (
    "sub_new_count",
    "sub_recharge_count",
    "sub_recharge_money",
    "sub_deduction_count",
    "sub_deduction_money",
    "exchange_rate",
    "vat_rate",
    "channel_rate",
    "channel_market_sharing",
)

OWNER_DECISION = {
    "CEO": _ALL_NINE_METRICS,
    "OTT": ("exchange_rate", "vat_rate"),
    "内容": ("exchange_rate", "vat_rate", "channel_rate", "channel_market_sharing"),
    "商务": ("exchange_rate", "vat_rate"),
    "财务": (
        "sub_new_count",
        "sub_recharge_count",
        "sub_recharge_money",
        "sub_deduction_count",
        "sub_deduction_money",
        "exchange_rate",
        "vat_rate",
    ),
    "运营": _ALL_NINE_METRICS,
    "销售": ("sub_new_count", "exchange_rate", "vat_rate"),
    # Issue #320 / 2026-08-26 裁定：银河「后台管理员」（role_id 513）= 全公司×全指标。
    # 取值与 CEO/运营同一份「全部指标」，独立职能标签（不复用 CEO/运营），理由见
    # config/company_function_metric_map.toml 的「后台管理员条目」一节。
    "后台管理员": _ALL_NINE_METRICS,
}

#: 2026-08-06 银河受控导出中实际出现的公司编号，外加「全非」通配键。
EXPECTED_COMPANY_KEYS = frozenset(
    [str(number) for number in range(1, 40)] + ["44", "50", "61", "49368", "*"]
)


class ShippedConfigFileTest(unittest.TestCase):
    """随包发布的那份文件：内容必须与产品负责人的决定逐字一致。"""

    def test_the_shipped_file_matches_the_owner_supplied_decision(self) -> None:
        mapping = load_company_function_metric_map(default_company_function_metric_map_path())

        for company, functions in mapping.items():
            for function, metrics in functions.items():
                self.assertIn(function, OWNER_DECISION, f"{company} 下出现未登记的职能")
                self.assertEqual(
                    tuple(metrics),
                    OWNER_DECISION[function],
                    f"公司 {company} 的职能 {function} 与产品负责人 2026-08-19 的决定不一致",
                )

    def test_every_company_key_carries_all_eight_functions(self) -> None:
        """「同一职能全国统一」的机械展开：每个公司键都必须写满全部职能（现为八个，
        含 2026-08-26 补充的「后台管理员」）。

        缺一条不是"这个人在这里没有这项指标"，而是翻译层对这个组合 fail-closed
        ——那会让真的持有该职能的用户在这个公司下发布不出去，且现象是静默跳过。
        「后台管理员」同样必须写满**每一个**公司键（不是只写 ``[companies."*"]``
        一条）：持有该角色的人在银河那一侧的公司范围可能是「全非」通配，也可能是
        具体公司枚举，只写 "*" 会让后一种情形在翻译层 fail-closed。
        """
        mapping = load_company_function_metric_map(default_company_function_metric_map_path())

        self.assertEqual(set(mapping), set(EXPECTED_COMPANY_KEYS))
        for company, functions in mapping.items():
            self.assertEqual(
                set(functions),
                set(OWNER_DECISION),
                f"公司 {company} 的职能集合不完整",
            )
        self.assertEqual(
            sum(len(functions) for functions in mapping.values()),
            len(EXPECTED_COMPANY_KEYS) * len(OWNER_DECISION),
        )

    def test_the_function_labels_come_from_the_role_function_map(self) -> None:
        """职能词表必须与银河角色映射的右值完全相同。

        角色映射新增一个职能而这里没跟上，那个职能的持有者会在翻译层 fail-closed；
        反过来这里多一个不存在的职能标签，则是永远查不到的死条目。
        """
        self.assertEqual(set(OWNER_DECISION), set(load_role_function_map().values()))

    def test_the_shipped_file_records_the_decision_and_its_boundaries(self) -> None:
        text = default_company_function_metric_map_path().read_text(encoding="utf-8")

        self.assertIn("2026-08-19", text)
        # 指标 ID 而非中文名的依据，以及清单来源的时效边界，都必须写在文件里——
        # 只活在 PR 正文里的说明，下一个维护者看不到。
        self.assertIn("metric_id", text)
        self.assertIn("list_metrics", text)
        self.assertIn("全国统一", text)
        # 「后台管理员」条目（Issue #320）同样必须在文件里留痕决策来源与日期，
        # 不能只活在这条测试或 PR 正文里。
        self.assertIn("2026-08-26", text)
        self.assertIn("后台管理员", text)
        self.assertIn("320", text)

    def test_the_config_file_is_shipped_inside_the_python_package(self) -> None:
        path = default_company_function_metric_map_path()

        self.assertEqual(path.parent.name, "config")
        self.assertEqual(path.parent.parent.name, "lingxi")
        self.assertEqual(path.name, "company_function_metric_map.toml")


class LoaderFailureModeTest(unittest.TestCase):
    """文件缺失或格式错误：失败关闭，不静默退化成空映射。

    "空映射合法" 与 "读不出来" 必须可分辨——前者是内容还没填，后者是部署配置本身
    有问题；把两者混成同一种结果，会让运维分不清该找谁。
    """

    def test_a_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.toml"
            with self.assertRaises(OSError):
                load_company_function_metric_map(missing)

    def test_malformed_toml_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.toml"
            broken.write_text("this is not [ valid toml", encoding="utf-8")
            with self.assertRaises(Exception):
                load_company_function_metric_map(broken)

    def test_a_file_missing_the_companies_table_raises_not_defaults_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            no_table = Path(tmp) / "no-companies-table.toml"
            no_table.write_text('[meta]\ndecision = "x"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_company_function_metric_map(no_table)

    def test_a_well_formed_non_empty_file_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            filled = Path(tmp) / "filled.toml"
            filled.write_text(
                '[companies."9001"]\n"运营" = ["示例指标A", "示例指标B"]\n',
                encoding="utf-8",
            )

            mapping = load_company_function_metric_map(filled)

            self.assertEqual(mapping, {"9001": {"运营": ("示例指标A", "示例指标B")}})


class ExternalPathOverrideTest(unittest.TestCase):
    """外置路径的三条验收标准（Issue #320）：存在则优先、未配置回落包内默认、
    配置了但缺失/非法响亮失败——与系统提示词文件同一模式。

    本组断言只练 :func:`load_company_function_metric_map` 本身（``path`` 参数），
    不牵涉环境变量解析——那部分由 ``tests/test_permission_refresh_duty.py`` 的
    ``DutyRegistrationTest`` 覆盖装配层「``LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH``
    如何流到这个参数」，两层各自独立可证伪。
    """

    def test_a_present_external_file_takes_priority_over_the_packaged_default(self) -> None:
        """外置文件存在则优先：内容与包内默认不同，读到的必须是外置那一份。"""

        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / "override.toml"
            external.write_text(
                '[companies."9999"]\n"运营" = ["外置专用指标"]\n',
                encoding="utf-8",
            )

            mapping = load_company_function_metric_map(external)

            self.assertEqual(mapping, {"9999": {"运营": ("外置专用指标",)}})
            # 反证：外置内容与包内默认（44 个公司键，含 "后台管理员"）截然不同，
            # 证明确实没有悄悄回落到包内默认再叠加。
            default_mapping = load_company_function_metric_map(default_company_function_metric_map_path())
            self.assertNotEqual(mapping, default_mapping)

    def test_no_path_argument_falls_back_to_the_packaged_default(self) -> None:
        """未配置（``path=None``）回落包内默认——外置能力交付前后行为逐字节一致。"""

        self.assertEqual(
            load_company_function_metric_map(None),
            load_company_function_metric_map(default_company_function_metric_map_path()),
        )

    def test_a_configured_but_missing_external_path_fails_loudly_not_silently(self) -> None:
        """配置了外置路径但文件缺失：响亮失败（``OSError``），不静默回落包内默认。

        这是「翻译映射不可用→告警」既有纪律在外置路径上的落点：一个指向不存在
        文件的外置路径必须让调用方（`_build_permission_refresh_duty`）判定
        ``metric_translation_map_unavailable`` 并不注册职责，而不是悄悄用回
        包内那份「安全」内容——错配不是未配。
        """

        with tempfile.TemporaryDirectory() as tmp:
            missing_external = Path(tmp) / "configured-but-missing.toml"

            with self.assertRaises(OSError):
                load_company_function_metric_map(missing_external)

    def test_a_configured_but_malformed_external_path_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            malformed = Path(tmp) / "configured-but-malformed.toml"
            malformed.write_text("not [ valid toml at all", encoding="utf-8")

            with self.assertRaises(Exception):
                load_company_function_metric_map(malformed)

    def test_loading_successfully_logs_a_content_digest(self) -> None:
        """加载成功时记录内容 digest 到日志（沿用系统提示词 digest 先例）。

        两次加载**内容相同**的外置文件必须得到**相同**的 digest（可用于产品负责人
        编辑后自行核对"这次改动是否真的被读到"）；内容不同则 digest 不同——否则
        digest 退化成一个恒定的装饰字段，起不到"内容变没变"的判别作用。
        """

        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.toml"
            first.write_text('[companies."1"]\n"运营" = ["指标A"]\n', encoding="utf-8")
            second = Path(tmp) / "b.toml"
            second.write_text('[companies."1"]\n"运营" = ["指标A"]\n', encoding="utf-8")
            third = Path(tmp) / "c.toml"
            third.write_text('[companies."1"]\n"运营" = ["指标B"]\n', encoding="utf-8")

            logger_name = "lingxi.adapters.company_function_metric_map_file"
            with self.assertLogs(logger_name, level="INFO") as first_log:
                load_company_function_metric_map(first)
            with self.assertLogs(logger_name, level="INFO") as second_log:
                load_company_function_metric_map(second)
            with self.assertLogs(logger_name, level="INFO") as third_log:
                load_company_function_metric_map(third)

            def digest_of(records: list[str]) -> str:
                self.assertEqual(len(records), 1, "加载成功恰记一条日志")
                self.assertIn("digest=", records[0])
                return records[0].split("digest=", 1)[1].split()[0]

            digest_first = digest_of(first_log.output)
            digest_second = digest_of(second_log.output)
            digest_third = digest_of(third_log.output)

            self.assertEqual(digest_first, digest_second, "内容相同的文件 digest 必须相同")
            self.assertNotEqual(digest_first, digest_third, "内容不同的文件 digest 必须不同")

    def test_a_failed_load_does_not_log_a_digest_line(self) -> None:
        """加载失败（文件缺失/格式非法）不产生 digest 日志行——digest 只描述
        "读到了什么"，不该在什么都没读到时也输出一个看似有效的值。
        """

        logger_name = "lingxi.adapters.company_function_metric_map_file"
        logger = logging.getLogger(logger_name)
        handler = logging.Handler()
        records: list[logging.LogRecord] = []
        handler.emit = records.append  # type: ignore[method-assign]
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.toml"
            with self.assertRaises(OSError):
                load_company_function_metric_map(missing)

        self.assertEqual(records, [], "加载失败时不该有任何一条来自本模块的日志")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
