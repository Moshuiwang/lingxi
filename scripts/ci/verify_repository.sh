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
