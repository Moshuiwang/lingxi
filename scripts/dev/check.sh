#!/usr/bin/env bash
# 一键复现门禁环境 + 本机分层验证（Issue #236）。
#
# 用法：
#   scripts/dev/check.sh                    # 按当前改动自动分层（对比 --base，默认 main）
#   scripts/dev/check.sh docs|fast|full      # 强制指定层级，跳过自动判定
#   scripts/dev/check.sh --base <ref>        # 指定分层对比基线
#   scripts/dev/check.sh --committed-only    # 分层判定只看已提交差异，不含工作树改动
#   scripts/dev/check.sh --print-mode        # 只打印分层结论，不安装依赖、不运行任何检查
#   scripts/dev/check.sh --keep-db           # full 模式结束后不清理临时真库容器
#
# 三层与 CI 的对应关系（验证与门禁第五节 / 第十一节）：
#   docs  等价于 Story / docs 与 Epic Full / docs：只跑 scripts/ci/verify_docs.sh，
#         不装依赖、不起数据库或 Docker。
#   fast  等价于 Story Fast 的 `fast` job：extras 组合现读自
#         .github/workflows/story.yml，不启动真库、不构建镜像。
#   full  等价于 Epic Full 的 `gate` job：extras 组合现读自
#         .github/workflows/ci.yml，起一次性 postgres:16-alpine（trust 认证、
#         lingxi_test 库）真库，跑完自动清理。
#
# **extras 组合、shellcheck 版本、Python 版本、真库参数不在本脚本里硬编码**：全部由
# scripts/dev/gate_spec.py 从上述两份工作流 YAML 现读。这是 Issue #236 的明确约束——
# 不允许「本机一份、门禁一份」两处清单迟早漂移。工作流改了这些值，本脚本下一次运行
# 就自动跟着变；工作流的写法本身变了导致解析不出来，gate_spec.py 会响亮失败并说明
# 原因，不会安静地退回旧值。
#
# **本入口对齐的是依赖版本与真实数据库，不是操作系统本身**：GitHub Actions runner 是
# ubuntu-24.04，本机操作系统不保证逐位一致。PR #233 暴露的那类漂移（extras 组合、
# Python 依赖版本）正是本入口要消灭的维度；操作系统级差异不在本 Issue 范围内
# （历史上也没有出过这类事故）。
#
# 冻结前仍必须跑一次 full（即完整 Epic Full 门禁）；分层判定不重新实现规则，
# 直接调用 scripts/dev/local_layer.py（进而调用 scripts/ci/classify_story_changes.py
# 的同一个 classify() 函数），因此本机结论与 CI 实际会跑哪一层保证一致。

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

usage() {
  cat <<'EOF'
用法：scripts/dev/check.sh [docs|fast|full] [选项]

选项：
  --base <ref>        分层判定的对比基线（默认 main）
  --committed-only     分层判定只看 base..HEAD 已提交差异，不含工作树未提交内容
  --print-mode         只打印分层结论（docs/fast/full），不做任何安装或检查
  --keep-db            full 模式结束后保留临时真库容器（默认用完即删）
  -h, --help           显示本帮助
EOF
}

mode_arg=""
base_ref="main"
keep_db=0
print_mode_only=0
committed_only=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    docs | fast | full)
      mode_arg="$1"
      shift
      ;;
    --base)
      base_ref="$2"
      shift 2
      ;;
    --keep-db)
      keep_db=1
      shift
      ;;
    --print-mode)
      print_mode_only=1
      shift
      ;;
    --committed-only)
      committed_only=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf '未知参数：%s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

for required_command in git python3; do
  command -v "${required_command}" >/dev/null || {
    printf '缺少命令：%s\n' "${required_command}" >&2
    exit 1
  }
done

if [[ -z "${mode_arg}" ]]; then
  layer_args=(--base "${base_ref}")
  if [[ "${committed_only}" -eq 1 ]]; then
    layer_args+=(--committed-only)
  fi
  mode=$(python3 scripts/dev/local_layer.py "${layer_args[@]}")
  printf '本机分层判定（对比 %s，复用 classify_story_changes.classify）：%s\n' "${base_ref}" "${mode}" >&2
else
  mode="${mode_arg}"
fi

if [[ "${print_mode_only}" -eq 1 ]]; then
  printf '%s\n' "${mode}"
  exit 0
fi

# 选 Python 解释器：优先用与门禁声明版本一致的 python<major.minor>，本机没有这个
# 版本化二进制时退回裸 python3——与 verify_repository.sh 自身的版本校验同一个思路，
# 缺依赖必须让调用方看得见，而不是安静地用一个不满足声明版本的解释器建环境。
pick_python() {
  local wanted="$1"
  if command -v "python${wanted}" >/dev/null 2>&1; then
    command -v "python${wanted}"
    return 0
  fi
  command -v python3
}

# 建一个只装指定 extras 的干净虚拟环境。**默认走 `python -m venv` + venv 自带的
# pip**——这与门禁的 `python3 -m pip install` 是同一套工具链，命令行为最贴近；
# 只有 venv 模块本身不可用（例如系统裁掉了 ensurepip）且装了 uv 时才退回 uv，
# 那是 tz 这台机器系统 python3 没有 pip 模块时的已知救场路径，不是首选。
build_venv() {
  local venv_dir="$1"
  local python_version="$2"
  shift 2
  local extras_spec=("$@")

  if [[ -d "${venv_dir}" ]]; then
    printf '复用已存在的虚拟环境：%s\n' "${venv_dir}" >&2
  else
    local base_python
    base_python=$(pick_python "${python_version}")
    printf '用 %s 建虚拟环境：%s\n' "${base_python}" "${venv_dir}" >&2
    if "${base_python}" -m venv "${venv_dir}" 2>/dev/null && [[ -x "${venv_dir}/bin/pip" ]]; then
      :
    elif command -v uv >/dev/null 2>&1; then
      rm -rf "${venv_dir}"
      printf '`python -m venv` 不可用或没带 pip，退回 uv venv（本机已知救场路径）。\n' >&2
      uv venv --python "${base_python}" "${venv_dir}" >&2
    else
      printf '无法建出可用的虚拟环境：%s -m venv 失败，且本机没有 uv 可退回。\n' "${base_python}" >&2
      exit 1
    fi
  fi

  if [[ -x "${venv_dir}/bin/pip" ]]; then
    "${venv_dir}/bin/pip" install --quiet "${extras_spec[@]}"
  elif command -v uv >/dev/null 2>&1; then
    uv pip install --python "${venv_dir}/bin/python" "${extras_spec[@]}" >&2
  else
    printf '虚拟环境 %s 没有 pip，且本机没有 uv 可退回。\n' "${venv_dir}" >&2
    exit 1
  fi
}

# 与 CI 同理：制品完整性检查必须在仓库目录之外运行，否则只是又测了一遍源码树
# （PYTHONPATH=src 的测试跑法与 pip 装出来的制品分叉，只有在仓库外才会暴露）。
check_installed_package_outside_repo() {
  local python_bin="$1"
  local work
  work=$(mktemp -d)
  ( cd "${work}" && "${python_bin}" "${repository_root}/scripts/ci/check_installed_package.py" )
  rm -rf "${work}"
}

run_docs() {
  scripts/ci/verify_docs.sh
}

run_fast() {
  local spec
  spec=$(python3 scripts/dev/gate_spec.py fast)
  eval "${spec}"
  printf 'Story Fast 环境配方（现读自 .github/workflows/story.yml）：extras=%s shellcheck=%s python=%s\n' \
    "${EXTRAS}" "${SHELLCHECK_VERSION}" "${PYTHON_VERSION}" >&2

  local venv_dir="${repository_root}/.dev-check/venv-fast"
  mkdir -p "${repository_root}/.dev-check"
  build_venv "${venv_dir}" "${PYTHON_VERSION}" ".[${EXTRAS}]" "shellcheck-py==${SHELLCHECK_VERSION}"

  check_installed_package_outside_repo "${venv_dir}/bin/python"
  "${venv_dir}/bin/python" scripts/ci/check_agent_sdk_binding.py

  # Story Fast 的 fast job 不设 LINGXI_POSTGRES_CONTAINER/DSN：无真库、无镜像。
  PATH="${venv_dir}/bin:${PATH}" scripts/ci/verify_repository.sh
}

full_pg_container=""
cleanup_full_pg() {
  if [[ -z "${full_pg_container}" ]]; then
    return 0
  fi
  if [[ "${keep_db}" -eq 1 ]]; then
    printf '保留临时真库容器（--keep-db）：%s，用完请自行 docker rm -f %s\n' \
      "${full_pg_container}" "${full_pg_container}" >&2
    return 0
  fi
  docker rm -f "${full_pg_container}" >/dev/null 2>&1 || true
  printf '已清理临时真库容器：%s\n' "${full_pg_container}" >&2
}

run_full() {
  local spec
  spec=$(python3 scripts/dev/gate_spec.py gate)
  eval "${spec}"
  printf 'Epic Full / gate 环境配方（现读自 .github/workflows/ci.yml）：extras=%s shellcheck=%s python=%s postgres=%s\n' \
    "${EXTRAS}" "${SHELLCHECK_VERSION}" "${PYTHON_VERSION}" "${POSTGRES_IMAGE}" >&2

  command -v docker >/dev/null || {
    printf '缺少 docker：full 模式要起一次性真库容器，与 Epic Full / gate 同构。\n' >&2
    exit 1
  }

  local venv_dir="${repository_root}/.dev-check/venv-full"
  mkdir -p "${repository_root}/.dev-check"
  build_venv "${venv_dir}" "${PYTHON_VERSION}" ".[${EXTRAS}]" "shellcheck-py==${SHELLCHECK_VERSION}"

  check_installed_package_outside_repo "${venv_dir}/bin/python"
  "${venv_dir}/bin/python" scripts/ci/check_agent_sdk_binding.py

  # 容器名可用 LINGXI_DEV_CHECK_PG_NAME 覆盖（例如受控验证要求用一个可辨识的
  # 专属名字）；不给时按 PID 生成，避免同一台机器上多次调用互相冲突或碰撞到
  # 别的代理正在用的容器（AGENTS.md：共享外部通道同一时刻只允许一个客户端）。
  full_pg_container="${LINGXI_DEV_CHECK_PG_NAME:-lingxi-dev-check-pg-$$}"
  if docker inspect "${full_pg_container}" >/dev/null 2>&1; then
    printf '容器名 %s 已存在：本工具只创建自己的一次性容器，不复用/不接管既有容器。\n' \
      "${full_pg_container}" >&2
    printf '请先确认它不是别的会话正在用的容器，再手动清理或换一个 LINGXI_DEV_CHECK_PG_NAME。\n' >&2
    exit 1
  fi
  trap cleanup_full_pg EXIT

  docker run -d --name "${full_pg_container}" \
    -e "POSTGRES_HOST_AUTH_METHOD=${POSTGRES_AUTH_METHOD}" \
    -e "POSTGRES_DB=${POSTGRES_DB}" \
    -P \
    "${POSTGRES_IMAGE}" >/dev/null

  local host_port
  host_port=$(docker inspect -f '{{ (index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort }}' "${full_pg_container}")

  local tries=0
  until docker exec "${full_pg_container}" pg_isready -U postgres -d "${POSTGRES_DB}" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if ((tries > 30)); then
      printf '临时真库 %s 探测 30 次仍未就绪。\n' "${full_pg_container}" >&2
      exit 1
    fi
    sleep 1
  done
  printf '临时真库已就绪：容器=%s 端口=%s（trust 认证，与本机既有 scram 测试库互不影响）\n' \
    "${full_pg_container}" "${host_port}" >&2

  LINGXI_POSTGRES_CONTAINER="${full_pg_container}" \
    LINGXI_POSTGRES_DSN="postgresql://postgres@localhost:${host_port}/${POSTGRES_DB}" \
    PATH="${venv_dir}/bin:${PATH}" \
    scripts/ci/verify_repository.sh
}

case "${mode}" in
  docs) run_docs ;;
  fast) run_fast ;;
  full) run_full ;;
  *)
    printf '未知层级：%s（分层判定只应该产出 docs/fast/full）\n' "${mode}" >&2
    exit 1
    ;;
esac

printf '本机 %s 层验证：全部通过\n' "${mode}"
