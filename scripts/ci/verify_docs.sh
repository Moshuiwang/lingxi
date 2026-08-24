#!/usr/bin/env bash
# 不安装依赖、不启动数据库或 Docker 的文档门禁。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

python3 scripts/ci/check_markdown_links.py
python3 scripts/ci/check_project_skills.py
python3 scripts/ci/check_acceptance_matrix.py
# 归属核对（Issue #238）也接进纯文档路径：GROUNDED_ATTRIBUTIONS 登记表中
# 半数以上条目指向 docs/ 下的 .md，纯文档 PR 是它们唯一可能被改动的入口——只接进
# verify_repository.sh 等于给这些登记留了一条从不核对的路径（2026-08-19
# 三路独立复查实测坐实：往架构设计.md 追加一条未登记的归属，verify_docs.sh
# 之前会 EXIT=0）。同 check_acceptance_matrix.py 一样跨查合同文档，
# 不连数据库、不装依赖，实测耗时量级一致（见代码框架「三、横切约定」）。
python3 scripts/ci/check_contract_attribution.py
# 开工必读集体量预算（产品负责人 2026-08-24）：代码框架 + 验证与门禁合计字节数
# 设硬上限，超限即红——防膨胀的结构性门禁，与代码体量棘轮同一思路。
python3 scripts/ci/check_docs_size_budget.py

whitespace_files=$(git grep -Il -E '[[:blank:]]+$' -- . ':!.tmp/**' || true)
if [[ -n "${whitespace_files}" ]]; then
  printf '以下正式文件包含行尾空白：\n%s\n' "${whitespace_files}" >&2
  exit 1
fi

echo '文档门禁：全部通过'
