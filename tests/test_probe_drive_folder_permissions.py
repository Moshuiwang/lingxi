"""#97 云盘九步探针（``scripts/probe_drive_folder_permissions.py``）的契约测试。

用可注入的假传输层钉住四条产品合同级性质（编排者 2026-08-18 追加要求）：

1. ``--dry-run``（默认）真的不发任何真实请求；
2. 硬停止条款真的会终止后续步骤，并且没有任何开关能绕过——命中后再次调用
   除 ``--cleanup-only`` 以外的任何模式都会被拒绝；
3. 脱敏真的生效：完整 token/member id 不会出现在任何终端输出里；
4. ``only_owner_accessible`` 永远是 ``unknown``，不会被任何步骤成功改写成 ``ok``。

真实 HTTP 请求是否与飞书线上契约完全一致——不测，也测不了（见脚本
``LarkDriveTransport`` docstring：L1，未真实调用验证），留给窗口内第一次真实执行。

加载方式照抄既有先例 ``tests/test_probe_message_reactions_script.py``：``scripts/``
不是 ``lingxi`` 包的一部分，用 ``importlib`` 按文件路径直接加载。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

SCRIPT = Path(__file__).parents[1] / "scripts" / "probe_drive_folder_permissions.py"

FAKE_MEMBER_CROSS = "ou_cross_do_not_print_in_full_0001"
FAKE_MEMBER_SAME = "ou_same_do_not_print_in_full_0002"
FAKE_ROOT_TOKEN = "fldcntRootDoNotPrintInFull0000"
FAKE_FOLDER_TOKEN = "fldcntProbeFolderDoNotPrint0001"
FAKE_DOC_TOKEN = "doxcnProbeDocumentDoNotPrint0001"


def _load_script():
    spec = importlib.util.spec_from_file_location("probe_drive_folder_permissions_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 模块用了 @dataclass(frozen=True)；dataclasses 内部按 cls.__module__ 去
    # sys.modules 查找命名空间，exec 之前不注册这个名字会在 Python 3.12 上直接
    # AttributeError（'NoneType' object has no attribute '__dict__'）。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


MODULE = _load_script()


class FakeTransport:
    """按方法名分队列返回预置结果；不配置的调用默认成功且返回空 data。"""

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = {k: list(v) for k, v in (responses or {}).items()}

    def _result(self, name: str, *, default: dict[str, Any] | None = None):
        queue = self._responses.get(name)
        if queue:
            return queue.pop(0)
        return MODULE.ApiResult(ok=True, code=0, msg="", data=default or {})

    def get_root_folder_meta(self):
        self.calls.append(("get_root_folder_meta", {}))
        return self._result("get_root_folder_meta", default={"token": FAKE_ROOT_TOKEN})

    def create_folder(self, *, name, parent_token):
        self.calls.append(("create_folder", {"name": name, "parent_token": parent_token}))
        return self._result("create_folder", default={"token": FAKE_FOLDER_TOKEN})

    def list_collaborators(self, *, token, obj_type):
        self.calls.append(("list_collaborators", {"token": token, "obj_type": obj_type}))
        return self._result("list_collaborators", default={"members": []})

    def add_collaborator(self, *, token, obj_type, member_type, member_id, perm, notify):
        self.calls.append(
            (
                "add_collaborator",
                {"token": token, "obj_type": obj_type, "member_type": member_type, "member_id": member_id, "perm": perm, "notify": notify},
            )
        )
        return self._result("add_collaborator", default={})

    def remove_collaborator(self, *, token, obj_type, member_type, member_id):
        self.calls.append(("remove_collaborator", {"token": token, "member_id": member_id}))
        return self._result("remove_collaborator", default={})

    def create_document(self, *, folder_token):
        self.calls.append(("create_document", {"folder_token": folder_token}))
        return self._result("create_document", default={"token": FAKE_DOC_TOKEN})

    def delete_file(self, *, token, obj_type):
        self.calls.append(("delete_file", {"token": token, "obj_type": obj_type}))
        return self._result("delete_file", default={})


FULL_ENV = {
    "LINGXI_DRIVE_PROBE_APP_ID": "cli_fake_app",
    "LINGXI_DRIVE_PROBE_APP_SECRET": "fake_secret_do_not_print",
    "LINGXI_DRIVE_PROBE_MEMBER_CROSS": FAKE_MEMBER_CROSS,
    "LINGXI_DRIVE_PROBE_MEMBER_SAME": FAKE_MEMBER_SAME,
    "LINGXI_DRIVE_PROBE_STATE_DIR": "",  # 每个测试用例覆盖成自己的临时目录
}


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.env = {**FULL_ENV, "LINGXI_DRIVE_PROBE_STATE_DIR": str(self.state_dir)}

    def run_main(self, argv, transport):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MODULE.main(argv, transport=transport, env=self.env)
        return code, out.getvalue(), err.getvalue()

    def granted_folder_transport(self) -> FakeTransport:
        """三步（1/2/3）之后的常见起点：目录已创建、初始协作者为空。"""

        return FakeTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
            }
        )


# ---------------------------------------------------------------------------
# 性质 1：--dry-run（默认）真的不发任何真实请求
# ---------------------------------------------------------------------------


class DryRunMakesNoTransportCallsTests(ProbeTestCase):
    def test_dry_run_never_calls_transport_across_all_nine_steps(self) -> None:
        transport = FakeTransport()
        code, out, _ = self.run_main(["--all"], transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.calls, [], "dry-run 不应该发起任何一次传输层调用")
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(len(payload["steps"]), 9)
        self.assertTrue(all(s.get("dry_run") for s in payload["steps"]))

    def test_missing_execute_flag_is_the_default_not_an_opt_in(self) -> None:
        """`--execute` 不传就是 dry-run；不存在"默认真实执行"的反向开关。"""

        transport = FakeTransport()
        code, _, _ = self.run_main(["--step", "2"], transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.calls, [])


# ---------------------------------------------------------------------------
# 性质 2：硬停止条款真的会终止，且没有绕过开关
# ---------------------------------------------------------------------------


class HardStopTests(ProbeTestCase):
    def test_step_3_halts_on_unexpected_inherited_collaborator(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3, "命中硬停止的退出码必须是 3，不是 0 也不是 1")
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 3)
        self.assertEqual(payload["halt_reason"], "unexpected_inherited_collaborator")
        # 步骤 4-8 不应该出现在结果里——真的被终止了，不是继续跑完只是标了个失败。
        ran_steps = [s["step"] for s in payload["steps"]]
        self.assertNotIn(4, ran_steps)
        self.assertNotIn(8, ran_steps)

    def test_step_4_halts_when_grant_result_does_not_match_expected(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3 初始读回：空，正常
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 4 授权后读回：仍然是空 —— 不符合预期
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 4)
        self.assertEqual(payload["halt_reason"], "unexpected_grant_result")

    def test_step_5_halts_when_grant_result_does_not_match_expected(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
            # 步骤 5 授权后读回：还是只有 1 个协作者，T-Same-01 没有真的加进去
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 5)

    def test_step_6_halts_on_inheritance_mismatch(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
            MODULE.ApiResult(
                True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]}
            ),
            # 步骤 6：文档协作者只继承了一个人，另一人没继承
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 6)
        self.assertEqual(payload["halt_reason"], "inheritance_mismatch")

    def test_step_8_halts_when_t_neg_01_can_unexpectedly_access(self) -> None:
        transport = FakeTransport()
        code, out, _ = self.run_main(["--step", "8", "--execute", "--t-neg-01-result", "allowed"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 8)
        self.assertEqual(payload["halt_reason"], "unauthorized_access_succeeded")

    def test_step_8_does_not_halt_when_access_is_denied(self) -> None:
        transport = FakeTransport()
        code, _, _ = self.run_main(["--step", "8", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 0)

    def test_halt_automatically_triggers_cleanup_in_the_same_invocation(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        step_numbers = [s["step"] for s in payload["steps"]]
        self.assertIn(9, step_numbers, "命中硬停止后必须在同一次调用里自动进入清理，不能留给操作者手动补跑")
        # 清理阶段确实调用了 delete_file（目录本身要被清掉）
        self.assertTrue(any(name == "delete_file" for name, _ in transport.calls))

    def test_no_flag_exists_to_bypass_or_retry_past_a_halt(self) -> None:
        """结构性钉住"没有扩大 scope 后重试 / 改租户设置后继续"的开关——枚举全部
        CLI 参数名，任何一个都不能带有 force/retry/override/bypass/expand 语义。"""

        parser = MODULE.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        forbidden_fragments = ("force", "retry", "override", "bypass", "expand", "ignore-halt", "skip")
        for option in option_strings:
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, option.lower(), f"{option} 看起来像一个绕过硬停止的开关")

    def test_after_a_halt_only_cleanup_only_is_allowed_to_run_again(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        code, _, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)

        # 命中硬停止之后，任何非 --cleanup-only 的调用都必须被状态文件直接拒绝，
        # 连传输层都不会碰——这是"没有绕过开关"在运行时层面的落地，不只是没有 CLI flag。
        blocked_transport = FakeTransport()
        code, _, err = self.run_main(["--step", "4", "--execute"], blocked_transport)
        self.assertEqual(code, 3)
        self.assertEqual(blocked_transport.calls, [], "被拒绝的调用不应该碰传输层")
        self.assertIn("硬停止", err)

        # --from-step 同样被拒绝（上面只验证了 --step 4），且同样不碰传输层
        blocked_transport_2 = FakeTransport()
        code2, _, _ = self.run_main(["--from-step", "1", "--execute"], blocked_transport_2)
        self.assertEqual(code2, 3)
        self.assertEqual(blocked_transport_2.calls, [])


# ---------------------------------------------------------------------------
# 性质 3：脱敏真的生效
# ---------------------------------------------------------------------------


class RedactionTests(ProbeTestCase):
    def test_full_member_ids_and_tokens_never_appear_in_stdout(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
        ]
        _, first_out, _ = self.run_main(["--step", "1", "--execute"], transport)
        # 分步真实跑 1-4，逐次校验输出里不出现完整标识
        combined_out = first_out
        for step in (2, 3, 4):
            transport.calls.clear()
            _, step_out, _ = self.run_main(["--step", str(step), "--execute"], transport)
            combined_out += step_out

        for secret in (FAKE_MEMBER_CROSS, FAKE_MEMBER_SAME, FAKE_ROOT_TOKEN, FAKE_FOLDER_TOKEN, "fake_secret_do_not_print"):
            self.assertNotIn(secret, combined_out, f"{secret!r} 完整出现在了 stdout 里")

    def test_redact_id_never_returns_the_full_value(self) -> None:
        for value in (FAKE_MEMBER_CROSS, FAKE_ROOT_TOKEN, "short"):
            redacted = MODULE.redact_id(value)
            self.assertNotEqual(redacted, value)
            self.assertIn(f"len={len(value)}", redacted)

    def test_state_file_is_the_only_place_holding_the_real_tokens(self) -> None:
        """状态文件本身允许含真实标识（续跑要用），但要有一目了然的禁止外传提示。"""

        transport = self.granted_folder_transport()
        self.run_main(["--step", "1", "--execute"], transport)
        self.run_main(["--step", "2", "--execute"], transport)
        state_path = self.state_dir / "drive_folder_probe_state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("不得提交", payload["_notice"])


# ---------------------------------------------------------------------------
# 性质 4：only_owner_accessible 永远是 unknown
# ---------------------------------------------------------------------------


class OnlyOwnerAccessibleClaimTests(ProbeTestCase):
    def test_stays_unknown_even_when_every_step_succeeds(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
            MODULE.ApiResult(
                True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]}
            ),
            MODULE.ApiResult(
                True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]}
            ),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["collaborator_claim"], "collaborator_list_matches_expected")
        self.assertEqual(payload["only_owner_accessible"], "unknown", "全部步骤成功也不能把这个字段写成 ok")

    def test_stays_unknown_in_dry_run_too(self) -> None:
        code, out, _ = self.run_main(["--all"], FakeTransport())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["only_owner_accessible"], "unknown")

    def test_the_string_ok_is_never_assigned_to_the_claim(self) -> None:
        self.assertNotEqual(MODULE.ONLY_OWNER_ACCESSIBLE_CLAIM, "ok")
        self.assertEqual(MODULE.ONLY_OWNER_ACCESSIBLE_CLAIM, MODULE.UNKNOWN)


# ---------------------------------------------------------------------------
# 补充性质：环境变量失败关闭、幂等观察不判定成败、清理可重入
# ---------------------------------------------------------------------------


class MissingEnvironmentTests(ProbeTestCase):
    def test_missing_env_vars_fail_closed_and_report_only_the_names(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MODULE.main(["--all"], transport=FakeTransport(), env={})
        self.assertEqual(code, 2)
        self.assertIn("LINGXI_DRIVE_PROBE_APP_ID", err.getvalue())
        self.assertIn("LINGXI_DRIVE_PROBE_STATE_DIR", err.getvalue())
        # 只报变量名，不应该出现任何看起来像值的东西
        self.assertNotIn("fake_secret", err.getvalue())

    def test_command_line_never_accepts_credentials(self) -> None:
        parser = MODULE.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        for forbidden in ("--app-id", "--app-secret", "--token", "--secret"):
            self.assertNotIn(forbidden, option_strings)


class IdempotencyObservationTests(ProbeTestCase):
    def test_step_7_never_halts_regardless_of_repeat_outcome(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": []})]
        self.run_main(["--step", "1", "--execute"], transport)
        self.run_main(["--step", "2", "--execute"], transport)
        transport._responses["create_folder"] = [MODULE.ApiResult(False, 1062507, msg="folder already exists")]
        transport._responses["add_collaborator"] = [MODULE.ApiResult(False, 99991672, msg="duplicate member")]
        code, out, _ = self.run_main(["--step", "7", "--execute"], transport)
        self.assertEqual(code, 0, "重复调用无论成败都不算失败")
        payload = json.loads(out)
        step7 = payload["steps"][0]
        self.assertFalse(step7["repeat_create_ok"])
        self.assertFalse(step7["repeat_add_ok"])
        self.assertIs(step7["claims_idempotency_guarantee"], False)

    def test_step_7_records_success_without_claiming_a_guarantee(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": []})]
        self.run_main(["--step", "1", "--execute"], transport)
        self.run_main(["--step", "2", "--execute"], transport)
        code, out, _ = self.run_main(["--step", "7", "--execute"], transport)
        self.assertEqual(code, 0)
        step7 = json.loads(out)["steps"][0]
        self.assertTrue(step7["repeat_create_ok"])
        self.assertIs(
            step7["claims_idempotency_guarantee"],
            False,
            "哪怕这次重复调用『看起来』很顺利，也不能声称接口具有幂等保证",
        )


class CleanupReentrancyTests(ProbeTestCase):
    def test_cleanup_only_with_nothing_to_clean_is_a_successful_noop(self) -> None:
        code, out, _ = self.run_main(["--cleanup-only", "--execute"], FakeTransport())
        self.assertEqual(code, 0)
        payload = json.loads(out)["steps"][0]
        self.assertEqual(payload["collaborators_removed"], [])
        self.assertTrue(payload["complete"])

    def test_cleanup_only_is_safe_to_run_twice(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
        ]
        for step in (1, 2, 3, 4):
            self.run_main(["--step", str(step), "--execute"], transport)

        first_code, first_out, _ = self.run_main(["--cleanup-only", "--execute"], transport)
        self.assertEqual(first_code, 0)
        self.assertTrue(json.loads(first_out)["steps"][0]["complete"])

        second_transport = FakeTransport()
        second_code, second_out, _ = self.run_main(["--cleanup-only", "--execute"], second_transport)
        self.assertEqual(second_code, 0)
        self.assertEqual(second_transport.calls, [], "已经清空的状态不应该再触发任何真实调用")

    def test_incomplete_cleanup_is_reported_honestly_not_as_success(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
        ]
        for step in (1, 2, 3, 4):
            self.run_main(["--step", str(step), "--execute"], transport)

        transport._responses["remove_collaborator"] = [MODULE.ApiResult(False, 99991672, msg="remove failed")]
        code, out, _ = self.run_main(["--cleanup-only", "--execute"], transport)
        self.assertEqual(code, 3, "清理不完整不能返回成功退出码")
        payload = json.loads(out)["steps"][0]
        self.assertFalse(payload["complete"])
        self.assertIn("cross_tenant_positive", payload["collaborators_removal_pending"])


class StepOrderingTests(ProbeTestCase):
    def test_running_a_later_step_before_its_prerequisite_is_a_usage_error_not_a_halt(self) -> None:
        code, _, err = self.run_main(["--step", "4", "--execute"], FakeTransport())
        self.assertEqual(code, 2, "顺序错误是用法错误（退出码 2），不是探针硬停止（退出码 3）")
        self.assertIn("step_4_requires_step_2", err)


if __name__ == "__main__":
    unittest.main()
