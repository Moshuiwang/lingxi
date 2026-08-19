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
#   scripts/dev/check.sh --reuse-venv        # 复用已存在的虚拟环境，跳过默认的重建
#
# 三层与 CI 的对应关系（验证与门禁第五节 / 第十一节）：
#   docs  等价于 Story / docs 与 Epic Full / docs：只跑 scripts/ci/verify_docs.sh，
#         不装依赖、不起数据库或 Docker。
#   fast  等价于 Story / code fast：extras 组合现读自
#         .github/workflows/story.yml，不启动真库、不构建镜像，也不跑
#         workers/oauth-bridge 的 Node 校验（见 docs/技术设计/验证与门禁.md
#         第十一节「不做什么」列，Node 依赖有自己的 lockfile 保证复现）。
#   full  等价于 Epic Full / gate**这一个作业**（不是整个 Epic Full）：extras 组合
#         现读自 .github/workflows/ci.yml，起一次性 postgres:16-alpine（trust 认证、
#         lingxi_test 库）真库，跑完自动清理；同样不跑 Node 校验，也不含
#         Epic Full / extras 六条干净环境腿与 Epic Full / image 的双路径可复现构建、
#         部署契约、compose 结构核对——那些仍只在 CI 里跑。
#
# **extras 组合、shellcheck 版本、Python 版本、真库参数不在本脚本里硬编码**：全部由
# scripts/dev/gate_spec.py 从上述两份工作流 YAML 现读。这是 Issue #236 的明确约束——
# 不允许「本机一份、门禁一份」两处清单迟早漂移。工作流改了这些值，本脚本下一次运行
# 就自动跟着变；工作流的写法本身变了导致解析不出来，gate_spec.py 会响亮失败并说明
# 原因，不会安静地退回旧值。gate_spec.py 的输出只按 KEY=value 逐行解析，**不使用
# eval**——工作流 YAML 里的取值来自检出的分支内容，不受信任，eval 会把其中的
# `$(...)` 当命令执行（独立审查实测：把 POSTGRES_DB 改成
# `lingxi_test$(id>/tmp/PWNED)`，eval 真的执行了它）。
#
# **虚拟环境默认每次重建，不做「存在即复用」的缓存**：本机装了门禁不装的包（无论是
# 手工调试时装的、旧配方残留的、还是上游传递依赖变化带进来的）如果被静默复用，
# 这个工具就会给出它本该消灭的那种假信心——PR #233 的教训正是「本机环境比门禁多装了
# 东西，本机全绿、CI 直接 ERROR」。需要跳过重建加速反复调用时用 `--reuse-venv`
# 显式选择，默认不这样做。
#
# **本入口对齐的是依赖版本与真实数据库，不是操作系统本身**：GitHub Actions runner 是
# ubuntu-24.04，本机操作系统不保证逐位一致。PR #233 暴露的那类漂移（extras 组合、
# Python 依赖版本）正是本入口要消灭的维度；操作系统级差异不在本 Issue 范围内
# （历史上也没有出过这类事故）。
#
# 冻结前仍必须跑一次**完整的 CI Epic Full**（本机 full 只等价于其中的 gate 作业，
# 不能替代）；分层判定不重新实现规则，直接调用 scripts/dev/local_layer.py（进而调用
# scripts/ci/classify_story_changes.py 的同一个 classify() 函数），因此本机结论与
# CI 实际会跑哪一层保证一致。

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
  --reuse-venv         复用已存在的虚拟环境，跳过默认的「每次重建」
  -h, --help           显示本帮助
EOF
}

mode_arg=""
base_ref="main"
keep_db=0
print_mode_only=0
committed_only=0
reuse_venv=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    docs | fast | full)
      if [[ -n "${mode_arg}" ]]; then
        printf '层级参数给了两次：先是 %s，又给了 %s。只能指定一个层级。\n' "${mode_arg}" "$1" >&2
        exit 1
      fi
      mode_arg="$1"
      shift
      ;;
    --base)
      if [[ $# -lt 2 ]]; then
        printf -- '--base 需要一个参数（对比基线），例如 --base origin/main\n' >&2
        exit 1
      fi
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
    --reuse-venv)
      reuse_venv=1
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

# 安全解析 gate_spec.py 的 `KEY=value` 逐行输出，填进调用方传入的关联数组。
# **不用 eval**：工作流 YAML 的取值来自检出的分支内容，不受信任；`read` 只把整行
# 当纯文本赋值，永远不会把其中的内容当 shell 语法执行，天然免疫命令替换注入。
load_spec() {
  local -n out_map="$1"
  shift
  local key value
  while IFS='=' read -r key value; do
    [[ -z "${key}" ]] && continue
    # shellcheck disable=SC2034 # nameref：写 out_map 就是写调用方传入的关联数组，
    # ShellCheck 认不出 `local -n` 间接赋值，这是已知的误报模式。
    out_map["${key}"]="${value}"
  done < <("$@")
}

require_spec_key() {
  local -n map_ref="$1"
  local key="$2"
  if [[ -z "${map_ref[${key}]+set}" || -z "${map_ref[${key}]}" ]]; then
    printf 'gate_spec.py 输出里没有 %s，环境配方解析失败。\n' "${key}" >&2
    exit 1
  fi
}

# 选 Python 解释器：优先用与门禁声明版本一致的 python<major.minor>，本机没有这个
# 版本化二进制时退回裸 python3。**这一步的退回只是「尝试」，不是「承诺」**——
# 建完虚拟环境之后必须用 verify_venv_python_version 回读实际版本核对，退回到的
# 解释器版本不符时必须响亮失败，不能让脚本打印着「python=3.13」却悄悄用别的版本
# 建环境、还让 verify_repository.sh 的 `>=` 校验照样放行。
pick_python() {
  local wanted="$1"
  if command -v "python${wanted}" >/dev/null 2>&1; then
    command -v "python${wanted}"
    return 0
  fi
  command -v python3
}

verify_venv_python_version() {
  local venv_dir="$1"
  local wanted="$2"
  local actual
  actual=$("${venv_dir}/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  if [[ "${actual}" != "${wanted}" ]]; then
    printf '虚拟环境 %s 的解释器是 python %s，与配方声明的 %s 不符：本机大概率没有装 python%s。请安装后重试；不能用别的版本冒充，那样「与门禁逐位一致」的说法就是假的。\n' \
      "${venv_dir}" "${actual}" "${wanted}" "${wanted}" >&2
    exit 1
  fi
  printf '虚拟环境解释器版本核对：python %s（与配方一致）\n' "${actual}" >&2
}

# 建一个只装指定 extras 的干净虚拟环境。**默认每次重建**：本机装了门禁不装的包
# （手工调试时装的、旧配方残留的、上游传递依赖变化带进来的）如果被静默复用，
# 这个工具就会给出它本该消灭的那种假信心（见文件头注释、独立审查 F1）。
# `--reuse-venv` 是显式选择的逃生口，用户自己承担「这次没有重新核验」的代价。
build_venv() {
  local venv_dir="$1"
  local python_version="$2"
  shift 2
  local extras_spec=("$@")

  if [[ -d "${venv_dir}" ]]; then
    if [[ "${reuse_venv}" -eq 1 ]]; then
      printf '复用已存在的虚拟环境（--reuse-venv，未重新核验内容）：%s\n' "${venv_dir}" >&2
    else
      printf '默认重建虚拟环境（避免复用带来的假信心，--reuse-venv 可跳过）：%s\n' "${venv_dir}" >&2
      rm -rf "${venv_dir}"
    fi
  fi

  if [[ ! -d "${venv_dir}" ]]; then
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
    verify_venv_python_version "${venv_dir}" "${python_version}"
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

# 与 gate/fast job 末尾「校验没有改写受版本控制的文件」同一条检查（Issue #236
# 独立审查 F6）：本机门禁跑完不该在工作树里留下未提交的改动。
check_git_tree_is_clean() {
  local dirty
  dirty=$(git status --porcelain)
  if [[ -n "${dirty}" ]]; then
    printf '本机验证过程改写了工作树（与 gate/fast job 的同名检查同一条规则）：\n%s\n' "${dirty}" >&2
    exit 1
  fi
  printf '工作树洁净：本机验证没有改写受版本控制的文件\n' >&2
}

run_docs() {
  scripts/ci/verify_docs.sh
}

run_fast() {
  local -A spec=()
  load_spec spec python3 scripts/dev/gate_spec.py fast
  for key in EXTRAS SHELLCHECK_VERSION PYTHON_VERSION; do
    require_spec_key spec "${key}"
  done
  printf 'Story Fast 环境配方（现读自 .github/workflows/story.yml）：extras=%s shellcheck=%s python=%s\n' \
    "${spec[EXTRAS]}" "${spec[SHELLCHECK_VERSION]}" "${spec[PYTHON_VERSION]}" >&2

  local venv_dir="${repository_root}/.dev-check/venv-fast"
  mkdir -p "${repository_root}/.dev-check"
  build_venv "${venv_dir}" "${spec[PYTHON_VERSION]}" ".[${spec[EXTRAS]}]" "shellcheck-py==${spec[SHELLCHECK_VERSION]}"

  check_installed_package_outside_repo "${venv_dir}/bin/python"
  "${venv_dir}/bin/python" scripts/ci/check_agent_sdk_binding.py

  # Story Fast 的 fast job 不设 LINGXI_POSTGRES_CONTAINER/DSN：无真库、无镜像。
  PATH="${venv_dir}/bin:${PATH}" scripts/ci/verify_repository.sh
  check_git_tree_is_clean
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
  local -A spec=()
  load_spec spec python3 scripts/dev/gate_spec.py gate
  for key in EXTRAS SHELLCHECK_VERSION PYTHON_VERSION POSTGRES_IMAGE POSTGRES_AUTH_METHOD POSTGRES_DB; do
    require_spec_key spec "${key}"
  done
  printf 'Epic Full / gate 环境配方（现读自 .github/workflows/ci.yml）：extras=%s shellcheck=%s python=%s postgres=%s\n' \
    "${spec[EXTRAS]}" "${spec[SHELLCHECK_VERSION]}" "${spec[PYTHON_VERSION]}" "${spec[POSTGRES_IMAGE]}" >&2

  command -v docker >/dev/null || {
    printf '缺少 docker：full 模式要起一次性真库容器，与 Epic Full / gate 同构。\n' >&2
    exit 1
  }

  local venv_dir="${repository_root}/.dev-check/venv-full"
  mkdir -p "${repository_root}/.dev-check"
  build_venv "${venv_dir}" "${spec[PYTHON_VERSION]}" ".[${spec[EXTRAS]}]" "shellcheck-py==${spec[SHELLCHECK_VERSION]}"

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
    -e "POSTGRES_HOST_AUTH_METHOD=${spec[POSTGRES_AUTH_METHOD]}" \
    -e "POSTGRES_DB=${spec[POSTGRES_DB]}" \
    -P \
    "${spec[POSTGRES_IMAGE]}" >/dev/null

  local host_port
  host_port=$(docker inspect -f '{{ (index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort }}' "${full_pg_container}")

  local tries=0
  until docker exec "${full_pg_container}" pg_isready -U postgres -d "${spec[POSTGRES_DB]}" >/dev/null 2>&1; do
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
    LINGXI_POSTGRES_DSN="postgresql://postgres@localhost:${host_port}/${spec[POSTGRES_DB]}" \
    PATH="${venv_dir}/bin:${PATH}" \
    scripts/ci/verify_repository.sh
  check_git_tree_is_clean
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
