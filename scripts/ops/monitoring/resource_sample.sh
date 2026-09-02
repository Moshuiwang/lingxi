#!/usr/bin/env bash
# 资源层单轮采样（S-RC20-410，Issue #410）：一次调用 = 一行 JSON 样本，追加到按
# UTC 日期切分的本机文件。由 systemd timer `lingxi-resource-sample.timer` 每分钟
# 触发一次（分层采集清单「资源」行的 30-60s 目标区间，取上限对齐 1 分钟节奏，
# 与 D5 `lingxi-host-monitor.timer` 同一节奏惯例）；不是常驻循环——每次调用只采
# 一次、写一行、退出，循环节奏完全交给 systemd，脚本本身不 sleep。
#
# 零新依赖：本文件只负责调用外部命令 `docker stats`（bash 唯一擅长的部分——启动
# 子进程、按容器名逐个探测、容忍单个容器缺失），host 侧真正的指标读取（loadavg、
# meminfo、磁盘、网卡累计计数、与上一次样本做差分求增速）全部委托给同目录的
# `_resource_sample.py`——那些数据本来就是 /proc 下的纯文本文件与 Python 标准库
# 就能拿到的系统调用，没有理由在 bash 里再拼一遍字符串解析。
#
# 为什么逐个容器单独调用 `docker stats`，不是一次性传全部容器名：`docker stats`
# 只要列表里有一个容器不存在就会让整条命令非零退出、且不吐出其余容器的数据——
# 与 host_health_alert.py 对"容器缺失不是脚本故障"的既有处理原则一致，这里同样
# 需要单个容器缺失时其余容器的样本仍然正常采到。
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

OUTPUT_DIR="${LINGXI_MONITORING_DIR:-/var/log/lingxi/monitoring}"
STATE_DIR="${OUTPUT_DIR}/.state"

if [[ -n "${LINGXI_MONITORING_CONTAINERS:-}" ]]; then
  read -r -a CONTAINERS <<< "${LINGXI_MONITORING_CONTAINERS}"
else
  CONTAINERS=(lingxi-scheduler-1 lingxi-gateway-1 lingxi-worker-queue-1)
fi

if [[ -n "${LINGXI_MONITORING_DISK_MOUNTS:-}" ]]; then
  read -r -a DISK_MOUNTS <<< "${LINGXI_MONITORING_DISK_MOUNTS}"
else
  DISK_MOUNTS=(/)
fi

command -v docker >/dev/null 2>&1 || { echo "缺少命令：docker" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "缺少命令：python3" >&2; exit 2; }

install -d -m 750 "${OUTPUT_DIR}" "${STATE_DIR}"

# ---- 样本文件保留上限（对抗审查 2026-09-02 P3）---------------------------
# `/var/log/lingxi/monitoring` 下的 `resource-YYYYMMDD.log` /
# `db_business-YYYYMMDD.log` 是**每天一个新文件**，此前没有任何东西回收它们：
# `deploy/lingxi-container-logs.logrotate` 只管六个容器日志，这个目录不在它的
# 名单里。采样每分钟一轮、常驻运行，磁盘占用因此单调增长，直到把宿主机写满
# ——而写满宿主机会同时打掉容器日志收集与业务本身。
#
# 这里不引入 logrotate 配置（那要新增一个安装步骤，装没装不可见），改为**谁写
# 谁清**：每轮采样顺手删掉自己那一族里超过保留天数的旧文件。只按文件名前缀匹配
# 本脚本自己产出的那一族，绝不用通配删整个目录。
LINGXI_MONITORING_RETENTION_DAYS="${LINGXI_MONITORING_RETENTION_DAYS:-30}"
if [[ "${LINGXI_MONITORING_RETENTION_DAYS}" =~ ^[0-9]+$ ]] \
  && (( LINGXI_MONITORING_RETENTION_DAYS > 0 )); then
  find "${OUTPUT_DIR}" -maxdepth 1 -type f -name 'resource-*.log' \
    -mtime "+${LINGXI_MONITORING_RETENTION_DAYS}" -delete 2>/dev/null || true
fi

docker_stats_file=$(mktemp)
missing_file=$(mktemp)
cleanup() { rm -f "${docker_stats_file}" "${missing_file}"; }
trap cleanup EXIT

for name in "${CONTAINERS[@]}"; do
  if ! docker stats --no-stream --format '{{json .}}' "${name}" >> "${docker_stats_file}" 2>/dev/null; then
    echo "${name}" >> "${missing_file}"
  fi
done

disk_mount_args=()
for mount in "${DISK_MOUNTS[@]}"; do
  disk_mount_args+=(--disk-mount "${mount}")
done

python3 "${SCRIPT_DIR}/_resource_sample.py" \
  --docker-stats-file "${docker_stats_file}" \
  --missing-file "${missing_file}" \
  --output-dir "${OUTPUT_DIR}" \
  --state-dir "${STATE_DIR}" \
  "${disk_mount_args[@]}"
