#!/usr/bin/env python3
"""宿主级容器健康告警（S-H2-3，产品负责人 D5 裁定：不引 Prometheus）。

# 这一层补的是什么盲区

`src/lingxi/apps/healthcheck` 与 `core/alerting.py` 的 `AlertingDuty` 都跑在
scheduler/gateway/worker-queue **自己的进程里**——它们能发现"数据库不可达"或
"主循环停摆"，但没有办法发现"承载这些进程的容器本身已经不健康、甚至已经不在
运行"，因为出问题的正是发出告警这条链路自己所在的进程。这个脚本刻意运行在
**容器之外**，直接读宿主 Docker 的判定结果，补的正是这一段"scheduler 自己挂了、
没有人知道"的盲区。二者的分工边界见 `deploy/监控告警.md`。

# 为什么是宿主脚本而不是仓库包的一部分

- **不 import `lingxi` 包**：这个脚本要能在 `src/lingxi` 所在的容器全部起不来、
  甚至 `git` 工作区都不在宿主上的情况下依然跑得动——它只依赖 Python3 标准库与
  `docker`/`curl` 等宿主命令行工具，由宿主 cron 直接调用，不进任何镜像。
- **不发起任何入站网络监听**：只读 `docker inspect`（宿主 Docker socket 权限），
  只发出站 HTTP 请求（飞书开放平台 API），不扩大攻击面。
- **凭据边界**：飞书应用凭据与管理群 chat_id 从 `--env-file` 指定的 `KEY=VALUE`
  文件读取，调用方必须保证该文件 0600 且属主为运行 cron 的账户——本脚本在读取前
  会先校验一次，不满足直接拒绝启动（不回显取到的权限位以外的任何内容）。凭据
  **不进 argv**（只有文件路径是参数，值本身不出现在命令行/进程列表里），**不进
  日志**（错误信息只报字段名与错误类别，不回显凭据或 chat_id 取值）。

# 触发条件（与 `docker inspect` 的判定对应）

- `State.Running == false`（容器存在但未运行，例如 `exited`/`created`）；
- 容器不存在（`docker inspect` 对该名字返回非零退出码）；
- `State.Health.Status == "unhealthy"`——**这一条本身已经隐含"持续"**：docker
  自己的 `retries`（当前部署三个目标服务均为 3）机制已经要求连续三次探测失败
  才会把状态翻成 `unhealthy`，这个脚本不需要再自己攒一次连续失败计数。

`State.Health.Status == "starting"`（尚在 `start_period` 宽限期内）与没有配置
`HEALTHCHECK` 的容器（`Health` 字段不存在）都**不**触发告警——前者是正常启动期，
后者是"这个容器本来就没有可比较的健康信号"，不能当成异常。

# 防骚扰与恢复通知

同一个容器、同一个故障原因（`missing`/`stopped`/`unhealthy`）只在**首次进入**
这个原因时告警一次；只要这个原因没变，后续每一轮 cron 调用都不会重复发送。
原因发生变化（例如从 `unhealthy` 变成 `missing`，容器在探测之间被整个删除了）
视为新事件，允许再发一次——两种原因指向的排查动作不同，合并成一条噪声更大的
消息不如各自单独一条。判定恢复正常（回到非触发状态）时发**一条**恢复通知，
随后清空该容器的记忆状态。这套去重靠 `--state-file` 指向的本地 JSON 文件承担，
只有**发送成功**之后才会落盘新状态——发送失败不落盘，保证下一轮 cron 会重新
尝试同一个事件，而不是"发送失败也当作已经告警过，从此再也不重试"。

处于告警态的容器如果观察到 `Health.Status == "starting"`（重启已开始、仍在
`start_period` 宽限期内），既不算新的触发，也**不**确认恢复——过早发一条恢复
通知，很可能几十秒后宽限期结束又立刻收到一条新的告警，制造"刚说恢复又说挂了"
的噪声。这种情况下记忆状态原样保留，等下一轮拿到 `healthy`/无健康检查配置这类
确定结果，或者再次落入触发条件，才会真正发消息。

# 发送失败与本机日志

飞书发送失败（网络错误、超时、飞书返回业务错误码）只写本地日志文件
（`--log-file`），不抛出未捕获异常、不让 cron 因为一次网络抖动而"崩掉"。这与
`docs/技术设计/代码框架.md` 「三、横切约定」里"四进程不写日志文件"的约定并不
冲突——那条约定管的是 `src/lingxi/apps/` 下的四个常驻/一次性进程，这个脚本不是
其中之一，它是宿主基础设施层，本来就要在"容器化的结构化输出到 stdout 会被谁
收集"这条链路之外独立留痕，因此刻意写本地文件。

# 单实例纪律

同一时刻只允许一个实例真正执行检查（`fcntl.flock` 独占锁，`--lock-file`）：
cron 调用间隔（1-2 分钟）与单轮最坏耗时（三个容器 × `docker inspect` + 至多
一次飞书调用，每步 `--timeout-seconds` 上限）正常不会重叠，但网络抖动导致
上一轮挂起时必须避免两个实例同时读写同一份状态文件、同时发送同一个事件的
告警——与 `AGENTS.md`"共享的外部通道同一时刻只允许一个客户端"同一条纪律。
拿不到锁不是故障，只是"上一轮还没跑完"，本轮安静让路、退出码 0。

# 退出码

- ``0``：本轮检查已执行完成（不代表所有容器都健康——健康结果体现在飞书消息与
  状态文件里，不体现在退出码里；cron 不应该因为发现了一个 unhealthy 容器就
  把这次调用当成"脚本出错"上报）。拿不到单实例锁、按设计静默跳过本轮，也算 0。
- ``2``：脚本自身故障，本轮未能完成检查（凭据文件缺失/权限不对/字段不全、
  `docker` 命令不可用、状态文件读写失败）——这类需要人工介入，值得让 cron 的
  日志/退出码体现出来。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认监控目标——本批交付默认覆盖的三个常驻服务容器名，对应
#: `deploy/compose.yaml` 的 `scheduler`/`gateway`/`worker-queue` 三个 service。
DEFAULT_CONTAINERS: tuple[str, ...] = (
    "lingxi-scheduler-1",
    "lingxi-gateway-1",
    "lingxi-worker-queue-1",
)

DEFAULT_STATE_FILE = "/opt/lingxi/monitoring/state.json"
DEFAULT_LOG_FILE = "/opt/lingxi/monitoring/host-monitor.log"
DEFAULT_LOCK_FILE = "/opt/lingxi/monitoring/host-monitor.lock"
DEFAULT_BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_TIMEOUT_SECONDS = 10.0

REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "LINGXI_FEISHU_APP_ID",
    "LINGXI_FEISHU_APP_SECRET",
    "LINGXI_ADMIN_GROUP_CHAT_ID",
)

REASON_OK = "healthy"
REASON_STARTING = "starting"
REASON_NO_HEALTHCHECK = "no_healthcheck"
REASON_MISSING = "missing"
REASON_STOPPED = "stopped"
REASON_UNHEALTHY = "unhealthy"

_REASON_LABEL: Mapping[str, str] = {
    REASON_MISSING: "容器不存在",
    REASON_STOPPED: "容器未运行",
    REASON_UNHEALTHY: "健康检查判定为 unhealthy",
}

ACTION_NONE = "none"
ACTION_ALERT = "alert"
ACTION_RECOVERY = "recovery"

#: 只有落在这两个原因时才能确认"已经恢复"。`REASON_STARTING` 刻意不在其中：
#: 一个容器被重启后会先经过 `starting`（`start_period` 宽限期），这时既不满足
#: 任何触发条件、也还没有拿到一次真正的健康检查结果——过早在这里发一条恢复
#: 通知，很可能几十秒后宽限期结束又立刻收到一条新的告警，制造"刚说恢复又说
#: 挂了"的噪声。见 `decide_action` 与其单测
#: `test_starting_after_alerting_stays_pending`。
_RECOVERY_REASONS = frozenset({REASON_OK, REASON_NO_HEALTHCHECK})

GROUP_CHAT_ID_PREFIX = "oc_"


class HostMonitorError(RuntimeError):
    """脚本自身的、需要人工介入的故障（配置、权限、宿主命令不可用等）。

    消息文本只包含错误类别与字段名，不回显任何凭据或取值——调用方在异常打印到
    本地日志文件时,不需要再额外脱敏一次。
    """


# ---------------------------------------------------------------------------
# 纯逻辑：状态判定与去重状态机
#
# 本节全部是不做网络/进程调用、不读环境变量的纯函数与不可变数据类，可以在没有
# docker、没有网络的机器上直接单测——这是 Trace 交付要求的"决策逻辑拆成可单测
# 的纯函数"的落点。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """从一次 `docker inspect` 结果里提炼出的、判定所需的最小事实集合。"""

    name: str
    exists: bool
    running: bool | None = None
    health_status: str | None = None


@dataclass(frozen=True)
class Classification:
    """这一次观察对应的分类结果：要不要触发、原因是什么。"""

    name: str
    reason: str
    trigger: bool


@dataclass(frozen=True)
class ContainerState:
    """状态文件里，单个容器记住的"上一次已经确认送达的告警态"。"""

    alerting: bool = False
    reason: str | None = None


def parse_inspect_entry(name: str, entry: Mapping[str, object] | None) -> Observation:
    """把一条 `docker inspect` JSON 结果（或 ``None``＝容器不存在）转成 Observation。

    只读 `State.Running` 与 `State.Health.Status` 两个字段；其余字段（镜像、
    网络、挂载等）与本脚本的判定无关，刻意不解析、不保留，减少这份观察结构
    意外携带敏感信息的空间。
    """

    if entry is None:
        return Observation(name=name, exists=False)
    state = entry.get("State")
    state = state if isinstance(state, Mapping) else {}
    running = state.get("Running")
    running = running if isinstance(running, bool) else None
    health = state.get("Health")
    health = health if isinstance(health, Mapping) else {}
    health_status = health.get("Status")
    health_status = health_status if isinstance(health_status, str) else None
    return Observation(name=name, exists=True, running=running, health_status=health_status)


def classify(observation: Observation) -> Classification:
    """三条触发条件的唯一判定入口（见模块文档「触发条件」一节）。"""

    if not observation.exists:
        return Classification(observation.name, REASON_MISSING, True)
    if observation.running is False:
        return Classification(observation.name, REASON_STOPPED, True)
    if observation.health_status == "unhealthy":
        return Classification(observation.name, REASON_UNHEALTHY, True)
    if observation.health_status == "starting":
        return Classification(observation.name, REASON_STARTING, False)
    if not observation.health_status:
        return Classification(observation.name, REASON_NO_HEALTHCHECK, False)
    return Classification(observation.name, REASON_OK, False)


def decide_action(
    classification: Classification, prior: ContainerState
) -> tuple[str, ContainerState]:
    """去重与恢复通知的状态机（见模块文档「防骚扰与恢复通知」一节）。

    返回 ``(action, target_state)``——``target_state`` 是"如果这次消息确认送达
    成功，应该落盘的新状态"，是否真的落盘由调用方在发送结果出来之后决定，本函数
    不做任何 I/O，也不知道发送有没有成功。
    """

    if classification.trigger:
        if prior.alerting and prior.reason == classification.reason:
            return ACTION_NONE, prior
        return ACTION_ALERT, ContainerState(alerting=True, reason=classification.reason)
    if prior.alerting and classification.reason in _RECOVERY_REASONS:
        return ACTION_RECOVERY, ContainerState(alerting=False, reason=None)
    if prior.alerting:
        # 仍在 `starting` 宽限期：不是新的触发，也还不能确认恢复，原样保留
        # 记忆状态，等下一轮拿到确定结果（healthy/no_healthcheck 或再次触发）。
        return ACTION_NONE, prior
    return ACTION_NONE, ContainerState(alerting=False, reason=None)


def render_message(action: str, classification: Classification, *, host: str, now: str) -> str:
    """渲染纯文本告警/恢复消息；不接受、也不可能带上任何业务正文或凭据。"""

    if action == ACTION_ALERT:
        label = _REASON_LABEL.get(classification.reason, classification.reason)
        return (
            f"[Lingxi 宿主监控] 告警\n"
            f"容器：{classification.name}\n"
            f"状态：{label}\n"
            f"主机：{host}\n"
            f"时间：{now}"
        )
    if action == ACTION_RECOVERY:
        return (
            f"[Lingxi 宿主监控] 恢复\n"
            f"容器：{classification.name}\n"
            f"状态：已恢复正常\n"
            f"主机：{host}\n"
            f"时间：{now}"
        )
    raise ValueError("仅 alert / recovery 两种动作需要渲染文本")


# ---------------------------------------------------------------------------
# I/O：docker inspect、凭据文件、状态文件、飞书发送、单实例锁
# ---------------------------------------------------------------------------


def docker_inspect_one(
    name: str, *, docker_bin: str = "docker", timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Mapping[str, object] | None:
    """探测单个容器；返回 ``None`` 表示"容器不存在"这一正常情况，不是错误。

    只有 `docker` 命令本身跑不起来（不在 PATH、挂起超时）或返回了无法解析的
    输出，才算脚本级故障并抛出 :class:`HostMonitorError`——那种情况下我们连
    "容器存不存在"这个最基本的事实都确认不了，不能悄悄当成"不存在"处理。
    """

    try:
        proc = subprocess.run(
            [docker_bin, "inspect", name],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise HostMonitorError(f"docker_binary_not_found:{docker_bin}") from error
    except subprocess.TimeoutExpired as error:
        raise HostMonitorError("docker_inspect_timeout") from error
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise HostMonitorError("docker_inspect_invalid_json") from error
    if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        return None
    return data[0]


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析形如 ``KEY=VALUE`` 的凭据文件；不做变量展开，只做可选引号剥离。"""

    result: dict[str, str] = {}
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HostMonitorError(f"env_file_unreadable:{type(error).__name__}") from error
    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise HostMonitorError(f"env_file_malformed_line:{lineno}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not key:
            raise HostMonitorError(f"env_file_malformed_line:{lineno}")
        result[key] = value
    return result


def load_credentials(path: Path) -> dict[str, str]:
    """校验权限、解析并抽取本脚本需要的三个字段；错误信息不回显任何取值。"""

    if not path.is_file():
        raise HostMonitorError("env_file_not_found")
    mode = os.stat(path).st_mode & 0o777
    if mode != 0o600:
        raise HostMonitorError(f"env_file_permission_unsafe:{oct(mode)}")
    raw = _parse_env_file(path)
    missing = [key for key in REQUIRED_ENV_KEYS if not raw.get(key)]
    if missing:
        raise HostMonitorError(f"env_file_missing_keys:{','.join(missing)}")
    chat_id = raw["LINGXI_ADMIN_GROUP_CHAT_ID"].strip()
    if not chat_id.startswith(GROUP_CHAT_ID_PREFIX) or any(ch.isspace() for ch in chat_id):
        raise HostMonitorError("env_admin_group_chat_id_invalid_format")
    return {
        "app_id": raw["LINGXI_FEISHU_APP_ID"],
        "app_secret": raw["LINGXI_FEISHU_APP_SECRET"],
        "chat_id": chat_id,
    }


def load_state(path: Path) -> dict[str, ContainerState]:
    """读取状态文件；不存在、损坏或格式不对都按"从空状态开始"处理，不崩溃。"""

    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, ContainerState] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        reason = value.get("reason")
        result[str(name)] = ContainerState(
            alerting=bool(value.get("alerting", False)),
            reason=reason if isinstance(reason, str) else None,
        )
    return result


def save_state(path: Path, states: Mapping[str, ContainerState]) -> None:
    """原子落盘（写临时文件后 `os.replace`），避免并发/崩溃留下半截 JSON。"""

    payload = {
        name: {"alerting": state.alerting, "reason": state.reason}
        for name, state in states.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as error:
        raise HostMonitorError(f"state_file_write_failed:{type(error).__name__}") from error


def _feishu_tenant_access_token(
    base_url: str, app_id: str, app_secret: str, *, timeout_seconds: float
) -> str:
    request = urllib.request.Request(
        f"{base_url}/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise HostMonitorError(f"feishu_token_transport_error:{type(error).__name__}") from error
    if not isinstance(payload, Mapping):
        raise HostMonitorError("feishu_token_invalid_response_shape")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise HostMonitorError(f"feishu_token_error_code_{code}")
    token = payload.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise HostMonitorError("feishu_token_missing")
    return token


def feishu_send_text(
    *,
    base_url: str,
    chat_id: str,
    app_id: str,
    app_secret: str,
    text: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """向 `chat_id` 发一条纯文本消息；`app_secret` 只出现在请求体，不进日志。

    与 `src/lingxi/adapters/feishu_group_message.py` 的 `FeishuGroupMessages`
    是同一个 `im/v1/messages?receive_id_type=chat_id` 接口、同一种"先换令牌、
    每次现取不缓存"姿势——这里独立重写一份而不是 import 那个模块，理由见模块
    文档「为什么是宿主脚本而不是仓库包的一部分」。
    """

    token = _feishu_tenant_access_token(base_url, app_id, app_secret, timeout_seconds=timeout_seconds)
    body = json.dumps(
        {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/im/v1/messages?receive_id_type=chat_id",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise HostMonitorError(f"feishu_send_transport_error:{type(error).__name__}") from error
    if not isinstance(payload, Mapping):
        raise HostMonitorError("feishu_send_invalid_response_shape")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise HostMonitorError(f"feishu_send_error_code_{code}")


@contextlib.contextmanager
def single_instance_lock(path: Path) -> Iterator[bool]:
    """`fcntl.flock` 独占锁；拿不到锁时 ``yield False``，不是脚本故障。

    见模块文档「单实例纪律」——与 `AGENTS.md` 共享外部通道同一时刻只允许一个
    客户端是同一条纪律：这里的共享资源是状态文件与"同一事件不重复告警"这条
    承诺，两个并发实例同时读旧状态、同时判定"需要告警"，会绕开去重机制各发
    一条重复消息。
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _configure_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("lingxi.host_monitor")
    logger.setLevel(logging.INFO)
    # 先关闭再摘掉旧 handler：单个 cron 调用只会走到这里一次，但 `run()` 也被
    # 测试在同一个进程里反复调用，不先 `close()` 会一次次打开新的文件描述符，
    # 只是 `handlers.clear()` 丢掉引用，句柄永远不释放。
    for old_handler in logger.handlers:
        old_handler.close()
    logger.handlers.clear()
    log_path = Path(log_file)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="宿主级容器健康告警：docker inspect 判定 + 飞书管理群通知（S-H2-3，D5 裁定）。"
    )
    parser.add_argument(
        "--env-file",
        required=True,
        help="凭据文件路径，须 0600；内容为 LINGXI_FEISHU_APP_ID / "
        "LINGXI_FEISHU_APP_SECRET / LINGXI_ADMIN_GROUP_CHAT_ID 三行 KEY=VALUE",
    )
    parser.add_argument(
        "--containers",
        nargs="+",
        default=list(DEFAULT_CONTAINERS),
        help=f"要监控的容器名列表，默认 {' '.join(DEFAULT_CONTAINERS)}",
    )
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="去重状态文件路径")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE, help="本地日志文件路径")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help="单实例锁文件路径")
    parser.add_argument("--docker-bin", default="docker", help="docker 可执行文件名或路径")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="飞书开放平台 base_url")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="docker inspect 与飞书 HTTP 调用的单次超时秒数",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只判定、打日志，不真实发送飞书消息、不落盘状态变化（用于安装后先验证判定逻辑）",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logger = _configure_logger(args.log_file)

    with single_instance_lock(Path(args.lock_file)) as acquired:
        if not acquired:
            logger.warning("拿不到单实例锁，上一轮可能还在执行，本轮安静跳过")
            return 0

        try:
            credentials = load_credentials(Path(args.env_file))
        except HostMonitorError as error:
            logger.error("凭据文件校验失败，本轮未执行任何检查 error=%s", error)
            return 2

        try:
            if shutil.which(args.docker_bin) is None:
                raise HostMonitorError(f"docker_binary_not_found:{args.docker_bin}")
        except HostMonitorError as error:
            logger.error("docker 命令不可用，本轮未执行任何检查 error=%s", error)
            return 2

        state_path = Path(args.state_file)
        states = load_state(state_path)

        host = socket.gethostname()
        changed = False
        fatal = False

        for name in args.containers:
            try:
                entry = docker_inspect_one(
                    name, docker_bin=args.docker_bin, timeout_seconds=args.timeout_seconds
                )
            except HostMonitorError as error:
                logger.error("docker inspect 执行失败，本轮跳过该容器 container=%s error=%s", name, error)
                fatal = True
                continue

            observation = parse_inspect_entry(name, entry)
            classification = classify(observation)
            prior = states.get(name, ContainerState())
            action, target_state = decide_action(classification, prior)

            if action == ACTION_NONE:
                continue

            text = render_message(action, classification, host=host, now=_now_iso())

            if args.dry_run:
                logger.info(
                    "dry-run，未真实发送 container=%s action=%s reason=%s",
                    name,
                    action,
                    classification.reason,
                )
                continue

            try:
                feishu_send_text(
                    base_url=args.base_url,
                    chat_id=credentials["chat_id"],
                    app_id=credentials["app_id"],
                    app_secret=credentials["app_secret"],
                    text=text,
                    timeout_seconds=args.timeout_seconds,
                )
            except HostMonitorError as error:
                logger.error(
                    "告警发送失败，状态未落盘，下一轮 cron 会重试 container=%s action=%s error=%s",
                    name,
                    action,
                    error,
                )
                continue

            states[name] = target_state
            changed = True
            logger.info(
                "告警已发送 container=%s action=%s reason=%s", name, action, classification.reason
            )

        if changed and not args.dry_run:
            try:
                save_state(state_path, states)
            except HostMonitorError as error:
                logger.error("状态文件写入失败，下一轮可能重复告警 error=%s", error)
                fatal = True

        return 2 if fatal else 0


def main() -> int:  # pragma: no cover - 由 __main__ 调用，逻辑全部委托给 run()
    return run()


if __name__ == "__main__":
    sys.exit(main())
