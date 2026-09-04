"""Issue #498 分级资产门禁的正向、伪装负向与影响面单测。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CLASSIFIER = load(ROOT / "scripts/ci/classify_story_changes.py", "asset_classifier_under_test")
L1 = load(ROOT / "scripts/ci/check_l1_assets.py", "l1_gate_under_test")
IMPACT = load(ROOT / "scripts/ci/check_permission_impact.py", "permission_impact_under_test")
PREPARE = load(
    ROOT / "scripts/ci/prepare_permission_impact_counts.py",
    "permission_impact_prepare_under_test",
)
EXPORT = load(
    ROOT / "scripts/ops/export_permission_impact_counts.py",
    "permission_impact_export_under_test",
)


class AssetClassificationTest(unittest.TestCase):
    def test_legacy_modes_and_new_risk_levels_are_both_exposed(self) -> None:
        docs = CLASSIFIER.classify_detail(["docs/协作约定.md", "README.md"])
        self.assertEqual((docs.mode, docs.risk_level), ("docs", "l0"))

        # Issue #520 F2：L1 轻量档已停用，只改内容资产也走完整门禁；l1_changed 仍为真，
        # 完整门禁里的 L1 资产检查照常执行。
        l1 = CLASSIFIER.classify_detail(["src/lingxi/config/content.toml", "docs/文案说明.md"])
        self.assertEqual((l1.mode, l1.risk_level), ("full", "full"))
        self.assertTrue(l1.docs_changed)
        self.assertTrue(l1.l1_changed)

        l3 = CLASSIFIER.classify_detail(["src/lingxi/config/company_function_metric_map.toml"])
        self.assertEqual((l3.mode, l3.risk_level), ("full", "l3"))
        self.assertTrue(l3.l3_changed)

    def test_l1_only_change_must_reach_the_image_producing_lane(self) -> None:
        """Issue #520 F2：只改 L1 内容资产不得落到不产出候选镜像的轻量档。

        这三份资产随镜像发布、位于 ``src/**``，会命中 publish.yml 的路径过滤。走
        ``risk_level == 'l1'`` 时 Epic Full 会跳过 image job、也不写候选证明，合入
        main 之后 Main Publish 的 verify_epic_candidate.py 必然找不到候选而失败。
        这条锁的方向是：**只要轻量档还停用，L1-only 改动就必须路由到完整门禁**。
        """

        self.assertFalse(
            CLASSIFIER.L1_LIGHT_ROUTE_ENABLED,
            "L1 轻量档若要重新启用，必须先解决内容资产随镜像发布导致的候选证明缺失",
        )
        for paths in (
            ["src/lingxi/config/content.toml"],
            ["src/lingxi/config/content.toml", "src/lingxi/config/content.lock.toml"],
            ["src/lingxi/config/admin_metric_alias_map.toml"],
        ):
            with self.subTest(paths=paths):
                detail = CLASSIFIER.classify_detail(paths)
                self.assertEqual((detail.mode, detail.risk_level), ("full", "full"))
                self.assertTrue(detail.l1_changed)

    def test_light_l1_lane_code_is_kept_and_reactivates_with_the_switch(self) -> None:
        """判定这一档的代码原样保留：开关翻回 True 时轻量档立刻恢复（Issue #520 F2）。"""

        original = CLASSIFIER.L1_LIGHT_ROUTE_ENABLED
        CLASSIFIER.L1_LIGHT_ROUTE_ENABLED = True
        try:
            detail = CLASSIFIER.classify_detail(["src/lingxi/config/content.toml"])
        finally:
            CLASSIFIER.L1_LIGHT_ROUTE_ENABLED = original
        self.assertEqual((detail.mode, detail.risk_level), ("fast", "l1"))
        self.assertTrue(detail.l1_changed)

    def test_l1_mixed_with_code_keeps_fast_but_cannot_skip_l1_check(self) -> None:
        detail = CLASSIFIER.classify_detail(
            ["src/lingxi/config/content.toml", "src/lingxi/core/ids.py"]
        )
        self.assertEqual(detail.mode, "fast")
        self.assertEqual(detail.risk_level, "fast")
        self.assertTrue(detail.l1_changed)

    def test_docs_mixed_with_l1_keeps_l1_flag_and_sets_docs_gate_flag(self) -> None:
        detail = CLASSIFIER.classify_detail(["docs/文案说明.md", "src/lingxi/config/content.toml"])
        # 轻量档停用后走完整门禁；两个事实位都必须仍然为真，否则文档门禁或 L1
        # 资产检查会被静默跳过。
        self.assertEqual((detail.mode, detail.risk_level), ("full", "full"))
        self.assertTrue(detail.docs_changed)
        self.assertTrue(detail.l1_changed)

    def test_l3_wins_over_every_other_safe_or_fast_path(self) -> None:
        detail = CLASSIFIER.classify_detail(
            [
                "src/lingxi/config/galaxy_role_function_map.toml",
                "src/lingxi/config/content.toml",
                "src/lingxi/core/ids.py",
            ]
        )
        self.assertEqual((detail.mode, detail.risk_level), ("full", "l3"))

    def test_unregistered_config_and_filename_tricks_fail_closed(self) -> None:
        """伪装路径必须既拿不到轻档，也点不亮 l1/l3 事实位。

        只断言 mode/risk 是不够的：Issue #520 F2 停用 L1 轻量档之后，L1-only 改动
        本来就是 full/full，于是「把空格 strip 掉、把 ``/./`` 折叠掉」这类放宽
        normalize_path 的变异能在只看 mode/risk 的断言下存活（实测存活）。事实位
        才是这条防护的真正观察点——它直接决定 L1 资产检查与 L3 影响面步骤跑不跑。
        """

        for path in (
            "src/lingxi/config/new.toml",
            " src/lingxi/config/content.toml",
            "src/lingxi/config/content.toml ",
            "src/lingxi/config/./content.toml",
            "src/lingxi/config/content.toml.bak",
            " src/lingxi/config/galaxy_role_function_map.toml",
            "src/lingxi/config/./company_function_metric_map.toml",
        ):
            with self.subTest(path=path):
                detail = CLASSIFIER.classify_detail([path])
                self.assertEqual(detail.mode, "full")
                self.assertEqual(detail.risk_level, "full")
                self.assertFalse(detail.l1_changed)
                self.assertFalse(detail.l3_changed)

        # 非法 UTF-8 文件名（changed_paths 用 surrogateescape 保留）不得被折叠成
        # 安全路径；与文档混合时也必须把整批改动升级，不能只看「其余都是文档」。
        broken = "\udcffsrc/lingxi/config/content.toml"
        broken_detail = CLASSIFIER.classify_detail([broken])
        self.assertEqual((broken_detail.mode, broken_detail.risk_level), ("full", "full"))
        self.assertFalse(broken_detail.l1_changed)
        mixed = CLASSIFIER.classify_detail([broken, "docs/正常.md"])
        self.assertEqual((mixed.mode, mixed.risk_level), ("full", "full"))
        self.assertTrue(mixed.docs_changed)

    def test_whitelists_are_full_exact_paths_not_suffix_matches(self) -> None:
        """Issue #520 P2：L1/L3 白名单声明的是**完整精确路径**，这条锁把它钉死。

        此前没有任何测试区分「精确相等」与「后缀匹配」：把 ``path in L1_FILES`` 换成
        ``any(path.endswith(x) for x in L1_FILES)`` 变异能存活。而后缀匹配意味着任何
        目录下同名的文件（vendor 副本、第三方子树、备份目录）都能挤进内容资产档，
        绕开针对那三份已批准事实源的分层前提。方向只能是变严：同名但不同路径一律
        按未知路径升级到完整门禁，且不得点亮 l1/l3 事实位。
        """

        for path in (
            "vendor/src/lingxi/config/content.toml",
            "backup/src/lingxi/config/content.lock.toml",
            "third_party/src/lingxi/config/admin_metric_alias_map.toml",
            "vendor/src/lingxi/config/galaxy_role_function_map.toml",
            "backup/src/lingxi/config/company_function_metric_map.toml",
        ):
            with self.subTest(path=path):
                detail = CLASSIFIER.classify_detail([path])
                self.assertEqual((detail.mode, detail.risk_level), ("full", "full"))
                self.assertFalse(detail.l1_changed)
                self.assertFalse(detail.l3_changed)

        # scripts/ci/ 的数据文件豁免同样是精确路径：同名文件放在别处不得继承豁免。
        detail = CLASSIFIER.classify_detail(["scripts/ci/nested/size_ratchet_baseline.txt"])
        self.assertEqual((detail.mode, detail.risk_level), ("full", "full"))

        # 两条新棘轮基线同样只精确豁免登记的路径本身。
        for nested_path in (
            "scripts/ci/nested/function_size_ratchet_baseline.txt",
            "scripts/ci/nested/comment_ratchet_baseline.txt",
        ):
            with self.subTest(path=nested_path):
                detail = CLASSIFIER.classify_detail([nested_path])
                self.assertEqual((detail.mode, detail.risk_level), ("full", "full"))

    def test_reserved_l2_extension_is_not_implemented_as_a_light_gate(self) -> None:
        original = CLASSIFIER.L2_FILES
        CLASSIFIER.L2_FILES = frozenset({"src/lingxi/config/prompt.toml"})
        try:
            detail = CLASSIFIER.classify_detail(["src/lingxi/config/prompt.toml"])
        finally:
            CLASSIFIER.L2_FILES = original
        self.assertEqual((detail.mode, detail.risk_level), ("full", "l2"))

    def test_write_output_includes_risk_and_change_flags(self) -> None:
        detail = CLASSIFIER.classify_detail(["src/lingxi/config/content.toml"])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github-output"
            CLASSIFIER.write_output(output, detail, ["src/lingxi/config/content.toml"])
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines(),
                [
                    "mode=full",
                    "risk_level=full",
                    "docs_changed=false",
                    "l1_changed=true",
                    "l3_changed=false",
                    "worker_changed=false",
                ],
            )


class L1GateTest(unittest.TestCase):
    def test_real_l1_assets_pass(self) -> None:
        self.assertEqual(L1.check_l1_assets(), [])

    def _paths(
        self,
        content: str,
        lock: str,
        aliases: str,
        source: str = "pass\n",
        *,
        text_keys: tuple[str, ...] = ("greeting",),
        card_keys: tuple[str, ...] = ("main",),
    ):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        directory = Path(tmp)
        content_path = directory / "content.toml"
        lock_path = directory / "content.lock.toml"
        alias_path = directory / "aliases.toml"
        source_root = directory / "admin"
        source_root.mkdir()
        daily_report = directory / "daily_report.py"
        content_path.write_text(content, encoding="utf-8")
        lock_path.write_text(lock, encoding="utf-8")
        alias_path.write_text(aliases, encoding="utf-8")
        (source_root / "sample.py").write_text(source, encoding="utf-8")
        daily_report.write_text("pass\n", encoding="utf-8")
        # Issue #520 F1：门禁要把 content.toml 的键集合与运行时登记表精确比对，
        # 所以合成场景也要有一份对应的 content.py（只做 ast 静态读取，不 import）。
        module_path = directory / "content.py"
        module_path.write_text(
            "REQUIRED_TEXT_KEYS: tuple[str, ...] = (\n"
            + "".join(f"    {key!r},\n" for key in text_keys)
            + ")\n\nREQUIRED_CARD_KEYS: tuple[str, ...] = (\n"
            + "".join(f"    {key!r},\n" for key in card_keys)
            + ")\n",
            encoding="utf-8",
        )
        return content_path, lock_path, alias_path, source_root, daily_report, module_path

    def _run(self, paths) -> list[str]:
        return L1.check_l1_assets(
            paths[0],
            paths[1],
            paths[2],
            admin_source_root=paths[3],
            daily_report_path=paths[4],
            content_module_path=paths[5],
        )

    def test_bad_alias_shape_is_rejected_even_though_runtime_loader_is_fail_open(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "not a metric"\n',
        )
        failures = self._run(paths)
        self.assertTrue(any("指标 token 形状" in failure for failure in failures), failures)

    def test_content_lock_mismatch_is_rejected(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:wrong"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
        )
        failures = self._run(paths)
        self.assertTrue(
            any("文案变了" in failure or "整体摘要不符" in failure for failure in failures),
            failures,
        )

    def test_missing_content_key_is_rejected_even_after_version_bump_and_refresh(self) -> None:
        """Issue #520 F1-E1：删一条文案键 + 递增版本 + 刷新锁，此前全绿而运行时会崩。

        运行时 ``ContentCatalog._require_exact_keys`` 做的是**精确相等**比对，缺键
        直接抛 ContentValidationError，进程加载内容目录时就起不来。版本锁只能证明
        「文案变了版本也跟着变了」，证明不了键集合仍然是运行时要求的那一套。
        """

        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
            text_keys=("greeting", "farewell"),
        )
        failures = self._run(paths)
        key_failures = [f for f in failures if "REQUIRED_TEXT_KEYS" in f]
        self.assertEqual(len(key_failures), 1, failures)
        self.assertIn("缺少 farewell", key_failures[0])

    def test_extra_content_key_is_rejected_even_after_version_bump_and_refresh(self) -> None:
        """Issue #520 F1-E2：新增一条没人消费的键同样会让运行时加载失败。"""

        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\nspare = "多的"\n'
            '[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
        )
        failures = self._run(paths)
        key_failures = [f for f in failures if "REQUIRED_TEXT_KEYS" in f]
        self.assertEqual(len(key_failures), 1, failures)
        self.assertIn("多余 spare", key_failures[0])

    def test_card_key_set_is_checked_against_the_runtime_registration_too(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
            card_keys=("main", "detail"),
        )
        failures = self._run(paths)
        key_failures = [f for f in failures if "REQUIRED_CARD_KEYS" in f]
        self.assertEqual(len(key_failures), 1, failures)
        self.assertIn("缺少 detail", key_failures[0])

    def test_unreadable_runtime_registration_fails_closed(self) -> None:
        """登记表读不出来时必须失败关闭，不能退化成「跳过键集合检查」。"""

        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
        )
        module_path = paths[5]
        for body, expected in (
            ("REQUIRED_CARD_KEYS = ('main',)\n", "REQUIRED_TEXT_KEYS"),
            (
                "REQUIRED_TEXT_KEYS = tuple(sorted(_KEYS))\nREQUIRED_CARD_KEYS = ('main',)\n",
                "REQUIRED_TEXT_KEYS",
            ),
        ):
            with self.subTest(body=body):
                module_path.write_text(body, encoding="utf-8")
                failures = self._run(paths)
                self.assertTrue(any(expected in failure for failure in failures), failures)

    def _registration_paths(self):
        """一份「content.toml 与登记表原本对得上」的合成场景。

        下面三条绕过用例都只改 ``content.py`` 一处：改完之后**运行时**要求的键集合
        和 ``content.toml`` 就对不上了，所以门禁必须失败关闭；旧实现读到的却仍是
        改动前那条字面量，于是全绿放行。
        """

        return self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
        )

    def test_augmented_assignment_to_the_registration_is_rejected(self) -> None:
        """rc24 fable 审查 P2-1：顶层 ``+=`` 改过登记表，门禁不能还读前面那条字面量。

        ``REQUIRED_TEXT_KEYS += ('spare',)`` 是 ``ast.AugAssign``，旧实现的
        ``isinstance(node, (ast.AnnAssign, ast.Assign))`` 分支根本不认识它，会静默
        跳过整条语句——门禁读到 ``('greeting',)``、与 content.toml 精确相等，全绿；
        运行时读到的却是 ``('greeting', 'spare')``，加载内容目录时直接崩。
        """

        paths = self._registration_paths()
        paths[5].write_text(
            "REQUIRED_TEXT_KEYS: tuple[str, ...] = ('greeting',)\n"
            "REQUIRED_TEXT_KEYS += ('spare',)\n"
            "REQUIRED_CARD_KEYS: tuple[str, ...] = ('main',)\n",
            encoding="utf-8",
        )
        failures = self._run(paths)
        key_failures = [f for f in failures if "REQUIRED_TEXT_KEYS" in f]
        self.assertTrue(key_failures, failures)
        self.assertTrue(any("被绑定了 2 次" in f for f in key_failures), key_failures)

    def test_non_top_level_rebinding_of_the_registration_is_rejected(self) -> None:
        """rc24 fable 审查 P2-1：非顶层重新绑定同样不能骗过门禁。

        旧实现只遍历 ``tree.body``，``if``/``try`` 体里的重绑和 import 期被调用的
        ``global`` 重绑都看不见；只要顶层还留着一条「好看的」字面量，门禁就照读不误。
        """

        for body, label in (
            (
                "REQUIRED_TEXT_KEYS: tuple[str, ...] = ('greeting',)\n"
                "if True:\n"
                "    REQUIRED_TEXT_KEYS = ('greeting', 'spare')\n"
                "REQUIRED_CARD_KEYS: tuple[str, ...] = ('main',)\n",
                "if 分支里重绑",
            ),
            (
                "REQUIRED_TEXT_KEYS: tuple[str, ...] = ('greeting',)\n"
                "def _patch() -> None:\n"
                "    global REQUIRED_TEXT_KEYS\n"
                "    REQUIRED_TEXT_KEYS = ('greeting', 'spare')\n"
                "_patch()\n"
                "REQUIRED_CARD_KEYS: tuple[str, ...] = ('main',)\n",
                "import 期 global 重绑",
            ),
            (
                "import os as REQUIRED_TEXT_KEYS\n"
                "REQUIRED_CARD_KEYS: tuple[str, ...] = ('main',)\n",
                "被 import as 占用同一个名字",
            ),
        ):
            with self.subTest(label=label):
                paths = self._registration_paths()
                paths[5].write_text(body, encoding="utf-8")
                failures = self._run(paths)
                self.assertTrue([f for f in failures if "REQUIRED_TEXT_KEYS" in f], failures)

    def test_registration_declared_only_inside_a_branch_is_rejected(self) -> None:
        """唯一那次绑定不在模块顶层时，门禁不能把它当成可静态确定的登记表。"""

        paths = self._registration_paths()
        paths[5].write_text(
            "if True:\n"
            "    REQUIRED_TEXT_KEYS: tuple[str, ...] = ('greeting',)\n"
            "REQUIRED_CARD_KEYS: tuple[str, ...] = ('main',)\n",
            encoding="utf-8",
        )
        failures = self._run(paths)
        key_failures = [f for f in failures if "REQUIRED_TEXT_KEYS" in f]
        self.assertTrue(key_failures, failures)
        self.assertTrue(any("不是模块顶层的直接赋值" in f for f in key_failures), key_failures)

    def test_l1_gate_does_not_import_the_business_package(self) -> None:
        """键集合检查必须是纯 stdlib 静态读取：门禁不导入 lingxi（Issue #520 F1）。

        用 AST 判定真正的 import 语句，而不是文本子串——注释里说明「不 import
        lingxi」不该让这条锁误红。
        """

        import ast as _ast

        tree = _ast.parse((ROOT / "scripts/ci/check_l1_assets.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("lingxi", imported)

    def test_retired_term_in_real_string_literal_is_rejected_but_docstring_is_ignored(self) -> None:
        paths = self._paths(
            '[meta]\nversion = "v1"\n[texts]\ngreeting = "hello"\n[cards]\nmain = "ok"\n',
            'version = "v1"\ndigest = "sha256:bad"\nretired_versions = []\n[keys]\n',
            '[aliases]\n"别名" = "sub_count"\n',
            '"""历史记录：收回不是当前出口术语。"""\n\ndef show():\n    return "收回"\n',
        )
        failures = self._run(paths)
        self.assertTrue(any("退役术语" in failure for failure in failures), failures)


class PermissionImpactTest(unittest.TestCase):
    OLD_ROLE = {"roles": {"银河运营": "运营"}}
    NEW_ROLE = {"roles": {"银河运营": "运营", "银河销售": "销售"}}
    OLD_METRIC = {"companies": {"1": {"运营": ["m1", "m2"], "销售": ["m0"]}}}
    NEW_METRIC = {"companies": {"1": {"运营": ["m2", "m3"], "销售": ["m0"]}}}

    @staticmethod
    def _commit(repository: Path, message: str) -> str:
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                message,
            ],
            cwd=repository,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @staticmethod
    def _stage_registration(manifest: dict[str, object]) -> dict[str, object]:
        source = manifest["source"]
        assert isinstance(source, dict)
        return {
            "schema": IMPACT.PROVENANCE_SCHEMA,
            "manifest_sha256": IMPACT._document_digest(manifest),
            "base_facts_sha256": manifest["base_facts_sha256"],
            "head_facts_sha256": manifest["head_facts_sha256"],
            "grant_surface_sha256": manifest["grant_surface_sha256"],
            "shrink_surface_sha256": manifest["shrink_surface_sha256"],
            "source": {
                "kind": IMPACT.STAGE_PROVENANCE_SOURCE,
                "environment": source["environment"],
                "dataset": source["dataset"],
                "query_version": source["query_version"],
                "captured_at": source["captured_at"],
            },
            "registered_at": "2026-09-01T00:00:00+00:00",
        }

    def test_grant_and_shrink_are_separate_and_counts_are_explicit(self) -> None:
        with self.assertRaises(IMPACT.CountEvidenceError):
            IMPACT.build_report(
                self.OLD_ROLE,
                self.NEW_ROLE,
                self.OLD_METRIC,
                self.NEW_METRIC,
            )
        report = IMPACT.build_report(
            self.OLD_ROLE,
            self.NEW_ROLE,
            self.OLD_METRIC,
            self.NEW_METRIC,
            user_counts={"grant": 3, "shrink": 1},
        )
        grants = {(row["role"], tuple(row["metrics"])) for row in report["grant"]}
        shrinks = {(row["role"], tuple(row["metrics"])) for row in report["shrink"]}
        self.assertIn(("银河销售", ("m0",)), grants)
        self.assertIn(("银河运营", ("m3",)), grants)
        self.assertIn(("银河运营", ("m1",)), shrinks)
        self.assertEqual(report["affected_user_counts"]["grant"], 3)
        self.assertEqual(report["affected_user_counts"]["shrink"], 1)
        self.assertEqual(report["affected_user_counts"]["status"], "provided")
        self.assertEqual(report["affected_user_counts"]["source"]["kind"], "test-explicit")
        rendered = IMPACT.render_report(report)
        self.assertIn("新增授予面（grant）", rendered)
        self.assertIn("收缩面（shrink）", rendered)
        self.assertIn("受影响用户数量（仅数量，不含内部 ID）", rendered)
        self.assertNotIn("user-", rendered)
        self.assertNotIn("internal-user", rendered)

    def test_only_explicit_pure_counts_are_accepted(self) -> None:
        report = IMPACT.build_report(
            self.OLD_ROLE,
            self.NEW_ROLE,
            self.OLD_METRIC,
            self.NEW_METRIC,
            user_counts={"grant": 3, "shrink": 1},
        )
        self.assertEqual(report["affected_user_counts"]["grant"], 3)
        with self.assertRaises(ValueError):
            IMPACT.build_report(
                self.OLD_ROLE,
                self.NEW_ROLE,
                self.OLD_METRIC,
                self.NEW_METRIC,
                user_counts={"grant": ["internal-user-1"], "shrink": 0},
            )

    def test_strict_manifest_is_bound_to_candidate_and_contains_only_aggregate_metadata(
        self,
    ) -> None:
        base_digest = "a" * 64
        head_digest = "b" * 64
        base_surface = IMPACT.build_surface(
            IMPACT._role_map(self.OLD_ROLE, "base"),
            IMPACT._metric_map(self.OLD_METRIC, "base"),
        )
        head_surface = IMPACT.build_surface(
            IMPACT._role_map(self.NEW_ROLE, "head"),
            IMPACT._metric_map(self.NEW_METRIC, "head"),
        )
        grant = head_surface - base_surface
        shrink = base_surface - head_surface
        manifest = {
            "schema": IMPACT.COUNT_SCHEMA,
            "base_facts_sha256": base_digest,
            "head_facts_sha256": head_digest,
            "grant_surface_sha256": IMPACT._surface_digest(grant),
            "shrink_surface_sha256": IMPACT._surface_digest(shrink),
            "counts": {"grant": 7, "shrink": 4},
            "source": {
                "kind": IMPACT.STAGE_COUNT_CLAIM_SOURCE,
                "environment": "biai-stage",
                "dataset": "galaxy_user_role",
                "query_version": "permission-impact-users/v1",
                "captured_at": "2026-08-31T00:00:00+00:00",
            },
        }
        report = IMPACT.build_report(
            self.OLD_ROLE,
            self.NEW_ROLE,
            self.OLD_METRIC,
            self.NEW_METRIC,
            user_counts=manifest,
            base_facts_sha256=base_digest,
            head_facts_sha256=head_digest,
            strict_count_manifest=True,
        )
        self.assertEqual(report["affected_user_counts"]["grant"], 7)
        self.assertEqual(report["affected_user_counts"]["status"], "unverified-claim")
        self.assertEqual(
            report["affected_user_counts"]["source"]["kind"],
            IMPACT.STAGE_COUNT_CLAIM_SOURCE,
        )
        self.assertNotIn("user_id", json.dumps(report, ensure_ascii=False))

        forged = dict(manifest)
        forged["source"] = dict(manifest["source"])
        forged["source"]["user_id"] = "internal-user-1"
        with self.assertRaises(IMPACT.CountEvidenceError):
            IMPACT.build_report(
                self.OLD_ROLE,
                self.NEW_ROLE,
                self.OLD_METRIC,
                self.NEW_METRIC,
                user_counts=forged,
                base_facts_sha256=base_digest,
                head_facts_sha256=head_digest,
                strict_count_manifest=True,
            )

        # 夹带也可能发生在清单顶层，而不只是 source 里。此前只覆盖了 source，
        # 把顶层的 `set(manifest) != COUNT_MANIFEST_KEYS` 放宽成 issubset 的变异
        # 能存活（实测存活）；这两条把两层都钉死，方向是变严。
        for extra_key, extra_value in (
            ("user_ids", ["internal-user-1", "internal-user-2"]),
            ("rows", [{"user": "u1"}]),
        ):
            with self.subTest(extra_key=extra_key):
                smuggled = dict(manifest)
                smuggled[extra_key] = extra_value
                with self.assertRaises(IMPACT.CountEvidenceError):
                    IMPACT.build_report(
                        self.OLD_ROLE,
                        self.NEW_ROLE,
                        self.OLD_METRIC,
                        self.NEW_METRIC,
                        user_counts=smuggled,
                        base_facts_sha256=base_digest,
                        head_facts_sha256=head_digest,
                        strict_count_manifest=True,
                    )

    def test_manifest_survives_its_own_pr_commit_and_tampering_turns_red(self) -> None:
        """P1/P2：清单绑定事实摘要，PR 提交清单本身不会改变绑定；篡改必失败。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            role_path = repository / IMPACT.ROLE_MAP_PATH
            metric_path = repository / IMPACT.METRIC_MAP_PATH
            role_path.parent.mkdir(parents=True, exist_ok=True)
            metric_path.parent.mkdir(parents=True, exist_ok=True)
            role_path.write_text('[roles]\n"角色" = "职能"\n', encoding="utf-8")
            metric_path.write_text('[companies."1"]\n"职能" = ["m1"]\n', encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            base = self._commit(repository, "base")
            metric_path.write_text('[companies."1"]\n"职能" = ["m1", "m2"]\n', encoding="utf-8")
            facts_head = self._commit(repository, "permission fact change")

            base_roles, base_metrics, base_role_raw, base_metric_raw = (
                IMPACT._load_ref_documents_with_raw(repository, base)
            )
            head_roles, head_metrics, head_role_raw, head_metric_raw = (
                IMPACT._load_ref_documents_with_raw(repository, facts_head)
            )
            base_surface = IMPACT.build_surface(
                IMPACT._role_map(base_roles, "base"), IMPACT._metric_map(base_metrics, "base")
            )
            head_surface = IMPACT.build_surface(
                IMPACT._role_map(head_roles, "head"), IMPACT._metric_map(head_metrics, "head")
            )
            grant_surface = head_surface - base_surface
            shrink_surface = base_surface - head_surface
            manifest: dict[str, object] = {
                "schema": IMPACT.COUNT_SCHEMA,
                "base_facts_sha256": IMPACT._facts_digest(base_role_raw, base_metric_raw),
                "head_facts_sha256": IMPACT._facts_digest(head_role_raw, head_metric_raw),
                "grant_surface_sha256": IMPACT._surface_digest(grant_surface),
                "shrink_surface_sha256": IMPACT._surface_digest(shrink_surface),
                "counts": {"grant": 12, "shrink": 5},
                "source": {
                    "kind": IMPACT.STAGE_COUNT_CLAIM_SOURCE,
                    "environment": "biai-stage",
                    "dataset": "galaxy_user_role",
                    "query_version": "permission-impact-users/v1",
                    "captured_at": "2026-09-01T00:00:00+00:00",
                },
            }
            manifest_path = repository / ".github" / "permission-impact-counts.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            registration_path = root / "permission-impact-provenance.json"
            registration = self._stage_registration(manifest)
            registration_path.write_text(
                json.dumps(registration, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

            # 把 manifest 本身加入 head：它改变了提交 SHA，但两份权限事实没有改变。
            committed_head = self._commit(repository, "submit stage claim manifest")
            prepared = root / "prepared-counts.json"
            # Issue #520 F3：仓库外 registration 缺失不再终止；计数照常产出，
            # 但清单内容一字不改（不得夹带任何新字段，否则严格校验会拒绝）。
            degraded = PREPARE.prepare(
                base,
                committed_head,
                repository=repository,
                manifest=manifest_path,
                output=prepared,
            )
            self.assertEqual(degraded["counts"], {"grant": 12, "shrink": 5})
            self.assertEqual(set(degraded), IMPACT.COUNT_MANIFEST_KEYS)
            evidence = PREPARE.prepare(
                base,
                committed_head,
                repository=repository,
                manifest=manifest_path,
                trusted_provenance=registration_path,
                output=prepared,
            )
            self.assertEqual(evidence["counts"], {"grant": 12, "shrink": 5})
            self.assertNotIn("head_ref", manifest)
            self.assertNotIn("head_ref", prepared.read_text(encoding="utf-8"))
            checked = root / "permission-impact-report.json"
            # Issue #520 F3 场景③：合法清单 + 没有 registration —— 现在必须**走完**
            # 并产出扩权/缩权 diff 产物，数量如实标注为未验证声明。
            degraded_report = root / "permission-impact-degraded.json"
            self.assertEqual(
                IMPACT.run_check(
                    base,
                    committed_head,
                    repository=repository,
                    user_counts_path=prepared,
                    output=degraded_report,
                ),
                0,
            )
            degraded_document = json.loads(degraded_report.read_text(encoding="utf-8"))
            self.assertEqual(
                degraded_document["count_registration"]["status"],
                IMPACT.REGISTRATION_UNREGISTERED,
            )
            self.assertIn("从未被实现", degraded_document["count_registration"]["note"])
            self.assertEqual(
                degraded_document["affected_user_counts"]["status"], "unverified-claim"
            )
            # 扩权与缩权仍然分列，且是真实非空 diff。
            self.assertEqual(degraded_document["grant_entry_count"], 1)
            self.assertEqual(degraded_document["shrink_entry_count"], 0)
            self.assertEqual(
                [(row["role"], tuple(row["metrics"])) for row in degraded_document["grant"]],
                [("角色", ("m2",))],
            )
            self.assertNotIn("user_id", json.dumps(degraded_document, ensure_ascii=False))
            self.assertEqual(
                IMPACT.run_check(
                    base,
                    committed_head,
                    repository=repository,
                    user_counts_path=prepared,
                    trusted_provenance_path=registration_path,
                    output=checked,
                ),
                0,
            )
            self.assertIn(
                "out-of-band-hash-registered",
                checked.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                json.loads(checked.read_text(encoding="utf-8"))["affected_user_counts"]["status"],
                "provided-registered",
            )

            # 修改 facts 后仍指向同一份 stage registration，必须被事实摘要绑定挡住。
            metric_path.write_text(
                '[companies."1"]\n"职能" = ["m1", "m2", "m3"]\n', encoding="utf-8"
            )
            changed_facts_head = self._commit(repository, "tamper permission facts")
            with self.assertRaises(PREPARE.IMPACT.CountEvidenceError):
                PREPARE.prepare(
                    base,
                    changed_facts_head,
                    repository=repository,
                    manifest=manifest_path,
                    trusted_provenance=registration_path,
                    output=prepared,
                )

            # 修改 count 或 surface digest，registration 与当前 claim 的 hash/绑定均应失败。
            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for field, value in (
                ("counts", {"grant": 99, "shrink": 5}),
                ("grant_surface_sha256", "0" * 64),
                ("head_facts_sha256", "1" * 64),
            ):
                tampered = json.loads(json.dumps(original_manifest))
                tampered[field] = value
                manifest_path.write_text(
                    json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(PREPARE.IMPACT.CountEvidenceError):
                    PREPARE.prepare(
                        base,
                        committed_head,
                        repository=repository,
                        manifest=manifest_path,
                        trusted_provenance=registration_path,
                        output=prepared,
                    )
            manifest_path.write_text(
                json.dumps(original_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

            tampered_registration = dict(registration)
            tampered_registration["manifest_sha256"] = "2" * 64
            registration_path.write_text(
                json.dumps(tampered_registration, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT._validate_stage_provenance(
                    tampered_registration,
                    manifest=original_manifest,
                    expected_base_facts_sha256=original_manifest["base_facts_sha256"],
                    expected_head_facts_sha256=original_manifest["head_facts_sha256"],
                    expected_grant_surface_sha256=original_manifest["grant_surface_sha256"],
                    expected_shrink_surface_sha256=original_manifest["shrink_surface_sha256"],
                )

    def test_empty_surface_derives_zero_from_static_diff(self) -> None:
        role = {"roles": {"角色": "没有对应指标的职能"}}
        metric = {"companies": {"1": {"另一职能": ["m1"]}}}
        report = IMPACT.build_report(role, role, metric, metric)
        counts = report["affected_user_counts"]
        self.assertEqual((counts["grant"], counts["shrink"]), (0, 0))
        self.assertEqual(counts["status"], "derived")
        self.assertEqual(counts["source"]["kind"], IMPACT.EMPTY_COUNT_SOURCE)

    def test_pr_stage_claim_and_in_tree_registration_are_not_trusted(self) -> None:
        manifest = {
            "schema": IMPACT.COUNT_SCHEMA,
            "base_facts_sha256": "a" * 64,
            "head_facts_sha256": "b" * 64,
            "grant_surface_sha256": "c" * 64,
            "shrink_surface_sha256": "d" * 64,
            "counts": {"grant": 1, "shrink": 0},
            "source": {
                "kind": IMPACT.STAGE_COUNT_CLAIM_SOURCE,
                "environment": "biai-stage",
                "dataset": "galaxy_user_role",
                "query_version": "permission-impact-users/v1",
                "captured_at": "2026-09-01T00:00:00+00:00",
            },
        }
        old_source = dict(manifest)
        old_source["source"] = dict(manifest["source"])
        old_source["source"]["kind"] = "biai-stage-read-only-aggregate"
        with self.assertRaises(IMPACT.CountEvidenceError):
            IMPACT._validate_count_manifest(
                old_source,
                expected_base_facts_sha256=manifest["base_facts_sha256"],
                expected_head_facts_sha256=manifest["head_facts_sha256"],
                expected_grant_surface_sha256=manifest["grant_surface_sha256"],
                expected_shrink_surface_sha256=manifest["shrink_surface_sha256"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            in_tree = repository / "provenance.json"
            in_tree.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT._load_external_provenance(in_tree, repository=repository)

    def test_optional_registration_degrades_only_when_truly_absent(self) -> None:
        """Issue #520 F3：降级只覆盖「文件根本不存在」，注入尝试仍然失败关闭。

        registration 是一个从未被实现的输入，所以缺席要降级；但仓库内注入、符号
        链接（含断链）这些**是**伪造证据的路径，一条都不能跟着放行。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()

            # 缺席：降级为 None，不抛错。
            self.assertIsNone(IMPACT.load_optional_provenance(None, repository=repository))
            self.assertIsNone(
                IMPACT.load_optional_provenance(root / "never-written.json", repository=repository)
            )

            # 仓库内注入：仍然失败关闭。
            in_tree = repository / "provenance.json"
            in_tree.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT.load_optional_provenance(in_tree, repository=repository)

            # 符号链接：仍然失败关闭，断链也不得被 exists() 误读成「没提供」。
            target = root / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT.load_optional_provenance(link, repository=repository)
            dangling = root / "dangling.json"
            dangling.symlink_to(root / "does-not-exist.json")
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT.load_optional_provenance(dangling, repository=repository)

    def test_unreadable_registration_path_fails_closed_instead_of_degrading(self) -> None:
        """rc24 fable 审查 P2-2 → rc25 S-4d：stat 失败不是「没提供」，必须失败关闭。

        ``Path.exists()``/``Path.is_symlink()`` 把 ``ENOENT``/``ENOTDIR``/``EBADF``/
        ``ELOOP`` 一律吞成 ``False``。其中 ``ELOOP``（路径中间某一段是符号链接环）
        是**读不出来**，不是「没提供」；旧实现据此 ``return None``，L3 门禁就会打印
        「计数证明降级：unregistered」并放行——门禁自己坏了却报通过。这一类必须失败
        关闭，才不会把一次读取故障伪装成一次合法降级。
        """

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()

            # 路径中间一段是自指符号链接：解析父目录时 ELOOP。
            loop = root / "loop"
            loop.symlink_to(loop)
            through_loop = loop / "provenance.json"
            # 这一行钉住旧实现为什么会静默降级：exists()/is_symlink() 都返回 False。
            self.assertFalse(through_loop.exists())
            self.assertFalse(through_loop.is_symlink())
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT.load_optional_provenance(through_loop, repository=repository)

            # 名字超长：读不出来的另一种形态，同样只能失败关闭。
            with self.assertRaises(IMPACT.CountEvidenceError):
                IMPACT.load_optional_provenance(root / ("x" * 4096), repository=repository)

    def test_count_input_symlink_is_rejected_before_reading_runner_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "target.json"
            target.write_text('{"grant": 1, "shrink": 0}\n', encoding="utf-8")
            link = directory / "counts.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                IMPACT._load_user_counts(link)

    def test_malformed_permission_facts_fail_closed(self) -> None:
        with self.assertRaises(IMPACT.ConfigShapeError):
            IMPACT.build_report(
                {"roles": {"r": "f"}},
                {"roles": {"r": "f"}},
                {"companies": {"1": {"f": []}}},
                {"companies": {"1": {"f": ["m"]}}},
            )

    def test_git_ref_cli_reads_only_the_two_permission_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            for relative, body in {
                IMPACT.ROLE_MAP_PATH: '[roles]\n"角色" = "职能"\n',
                IMPACT.METRIC_MAP_PATH: '[companies."1"]\n"职能" = ["m1"]\n',
            }.items():
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (repository / IMPACT.METRIC_MAP_PATH).write_text(
                '[companies."1"]\n"职能" = ["m1", "m2"]\n', encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "change",
                ],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/ci/check_permission_impact.py"),
                    "--base-ref",
                    base,
                    "--head-ref",
                    head,
                    "--repository",
                    str(repository),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("失败关闭", result.stderr)
        self.assertNotIn("not_provided", result.stdout + result.stderr)

    def test_prepare_derives_zero_only_when_surface_diff_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            role_path = repository / IMPACT.ROLE_MAP_PATH
            metric_path = repository / IMPACT.METRIC_MAP_PATH
            role_path.parent.mkdir(parents=True, exist_ok=True)
            role_path.write_text('[roles]\n"角色" = "职能"\n', encoding="utf-8")
            metric_path.parent.mkdir(parents=True, exist_ok=True)
            metric_path.write_text('[companies."1"]\n"未绑定职能" = ["m1"]\n', encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            metric_path.write_text(
                '[companies."1"]\n"未绑定职能" = ["m1", "m2"]\n', encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "unbound-change",
                ],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            output = repository / "counts.json"
            evidence = PREPARE.prepare(
                base,
                head,
                repository=repository,
                manifest=repository / "missing-manifest.json",
                output=output,
            )
            self.assertEqual(evidence["counts"], {"grant": 0, "shrink": 0})
            self.assertEqual(evidence["source"]["kind"], IMPACT.EMPTY_COUNT_SOURCE)
            self.assertNotIn("user_id", output.read_text(encoding="utf-8"))

    def test_stage_export_writes_only_pure_counts_and_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            role_path = repository / IMPACT.ROLE_MAP_PATH
            metric_path = repository / IMPACT.METRIC_MAP_PATH
            role_path.parent.mkdir(parents=True, exist_ok=True)
            metric_path.parent.mkdir(parents=True, exist_ok=True)
            role_path.write_text('[roles]\n"角色" = "职能"\n', encoding="utf-8")
            metric_path.write_text('[companies."1"]\n"职能" = ["m1"]\n', encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "base",
                ],
                cwd=repository,
                check=True,
            )
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            metric_path.write_text('[companies."1"]\n"职能" = ["m1", "m2"]\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "change",
                ],
                cwd=repository,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            original = EXPORT._read_stage_counts
            EXPORT._read_stage_counts = lambda _dsn, *, grant_roles, shrink_roles: (
                12,
                5,
                "2026-08-31T00:00:00+00:00",
            )
            try:
                output = repository / "counts.json"
                provenance_output = repository / "provenance.json"
                with mock.patch.dict(EXPORT.os.environ, {EXPORT.DSN_ENV: "redacted"}):
                    evidence = EXPORT.export(
                        base,
                        head,
                        repository=repository,
                        output=output,
                        provenance_output=provenance_output,
                    )
            finally:
                EXPORT._read_stage_counts = original
            self.assertEqual(evidence["counts"], {"grant": 12, "shrink": 5})
            registration = json.loads(provenance_output.read_text(encoding="utf-8"))
            IMPACT._validate_stage_provenance(
                registration,
                manifest=evidence,
                expected_base_facts_sha256=evidence["base_facts_sha256"],
                expected_head_facts_sha256=evidence["head_facts_sha256"],
                expected_grant_surface_sha256=evidence["grant_surface_sha256"],
                expected_shrink_surface_sha256=evidence["shrink_surface_sha256"],
            )
            serialized = output.read_text(encoding="utf-8")
            self.assertNotIn("redacted", serialized)
            self.assertNotIn("user_id", serialized)
            self.assertNotIn("user-", serialized)
            self.assertNotIn("head_ref", serialized)

    def test_stage_export_query_is_aggregate_only(self) -> None:
        source = (ROOT / "scripts/ops/export_permission_impact_counts.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("count(DISTINCT role.user_id)", source)
        self.assertNotIn("SELECT role.user_id", source)
        self.assertNotIn("role.email", source)


if __name__ == "__main__":
    unittest.main()
