"""`scripts/ci/check_runtime_dependencies.py`、`check_deploy_contract.py` 与
`push_image.py` 的判定用例（Issue #62 / S11）。

这三份检查的价值全在它们**会变红**。所以下面每一条都构造一份坏输入，断言它被
**具体地**拒绝；最后一组反过来跑真实仓库状态，防止检查因为文件结构变化而变成空转
（一个再也找不到目标的检查会安静地永远通过，比没有检查更危险）。

镜像层面的断言（rootfs 内容、非 root、两次构建等价、旧镜像在新库上启动）需要
docker，留给 `scripts/ci/verify_image_contract.sh`、`verify_compose_structure.sh`
与 `verify_old_image_new_schema.sh`，由 `CI / image` job 执行，不在本文件覆盖范围。
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPTS = REPOSITORY_ROOT / "scripts" / "ci"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先登记进 sys.modules 再执行：`@dataclass` 会用 cls.__module__ 回查
    # sys.modules 来解析类型注解，模块不在表里时报一个与 dataclass 毫无关系的
    # AttributeError（push_image.py 的 CommandResult 就是这样炸的）。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DEPENDENCIES = _load_module(SCRIPTS / "check_runtime_dependencies.py", "runtime_dependency_check_under_test")
CONTRACT = _load_module(SCRIPTS / "check_deploy_contract.py", "deploy_contract_check_under_test")
PUSH = _load_module(SCRIPTS / "push_image.py", "push_image_under_test")


class RequirementParsingTest(unittest.TestCase):
    def test_extras_and_specifier_are_separated(self) -> None:
        name, specifier = DEPENDENCIES.parse_requirement("psycopg[binary]>=3.2,<4")
        self.assertEqual(name, "psycopg")
        self.assertEqual(specifier, ">=3.2,<4")

    def test_distribution_names_are_normalized(self) -> None:
        # PEP 503：`claude-agent-sdk`、`claude_agent_sdk`、`Claude.Agent.SDK` 是同一个包。
        for raw in ("claude-agent-sdk==1", "claude_agent_sdk==1", "Claude.Agent.SDK==1"):
            self.assertEqual(DEPENDENCIES.parse_requirement(raw)[0], "claude-agent-sdk")

    def test_unparsable_requirement_raises(self) -> None:
        with self.assertRaises(ValueError):
            DEPENDENCIES.parse_requirement("==1.2.3")


class ExactPinTest(unittest.TestCase):
    """安全边界组件必须锁 `==`。这一组对应变异 D7。"""

    def test_range_is_rejected(self) -> None:
        failures = DEPENDENCIES.check_pins({"cryptography": [("scheduler", ">=45.0.7")],
                                            "claude-agent-sdk": [("worker", "==0.2.128")]})
        self.assertTrue(any("cryptography" in line for line in failures))

    def test_wildcard_pin_is_rejected(self) -> None:
        # `==45.0.*` 看起来像精确锁，其实是一个范围——补丁版本可以自己变。
        failures = DEPENDENCIES.check_pins({"cryptography": [("scheduler", "==45.0.*")],
                                            "claude-agent-sdk": [("worker", "==0.2.128")]})
        self.assertTrue(any("cryptography" in line for line in failures))

    def test_missing_declaration_is_rejected(self) -> None:
        failures = DEPENDENCIES.check_pins({"claude-agent-sdk": [("worker", "==0.2.128")]})
        self.assertTrue(any("cryptography" in line for line in failures))

    def test_every_occurrence_is_checked_not_just_the_first(self) -> None:
        # cryptography 同时出现在 scheduler 与 bot-test 两组；只看第一处会漏掉后一处。
        failures = DEPENDENCIES.check_pins({
            "cryptography": [("scheduler", "==45.0.7"), ("bot-test", ">=45.0.7")],
            "claude-agent-sdk": [("worker", "==0.2.128")],
        })
        self.assertTrue(any("bot-test" in line for line in failures))

    def test_exact_pin_passes(self) -> None:
        failures = DEPENDENCIES.check_pins({"cryptography": [("scheduler", "==45.0.7")],
                                            "claude-agent-sdk": [("worker", "==0.2.128")]})
        self.assertEqual(failures, [])


class LazyImportScanTest(unittest.TestCase):
    """扫描必须走进函数体。这一组对应变异 D8。"""

    def _scan(self, source: str) -> dict[str, list[str]]:
        directory = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (directory / "sample.py").write_text(textwrap.dedent(source), encoding="utf-8")
        original = DEPENDENCIES.SOURCE_ROOT
        DEPENDENCIES.SOURCE_ROOT = directory
        try:
            return DEPENDENCIES.imported_top_level_modules()
        finally:
            DEPENDENCIES.SOURCE_ROOT = original

    def test_function_level_import_is_found(self) -> None:
        found = self._scan(
            """
            def build():
                import httpx
                return httpx
            """
        )
        self.assertIn("httpx", found)

    def test_import_inside_class_and_try_is_found(self) -> None:
        found = self._scan(
            """
            class Thing:
                def method(self):
                    try:
                        from deeply.nested import thing
                    except ImportError:
                        import fallback_package
                    return thing
            """
        )
        self.assertIn("deeply", found)
        self.assertIn("fallback_package", found)

    def test_relative_imports_are_not_third_party(self) -> None:
        found = self._scan(
            """
            def build():
                from .sibling import helper
                from ..parent import other
                return helper, other
            """
        )
        self.assertNotIn("sibling", found)
        self.assertNotIn("parent", found)


class DurationParsingTest(unittest.TestCase):
    def test_supported_forms(self) -> None:
        self.assertEqual(CONTRACT.parse_duration_seconds("90s"), 90.0)
        self.assertEqual(CONTRACT.parse_duration_seconds("1m30s"), 90.0)
        self.assertEqual(CONTRACT.parse_duration_seconds("2m"), 120.0)
        self.assertEqual(CONTRACT.parse_duration_seconds("45"), 45.0)

    def test_unparsable_returns_none(self) -> None:
        self.assertIsNone(CONTRACT.parse_duration_seconds("很久"))


class CommentStrippingTest(unittest.TestCase):
    """判定必须落在去掉注释之后的文本上。

    本仓库的 compose 注释里就写着「本文件没有 `build:` 键」这样的句子——
    天真的 grep 会把说明文字当成违规，然后有人为了让门禁变绿去删注释。
    """

    def test_full_line_comments_are_removed(self) -> None:
        stripped = CONTRACT.strip_comments("# 这里提到 build: 键\nimage: real\n")
        self.assertNotIn("build:", stripped)
        self.assertIn("image: real", stripped)

    def test_line_numbers_are_preserved(self) -> None:
        stripped = CONTRACT.strip_comments("# a\n# b\nreal\n")
        self.assertEqual(stripped.splitlines()[2], "real")


class ServiceBlockTest(unittest.TestCase):
    def test_block_stops_at_next_service(self) -> None:
        compose = textwrap.dedent(
            """
            services:
              scheduler:
                restart: unless-stopped
                stop_grace_period: 90s
              worker:
                restart: "no"
            """
        )
        scheduler = CONTRACT.service_block(compose, "scheduler")
        self.assertIn("stop_grace_period: 90s", scheduler)
        self.assertNotIn('restart: "no"', scheduler)


class PushFailureClassificationTest(unittest.TestCase):
    """降级只对权限失败生效。这一组对应变异 D15 的另一半。

    误判方向是这条断言的核心：把网络问题误判成"权限不足"会让 job 变绿而没人来看，
    比把权限问题误判成"其他失败"（job 变红，有人来看）危险得多。
    """

    def test_permission_wordings_are_recognized(self) -> None:
        for message in (
            "denied: permission_denied: write_package",
            "unauthorized: authentication required",
            "error parsing HTTP 403 response body",
            "insufficient_scope: authorization failed",
        ):
            self.assertEqual(PUSH.classify_push_failure(message), PUSH.PERMISSION, message)

    def test_non_permission_failures_are_not_degraded(self) -> None:
        for message in (
            "net/http: TLS handshake timeout",
            "no space left on device",
            "manifest invalid: manifest blob unknown",
            "connection refused",
        ):
            self.assertEqual(PUSH.classify_push_failure(message), PUSH.OTHER, message)


class RealRepositoryTest(unittest.TestCase):
    """反过来跑真实仓库状态：检查一旦找不到目标就会安静地永远通过。"""

    def test_declared_requirements_cover_the_known_extras(self) -> None:
        declared = DEPENDENCIES.declared_requirements()
        for distribution in ("cryptography", "psycopg", "claude-agent-sdk", "alembic"):
            self.assertIn(distribution, declared, f"pyproject.toml 里找不到 {distribution}")

    def test_source_scan_finds_the_known_lazy_imports(self) -> None:
        # 全部是函数内延迟导入；扫不到它们说明扫描退化成了只看模块级。
        found = DEPENDENCIES.imported_top_level_modules()
        for module in ("cryptography", "psycopg", "claude_agent_sdk"):
            self.assertIn(module, found, f"src/lingxi/ 里扫不到 {module} 的延迟导入")

    def test_scheduler_constants_are_readable(self) -> None:
        # 停止宽限期的联动检查依赖这两个常量读得到；读不到时它会退化成不判定。
        self.assertIsInstance(
            CONTRACT.module_constant(CONTRACT.FEISHU_DIRECTORY, "REQUEST_TIMEOUT_SECONDS"), int
        )
        backoff = CONTRACT.module_constant(CONTRACT.SCHEDULER_APP, "SAVE_RETRY_BACKOFF_SECONDS")
        self.assertIsInstance(backoff, tuple)

    def test_compose_declares_a_sufficient_stop_grace_period(self) -> None:
        self.assertEqual(CONTRACT.check_stop_grace_period(), [])

    def test_deploy_files_exist(self) -> None:
        for path in (CONTRACT.DOCKERFILE, CONTRACT.DOCKERIGNORE, CONTRACT.COMPOSE_BASE,
                     CONTRACT.COMPOSE_STAGE, CONTRACT.COMPOSE_PROD, CONTRACT.ENV_EXAMPLE):
            self.assertTrue(path.is_file(), f"{path} 不存在，对应的检查会变成空转")


if __name__ == "__main__":
    unittest.main()
