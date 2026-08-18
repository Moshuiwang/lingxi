"""#97 云盘九步探针（``scripts/probe_drive_folder_permissions.py``）的契约测试。

用可注入的假传输层钉住产品合同级性质。第一轮（编排者 2026-08-18 追加要求）：

1. ``--dry-run``（默认）真的不发任何真实请求；
2. 硬停止条款真的会终止后续步骤，并且没有任何开关能绕过——命中后再次调用
   除纯清理请求以外的任何模式都会被拒绝；
3. 脱敏真的生效：完整 token/member id 不会出现在任何终端输出里；
4. ``only_owner_accessible`` 永远是 ``unknown``，不会被任何步骤成功改写成 ``ok``。

第二轮（2026-08-18 独立审查坐实"正门锁了、后门没锁"，逐条补测）：

5. 命中硬停止后，换一个 ``LINGXI_DRIVE_PROBE_STATE_DIR`` 也救不了——全局哨兵
   与状态目录无关（``GlobalHaltSentinelTests``）；
6. ``--e``/``--ex``/``--exec`` 一律被拒绝，不会被静默解释成 ``--execute``
   （``AbbreviationTests``）；
7. 飞书返回的自由文本 ``msg``、目录名都不透传原文（``MessageRedactionTests``），
   路径不泄露真实用户名/目录结构（``PathRedactionTests``）；
8. 步骤 5 逐个身份比对协作者集合、步骤 6 以目录**当前重新读回**的协作者为准
   （``HardStopTests`` 中新增的两个用例）；
9. 状态的读—判—跑—存持有真实的跨进程互斥锁（``StateLockingTests``），且清理
   阶段崩溃也不会抹掉已经落盘的停止状态（``HaltCleanupCrashTests``）。

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
        # 每个测试用例用自己独立的全局硬停止哨兵路径——真实调用固定落在
        # home 目录下（见脚本 _DEFAULT_HALT_SENTINEL_PATH），测试必须注入
        # 别的路径，否则不同测试会通过真实 home 目录互相污染。
        self.halt_sentinel_path = Path(self._tmp.name) / "_halt-sentinel" / "halted.json"

    def run_main(self, argv, transport, *, env=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = MODULE.main(
                argv,
                transport=transport,
                env=env if env is not None else self.env,
                halt_sentinel_path=self.halt_sentinel_path,
            )
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
            # 步骤 6 先重新读一次目录当前协作者（与上一行一致，两人）
            MODULE.ApiResult(
                True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]}
            ),
            # 文档协作者只继承了一个人，另一人没继承
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 6)
        self.assertEqual(payload["halt_reason"], "inheritance_mismatch")

    def test_step_6_halts_when_document_differs_from_a_freshly_reread_folder(self) -> None:
        """独立审查 P1-4：旧实现比较文档协作者与**本地内存**里以为授过的人，不是
        目录当前真实协作者。这里构造"文档协作者与内存记录一致，但目录实际（重新
        读回）已经多了一个意外协作者"的场景——旧实现会因为"和内存记录一致"而
        放行，新实现必须以目录当前的真实状态为准，命中硬停止。

        用一个按 obj_type 分流的假传输层：文档（``docx``）协作者读回固定为
        ``{cross, same}``，与调用次数无关——这样"步骤 6 是否真的多发起一次
        folder 读回"不会因为共享 FIFO 队列的位置整体前移而意外把结果读岔，
        才能精确地只测"步骤 6 到底比较的是内存还是目录当前状态"这一件事
        （第一版这个用例曾经因为队列位置巧合，在旧实现下也会"碰巧"命中硬停止，
        没有真正钉住这条性质，已改成这个更精确的写法）。
        """

        class _FolderVsDocumentTransport(FakeTransport):
            def list_collaborators(self, *, token, obj_type):
                if obj_type == "docx":
                    self.calls.append(("list_collaborators", {"token": token, "obj_type": obj_type}))
                    return MODULE.ApiResult(
                        True,
                        0,
                        data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]},
                    )
                return super().list_collaborators(token=token, obj_type=obj_type)

        transport = _FolderVsDocumentTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
                "list_collaborators": [
                    MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
                    MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),  # 步骤 4
                    MODULE.ApiResult(
                        True,
                        0,
                        data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]},
                    ),  # 步骤 5
                    # 步骤 6 的"重新读回"（只有真的发起这次调用才会被消费）：
                    # 目录实际已经多了一个意外协作者。
                    MODULE.ApiResult(
                        True,
                        0,
                        data={
                            "members": [
                                {"member_id": FAKE_MEMBER_CROSS, "perm": "edit"},
                                {"member_id": FAKE_MEMBER_SAME, "perm": "edit"},
                                {"member_id": "ou_extra_out_of_band", "perm": "edit"},
                            ]
                        },
                    ),
                ],
            }
        )
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 6)
        self.assertEqual(payload["halt_reason"], "inheritance_mismatch")

    def test_step_5_halts_when_an_unexpected_extra_collaborator_is_present(self) -> None:
        """独立审查 P1-4：旧实现只检查"目标成员出现一次 + 总数对上"。读回
        [T-Same-01, 意外成员] 时总数为 2、T-Same-01 也确实出现一次，旧逻辑会
        误判通过——而"有没有意外协作者"正是这次探针要回答的核心问题。新逻辑
        逐个身份比对期望集合，必须命中硬停止。"""

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),  # 步骤 4
            # 步骤 5 读回：T-Same-01 确实在，但另一个不是 T-Cross-01，是意外成员
            MODULE.ApiResult(
                True, 0, data={"members": [{"member_id": FAKE_MEMBER_SAME, "perm": "edit"}, {"member_id": "ou_unexpected_extra", "perm": "edit"}]}
            ),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 5)
        self.assertEqual(payload["halt_reason"], "unexpected_grant_result")

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
            # 步骤 6 先重新读一次目录（与上一行一致），再读文档协作者
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
            code = MODULE.main(
                ["--all"], transport=FakeTransport(), env={}, halt_sentinel_path=self.halt_sentinel_path
            )
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


# ---------------------------------------------------------------------------
# 2026-08-18 独立审查（P1-1..P1-4、P2）：上一版钉住了"正门"（硬停止会终止、
# CLI 参数名里没有 force/retry 之类的字样），但没钉住"后门"——换一个
# --state-file、用 --e 缩写、msg/目录名截断式假脱敏、以及两处判定实际没在判
# 它声称的东西。以下测试逐条钉住修复。
# ---------------------------------------------------------------------------


class AbbreviationTests(ProbeTestCase):
    def test_execute_abbreviations_are_rejected_not_silently_expanded(self) -> None:
        """独立审查 P1-2：argparse 默认允许前缀缩写，`--e` 会被解释成
        `--execute`。修复是 allow_abbrev=False；这里逐个枚举缩写形态确认它们
        全部被当成未知参数拒绝（SystemExit(2)），而不是被当成 --execute。"""

        transport = FakeTransport()
        for abbreviation in ("--e", "--ex", "--exec", "--execu"):
            with self.subTest(flag=abbreviation):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    with self.assertRaises(SystemExit) as raised:
                        MODULE.main(
                            ["--all", abbreviation],
                            transport=transport,
                            env=self.env,
                            halt_sentinel_path=self.halt_sentinel_path,
                        )
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(transport.calls, [], f"{abbreviation} 不应该发起任何一次传输层调用")


class MessageRedactionTests(ProbeTestCase):
    def test_a_leaking_platform_message_never_reaches_stdout_or_stderr(self) -> None:
        """独立审查 P1-3：旧的 redact_message 只是 text[:200] 截断，飞书返回的
        msg 偶尔回显请求参数（完整 token/成员标识/邮箱），落在前 200 字符内
        就原样进 stdout。这里构造一条这样的 msg，断言它完全不出现在任何输出里
        ——之前这条性质没有任何用例钉住。"""

        leaking_msg = f"invalid request for member {FAKE_MEMBER_CROSS} token=fld_shouldnotleak0001 contact ops@example.com"
        transport = self.granted_folder_transport()
        self.run_main(["--step", "1", "--execute"], transport)
        transport._responses["create_folder"] = [MODULE.ApiResult(False, 99991672, msg=leaking_msg)]
        code, out, err = self.run_main(["--step", "2", "--execute"], transport)
        self.assertEqual(code, 3)
        combined = out + err
        self.assertNotIn(leaking_msg, combined)
        self.assertNotIn(FAKE_MEMBER_CROSS, combined)
        self.assertNotIn("fld_shouldnotleak0001", combined)
        self.assertNotIn("ops@example.com", combined)

    def test_redact_message_never_returns_raw_text(self) -> None:
        raw = "token=fld_abc123 secret_value_here"
        result = MODULE.redact_message(raw)
        self.assertNotIn("fld_abc123", json.dumps(result))
        self.assertNotIn(raw, json.dumps(result))
        self.assertEqual(result["present"], True)
        self.assertEqual(result["length"], len(raw))
        self.assertTrue(result["looks_like_identifier"])

    def test_redact_message_handles_empty_text(self) -> None:
        self.assertEqual(MODULE.redact_message(""), {"present": False})
        self.assertEqual(MODULE.redact_message(None), {"present": False})

    def test_folder_name_never_appears_in_full_in_any_output(self) -> None:
        """独立审查 P1-3：步骤 2 的计划与成功摘要、步骤 7 的 dry-run 计划此前
        直接输出完整目录名——只跑默认 dry-run --all 就会泄露。"""

        code, out, _ = self.run_main(["--step", "2"], FakeTransport())
        self.assertEqual(code, 0)
        preview_name = json.loads(out)["steps"][0]["would_call"]["params"]["name"]
        self.assertIn("len=", preview_name)
        self.assertNotIn("lingxi-drive-probe-", preview_name)

        transport = self.granted_folder_transport()
        self.run_main(["--step", "1", "--execute"], transport)
        code, out, _ = self.run_main(["--step", "2", "--execute"], transport)
        self.assertEqual(code, 0)
        real_name_output = json.loads(out)["steps"][0]["folder_name"]
        self.assertIn("len=", real_name_output)
        self.assertNotIn("lingxi-drive-probe-", real_name_output)

        # 状态文件里仍然是完整明文——续跑需要真值，只是不出现在终端输出里。
        state_path = self.state_dir / "drive_folder_probe_state.json"
        raw_name = json.loads(state_path.read_text(encoding="utf-8"))["folder_name"]
        self.assertTrue(raw_name.startswith("lingxi-drive-probe-"))


class PathRedactionTests(ProbeTestCase):
    def test_state_corrupt_error_does_not_leak_the_full_path(self) -> None:
        state_path = self.state_dir / "drive_folder_probe_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{not valid json", encoding="utf-8")
        code, _, err = self.run_main(["--step", "1", "--execute"], FakeTransport())
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.state_dir), err)
        self.assertIn("drive_folder_probe_state.json", err)

    def test_redact_path_only_keeps_the_filename(self) -> None:
        redacted = MODULE.redact_path(Path("/home/wangzhipeng/secret/drive_folder_probe_state.json"))
        self.assertNotIn("wangzhipeng", redacted)
        self.assertNotIn("secret", redacted)
        self.assertTrue(redacted.endswith("drive_folder_probe_state.json"))


class NoStateFileOverrideTests(ProbeTestCase):
    def test_state_file_cli_flag_no_longer_exists(self) -> None:
        """独立审查 P1-1：``--state-file`` 本身就是上一版的绕过开关（换一个
        路径 = 换一次"干净"的探针，硬停止形同虚设）。修复是整体移除这个参数，
        状态文件名固定派生自 LINGXI_DRIVE_PROBE_STATE_DIR。"""

        parser = MODULE.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        self.assertNotIn("--state-file", option_strings)


class GlobalHaltSentinelTests(ProbeTestCase):
    def test_switching_state_dir_after_a_halt_is_still_blocked(self) -> None:
        """独立审查 P1-1 的核心场景：命中硬停止后，换一个全新、此前从未用过的
        LINGXI_DRIVE_PROBE_STATE_DIR，不应该能让脚本当成一次"干净"的探针继续
        发起真实写请求——全局哨兵与状态目录无关。"""

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        code, _, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)

        fresh_dir = self.state_dir / "brand-new-unrelated-directory"
        fresh_env = {**self.env, "LINGXI_DRIVE_PROBE_STATE_DIR": str(fresh_dir)}
        blocked_transport = FakeTransport()
        code2, _, err = self.run_main(["--all", "--execute"], blocked_transport, env=fresh_env)
        self.assertEqual(code2, 3, "换一个全新的状态目录不应该能绕过全局硬停止哨兵")
        self.assertEqual(blocked_transport.calls, [], "被挡住的调用不应该碰传输层")
        self.assertIn("全局硬停止哨兵", err)
        # 换新目录之后连状态文件都不应该被创建——请求在碰状态文件之前就被拒绝了。
        self.assertFalse((fresh_dir / "drive_folder_probe_state.json").exists())

    def test_sentinel_does_not_block_cleanup_only(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        code, _, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)

        code_cleanup, _, _ = self.run_main(["--cleanup-only", "--execute"], transport)
        self.assertEqual(code_cleanup, 0, "--cleanup-only 必须始终放行，否则命中硬停止后连清理都做不了")

    def test_step_9_is_treated_the_same_as_cleanup_only_by_both_gates(self) -> None:
        """2026-08-18 穷尽 CLI 面审查发现的一致性缺口：`--step 9 --execute` 和
        `--cleanup-only --execute` 产生完全相同的 steps=[9]，都只调用
        step_9_cleanup，但旧写法只按 `--cleanup-only` 这一个 flag 名字放行，会把
        等价的 `--step 9` 也一起挡住。不是安全漏洞（挡多了不会导致意外真实写），
        但会让操作者误以为清理都得靠一个不存在的开关。"""

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        code, _, _ = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)

        code_step9, _, _ = self.run_main(["--step", "9", "--execute"], transport)
        self.assertEqual(code_step9, 0, "--step 9 应该和 --cleanup-only 一样被放行")

    def test_sentinel_is_written_with_only_redacted_state_dir(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [{"member_id": "ou_surprise", "member_type": "openid", "perm": "edit"}]})
        ]
        self.run_main(["--from-step", "1", "--execute"], transport)
        payload = json.loads(self.halt_sentinel_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["step"], 3)
        self.assertNotIn(str(self.state_dir), payload["state_dir"])


class StateLockingTests(ProbeTestCase):
    def test_locked_state_provides_real_mutual_exclusion(self) -> None:
        """独立审查 P2-a：状态的读—判—跑—存必须在同一把跨进程锁内完成。这里
        直接验证 _locked_state 用的 flock 真的互斥——Linux 的 flock 语义是
        "同一文件的不同 open() 之间互斥"，所以可以在单个测试进程内用第二次
        open() 去验证持锁期间确实拿不到锁，释放后又能拿到。"""

        import fcntl

        state_path = self.state_dir / "drive_folder_probe_state.json"
        with MODULE._locked_state(state_path):
            lock_path = state_path.with_name(state_path.name + ".lock")
            with open(lock_path, "a+") as second_handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        # with 块退出后锁必须已经释放，现在应该能立刻非阻塞拿到。
        lock_path = state_path.with_name(state_path.name + ".lock")
        with open(lock_path, "a+") as second_handle:
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_UN)

    def test_locked_state_releases_the_lock_even_on_early_return(self) -> None:
        """main() 内部在锁的作用域中有多处 return；确认异常路径同样会释放锁。"""

        import fcntl

        state_path = self.state_dir / "drive_folder_probe_state.json"

        def _use_and_raise():
            with MODULE._locked_state(state_path):
                raise RuntimeError("simulated failure inside the locked section")

        with self.assertRaises(RuntimeError):
            _use_and_raise()

        lock_path = state_path.with_name(state_path.name + ".lock")
        with open(lock_path, "a+") as second_handle:
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_UN)


class HaltCleanupCrashTests(ProbeTestCase):
    def test_a_crash_during_auto_cleanup_still_persists_the_halt(self) -> None:
        """独立审查 P2-b：旧实现的自动清理在异常捕获块之外——清理请求抛网络
        异常会让代码在保存状态前退出，下次运行读到的状态就像这次硬停止从未
        发生过。修复是先落盘停止状态，再尝试清理，清理本身也包一层
        try/except。这里让 remove_collaborator 抛异常，断言退出码仍是 3、
        stderr 提到异常类型，且**磁盘上的状态文件**确实记录了 halted_at_step。"""

        class _CrashingTransport(FakeTransport):
            def remove_collaborator(self, **kwargs):
                raise ConnectionError("dns hiccup on stage")

        transport = _CrashingTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
                "list_collaborators": [
                    MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
                    MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),  # 步骤 4
                    MODULE.ApiResult(
                        True,
                        0,
                        data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]},
                    ),  # 步骤 5
                    MODULE.ApiResult(
                        True,
                        0,
                        data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}, {"member_id": FAKE_MEMBER_SAME, "perm": "edit"}]},
                    ),  # 步骤 6 folder 重新读回
                    MODULE.ApiResult(True, 0, data={"members": [{"member_id": FAKE_MEMBER_CROSS, "perm": "edit"}]}),  # 步骤 6 文档协作者：不一致，触发硬停止
                ],
            }
        )
        code, out, err = self.run_main(["--from-step", "1", "--execute"], transport)
        self.assertEqual(code, 3)
        self.assertIn("ConnectionError", err)

        state_path = self.state_dir / "drive_folder_probe_state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["halted_at_step"], 6, "清理阶段崩溃不能抹掉已经落盘的停止状态")
        self.assertEqual(payload["halt_reason"], "inheritance_mismatch")


class CliSurfaceAuditTests(ProbeTestCase):
    def test_every_defined_option_is_accounted_for(self) -> None:
        """穷尽 CLI 面：逐个列出全部已定义参数，防止未来悄悄加回一个绕过开关
        而没人注意到——新增参数必须显式加进这个白名单，逼一次人工复核。"""

        parser = MODULE.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        expected = {
            "-h",
            "--help",
            "--step",
            "--from-step",
            "--all",
            "--cleanup-only",
            "--execute",
            "--t-neg-01-result",
        }
        self.assertEqual(option_strings, expected)
        self.assertTrue(parser.allow_abbrev is False, "allow_abbrev 必须显式关闭")


if __name__ == "__main__":
    unittest.main()
