"""受控预开通脚本借 `build_loop` 重建装配时不注册受限管理入口（Issue #826）。

常驻 scheduler 已持有内测管理 socket 及其文件锁；同容器内 `docker exec` 起的第二个进程
再建一个监听器会以 ``socket_in_use`` 失败关闭，脚本因此「开通编排装配失败，未执行任何
一人」。修法是 `build_loop` 的显式参数 ``register_innertest_entry``（默认 ``True``，常驻
``main()`` 不传、行为不变），脚本的 `resolve_start_system` 传 ``False``。

三条用例都跑**真的** `build_loop`（完整接线的环境 + 内测 scope 已配置），只替换会碰真实
文件 / socket / 线程的三处（绑定文件读取、socket 监听器类、后台阶段消费者的线程启动）：

- (a) ``register_innertest_entry=False``：监听器、后台阶段消费者、租约保活三者都不建，
  `loop.duties` 里仍有且仅有一个带 ``onboarding_runner`` 的职责，并留一行 info 日志；
- (b) 默认调用（不传参数）在同一配置下仍注册入口，三者都建；
- (c) 脚本的 `resolve_start_system` 真的传了 ``False``，装出来的编排也没有监听器。

变异锚点：把「``False`` 时跳过」删掉（恒注册）→ (a) 红；把默认值改成 ``False`` → (b) 红；
把脚本里的 ``register_innertest_entry=False`` 删掉 → (c) 红。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_preprovision_ops import TOOL
from test_scheduler_onboarding_assembly import WIRED_ENV, RecordingAudit

from lingxi.apps.scheduler import SchedulerConfig, build_loop
from lingxi.apps.scheduler.onboarding import join_onboarding_executors


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "需要 scheduler 加密依赖")
class PreprovisionAssemblyTests(unittest.TestCase):
    def _env(self) -> dict[str, str]:
        """完整接线（开通编排真的注册）+ 内测 scope 已配置（常驻会注册受限管理入口）。"""
        from cryptography.fernet import Fernet

        credential_dir = tempfile.TemporaryDirectory()
        self.addCleanup(credential_dir.cleanup)
        user_env_dir = tempfile.TemporaryDirectory()
        self.addCleanup(user_env_dir.cleanup)
        socket_dir = tempfile.TemporaryDirectory()
        self.addCleanup(socket_dir.cleanup)
        return {
            **WIRED_ENV,
            "LINGXI_DELEGATED_CREDENTIAL_KEY": Fernet.generate_key().decode(),
            "LINGXI_DELEGATED_CREDENTIAL_PATH": str(Path(credential_dir.name) / "delegated.enc"),
            "LINGXI_USER_ENV_ROOT": user_env_dir.name,
            "LINGXI_PERMISSION_BITABLE_APP_TOKEN": "bascnFakeToken",
            "LINGXI_PERMISSION_BITABLE_TABLE_ID": "tblFakeTable",
            "LINGXI_INNERTEST_SCOPE": "synthetic",
            "LINGXI_INNERTEST_BINDING_ID": "synthetic",
            "LINGXI_INNERTEST_BINDING_PATH": "synthetic",
            "LINGXI_INNERTEST_SOCKET_PATH": str(Path(socket_dir.name) / "admin.sock"),
        }

    def _doubles(self):
        """返回 (监听器类, 消费者启动, 租约保活类) 三个替身；`build_loop` 其余全是真函数。"""
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch(
                "lingxi.adapters.innertest_binding.load_binding",
                return_value=SimpleNamespace(
                    binding_id="synthetic", peer_uid=1234, socket_gid=1234
                ),
            )
        )
        listener_cls = stack.enter_context(
            patch("lingxi.adapters.innertest_socket.InnertestSocketListener")
        )
        consumer_start = stack.enter_context(
            patch("lingxi.core.admin.followup_consumer.FollowupConsumer.start", autospec=True)
        )
        lease_keeper_cls = stack.enter_context(
            patch("lingxi.apps.scheduler.innertest.FollowupLeaseKeeper")
        )
        return listener_cls, consumer_start, lease_keeper_cls

    def _teardown_loop(self, loop) -> None:
        # 与 `resolve_start_system` 返回的收尾同一姿态：先停，再等开通执行器的线程池收工。
        loop.request_stop()
        join_onboarding_executors(loop.duties)

    def test_a_script_style_call_skips_the_whole_restricted_entry(self) -> None:
        """(a) ``False``：三者都不建、开通编排句柄仍恰有一个、留一行 info 日志。

        变异验红：把 `build_loop` 里「``register_innertest_entry`` 为假时跳过」删掉
        （恒调用 ``wire_innertest``），监听器类会被构造一次，第一条断言失败。
        """
        listener_cls, consumer_start, lease_keeper_cls = self._doubles()
        audit = RecordingAudit()

        with self.assertLogs("lingxi.apps.scheduler", level="INFO") as captured:
            loop = build_loop(
                SchedulerConfig.from_env(self._env()),
                roster_access_token=lambda: "employment-token",
                audit=audit,
                register_innertest_entry=False,
            )
        self.addCleanup(self._teardown_loop, loop)

        listener_cls.assert_not_called()
        consumer_start.assert_not_called()
        lease_keeper_cls.assert_not_called()
        runners = [
            duty.onboarding_runner
            for duty in loop.duties
            if getattr(duty, "onboarding_runner", None) is not None
        ]
        self.assertEqual(len(runners), 1, [duty.name for duty in loop.duties])
        self.assertTrue(callable(getattr(runners[0], "start_system", None)))
        self.assertTrue(
            any("按调用方要求不注册受限管理入口" in line for line in captured.output),
            captured.output,
        )

    def test_b_the_default_call_still_registers_the_restricted_entry(self) -> None:
        """(b) 不传参数＝常驻 ``main()`` 的形态：同一配置下三者都建、监听器真的起动。

        变异验红：把默认值改成 ``False``，监听器类一次都不会被构造，第一条断言失败。
        """
        listener_cls, consumer_start, lease_keeper_cls = self._doubles()
        config = SchedulerConfig.from_env(self._env())

        loop = build_loop(
            config, roster_access_token=lambda: "employment-token", audit=RecordingAudit()
        )
        self.addCleanup(self._teardown_loop, loop)

        listener_cls.assert_called_once()
        self.assertEqual(listener_cls.call_args.kwargs["path"], config.innertest_socket_path)
        listener_cls.return_value.start.assert_called_once_with()
        consumer_start.assert_called_once()
        lease_keeper_cls.assert_called_once()
        self.assertEqual(lease_keeper_cls.call_args.args[0], [consumer_start.call_args.args[0]])

    def test_c_resolve_start_system_passes_false_and_builds_no_listener(self) -> None:
        """(c) 脚本走真实装配：`build_loop` 收到 ``register_innertest_entry=False``，
        装出来的编排没有监听器、没有后台阶段消费者，开通入口句柄照常拿到。

        变异验红：把 `resolve_start_system` 里的 ``register_innertest_entry=False`` 删掉，
        第一条断言（收到的实参是 ``False``）失败。
        """
        import lingxi.apps.scheduler.assembly as assembly

        listener_cls, consumer_start, lease_keeper_cls = self._doubles()
        env = self._env()

        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(assembly, "build_loop", wraps=assembly.build_loop) as build_loop_spy,
        ):
            start_system, shutdown = TOOL.resolve_start_system(env["LINGXI_POSTGRES_DSN"])
        self.addCleanup(shutdown)

        self.assertIs(build_loop_spy.call_args.kwargs.get("register_innertest_entry"), False)
        listener_cls.assert_not_called()
        consumer_start.assert_not_called()
        lease_keeper_cls.assert_not_called()
        self.assertTrue(callable(start_system))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
