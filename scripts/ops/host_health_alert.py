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
- **不发起任何入站网络监听**：只读 `docker container inspect --format
  '{{json .State}}'`（宿主 Docker socket 权限，只取 `State` 字段，不读含
  凭据的 `Config.Env`，也不会被同名的非容器对象误命中），只发出站 HTTP
  请求（飞书开放平台 API），不扩大攻击面。
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

# 可选的资源阈值检查（S-RC20-410，Issue #410）

``--enable-resource-thresholds`` 打开后，在既有的三容器健康检查之外，额外做
三项独立判定：磁盘用量、系统负载、`scripts/ops/monitoring/` 两个采样脚本
（`resource_sample.sh`/`db_business_sample.sh`）产出的本机文件是否停更。默认
关闭，不改变既有部署（未传这个参数的现有 crontab/timer 行为与此前完全一致）。

这三项复用同一套"简短警报一句话"渲染与飞书群通道，但**去重规则与容器判据不同
一份状态**：容器判据是"原因变化即新事件"，阈值判据是"连续 N 轮都超过阈值才算
真正告警"（磁盘/停更连续 1 轮即告警，负载默认要求连续 3 轮——对应issue 原文
"load 持续 >2×核数"里的"持续"，磁盘用量与停更本身已经是持续性状态，不需要
额外的多轮确认）。两套状态各自存自己的状态文件，互不干扰、互不共享去重记忆。

这三项检查只读取本机数据（`/proc`、`shutil.disk_usage`、采样文件 mtime），不
依赖 `scripts/ops/monitoring/` 里任何脚本正在运行——即使采样管线整体挂了，
"采样文件停更"这条判据本身依然能独立算出来并告警,这正是它存在的意义。

# 拉取代理单元检查（默认开启，``--disable-release-pull-check`` 关闭）

拉取代理（`lingxi-release-pull.timer` 每五分钟触发一次 `.service`）是无人值守升级
的唯一入口，它自己停摆时没有任何进程会替它出声。本检查挂在阈值副线上（同一份
`--threshold-state-file` 去重记忆、同一套告警 / 恢复语义），只读任何用户都能查的
systemd 属性（`systemctl show`），**不读代理状态账**：代理以 root 运行，状态账在
root 0700 目录里，本脚本按部署用户运行读不到。三个判据各自独立去重：timer 不是
`active`；最近一次留痕（timer 上一次触发与 service 上一次结束二者取新）距今超过
`--release-pull-stale-minutes`（默认 15 分钟 = 三个轮询周期）；上一轮 `Result`
不是 `success` 且退出码非零。判的是**触发**过期而不是完成过期：单轮可长达一小时
（含部署观察），service 仍在运行时不判过期、也不判上一轮失败。任何一步读不到
（`systemctl` 不存在 / 非零退出 / 属性缺失或解析不了）都以「未知」形态告警一次
并同样去重，不得静默当作正常。本检查不改变容器主线的退出码语义。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

#: 可选阈值检查（S-RC20-410）的默认值，独立于上面三个容器检查的状态/日志路径，
#: 避免两套判据共享同一份状态文件导致 JSON 形状混淆。
DEFAULT_THRESHOLD_STATE_FILE = "/opt/lingxi/monitoring/threshold-state.json"
DEFAULT_MONITORING_DIR = "/var/log/lingxi/monitoring"
DEFAULT_DISK_MOUNT = "/"
DEFAULT_DISK_THRESHOLD_PERCENT = 85.0
DEFAULT_LOAD_MULTIPLIER = 2.0
DEFAULT_LOAD_CONSECUTIVE = 3
DEFAULT_STALENESS_THRESHOLD_MINUTES = 10.0

#: 拉取代理单元检查：单元基名（派生 `.timer` / `.service`）与留痕过期阈值。15 分钟
#: 是 timer 五分钟周期的三倍——一次错过可能只是 AccuracySec 抖动，连续三个周期没有
#: 任何留痕才算停摆。
DEFAULT_RELEASE_PULL_UNIT = "lingxi-release-pull"
DEFAULT_RELEASE_PULL_STALE_MINUTES = 15.0

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


def parse_inspect_entry(name: str, state: Mapping[str, object] | None) -> Observation:
    """把 `docker container inspect --format '{{json .State}}'` 的结果
    （或 ``None``＝容器不存在）转成 Observation。

    入参已经只是 `State` 那一段（`docker_inspect_one` 用 `--format` 在
    docker CLI 那一步就把其余字段——尤其是含凭据的 `Config.Env`——过滤掉了，
    这里不需要也不应该再从更大的结构里剥一层）。只读 `Running` 与
    `Health.Status` 两个字段。
    """

    if state is None:
        return Observation(name=name, exists=False)
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
            f"[BI Plus 宿主监控] 告警\n"
            f"容器：{classification.name}\n"
            f"状态：{label}\n"
            f"主机：{host}\n"
            f"时间：{now}"
        )
    if action == ACTION_RECOVERY:
        return (
            f"[BI Plus 宿主监控] 恢复\n"
            f"容器：{classification.name}\n"
            f"状态：已恢复正常\n"
            f"主机：{host}\n"
            f"时间：{now}"
        )
    raise ValueError("仅 alert / recovery 两种动作需要渲染文本")


# ---------------------------------------------------------------------------
# 可选阈值检查的纯逻辑（S-RC20-410，Issue #410）：磁盘用量、系统负载、采样文件
# 停更。与上面的容器判据共用 ACTION_* 常量，但去重状态的形状不同，见模块文档
# 「可选的资源阈值检查」一节，单独一个 ThresholdState 而不是复用 ContainerState。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdState:
    """阈值检查各自独立的连续触发计数与告警态记忆，与 ContainerState 分开维护。"""

    alerting: bool = False
    consecutive: int = 0


#: 三项阈值检查在状态字典里各自的 key，用于状态文件与日志里区分是哪一项。
THRESHOLD_DISK = "disk"
THRESHOLD_LOAD = "load"
THRESHOLD_STALE_RESOURCE = "stale_resource_sample"
THRESHOLD_STALE_DB_BUSINESS = "stale_db_business_sample"


def classify_threshold(
    breached: bool, prior: ThresholdState, *, consecutive_required: int
) -> tuple[str, ThresholdState]:
    """通用阈值判定：连续 `consecutive_required` 次观测都超过阈值才真正告警一次
    ——与 docker healthcheck.retries 同一条"连续才算数"纪律：任意一次没有超过
    阈值都会把连续计数清零重新数起，不做"允许中间抖动一次"的宽松处理，保持判定
    规则简单可推理。

    返回的 `next_state` 是"这一轮观测之后，状态**应该**变成什么"——它的
    `consecutive` 字段是纯观测事实，不受"消息有没有发送成功"影响；但 `alerting`
    字段表示"已经真正通知过"，调用方在发送失败时应该保留 `prior.alerting`、
    只采纳 `next_state.consecutive`，让下一轮在计数仍然达标时重新尝试发送
    （与容器判据"发送失败不落盘，下一轮据此重试"同一条纪律）。
    """

    if breached:
        consecutive = prior.consecutive + 1
        if consecutive >= consecutive_required:
            action = ACTION_NONE if prior.alerting else ACTION_ALERT
            return action, ThresholdState(alerting=True, consecutive=consecutive)
        return ACTION_NONE, ThresholdState(alerting=prior.alerting, consecutive=consecutive)
    if prior.alerting:
        return ACTION_RECOVERY, ThresholdState(alerting=False, consecutive=0)
    return ACTION_NONE, ThresholdState(alerting=False, consecutive=0)


def render_threshold_message(
    action: str, *, label: str, detail: str, host: str, now: str, category: str = "资源监控"
) -> str:
    """渲染阈值告警/恢复的纯文本消息，保持"简短警报一句话"原则（issue #410 目标
    形态）：一个判据名 + 一句话细节，不像容器告警那样需要从取值域里查文案。

    `category` 只决定首行的分类词：资源三项沿用「资源监控」，拉取代理单元检查用
    「宿主监控」——它判的是宿主上一个 systemd 单元活不活，不是资源水位。
    """

    if action == ACTION_ALERT:
        return f"[BI Plus {category}] 告警\n{label}：{detail}\n主机：{host}\n时间：{now}"
    if action == ACTION_RECOVERY:
        return f"[BI Plus {category}] 恢复\n{label}：已恢复正常\n主机：{host}\n时间：{now}"
    raise ValueError("仅 alert / recovery 两种动作需要渲染文本")


# ---------------------------------------------------------------------------
# 拉取代理单元检查的纯逻辑：从 systemd 属性提炼观察值、按三个判据 + 未知形态产出
# 阈值检查项。全部不做进程调用，可以在没有 systemd 的机器上直接单测。
# ---------------------------------------------------------------------------

RELEASE_PULL_TIMER_INACTIVE = "release_pull_timer_inactive"
RELEASE_PULL_TRIGGER_STALE = "release_pull_trigger_stale"
RELEASE_PULL_LAST_RUN_FAILED = "release_pull_last_run_failed"
RELEASE_PULL_UNKNOWN = "release_pull_unknown"

_RELEASE_PULL_LABEL: Mapping[str, str] = {
    RELEASE_PULL_TIMER_INACTIVE: "拉取代理定时器未激活",
    RELEASE_PULL_TRIGGER_STALE: "拉取代理留痕过期",
    RELEASE_PULL_LAST_RUN_FAILED: "拉取代理上一轮失败",
    RELEASE_PULL_UNKNOWN: "拉取代理状态未知",
}

#: 拉取代理四个键的消息分类词；其余阈值键沿用渲染函数的默认值。
_THRESHOLD_CATEGORY: Mapping[str, str] = dict.fromkeys(_RELEASE_PULL_LABEL, "宿主监控")

#: oneshot 服务执行中是 `activating`；其余几个是保守起见一并视为"仍在跑"的状态。
_SERVICE_RUNNING_STATES = frozenset({"activating", "active", "reloading", "deactivating"})

#: `systemctl show` 在 `TZ=UTC` 下打印的时间戳形如 `Sun 2026-09-20 06:40:04 UTC`；
#: 星期缩写允许缺省，秒后允许小数。
_SYSTEMD_TIMESTAMP = re.compile(
    r"^(?:[A-Za-z]{3}\s+)?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+UTC$"
)
_TIMER_PROPERTIES = ("ActiveState", "LoadState", "LastTriggerUSec")
_SERVICE_PROPERTIES = ("ActiveState", "Result", "ExecMainStatus", "ExecMainExitTimestamp")


@dataclass(frozen=True)
class ReleasePullObservation:
    """判定拉取代理单元所需的最小事实集合，全部来自任何用户可读的 systemd 属性。"""

    unit: str
    timer_active_state: str
    timer_load_state: str
    last_trigger: datetime | None
    service_active_state: str
    service_result: str
    service_exit_status: int
    service_exit_at: datetime | None


def parse_systemd_timestamp(value: str | None) -> datetime | None:
    """把 `systemctl show`（`TZ=UTC`）的时间戳文本转成带时区的 datetime。

    空串、`n/a`、`0` 与属性整行缺失（systemd 对未设置的时间戳就是这么打印的）都
    返回 ``None`` 表示"尚无此事件"；非空却对不上格式的文本抛 ``ValueError``，交由
    调用方按「未知」形态处理，不猜测。
    """

    text = (value or "").strip()
    if text in ("", "n/a", "0"):
        return None
    match = _SYSTEMD_TIMESTAMP.match(text)
    if match is None:
        raise ValueError("systemd_timestamp_unparseable")
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _format_utc(moment: datetime | None) -> str:
    if moment is None:
        return "n/a"
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def release_pull_trace_at(observation: ReleasePullObservation) -> datetime | None:
    """最近一次留痕时刻 = timer 上一次触发与 service 上一次结束二者取新。

    只看触发时刻会在一轮长部署刚结束、下一个五分钟刻度还没到的窗口里把"刚跑完"
    误判成"过期"；把 service 结束时刻并进来，这个窗口就消失了。
    """

    candidates = [t for t in (observation.last_trigger, observation.service_exit_at) if t]
    return max(candidates) if candidates else None


def describe_release_pull(
    observation: ReleasePullObservation, *, now: datetime, stale_minutes: float
) -> str:
    """告警正文里的一句话事实：单元名、两个单元的状态、上一次触发（UTC）、上一轮结果、
    留痕年龄与阈值。只含 systemd 属性取值，不含路径、命令与日志原文。
    """

    timer_state = observation.timer_active_state
    if observation.timer_load_state not in ("", "loaded"):
        timer_state = f"{timer_state}（{observation.timer_load_state}）"
    trace_at = release_pull_trace_at(observation)
    age = "n/a" if trace_at is None else f"{(now - trace_at).total_seconds() / 60:.1f} 分钟前"
    return (
        f"单元 {observation.unit}：timer={timer_state}，service={observation.service_active_state}，"
        f"上一次触发 {_format_utc(observation.last_trigger)}，"
        f"上一轮 {observation.service_result}/{observation.service_exit_status}，"
        f"留痕 {age}（阈值 {stale_minutes:.0f} 分钟）"
    )


def judge_release_pull(
    observation: ReleasePullObservation, *, now: datetime, stale_minutes: float
) -> list[tuple[str, str, bool, str, int]]:
    """三个判据各自独立成阈值检查项 `(key, label, breached, detail, consecutive_required)`。

    - timer 不是 `active` → 定时器未激活；此时不再判留痕过期（停摆的根因已经报过，
      不为同一件事再发第二条）。
    - service 正在运行 → 留痕按未过期处理（一轮已经开始就是最新留痕，先前的过期
      告警随之恢复）；上一轮失败这一轮不出结论（运行中 systemd 会把 `Result` /
      退出码重置为成功，要等它跑完）。
    - 从未触发也从未结束（两个时间戳都没有）按过期处理。
    """

    detail = describe_release_pull(observation, now=now, stale_minutes=stale_minutes)
    timer_active = observation.timer_active_state == "active"
    running = observation.service_active_state in _SERVICE_RUNNING_STATES
    checks = [
        (
            RELEASE_PULL_TIMER_INACTIVE,
            _RELEASE_PULL_LABEL[RELEASE_PULL_TIMER_INACTIVE],
            not timer_active,
            detail,
            1,
        )
    ]
    if timer_active:
        trace_at = release_pull_trace_at(observation)
        stale = not running and (
            trace_at is None or (now - trace_at) > timedelta(minutes=stale_minutes)
        )
        checks.append(
            (
                RELEASE_PULL_TRIGGER_STALE,
                _RELEASE_PULL_LABEL[RELEASE_PULL_TRIGGER_STALE],
                stale,
                detail,
                1,
            )
        )
    if not running:
        failed = observation.service_result != "success" and observation.service_exit_status != 0
        checks.append(
            (
                RELEASE_PULL_LAST_RUN_FAILED,
                _RELEASE_PULL_LABEL[RELEASE_PULL_LAST_RUN_FAILED],
                failed,
                detail,
                1,
            )
        )
    return checks


# ---------------------------------------------------------------------------
# I/O：docker inspect、凭据文件、状态文件、飞书发送、单实例锁
# ---------------------------------------------------------------------------


#: `docker container inspect` 在容器确实不存在时的错误文案子串（跨常见 docker
#: CLI 版本，`docker container inspect` 报 `No such container`，通用 `docker
#: inspect` 报 `No such object`——两者都判定为"正常的不存在"，不是脚本故障）。
_NOT_FOUND_STDERR_MARKERS: tuple[str, ...] = ("No such container", "No such object")


def docker_inspect_one(
    name: str, *, docker_bin: str = "docker", timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Mapping[str, object] | None:
    """探测单个容器的 `State` 字段；返回 ``None`` 表示"容器不存在"这一正常情况。

    用 ``docker container inspect --format '{{json .State}}'``，而不是裸
    ``docker inspect``，收紧两层（独立审查 P2-1/P2-2）：

    1. **`container inspect` 只在容器命名空间里查找**：裸 `docker inspect`
       跨镜像/网络/卷/容器等多种对象类型按名字查找，一旦有同名的非容器对象
       存在会被优先命中、返回一份与容器毫不相关的结构，`State` 字段读不到，
       静默误判成"容器不存在"。
    2. **`--format '{{json .State}}'` 只取 `State` 字段**：裸 `docker
       inspect` 的完整输出含 `Config.Env`——`scheduler`/`gateway`/
       `worker-queue` 的容器环境变量正装着数据库连接串、Fernet 密钥与飞书
       应用密钥，这个宿主脚本没有任何理由读到它们，即使读到后只取 `State`
       也不该让凭据先落进这个进程的内存/子进程输出里。

    `returncode != 0` 时区分两种情况（P2-3）：stderr 提示"没有这个对象/容器"
    是正常情况（容器确实不存在），返回 `None`；其余任何非零退出（Docker
    daemon 不可达、权限不足等）都是脚本自身故障，抛出 `HostMonitorError`，
    交由调用方以 `exit=2` 收尾、**不对这一轮的观察结果做任何判定**——不能把
    "问不到 daemon"悄悄当成"容器不存在"，那样会在 daemon 短暂抖动又恢复时，
    把"从来没问到过"误判成一次"从缺失变健康"的假恢复。
    """

    try:
        proc = subprocess.run(
            [docker_bin, "container", "inspect", "--format", "{{json .State}}", name],
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
        stderr = proc.stderr or ""
        if any(marker in stderr for marker in _NOT_FOUND_STDERR_MARKERS):
            return None
        raise HostMonitorError(f"docker_inspect_daemon_error:{proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise HostMonitorError("docker_inspect_invalid_json") from error
    if not isinstance(data, Mapping):
        return None
    return data


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
    """校验权限与属主、解析并抽取本脚本需要的三个字段；错误信息不回显任何取值。

    模块文档「凭据边界」写的是"调用方必须保证该文件 0600 **且属主为运行 cron
    的账户**"——此前的实现只核对了权限位，没有核对属主，文档口径与代码口径
    不一致（独立审查 P2-4）：0600 只挡住"其他账户能不能读"，挡不住"这个文件
    其实属于另一个账户，运行 cron 的账户凭某种历史原因（例如属主账户被删）
    仍然读得到"这类错配。两项一起核对才是文档承诺的边界。
    """

    if not path.is_file():
        raise HostMonitorError("env_file_not_found")
    file_stat = os.stat(path)
    mode = file_stat.st_mode & 0o777
    if mode != 0o600:
        raise HostMonitorError(f"env_file_permission_unsafe:{oct(mode)}")
    if file_stat.st_uid != os.getuid():
        raise HostMonitorError("env_file_owner_mismatch")
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
        name: {"alerting": state.alerting, "reason": state.reason} for name, state in states.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as error:
        raise HostMonitorError(f"state_file_write_failed:{type(error).__name__}") from error


def load_threshold_state(path: Path) -> dict[str, ThresholdState]:
    """与 `load_state` 同一套"缺失/损坏都当空状态"纪律，形状换成 ThresholdState。"""

    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, ThresholdState] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        consecutive = value.get("consecutive", 0)
        result[str(name)] = ThresholdState(
            alerting=bool(value.get("alerting", False)),
            consecutive=consecutive
            if isinstance(consecutive, int) and not isinstance(consecutive, bool)
            else 0,
        )
    return result


def save_threshold_state(path: Path, states: Mapping[str, ThresholdState]) -> None:
    """与 `save_state` 同一套原子落盘手法，独立文件、不与容器状态混在一起。"""

    payload = {
        name: {"alerting": state.alerting, "consecutive": state.consecutive}
        for name, state in states.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as error:
        raise HostMonitorError(
            f"threshold_state_file_write_failed:{type(error).__name__}"
        ) from error


def read_disk_usage_percent(mount: str) -> float:
    """磁盘已用百分比；只依赖标准库 `shutil.disk_usage`，不额外拉起 `df` 子进程。"""

    usage = shutil.disk_usage(mount)
    if usage.total <= 0:
        return 0.0
    return usage.used / usage.total * 100


def read_load_per_cpu() -> tuple[float, int]:
    """返回 `(load1, cpu_count)`；判定用的"倍数"由调用方自己算，这里只取原始值，
    方便渲染消息时同时展示两个数字，不是只展示一个已经算好的比值。
    """

    load1 = os.getloadavg()[0]
    cpu_count = os.cpu_count() or 1
    return load1, cpu_count


def read_sample_age_seconds(
    monitoring_dir: Path, file_prefix: str, *, now: datetime
) -> float | None:
    """采样文件停更判定：取"今天"与"昨天"两个按 UTC 日期切分的候选文件（对应
    `scripts/ops/monitoring/resource_sample.sh` / `db_business_sample.sh` 的按日
    切分约定）里较新的 mtime，返回它与 `now` 的差值（秒）。

    同时看两个候选是为了避免"刚过 UTC 零点、今天的文件还没写出第一行"这类边界
    情况被误判成"停更"——采样脚本每 1-5 分钟才追加一行，零点前后那一小段窗口里
    今天的文件本就该是空的，此时昨天文件的 mtime 仍然是唯一有意义的参照。两个
    候选都不存在时返回 ``None``，由调用方决定怎么处理（通常等同于"从未采样过"，
    但那是调用方的判断，不属于本函数职责）。
    """

    mtimes: list[float] = []
    for day_offset in (0, 1):
        date_str = (now - timedelta(days=day_offset)).strftime("%Y%m%d")
        candidate = monitoring_dir / f"{file_prefix}-{date_str}.log"
        try:
            mtimes.append(candidate.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return now.timestamp() - max(mtimes)


def _systemctl_show(
    unit: str, properties: Sequence[str], *, systemctl_bin: str, timeout_seconds: float
) -> dict[str, str]:
    """`systemctl show --all -p … <unit>` 读成 `{属性: 文本}`。

    `show` 对已停止、甚至不存在（`LoadState=not-found`）的单元都以 0 退出并照常打印
    属性，非零退出只剩"问不到 systemd 本身"这一种含义——与 `is-active` 不同，后者
    对非 active 单元本来就非零退出，无法当作读取失败的信号。`--all` 让未设置的时间
    戳属性也打印出来（值为空），否则旧版 systemd 会整行省略。子进程固定 `TZ=UTC`，
    时间戳文本才有确定的时区可解析。错误信息只带类别，不带可执行文件路径。
    """

    argv = [systemctl_bin, "--no-pager", "show", "--all", "--property", ",".join(properties), unit]
    env = {**os.environ, "TZ": "UTC", "LC_ALL": "C"}
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False, env=env
        )
    except FileNotFoundError as error:
        raise HostMonitorError("systemctl_not_found") from error
    except subprocess.TimeoutExpired as error:
        raise HostMonitorError("systemctl_timeout") from error
    if proc.returncode != 0:
        raise HostMonitorError(f"systemctl_exit_{proc.returncode}")
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key.strip()] = value.strip()
    return result


def _required_property(properties: Mapping[str, str], name: str) -> str:
    value = properties.get(name, "")
    if not value:
        raise HostMonitorError(f"property_missing:{name}")
    return value


def _timestamp_property(properties: Mapping[str, str], name: str) -> datetime | None:
    try:
        return parse_systemd_timestamp(properties.get(name))
    except ValueError as error:
        raise HostMonitorError(f"property_unparseable:{name}") from error


def read_release_pull_observation(
    unit: str, *, systemctl_bin: str = "systemctl", timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> ReleasePullObservation:
    """两次 `systemctl show`（timer 与 service）拼成一份观察值；任何一步读不到都抛
    `HostMonitorError`，由调用方以「未知」形态告警，绝不把读不到当成正常。
    """

    base = unit.removesuffix(".timer").removesuffix(".service")
    timer = _systemctl_show(
        f"{base}.timer",
        _TIMER_PROPERTIES,
        systemctl_bin=systemctl_bin,
        timeout_seconds=timeout_seconds,
    )
    service = _systemctl_show(
        f"{base}.service",
        _SERVICE_PROPERTIES,
        systemctl_bin=systemctl_bin,
        timeout_seconds=timeout_seconds,
    )
    exit_status_text = _required_property(service, "ExecMainStatus")
    try:
        exit_status = int(exit_status_text)
    except ValueError as error:
        raise HostMonitorError("property_unparseable:ExecMainStatus") from error
    return ReleasePullObservation(
        unit=base,
        timer_active_state=_required_property(timer, "ActiveState"),
        timer_load_state=timer.get("LoadState", ""),
        last_trigger=_timestamp_property(timer, "LastTriggerUSec"),
        service_active_state=_required_property(service, "ActiveState"),
        service_result=_required_property(service, "Result"),
        service_exit_status=exit_status,
        service_exit_at=_timestamp_property(service, "ExecMainExitTimestamp"),
    )


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

    token = _feishu_tenant_access_token(
        base_url, app_id, app_secret, timeout_seconds=timeout_seconds
    )
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
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


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
    parser.add_argument(
        "--enable-resource-thresholds",
        action="store_true",
        help="额外检查磁盘用量、系统负载与 scripts/ops/monitoring/ 采样文件是否停更三项阈值"
        "（默认关闭，见模块文档「可选的资源阈值检查」一节；--dry-run 同样适用于这三项）",
    )
    parser.add_argument(
        "--monitoring-dir",
        default=DEFAULT_MONITORING_DIR,
        help="resource_sample.sh/db_business_sample.sh 的本机样本文件目录，用于停更判定",
    )
    parser.add_argument(
        "--threshold-state-file",
        default=DEFAULT_THRESHOLD_STATE_FILE,
        help="阈值检查的去重状态文件路径，与 --state-file 是两份独立文件",
    )
    parser.add_argument("--disk-mount", default=DEFAULT_DISK_MOUNT, help="磁盘用量检查的挂载点")
    parser.add_argument(
        "--disk-threshold-percent",
        type=float,
        default=DEFAULT_DISK_THRESHOLD_PERCENT,
        help="磁盘已用超过这个百分比告警",
    )
    parser.add_argument(
        "--load-multiplier",
        type=float,
        default=DEFAULT_LOAD_MULTIPLIER,
        help="load1 超过「核数 × 这个倍数」计入一次超阈值观测",
    )
    parser.add_argument(
        "--load-consecutive",
        type=int,
        default=DEFAULT_LOAD_CONSECUTIVE,
        help="负载连续超阈值多少轮才算「持续」并真正告警",
    )
    parser.add_argument(
        "--staleness-threshold-minutes",
        type=float,
        default=DEFAULT_STALENESS_THRESHOLD_MINUTES,
        help="resource/db_business 采样文件超过多少分钟没有新样本视为停更",
    )
    parser.add_argument(
        "--disable-release-pull-check",
        action="store_true",
        help="关闭拉取代理单元检查（默认开启：timer 非 active / 留痕过期 / 上一轮失败 / 读不到"
        "以未知形态告警，去重状态与阈值检查共用 --threshold-state-file）",
    )
    parser.add_argument(
        "--release-pull-unit",
        default=DEFAULT_RELEASE_PULL_UNIT,
        help=f"拉取代理单元基名，派生 .timer / .service，默认 {DEFAULT_RELEASE_PULL_UNIT}",
    )
    parser.add_argument(
        "--release-pull-stale-minutes",
        type=float,
        default=DEFAULT_RELEASE_PULL_STALE_MINUTES,
        help="拉取代理最近一次留痕（timer 触发 / service 结束取新）超过多少分钟视为过期",
    )
    parser.add_argument(
        "--systemctl-bin", default="systemctl", help="systemctl 可执行文件名或路径（测试注入用）"
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
                logger.error(
                    "docker inspect 执行失败，本轮跳过该容器 container=%s error=%s", name, error
                )
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
            except Exception as error:  # noqa: BLE001 - 发送路径任何异常都不能
                # 让整轮崩掉（独立审查 P2-5）。上面几处 HostMonitorError 是脚本
                # 自己主动识别的已知故障；这里改用兜底 Exception 是因为
                # `feishu_send_text` 内部调用的是标准库网络/JSON 原语，任何一个
                # 没被枚举到的异常类型（连接被重置的具体子类、意外的证书错误等）
                # 都不该让本轮剩余容器的检查、乃至整个 cron 调用直接崩溃退出——
                # 那样反而会丢掉"失败不落盘、下一轮据此重试"这条既有语义：一次
                # 未捕获异常会让 `run()` 整体抛出，cron 记录一次非零退出，但当轮
                # 已经判定过的其它容器结果同样不会被保存，行为上退化成"部分容器
                # 这一轮完全没被检查过"而不是"这一个容器的发送失败被记录并等待
                # 重试"。
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

        if args.enable_resource_thresholds or not args.disable_release_pull_check:
            _run_threshold_checks(args, credentials, host=host, logger=logger)

        return 2 if fatal else 0


def _collect_release_pull_checks(
    args: argparse.Namespace, logger: logging.Logger
) -> list[tuple[str, str, bool, str, int]]:
    """拉取代理单元检查项。读取失败不是"跳过"：以 `release_pull_unknown` 这一键
    告警一次（同样去重、恢复后一条恢复通知），三个正常判据这一轮不出结论、状态
    原样保留。读取成功时未知键以未触发形态参与，才能在恢复后发出恢复通知。
    """

    unit = args.release_pull_unit
    unknown_label = _RELEASE_PULL_LABEL[RELEASE_PULL_UNKNOWN]
    try:
        observation = read_release_pull_observation(
            unit, systemctl_bin=args.systemctl_bin, timeout_seconds=args.timeout_seconds
        )
    except HostMonitorError as error:
        logger.warning("拉取代理单元状态读取失败，按未知形态告警 unit=%s error=%s", unit, error)
        detail = f"单元 {unit}：读不到 systemd 属性（{error}）"
        return [(RELEASE_PULL_UNKNOWN, unknown_label, True, detail, 1)]
    checks: list[tuple[str, str, bool, str, int]] = [
        (RELEASE_PULL_UNKNOWN, unknown_label, False, "", 1)
    ]
    checks.extend(
        judge_release_pull(
            observation, now=datetime.now(UTC), stale_minutes=args.release_pull_stale_minutes
        )
    )
    return checks


def _run_threshold_checks(
    args: argparse.Namespace, credentials: Mapping[str, str], *, host: str, logger: logging.Logger
) -> None:
    """阈值副线：磁盘用量/系统负载/采样文件停更三项（S-RC20-410）与拉取代理单元
    检查共用一份去重状态文件与同一套发送/落盘流程。独立于容器检查的去重状态与
    退出码——采集失败（例如挂载点不存在）只记警告并跳过那一项，不把整个
    host_health_alert 调用判成脚本自身故障（`fatal`/退出码 2 仍然只由容器检查那条
    主线决定）；拉取代理读取失败则按「未知」形态告警，见 `_collect_release_pull_checks`。
    """

    threshold_state_path = Path(args.threshold_state_file)
    threshold_states = load_threshold_state(threshold_state_path)

    checks: list[tuple[str, str, bool, str, int]] = []
    if args.enable_resource_thresholds:
        checks.extend(_collect_resource_checks(args, logger))
    if not args.disable_release_pull_check:
        checks.extend(_collect_release_pull_checks(args, logger))

    threshold_changed = False
    for key, label, breached, detail, consecutive_required in checks:
        prior = threshold_states.get(key, ThresholdState())
        action, next_state = classify_threshold(
            breached, prior, consecutive_required=consecutive_required
        )

        if action == ACTION_NONE:
            if not args.dry_run and next_state != prior:
                threshold_states[key] = next_state
                threshold_changed = True
            continue

        text = render_threshold_message(
            action,
            label=label,
            detail=detail,
            host=host,
            now=_now_iso(),
            category=_THRESHOLD_CATEGORY.get(key, "资源监控"),
        )

        if args.dry_run:
            logger.info("dry-run，未真实发送 threshold=%s action=%s detail=%s", key, action, detail)
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
        except Exception as error:  # noqa: BLE001 - 与容器告警发送路径同一条纪律
            # 发送失败：保留旧的 `alerting` 记忆（下一轮达标时会重新尝试发送），
            # 但连续计数本身是纯观测事实，不因为通知没发出去而回退到 0。
            threshold_states[key] = ThresholdState(
                alerting=prior.alerting, consecutive=next_state.consecutive
            )
            threshold_changed = True
            logger.error(
                "阈值告警发送失败，告警记忆未推进，下一轮达标会重试 threshold=%s action=%s error=%s",
                key,
                action,
                error,
            )
            continue

        threshold_states[key] = next_state
        threshold_changed = True
        logger.info("阈值告警已发送 threshold=%s action=%s detail=%s", key, action, detail)

    if threshold_changed and not args.dry_run:
        try:
            save_threshold_state(threshold_state_path, threshold_states)
        except HostMonitorError as error:
            logger.error("阈值状态文件写入失败，下一轮可能重复告警 error=%s", error)


def _collect_resource_checks(
    args: argparse.Namespace, logger: logging.Logger
) -> list[tuple[str, str, bool, str, int]]:
    """磁盘用量/系统负载/采样文件停更三项的采集与判据，产出
    `(key, label, breached, detail, consecutive_required)` 供阈值副线统一去重发送。
    """

    checks: list[tuple[str, str, bool, str, int]] = []

    try:
        disk_percent = read_disk_usage_percent(args.disk_mount)
        checks.append(
            (
                THRESHOLD_DISK,
                "磁盘用量",
                disk_percent > args.disk_threshold_percent,
                f"{args.disk_mount} 已用 {disk_percent:.1f}%（阈值 {args.disk_threshold_percent:.0f}%）",
                1,
            )
        )
    except OSError as error:
        logger.warning("磁盘用量阈值检查跳过 mount=%s error=%s", args.disk_mount, error)

    try:
        load1, cpu_count = read_load_per_cpu()
        multiple = load1 / cpu_count if cpu_count else 0.0
        checks.append(
            (
                THRESHOLD_LOAD,
                "系统负载",
                multiple > args.load_multiplier,
                f"load1={load1:.2f}，{cpu_count} 核（{multiple:.1f}x，阈值 {args.load_multiplier:.1f}x）",
                max(1, args.load_consecutive),
            )
        )
    except OSError as error:
        logger.warning("负载阈值检查跳过 error=%s", error)

    monitoring_dir = Path(args.monitoring_dir)
    now_dt = datetime.now(UTC)
    for key, prefix, label in (
        (THRESHOLD_STALE_RESOURCE, "resource", "资源采样文件停更"),
        (THRESHOLD_STALE_DB_BUSINESS, "db_business", "数据库/业务采样文件停更"),
    ):
        age_seconds = read_sample_age_seconds(monitoring_dir, prefix, now=now_dt)
        threshold_minutes = args.staleness_threshold_minutes
        if age_seconds is None:
            breached = True
            detail = f"{prefix}-*.log 不存在（从未成功采样，或已超过一天未写入）"
        else:
            age_minutes = age_seconds / 60
            breached = age_minutes > threshold_minutes
            detail = f"最近一次样本 {age_minutes:.1f} 分钟前（阈值 {threshold_minutes:.0f} 分钟）"
        checks.append((key, label, breached, detail, 1))

    return checks


def main() -> int:  # pragma: no cover - 由 __main__ 调用，逻辑全部委托给 run()
    return run()


if __name__ == "__main__":
    sys.exit(main())
