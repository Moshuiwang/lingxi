#!/usr/bin/env bash
# 无网络、无业务系统副作用的仓库基础质量门禁。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

for required_command in git python3 shellcheck; do
  command -v "${required_command}" >/dev/null || {
    printf '缺少 CI 命令：%s\n' "${required_command}" >&2
    exit 1
  }
done

# 门禁必须跑在项目声明支持的解释器上。此前用的是裸 python3：CI 上恰好是 3.12
# 所以一直没暴露，但本地 python3 可能是 3.9——那样门禁会在一个项目不支持的
# 解释器上给出绿灯，属于假信心，比没有门禁更危险。
declared_python=$(sed -n 's/^requires-python = ">=\([0-9]\+\.[0-9]\+\)"$/\1/p' pyproject.toml)
if [[ -z "${declared_python}" ]]; then
  printf 'pyproject.toml 里读不到 requires-python，无法校验解释器版本。\n' >&2
  exit 1
fi
actual_python=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! python3 -c 'import sys; d=sys.argv[1].split("."); sys.exit(0 if sys.version_info[:2] >= (int(d[0]), int(d[1])) else 1)' "${declared_python}"; then
  printf 'python3 是 %s，低于 pyproject.toml 声明的 %s。请用 python%s 运行本门禁。\n' \
    "${actual_python}" "${declared_python}" "${declared_python}" >&2
  exit 1
fi
printf '解释器版本：python3 = %s，满足声明的 >=%s\n' "${actual_python}" "${declared_python}"

tracked_scripts=()
while IFS= read -r script_path; do
  tracked_scripts+=("${script_path}")
done < <(git ls-files 'scripts/*.sh' 'tests/*.sh')

if ((${#tracked_scripts[@]} == 0)); then
  printf '没有找到受版本控制的 Bash 脚本。\n' >&2
  exit 1
fi

bash -n "${tracked_scripts[@]}"
printf 'Bash 语法：通过\n'

shellcheck --severity=warning "${tracked_scripts[@]}"
printf 'ShellCheck：通过\n'

python3 scripts/ci/check_markdown_links.py
python3 scripts/ci/check_project_skills.py

if [[ -d tests ]]; then
  PYTHONPATH=src python3 -m unittest discover -s tests -v
  printf 'Python 自动测试：通过\n'
fi

if [[ -n "${LINGXI_POSTGRES_CONTAINER:-}" ]]; then
  tests/test_identity_postgres.sh
  printf 'PostgreSQL 自动测试：通过\n'
fi

whitespace_files=$(git grep -Il -E '[[:blank:]]+$' -- . ':!.tmp/**' || true)
if [[ -n "${whitespace_files}" ]]; then
  printf '以下正式文件包含行尾空白：\n%s\n' "${whitespace_files}" >&2
  exit 1
fi
printf '行尾空白：通过\n'

sensitive_config_files=$(
  git ls-files |
    awk -F/ '
      $NF == ".env" { print; next }
      $NF ~ /^\.env\./ && $NF != ".env.example" { print }
    '
)
if [[ -n "${sensitive_config_files}" ]]; then
  printf '以下敏感配置文件不应进入版本控制：\n%s\n' "${sensitive_config_files}" >&2
  exit 1
fi

private_key_files=$(
  git grep -Il -E -- '-----BEGIN ([A-Z0-9]+ )?PRIVATE KEY-----' -- . || true
)
if [[ -n "${private_key_files}" ]]; then
  printf '以下文件疑似包含私钥：\n%s\n' "${private_key_files}" >&2
  exit 1
fi
printf '敏感配置文件：通过\n'

printf 'CI 基础质量门禁：全部通过\n'
