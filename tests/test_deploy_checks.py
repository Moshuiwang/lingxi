"""`scripts/ci/check_runtime_dependencies.py`、`check_deploy_contract.py` 与
`push_image.py` 的判定用例（Issue #62 / S11）。

这三份检查的价值全在它们**会变红**。所以下面每一条都构造一份坏输入，断言它被
**具体地**拒绝；最后一组反过来跑真实仓库状态，防止检查因为文件结构变化而变成空转
（一个再也找不到目标的检查会安静地永远通过，比没有检查更危险）。

镜像层面的断言（rootfs 内容、非 root、两次构建等价、旧镜像在新库上启动）需要
docker，留给 `scripts/ci/verify_image_contract.sh`、`verify_compose_structure.sh`
与 `verify_old_image_new_schema.sh`，由 `Epic Full / image` job 执行，不在本文件覆盖范围。
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
    """不可变 tag 不得被覆盖，但**重跑必须幂等**（codex P1-5 + 二轮 P1-2）。

    第一版拿 config digest 当身份，那是错的：config 里有 created 与 history 时间戳，
    同一提交两次构建必然不同（实测两个不同的 Id）。于是每一次重跑都会被判成冲突，
    一次部分推送失败之后就再也推不上去——一个把自己锁死的守卫。
    身份改用来源：源提交 sha + 源码树哈希，两者对同一提交恒定。
    """

    SOURCE = ("8e26bc6b2f40383e7b9d33955d8b770352fa726b", "6d04c0a84f19")
    OTHER = ("0000000000000000000000000000000000000000", "ffffffffffff")

    def test_rerun_of_same_commit_is_skipped(self) -> None:
        self.assertEqual(PUSH.classify_existing_tag(self.SOURCE, self.SOURCE), PUSH.TAG_IDENTICAL)

    def test_different_source_is_refused(self) -> None:
        self.assertEqual(PUSH.classify_existing_tag(self.SOURCE, self.OTHER), PUSH.TAG_CONFLICT)

    def test_same_commit_but_different_tree_is_refused(self) -> None:
        # 同一个 commit sha 却是不同的树（例如带未提交改动构建出来的），仍算异源。
        mutated = (self.SOURCE[0], "aaaaaaaaaaaa")
        self.assertEqual(PUSH.classify_existing_tag(self.SOURCE, mutated), PUSH.TAG_CONFLICT)

    def test_unreadable_labels_are_refused_not_allowed(self) -> None:
        # 无法判定时放行覆盖是最坏的选择。
        self.assertEqual(PUSH.classify_existing_tag(self.SOURCE, None), PUSH.TAG_UNKNOWN)
        self.assertEqual(PUSH.classify_existing_tag(None, self.SOURCE), PUSH.TAG_UNKNOWN)

    def _runner(self, returncode: int, stdout: str = "", stderr: str = ""):
        return lambda argv: PUSH.CommandResult(returncode, stdout, stderr)

    def test_absent_tag_reports_not_exists(self) -> None:
        runner = self._runner(1, stderr="manifest unknown: manifest unknown")
        self.assertFalse(PUSH.remote_tag_exists("ghcr.io/x/y:z", runner=runner))

    def test_existing_tag_reports_exists(self) -> None:
        runner = self._runner(0, stdout="{}")
        self.assertTrue(PUSH.remote_tag_exists("ghcr.io/x/y:z", runner=runner))

    def test_query_failure_raises_rather_than_assuming_absent(self) -> None:
        # 网络故障被读成"远端没有这个 tag"，等于直接放行覆盖。
        runner = self._runner(1, stderr="net/http: TLS handshake timeout")
        with self.assertRaises(RuntimeError):
            PUSH.remote_tag_exists("ghcr.io/x/y:z", runner=runner)

    def test_identity_is_read_from_labels(self) -> None:
        runner = self._runner(0, stdout="8e26bc6b2f40 6d04c0a84f19\n")
        self.assertEqual(
            PUSH.image_source_identity("x:y", runner=runner), ("8e26bc6b2f40", "6d04c0a84f19")
        )

    def test_missing_labels_yield_none(self) -> None:
        for stdout in ("<no value> <no value>\n", "unknown unknown\n", "\n"):
            self.assertIsNone(PUSH.image_source_identity("x:y", runner=self._runner(0, stdout=stdout)))

    def test_partial_failure_is_recoverable(self) -> None:
        """一次部分推送失败之后的补推：已推上去的跳过，没推的照常推。

        这正是 digest 方案做不到的——它会把已推上去的那个判成冲突并整体卡死。
        """

        already_pushed = PUSH.classify_existing_tag(self.SOURCE, self.SOURCE)
        self.assertEqual(already_pushed, PUSH.TAG_IDENTICAL, "已推上去的应跳过而不是冲突")
        # 没推上去的那个远端不存在，走的是 remote_tag_exists → False → 正常推送路径。
        runner = self._runner(1, stderr="manifest unknown")
        self.assertFalse(PUSH.remote_tag_exists("ghcr.io/x/lingxi-worker:t", runner=runner))


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


class PublishJobGuardTest(unittest.TestCase):
    """Epic Full 候选与 main Publish 之间不能被一行配置绕过。"""

    FULL = """
        on:
          pull_request:
            branches: [main]
          workflow_call:
        jobs:
          classify:
          docs:
            name: Epic Full / docs
            if: needs.classify.outputs.mode == 'docs'
            steps:
              - run: scripts/ci/verify_docs.sh
          gate:
            if: needs.classify.outputs.mode != 'docs'
          extras:
            strategy:
              matrix:
                extra: [scheduler, worker, gateway, bot-test, migrate]
          image:
            steps:
              - run: python3 scripts/ci/write_epic_candidate_images.py
              - run: python3 scripts/ci/verify_epic_candidate_bundle.py
              - uses: actions/upload-artifact@sha
                with:
                  name: epic-candidate-images-pr-1-abc
          candidate:
            needs: [classify, docs, gate, extras, image]
            steps:
              - run: python3 scripts/ci/write_epic_candidate.py
              - uses: actions/upload-artifact@sha
    """
    STORY = """
        name: Story Fast
        on:
          pull_request:
            branches: ['epic/**']
        jobs:
          classify:
            steps:
              - run: python3 scripts/ci/classify_story_changes.py
          docs:
            steps:
              - run: scripts/ci/verify_docs.sh
          full:
            uses: ./.github/workflows/ci.yml
    """
    PUBLISH = """
        on:
          push:
            paths:
              - 'Dockerfile'
              - '.dockerignore'
              - 'deploy/**'
        jobs:
          candidate:
            steps:
              - run: python3 scripts/ci/verify_epic_candidate.py
          publish:
            if: github.event_name == 'push' && github.ref == 'refs/heads/main'
            needs: [candidate]
            permissions:
              contents: read
              packages: write
            steps:
              - run: scripts/ci/build_image.sh "${service}"
              - run: python3 scripts/ci/push_image.py x
    """

    def _with_workflows(self, *, full: str | None = None, story: str | None = None, publish: str | None = None):
        directory = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        paths = {
            "CI_WORKFLOW": directory / "ci.yml",
            "STORY_WORKFLOW": directory / "story.yml",
            "PUBLISH_WORKFLOW": directory / "publish.yml",
        }
        paths["CI_WORKFLOW"].write_text(textwrap.dedent(full or self.FULL), encoding="utf-8")
        paths["STORY_WORKFLOW"].write_text(textwrap.dedent(story or self.STORY), encoding="utf-8")
        paths["PUBLISH_WORKFLOW"].write_text(textwrap.dedent(publish or self.PUBLISH), encoding="utf-8")
        originals = {name: getattr(CONTRACT, name) for name in paths}
        for name, path in paths.items():
            setattr(CONTRACT, name, path)
        try:
            return CONTRACT.check_ci_workflow()
        finally:
            for name, path in originals.items():
                setattr(CONTRACT, name, path)

    def test_publish_without_push_main_gate_is_caught(self) -> None:
        publish = self.PUBLISH.replace(
            "if: github.event_name == 'push' && github.ref == 'refs/heads/main'", "if: always()"
        )
        failures = self._with_workflows(publish=publish)
        self.assertTrue(any("packages: write" in f and "refs/heads/main" in f for f in failures), failures)

    def test_publish_without_candidate_in_needs_is_caught(self) -> None:
        failures = self._with_workflows(publish=self.PUBLISH.replace("needs: [candidate]", "needs: []"))
        self.assertTrue(any("needs" in f and "candidate" in f for f in failures), failures)

    def test_wellformed_publish_job_passes(self) -> None:
        failures = self._with_workflows()
        self.assertEqual(failures, [])

    def test_repeating_full_gate_on_main_is_caught(self) -> None:
        publish = self.PUBLISH.replace(
            "- run: python3 scripts/ci/verify_epic_candidate.py",
            "- run: python3 scripts/ci/verify_epic_candidate.py\n              - run: scripts/ci/verify_repository.sh",
        )
        failures = self._with_workflows(publish=publish)
        self.assertTrue(any("不得重复验收" in failure for failure in failures), failures)

    def test_candidate_must_need_all_full_legs(self) -> None:
        full = self.FULL.replace(
            "needs: [classify, docs, gate, extras, image]", "needs: [classify, docs, gate, extras]"
        )
        failures = self._with_workflows(full=full)
        self.assertTrue(any("candidate needs" in failure for failure in failures), failures)

    def test_missing_image_job_is_caught(self) -> None:
        """Issue #150：没有 image job 就没有 PR 候选四镜像 artifact，必须明确报错。"""

        full = self.FULL.replace(
            """
          image:
            steps:
              - run: python3 scripts/ci/write_epic_candidate_images.py
              - run: python3 scripts/ci/verify_epic_candidate_bundle.py
              - uses: actions/upload-artifact@sha
                with:
                  name: epic-candidate-images-pr-1-abc""",
            "",
        )
        failures = self._with_workflows(full=full)
        self.assertTrue(any("缺少 image job" in failure for failure in failures), failures)

    def test_image_job_missing_export_script_is_caught(self) -> None:
        """image job 存在但漏掉导出/自校验/上传其中一步，同样要挡住（Issue #150）。"""

        full = self.FULL.replace(
            "- run: python3 scripts/ci/write_epic_candidate_images.py\n              ", ""
        )
        failures = self._with_workflows(full=full)
        self.assertTrue(
            any("write_epic_candidate_images.py" in failure for failure in failures), failures
        )


class RealWorkflowTest(unittest.TestCase):
    """真实 ci.yml 的两腿构建必须走同一条路径（验收微验 P1）。

    A 腿走 build_image.sh、B 腿裸 docker build 时，A 带上来源标签而 B 是 unknown；
    等价步骤逐字段比 .Config（Labels 就在里面）必然不一致，等价检查恒红，
    而 publish needs 着 image——整条发布路径被自己堵死。
    """

    def test_contract_check_is_invoked_with_every_image(self) -> None:
        """ci.yml 必须把**四个**镜像都传给契约检查（PR #78 的 CI 首跑缺陷）。

        少传一个时脚本会回落到 `:probe` 默认值——一个"本机碰巧有、CI 上从来没有"的
        tag，于是 CI 跑到一半炸出 `No such object: lingxi-gateway:probe`，而本机因为
        留着验收用的 :probe 镜像，同一份脚本全绿。**本机残留掩盖了参数缺失。**

        这条断言直接盯住调用点：新增服务却忘了加进这一行，会在门禁上变红。
        """

        text = CONTRACT.read(CONTRACT.CI_WORKFLOW)
        index = text.index("verify_image_contract.sh")
        invocation = text[index:index + 400]
        # 取到该命令的结尾（下一个不以续行符结尾的行）
        lines = []
        for line in invocation.splitlines()[:6]:
            lines.append(line)
            if not line.rstrip().endswith("\\"):
                break
        joined = " ".join(lines)
        for service in ("scheduler", "worker", "migrate", "gateway"):
            self.assertIn(
                f"lingxi-{service}:build-a", joined,
                f"ci.yml 调用 verify_image_contract.sh 时漏了 {service}；"
                "脚本会回落到 :probe 默认值，CI 上不存在",
            )

    def test_both_build_legs_use_the_same_script(self) -> None:
        text = CONTRACT.read(CONTRACT.CI_WORKFLOW)
        image_job = text[text.index("  image:"):text.index("  candidate:")]
        bare_builds = [
            line.strip()
            for line in image_job.splitlines()
            if "docker build" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            bare_builds, [],
            "image job 里出现了裸 docker build；两腿都必须走 scripts/ci/build_image.sh，"
            "否则构建参数会在两腿之间漂移（来源标签就是这么漂的）",
        )
        self.assertEqual(image_job.count("scripts/ci/build_image.sh"), 2)


class DatabaseTimeoutTest(unittest.TestCase):
    """停机上界必须来自连接工厂事实源，示例 DSN 只做默认值对账。"""

    def test_real_example_carries_all_three_settings(self) -> None:
        self.assertEqual(CONTRACT.check_database_timeouts(), [])

    def test_budget_is_derived_from_the_documented_timeouts(self) -> None:
        # 默认值仍是 5+3+3=11；停止预算必须按合法覆盖的最坏 5+5+5=15 建模。
        self.assertEqual(CONTRACT._default_database_operation_seconds(), 11.0)
        self.assertEqual(CONTRACT.DATABASE_OPERATION_SECONDS, 15.0)
        self.assertEqual(CONTRACT.DATABASE_ROUNDTRIP_BUDGET_SECONDS, 75.0)
        self.assertEqual(CONTRACT.POSTGRES_MAX_TIMEOUT_SECONDS, 5)

    def test_an_overbudget_factory_maximum_is_caught(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as directory:
            adapter = Path(directory) / "postgres.py"
            adapter.write_text(
                "DEFAULT_CONNECT_TIMEOUT_SECONDS = 5\n"
                "DEFAULT_STATEMENT_TIMEOUT_SECONDS = 3\n"
                "DEFAULT_LOCK_TIMEOUT_SECONDS = 2\n"
                "MAX_TIMEOUT_SECONDS = 60\n",
                encoding="utf-8",
            )
            original = CONTRACT.POSTGRES_ADAPTER
            CONTRACT.POSTGRES_ADAPTER = adapter
            try:
                failures = CONTRACT.check_stop_grace_period()
            finally:
                CONTRACT.POSTGRES_ADAPTER = original

        self.assertTrue(any("合法覆盖按 60s 建模" in failure for failure in failures), failures)
        self.assertTrue(any("低于要求" in failure for failure in failures), failures)

    def test_dsn_assumption_drift_from_factory_default_is_caught(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as directory:
            adapter = Path(directory) / "postgres.py"
            env_example = Path(directory) / ".env.example"
            adapter.write_text(
                "DEFAULT_CONNECT_TIMEOUT_SECONDS = 5\n"
                "DEFAULT_STATEMENT_TIMEOUT_SECONDS = 4\n"
                "DEFAULT_LOCK_TIMEOUT_SECONDS = 2\n"
                "MAX_TIMEOUT_SECONDS = 5\n",
                encoding="utf-8",
            )
            env_example.write_text(
                "LINGXI_POSTGRES_DSN=postgresql://user:password@host:5432/db?connect_timeout=5"
                "&options=-c%20statement_timeout%3D3000%20-c%20lock_timeout%3D2000\n",
                encoding="utf-8",
            )
            original_adapter = CONTRACT.POSTGRES_ADAPTER
            original_env = CONTRACT.ENV_EXAMPLE
            CONTRACT.POSTGRES_ADAPTER = adapter
            CONTRACT.ENV_EXAMPLE = env_example
            try:
                failures = CONTRACT.check_database_timeouts()
            finally:
                CONTRACT.POSTGRES_ADAPTER = original_adapter
                CONTRACT.ENV_EXAMPLE = original_env

        self.assertTrue(any("statement_timeout=4000" in failure for failure in failures), failures)


class GatewayOrchestrationTest(unittest.TestCase):
    """gateway 纳入编排后的三条硬约束（#57 增补轮）。

    gateway 是本批唯一一个「组件已交付但**不能上线**」的服务：它入队之后没有消费者
    （worker 是单回合 CLI，不领任务）。编排必须让这件事在机制上成立，而不是靠记得。
    """

    NO_PROFILE = 'gateway:\n  image: ${LINGXI_IMAGE_REGISTRY:?x}/lingxi-gateway:${LINGXI_IMAGE_TAG:?y}\n  user: "10001:10001"\n  read_only: true\n  stop_grace_period: 60s\n'
    SHORT_GRACE = 'gateway:\n  image: ${LINGXI_IMAGE_REGISTRY:?x}/lingxi-gateway:${LINGXI_IMAGE_TAG:?y}\n  profiles: ["gateway"]\n  user: "10001:10001"\n  read_only: true\n  stop_grace_period: 20s\n'

    def _with_base(self, body: str):
        directory = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        base = directory / "compose.yaml"
        base.write_text("services:\n" + textwrap.indent(body, "  "), encoding="utf-8")
        original = CONTRACT.COMPOSE_BASE
        CONTRACT.COMPOSE_BASE = base
        try:
            return CONTRACT.check_compose_contract()
        finally:
            CONTRACT.COMPOSE_BASE = original

    def test_gateway_without_profile_is_caught(self) -> None:
        failures = self._with_base(self.NO_PROFILE)
        self.assertTrue(any("profile" in f for f in failures), failures)

    def test_gateway_short_stop_grace_is_caught(self) -> None:
        failures = self._with_base(self.SHORT_GRACE)
        self.assertTrue(any("gateway" in f and "stop_grace_period" in f for f in failures), failures)

    def test_gateway_budget_is_derived_from_its_own_config(self) -> None:
        # 停机超时来自 apps/gateway/config.py 的默认值，出站取它的 1/4。
        self.assertEqual(CONTRACT._gateway_shutdown_timeout(), 20.0)
        self.assertEqual(CONTRACT._gateway_worst_case_seconds(), 20.0 + 5.0 + 15.0)

    def test_real_compose_gateway_passes(self) -> None:
        self.assertEqual([f for f in CONTRACT.check_compose_contract() if "gateway" in f], [])


class RealRepositoryTest(unittest.TestCase):
    """反过来跑真实仓库状态：检查一旦找不到目标就会安静地永远通过。"""

    def test_declared_requirements_cover_the_known_extras(self) -> None:
        declared = DEPENDENCIES.declared_requirements()
        for distribution in ("cryptography", "psycopg", "claude-agent-sdk", "alembic", "lark-oapi"):
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
        backoff = CONTRACT.module_constant(
            CONTRACT.SCHEDULER_CREDENTIAL_ROTATION, "SAVE_RETRY_BACKOFF_SECONDS"
        )
        self.assertIsInstance(backoff, tuple)

    def test_compose_declares_a_sufficient_stop_grace_period(self) -> None:
        self.assertEqual(CONTRACT.check_stop_grace_period(), [])

    def test_deploy_files_exist(self) -> None:
        for path in (CONTRACT.DOCKERFILE, CONTRACT.DOCKERIGNORE, CONTRACT.COMPOSE_BASE,
                     CONTRACT.COMPOSE_STAGE, CONTRACT.COMPOSE_PROD, CONTRACT.ENV_EXAMPLE):
            self.assertTrue(path.is_file(), f"{path} 不存在，对应的检查会变成空转")


if __name__ == "__main__":
    unittest.main()
