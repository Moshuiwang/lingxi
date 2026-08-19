"""#97 云盘九步探针（``scripts/probe_drive_folder_permissions.py``）的契约测试。

三轮独立审查逐条补测的产品合同级性质：

**第一轮**（2026-08-18）：dry-run 不发真实请求；硬停止真的终止且不可绕过；脱敏
生效；``only_owner_accessible`` 永远 ``unknown``。

**第二轮**（2026-08-18，"正门锁了、后门没锁"）：换状态目录救不了硬停止（全局
哨兵）；``--e`` 类缩写被拒绝；``msg``/目录名/路径不透传原文；步骤 5/6 的判定
真的在判它声称的东西；状态读—判—跑—存持有跨进程锁；清理阶段崩溃不丢停止状态。

**第三轮**（2026-08-19，"六条必修 + 三条加固"，最后一轮）：

- M1：``--all/--from-step`` 覆盖到步骤 8 且缺 ``--t-neg-01-result`` 时，在发出
  任何请求之前拒绝；用法错误分支也要保存状态并提示遗留对象；
- M2：步骤 7 要求步骤 3–6 已经真实跑完，不能绕开继承与授权验证直接写；
- M3：步骤 7 重复创建返回不同 token 时，那个 token 也要记进状态、纳入清理；
- M4：身份比对用 ``(member_type, member_id, perm)`` 三元组 + 可数结构（不用
  ``set``，防止吞掉重复条目）；
- M5：步骤 4/5 的期望集合与步骤 3 认可的基线对齐，不会因为创建者条目持续出现
  就必然在步骤 4 误停；
- M6：``collaborator_claim`` 只在步骤 3/4/5/6 真实跑完且非 dry-run、未halt 时
  才给 ``collaborator_list_matches_expected``，否则 ``not_established``；
- H1：全局哨兵的检查与写入都在它自己的跨进程锁内完成；
- H2：保存状态/写哨兵失败时不静默，打印明确提示并以非零码退出；
- H3：锁文件的 mkdir/open 失败时转成清晰错误，路径经脱敏。

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
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "probe_drive_folder_permissions.py"

FAKE_MEMBER_CROSS = "ou_cross_do_not_print_in_full_0001"
FAKE_MEMBER_SAME = "ou_same_do_not_print_in_full_0002"
FAKE_ROOT_TOKEN = "fldcntRootDoNotPrintInFull0000"
FAKE_FOLDER_TOKEN = "fldcntProbeFolderDoNotPrint0001"
FAKE_EXTRA_FOLDER_TOKEN = "fldcntExtraRepeatFolderDoNotPr2"
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


def _member(member_id: str, *, perm: str = "edit", member_type: str = "openid") -> dict[str, str]:
    """协作者字典的标准构造——默认 ``member_type="openid"``，与脚本自己发起
    授权时使用的类型一致（独立审查 M4：身份比对现在会检查 member_type，裸
    ``{"member_id":..., "perm":...}`` 字典不再能代表"这是我方正常授予的协作者"，
    必须显式带上类型）。"""

    return {"member_type": member_type, "member_id": member_id, "perm": perm}


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

    def state_file_path(self) -> Path:
        return self.state_dir / "drive_folder_probe_state.json"

    def read_state(self) -> dict[str, Any]:
        return json.loads(self.state_file_path().read_text(encoding="utf-8"))

    def granted_folder_transport(self) -> FakeTransport:
        """两步（1/2）之后的常见起点：根目录已读、目录已创建。"""

        return FakeTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
            }
        )

    def happy_path_transport(self) -> FakeTransport:
        """步骤 1–6 全部成功所需的完整假传输层：目录已建、无意外初始协作者、
        T-Cross-01/T-Same-01 依次授予成功、文档正确继承两人。步骤 7 起的额外
        调用会退回 FakeTransport 的默认成功值，按需在测试里覆盖。"""

        return FakeTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
                "list_collaborators": [
                    MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3：基线为空
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),  # 步骤 4 读回
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 5 读回
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 6 目录重新读回
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 6 文档协作者
                ],
            }
        )

    def run_steps_individually(self, transport: FakeTransport, upto: int, *, extra_argv: list[str] | None = None):
        """依次以独立调用跑完 1..upto（模拟真实分步操作），返回每步的
        ``(code, out, err)`` 列表。"""

        results = []
        for step in range(1, upto + 1):
            argv = ["--step", str(step), "--execute"]
            if step == 8 and extra_argv:
                argv += extra_argv
            results.append(self.run_main(argv, transport))
        return results


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
            MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3, "命中硬停止的退出码必须是 3，不是 0 也不是 1")
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 3)
        self.assertEqual(payload["halt_reason"], "unexpected_inherited_collaborator")
        ran_steps = [s["step"] for s in payload["steps"]]
        self.assertNotIn(4, ran_steps)
        self.assertNotIn(8, ran_steps)

    def test_step_4_halts_when_grant_result_does_not_match_expected(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3 初始读回：空，正常
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 4 授权后读回：仍然是空 —— 不符合预期
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 4)
        self.assertEqual(payload["halt_reason"], "unexpected_grant_result")

    def test_step_5_halts_when_grant_result_does_not_match_expected(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),
            # 步骤 5 授权后读回：还是只有 1 个协作者，T-Same-01 没有真的加进去
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 5)

    def test_step_6_halts_on_inheritance_mismatch(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),
            # 步骤 6 先重新读一次目录当前协作者（与上一行一致，两人）
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),
            # 文档协作者只继承了一个人，另一人没继承
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 6)
        self.assertEqual(payload["halt_reason"], "inheritance_mismatch")

    def test_step_6_halts_when_document_differs_from_a_freshly_reread_folder(self) -> None:
        """旧实现比较文档协作者与**本地内存**里以为授过的人，不是目录当前真实
        协作者。用一个按 obj_type 分流的假传输层：文档（``docx``）协作者读回
        固定为 ``{cross, same}``，与调用次数无关，精确地只测"步骤 6 到底比较
        的是内存还是目录当前状态"这一件事。"""

        class _FolderVsDocumentTransport(FakeTransport):
            def list_collaborators(self, *, token, obj_type):
                if obj_type == "docx":
                    self.calls.append(("list_collaborators", {"token": token, "obj_type": obj_type}))
                    return MODULE.ApiResult(
                        True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}
                    )
                return super().list_collaborators(token=token, obj_type=obj_type)

        transport = _FolderVsDocumentTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
                "list_collaborators": [
                    MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),  # 步骤 4
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 5
                    # 步骤 6 的"重新读回"：目录实际已经多了一个意外协作者
                    MODULE.ApiResult(
                        True,
                        0,
                        data={
                            "members": [
                                _member(FAKE_MEMBER_CROSS),
                                _member(FAKE_MEMBER_SAME),
                                _member("ou_extra_out_of_band"),
                            ]
                        },
                    ),
                ],
            }
        )
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 6)
        self.assertEqual(payload["halt_reason"], "inheritance_mismatch")

    def test_step_5_halts_when_an_unexpected_extra_collaborator_is_present(self) -> None:
        """旧实现只检查"目标成员出现一次 + 总数对上"。读回 [T-Same-01, 意外成员]
        时总数为 2、T-Same-01 也确实出现一次，旧逻辑会误判通过。"""

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),  # 步骤 4
            # 步骤 5 读回：T-Same-01 确实在，但另一个不是 T-Cross-01，是意外成员
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_SAME), _member("ou_unexpected_extra")]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 5)
        self.assertEqual(payload["halt_reason"], "unexpected_grant_result")

    def test_grant_halts_when_member_type_differs_even_if_id_and_perm_match(self) -> None:
        """独立审查 M4：身份比对必须含 member_type——`openid/X` 与 `email/X`
        不能被判相等，"协作者到底是谁"正是这次探针要回答的核心问题之一。"""

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
            # 步骤 4 读回：member_id/perm 都对，但类型是 email 不是 openid
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS, member_type="email")]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 4)
        self.assertEqual(payload["halt_reason"], "unexpected_grant_result")

    def test_grant_halts_when_a_duplicate_collaborator_entry_is_returned(self) -> None:
        """独立审查 M4：用 Counter 而不是 set——完全重复的条目不会被吞掉。"""

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
            # 步骤 4 读回：T-Cross-01 出现了两次（重复条目）
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_CROSS)]}),
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(payload["halted_at_step"], 4)
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
            MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})
        ]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        payload = json.loads(out)
        step_numbers = [s["step"] for s in payload["steps"]]
        self.assertIn(9, step_numbers, "命中硬停止后必须在同一次调用里自动进入清理，不能留给操作者手动补跑")
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
            MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})
        ]
        code, _, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)

        blocked_transport = FakeTransport()
        code, _, err = self.run_main(["--step", "4", "--execute"], blocked_transport)
        self.assertEqual(code, 3)
        self.assertEqual(blocked_transport.calls, [], "被拒绝的调用不应该碰传输层")
        self.assertIn("硬停止", err)

        blocked_transport_2 = FakeTransport()
        code2, _, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], blocked_transport_2)
        self.assertEqual(code2, 3)
        self.assertEqual(blocked_transport_2.calls, [])


# ---------------------------------------------------------------------------
# M5：步骤 4/5 的期望集合必须与步骤 3 认可的基线对齐
# ---------------------------------------------------------------------------


class BaselineAlignmentTests(ProbeTestCase):
    def test_step_4_succeeds_when_creator_entry_persists_in_every_read_back(self) -> None:
        """独立审查 M5 的核心场景：如果真实 API 在每次读回时都带着创建者条目
        （``perm=owner, member_type=app``），步骤 3 会把它认作基线通过；步骤 4
        的期望集合必须把这份基线也算进去，否则"多出一份创建者"会被误判成
        "多出一个意外协作者"，窗口在第 4 步必然报废。"""

        creator = {"member_type": "app", "member_id": "cli_bot_test_app", "perm": "owner"}
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [creator]}),  # 步骤 3：只有创建者，通过
            # 步骤 4 读回：创建者仍在，加上新授予的 T-Cross-01
            MODULE.ApiResult(True, 0, data={"members": [creator, _member(FAKE_MEMBER_CROSS)]}),
        ]
        results = self.run_steps_individually(transport, 4)
        codes = [code for code, _, _ in results]
        self.assertEqual(codes, [0, 0, 0, 0], f"步骤 1-4 都应该成功，实际退出码：{codes}")

    def test_step_5_also_accounts_for_the_baseline_and_the_earlier_grant(self) -> None:
        creator = {"member_type": "app", "member_id": "cli_bot_test_app", "perm": "owner"}
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [
            MODULE.ApiResult(True, 0, data={"members": [creator]}),  # 步骤 3
            MODULE.ApiResult(True, 0, data={"members": [creator, _member(FAKE_MEMBER_CROSS)]}),  # 步骤 4
            MODULE.ApiResult(True, 0, data={"members": [creator, _member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 5
        ]
        results = self.run_steps_individually(transport, 5)
        codes = [code for code, _, _ in results]
        self.assertEqual(codes, [0, 0, 0, 0, 0], f"步骤 1-5 都应该成功，实际退出码：{codes}")


# ---------------------------------------------------------------------------
# M1：步骤 8 缺观察结果时，--all/--from-step 必须在发出任何请求前拒绝；
# 用法错误分支必须保存状态并提示遗留对象。
# ---------------------------------------------------------------------------


class Step8UpfrontGuardTests(ProbeTestCase):
    def test_all_execute_without_t_neg_01_result_makes_no_real_calls_and_no_state_file(self) -> None:
        """独立审查 M1 的核心场景：文档给的示例就是 `--all --execute`（不带该
        参数）；如果没有这条前置检查，步骤 1-7 会先都真的跑完，才在步骤 8 因为
        用法错误退出——那时真实对象已经建好，却既不保存状态也不清理。"""

        transport = self.happy_path_transport()
        code, _, err = self.run_main(["--all", "--execute"], transport)
        self.assertEqual(code, 2)
        self.assertEqual(transport.calls, [], "缺 --t-neg-01-result 时不应该发起任何一次真实调用")
        self.assertFalse(self.state_file_path().exists(), "在发出任何请求之前拒绝，不应该创建状态文件")
        self.assertIn("t-neg-01-result", err)

    def test_from_step_covering_step_8_without_result_is_also_rejected_upfront(self) -> None:
        transport = self.happy_path_transport()
        code, _, _ = self.run_main(["--from-step", "5", "--execute"], transport)
        self.assertEqual(code, 2)
        self.assertEqual(transport.calls, [])
        self.assertFalse(self.state_file_path().exists())

    def test_step_range_not_reaching_step_8_is_unaffected(self) -> None:
        """前置检查只在请求真的覆盖到步骤 8 时才生效，不能误伤正常的分步执行。"""

        transport = self.granted_folder_transport()
        code, _, _ = self.run_main(["--step", "1", "--execute"], transport)
        self.assertEqual(code, 0)

    def test_dry_run_covering_step_8_does_not_need_the_result(self) -> None:
        code, _, _ = self.run_main(["--all"], FakeTransport())
        self.assertEqual(code, 0)


class UsageErrorPersistsStateTests(ProbeTestCase):
    def test_usage_error_after_real_objects_exist_saves_state_and_warns(self) -> None:
        transport = self.granted_folder_transport()
        self.run_main(["--step", "1", "--execute"], transport)
        self.run_main(["--step", "2", "--execute"], transport)
        # 独立审查 M2：步骤 3-6 还没做，步骤 7 会被拒绝——这是构造"已经有真实
        # 对象、随后命中用法错误"场景最直接的方式。
        code, _, err = self.run_main(["--step", "7", "--execute"], transport)
        self.assertEqual(code, 2)
        self.assertIn("step_7_requires_steps_3_through_6", err)
        self.assertIn("当前状态可能持有尚未清理的真实对象", err)
        self.assertIn("--cleanup-only", err)
        state = self.read_state()
        self.assertIsNotNone(state["probe_folder_token"], "用法错误分支也必须保存状态")

    def test_usage_error_with_nothing_created_yet_does_not_print_a_lingering_warning(self) -> None:
        code, _, err = self.run_main(["--step", "4", "--execute"], FakeTransport())
        self.assertEqual(code, 2)
        self.assertNotIn("当前状态可能持有尚未清理的真实对象", err)


# ---------------------------------------------------------------------------
# M2：步骤 7 必须要求步骤 3-6 已经真实跑完
# ---------------------------------------------------------------------------


class Step7PrerequisiteTests(ProbeTestCase):
    def test_step_1_then_2_then_7_is_rejected_as_a_usage_error_not_a_halt(self) -> None:
        """独立审查 M2 的核心场景：`step 1 → step 2 → step 7` 不应该能跳过
        继承与授权验证直接发起真实写。"""

        transport = self.granted_folder_transport()
        self.run_main(["--step", "1", "--execute"], transport)
        self.run_main(["--step", "2", "--execute"], transport)
        transport.calls.clear()  # 步骤 1/2 的调用是合法的真实调用；只关心步骤 7 这一次
        code, _, err = self.run_main(["--step", "7", "--execute"], transport)
        self.assertEqual(code, 2, "缺前置步骤是用法错误（退出码 2），不是硬停止（退出码 3）")
        self.assertIn("step_7_requires_steps_3_through_6", err)
        self.assertEqual(transport.calls, [], "被拒绝的这次调用不应该碰传输层——不能先写了才发现前置条件不满足")

    def test_step_7_succeeds_once_steps_3_through_6_are_actually_done(self) -> None:
        transport = self.happy_path_transport()
        results = self.run_steps_individually(transport, 6)
        self.assertEqual([code for code, _, _ in results], [0] * 6)
        code, _, _ = self.run_main(["--step", "7", "--execute"], transport)
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# M3：步骤 7 重复创建返回不同 token 时，那个 token 必须被记进状态并纳入清理
# ---------------------------------------------------------------------------


class Step7ExtraTokenCleanupTests(ProbeTestCase):
    def test_step_7_records_a_different_repeat_token_for_cleanup(self) -> None:
        transport = self.happy_path_transport()
        self.run_steps_individually(transport, 6)
        transport._responses["create_folder"] = [MODULE.ApiResult(True, 0, data={"token": FAKE_EXTRA_FOLDER_TOKEN})]
        code, _, _ = self.run_main(["--step", "7", "--execute"], transport)
        self.assertEqual(code, 0)
        state = self.read_state()
        self.assertIn(FAKE_EXTRA_FOLDER_TOKEN, state["extra_folder_tokens"])

    def test_cleanup_deletes_both_original_and_extra_folder_tokens(self) -> None:
        """独立审查 M3 的核心场景：重复创建产生的第二个目录不能变成清理不掉的
        遗留对象——步骤 9 必须把它也删掉。"""

        transport = self.happy_path_transport()
        self.run_steps_individually(transport, 6)
        transport._responses["create_folder"] = [MODULE.ApiResult(True, 0, data={"token": FAKE_EXTRA_FOLDER_TOKEN})]
        self.run_main(["--step", "7", "--execute"], transport)

        code, out, _ = self.run_main(["--cleanup-only", "--execute"], transport)
        self.assertEqual(code, 0)
        payload = json.loads(out)["steps"][0]
        self.assertEqual(payload["extra_folders_deleted_count"], 1)
        self.assertEqual(payload["extra_folders_pending_count"], 0)
        self.assertTrue(payload["complete"])
        deleted_tokens = {call_args["token"] for name, call_args in transport.calls if name == "delete_file"}
        self.assertIn(FAKE_FOLDER_TOKEN, deleted_tokens)
        self.assertIn(FAKE_EXTRA_FOLDER_TOKEN, deleted_tokens)
        state = self.read_state()
        self.assertEqual(state["extra_folder_tokens"], [])


# ---------------------------------------------------------------------------
# M6：collaborator_claim 只在真正验证过时才给出结论
# ---------------------------------------------------------------------------


class CollaboratorClaimHonestyTests(ProbeTestCase):
    def test_not_established_when_only_step_1_ran(self) -> None:
        """独立审查 M6 的核心场景：只跑了步骤 1 也不该宣称协作者列表符合预期
        ——此前的实现只要没 halt 就给这个结论，属于"用低一级证据宣称高一级
        完成"。"""

        transport = self.granted_folder_transport()
        code, out, _ = self.run_main(["--step", "1", "--execute"], transport)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["collaborator_claim"], "not_established")

    def test_not_established_in_dry_run(self) -> None:
        code, out, _ = self.run_main(["--all"], FakeTransport())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["collaborator_claim"], "not_established")

    def test_not_established_when_halted(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        code, out, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["collaborator_claim"], "not_established")

    def test_established_only_after_steps_3_through_6_all_complete(self) -> None:
        transport = self.happy_path_transport()
        results = self.run_steps_individually(transport, 6)
        codes_and_claims = [(code, json.loads(out)["collaborator_claim"]) for code, out, _ in results]
        # 前 5 步（1、2、3、4、5）都还没有把 3/4/5/6 全部跑完，必须是 not_established；
        # 第 6 步跑完之后（索引 5，对应"步骤 6"）才允许转正。
        for index, (code, claim) in enumerate(codes_and_claims[:5], start=1):
            self.assertEqual(code, 0, f"步骤 {index} 应该成功")
            self.assertEqual(claim, "not_established", f"步骤 {index} 完成后不该转正")
        final_code, final_claim = codes_and_claims[5]
        self.assertEqual(final_code, 0)
        self.assertEqual(final_claim, "collaborator_list_matches_expected")


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


class RedactionTests(ProbeTestCase):
    def test_full_member_ids_and_tokens_never_appear_in_stdout(self) -> None:
        transport = self.happy_path_transport()
        combined_out = ""
        for step in (1, 2, 3, 4):
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
        payload = self.read_state()
        self.assertIn("不得提交", payload["_notice"])


class MessageRedactionTests(ProbeTestCase):
    def test_a_leaking_platform_message_never_reaches_stdout_or_stderr(self) -> None:
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

        raw_name = self.read_state()["folder_name"]
        self.assertTrue(raw_name.startswith("lingxi-drive-probe-"))


class PathRedactionTests(ProbeTestCase):
    def test_state_corrupt_error_does_not_leak_the_full_path(self) -> None:
        state_path = self.state_file_path()
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


# ---------------------------------------------------------------------------
# P1-1 / H1：全局哨兵——路径固定、检查与写入都在自己的锁内完成
# ---------------------------------------------------------------------------


class NoStateFileOverrideTests(ProbeTestCase):
    def test_state_file_cli_flag_no_longer_exists(self) -> None:
        parser = MODULE.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        self.assertNotIn("--state-file", option_strings)


class GlobalHaltSentinelTests(ProbeTestCase):
    def test_switching_state_dir_after_a_halt_is_still_blocked(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        code, _, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)

        fresh_dir = self.state_dir / "brand-new-unrelated-directory"
        fresh_env = {**self.env, "LINGXI_DRIVE_PROBE_STATE_DIR": str(fresh_dir)}
        blocked_transport = FakeTransport()
        code2, _, err = self.run_main(
            ["--all", "--execute", "--t-neg-01-result", "denied"], blocked_transport, env=fresh_env
        )
        self.assertEqual(code2, 3, "换一个全新的状态目录不应该能绕过全局硬停止哨兵")
        self.assertEqual(blocked_transport.calls, [], "被挡住的调用不应该碰传输层")
        self.assertIn("全局硬停止哨兵", err)
        self.assertFalse((fresh_dir / "drive_folder_probe_state.json").exists())

    def test_sentinel_does_not_block_cleanup_only(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        code, _, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        code_cleanup, _, _ = self.run_main(["--cleanup-only", "--execute"], transport)
        self.assertEqual(code_cleanup, 0, "--cleanup-only 必须始终放行，否则命中硬停止后连清理都做不了")

    def test_sentinel_is_written_with_only_redacted_state_dir(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        payload = json.loads(self.halt_sentinel_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["step"], 3)
        self.assertNotIn(str(self.state_dir), payload["state_dir"])

    def test_step_9_is_treated_the_same_as_cleanup_only_by_both_gates(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        code, _, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        code_step9, _, _ = self.run_main(["--step", "9", "--execute"], transport)
        self.assertEqual(code_step9, 0, "--step 9 应该和 --cleanup-only 一样被放行")

    def test_sentinel_check_and_write_go_through_locked_sentinel(self) -> None:
        """独立审查 H1：哨兵检查与写入都要在 _locked_sentinel 内完成——用
        unittest.mock 断言那两次调用确实发生在锁的作用域里（通过 patch 记录
        调用顺序）。"""

        calls_order: list[str] = []
        original_locked_sentinel = MODULE._locked_sentinel

        import contextlib

        @contextlib.contextmanager
        def _tracking_locked_sentinel(path):
            calls_order.append("lock_enter")
            with original_locked_sentinel(path):
                yield
            calls_order.append("lock_exit")

        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        with patch.object(MODULE, "_locked_sentinel", _tracking_locked_sentinel):
            code, _, _ = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        # 至少两次进出：一次是启动时的哨兵检查，一次是命中硬停止后的哨兵写入。
        self.assertGreaterEqual(calls_order.count("lock_enter"), 2)
        self.assertEqual(calls_order.count("lock_enter"), calls_order.count("lock_exit"))


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
        self.assertNotIn("fake_secret", err.getvalue())

    def test_command_line_never_accepts_credentials(self) -> None:
        parser = MODULE.build_arg_parser()
        option_strings = {opt for action in parser._actions for opt in action.option_strings}
        for forbidden in ("--app-id", "--app-secret", "--token", "--secret"):
            self.assertNotIn(forbidden, option_strings)


class IdempotencyObservationTests(ProbeTestCase):
    def test_step_7_never_halts_regardless_of_repeat_outcome(self) -> None:
        transport = self.happy_path_transport()
        self.run_steps_individually(transport, 6)
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
        transport = self.happy_path_transport()
        self.run_steps_individually(transport, 6)
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
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),
        ]
        self.run_steps_individually(transport, 4)

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
            MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),
        ]
        self.run_steps_individually(transport, 4)

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

    def test_step_4_requires_step_3_specifically(self) -> None:
        transport = self.granted_folder_transport()
        self.run_main(["--step", "1", "--execute"], transport)
        self.run_main(["--step", "2", "--execute"], transport)
        code, _, err = self.run_main(["--step", "4", "--execute"], transport)
        self.assertEqual(code, 2)
        self.assertIn("step_4_requires_step_3", err)


# ---------------------------------------------------------------------------
# 命令行面：缩写关闭、穷尽枚举
# ---------------------------------------------------------------------------


class AbbreviationTests(ProbeTestCase):
    def test_execute_abbreviations_are_rejected_not_silently_expanded(self) -> None:
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


# ---------------------------------------------------------------------------
# 性质 4：only_owner_accessible 永远是 unknown
# ---------------------------------------------------------------------------


class OnlyOwnerAccessibleClaimTests(ProbeTestCase):
    def test_stays_unknown_even_when_every_step_succeeds(self) -> None:
        transport = self.happy_path_transport()
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
# 状态锁与哨兵锁：真实的跨进程互斥
# ---------------------------------------------------------------------------


class StateLockingTests(ProbeTestCase):
    def test_locked_state_provides_real_mutual_exclusion(self) -> None:
        import fcntl

        state_path = self.state_file_path()
        with MODULE._locked_state(state_path):
            lock_path = state_path.with_name(state_path.name + ".lock")
            with open(lock_path, "a+") as second_handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        lock_path = state_path.with_name(state_path.name + ".lock")
        with open(lock_path, "a+") as second_handle:
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_UN)

    def test_locked_state_releases_the_lock_even_on_early_return(self) -> None:
        import fcntl

        state_path = self.state_file_path()

        def _use_and_raise():
            with MODULE._locked_state(state_path):
                raise RuntimeError("simulated failure inside the locked section")

        with self.assertRaises(RuntimeError):
            _use_and_raise()

        lock_path = state_path.with_name(state_path.name + ".lock")
        with open(lock_path, "a+") as second_handle:
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_UN)

    def test_locked_sentinel_provides_real_mutual_exclusion(self) -> None:
        """独立审查 H1：全局哨兵有自己独立的锁，与状态目录的锁是两把不同的锁。"""

        import fcntl

        with MODULE._locked_sentinel(self.halt_sentinel_path):
            lock_path = self.halt_sentinel_path.with_name(self.halt_sentinel_path.name + ".lock")
            with open(lock_path, "a+") as second_handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        lock_path = self.halt_sentinel_path.with_name(self.halt_sentinel_path.name + ".lock")
        with open(lock_path, "a+") as second_handle:
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second_handle.fileno(), fcntl.LOCK_UN)


class HaltCleanupCrashTests(ProbeTestCase):
    def test_a_crash_during_auto_cleanup_still_persists_the_halt(self) -> None:
        class _CrashingTransport(FakeTransport):
            def remove_collaborator(self, **kwargs):
                raise ConnectionError("dns hiccup on stage")

        transport = _CrashingTransport(
            {
                "get_root_folder_meta": [MODULE.ApiResult(True, 0, data={"token": FAKE_ROOT_TOKEN})],
                "create_folder": [MODULE.ApiResult(True, 0, data={"token": FAKE_FOLDER_TOKEN})],
                "list_collaborators": [
                    MODULE.ApiResult(True, 0, data={"members": []}),  # 步骤 3
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),  # 步骤 4
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 5
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS), _member(FAKE_MEMBER_SAME)]}),  # 步骤 6 folder 重新读回
                    MODULE.ApiResult(True, 0, data={"members": [_member(FAKE_MEMBER_CROSS)]}),  # 步骤 6 文档协作者：不一致，触发硬停止
                ],
            }
        )
        code, out, err = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 3)
        self.assertIn("ConnectionError", err)

        state = self.read_state()
        self.assertEqual(state["halted_at_step"], 6, "清理阶段崩溃不能抹掉已经落盘的停止状态")
        self.assertEqual(state["halt_reason"], "inheritance_mismatch")


# ---------------------------------------------------------------------------
# H2：保存状态/写哨兵失败不静默
# ---------------------------------------------------------------------------


class PersistenceFailureTests(ProbeTestCase):
    def test_save_state_failure_is_reported_and_exits_nonzero(self) -> None:
        transport = self.granted_folder_transport()
        with patch.object(MODULE, "save_state", side_effect=OSError("disk full (simulated)")):
            code, _, err = self.run_main(["--step", "1", "--execute"], transport)
        self.assertEqual(code, 1)
        self.assertIn("停止事实未能持久化", err)

    def test_sentinel_write_failure_is_reported_and_exits_nonzero(self) -> None:
        transport = self.granted_folder_transport()
        transport._responses["list_collaborators"] = [MODULE.ApiResult(True, 0, data={"members": [_member("ou_surprise")]})]
        with patch.object(MODULE, "_write_halt_sentinel", side_effect=OSError("simulated")):
            code, _, err = self.run_main(["--from-step", "1", "--execute", "--t-neg-01-result", "denied"], transport)
        self.assertEqual(code, 1)
        self.assertIn("写入全局硬停止哨兵失败", err)
        self.assertIn("全局哨兵未能生效", err)


# ---------------------------------------------------------------------------
# H3：锁文件建立失败时不留裸 traceback，路径经脱敏
# ---------------------------------------------------------------------------


class LockAcquisitionFailureTests(ProbeTestCase):
    def test_lock_acquisition_failure_does_not_leak_the_full_path(self) -> None:
        blocked_path = self.state_dir / "blocked-not-a-directory"
        blocked_path.write_text("this is a file, not a directory", encoding="utf-8")
        bad_env = {**self.env, "LINGXI_DRIVE_PROBE_STATE_DIR": str(blocked_path)}
        code, _, err = self.run_main(["--step", "1", "--execute"], FakeTransport(), env=bad_env)
        self.assertEqual(code, 1)
        self.assertNotIn(str(blocked_path), err)
        self.assertIn("无法建立文件锁", err)


if __name__ == "__main__":
    unittest.main()
