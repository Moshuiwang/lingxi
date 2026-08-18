#!/usr/bin/env python3
"""#97 专属云盘目录九步探针（受控验证脚本，只在 biai-stage/tz 等受控环境运行，
不进生产镜像）。

**适用范围**：只验证在职用户的跨租户访问、协作者边界、继承与未授权访问。
**不执行离职交接或所有权转移**——飞书官方规范已确认「机器人持有目录随关联租户
员工离职自动交接」不成立，产品负责人已于 2026-08-14（#140）决定离职场景不做自动
迁移，本脚本不提供任何 ``transfer_owner`` 或离职模拟能力。

对应操作说明见
``docs/参考证据/专属云盘目录九步探针执行卡.md``——谁操作、预期结果、失败怎么办、
判定口径、硬停止条款与清理回读要求，本文件是那张卡片的**唯一可执行入口**：卡片
把「执行 Owner」定义为「编排者或其指定的受控脚本」，指的就是本文件。

**默认 dry-run**：不传 ``--execute`` 时，任何步骤都只打印将要发起的调用与参数
形状（且参数已脱敏），不发出任何真实请求——用 ``FakeTransport``/真实
``LarkDriveTransport`` 的契约测试见 ``tests/test_probe_drive_folder_permissions.py``
的 ``test_dry_run_never_calls_transport``。

**硬停止是代码，不是约定**：步骤 3 出现预期外协作者、步骤 4/5 协作者授予结果不
符合预期、步骤 6 文档继承与目录协作者不一致、步骤 8 ``T-Neg-01`` 意外可以访问，
任一命中都会抛出 :class:`ProbeHalt`，终止后续步骤并自动进入清理（步骤 9）。
**这里没有、也不会加「扩大 scope 后重试」或「修改租户设置后继续」的开关**——
:class:`DriveTransport` 协议里根本不存在任何租户设置写接口，触发硬停止后，
再次运行本脚本（``--cleanup-only`` 以外的任何调用）会被状态文件里记录的
``halted_at_step`` 直接拒绝，见 ``main()``。

**凭据只从环境变量读取，不接受命令行参数**（命令行参数会进 shell 历史与进程
列表）：

.. code-block:: bash

    export LINGXI_DRIVE_PROBE_APP_ID=...
    export LINGXI_DRIVE_PROBE_APP_SECRET=...
    export LINGXI_DRIVE_PROBE_MEMBER_CROSS=...   # T-Cross-01 的协作者标识
    export LINGXI_DRIVE_PROBE_MEMBER_SAME=...    # T-Same-01 的协作者标识
    export LINGXI_DRIVE_PROBE_STATE_DIR=...      # 独立临时状态/日志目录，谁建谁清

    PYTHONPATH=src python3 scripts/probe_drive_folder_permissions.py --all --execute

任一变量缺失，脚本在做任何事之前失败关闭，只报告缺失的变量名（退出码 2）。

**状态文件**（``$LINGXI_DRIVE_PROBE_STATE_DIR/drive_folder_probe_state.json``）
让九步可以逐步执行、可以在窗口中断后从任意步续跑——它保存本次探针创建的真实
对象标识（目录/文档 token、已授予的协作者），**不脱敏**，因此只能活在独立临时
目录里，不得提交、粘贴进 Issue、日志或聊天。终端输出与状态文件是两回事：终端
输出（``build_report``）永远脱敏。

**协作者列表符合预期 ≠ 仅本人可访问**：文件夹链接分享范围没有已知可读写 API，
程序永远无法自证「除协作者外没有别的入口能打开」。``only_owner_accessible`` 字段
是硬编码常量 :data:`ONLY_OWNER_ACCESSIBLE_CLAIM`，不是任何步骤判断出来的结果，
任何步骤成功都不会、也不能把它改写成别的值。

**退出码**：0 全部请求步骤正常完成（含 dry-run 预览、cleanup-only 完全清空）；
1 脚本内部异常（环境/网络问题，不是探针结论）；2 用法或配置错误（缺环境变量、
步骤顺序不对、命令行参数冲突）；3 命中硬停止条款（已自动清理，属预期内的
「停止并记录」，不是崩溃），**或**清理步骤本身没有完全成功（例如某个协作者
删不掉）——两种情况都必须看 JSON 输出里的 ``halted_at_step``/``complete`` 字段
分辨具体原因，退出码只负责告诉调用方"不能当作已经收尾，需要看详情"。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# 传输层协议：九步只需要这七个动作。真实实现 LarkDriveTransport 在文件末尾；
# 契约测试全部注入假传输层，不依赖网络或飞书 SDK。协议里**没有**、也不会加任何
# 租户设置读写方法——不给「改设置后继续」留代码空间。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiResult:
    """飞书 API 单次调用结果的最小抽象，屏蔽 REST 与真实 SDK 的差异。"""

    ok: bool
    code: int
    msg: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class DriveTransport(Protocol):
    def get_root_folder_meta(self) -> ApiResult: ...

    def create_folder(self, *, name: str, parent_token: str) -> ApiResult: ...

    def list_collaborators(self, *, token: str, obj_type: str) -> ApiResult: ...

    def add_collaborator(
        self,
        *,
        token: str,
        obj_type: str,
        member_type: str,
        member_id: str,
        perm: str,
        notify: bool,
    ) -> ApiResult: ...

    def remove_collaborator(
        self, *, token: str, obj_type: str, member_type: str, member_id: str
    ) -> ApiResult: ...

    def create_document(self, *, folder_token: str) -> ApiResult: ...

    def delete_file(self, *, token: str, obj_type: str) -> ApiResult: ...


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def redact_id(value: str | None, *, keep: int = 4) -> str:
    """截断标识：只保留前 ``keep`` 位与长度，永不回显完整 token/用户标识。"""

    if not value:
        return "(none)"
    text = str(value)
    if len(text) <= keep:
        return f"{text[0]}…(len={len(text)})"
    return f"{text[:keep]}…(len={len(text)})"


def redact_message(text: str | None, *, limit: int = 200) -> str:
    """飞书返回的 ``msg`` 字段偶尔会回显请求参数；截断为保守长度，不做语义解析。"""

    if not text:
        return ""
    return text[:limit]


UNKNOWN = "unknown"

ONLY_OWNER_ACCESSIBLE_CLAIM = UNKNOWN
"""硬编码常量，**不是任何函数的返回值**。

文件夹链接分享范围没有已知可读写 API（2026-08-09 规范研究已确认，见
``docs/参考证据/专属云盘目录九步探针执行卡.md``）。因此无论第 3~6 步的协作者
读回多么符合预期，程序都无法自证"除协作者外没有别的入口能打开"——这个结论
永远只能是 ``unknown``，绝不允许被任何代码路径改写成 ``"ok"``。写成模块级常量
而不是某个判定函数的返回值，是为了让"以后有人顺手加一条分支让它变成 ok"这件事
在代码结构上就做不到：:func:`build_report` 只能引用这个名字，没有计算它的逻辑。
"""


# ---------------------------------------------------------------------------
# 配置：只从环境变量读取
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = (
    "LINGXI_DRIVE_PROBE_APP_ID",
    "LINGXI_DRIVE_PROBE_APP_SECRET",
    "LINGXI_DRIVE_PROBE_MEMBER_CROSS",
    "LINGXI_DRIVE_PROBE_MEMBER_SAME",
    "LINGXI_DRIVE_PROBE_STATE_DIR",
)


class MissingEnvironmentError(RuntimeError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("missing_environment_variables:" + ",".join(missing))


class UsageError(RuntimeError):
    """步骤顺序、命令行参数或状态文件不满足前提；不是探针结论，是操作错误。"""


class StateCorruptError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeConfig:
    app_id: str
    app_secret: str
    member_cross: str
    member_same: str
    state_dir: Path


def load_config(env: dict[str, str]) -> ProbeConfig:
    """任一必需变量缺失即失败关闭，只报告变量名，不读取也不回显任何值。"""

    missing = [name for name in REQUIRED_ENV_VARS if not (env.get(name) or "").strip()]
    if missing:
        raise MissingEnvironmentError(missing)
    return ProbeConfig(
        app_id=env["LINGXI_DRIVE_PROBE_APP_ID"].strip(),
        app_secret=env["LINGXI_DRIVE_PROBE_APP_SECRET"].strip(),
        member_cross=env["LINGXI_DRIVE_PROBE_MEMBER_CROSS"].strip(),
        member_same=env["LINGXI_DRIVE_PROBE_MEMBER_SAME"].strip(),
        state_dir=Path(env["LINGXI_DRIVE_PROBE_STATE_DIR"].strip()),
    )


# ---------------------------------------------------------------------------
# 状态：让九步可以逐步执行、可以在窗口中断后续跑。**不脱敏**——只允许活在
# LINGXI_DRIVE_PROBE_STATE_DIR 指定的独立临时目录，不得提交、粘贴进 Issue/日志。
# ---------------------------------------------------------------------------


@dataclass
class ProbeState:
    folder_name: str | None = None
    root_folder_token: str | None = None
    probe_folder_token: str | None = None
    probe_document_token: str | None = None
    collaborators_granted: list[dict[str, str]] = field(default_factory=list)
    completed_steps: list[int] = field(default_factory=list)
    halted_at_step: int | None = None
    halt_reason: str | None = None
    step_summaries: dict[str, Any] = field(default_factory=dict)
    cleanup_done: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["_notice"] = (
            "本文件含未脱敏的探针运行状态，只应存在于独立临时目录，"
            "不得提交、粘贴进 Issue、日志或聊天。"
        )
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ProbeState":
        # 只接受已知字段：状态文件里的 "_notice" 提示语和未来可能新增的旧字段
        # 都不应该让构造函数报错。
        known_fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known_fields})


def load_state(path: Path) -> ProbeState:
    if not path.exists():
        return ProbeState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateCorruptError(
            f"状态文件 {path} 无法读取或不是合法 JSON：{type(error).__name__}；"
            "不猜测状态，手动核对后再继续（必要时删除该文件重新开始）"
        ) from error
    return ProbeState.from_json(payload)


def save_state(path: Path, state: ProbeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 硬停止：命中即终止后续步骤并触发自动清理。没有绕过开关。
# ---------------------------------------------------------------------------


class ProbeHalt(Exception):
    def __init__(self, step: int, reason: str, summary: dict[str, Any]) -> None:
        self.step = step
        self.reason = reason
        self.summary = summary
        super().__init__(f"halt_at_step_{step}:{reason}")


@dataclass
class StepOutcome:
    step: int
    ok: bool
    summary: dict[str, Any]


def _generate_unique_folder_name(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"lingxi-drive-probe-{moment:%Y%m%d}-{secrets.token_hex(4)}"


def _is_creator_entry(member: dict[str, Any]) -> bool:
    """创建者（Bot-Test 应用自身）在协作者列表里的预期形状。

    真实 API 是否真的把所有者列进 ``members``、以及具体字段取值，目前未经真实
    调用验证（L1，见模块 docstring）；第一次真实执行如与此假设不符，需要回来
    修正这条判定，不影响其余编排逻辑与契约测试覆盖的行为。
    """

    return member.get("perm") == "owner" and member.get("member_type") == "app"


def _unexpected_collaborators(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in members if not _is_creator_entry(m)]


# ---------------------------------------------------------------------------
# 九步
# ---------------------------------------------------------------------------


def step_1_root_folder_meta(
    transport: DriveTransport, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    plan = {"call": "get_root_folder_meta", "params": {}}
    if dry_run:
        return StepOutcome(1, True, {"dry_run": True, "would_call": plan})

    result = transport.get_root_folder_meta()
    if not result.ok:
        raise ProbeHalt(1, "root_meta_failed", {"code": result.code, "msg": redact_message(result.msg)})
    token = result.data.get("token")
    if not token:
        raise ProbeHalt(1, "root_meta_missing_token", {"code": result.code})
    state.root_folder_token = token
    return StepOutcome(1, True, {"code": result.code, "root_folder_token": redact_id(token)})


def step_2_create_folder(
    transport: DriveTransport, state: ProbeState, *, dry_run: bool, now: datetime | None = None
) -> StepOutcome:
    if not dry_run and not state.root_folder_token:
        raise UsageError("step_2_requires_step_1：先跑 --step 1 拿到根目录 token")

    name = state.folder_name or _generate_unique_folder_name(now)
    parent_display = (
        redact_id(state.root_folder_token) if state.root_folder_token else "(尚未从步骤 1 取得，非真实标识)"
    )
    plan = {
        "call": "create_folder",
        "params": {"name": name, "parent_token": parent_display},
    }
    if dry_run:
        return StepOutcome(2, True, {"dry_run": True, "would_call": plan})

    state.folder_name = name
    result = transport.create_folder(name=name, parent_token=state.root_folder_token or "")
    if not result.ok:
        raise ProbeHalt(2, "create_folder_failed", {"code": result.code, "msg": redact_message(result.msg)})
    token = result.data.get("token")
    if not token:
        raise ProbeHalt(2, "create_folder_missing_token", {"code": result.code})
    state.probe_folder_token = token
    return StepOutcome(
        2, True, {"code": result.code, "folder_token": redact_id(token), "folder_name": name}
    )


def step_3_read_initial_collaborators(
    transport: DriveTransport, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    if not dry_run and not state.probe_folder_token:
        raise UsageError("step_3_requires_step_2：先跑 --step 2 创建测试目录")

    plan = {"call": "list_collaborators", "params": {"token": redact_id(state.probe_folder_token), "type": "folder"}}
    if dry_run:
        return StepOutcome(3, True, {"dry_run": True, "would_call": plan})

    result = transport.list_collaborators(token=state.probe_folder_token or "", obj_type="folder")
    if not result.ok:
        raise ProbeHalt(3, "list_collaborators_failed", {"code": result.code})
    members = result.data.get("members", [])
    unexpected = _unexpected_collaborators(members)
    if unexpected:
        raise ProbeHalt(
            3,
            "unexpected_inherited_collaborator",
            {"member_count": len(members), "unexpected_count": len(unexpected)},
        )
    return StepOutcome(3, True, {"code": result.code, "member_count": len(members), "unexpected_members": False})


def _grant_and_verify(
    transport: DriveTransport,
    state: ProbeState,
    *,
    step: int,
    member_id: str,
    role: str,
    expected_member_count: int,
    dry_run: bool,
) -> StepOutcome:
    if not dry_run and not state.probe_folder_token:
        raise UsageError(f"step_{step}_requires_step_2：先跑 --step 2 创建测试目录")

    plan = {
        "call": "add_collaborator",
        "params": {"token": redact_id(state.probe_folder_token), "member_id": redact_id(member_id), "perm": "edit"},
    }
    if dry_run:
        return StepOutcome(step, True, {"dry_run": True, "would_call": plan})

    add_result = transport.add_collaborator(
        token=state.probe_folder_token or "",
        obj_type="folder",
        member_type="openid",
        member_id=member_id,
        perm="edit",
        notify=False,
    )
    if not add_result.ok:
        raise ProbeHalt(step, "grant_failed", {"code": add_result.code, "msg": redact_message(add_result.msg)})

    read_result = transport.list_collaborators(token=state.probe_folder_token or "", obj_type="folder")
    if not read_result.ok:
        raise ProbeHalt(step, "read_back_failed", {"code": read_result.code})

    members = read_result.data.get("members", [])
    matches = [m for m in members if m.get("member_id") == member_id and m.get("perm") == "edit"]
    if len(matches) != 1 or len(members) != expected_member_count:
        raise ProbeHalt(
            step,
            "unexpected_grant_result",
            {"member_count": len(members), "expected_member_count": expected_member_count},
        )

    state.collaborators_granted.append(
        {"member_type": "openid", "member_id": member_id, "perm": "edit", "role": role}
    )
    return StepOutcome(step, True, {"code": add_result.code, "member_count": len(members), "granted_role": role})


def step_4_grant_cross_tenant(
    transport: DriveTransport, config: ProbeConfig, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    return _grant_and_verify(
        transport,
        state,
        step=4,
        member_id=config.member_cross,
        role="cross_tenant_positive",
        expected_member_count=1,
        dry_run=dry_run,
    )


def step_5_grant_same_tenant(
    transport: DriveTransport, config: ProbeConfig, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    return _grant_and_verify(
        transport,
        state,
        step=5,
        member_id=config.member_same,
        role="same_tenant_control",
        expected_member_count=2,
        dry_run=dry_run,
    )


def step_6_create_probe_document(
    transport: DriveTransport, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    if not dry_run and not state.probe_folder_token:
        raise UsageError("step_6_requires_step_2：先跑 --step 2 创建测试目录")
    if not dry_run and not state.collaborators_granted:
        raise UsageError("step_6_requires_step_4_or_5：先至少完成一次协作者授予再检查继承")

    plan = {"call": "create_document", "params": {"folder_token": redact_id(state.probe_folder_token)}}
    if dry_run:
        return StepOutcome(6, True, {"dry_run": True, "would_call": plan})

    result = transport.create_document(folder_token=state.probe_folder_token or "")
    if not result.ok:
        raise ProbeHalt(6, "create_document_failed", {"code": result.code, "msg": redact_message(result.msg)})
    doc_token = result.data.get("token")
    if not doc_token:
        raise ProbeHalt(6, "create_document_missing_token", {"code": result.code})
    state.probe_document_token = doc_token

    read_result = transport.list_collaborators(token=doc_token, obj_type="docx")
    if not read_result.ok:
        raise ProbeHalt(6, "read_document_collaborators_failed", {"code": read_result.code})

    doc_members = {(m.get("member_id"), m.get("perm")) for m in read_result.data.get("members", [])}
    expected = {(g["member_id"], g["perm"]) for g in state.collaborators_granted}
    if doc_members != expected:
        raise ProbeHalt(
            6,
            "inheritance_mismatch",
            {"expected_count": len(expected), "actual_count": len(doc_members)},
        )

    return StepOutcome(
        6, True, {"code": result.code, "document_token": redact_id(doc_token), "inherited_member_count": len(doc_members)}
    )


def step_7_repeat_calls(
    transport: DriveTransport, config: ProbeConfig, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    """幂等观察：只记录行为，不判定成败，也不声称接口具有幂等保证。

    这是九步里**唯一没有硬停止条件**的一步——执行卡明确要求"如实记录观察到的
    行为"，无论重复调用报错、产生第二个对象还是原样返回，都不属于失败。
    """

    if not dry_run and (not state.probe_folder_token or not state.folder_name):
        raise UsageError("step_7_requires_step_2：先跑 --step 2 创建测试目录")

    plan = {
        "call": "create_folder + add_collaborator (repeat)",
        "params": {"name": state.folder_name, "member_id": redact_id(config.member_cross)},
    }
    if dry_run:
        return StepOutcome(7, True, {"dry_run": True, "would_call": plan})

    repeat_create = transport.create_folder(name=state.folder_name or "", parent_token=state.root_folder_token or "")
    repeat_add = transport.add_collaborator(
        token=state.probe_folder_token or "",
        obj_type="folder",
        member_type="openid",
        member_id=config.member_cross,
        perm="edit",
        notify=False,
    )
    observation = {
        "repeat_create_ok": repeat_create.ok,
        "repeat_create_code": repeat_create.code,
        "repeat_create_same_token": (
            repeat_create.data.get("token") == state.probe_folder_token if repeat_create.ok else None
        ),
        "repeat_add_ok": repeat_add.ok,
        "repeat_add_code": repeat_add.code,
        # 硬编码 False：这一步永远只描述观察到的行为，绝不允许被解读成"接口保证幂等"。
        "claims_idempotency_guarantee": False,
    }
    state.step_summaries["step_7_idempotency_observation"] = observation
    return StepOutcome(7, True, observation)


def step_8_negative_access_check(
    state: ProbeState, *, dry_run: bool, observed_result: str | None
) -> StepOutcome:
    """T-Neg-01 用她自己的飞书客户端手动尝试访问，本脚本不代持她的身份。

    脚本这里唯一的职责是如实记录主持人现场观察到的结果，并对"意外可访问"
    执行硬停止——没有 API 调用，因为让脚本代 T-Neg-01 去访问，需要她自己的
    真实凭据，那本身就是本脚本明确拒绝触碰的东西。
    """

    plan = {"call": "human_observation", "who": "T-Neg-01", "action": "尝试打开测试目录与测试文档"}
    if dry_run:
        return StepOutcome(8, True, {"dry_run": True, "would_ask": plan})

    if observed_result not in ("allowed", "denied"):
        raise UsageError(
            "step_8_requires_observed_result："
            "必须传 --t-neg-01-result allowed|denied（由主持人现场观察后如实填写）"
        )
    if observed_result == "allowed":
        raise ProbeHalt(8, "unauthorized_access_succeeded", {"observed": "allowed"})

    state.step_summaries["step_8_negative_access_observed"] = "denied"
    return StepOutcome(8, True, {"observed": "denied"})


def step_9_cleanup(transport: DriveTransport, state: ProbeState, *, dry_run: bool) -> StepOutcome:
    """可重入：中断后单独用 ``--cleanup-only`` 重跑是安全的——没有可清理的对象时
    直接返回"已完成、无需处理"，不是错误。删不掉的如实登记为未完成，不假装成功。
    """

    plan_actions: list[dict[str, Any]] = [
        {"call": "remove_collaborator", "member": redact_id(g["member_id"])} for g in state.collaborators_granted
    ]
    if state.probe_document_token:
        plan_actions.append({"call": "delete_file", "target": "document"})
    if state.probe_folder_token:
        plan_actions.append({"call": "delete_file", "target": "folder"})
    if dry_run:
        return StepOutcome(9, True, {"dry_run": True, "would_call": plan_actions})

    removed: list[str] = []
    still_pending: list[dict[str, str]] = []
    for grant in list(state.collaborators_granted):
        result = transport.remove_collaborator(
            token=state.probe_folder_token or "",
            obj_type="folder",
            member_type=grant["member_type"],
            member_id=grant["member_id"],
        )
        if result.ok:
            removed.append(grant["role"])
        else:
            still_pending.append(grant)
    state.collaborators_granted = still_pending

    document_deleted: bool | None = None
    if state.probe_document_token:
        result = transport.delete_file(token=state.probe_document_token, obj_type="docx")
        document_deleted = bool(result.ok)
        if result.ok:
            state.probe_document_token = None

    folder_deleted: bool | None = None
    if state.probe_folder_token and not still_pending:
        result = transport.delete_file(token=state.probe_folder_token, obj_type="folder")
        folder_deleted = bool(result.ok)
        if result.ok:
            state.probe_folder_token = None

    incomplete = bool(still_pending) or state.probe_document_token is not None or state.probe_folder_token is not None
    summary = {
        "collaborators_removed": removed,
        "collaborators_removal_pending": [g["role"] for g in still_pending],
        "document_deleted": document_deleted,
        "folder_deleted": folder_deleted,
        "complete": not incomplete,
    }
    if not incomplete and (document_deleted or folder_deleted):
        # API 的"删除"是移到回收站（约 30 天），不是永久清空；这条提醒对应门槛
        # 「回收站清理 Owner 与期限」——脚本不做永久清空，只提醒别忘了登记。
        summary["recycle_bin_followup_required"] = True
    state.cleanup_done = summary
    return StepOutcome(9, not incomplete, summary)


STEP_FUNCTIONS = {1, 2, 3, 4, 5, 6, 7, 8, 9}


def run_steps(
    transport: DriveTransport,
    config: ProbeConfig,
    state: ProbeState,
    steps: list[int],
    *,
    dry_run: bool,
    t_neg_01_result: str | None,
) -> tuple[list[StepOutcome], ProbeHalt | None]:
    outcomes: list[StepOutcome] = []
    halt: ProbeHalt | None = None
    for step in steps:
        try:
            if step == 1:
                outcome = step_1_root_folder_meta(transport, state, dry_run=dry_run)
            elif step == 2:
                outcome = step_2_create_folder(transport, state, dry_run=dry_run)
            elif step == 3:
                outcome = step_3_read_initial_collaborators(transport, state, dry_run=dry_run)
            elif step == 4:
                outcome = step_4_grant_cross_tenant(transport, config, state, dry_run=dry_run)
            elif step == 5:
                outcome = step_5_grant_same_tenant(transport, config, state, dry_run=dry_run)
            elif step == 6:
                outcome = step_6_create_probe_document(transport, state, dry_run=dry_run)
            elif step == 7:
                outcome = step_7_repeat_calls(transport, config, state, dry_run=dry_run)
            elif step == 8:
                outcome = step_8_negative_access_check(state, dry_run=dry_run, observed_result=t_neg_01_result)
            elif step == 9:
                outcome = step_9_cleanup(transport, state, dry_run=dry_run)
            else:  # pragma: no cover - argparse 的 choices 已经挡住这个分支
                raise UsageError(f"unknown_step:{step}")
        except ProbeHalt as raised:
            state.halted_at_step = raised.step
            state.halt_reason = raised.reason
            state.step_summaries[f"step_{raised.step}_halt"] = raised.summary
            outcomes.append(StepOutcome(raised.step, False, {"halted": True, "reason": raised.reason, **raised.summary}))
            halt = raised
            break
        else:
            if not dry_run and step not in state.completed_steps:
                state.completed_steps.append(step)
            state.step_summaries[f"step_{step}"] = outcome.summary
            outcomes.append(outcome)
    return outcomes, halt


def build_report(outcomes: list[StepOutcome], state: ProbeState, *, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "steps": [{"step": o.step, "ok": o.ok, **o.summary} for o in outcomes],
        "halted_at_step": state.halted_at_step,
        "halt_reason": state.halt_reason,
        "collaborator_claim": (
            "collaborator_list_matches_expected" if state.halted_at_step is None else "not_established"
        ),
        # 常量引用，不是计算结果——见 ONLY_OWNER_ACCESSIBLE_CLAIM 的 docstring。
        "only_owner_accessible": ONLY_OWNER_ACCESSIBLE_CLAIM,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="#97 专属云盘目录九步探针；默认 dry-run，见模块 docstring。"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--step", type=int, choices=sorted(STEP_FUNCTIONS), help="只跑这一步")
    mode.add_argument("--from-step", type=int, choices=sorted(STEP_FUNCTIONS), help="从这一步跑到第 9 步")
    mode.add_argument("--all", action="store_true", help="等价于 --from-step 1")
    mode.add_argument("--cleanup-only", action="store_true", help="只跑步骤 9，可重入")
    parser.add_argument("--execute", action="store_true", help="真实发起请求；不传则只打印计划（默认）")
    parser.add_argument(
        "--t-neg-01-result",
        choices=("allowed", "denied"),
        default=None,
        help="步骤 8：主持人现场观察到的结果（真实执行时必填）",
    )
    parser.add_argument("--state-file", default=None, help="覆盖默认状态文件路径")
    return parser


def _steps_from_args(args: argparse.Namespace) -> list[int] | None:
    if args.cleanup_only:
        return [9]
    if args.step is not None:
        return [args.step]
    if args.from_step is not None:
        return list(range(args.from_step, 10))
    if args.all:
        return list(range(1, 10))
    return None


def main(
    argv: list[str] | None = None,
    *,
    transport: DriveTransport | None = None,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    env = os.environ if env is None else env  # type: ignore[assignment]
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    dry_run = not args.execute

    try:
        config = load_config(env)
    except MissingEnvironmentError as error:
        print(f"缺少环境变量：{', '.join(error.missing)}", file=sys.stderr)
        return 2

    steps = _steps_from_args(args)
    if steps is None:
        parser.print_help(sys.stderr)
        return 2

    state_path = Path(args.state_file) if args.state_file else config.state_dir / "drive_folder_probe_state.json"
    try:
        state = load_state(state_path)
    except StateCorruptError as error:
        print(str(error), file=sys.stderr)
        return 2

    if state.halted_at_step is not None and not args.cleanup_only:
        print(
            f"此前已在步骤 {state.halted_at_step} 触发硬停止（{state.halt_reason}）。"
            "只允许 --cleanup-only；本脚本不提供跳过硬停止或扩大权限后继续的开关。",
            file=sys.stderr,
        )
        return 3

    if transport is None:
        transport = LarkDriveTransport(app_id=config.app_id, app_secret=config.app_secret)

    try:
        outcomes, halt = run_steps(
            transport, config, state, steps, dry_run=dry_run, t_neg_01_result=args.t_neg_01_result
        )
    except UsageError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - 探针必须以文档化退出码收口，不留裸 traceback
        print(f"{type(error).__name__}：探针内部错误，非硬停止结论", file=sys.stderr)
        if not dry_run:
            save_state(state_path, state)
        return 1

    exit_code = 0
    if halt is not None:
        exit_code = 3
        # 判断"是否已经跑过清理"要看**实际产出的 outcomes**，不能看请求的 steps
        # 列表——`--from-step 1` 把 9 也列在请求范围内，但硬停止会让循环在半路
        # break，9 从未真的执行过。这里如果只检查 `9 not in steps` 会在这种情况
        # 下误判"清理已经跑过"，导致硬停止后目录/协作者留在原地没人清理。
        if not dry_run and not any(o.step == 9 for o in outcomes):
            cleanup_outcome = step_9_cleanup(transport, state, dry_run=False)
            state.step_summaries["step_9"] = cleanup_outcome.summary
            outcomes.append(cleanup_outcome)
    elif any(o.step == 9 and not o.ok for o in outcomes):
        # 没有硬停止，但清理本身没做完（例如某个协作者删不掉）：如实报告，
        # 不能因为"没有命中硬停止条款"就顺势返回成功。
        exit_code = 3

    if not dry_run:
        save_state(state_path, state)

    report = build_report(outcomes, state, dry_run=dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


# ---------------------------------------------------------------------------
# 真实传输层：裸 HTTP + tenant_access_token（与 adapters/feishu_directory.py 同一
# 习惯，不额外依赖 lark-oapi）。**本类未经真实调用验证（L1）**——端点路径与字段名
# 依据 2026-08-09 规范研究评论；第一次真实执行如与线上契约不符，只需要修正这个
# 类，不影响上面的九步编排逻辑与其契约测试覆盖的行为。
# ---------------------------------------------------------------------------


class LarkDriveTransport:
    _BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, *, app_id: str, app_secret: str, timeout_seconds: float = 10.0) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout_seconds
        self._tenant_access_token: str | None = None

    def _token(self) -> str:
        if self._tenant_access_token is None:
            payload = self._call(
                "POST",
                "/auth/v3/tenant_access_token/internal",
                body={"app_id": self._app_id, "app_secret": self._app_secret},
                authorized=False,
            )
            token = payload.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("tenant_access_token_not_confirmed")
            self._tenant_access_token = token
        return self._tenant_access_token

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        authorized: bool = True,
    ) -> dict[str, Any]:
        import urllib.parse
        import urllib.request

        url = f"{self._BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authorized:
            headers["Authorization"] = f"Bearer {self._token()}"
        data = json.dumps(body or {}).encode("utf-8") if method in ("POST", "DELETE") else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - 固定 open.feishu.cn
            return json.loads(response.read().decode("utf-8"))

    def _result(self, payload: dict[str, Any]) -> ApiResult:
        code = payload.get("code", -1)
        ok = code in (0, "0")
        return ApiResult(ok=bool(ok), code=int(code) if isinstance(code, (int, str)) and str(code).lstrip("-").isdigit() else -1, msg=str(payload.get("msg", "")), data=payload.get("data") or {})

    def get_root_folder_meta(self) -> ApiResult:
        return self._result(self._call("GET", "/drive/explorer/v2/root_folder/meta"))

    def create_folder(self, *, name: str, parent_token: str) -> ApiResult:
        return self._result(
            self._call("POST", "/drive/v1/files/create_folder", body={"name": name, "folder_token": parent_token})
        )

    def list_collaborators(self, *, token: str, obj_type: str) -> ApiResult:
        return self._result(self._call("GET", f"/drive/v1/permissions/{token}/members", params={"type": obj_type}))

    def add_collaborator(
        self, *, token: str, obj_type: str, member_type: str, member_id: str, perm: str, notify: bool
    ) -> ApiResult:
        return self._result(
            self._call(
                "POST",
                f"/drive/v1/permissions/{token}/members",
                params={"type": obj_type},
                body={
                    "member_type": member_type,
                    "member_id": member_id,
                    "perm": perm,
                    "need_notification": notify,
                },
            )
        )

    def remove_collaborator(self, *, token: str, obj_type: str, member_type: str, member_id: str) -> ApiResult:
        return self._result(
            self._call(
                "DELETE",
                f"/drive/v1/permissions/{token}/members/{member_id}",
                params={"type": obj_type, "member_type": member_type},
            )
        )

    def create_document(self, *, folder_token: str) -> ApiResult:
        return self._result(self._call("POST", "/docx/v1/documents", body={"folder_token": folder_token}))

    def delete_file(self, *, token: str, obj_type: str) -> ApiResult:
        return self._result(self._call("DELETE", f"/drive/v1/files/{token}", params={"type": obj_type}))


if __name__ == "__main__":
    raise SystemExit(main())
