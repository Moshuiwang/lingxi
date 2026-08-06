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
import json
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


class ImmutableTagOverwriteTest(unittest.TestCase):
    """不可变 tag 不得被覆盖（codex 审查 P1-5）。

    `docker push` 默认直接覆盖远端同名 tag。不拦这一下，"同一个 tag 永远指向同一份
    源码"就只是口头承诺，而回滚正建立在它之上——切回去的人会拿到别的镜像，且无提示。
    """

    def test_same_content_is_skipped(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(PUSH.classify_existing_tag(digest, digest), PUSH.TAG_IDENTICAL)

    def test_different_content_is_refused(self) -> None:
        local = "sha256:" + "a" * 64
        remote = "sha256:" + "b" * 64
        self.assertEqual(PUSH.classify_existing_tag(local, remote), PUSH.TAG_CONFLICT)

    def test_unknown_digest_is_refused_not_allowed(self) -> None:
        # 读不到 digest 时必须**拒绝**，不能当成"大概是同一个"放行。
        digest = "sha256:" + "a" * 64
        self.assertEqual(PUSH.classify_existing_tag(digest, None), PUSH.TAG_CONFLICT)
        self.assertEqual(PUSH.classify_existing_tag(None, digest), PUSH.TAG_CONFLICT)

    def _runner(self, returncode: int, stdout: str = "", stderr: str = ""):
        return lambda argv: PUSH.CommandResult(returncode, stdout, stderr)

    def test_absent_tag_reports_not_exists(self) -> None:
        runner = self._runner(1, stderr="manifest unknown: manifest unknown")
        exists, digest = PUSH.remote_config_digest("ghcr.io/x/y:z", runner=runner)
        self.assertFalse(exists)
        self.assertIsNone(digest)

    def test_query_failure_raises_rather_than_assuming_absent(self) -> None:
        # 网络故障不能被读成"远端没有这个 tag"——那会直接把覆盖放行。
        runner = self._runner(1, stderr="net/http: TLS handshake timeout")
        with self.assertRaises(RuntimeError):
            PUSH.remote_config_digest("ghcr.io/x/y:z", runner=runner)

    def test_existing_tag_returns_config_digest(self) -> None:
        digest = "sha256:" + "c" * 64
        runner = self._runner(0, stdout=json.dumps({"config": {"digest": digest}}))
        exists, found = PUSH.remote_config_digest("ghcr.io/x/y:z", runner=runner)
        self.assertTrue(exists)
        self.assertEqual(found, digest)


class OverrideBypassTest(unittest.TestCase):
    """覆盖文件不得把基线里的安全设置改回去（内审 P2-2）。

    compose 的覆盖文件后加载后生效：在 compose.prod.yaml 里写一行 `user: root` 就能
    把基线整个盖掉，而基线文件一个字没动——只看基线的静态检查会全绿。
    """

    def _with_override(self, body: str):
        directory = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        override = directory / "compose.prod.yaml"
        override.write_text("services:\n" + textwrap.indent(textwrap.dedent(body), "  "), encoding="utf-8")
        original = CONTRACT.COMPOSE_PROD
        CONTRACT.COMPOSE_PROD = override
        try:
            return CONTRACT.check_compose_contract()
        finally:
            CONTRACT.COMPOSE_PROD = original

    def test_override_to_root_is_caught(self) -> None:
        failures = self._with_override("scheduler:\n  user: root\n")
        self.assertTrue(any("root" in line for line in failures), failures)

    def test_override_privileged_is_caught(self) -> None:
        failures = self._with_override("worker:\n  privileged: true\n")
        self.assertTrue(any("privileged" in line for line in failures), failures)

    def test_override_disabling_read_only_is_caught(self) -> None:
        failures = self._with_override("scheduler:\n  read_only: false\n")
        self.assertTrue(any("read_only" in line for line in failures), failures)

    def test_override_shrinking_stop_grace_is_caught(self) -> None:
        failures = self._with_override("scheduler:\n  stop_grace_period: 10s\n")
        self.assertTrue(any("stop_grace_period" in line for line in failures), failures)

    def test_override_restarting_a_job_is_caught(self) -> None:
        failures = self._with_override("worker:\n  restart: always\n")
        self.assertTrue(any("restart" in line for line in failures), failures)

    def test_shared_env_file_across_services_is_caught(self) -> None:
        failures = self._with_override(
            "scheduler:\n  env_file:\n    - ./.env.prod\n"
            "worker:\n  env_file:\n    - ./.env.prod\n"
        )
        self.assertTrue(any("env_file" in line for line in failures), failures)


class DatabaseTimeoutTest(unittest.TestCase):
    """停机上界的依据必须真的写在示例 DSN 里（codex 审查 P1-3）。"""

    def test_real_example_carries_all_three_settings(self) -> None:
        self.assertEqual(CONTRACT.check_database_timeouts(), [])

    def test_budget_is_derived_from_the_documented_timeouts(self) -> None:
        # 每次操作 = 建连 + 语句 + 提交；不是只算建连。
        self.assertEqual(CONTRACT.DATABASE_OPERATION_SECONDS, 11.0)
        self.assertEqual(CONTRACT.DATABASE_ROUNDTRIP_BUDGET_SECONDS, 55.0)


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
