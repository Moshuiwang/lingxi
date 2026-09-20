#!/usr/bin/env bash
# 远程串行完整门禁：把「候选冻结前的一次 scripts/dev/check.sh full」搬到一台可配置的
# Linux 主机上跑，本机只负责送提交、收结论。门禁本体仍是远端检出里的 check.sh，本脚本
# 不改它的分层、断言与真库配方，也不替它传 --reuse-venv（venv 是否重建由它自己决定）。
#
# 用法：
#   scripts/dev/remote_check.sh [<提交>]   # 默认 HEAD；只检该提交，不检工作树
#
# 配置全部来自环境变量；未设置的项从 .dev-check/remote.env（相对仓库根，gitignored，
# 可用 LINGXI_REMOTE_ENV_FILE 换一份）补齐。每项含义与样例见 scripts/dev/remote.env.example，
# 目标主机开通清单与残留清理责任见 scripts/dev/README.md「远程串行完整门禁」。
# **主机、用户、路径一处不写死**：换主机只改配置，不改本文件。
#
# 流程：本机解析提交 → 远端预检（目录在、工作树干净、有没有该提交、工具在位、开临时
# 目录）→ 传输（默认 bundle：本机 git bundle 经同一条 SSH 送到远端再 fetch，不依赖 GitHub
# 可达；fetch：远端自己 git fetch origin <sha>，只适用于已推送的提交）→ 远端
# checkout --detach → 远端 /usr/bin/time -v 包住 check.sh（取内存峰值）→ 日志回传到
# .dev-check/remote-runs/<sha>-<UTC时刻>.log → 回读退出码 / 用时 / Ran N / 峰值 / 残留容器 /
# 远端工作树 → 打印一行结论摘要。
#
# 失败即关：SSH 连不上、远端中断、日志回传失败、解析不到 Ran N、残留容器、远端工作树
# 变脏，一律非零退出并说明原因，绝不判绿。远端 check.sh 的退出码原样透传；本脚本自身的
# 失败用 sysexits 区间的退出码（78 配置、69 连接 / 预检 / 中断、74 传输 / 日志回传、
# 70 结果判定），与 check.sh 常见的 1 / 2 区分开。
#
# 清理：本机 bundle、临时 ref、远端 bundle 与临时目录由 trap 自删；一次性真库容器由
# check.sh 自清，本脚本结束时回读一次，残留即非零。远端检出与 venv 保留复用。

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

readonly EXIT_CONFIG=78
readonly EXIT_UNAVAILABLE=69
readonly EXIT_IOERR=74
readonly EXIT_RESULT=70

usage() {
  cat <<'EOF'
用法：scripts/dev/remote_check.sh [<提交>]

  <提交>   要检的提交（默认 HEAD），解析为完整 SHA；只检该提交，不检工作树。

配置（环境变量优先，缺省从 .dev-check/remote.env 补齐；样例见 scripts/dev/remote.env.example）：
  LINGXI_REMOTE_SSH              连接命令整串（必填），例如 "ssh gate-host" 或 "tailscale ssh user@gate-host"
  LINGXI_REMOTE_WORKDIR          远端检出目录（必填），例如 ~/lingxi-gate
  LINGXI_REMOTE_PG_NAME          远端一次性真库容器名（默认 lingxi-remote-check-pg）
  LINGXI_REMOTE_TRANSPORT        bundle（默认，不依赖 GitHub 可达）或 fetch（远端 git fetch origin）
  LINGXI_REMOTE_CHECK_ARGS       透传给 check.sh 的参数（默认 full）
  LINGXI_REMOTE_TIMEOUT_SECONDS  可选，远端 check.sh 的最长运行秒数，超时按 124 判失败
  LINGXI_REMOTE_ENV_FILE         可选，换一份配置文件（默认 <仓库根>/.dev-check/remote.env）

退出码：0 通过；远端 check.sh 非零时原样透传；78 配置错误；69 连不上 / 预检失败 / 远端中断；
        74 传输或日志回传失败；70 结果判定失败（解析不到 Ran N、残留容器、远端工作树变脏）。
EOF
}

if [[ $# -gt 1 ]]; then
  usage >&2
  exit "${EXIT_CONFIG}"
fi
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

# POSIX 单引号转义：远端登录 shell 无论是 bash 还是 sh 都按原文收到参数，
# 不会二次展开本机变量或远端变量。
sq() {
  printf "'%s'" "${1//\'/\'\\\'\'}"
}

fail() {
  local code="$1"
  shift
  printf '%s\n' "$@" >&2
  exit "${code}"
}

# ---------- 配置 ----------

load_env_file() {
  local file="$1"
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    line="${line#export }"
    [[ "${line}" == *=* ]] || fail "${EXIT_CONFIG}" "配置文件 ${file} 有一行不是 KEY=value：${line}"
    key="${line%%=*}"
    value="${line#*=}"
    if [[ ! "${key}" =~ ^LINGXI_REMOTE_[A-Z0-9_]+$ ]]; then
      fail "${EXIT_CONFIG}" "配置文件 ${file} 只接受 LINGXI_REMOTE_* 变量，遇到：${key}"
    fi
    if [[ ${#value} -ge 2 && ("${value}" == \"*\" || "${value}" == \'*\') ]]; then
      value="${value:1:${#value}-2}"
    fi
    # 显式环境变量优先：文件只补没设的项。
    if [[ -z "${!key+set}" ]]; then
      printf -v "${key}" '%s' "${value}"
    fi
  done <"${file}"
}

env_file="${LINGXI_REMOTE_ENV_FILE:-${repository_root}/.dev-check/remote.env}"
if [[ -f "${env_file}" ]]; then
  load_env_file "${env_file}"
elif [[ -n "${LINGXI_REMOTE_ENV_FILE:-}" ]]; then
  fail "${EXIT_CONFIG}" "LINGXI_REMOTE_ENV_FILE 指向的配置文件不存在：${env_file}"
fi

for required in LINGXI_REMOTE_SSH LINGXI_REMOTE_WORKDIR; do
  if [[ -z "${!required:-}" ]]; then
    fail "${EXIT_CONFIG}" \
      "缺少配置 ${required}：请设置环境变量，或写进 ${env_file}（样例见 scripts/dev/remote.env.example）。"
  fi
done

pg_name="${LINGXI_REMOTE_PG_NAME:-lingxi-remote-check-pg}"
transport="${LINGXI_REMOTE_TRANSPORT:-bundle}"
check_args_raw="${LINGXI_REMOTE_CHECK_ARGS:-full}"
timeout_seconds="${LINGXI_REMOTE_TIMEOUT_SECONDS:-}"
remote_workdir="${LINGXI_REMOTE_WORKDIR}"

# 连接命令按空白切词，不解析引号：复杂选项请写进 ~/.ssh/config 用别名引用。
read -r -a ssh_cmd <<<"${LINGXI_REMOTE_SSH}"
if [[ ${#ssh_cmd[@]} -eq 0 ]]; then
  fail "${EXIT_CONFIG}" "LINGXI_REMOTE_SSH 为空。"
fi
command -v "${ssh_cmd[0]}" >/dev/null 2>&1 \
  || fail "${EXIT_CONFIG}" "LINGXI_REMOTE_SSH 的连接命令本机不存在：${ssh_cmd[0]}"

case "${transport}" in
  bundle | fetch) ;;
  *) fail "${EXIT_CONFIG}" "LINGXI_REMOTE_TRANSPORT 只能是 bundle 或 fetch，实际是：${transport}" ;;
esac
if [[ ! "${pg_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  fail "${EXIT_CONFIG}" "LINGXI_REMOTE_PG_NAME 不是合法的容器名：${pg_name}"
fi
if [[ -n "${timeout_seconds}" && ! "${timeout_seconds}" =~ ^[1-9][0-9]*$ ]]; then
  fail "${EXIT_CONFIG}" "LINGXI_REMOTE_TIMEOUT_SECONDS 必须是正整数秒，实际是：${timeout_seconds}"
fi
read -r -a check_args <<<"${check_args_raw}"
if [[ ${#check_args[@]} -eq 0 ]]; then
  fail "${EXIT_CONFIG}" "LINGXI_REMOTE_CHECK_ARGS 为空。"
fi

# ---------- 本机：解析提交 ----------

target="${1:-HEAD}"
sha=$(git rev-parse --verify --quiet "${target}^{commit}") \
  || fail "${EXIT_CONFIG}" "解析不到提交：${target}"
if [[ -n "$(git status --porcelain)" ]]; then
  printf '工作树有未提交改动：本脚本只检提交 %s，不检工作树。\n' "${sha}" >&2
fi

# ---------- 远端调用与清理 ----------

remote_bash() {
  local script="$1"
  shift
  local cmd arg
  cmd="bash -c $(sq "${script}") lingxi-remote-check"
  for arg in "$@"; do
    cmd+=" $(sq "${arg}")"
  done
  "${ssh_cmd[@]}" "${cmd}"
}

local_tmp=""
temp_ref=""
remote_tmp=""
cleanup() {
  local rc=$?
  trap - EXIT
  if [[ -n "${temp_ref}" ]]; then
    git update-ref -d "${temp_ref}" 2>/dev/null || true
  fi
  if [[ -n "${local_tmp}" ]]; then
    rm -rf -- "${local_tmp}"
  fi
  if [[ -n "${remote_tmp}" ]]; then
    if ! remote_bash 'rm -rf -- "$1"' "${remote_tmp}" </dev/null >/dev/null 2>&1; then
      printf '警告：远端临时目录未能删除，请手动清理：%s\n' "${remote_tmp}" >&2
    fi
  fi
  exit "${rc}"
}
trap cleanup EXIT

# 与 check.sh 同款：KEY=value 逐行 read 进关联数组，不用 eval。
parse_kv() {
  local -n kv_out="$1"
  local text="$2"
  local key value
  while IFS='=' read -r key value; do
    [[ -z "${key}" ]] && continue
    # shellcheck disable=SC2034 # nameref 写的是调用方的关联数组。
    kv_out["${key}"]="${value}"
  done <<<"${text}"
}

# ---------- 远端预检 ----------

read -r -d '' preflight_script <<'EOF' || true
set -eu
w=$1; sha=$2
case "$w" in "~/"*) w="$HOME/${w#"~/"}" ;; esac
[ -d "$w" ] || { echo "远端工作目录不存在：$w" >&2; exit 3; }
git -C "$w" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "远端目录不是 git 工作树：$w" >&2; exit 3; }
dirty=$(git -C "$w" status --porcelain)
if [ -n "$dirty" ]; then
  echo "远端工作树不干净，拒绝 checkout；请先到远端处理这些改动：" >&2
  printf '%s\n' "$dirty" >&2
  exit 4
fi
command -v docker >/dev/null 2>&1 || { echo "远端缺少 docker，full 门禁要起一次性真库容器。" >&2; exit 5; }
echo "WORKDIR=$w"
if git -C "$w" cat-file -e "$sha^{commit}" 2>/dev/null; then echo HAS_SHA=1; else echo HAS_SHA=0; fi
basis=$(git -C "$w" rev-parse HEAD; git -C "$w" for-each-ref --format='%(objectname)' refs/remotes/origin/main refs/heads/main)
echo "BASIS=$(printf '%s ' $basis)"
if [ -x /usr/bin/time ]; then echo TIME_BIN=1; else echo TIME_BIN=0; fi
if command -v timeout >/dev/null 2>&1; then echo TIMEOUT_BIN=1; else echo TIMEOUT_BIN=0; fi
mkdir -p "$w/.dev-check"
echo "TMP=$(mktemp -d "$w/.dev-check/remote-tmp.XXXXXX")"
EOF

printf '远端预检：%s（目录 %s）\n' "${LINGXI_REMOTE_SSH}" "${remote_workdir}" >&2
if ! preflight_out=$(remote_bash "${preflight_script}" "${remote_workdir}" "${sha}" </dev/null); then
  fail "${EXIT_UNAVAILABLE}" "远端预检失败或 SSH 连不上（连接命令：${LINGXI_REMOTE_SSH}），不判绿。"
fi
declare -A pre=()
parse_kv pre "${preflight_out}"
remote_tmp="${pre[TMP]:-}"
for key in WORKDIR HAS_SHA BASIS TIME_BIN TIMEOUT_BIN TMP; do
  [[ -n "${pre[${key}]+set}" ]] || fail "${EXIT_UNAVAILABLE}" "远端预检输出里没有 ${key}，视为连接不完整。"
done
remote_workdir="${pre[WORKDIR]}"
if [[ -n "${timeout_seconds}" && "${pre[TIMEOUT_BIN]}" != "1" ]]; then
  fail "${EXIT_CONFIG}" "配置了 LINGXI_REMOTE_TIMEOUT_SECONDS，但远端没有 timeout 命令。"
fi

# ---------- 传输 + checkout ----------

read -r -d '' checkout_script <<'EOF' || true
set -eu
w=$1; tmp=$2; sha=$3; ref=$4; mode=$5
case "$mode" in
  bundle)
    cat >"$tmp/candidate.bundle"
    git -C "$w" bundle verify "$tmp/candidate.bundle" >"$tmp/verify.out" 2>&1 \
      || { cat "$tmp/verify.out" >&2; exit 6; }
    git -C "$w" fetch --quiet --no-tags "$tmp/candidate.bundle" "$ref"
    rm -f "$tmp/candidate.bundle"
    ;;
  fetch)
    git -C "$w" fetch --quiet --no-tags origin "$sha"
    ;;
  none) ;;
esac
git -C "$w" cat-file -e "$sha^{commit}" 2>/dev/null \
  || { echo "远端仍没有提交 $sha（传输方式：$mode）。" >&2; exit 6; }
git -C "$w" checkout --quiet --detach "$sha"
[ "$(git -C "$w" rev-parse HEAD)" = "$sha" ] || { echo "远端 checkout 后 HEAD 不是 $sha。" >&2; exit 6; }
[ -x "$w/scripts/dev/check.sh" ] || { echo "目标提交里没有 scripts/dev/check.sh。" >&2; exit 6; }
echo "REMOTE_HEAD=$(git -C "$w" rev-parse HEAD)"
EOF

bundle_ref="refs/lingxi-remote-check/${sha}"
if [[ "${pre[HAS_SHA]}" == "1" ]]; then
  printf '远端已有提交 %s，跳过传输。\n' "${sha}" >&2
  checkout_mode="none"
  checkout_input=/dev/null
elif [[ "${transport}" == "bundle" ]]; then
  mkdir -p "${repository_root}/.dev-check"
  local_tmp=$(mktemp -d "${repository_root}/.dev-check/remote-tmp.XXXXXX")
  temp_ref="${bundle_ref}"
  git update-ref "${temp_ref}" "${sha}"
  # 增量基线 = 远端已有且本机也认识的提交；一个都对不上就全量打包。
  read -r -a basis_list <<<"${pre[BASIS]}"
  exclude=()
  for basis in ${basis_list[@]+"${basis_list[@]}"}; do
    if git cat-file -e "${basis}^{commit}" 2>/dev/null; then
      exclude+=("^${basis}")
    fi
  done
  bundle_path="${local_tmp}/candidate.bundle"
  if ! git bundle create --quiet "${bundle_path}" "${temp_ref}" ${exclude[@]+"${exclude[@]}"} 2>/dev/null; then
    printf '增量 bundle 建不出来，改打全量 bundle。\n' >&2
    git bundle create --quiet "${bundle_path}" "${temp_ref}" \
      || fail "${EXIT_IOERR}" "本机 git bundle create 失败。"
  fi
  printf '传输：bundle %s 字节 → 远端 %s\n' "$(wc -c <"${bundle_path}" | tr -d ' ')" "${remote_tmp}" >&2
  checkout_mode="bundle"
  checkout_input="${bundle_path}"
else
  printf '传输：远端 git fetch origin %s（fetch 模式，要求该提交已推送）。\n' "${sha}" >&2
  checkout_mode="fetch"
  checkout_input=/dev/null
fi

if ! checkout_out=$(remote_bash "${checkout_script}" "${remote_workdir}" "${remote_tmp}" "${sha}" \
  "${bundle_ref}" "${checkout_mode}" <"${checkout_input}"); then
  fail "${EXIT_IOERR}" "远端取提交 / checkout 失败（方式：${checkout_mode}），不判绿。"
fi
declare -A co=()
parse_kv co "${checkout_out}"
[[ "${co[REMOTE_HEAD]:-}" == "${sha}" ]] \
  || fail "${EXIT_IOERR}" "远端 HEAD 回读不是目标提交：${co[REMOTE_HEAD]:-（空）}"
if [[ -n "${temp_ref}" ]]; then
  git update-ref -d "${temp_ref}" 2>/dev/null || true
  temp_ref=""
fi

# ---------- 远端运行 check.sh ----------

read -r -d '' run_script <<'EOF' || true
set -u
w=$1; tmp=$2; pg=$3; limit=$4; shift 4
cd "$w" || exit 6
# 与本机门禁前的既定姿势一致：结论不依赖主机默认 umask。
umask 022
export LINGXI_DEV_CHECK_PG_NAME="$pg"
prefix=()
if [ -x /usr/bin/time ]; then prefix=(/usr/bin/time -v -o "$tmp/time.txt"); fi
if [ -n "$limit" ]; then prefix+=(timeout -k 30 "$limit"); fi
${prefix[@]+"${prefix[@]}"} scripts/dev/check.sh "$@" </dev/null 2>&1 | tee "$tmp/run.log"
rc=${PIPESTATUS[0]}
echo "$rc" >"$tmp/exit_code"
exit "$rc"
EOF

mkdir -p "${repository_root}/.dev-check/remote-runs"
log_path="${repository_root}/.dev-check/remote-runs/${sha}-$(date -u +%Y%m%dT%H%M%SZ).log"
printf '远端运行：LINGXI_DEV_CHECK_PG_NAME=%s scripts/dev/check.sh %s（提交 %s，开始 %s）\n' \
  "${pg_name}" "${check_args_raw}" "${sha}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
run_start=$(date +%s)
set +e
remote_bash "${run_script}" "${remote_workdir}" "${remote_tmp}" "${pg_name}" "${timeout_seconds}" \
  "${check_args[@]}" </dev/null | tee "${log_path}.streaming"
ssh_rc=${PIPESTATUS[0]}
set -e
run_seconds=$(($(date +%s) - run_start))

# ---------- 回读 + 日志回传 ----------

read -r -d '' collect_script <<'EOF' || true
set -u
w=$1; tmp=$2; pg=$3
if [ -f "$tmp/exit_code" ]; then echo "EXIT_CODE=$(cat "$tmp/exit_code")"; else echo "EXIT_CODE="; fi
if [ -f "$tmp/time.txt" ]; then
  echo "MAX_RSS_KB=$(sed -n 's/.*Maximum resident set size (kbytes): *//p' "$tmp/time.txt" | tail -n 1)"
  echo "ELAPSED=$(sed -n 's/.*Elapsed (wall clock) time ([^)]*): *//p' "$tmp/time.txt" | tail -n 1)"
fi
echo "RESIDUE=$(docker ps -a --filter "name=^/${pg}\$" --format '{{.Names}}' 2>&1 | tr '\n' ' ')"
echo "DIRTY_LINES=$(git -C "$w" status --porcelain | wc -l | tr -d ' ')"
EOF

verdict_code=0
reasons=()
# 记一条失败原因；退出码只在尚未定码时取本条的，远端 check.sh 的退出码另行强制透传。
note_failure() {
  reasons+=("$2")
  if [[ "${verdict_code}" -eq 0 ]]; then
    verdict_code="$1"
  fi
}

collect_ok=0
declare -A res=()
if collect_out=$(remote_bash "${collect_script}" "${remote_workdir}" "${remote_tmp}" "${pg_name}" </dev/null); then
  parse_kv res "${collect_out}"
  collect_ok=1
else
  note_failure "${EXIT_UNAVAILABLE}" "远端结果回读失败（SSH 连不上或远端命令出错，SSH 返回 ${ssh_rc}）"
fi

if remote_bash 'cat -- "$1/run.log"' "${remote_tmp}" </dev/null >"${log_path}"; then
  rm -f "${log_path}.streaming"
else
  mv -f "${log_path}.streaming" "${log_path}" 2>/dev/null || true
  note_failure "${EXIT_IOERR}" "远端日志回传失败（本机只留下流式抄本）"
fi

remote_exit="${res[EXIT_CODE]:-}"
if [[ ! "${remote_exit}" =~ ^[0-9]+$ ]]; then
  remote_exit=""
  if [[ "${collect_ok}" -eq 1 ]]; then
    note_failure "${EXIT_UNAVAILABLE}" "远端 check.sh 没有写出退出码：命令被中断（SSH 返回 ${ssh_rc}）"
  fi
elif [[ "${remote_exit}" -ne 0 ]]; then
  if [[ "${remote_exit}" -eq 124 && -n "${timeout_seconds}" ]]; then
    reasons+=("远端 check.sh 超过 ${timeout_seconds} 秒被终止（退出码 124）")
  else
    reasons+=("远端 check.sh 退出码 ${remote_exit}")
  fi
  verdict_code="${remote_exit}"
fi

# 日志里可能有多条 Ran N（主套件之外还有定向小套件），取 N 最大的一条 = 主套件。
ran_line=$(grep -E -o 'Ran [0-9]+ tests? in [0-9.]+s' "${log_path}" 2>/dev/null | sort -t ' ' -k2,2n | tail -n 1 || true)
ran_count="未知"
if [[ -n "${ran_line}" ]]; then
  ran_count=$(printf '%s' "${ran_line}" | grep -E -o '[0-9]+' | head -n 1)
else
  note_failure "${EXIT_RESULT}" "日志里解析不到「Ran N tests」行"
fi

max_rss_text="未知"
if [[ "${res[MAX_RSS_KB]:-}" =~ ^[0-9]+$ ]]; then
  max_rss_text="${res[MAX_RSS_KB]}KB"
elif [[ "${pre[TIME_BIN]}" != "1" ]]; then
  printf '远端没有 /usr/bin/time，内存峰值记为「未知」。\n' >&2
fi

if [[ "${collect_ok}" -eq 1 ]]; then
  residue="${res[RESIDUE]:-}"
  residue="${residue% }"
  if [[ -n "${residue}" ]]; then
    note_failure "${EXIT_RESULT}" "远端残留容器：${residue}（请到远端 docker rm -f）"
  fi
  if [[ "${res[DIRTY_LINES]:-}" != "0" ]]; then
    note_failure "${EXIT_RESULT}" "远端工作树跑完后不干净（git status --porcelain 有 ${res[DIRTY_LINES]:-未知} 行）"
  fi
fi

verdict="失败"
if [[ "${verdict_code}" -eq 0 && ${#reasons[@]} -eq 0 && "${remote_exit}" == "0" ]]; then
  verdict="通过"
elif [[ "${verdict_code}" -eq 0 ]]; then
  verdict_code="${EXIT_RESULT}"
fi

if [[ ${#reasons[@]} -gt 0 ]]; then
  printf '失败原因：\n' >&2
  printf -- '- %s\n' "${reasons[@]}" >&2
fi
if [[ -n "${ran_line}" ]]; then
  printf '%s\n' "${ran_line}" >&2
fi
if [[ -n "${res[ELAPSED]:-}" ]]; then
  printf '远端 /usr/bin/time 墙钟：%s\n' "${res[ELAPSED]}" >&2
fi
printf '结论=%s 提交=%s 用时=%s Ran=%s 峰值内存=%s 日志=%s\n' \
  "${verdict}" "${sha}" "${run_seconds}" "${ran_count}" "${max_rss_text}" "${log_path}"
exit "${verdict_code}"
