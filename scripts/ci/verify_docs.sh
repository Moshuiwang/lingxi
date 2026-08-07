#!/usr/bin/env bash
# 不安装依赖、不启动数据库或 Docker 的文档门禁。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

python3 scripts/ci/check_markdown_links.py
python3 scripts/ci/check_project_skills.py
python3 scripts/ci/check_acceptance_matrix.py

whitespace_files=$(git grep -Il -E '[[:blank:]]+$' -- . ':!.tmp/**' || true)
if [[ -n "${whitespace_files}" ]]; then
  printf '以下正式文件包含行尾空白：\n%s\n' "${whitespace_files}" >&2
  exit 1
fi

echo '文档门禁：全部通过'
