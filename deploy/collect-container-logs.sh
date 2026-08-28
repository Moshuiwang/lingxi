#!/usr/bin/env bash
# 容器日志增量收集（Issue #343 / Trace #373 H2，S-H2-2）。
#
# 背景：compose 的 json-file 日志驱动只在**容器存活期间**保留日志，重部署把旧容器
# 换成新容器时，旧容器的日志随之消失——2026-08-27 调查产品负责人真实会话时，
# lingxi-worker-queue-1 已因当日重部署轮转掉原始日志，只能靠数据库字段等效还原，
# 且这条等效来源在硬切后会关闭（#343）。本脚本把各容器的 stdout/stderr 增量追加到
# 宿主机持久目录，独立于容器生命周期——旧容器被换掉，已经收集走的这部分不会跟着丢。
#
# 完整安装、cron 配置与产品要求的 30 天取证窗口说明见 deploy/日志留存.md，本文件
# 只是被安装的脚本本体，不重复那边的安装步骤。
#
# 用法：
#   deploy/collect-container-logs.sh
#
# 环境变量（均可选，不设时使用下面的默认值；未来若同一台宿主机部署多套编排、
# compose `name:` 不是默认的 `lingxi` 才需要覆盖 LINGXI_LOG_COLLECT_PROJECT）：
#   LINGXI_LOG_COLLECT_PROJECT   compose 项目名，默认 lingxi（对应 deploy/compose.yaml
#                                顶部的 `name: lingxi`，决定容器名前缀）
#   LINGXI_LOG_COLLECT_DIR       宿主机持久日志目录，默认 /var/log/lingxi
#
# 安全边界（务必保持）：
#   本脚本只读容器的 stdout/stderr（`docker logs`），**绝不**执行会吐出容器完整配置
#   的命令（例如裸 `docker inspect <容器>` 或 `docker inspect --format '{{json .}}'`）
#   ——那类命令的输出里含 `Config.Env`，而 scheduler/gateway/worker-queue 的环境变量
#   正好装着数据库连接串、Fernet 密钥与飞书应用密钥（产品合同：凭据不得进入日志或
#   任何用户交付物）。下面唯一用到的 `docker container inspect` 调用把 `--format`
#   锁定为只取容器 ID，不取任何配置字段；新增功能时如果需要判断容器状态，同样只能
#   用窄 `--format`，不允许改成裸 inspect。

set -euo pipefail

SERVICES=(scheduler gateway worker worker-queue migrate reauthorize)
PROJECT="${LINGXI_LOG_COLLECT_PROJECT:-lingxi}"
LOG_DIR="${LINGXI_LOG_COLLECT_DIR:-/var/log/lingxi}"
STATE_DIR="${LOG_DIR}/.state"

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

exit_status=0

for service in "${SERVICES[@]}"; do
  container="${PROJECT}-${service}-1"

  # 容器不存在：一次性 job（worker/migrate/reauthorize）没有正在跑，或这台机器
  # 从未启动过某个 profile 下的服务——都是正常情况，不是错误，跳过即可。
  # 只取容器 ID，不取任何配置字段（见文件头部「安全边界」）。
  if ! docker container inspect --format '{{.Id}}' "${container}" >/dev/null 2>&1; then
    continue
  fi

  state_file="${STATE_DIR}/${service}.since"
  log_file="${LOG_DIR}/${service}.log"

  # 先算好本轮收集的截止时间戳，再执行 docker logs——避免命令执行期间产生的新
  # 日志行落在"已经读过"和"还没读到"之间的空隙里被永久跳过。宁可下一轮与本轮
  # 在边界上有一行重复（--since 是开区间下界，边界重复是可接受的重复），也不要漏。
  until_ts="$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"

  since_args=()
  if [[ -f "${state_file}" ]]; then
    since_args=(--since "$(cat "${state_file}")")
  fi
  # 首次收集（没有水位文件）：不传 --since，拿 docker 侧当前留存的全部历史——
  # 这次收集不受 json-file max-size/max-file 影响，之后每一轮才是真正的增量。

  # docker logs 把容器 stdout 写到自己的 stdout、容器 stderr 写到自己的 stderr；
  # 两路都追加进同一个宿主机文件，交错顺序不保证逐行精确，但对取证窗口的价值
  # 不受影响（每行仍带 --timestamps 时间戳，可按时间重新排序）。
  if docker logs --timestamps "${since_args[@]}" --until "${until_ts}" "${container}" \
      >>"${log_file}" 2>>"${log_file}"; then
    printf '%s\n' "${until_ts}" >"${state_file}"
  else
    echo "警告：${service}（容器 ${container}）日志收集失败，水位保持不变，下一轮重试" >&2
    exit_status=1
  fi
done

exit "${exit_status}"
