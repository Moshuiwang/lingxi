"""#585 部署契约：三个常驻服务看到同一个 runtime-config 只读挂载。

补齐前 `gateway` 是三个常驻进程里唯一没挂这个目录的那一个（曾登记为「已知
边界」）：给 scheduler 与 gateway 都配上 `LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH`
会让 gateway「配了却读不到」，三处管理动作各自失败关闭；用户可见文案外置同理，
gateway 渲染的句子会与另外两个进程不一致。这类失败不会让容器起不来，只会让某
一类操作静默不可用——必须由会变红的断言守着，不能靠读 compose 靠眼睛。

挂载源是 `${...}` 插值，`check_deploy_contract.py` 的 `_volume_mounts` 只解析
字面量卷名，因此这里用整行字面量比对，并复用它的 `strip_comments`/`service_block`
定界（compose 注释里写着这条挂载的说明文字，天真的 grep 会把说明当成声明）。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPTS = REPOSITORY_ROOT / "scripts" / "ci"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTRACT = _load_module(
    SCRIPTS / "check_deploy_contract.py", "deploy_contract_runtime_config_under_test"
)

#: 三个常驻服务共用的运行时配置只读挂载，逐字取自 compose。
RUNTIME_CONFIG_MOUNTS = {
    "scheduler": "${LINGXI_SCHEDULER_RUNTIME_CONFIG_DIR:-/opt/lingxi/runtime-config}:/etc/lingxi/runtime:ro",
    "gateway": "${LINGXI_SCHEDULER_RUNTIME_CONFIG_DIR:-/opt/lingxi/runtime-config}:/etc/lingxi/runtime:ro",
    "worker-queue": "${LINGXI_WORKER_RUNTIME_CONFIG_DIR:-/opt/lingxi/runtime-config}:/etc/lingxi/runtime:ro",
}

CONTAINER_PATH = "/etc/lingxi/runtime"


def declared_runtime_config_mounts(compose_text: str, service: str) -> list[str]:
    """某个 service 的 ``volumes:`` 里挂到容器 ``/etc/lingxi/runtime`` 的条目原文。

    先按 service 定界、再按 ``volumes:`` 子块定界，最后只看以 ``- `` 开头的列表
    项——三层都不做整块字符串包含判断，避免注释或别处的形似字符串造成假绿。
    """
    block = CONTRACT.service_block(CONTRACT.strip_comments(compose_text), service)
    if block is None:
        return []
    volumes = CONTRACT.service_block(block, "volumes")
    if volumes is None:
        return []
    declared: list[str] = []
    for line in volumes.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and f":{CONTAINER_PATH}:" in stripped:
            declared.append(stripped[2:].strip())
        elif stripped.startswith("- ") and stripped.endswith(f":{CONTAINER_PATH}"):
            declared.append(stripped[2:].strip())
    return declared


def check_runtime_config_mounts(compose_text: str, origin: str) -> list[str]:
    """核对三个常驻服务各自声明了自己那条只读挂载；返回失败说明列表。"""
    failures: list[str] = []
    for service, expected in RUNTIME_CONFIG_MOUNTS.items():
        declared = declared_runtime_config_mounts(compose_text, service)
        if expected not in declared:
            failures.append(
                f"{origin} 的 {service} 没有声明 runtime-config 只读挂载 {expected}；"
                "缺了它，这个进程会「配了外置文件路径却读不到」——文件在宿主机上、"
                "容器里没有——对应能力静默失效而容器健康检查照常通过。"
                f"实际声明：{declared or '（无）'}"
            )
    return failures


class RealComposeDeclaresTheMountTest(unittest.TestCase):
    """入库的 stage 与 prod compose 都必须让三个常驻服务看到同一个目录。"""

    def test_stage_and_prod_declare_the_mount_for_all_three_resident_services(self) -> None:
        for name in ("compose.stage.yaml", "compose.prod.yaml"):
            with self.subTest(compose=name):
                text = (REPOSITORY_ROOT / "deploy" / name).read_text(encoding="utf-8")
                self.assertEqual(check_runtime_config_mounts(text, name), [])

    def test_the_mount_is_read_only_in_both_compose_files(self) -> None:
        """只读是刻意的：三个进程都没有任何理由写运维手动维护的配置目录。

        写权限一旦给出去，一次误写就会让宿主机上的文案 / 映射文件被容器改掉，
        而运维仍以为自己是唯一的写入方。
        """
        for name in ("compose.stage.yaml", "compose.prod.yaml"):
            text = (REPOSITORY_ROOT / "deploy" / name).read_text(encoding="utf-8")
            for service in RUNTIME_CONFIG_MOUNTS:
                with self.subTest(compose=name, service=service):
                    declared = declared_runtime_config_mounts(text, service)
                    self.assertTrue(declared, f"{name}/{service} 没有任何 runtime-config 挂载")
                    for mount in declared:
                        self.assertTrue(mount.endswith(":ro"), mount)

    def test_scheduler_and_gateway_share_the_same_interpolation_variable(self) -> None:
        """一台宿主机一个 runtime-config 目录：两个服务分开变量就会各读各的。"""
        for name in ("compose.stage.yaml", "compose.prod.yaml"):
            text = (REPOSITORY_ROOT / "deploy" / name).read_text(encoding="utf-8")
            with self.subTest(compose=name):
                self.assertEqual(
                    declared_runtime_config_mounts(text, "scheduler"),
                    declared_runtime_config_mounts(text, "gateway"),
                )


class MutationTest(unittest.TestCase):
    """变异验红：去掉 gateway 那条挂载必须让上面的核对变红。"""

    @staticmethod
    def _compose(gateway_body: str) -> str:
        """合成一份只有三个常驻服务的 compose；``gateway_body`` 是它块内的正文。"""
        return "\n".join(
            [
                "services:",
                "  scheduler:",
                "    volumes:",
                f"      - {RUNTIME_CONFIG_MOUNTS['scheduler']}",
                "  gateway:",
                textwrap.indent(textwrap.dedent(gateway_body).strip("\n"), "    "),
                "  worker-queue:",
                "    volumes:",
                f"      - {RUNTIME_CONFIG_MOUNTS['worker-queue']}",
                "",
            ]
        )

    def test_a_gateway_without_the_mount_is_caught(self) -> None:
        """这正是 #585 修复前 compose 的真实形状。"""
        text = self._compose("env_file:\n  - ./.env.stage.gateway")
        failures = check_runtime_config_mounts(text, "合成 compose")
        self.assertTrue(any("gateway" in failure for failure in failures), failures)

    def test_a_read_write_mount_is_not_accepted_as_the_declaration(self) -> None:
        mount = RUNTIME_CONFIG_MOUNTS["gateway"].removesuffix(":ro")
        text = self._compose(f"volumes:\n  - {mount}")
        failures = check_runtime_config_mounts(text, "合成 compose")
        self.assertTrue(any("gateway" in failure for failure in failures), failures)

    def test_the_mount_string_in_a_comment_does_not_produce_a_false_pass(self) -> None:
        text = self._compose(
            "env_file:\n  - ./.env.stage.gateway\n"
            f"# 说明：这里本该有 - {RUNTIME_CONFIG_MOUNTS['gateway']}"
        )
        failures = check_runtime_config_mounts(text, "合成 compose")
        self.assertTrue(any("gateway" in failure for failure in failures), failures)

    def test_a_fully_wired_compose_passes(self) -> None:
        text = self._compose(f"volumes:\n  - {RUNTIME_CONFIG_MOUNTS['gateway']}")
        self.assertEqual(check_runtime_config_mounts(text, "合成 compose"), [])


class UnsetVariableIsZeroChangeTest(unittest.TestCase):
    """部署契约的另一半：挂载在、变量不配时行为与交付前逐字相同。"""

    def test_no_compose_file_configures_the_override_variable(self) -> None:
        """本批**不给生产开这个变量**：交付的是能力与文档，不是一次文案变更。

        变量若被写进入库的 compose `environment:` 块，就会绕过 env 文件、在每
        台机器上强制生效——这正是需要一条会变红的断言守住的形状。
        """
        for name in ("compose.yaml", "compose.stage.yaml", "compose.prod.yaml"):
            text = CONTRACT.strip_comments(
                (REPOSITORY_ROOT / "deploy" / name).read_text(encoding="utf-8")
            )
            with self.subTest(compose=name):
                self.assertNotIn("LINGXI_CONTENT_OVERRIDE_PATH", text)

    def test_an_unset_variable_renders_the_image_catalog(self) -> None:
        from lingxi.config.content import REQUIRED_TEXT_KEYS, ContentCatalog
        from lingxi.config.content_override import load_content_source

        base = ContentCatalog.from_file()
        catalog = load_content_source(None).catalog
        self.assertEqual(catalog.version, base.version)
        for key in REQUIRED_TEXT_KEYS:
            self.assertEqual(catalog.text_template(key), base.text_template(key))

    def test_a_configured_variable_pointing_at_a_missing_file_does_not_block_startup(
        self,
    ) -> None:
        """配了变量但文件还没放上去（或刚被删掉回滚）不得让进程起不来。"""
        from lingxi.config.content_override import load_content_source

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = load_content_source(directory / "content.override.toml")
        self.assertIsNone(source.rejection)
        self.assertEqual(source.override_keys, ())

    def test_env_example_documents_the_variable_for_all_three_services(self) -> None:
        """三个进程要配就一起配：示例文件里三处都得有示范，删掉任一处即变红。"""
        text = (REPOSITORY_ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("LINGXI_CONTENT_OVERRIDE_PATH"), 3)


if __name__ == "__main__":
    unittest.main()
