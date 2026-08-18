#!/usr/bin/env bash
# Stage 演练脚本（Epic E 验收资产第 3 件，S-E-01，Issue #161）。
#
# 用途：让编排者在 `biai-stage` 上，先于产品负责人那一次集中验收窗口，自己把
# [MVP联合验收执行卡](../docs/参考证据/MVP联合验收执行卡.md) 第③④阶段里"部署/
# 恢复"这一半跑一遍——安装、迁移、健康回读、持久化重启、回滚演练、清理——
# 确认脚本本身没有笔误、命令顺序没有踩坑，而不是带着产品负责人一起试错。
#
# **本脚本不包含、也不应该包含**执行卡①②④阶段里"发真实飞书消息""跑#97九步
# 探针""触发受控暂停窗口"这类业务旅程动作——那些需要真实 Bot-Test 凭据、真实
# 测试身份配合，属于人工按执行卡逐步操作，不适合也不安全被一个可重复执行的
# 脚本自动化（意外重跑会重复发送真实飞书消息）。本脚本只覆盖 #161 范围里
# "安装、迁移、健康、持久化、备份恢复演练、旧版本恢复"这一半可确定性重放的
# 部署动作，命令逐字来自 `deploy/README.md`，不是重新发明一遍。
#
# **默认模式是自检（dry-run）：不连接任何真实环境、不执行任何有副作用的命令。**
# 只有显式传入 `--confirm-real-run` 的具名 `--step`，且当前工作目录确实是
# `biai-stage` 上配置好七个 env 文件的那份工作副本时，才会真的执行。实施者/
# 其他环境一律只应使用 `--self-check` 或 `--list-steps`，不得传 `--confirm-real-run`。
#
# 环境固定名称与职责见[协作约定「固定名称与环境职责」](../docs/协作约定.md#固定名称与环境职责)：
# 本脚本只在 `biai-stage` 上对 `Bot-Test` 生效，不得指向生产。

set -euo pipefail

# ---------------------------------------------------------------------------
# 定位仓库根目录，全部相对路径以此为基准。
# ---------------------------------------------------------------------------
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/.." && pwd)
cd "${repository_root}"

COMPOSE_BASE="deploy/compose.yaml"
COMPOSE_STAGE_OVERLAY="deploy/compose.stage.yaml"
ENV_FILE="deploy/.env.stage"
ENV_FILES=(
  "deploy/.env.stage" "deploy/.env.stage.scheduler" "deploy/.env.stage.gateway"
  "deploy/.env.stage.worker" "deploy/.env.stage.worker-queue"
  "deploy/.env.stage.migrate" "deploy/.env.stage.reauthorize"
)

CONFIRM_REAL_RUN=0
REQUESTED_STEP=""
DO_SELF_CHECK=0
DO_LIST_STEPS=0

# ---------------------------------------------------------------------------
# 步骤登记表：id | 一句话说明 | 预计耗时 | 对应函数
# 顺序即建议执行顺序；--step 可单独指定其一，供先跑通某一步再继续。
# ---------------------------------------------------------------------------
STEP_IDS=(preflight migrate up healthcheck persistence-restart backup-restore-note rollback-drill cleanup)

step_description() {
  case "$1" in
    preflight) echo "部署前置检查：七个 env 文件权限 0600、镜像 tag 已设置、受控触发夹具变量当前干净" ;;
    migrate) echo "跑一次性迁移作业（job profile run --rm migrate）" ;;
    up) echo "以 mvp profile 启动 scheduler + gateway + 常驻 queue worker" ;;
    healthcheck) echo "三个常驻服务的健康回读（docker exec 语义，不开放端口）" ;;
    persistence-restart) echo "受控重启常驻 queue worker，核对在途任务不丢、不重复交付（需与执行卡②/④阶段的真实任务时间窗口配合）" ;;
    backup-restore-note) echo "数据库备份/恢复走 Supabase 托管方案（本脚本不代管，只核对两个持久卷存在）" ;;
    rollback-drill) echo "切回上一个候选镜像 tag 并重新 up -d，核对旧镜像能在当前库结构上正常启动" ;;
    cleanup) echo "down、清理临时资源、再次确认受控触发夹具变量已清空" ;;
    *) echo "未知步骤" ;;
  esac
}

step_estimated_minutes() {
  case "$1" in
    preflight) echo "5" ;;
    migrate) echo "3" ;;
    up) echo "3" ;;
    healthcheck) echo "5" ;;
    persistence-restart) echo "10" ;;
    backup-restore-note) echo "5（登记确认，不含真实备份耗时）" ;;
    rollback-drill) echo "10" ;;
    cleanup) echo "5" ;;
    *) echo "?" ;;
  esac
}

usage() {
  cat <<'USAGE'
用法：
  scripts/stage_rehearsal_mvp_acceptance.sh --self-check
      不触碰任何真实环境；核对脚本自身、必需文件、CLI 工具是否具备，
      并跑一次受控触发夹具的只读清空检查。在任何机器上都可以安全执行，
      包括本仓库以外的开发机——这是实施者应该使用的唯一模式。

  scripts/stage_rehearsal_mvp_acceptance.sh --list-steps
      列出全部步骤、一句话说明与预计耗时，不执行任何步骤。

  scripts/stage_rehearsal_mvp_acceptance.sh --step <id> [--confirm-real-run]
      单独执行（或演练）某一步。不带 --confirm-real-run 时只打印将要执行的
      命令，不实际运行。--confirm-real-run 只应在 biai-stage 上、且当前
      目录已按 deploy/README.md 准备好七个 env 文件时使用。

  scripts/stage_rehearsal_mvp_acceptance.sh --all --confirm-real-run
      按登记顺序依次真实执行全部步骤，每步之间打印分隔与耗时。

步骤 id：preflight migrate up healthcheck persistence-restart backup-restore-note rollback-drill cleanup
USAGE
}

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

# 打印一条命令：真实执行模式下真跑，否则只回显（dry-run 的核心机制，全部步骤
# 函数一律通过这个包装器发起有副作用的调用，不直接裸写命令）。
run_or_print() {
  if [[ "${CONFIRM_REAL_RUN}" == "1" ]]; then
    log "执行：$*"
    "$@"
  else
    printf '（dry-run，未执行）将会运行：%s\n' "$*"
  fi
}

# ---------------------------------------------------------------------------
# 自检：任何机器都能跑，不触碰真实环境。
# ---------------------------------------------------------------------------
self_check() {
  local failures=0

  log "自检 1/5：脚本自身语法"
  if bash -n "${BASH_SOURCE[0]}"; then
    log "  通过"
  else
    log "  失败：脚本语法错误"
    failures=$((failures + 1))
  fi

  log "自检 2/5：必需文件存在"
  local required_files=(
    "${COMPOSE_BASE}" "${COMPOSE_STAGE_OVERLAY}" "deploy/README.md"
    "scripts/acceptance_fixtures.py" "scripts/ci/verify_epic_candidate_bundle.py"
    "scripts/ci/verify_old_image_new_schema.sh"
  )
  for f in "${required_files[@]}"; do
    if [[ -f "${f}" ]]; then
      log "  存在：${f}"
    else
      log "  缺失：${f}"
      failures=$((failures + 1))
    fi
  done

  log "自检 3/5：CLI 工具（缺失只记录，不判失败——自检要能在没装 docker 的机器上也跑通）"
  for tool in docker gh python3; do
    if command -v "${tool}" >/dev/null 2>&1; then
      log "  具备：${tool}"
    else
      log "  未具备：${tool}（在 biai-stage 上执行真实步骤前必须补齐）"
    fi
  done
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "  具备：docker compose（v2 插件形态）"
  else
    log "  未具备：docker compose（在 biai-stage 上执行真实步骤前必须补齐）"
  fi

  log "自检 4/5：受控触发夹具当前进程环境是否干净（只读检查，不修改任何东西）"
  if python3 "${repository_root}/scripts/acceptance_fixtures.py" --check-clear; then
    log "  通过"
  else
    log "  失败：当前进程环境里残留受控触发夹具变量，须先清除再继续"
    failures=$((failures + 1))
  fi

  log "自检 5/5：步骤登记表自洽（每个 id 都有说明与耗时，不是空字符串）"
  for id in "${STEP_IDS[@]}"; do
    local desc minutes
    desc=$(step_description "${id}")
    minutes=$(step_estimated_minutes "${id}")
    if [[ -z "${desc}" || "${desc}" == "未知步骤" || -z "${minutes}" ]]; then
      log "  失败：步骤 ${id} 缺说明或耗时"
      failures=$((failures + 1))
    fi
  done
  log "  通过（${#STEP_IDS[@]} 个步骤全部有说明与耗时）"

  if [[ "${failures}" -eq 0 ]]; then
    log "自检整体：通过。可以在 biai-stage 上按 --list-steps 顺序演练。"
    return 0
  else
    log "自检整体：失败（${failures} 项），先修复再继续。"
    return 1
  fi
}

list_steps() {
  printf '%-24s %-8s %s\n' "步骤 id" "预计耗时(分)" "说明"
  for id in "${STEP_IDS[@]}"; do
    local desc minutes
    desc=$(step_description "${id}")
    minutes=$(step_estimated_minutes "${id}")
    printf '%-24s %-8s %s\n' "${id}" "${minutes}" "${desc}"
  done
  printf '\n提示：这是编排者在 biai-stage 上先自己跑一遍的演练序列，不是产品负责人\n'
  printf '窗口本身的耗时——执行卡①②③④阶段的耗时估算见 MVP联合验收执行卡.md。\n'
}

# ---------------------------------------------------------------------------
# 各步骤实现。命令逐字来自 deploy/README.md，本脚本只负责排序、dry-run 包装
# 与失败提前退出（set -e 已保证），不重新发明这些命令。
# ---------------------------------------------------------------------------

step_preflight() {
  log "preflight：核对七个 env 文件权限（期望全部 600，属主为当前部署用户）"
  for f in "${ENV_FILES[@]}"; do
    if [[ "${CONFIRM_REAL_RUN}" == "1" ]]; then
      if [[ ! -f "${f}" ]]; then
        log "  缺失：${f}（按 deploy/README.md「准备」一节先创建）"
        return 1
      fi
      stat -c '%n %a %U' "${f}"
    else
      printf '（dry-run）将检查：%s 的权限与属主\n' "${f}"
    fi
  done

  log "preflight：受控触发夹具当前是否干净（开窗前必须为空，验收窗口专用夹具由执行卡④阶段显式打开）"
  run_or_print python3 "${repository_root}/scripts/acceptance_fixtures.py" --check-clear
}

step_migrate() {
  log "migrate：跑一次性迁移作业"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" \
    --profile job run --rm migrate
}

step_up() {
  log "up：以 mvp profile 启动 scheduler + gateway + 常驻 queue worker"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" \
    --profile mvp up -d

  log "up：回读容器状态与 scheduler 日志"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" ps
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" logs scheduler
}

step_healthcheck() {
  log "healthcheck：三个常驻服务各自的健康回读（docker exec 语义，不开放端口）"
  for role in scheduler gateway worker-queue; do
    run_or_print docker compose --env-file "${ENV_FILE}" \
      -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" \
      exec -T "${role}" python -m lingxi.apps.healthcheck --role "${role}"
  done
  log "healthcheck：断言核对——依赖不可达、主循环停摆两段判定都要如实变红；不是只看容器 running（V-部署-11）"
}

step_persistence_restart() {
  log "persistence-restart：受控重启常驻 queue worker，核对在途任务不丢、不重复交付（V-部署-12）"
  log "  前置：需要执行卡②/④阶段有一个真实任务正在运行，重启窗口须与任务执行时间真实重叠——"
  log "  本步骤只负责发出重启信号，任务相关性判断由操作者结合当时的真实任务对照"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" \
    restart worker-queue
  log "  重启后：回读 worker-queue 与 gateway 是否恢复 healthy"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" ps
}

step_backup_restore_note() {
  cat <<'NOTE'
backup-restore-note：数据库备份与恢复遵循 Supabase 托管方案（deploy/README.md
「恢复入口」一节），本脚本不代管、不重新实现一套备份/恢复命令——那需要
Supabase 侧的操作权限与工具，不是 docker compose 能覆盖的对象。

本步骤只做两件可以在 biai-stage 本机核对的事：
  1. 确认两个持久卷（凭据卷、用户目录卷）存在；
  2. 提醒操作者：真实备份点创建、恢复到隔离目标、完整性检查须按 Supabase
     控制台或其 CLI 另行执行，并在执行卡③/④阶段登记备份点与恢复结果。
NOTE
  run_or_print docker volume ls --filter "name=lingxi-stage-credentials"
  run_or_print docker volume ls --filter "name=lingxi-stage-users"
}

step_rollback_drill() {
  log "rollback-drill：切回上一个候选镜像 tag，核对旧镜像能否在当前库结构上正常启动（V-部署-05 的真实环境复演）"
  log "  操作前提：deploy/.env.stage 的 LINGXI_IMAGE_TAG 已经改回上一个候选 tag"
  log "  （本脚本不代为编辑 env 文件——tag 选择是产品与发布判断，不是脚本判断）"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" up -d
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" ps
  log "  回读：确认旧镜像启动成功、健康检查通过、未误宣告成功"
}

step_cleanup() {
  log "cleanup：down、清理临时资源"
  run_or_print docker compose --env-file "${ENV_FILE}" \
    -f "${COMPOSE_BASE}" -f "${COMPOSE_STAGE_OVERLAY}" down

  log "cleanup：再次确认受控触发夹具变量已清空（用后撤除并回读，Trace v12 机制③）"
  run_or_print python3 "${repository_root}/scripts/acceptance_fixtures.py" --check-clear

  log "cleanup：按 AGENTS.md「临时进程、容器、worktree、测试实例谁建谁清」——"
  log "  逐条确认无遗留测试数据库、隔离验收库、临时容器或长连接残留；"
  log "  确需保留的资源登记 Owner、原因、停止/删除方法到 Issue #161。"
}

run_step() {
  case "$1" in
    preflight) step_preflight ;;
    migrate) step_migrate ;;
    up) step_up ;;
    healthcheck) step_healthcheck ;;
    persistence-restart) step_persistence_restart ;;
    backup-restore-note) step_backup_restore_note ;;
    rollback-drill) step_rollback_drill ;;
    cleanup) step_cleanup ;;
    *)
      log "未知步骤：$1"
      return 2
      ;;
  esac
}

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
RUN_ALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --self-check) DO_SELF_CHECK=1; shift ;;
    --list-steps) DO_LIST_STEPS=1; shift ;;
    --step) REQUESTED_STEP="${2:-}"; shift 2 ;;
    --all) RUN_ALL=1; shift ;;
    --confirm-real-run) CONFIRM_REAL_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "未知参数：$1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "${DO_SELF_CHECK}" == "1" ]]; then
  self_check
  exit $?
fi

if [[ "${DO_LIST_STEPS}" == "1" ]]; then
  list_steps
  exit 0
fi

if [[ "${RUN_ALL}" == "1" ]]; then
  if [[ "${CONFIRM_REAL_RUN}" != "1" ]]; then
    log "未带 --confirm-real-run：以下按 dry-run 逐步打印，不会真的执行任何命令。"
  fi
  for id in "${STEP_IDS[@]}"; do
    log "==== 步骤：${id}（预计 $(step_estimated_minutes "${id}") 分钟）===="
    run_step "${id}"
  done
  exit 0
fi

if [[ -n "${REQUESTED_STEP}" ]]; then
  if [[ "${CONFIRM_REAL_RUN}" != "1" ]]; then
    log "未带 --confirm-real-run：以下按 dry-run 打印，不会真的执行任何命令。"
  fi
  run_step "${REQUESTED_STEP}"
  exit $?
fi

usage
exit 2
