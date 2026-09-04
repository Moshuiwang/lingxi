"""资源层单轮采样的实际实现（S-RC20-410，Issue #410）。

只由 `resource_sample.sh` 调用，不单独作为入口使用——bash 侧已经把"逐容器探测
`docker stats`、容忍单个容器缺失"这一段做完，本模块只负责：解析那份 docker
stats 原始输出、直接从 `/proc` 与标准库读取本机 load/内存/磁盘/网卡指标、与上一
轮样本做差分求"增速"、拼成一行 JSON 追加进按 UTC 日期切分的输出文件。

**只依赖 Python 标准库**：与 `host_health_alert.py` 同一条纪律，不 import
`lingxi` 包，不装第三方依赖，可以在容器化服务全部不可用、甚至仓库 `src/lingxi`
都装不上的宿主环境下独立跑通。

输出信封（每行一个这样的 JSON 对象，`ensure_ascii=False` 保留中文可读性，但当前
字段全部是数值/容器名，不含中文正文）：

```json
{"ts": "2026-08-29T10:00:00Z", "host": "biai-stage", "layer": "resource",
 "metrics": {"load": {...}, "memory": {...}, "disk": [...], "net": {...},
             "containers": [...], "containers_unavailable": [...]}}
```

`layer` 恒为 ``"resource"``——与 `db_business_sample.sh` 产出的 ``"db_business"``
共用同一张监控库样本表（`monitoring_schema.sql` 的 `layer` 列），下游按这个字段
区分来源，不需要靠文件名猜测。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# docker stats 的人类可读尺寸后缀（先按后缀长度降序匹配，避免 "B" 提前命中
# "KiB"/"MiB" 的结尾）。同时兼容十进制（kB/MB/GB，docker 早期版本偶见）与
# 二进制（KiB/MiB/GiB/TiB，当前默认）两种单位族。
_SIZE_UNITS: dict[str, int] = {
    "TiB": 1024**4,
    "GiB": 1024**3,
    "MiB": 1024**2,
    "KiB": 1024,
    "GB": 1000**3,
    "MB": 1000**2,
    "kB": 1000,
    "B": 1,
}


def _parse_percent(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        return None


def _parse_size_to_bytes(token: str) -> float | None:
    token = token.strip()
    if not token:
        return None
    for suffix, multiplier in _SIZE_UNITS.items():
        if token.endswith(suffix):
            number = token[: -len(suffix)]
            try:
                return float(number) * multiplier
            except ValueError:
                return None
    try:
        return float(token)
    except ValueError:
        return None


def _parse_slash_pair(value: object) -> tuple[float | None, float | None]:
    if not isinstance(value, str) or "/" not in value:
        return None, None
    left, _, right = value.partition("/")
    return _parse_size_to_bytes(left), _parse_size_to_bytes(right)


def normalize_container_stat(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把 `docker stats --format '{{json .}}'` 一行原始输出换算成数值字段。

    docker 原生输出里的数值全部是给人看的字符串（``"1.23%"``、``"12.3MiB /
    512MiB"``），不能直接落进要给 AI 做趋势分析的结构化样本——那样每次读取都要
    重新解析一遍格式，且格式本身随 docker 版本、语言环境可能变化。这里在采样时
    一次性转成数值，下游消费者不用再关心 docker 的展示格式。
    """

    mem_used, mem_limit = _parse_slash_pair(raw.get("MemUsage"))
    net_rx, net_tx = _parse_slash_pair(raw.get("NetIO"))
    block_read, block_write = _parse_slash_pair(raw.get("BlockIO"))
    pids_raw = raw.get("PIDs")
    try:
        pids = int(pids_raw) if pids_raw is not None else None
    except (TypeError, ValueError):
        pids = None
    return {
        "name": raw.get("Name"),
        "cpu_percent": _parse_percent(raw.get("CPUPerc")),
        "mem_used_bytes": mem_used,
        "mem_limit_bytes": mem_limit,
        "mem_percent": _parse_percent(raw.get("MemPerc")),
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "block_read_bytes": block_read,
        "block_write_bytes": block_write,
        "pids": pids,
    }


def parse_docker_stats_lines(text: str) -> list[dict[str, Any]]:
    """逐行解析 `docker stats` 原始输出；单行损坏只跳过那一行，不让整轮采样失败
    ——一个容器的输出格式异常不该拖累其余容器的样本。
    """

    containers: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            containers.append(normalize_container_stat(raw))
    return containers


def read_missing_names(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def read_load_avg() -> dict[str, Any]:
    load1, load5, load15 = os.getloadavg()
    return {"load1": load1, "load5": load5, "load15": load15, "cpu_count": os.cpu_count() or 0}


def read_mem_info() -> dict[str, int | None]:
    fields: dict[str, int] = {}
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return {"mem_total_kb": None, "mem_available_kb": None}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            parts = rest.strip().split()
            if parts:
                try:
                    fields[key] = int(parts[0])
                except ValueError:
                    continue
    return {
        "mem_total_kb": fields.get("MemTotal"),
        "mem_available_kb": fields.get("MemAvailable"),
    }


def read_disk_usage(mounts: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for mount in mounts:
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        percent = (usage.used / usage.total * 100) if usage.total else 0.0
        result.append(
            {
                "mount": mount,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "used_percent": round(percent, 2),
            }
        )
    return result


def read_net_totals() -> dict[str, int | None]:
    """汇总除 `lo` 外全部网卡自启用起的累计收发字节数。

    `/proc/net/dev` 给的是累计计数器,不是速率——"增速"由调用方结合上一轮样本的
    状态文件做差分,这里只负责读一次当前值。
    """

    try:
        lines = Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
    except OSError:
        return {"rx_bytes": None, "tx_bytes": None}
    rx_total = 0
    tx_total = 0
    for line in lines:
        iface, _, rest = line.partition(":")
        iface = iface.strip()
        if not iface or iface == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
        except ValueError:
            continue
    return {"rx_bytes": rx_total, "tx_bytes": tx_total}


def load_prev_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp_path, path)


def compute_rate(
    current: float | None, previous: float | None, prev_ts: float | None, now_ts: float
) -> float | None:
    """`(当前值 - 上一轮值) / 经过秒数`；任何一端缺失（首次采样、状态文件损坏、
    磁盘/网卡本轮读取失败）都返回 ``None``——增速在这种情况下"取不到"是精确
    语义，不编造成 0（与 `task_report_metrics` 迁移同一条"NULL 是精确语义"纪律）。
    """

    if current is None or previous is None or prev_ts is None:
        return None
    elapsed = now_ts - prev_ts
    if elapsed <= 0:
        return None
    return (current - previous) / elapsed


def build_sample(
    *,
    docker_stats_text: str,
    missing_text: str,
    disk_mounts: list[str],
    prev_state: Mapping[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 ``(sample, next_state)``：`sample` 是要落盘的一行 JSON，`next_state`
    是供下一轮计算增速用的状态（只有调用方确认样本已经成功写盘才应该落盘这份
    状态，避免"样本没写成功、状态却已经前进"的不一致）。
    """

    containers = parse_docker_stats_lines(docker_stats_text)
    missing = read_missing_names(missing_text)
    disks = read_disk_usage(disk_mounts)
    net = read_net_totals()

    now_ts = now.timestamp()
    prev_ts = prev_state.get("ts")
    prev_disks = {d.get("mount"): d for d in prev_state.get("disks", []) if isinstance(d, dict)}
    prev_net = prev_state.get("net", {}) if isinstance(prev_state.get("net"), dict) else {}

    for disk in disks:
        prev_disk = prev_disks.get(disk["mount"])
        disk["growth_bytes_per_sec"] = compute_rate(
            disk["used_bytes"],
            prev_disk.get("used_bytes") if prev_disk else None,
            prev_ts,
            now_ts,
        )

    net_with_rate = {
        **net,
        "rx_bytes_per_sec": compute_rate(
            net.get("rx_bytes"), prev_net.get("rx_bytes"), prev_ts, now_ts
        ),
        "tx_bytes_per_sec": compute_rate(
            net.get("tx_bytes"), prev_net.get("tx_bytes"), prev_ts, now_ts
        ),
    }

    sample = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "layer": "resource",
        "metrics": {
            "load": read_load_avg(),
            "memory": read_mem_info(),
            "disk": disks,
            "net": net_with_rate,
            "containers": containers,
            "containers_unavailable": missing,
        },
    }
    next_state = {"ts": now_ts, "disks": disks, "net": net}
    return sample, next_state


def append_sample(output_dir: Path, sample: Mapping[str, Any], *, now: datetime) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    output_path = output_dir / f"resource-{now.strftime('%Y%m%d')}.log"
    line = json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return output_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="资源层单轮采样实现，只由 resource_sample.sh 调用")
    parser.add_argument("--docker-stats-file", required=True, type=Path)
    parser.add_argument("--missing-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--disk-mount", dest="disk_mounts", action="append", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    disk_mounts = args.disk_mounts or ["/"]

    docker_stats_text = (
        args.docker_stats_file.read_text(encoding="utf-8")
        if args.docker_stats_file.is_file()
        else ""
    )
    missing_text = (
        args.missing_file.read_text(encoding="utf-8") if args.missing_file.is_file() else ""
    )

    state_path = args.state_dir / "resource_prev.json"
    prev_state = load_prev_state(state_path)

    now = datetime.now(UTC)
    sample, next_state = build_sample(
        docker_stats_text=docker_stats_text,
        missing_text=missing_text,
        disk_mounts=disk_mounts,
        prev_state=prev_state,
        now=now,
    )
    output_path = append_sample(args.output_dir, sample, now=now)
    save_state(state_path, next_state)
    print(f"resource sample appended: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 resource_sample.sh 调用
    sys.exit(main())
