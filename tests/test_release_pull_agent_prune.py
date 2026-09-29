"""拉取代理部署成功后按名单清理旧镜像（#910 第二步）与管理群消息项目名前缀（#909）。

Docker 调用全部经可注入的桩对象，本文件不接触真实 Docker、GitHub 或飞书。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_release_pull_agent import AGENT, PullHarness, _json

REPO = "ghcr.io/moshuiwang/lingxi-"


def _digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _image_id(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def _service_digest(seed: str, service: str, *, shared: bool) -> str:
    """真实发布里四个服务各有摘要；拉取代理夹具的清单四服务共用一个摘要（``shared``）。"""
    return _digest(seed if shared else seed + str(AGENT.SERVICES.index(service)))


def _release(seed: str) -> dict:
    return {
        "images": {
            service: f"{REPO}{service}@{_service_digest(seed, service, shared=False)}"
            for service in AGENT.SERVICES
        }
    }


def _product_image(
    service: str, seed: str, id_seed: str, *, tagged: bool = False, shared: bool = False
) -> dict:
    return {
        "id": _image_id(id_seed),
        "repo_tags": [f"{REPO}{service}:tag-{seed}"] if tagged else [],
        "repo_digests": [f"{REPO}{service}@{_service_digest(seed, service, shared=shared)}"],
        "size": 200 * 1024 * 1024,
    }


def _plan(old_seed: str = "1", new_seed: str = "2") -> dict:
    return {"id": "plan-new", "old": _release(old_seed), "new": _release(new_seed)}


def _host_images() -> list:
    """本机现场：old(1)、new(2)、更早一版(3，含悬空 migrate)、再早一版被停止容器引用(4)、
    数据库镜像、同机其他项目镜像、跨仓库打过标签的本产品镜像、本地构建无仓库镜像。"""
    images = []
    for index, service in enumerate(AGENT.SERVICES):
        images.append(_product_image(service, "1", f"a{index}"))
        images.append(_product_image(service, "2", f"b{index}", tagged=True))
        images.append(_product_image(service, "3", f"c{index}"))
    images.append(_product_image("worker", "4", "d0"))
    images.append(
        {
            "id": _image_id("e0"),
            "repo_tags": ["postgres:17"],
            "repo_digests": [f"postgres@{_digest('5')}"],
            "size": 454 * 1024 * 1024,
        }
    )
    images.append(
        {
            "id": _image_id("e1"),
            "repo_tags": [],
            "repo_digests": [f"postgres@{_digest('6')}"],
            "size": 454 * 1024 * 1024,
        }
    )
    images.append(
        {
            "id": _image_id("e2"),
            "repo_tags": ["ghcr.io/other/project-api:latest"],
            "repo_digests": [f"ghcr.io/other/project-api@{_digest('7')}"],
            "size": 1,
        }
    )
    images.append(
        {
            "id": _image_id("e3"),
            "repo_tags": ["registry.local:5000/mirror/lingxi-gateway:copy"],
            "repo_digests": [f"{REPO}gateway@{_digest('8')}"],
            "size": 1,
        }
    )
    images.append({"id": _image_id("e4"), "repo_tags": [], "repo_digests": [], "size": 1})
    return images


class FakeDocker:
    """``DockerImages`` 的同名方法桩：记录每次调用，删除后从现场移除。"""

    def __init__(self, images: list, containers: set, *, rm_fail: set = frozenset()):
        self.images = [dict(item) for item in images]
        self.containers = set(containers)
        self.rm_fail = set(rm_fail)
        self.calls: list[tuple] = []

    def list_images(self) -> list:
        self.calls.append(("list_images",))
        return [dict(item) for item in self.images]

    def container_image_ids(self) -> set:
        self.calls.append(("container_image_ids",))
        return set(self.containers)

    def remove(self, image_id: str) -> bool:
        self.calls.append(("remove", image_id))
        if image_id in self.rm_fail:
            return False
        self.images = [item for item in self.images if item["id"] != image_id]
        return True

    def present(self, reference: str) -> bool:
        self.calls.append(("present", reference))
        digest = reference.rsplit("@", 1)[1]
        return any(
            ref.rsplit("@", 1)[1] == digest for item in self.images for ref in item["repo_digests"]
        )

    def removed(self) -> list:
        return [call[1] for call in self.calls if call[0] == "remove"]


def _deploy_images() -> list:
    """与夹具清单对齐的现场：old 版本种子 1、目标版本种子 2、更早一版种子 3，外加数据库镜像。"""
    return [
        _product_image(service, seed, f"{prefix}{index}", shared=True)
        for index, service in enumerate(AGENT.SERVICES)
        for seed, prefix in (("1", "a"), ("2", "b"), ("3", "c"))
    ] + [_host_images()[-5]]


PRODUCT_OLDER = {_image_id(f"c{index}") for index in range(4)}
NON_PRODUCT = {_image_id(seed) for seed in ("e0", "e1", "e2", "e3", "e4")}


def _quiet(function, *args, **kwargs):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        value = function(*args, **kwargs)
    return value, output.getvalue()


class ImagePruneListTests(unittest.TestCase):
    """纯函数名单：保留集、仓库范围、拒绝条件。"""

    def lists(self, plan=None, old_source="prev-plan", containers=frozenset({_image_id("d0")})):
        return AGENT.image_prune_lists(
            _plan() if plan is None else plan, old_source, _host_images(), set(containers)
        )

    def test_keep_set_is_old_new_and_container_referenced(self):
        result = self.lists()
        self.assertIsNone(result["refused"])
        reasons = {entry["id"]: entry["reason"] for entry in result["keep"]}
        for index in range(4):
            self.assertEqual(reasons[_image_id(f"a{index}")], "plan_old")
            self.assertEqual(reasons[_image_id(f"b{index}")], "plan_new")
        self.assertEqual(reasons[_image_id("d0")], "container")
        self.assertEqual(len(result["keep"]), 9)
        self.assertEqual({entry["id"] for entry in result["candidates"]}, PRODUCT_OLDER)
        self.assertEqual(result["missing"], [])

    def test_old_release_images_are_never_candidates(self):
        # 部署器 preflight 要求 old 四镜像（含一次性 migrate）按摘要在位：即使无标签、无容器引用。
        candidates = {entry["id"] for entry in self.lists()["candidates"]}
        for index in range(4):
            self.assertNotIn(_image_id(f"a{index}"), candidates)
            self.assertNotIn(_image_id(f"b{index}"), candidates)

    def test_dangling_migrate_is_recognised_by_repo_digest(self):
        names = {entry["name"] for entry in self.lists()["candidates"]}
        self.assertIn(REPO + "migrate", names)

    def test_non_product_images_are_never_candidates_or_listed(self):
        result = self.lists(containers=frozenset())
        listed = {entry["id"] for entry in result["candidates"] + result["keep"]}
        self.assertFalse(listed & NON_PRODUCT)
        # 没有容器引用时，停止容器那一版本产品镜像才进候选；非本产品一律不进。
        self.assertIn(_image_id("d0"), {entry["id"] for entry in result["candidates"]})

    def test_container_referenced_product_image_is_kept(self):
        result = self.lists(containers=frozenset({_image_id("c0")}))
        self.assertNotIn(_image_id("c0"), {entry["id"] for entry in result["candidates"]})

    def test_old_missing_or_malformed_refuses_everything(self):
        for name, plan in {
            "absent": {"id": "p", "new": _release("2")},
            "empty": {"id": "p", "old": {}, "new": _release("2")},
            "empty_images": {"id": "p", "old": {"images": {}}, "new": _release("2")},
            "bad_digest": {
                "id": "p",
                "old": {"images": {s: f"{REPO}{s}:latest" for s in AGENT.SERVICES}},
                "new": _release("2"),
            },
        }.items():
            with self.subTest(name=name):
                result = self.lists(plan=plan)
                self.assertEqual(result["refused"], AGENT.PRUNE_REFUSED_OLD_MISSING)
                self.assertEqual(result["candidates"], [])

    def test_first_takeover_or_unknown_source_refuses(self):
        for source, reason in (
            ("first_takeover", AGENT.PRUNE_REFUSED_FIRST_TAKEOVER),
            (None, AGENT.PRUNE_REFUSED_OLD_SOURCE_UNKNOWN),
            ("", AGENT.PRUNE_REFUSED_OLD_SOURCE_UNKNOWN),
        ):
            with self.subTest(source=source):
                result = self.lists(old_source=source)
                self.assertEqual(result["refused"], reason)
                self.assertEqual(result["candidates"], [])

    def test_absent_planned_image_is_reported_missing(self):
        images = [item for item in _host_images() if item["id"] != _image_id("a1")]
        result = AGENT.image_prune_lists(_plan(), "prev", images, set())
        self.assertEqual(
            result["missing"],
            [{"name": REPO + "migrate", "digest": "11" * 6, "reason": "plan_old"}],
        )


class RunImagePruneTests(unittest.TestCase):
    def test_dry_run_makes_zero_remove_calls(self):
        docker = FakeDocker(_host_images(), {_image_id("d0")})
        summary, output = _quiet(AGENT.run_image_prune, _plan(), "prev", docker, dry_run=True)
        self.assertEqual(docker.removed(), [])
        self.assertFalse(any(call[0] == "present" for call in docker.calls))
        self.assertEqual(summary["removed"], [])
        self.assertEqual(output.count("结果码=candidate"), 4)
        self.assertIn("结果码=dry_run", output)

    def test_removes_only_candidates_by_id_and_reads_back(self):
        docker = FakeDocker(_host_images(), {_image_id("d0")})
        summary, output = _quiet(AGENT.run_image_prune, _plan(), "prev", docker, dry_run=False)
        self.assertEqual(set(docker.removed()), PRODUCT_OLDER)
        self.assertEqual(summary["readback_missing"], [])
        presents = [call[1] for call in docker.calls if call[0] == "present"]
        self.assertEqual(len(presents), 8)
        self.assertIn("结果码=summary", output)
        self.assertIn("removed=4 failed=0 freed_mb=800", output)

    def test_single_rm_failure_continues_with_the_rest(self):
        docker = FakeDocker(_host_images(), set(), rm_fail={_image_id("c1")})
        summary, output = _quiet(AGENT.run_image_prune, _plan(), "prev", docker, dry_run=False)
        self.assertEqual(set(docker.removed()), PRODUCT_OLDER | {_image_id("d0")})
        self.assertEqual([entry["id"] for entry in summary["failed"]], [_image_id("c1")])
        self.assertEqual(len(summary["removed"]), 4)
        self.assertIn("结果码=rm_failed", output)

    def test_refused_makes_no_remove_calls(self):
        docker = FakeDocker(_host_images(), set())
        summary, output = _quiet(
            AGENT.run_image_prune, _plan(), "first_takeover", docker, dry_run=False
        )
        self.assertEqual(docker.removed(), [])
        self.assertEqual(summary["refused"], "first_takeover")
        self.assertIn("结果码=refused", output)


class DockerImagesCommandTests(unittest.TestCase):
    """真实调用层只发固定参数：删除按 ID、不带 -f、没有任何 prune。"""

    def test_remove_argv_is_plain_rm_by_id(self):
        with patch.object(AGENT, "_run_command", return_value="") as run:
            self.assertTrue(AGENT.DockerImages("/usr/bin/docker", 5).remove(_image_id("c0")))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/docker", "image", "rm", _image_id("c0")])

    def test_remove_rejects_non_id_and_reports_failure(self):
        docker = AGENT.DockerImages("/usr/bin/docker", 5)
        with patch.object(AGENT, "_run_command") as run:
            self.assertFalse(docker.remove("postgres:17"))
            run.assert_not_called()
        with patch.object(AGENT, "_run_command", side_effect=AGENT.AgentError("command_failed")):
            self.assertFalse(docker.remove(_image_id("c0")))

    def test_source_has_no_prune_or_force(self):
        source = Path(AGENT.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"prune"', source)
        self.assertNotIn('"-f"', source)
        self.assertNotIn('"--force"', source)


class PlanOldSourceTests(unittest.TestCase):
    def test_reads_deployer_ledger_inventory(self):
        harness = PullHarness()
        self.addCleanup(harness.close)
        plan = {"id": "plan-new"}
        self.assertIsNone(AGENT._plan_old_source(harness.state, plan))
        for source in ("first_takeover", "prev-plan"):
            _json(harness.state / "plan-new.state.json", {"inventory": {"old_source": source}})
            self.assertEqual(AGENT._plan_old_source(harness.state, plan), source)
        path = _json(harness.state / "plan-new.state.json", {"stages": {}})
        self.assertIsNone(AGENT._plan_old_source(harness.state, plan))
        path.chmod(0o644)
        self.assertIsNone(AGENT._plan_old_source(harness.state, plan))


class AgentIntegrationTests(unittest.TestCase):
    """接入点：部署器 verified 后才清理；清理的任何结局不改部署结果与状态账。"""

    def setUp(self):
        self.harness = PullHarness()
        self.addCleanup(self.harness.close)
        hostname = patch.object(AGENT.socket, "gethostname", return_value=self.harness.host["host"])
        hostname.start()
        self.addCleanup(hostname.stop)
        self.plans: list[dict] = []

    def run_agent(self, docker, *, old_source="prev-plan"):
        def source(state_directory, plan):
            self.plans.append(plan)
            return old_source

        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(AGENT, "send_alert"),
            patch.object(AGENT, "DockerImages", return_value=docker),
            patch.object(AGENT, "_plan_old_source", side_effect=source),
        ):
            code = AGENT.run_once(
                self.harness.host_path, self.harness.config_path, self.harness.state
            )
        return code, output.getvalue()

    def read_state(self) -> dict:
        return json.loads((self.harness.state / "pull-agent.json").read_text())

    def deploy(self, docker, **kwargs):
        self.harness.state_for_old()
        return self.run_agent(docker, **kwargs)

    def test_verified_deploy_prunes_with_the_plan_of_this_round(self):
        docker = FakeDocker(_deploy_images(), set())
        code, output = self.deploy(docker)
        self.assertEqual(code, 0)
        self.assertEqual(self.read_state()["last_result"], "verified")
        self.assertEqual(set(docker.removed()), PRODUCT_OLDER)
        self.assertEqual(self.plans[0]["new"]["tag"], self.harness.target_tag)
        self.assertEqual(self.plans[0]["old"]["tag"], self.harness.old_tag)
        # 清理在状态账与通知收口之后。
        self.assertLess(
            output.index("阶段=state 结果码=verified"), output.index("阶段=image_prune")
        )

    def assert_verified_outcome(self, code: int, expected_keys: set):
        """部署结果仍是 verified，状态账键集合与清理关闭时逐一相同（形状不变）。"""
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertEqual(set(state), expected_keys)
        self.assertEqual(state["last_result"], "verified")
        self.assertEqual(state["deployer_state"], "verified")
        self.assertEqual(state["target_tag"], self.harness.target_tag)
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(
            state["verified_digests"], self.harness.expected_digests(self.harness.target_tag)
        )
        self.assertNotIn("alert_delivery_error", state)

    def disabled_keys(self) -> set:
        """同一部署、清理开关关闭时的状态账键集合；随后换一个全新夹具。"""
        self.harness.write_config(image_prune=False)
        docker = FakeDocker(_deploy_images(), set())
        code, output = self.deploy(docker)
        self.assertEqual(code, 0)
        self.assertIn("阶段=image_prune 结果码=disabled", output)
        self.assertEqual(docker.calls, [])
        keys = set(self.read_state())
        self.fresh_harness()
        return keys

    def fresh_harness(self):
        self.harness.close()
        self.harness = PullHarness()
        self.addCleanup(self.harness.close)
        self.plans.clear()

    def test_rm_failure_leaves_result_and_state_unchanged(self):
        keys = self.disabled_keys()
        docker = FakeDocker(_deploy_images(), set(), rm_fail={_image_id("c2")})
        code, output = self.deploy(docker)
        self.assertEqual(len(docker.removed()), 4)
        self.assertIn("结果码=rm_failed", output)
        self.assert_verified_outcome(code, keys)

    def test_prune_exception_is_logged_not_raised(self):
        keys = self.disabled_keys()
        for error in (AGENT.AgentError("docker_inspect_unavailable"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                self.fresh_harness()
                docker = FakeDocker([], set())

                def explode(error=error):
                    raise error

                docker.list_images = explode
                code, output = self.deploy(docker)
                self.assertIn("阶段=image_prune 结果码=error", output)
                self.assert_verified_outcome(code, keys)

    def test_first_takeover_deploy_does_not_prune(self):
        docker = FakeDocker(_deploy_images(), set())
        code, output = self.deploy(docker, old_source="first_takeover")
        self.assertEqual(code, 0)
        self.assertEqual(docker.removed(), [])
        self.assertIn("结果码=refused", output)

    def test_non_verified_deploy_does_not_prune(self):
        self.harness.set_env(DEPLOY_STATUS="failed")
        docker = FakeDocker(_deploy_images(), set())
        self.deploy(docker)
        self.assertEqual(docker.calls, [])
        self.assertEqual(self.plans, [])

    def test_already_in_place_paths_do_not_prune(self):
        docker = FakeDocker(_deploy_images(), set())
        self.harness.state_for_verified_target()
        self.harness.set_env(DOCKER_MODE="match")
        code, _ = self.run_agent(docker)
        self.assertEqual((code, self.read_state()["last_result"]), (0, "already_in_place"))
        (self.harness.state / "pull-agent.json").unlink()
        code, _ = self.run_agent(docker)
        self.assertEqual((code, self.read_state()["last_result"]), (0, "already_in_place_external"))
        self.assertEqual(docker.calls, [])
        self.assertEqual(self.plans, [])


class ManualPruneCommandTests(unittest.TestCase):
    """单次命令：名单来源与自动路径同一函数；取不到上一正式版即拒绝。"""

    def setUp(self):
        self.harness = PullHarness()
        self.addCleanup(self.harness.close)
        hostname = patch.object(AGENT.socket, "gethostname", return_value=self.harness.host["host"])
        hostname.start()
        self.addCleanup(hostname.stop)
        self.harness.state_for_verified_target()
        plan_path = self.harness.state / "old-plan.plan.json"
        plan = json.loads(plan_path.read_text())
        plan["old"] = json.loads((self.harness.manifests / "v2.4.3.json").read_text())
        _json(plan_path, plan)
        _json(
            self.harness.state / "old-plan.state.json",
            {"inventory": {"old_source": "earlier-plan"}},
        )
        self.docker = FakeDocker(_deploy_images(), set())

    def run_command(self, *mode: str):
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(AGENT, "DockerImages", return_value=self.docker),
        ):
            code = AGENT.main(
                [
                    "--host-contract",
                    str(self.harness.host_path),
                    "--config",
                    str(self.harness.config_path),
                    "--state-directory",
                    str(self.harness.state),
                    "prune-images",
                    *mode,
                ]
            )
        return code, output.getvalue()

    def test_dry_run_lists_and_writes_nothing(self):
        before = {path.name: path.read_bytes() for path in self.harness.state.iterdir()}
        code, output = self.run_command("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.removed(), [])
        self.assertEqual(output.count("结果码=keep "), 8)
        self.assertEqual(output.count("结果码=candidate"), 4)
        after = {path.name: path.read_bytes() for path in self.harness.state.iterdir()}
        self.assertEqual(before, after)

    def test_yes_removes_and_reads_back(self):
        code, output = self.run_command("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(set(self.docker.removed()), PRODUCT_OLDER)
        self.assertEqual(sum(call[0] == "present" for call in self.docker.calls), 8)
        self.assertIn("readback_missing=0", output)

    def test_readback_missing_exits_nonzero(self):
        # 夹具清单四服务共用一个摘要：拿掉 old 版本全部镜像才构成「不在位」。
        self.docker.images = [
            item for item in self.docker.images if not item["id"].startswith("sha256:a")
        ]
        code, output = self.run_command("--yes")
        self.assertEqual(code, 1)
        self.assertIn("结果码=readback_missing", output)

    def test_mode_is_required_and_exclusive(self):
        for mode in ((), ("--dry-run", "--yes")):
            with self.subTest(mode=mode):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    self.run_command(*mode)

    def test_refuses_without_previous_release(self):
        state_path = self.harness.state / "pull-agent.json"
        original = json.loads(state_path.read_text())
        for name, change in {
            "not_verified": {"deployer_state": "running"},
            "external_target": {"target_tag": "v9.9.9"},
            "no_digests": {"verified_digests": None},
            "no_plan": {"plan_id": "missing-plan"},
        }.items():
            with self.subTest(name=name):
                _json(state_path, dict(original, **change))
                code, output = self.run_command("--yes")
                self.assertEqual(code, 1)
                self.assertIn("reason=previous_release_unavailable", output)
                self.assertEqual(self.docker.calls, [])

    def test_first_takeover_ledger_refuses(self):
        _json(
            self.harness.state / "old-plan.state.json",
            {"inventory": {"old_source": "first_takeover"}},
        )
        code, output = self.run_command("--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.docker.removed(), [])
        self.assertIn("reason=first_takeover", output)


class NoticePrefixTests(unittest.TestCase):
    """#909：管理群卡片标题与纯文本首行带 ``[lingxi] ``，幂等，旧 BI Plus 标签改写。"""

    def test_prefix_rules(self):
        for raw, expected in (
            ("版本部署已完成", "[lingxi] 版本部署已完成"),
            ("[lingxi] 版本部署已完成", "[lingxi] 版本部署已完成"),
            ("[BI Plus 预发] 版本部署已完成", "[lingxi] 版本部署已完成"),
            ("[BI Plus]版本部署已完成", "[lingxi] 版本部署已完成"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(AGENT.with_notice_prefix(raw), expected)
                self.assertEqual(AGENT.with_notice_prefix(expected), expected)

    def test_text_prefix_only_on_first_line(self):
        self.assertEqual(AGENT.prefixed_text("甲\n乙"), "[lingxi] 甲\n乙")
        self.assertEqual(AGENT.prefixed_text("[lingxi] 甲\n乙"), "[lingxi] 甲\n乙")

    def test_alert_card_title_is_prefixed_once(self):
        host = {"host": "h", "environment": "stage"}
        for result in ("verified", "failed", "recovered", "unknown"):
            with self.subTest(result=result):
                notice = AGENT._alert_message(host, "v1.0.0", "status", result, "p")
                title = notice.card_payload()["header"]["title"]["content"]
                self.assertTrue(title.startswith("[lingxi] "))
                self.assertEqual(title.count("[lingxi]"), 1)

    def test_plain_string_path_is_prefixed(self):
        sent = []
        credentials = {
            "LINGXI_FEISHU_APP_ID": "a",
            "LINGXI_FEISHU_APP_SECRET": "b",
            "LINGXI_ADMIN_GROUP_CHAT_ID": "oc_x",
        }
        with (
            patch.object(AGENT, "_load_alert_credentials", return_value=credentials),
            patch.object(AGENT, "_feishu_token", return_value="t"),
            patch.object(AGENT, "_post_alert", side_effect=lambda *a: sent.append(a)),
        ):
            AGENT.send_alert("第一行\n第二行", Path("/nonexistent"), 5)
            AGENT.send_alert("[lingxi] 已带", Path("/nonexistent"), 5)
        self.assertEqual(sent[0][2:4], ("text", {"text": "[lingxi] 第一行\n第二行"}))
        self.assertEqual(sent[1][3], {"text": "[lingxi] 已带"})


if __name__ == "__main__":
    unittest.main()
