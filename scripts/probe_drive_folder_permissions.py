#!/usr/bin/env python3
"""#97 专属云盘目录九步探针（受控验证脚本，只在 biai-stage/tz 等受控环境运行，
不进生产镜像）。

**适用范围**：只验证在职用户的跨租户访问、协作者边界、继承与未授权访问。
**不执行离职交接或所有权转移**——飞书官方规范已确认「机器人持有目录随关联租户
员工离职自动交接」不成立，产品负责人已于 2026-08-14（#140）决定离职场景不做自动
迁移，本脚本不提供任何 ``transfer_owner`` 或离职模拟能力。

对应操作说明原载 ``docs/参考证据/专属云盘目录九步探针执行卡.md``（一次性执行卡，
已随 2026-08-24 维护批退场；正文见 git 历史存档
https://github.com/Moshuiwang/lingxi/blob/256a905c5f370ab8c13f77c11a2cb6416badd6b2/docs/参考证据/专属云盘目录九步探针执行卡.md
）——谁操作、预期结果、失败怎么办、判定口径、硬停止条款与清理回读要求；
本文件曾是那张卡片的唯一可执行入口（#97 已随 #140 裁定关闭）。

**默认 dry-run**：不传 ``--execute`` 时，任何步骤都只打印将要发起的调用与参数
形状（且参数已脱敏），不发出任何真实请求。

**硬停止是代码，不是约定**：步骤 3 出现预期外协作者、步骤 4/5 协作者授予结果不
符合预期、步骤 6 文档协作者与目录**当前实际重新读回**的协作者不一致、步骤 8
``T-Neg-01`` 意外可以访问，任一命中都会抛出 :class:`ProbeHalt`，终止后续步骤并
自动进入清理（步骤 9）。判定细节（2026-08-18/19 两轮独立审查坐实并逐条封死）：

- 步骤 3 通过后把观察到的协作者列表记为**基线**（:data:`ProbeState.baseline_collaborators`），
  步骤 4/5 的期望集合 = 该基线 + 已确认授予过的协作者 + 本次新授予的这一位，
  不会因为真实 API 持续返回创建者条目就必然在步骤 4 误停。
- 身份比对用 ``(member_type, member_id, perm)`` 三元组，不只看 ``member_id``——
  ``openid/X`` 与 ``email/X`` 不会被判相等；用可数结构（``Counter``）而不是
  ``set``，完全重复的条目不会被吞掉、能被发现。
- 步骤 6 用**重新读回**的目录协作者，不用本地内存里"以为授过谁"的那份。
- 步骤 7 要求步骤 3–6 已经真实跑完才允许执行，不能绕开继承与授权验证直接写；
  重复创建如果返回了不同 token，那个 token 也会被记进状态并纳入步骤 9 的
  清理范围，不会变成清理不掉的遗留对象。

**这里没有、也不会加「扩大 scope 后重试」或「修改租户设置后继续」的开关**——
:class:`DriveTransport` 协议里根本不存在任何租户设置写接口。

**命中硬停止后，没有任何参数组合能让脚本继续发起真实写请求**：

- 状态文件路径**固定**为 ``$LINGXI_DRIVE_PROBE_STATE_DIR/drive_folder_probe_state.json``，
  不接受任何路径覆盖参数。
- 每次命中硬停止都会**额外**写一份与状态目录无关的**全局哨兵文件**
  （``~/.lingxi_drive_probe_halted.json``，路径固定，不经过任何 CLI 参数或环境
  变量）：只要它存在，任何 ``--execute``（纯清理请求除外，见下）都会被直接拒绝
  ——哪怕把 ``LINGXI_DRIVE_PROBE_STATE_DIR`` 换成一个全新目录也一样。清除它
  **只能靠人工直接删除这个文件**。哨兵的检查与写入都在它自己的跨进程锁内完成
  （:func:`_locked_sentinel`），两个使用不同状态目录的并发调用不会同时通过门禁。
- "纯清理请求"按**实际要跑的步骤是否等于 ``[9]``** 判断，不是按 ``--cleanup-only``
  这一个 flag 的名字——``--step 9 --execute`` 与 ``--cleanup-only --execute``
  效果完全一样，两者都始终放行，其余任何真实写请求命中硬停止后都会被拒绝。
- 命令行解析关闭了前缀缩写匹配（``allow_abbrev=False``）。
- 状态的"读 → 判断 → 跑步骤 → 落盘"全过程持有跨进程文件锁（:func:`_locked_state`）。
- 命中硬停止后**先落盘停止状态，再尝试自动清理**；清理阶段哪怕抛异常，停止事实
  也已经写死。保存状态或写哨兵本身失败时（磁盘满、权限问题）**不会静默继续**：
  打印明确提示并以非零码退出，见 :func:`_try_save_state`/:func:`_try_write_halt_sentinel`。
- ``--all``/``--from-step`` 覆盖到步骤 8 且带 ``--execute`` 时，如果没有同时传
  ``--t-neg-01-result``，会在**发出任何请求之前**直接拒绝——不会先把步骤 1–7
  的真实对象都建好，最后才因为缺这个参数而在步骤 8 报错退出、还不清理。

**凭据只从环境变量读取，不接受命令行参数**（命令行参数会进 shell 历史与进程
列表）。真实窗口里，步骤 8 需要主持人现场观察 ``T-Neg-01`` 的结果后才能确定
``--t-neg-01-result`` 的值，因此**典型用法是逐步执行**（``--step 1`` … ``--step 7``，
观察后再 ``--step 8 --execute --t-neg-01-result <观察结果>``，最后
``--cleanup-only``），而不是一次性 ``--all --execute``——后者要求在还没开始
之前就已经知道步骤 8 的答案，这在真实窗口里不成立：

.. code-block:: bash

    export LINGXI_DRIVE_PROBE_APP_ID=...
    export LINGXI_DRIVE_PROBE_APP_SECRET=...
    export LINGXI_DRIVE_PROBE_MEMBER_CROSS=...   # T-Cross-01 的协作者标识
    export LINGXI_DRIVE_PROBE_MEMBER_SAME=...    # T-Same-01 的协作者标识
    export LINGXI_DRIVE_PROBE_STATE_DIR=...      # 独立临时状态/日志目录，谁建谁清

    PYTHONPATH=src python3 scripts/probe_drive_folder_permissions.py --step 1 --execute
    PYTHONPATH=src python3 scripts/probe_drive_folder_permissions.py --step 2 --execute
    # ... 依次跑到 --step 7 ...
    # T-Neg-01 现场尝试访问后：
    PYTHONPATH=src python3 scripts/probe_drive_folder_permissions.py --step 8 --execute --t-neg-01-result denied
    PYTHONPATH=src python3 scripts/probe_drive_folder_permissions.py --cleanup-only --execute

任一变量缺失，脚本在做任何事之前失败关闭，只报告缺失的变量名（退出码 2）。

**状态文件**（``$LINGXI_DRIVE_PROBE_STATE_DIR/drive_folder_probe_state.json``）
让九步可以逐步执行、可以在窗口中断后从任意步续跑——它保存本次探针创建的真实
对象标识（目录/文档 token、已授予的协作者、**完整目录名**），**不脱敏**，因此
只能活在独立临时目录里，不得提交、粘贴进 Issue、日志或聊天。终端输出与状态
文件是两回事：终端输出（``build_report`` 及九步各自的 summary）永远脱敏。

**协作者列表符合预期 ≠ 仅本人可访问**：文件夹链接分享范围没有已知可读写 API，
程序永远无法自证「除协作者外没有别的入口能打开」。``only_owner_accessible`` 字段
是硬编码常量 :data:`ONLY_OWNER_ACCESSIBLE_CLAIM`，不是任何步骤判断出来的结果。

**``collaborator_claim`` 只在真正验证过时才给出结论**：只有非 dry-run、未命中
硬停止、且步骤 3/4/5/6 全部真实跑完，才会给出 ``collaborator_list_matches_expected``；
否则一律 ``not_established``——这与 ``only_owner_accessible`` 可能出现的
``unknown`` 是两回事：``unknown`` 是「测了但读不到结论」，``not_established``
是「根本没测到那一步」，不得混用，也不得用只跑了一两步的结果宣称已经验证。

**退出码**：0 全部请求步骤正常完成（含 dry-run 预览、cleanup-only 完全清空）；
1 脚本内部异常（环境/网络问题、锁文件无法建立、状态或哨兵持久化失败——都不是
探针结论）；2 用法或配置错误（缺环境变量、步骤顺序或前置条件不满足、命令行
参数冲突/未知参数、状态文件损坏、步骤 8 缺观察结果）；3 命中硬停止条款（含
清理阶段本身抛异常或没有完全成功），或全局硬停止哨兵仍然存在。

**已知边界（明确接受，不再修，2026-08-18/19 三轮独立审查后由产品负责人裁定）**：

1. **切换 ``HOME`` 环境变量可以让全局哨兵解析到不同位置，从而绕开它。**
   接受：这需要操作者刻意修改一个会影响机器上几乎所有其他程序的环境变量，
   不是"手滑"能触发的参数组合——本脚本的威胁模型是"编排者在受控机器上操作
   失误"，不是"敌手蓄意绕过"；蓄意修改 ``HOME`` 已经超出这个模型。
2. **手工伪造一份含虚构 token 的状态文件，再跑 ``--cleanup-only --execute``，
   会让脚本对状态文件里记录的任意 token 发起真实的移除协作者/删除文件调用**
   （``--cleanup-only`` 为了保证"命中硬停止后清理永远能跑"，被设计成不受硬停止
   门禁约束，因此也不会去验证状态文件里的 token 是不是这次探针自己创建的）。
   接受：触发它需要有人手工编辑状态文件构造虚构内容，操作者是编排者本人、
   在受控机器（``biai-stage``/``tz``）上，不存在有能力这样做却意图不明的第三方。
   真实窗口里状态文件由脚本自己维护，不应该被手工编辑。
3. **符号链接类的文件系统层面绕过**（例如把状态目录或状态文件做成指向别处的
   符号链接）未做防范。接受：理由同上——需要操作者在受控机器上主动构造，
   不属于本脚本要防的"手滑"范围。

这三条边界只有在受控机器的使用者身份、权限或数量发生实质变化时才需要重新评估，
不属于本脚本自身的待办。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
from collections import Counter
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
    """截断标识：只保留前 ``keep`` 位与长度，永不回显完整 token/用户标识/目录名。"""

    if not value:
        return "(none)"
    text = str(value)
    if len(text) <= keep:
        return f"{text[0]}…(len={len(text)})"
    return f"{text[:keep]}…(len={len(text)})"


_IDENTIFIER_LIKE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,}")


def redact_message(text: str | None) -> dict[str, Any]:
    """把飞书返回的自由文本 ``msg`` 折叠成不透传原文的元信息。

    ``msg`` 偶尔会回显请求参数——完整 token、成员标识、邮箱都可能落在里面；
    截断只是"泄露得晚一点"，不是脱敏。这里**不透传任何原文片段**，只给长度和
    一个粗粒度的"看起来像不像含有标识符"信号。
    """

    if not text:
        return {"present": False}
    looks_like_identifier = bool(_IDENTIFIER_LIKE_PATTERN.search(text)) or "@" in text
    return {"present": True, "length": len(text), "looks_like_identifier": looks_like_identifier}


def redact_path(path: Path | str) -> str:
    """路径可能含真实用户名/主机目录结构；只保留文件名，不回显父目录。"""

    return f"…/{Path(path).name}"


UNKNOWN = "unknown"
NOT_ESTABLISHED = "not_established"

ONLY_OWNER_ACCESSIBLE_CLAIM = UNKNOWN
"""硬编码常量，**不是任何函数的返回值**——理由见模块 docstring。"""


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


class LockAcquisitionError(RuntimeError):
    """建锁文件本身失败（mkdir/open 抛 OSError）；不是硬停止，是环境问题。"""


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
# 状态文件名**固定**、不接受任何 CLI 覆盖。
# ---------------------------------------------------------------------------

_STATE_FILENAME = "drive_folder_probe_state.json"
_LOCK_SUFFIX = ".lock"

_DEFAULT_HALT_SENTINEL_PATH = Path.home() / ".lingxi_drive_probe_halted.json"
"""全局硬停止哨兵的默认位置：固定在 home 目录下，不经过任何 CLI 参数或环境
变量。``main()`` 的 ``halt_sentinel_path`` 形参**只用于测试注入**。
"""


@dataclass
class ProbeState:
    folder_name: str | None = None
    root_folder_token: str | None = None
    probe_folder_token: str | None = None
    probe_document_token: str | None = None
    extra_folder_tokens: list[str] = field(default_factory=list)
    baseline_collaborators: list[dict[str, str]] = field(default_factory=list)
    collaborators_granted: list[dict[str, str]] = field(default_factory=list)
    completed_steps: list[int] = field(default_factory=list)
    halted_at_step: int | None = None
    halt_reason: str | None = None
    step_summaries: dict[str, Any] = field(default_factory=dict)
    cleanup_done: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["_notice"] = (
            "本文件含未脱敏的探针运行状态（含完整目录名/token），只应存在于"
            "独立临时目录，不得提交、粘贴进 Issue、日志或聊天。"
        )
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ProbeState":
        known_fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known_fields})


def load_state(path: Path) -> ProbeState:
    if not path.exists():
        return ProbeState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateCorruptError(
            f"状态文件 {redact_path(path)} 无法读取或不是合法 JSON：{type(error).__name__}；"
            "不猜测状态，手动核对后再继续（必要时删除该文件重新开始）"
        ) from error
    return ProbeState.from_json(payload)


def save_state(path: Path, state: ProbeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _try_save_state(path: Path, state: ProbeState) -> bool:
    """保存失败时打印清晰提示并返回 ``False``，不静默吞掉（独立审查 H2）：
    调用方看到 ``False`` 必须把这一步的失败反映到退出码，不能假装状态已经
    落盘、继续往下走。"""

    try:
        save_state(path, state)
        return True
    except OSError as error:
        print(
            f"保存状态失败（{type(error).__name__}）：{redact_path(path)}。"
            "停止事实未能持久化，不要假设它已经记录下来——请手动检查当前状态"
            "目录并人工确认残留对象。",
            file=sys.stderr,
        )
        return False


@contextlib.contextmanager
def _open_lock_file(lock_path: Path):
    """建锁文件；mkdir/open 失败时统一转成 :class:`LockAcquisitionError`，
    附带脱敏路径（独立审查 H3：原本未捕获的 ``OSError`` 会带着完整路径的
    traceback 直接打到 stderr）。"""

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "a+")
    except OSError as error:
        raise LockAcquisitionError(
            f"无法建立文件锁（{type(error).__name__}）：{redact_path(lock_path)}"
        ) from error
    try:
        yield lock_file
    finally:
        lock_file.close()


@contextlib.contextmanager
def _locked_state(path: Path):
    """整个"读状态 → 判断是否已经命中过硬停止 → 跑步骤 → 落盘"必须在同一把
    跨进程互斥锁内完成：否则两个并发进程可能都读到"尚未停止"，其中一个已经
    停止的结果，还可能被后保存的另一个进程覆盖掉。

    锁文件与状态文件同目录、固定命名，不经过任何 CLI 参数——它锁的是"这个
    状态目录"，不是"这次调用挑的路径"。用 POSIX ``flock``：受控执行环境
    （``biai-stage``/``tz``）都是 Linux，不追求跨平台。
    """

    with _open_lock_file(path.with_name(path.name + _LOCK_SUFFIX)) as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _locked_sentinel(sentinel_path: Path):
    """全局哨兵自己的锁：检查与写入都必须在这把锁内完成（独立审查 H1）——
    否则两个使用不同 ``LINGXI_DRIVE_PROBE_STATE_DIR`` 的并发调用可能同时读到
    "哨兵不存在"、同时通过门禁；每个状态目录各自的 :func:`_locked_state` 锁
    只保护那一个目录，保护不了这个与目录无关的全局资源。"""

    with _open_lock_file(sentinel_path.with_name(sentinel_path.name + _LOCK_SUFFIX)) as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_halt_sentinel(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 哨兵本身损坏也按"存在"处理：宁可误挡一次真实写操作，也不能因为解析
        # 失败就放行——这个文件的唯一职责是挡，不是提供诊断信息。
        return {"step": None, "reason": "sentinel_unreadable"}


def _write_halt_sentinel(path: Path, *, step: int, reason: str, state_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "halted_at": datetime.now(timezone.utc).isoformat(),
        "step": step,
        "reason": reason,
        "state_dir": redact_path(state_dir),
        "notice": (
            "跨状态目录的全局硬停止哨兵：只要这个文件存在，任何 --execute"
            "（纯清理请求除外）都会被拒绝，没有命令行参数能清除它。"
            "确认已经人工处理后，手动删除本文件才能继续任何真实写操作。"
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _try_write_halt_sentinel(path: Path, *, step: int, reason: str, state_dir: Path) -> bool:
    try:
        _write_halt_sentinel(path, step=step, reason=reason, state_dir=state_dir)
        return True
    except OSError as error:
        print(
            f"写入全局硬停止哨兵失败（{type(error).__name__}）：{redact_path(path)}。"
            "全局哨兵未能生效，不要假设换一个状态目录也会被挡住——请手动处理后"
            "再决定是否继续。",
            file=sys.stderr,
        )
        return False


def _lingering_objects_summary(state: ProbeState) -> dict[str, Any]:
    """任何非硬停止的异常/用法错误退出前，用它判断状态里还留着什么真实对象，
    好在提示里明确告诉操作者要不要跑 ``--cleanup-only``（独立审查 M1）。"""

    return {
        "folder_created": state.probe_folder_token is not None,
        "extra_folders_created": len(state.extra_folder_tokens),
        "document_created": state.probe_document_token is not None,
        "collaborators_granted_count": len(state.collaborators_granted),
    }


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
    修正这条判定。真实环境如果存在租户级默认协作者，需要先在执行卡的
    《步骤 3 默认项名单》附录里实测登记——那属于窗口前的人工核实结论，不是
    脚本可以替人判断的事；本函数本身不做任何例外放行。
    """

    return member.get("perm") == "owner" and member.get("member_type") == "app"


def _unexpected_collaborators(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in members if not _is_creator_entry(m)]


def _member_signature(member: dict[str, Any]) -> tuple[Any, Any, Any]:
    """身份比对用三元组，不只看 ``member_id``（独立审查 M4）：``openid/X`` 与
    ``email/X`` 如果只比 ``member_id``+``perm`` 会被判相等，而"协作者到底是谁"
    正是这次探针要回答的核心问题之一。"""

    return (member.get("member_type"), member.get("member_id"), member.get("perm"))


def _member_multiset(members: list[dict[str, Any]]) -> Counter:
    """用 ``Counter`` 而不是 ``set``（独立审查 M4）：``set`` 会把完全重复的
    协作者条目吞掉，让"读回结果里其实有重复条目"这种异常永远发现不了。"""

    return Counter(_member_signature(m) for m in members)


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
        "params": {"name": redact_id(name), "parent_token": parent_display},
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
        2, True, {"code": result.code, "folder_token": redact_id(token), "folder_name": redact_id(name)}
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
    # 记录基线：这份通过了步骤 3 检查的协作者列表，就是步骤 4/5 判断"有没有
    # 意外协作者"时的比对基准（独立审查 M5）——不这样做的话，步骤 4/5 会假设
    # "期望里不含创建者"，而如果真实 API 持续把创建者列进 members，步骤 4
    # 会必然误停，窗口报废在第 4 步。
    state.baseline_collaborators = [
        {"member_type": m.get("member_type"), "member_id": m.get("member_id"), "perm": m.get("perm")}
        for m in members
    ]
    return StepOutcome(3, True, {"code": result.code, "member_count": len(members), "unexpected_members": False})


def _grant_and_verify(
    transport: DriveTransport,
    state: ProbeState,
    *,
    step: int,
    member_id: str,
    role: str,
    dry_run: bool,
) -> StepOutcome:
    if not dry_run:
        if not state.probe_folder_token:
            raise UsageError(f"step_{step}_requires_step_2：先跑 --step 2 创建测试目录")
        if 3 not in state.completed_steps:
            raise UsageError(
                f"step_{step}_requires_step_3：先跑 --step 3 建立基线协作者清单，"
                "否则无法判断读回结果里哪些是意外协作者"
            )

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
    actual = _member_multiset(members)
    # 期望 = 步骤 3 认可的基线 + 此前已确认授予过的全部协作者 + 这一次新授予
    # 的这一位。用可数结构逐个身份比对（含 member_type，见 _member_signature），
    # 不是"数量对了 + 目标成员在列表里就算过"。
    expected: Counter = Counter()
    expected.update(_member_signature(m) for m in state.baseline_collaborators)
    expected.update((g["member_type"], g["member_id"], g["perm"]) for g in state.collaborators_granted)
    expected[("openid", member_id, "edit")] += 1
    if actual != expected:
        raise ProbeHalt(
            step,
            "unexpected_grant_result",
            {"actual_member_count": len(members), "expected_member_count": sum(expected.values())},
        )

    state.collaborators_granted.append(
        {"member_type": "openid", "member_id": member_id, "perm": "edit", "role": role}
    )
    return StepOutcome(step, True, {"code": add_result.code, "member_count": len(members), "granted_role": role})


def step_4_grant_cross_tenant(
    transport: DriveTransport, config: ProbeConfig, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    return _grant_and_verify(
        transport, state, step=4, member_id=config.member_cross, role="cross_tenant_positive", dry_run=dry_run
    )


def step_5_grant_same_tenant(
    transport: DriveTransport, config: ProbeConfig, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    return _grant_and_verify(
        transport, state, step=5, member_id=config.member_same, role="same_tenant_control", dry_run=dry_run
    )


def step_6_create_probe_document(
    transport: DriveTransport, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    if not dry_run and not state.probe_folder_token:
        raise UsageError("step_6_requires_step_2：先跑 --step 2 创建测试目录")
    if not dry_run and not state.collaborators_granted:
        raise UsageError("step_6_requires_step_4_or_5：先至少完成一次协作者授予再检查继承")

    plan = {
        "call": "list_collaborators(folder) + create_document + list_collaborators(document)",
        "params": {"folder_token": redact_id(state.probe_folder_token)},
    }
    if dry_run:
        return StepOutcome(6, True, {"dry_run": True, "would_call": plan})

    # 先重新读一次目录**当前**的协作者，不用本地内存里"以为授过谁"的那份。
    folder_read = transport.list_collaborators(token=state.probe_folder_token or "", obj_type="folder")
    if not folder_read.ok:
        raise ProbeHalt(6, "read_folder_collaborators_failed", {"code": folder_read.code})
    folder_members = _member_multiset(folder_read.data.get("members", []))

    result = transport.create_document(folder_token=state.probe_folder_token or "")
    if not result.ok:
        raise ProbeHalt(6, "create_document_failed", {"code": result.code, "msg": redact_message(result.msg)})
    doc_token = result.data.get("token")
    if not doc_token:
        raise ProbeHalt(6, "create_document_missing_token", {"code": result.code})
    state.probe_document_token = doc_token

    doc_read = transport.list_collaborators(token=doc_token, obj_type="docx")
    if not doc_read.ok:
        raise ProbeHalt(6, "read_document_collaborators_failed", {"code": doc_read.code})
    doc_members = _member_multiset(doc_read.data.get("members", []))

    if doc_members != folder_members:
        raise ProbeHalt(
            6,
            "inheritance_mismatch",
            {"folder_member_count": sum(folder_members.values()), "document_member_count": sum(doc_members.values())},
        )

    return StepOutcome(
        6,
        True,
        {"code": result.code, "document_token": redact_id(doc_token), "inherited_member_count": sum(doc_members.values())},
    )


def step_7_repeat_calls(
    transport: DriveTransport, config: ProbeConfig, state: ProbeState, *, dry_run: bool
) -> StepOutcome:
    """幂等观察：只记录行为，不判定成败，也不声称接口具有幂等保证。

    这是九步里**唯一没有硬停止条件**的一步——执行卡明确要求"如实记录观察到的
    行为"，无论重复调用报错、产生第二个对象还是原样返回，都不属于失败。
    """

    if not dry_run:
        if not state.probe_folder_token or not state.folder_name:
            raise UsageError("step_7_requires_step_2：先跑 --step 2 创建测试目录")
        missing_prereqs = [s for s in (3, 4, 5, 6) if s not in state.completed_steps]
        if missing_prereqs:
            # 独立审查 M2：不加这条检查的话，`step 1 → step 2 → step 7` 就能
            # 跳过继承与授权验证直接发起真实写——这是用法错误，不是硬停止
            # （硬停止是"验证过了、结果不对"，这里是"根本没验证就想跑"）。
            raise UsageError(
                "step_7_requires_steps_3_through_6：先完整跑完步骤 3–6（继承与授权"
                f"验证）再重复调用，缺：{missing_prereqs}"
            )

    plan = {
        "call": "create_folder + add_collaborator (repeat)",
        "params": {"name": redact_id(state.folder_name), "member_id": redact_id(config.member_cross)},
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

    new_token = repeat_create.data.get("token") if repeat_create.ok else None
    same_token = (new_token == state.probe_folder_token) if repeat_create.ok else None
    if repeat_create.ok and new_token and new_token != state.probe_folder_token and new_token not in state.extra_folder_tokens:
        # 独立审查 M3：重复创建如果返回了一个不同的 token，那是一个真实存在、
        # 没有任何变量指向的目录——必须记进状态，否则步骤 9 清理时根本不知道
        # 它的存在，变成清理不掉的遗留对象。
        state.extra_folder_tokens.append(new_token)

    observation = {
        "repeat_create_ok": repeat_create.ok,
        "repeat_create_code": repeat_create.code,
        "repeat_create_same_token": same_token,
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
    """T-Neg-01 用她自己的飞书客户端手动尝试访问，本脚本不代持她的身份。"""

    plan = {"call": "human_observation", "who": "T-Neg-01", "action": "尝试打开测试目录与测试文档"}
    if dry_run:
        return StepOutcome(8, True, {"dry_run": True, "would_ask": plan})

    if observed_result not in ("allowed", "denied"):
        # main() 在 --execute 且请求覆盖到步骤 8 时会提前挡掉这个缺失（独立审查
        # M1）；这里保留检查是防止 --step 8 单独调用时绕过那道前置检查。
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
    覆盖步骤 7 可能留下的额外重复目录（独立审查 M3）。
    """

    plan_actions: list[dict[str, Any]] = [
        {"call": "remove_collaborator", "member": redact_id(g["member_id"])} for g in state.collaborators_granted
    ]
    if state.probe_document_token:
        plan_actions.append({"call": "delete_file", "target": "document"})
    if state.probe_folder_token:
        plan_actions.append({"call": "delete_file", "target": "folder"})
    plan_actions += [{"call": "delete_file", "target": f"extra_folder_{i}"} for i in range(len(state.extra_folder_tokens))]
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

    extra_deleted = 0
    extra_pending: list[str] = []
    for token in list(state.extra_folder_tokens):
        result = transport.delete_file(token=token, obj_type="folder")
        if result.ok:
            extra_deleted += 1
        else:
            extra_pending.append(token)
    state.extra_folder_tokens = extra_pending

    incomplete = (
        bool(still_pending)
        or state.probe_document_token is not None
        or state.probe_folder_token is not None
        or bool(state.extra_folder_tokens)
    )
    summary = {
        "collaborators_removed": removed,
        "collaborators_removal_pending": [g["role"] for g in still_pending],
        "document_deleted": document_deleted,
        "folder_deleted": folder_deleted,
        "extra_folders_deleted_count": extra_deleted,
        "extra_folders_pending_count": len(extra_pending),
        "complete": not incomplete,
    }
    if not incomplete and (document_deleted or folder_deleted or extra_deleted):
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
    # 独立审查 M6：只有真的非 dry-run、没有命中硬停止、且步骤 3/4/5/6 全部
    # 真实跑完，才能宣称协作者列表符合预期；否则一律 not_established——不能
    # 因为"没有失败"就默认"已经验证过"，那是用低一级证据宣称高一级完成。
    collaborator_claim_prereqs = {3, 4, 5, 6}
    collaborator_claim_established = (
        not dry_run
        and state.halted_at_step is None
        and collaborator_claim_prereqs.issubset(set(state.completed_steps))
    )
    return {
        "dry_run": dry_run,
        "steps": [{"step": o.step, "ok": o.ok, **o.summary} for o in outcomes],
        "halted_at_step": state.halted_at_step,
        "halt_reason": state.halt_reason,
        "collaborator_claim": (
            "collaborator_list_matches_expected" if collaborator_claim_established else NOT_ESTABLISHED
        ),
        # 常量引用，不是计算结果——见 ONLY_OWNER_ACCESSIBLE_CLAIM 的 docstring。
        "only_owner_accessible": ONLY_OWNER_ACCESSIBLE_CLAIM,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="#97 专属云盘目录九步探针；默认 dry-run，见模块 docstring。",
        allow_abbrev=False,
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
        help="步骤 8：主持人现场观察到的结果（真实执行且覆盖到步骤 8 时必填）",
    )
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
    halt_sentinel_path: Path | None = None,
) -> int:
    """``halt_sentinel_path`` 只用于测试注入；真实调用永远解析到固定的 home
    目录路径（:data:`_DEFAULT_HALT_SENTINEL_PATH`）。
    """

    env = os.environ if env is None else env  # type: ignore[assignment]
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    dry_run = not args.execute
    sentinel_path = halt_sentinel_path or _DEFAULT_HALT_SENTINEL_PATH
    steps = _steps_from_args(args)
    # 判断"这次请求是不是只做清理"看**实际要跑哪些步骤**，不看 `--cleanup-only`
    # 这一个特定 flag 的名字——`--step 9 --execute` 产生的 steps 和 `--cleanup-only`
    # 完全一样，都只调用 step_9_cleanup。
    is_cleanup_only_request = steps == [9]

    # 独立审查 M1：请求覆盖到步骤 8 且真实执行、却没给 --t-neg-01-result 时，
    # 必须在发出**任何**请求之前拒绝——不能等到步骤 1-7 都真的建好对象了，
    # 才在步骤 8 因为用法错误退出、还不落盘不清理。
    if args.execute and steps is not None and 8 in steps and args.t_neg_01_result is None:
        print(
            "step_8_requires_observed_result：请求覆盖到步骤 8 且真实执行时，必须"
            "同时传 --t-neg-01-result allowed|denied——这个值应该是主持人已经拿到"
            "的观察结果，不是预先猜测。若还没做到步骤 8，请先分步跑到步骤 7，"
            "观察 T-Neg-01 的结果后再补这个参数；不要用 --all/--from-step 1 一口气"
            "跑过去，那样步骤 1-7 会先真实执行完才发现缺参数。",
            file=sys.stderr,
        )
        return 2

    try:
        # 独立审查 H1：哨兵检查在它自己的全局锁内完成。
        with _locked_sentinel(sentinel_path):
            existing_sentinel = _load_halt_sentinel(sentinel_path)
            if existing_sentinel is not None and args.execute and not is_cleanup_only_request:
                print(
                    "检测到全局硬停止哨兵：此前某次探针命中过硬停止，尚未人工清除，"
                    f"没有命令行参数能跳过它（step={existing_sentinel.get('step')} "
                    f"reason={existing_sentinel.get('reason')}）。确认已处理后手动删除 "
                    f"{redact_path(sentinel_path)} 才能继续任何真实写操作。",
                    file=sys.stderr,
                )
                return 3

        try:
            config = load_config(env)
        except MissingEnvironmentError as error:
            print(f"缺少环境变量：{', '.join(error.missing)}", file=sys.stderr)
            return 2

        if steps is None:
            parser.print_help(sys.stderr)
            return 2

        state_path = config.state_dir / _STATE_FILENAME

        if transport is None:
            transport = LarkDriveTransport(app_id=config.app_id, app_secret=config.app_secret)

        # 独立审查 P2-a：从这里开始的"读状态 → 判断 → 跑步骤 → 落盘"全程持有跨
        # 进程锁。
        with _locked_state(state_path):
            try:
                state = load_state(state_path)
            except StateCorruptError as error:
                print(str(error), file=sys.stderr)
                return 2

            if state.halted_at_step is not None and not is_cleanup_only_request:
                print(
                    f"此前已在步骤 {state.halted_at_step} 触发硬停止（{state.halt_reason}）。"
                    "只允许 --cleanup-only（或等价的 --step 9）；本脚本不提供跳过硬停止"
                    "或扩大权限后继续的开关。",
                    file=sys.stderr,
                )
                return 3

            try:
                outcomes, halt = run_steps(
                    transport, config, state, steps, dry_run=dry_run, t_neg_01_result=args.t_neg_01_result
                )
            except UsageError as error:
                print(str(error), file=sys.stderr)
                if not dry_run:
                    saved = _try_save_state(state_path, state)
                    lingering = _lingering_objects_summary(state)
                    if any(lingering.values()):
                        # 独立审查 M1：用法错误不能吞掉"其实已经留下了真实对象"这件事。
                        print(
                            "当前状态可能持有尚未清理的真实对象："
                            f"{json.dumps(lingering, ensure_ascii=False)}。"
                            "请跑 --cleanup-only --execute 清理，不要忽略。",
                            file=sys.stderr,
                        )
                    if not saved:
                        return 1
                return 2
            except Exception as error:  # noqa: BLE001 - 探针必须以文档化退出码收口，不留裸 traceback
                print(f"{type(error).__name__}：探针内部错误，非硬停止结论", file=sys.stderr)
                if not dry_run and not _try_save_state(state_path, state):
                    return 1
                return 1

            exit_code = 0
            if halt is not None:
                exit_code = 3
                if not dry_run:
                    # 独立审查 P2-b：先落盘停止状态，接下来的自动清理即使崩溃，
                    # 停止事实也已经写死。保存本身失败也不能静默继续（H2）。
                    if not _try_save_state(state_path, state):
                        return 1
                    with _locked_sentinel(sentinel_path):
                        if not _try_write_halt_sentinel(
                            sentinel_path, step=halt.step, reason=halt.reason, state_dir=config.state_dir
                        ):
                            return 1
                    if not any(o.step == 9 for o in outcomes):
                        try:
                            cleanup_outcome = step_9_cleanup(transport, state, dry_run=False)
                        except Exception as error:  # noqa: BLE001 - 清理阶段异常也不能裸崩
                            state.step_summaries["step_9_crashed"] = {"error_type": type(error).__name__}
                            _try_save_state(state_path, state)
                            print(
                                f"清理阶段抛出异常（{type(error).__name__}）：硬停止状态已落盘，"
                                "需要人工确认清理结果后再决定下一步；不得据此重试或跳过。",
                                file=sys.stderr,
                            )
                            report = build_report(outcomes, state, dry_run=dry_run)
                            print(json.dumps(report, ensure_ascii=False, indent=2))
                            return 3
                        state.step_summaries["step_9"] = cleanup_outcome.summary
                        outcomes.append(cleanup_outcome)
            elif any(o.step == 9 and not o.ok for o in outcomes):
                # 没有硬停止，但清理本身没做完（例如某个协作者删不掉）：如实报告，
                # 不能因为"没有命中硬停止条款"就顺势返回成功。
                exit_code = 3

            if not dry_run and not _try_save_state(state_path, state):
                return 1

            report = build_report(outcomes, state, dry_run=dry_run)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return exit_code
    except LockAcquisitionError as error:
        print(str(error), file=sys.stderr)
        return 1


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
