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
declared_python=$(sed -nE -e 's/^requires-python = ">=([0-9]+\.[0-9]+)"$/\1/p' pyproject.toml)
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
# 打印版本：本机与 CI 装了不同版本的 linter，是一种不会报错的分歧。
# CI 用 pip 锁定 shellcheck-py==0.11.0.1，本机请装同一个：
#   python3 -m pip install 'shellcheck-py==0.11.0.1'
printf 'ShellCheck：通过（%s）\n' "$(shellcheck --version | sed -n 's/^version: //p')"

python3 scripts/ci/check_markdown_links.py
python3 scripts/ci/check_project_skills.py
# 验收矩阵的三态状态列与合同条款覆盖清单。这两样此前只是散文约定：
# 断言可以没人认领、合同可以新增一节而没有任何断言，门禁照样全绿。
python3 scripts/ci/check_acceptance_matrix.py
# Issue #75：正式 PostgreSQL 连接必须走唯一工厂，迁移入口必须有独立且有限的连接参数。
# 该检查登记在 #75 的共享位置；#76 的制品 / 进程依赖清单检查按编排者约定后续追加。
python3 scripts/ci/check_db_timeouts.py
# Issue #190（产品负责人 2026-08-17 决定）：content.toml 的用户可见文案变了，
# [meta] version 必须跟着递增。版本号会随投递与审计事件落库，是"用户当时看到哪版
# 文案"的追溯依据；此前它只是口头约定，改文案不改版本没有任何东西会红。
python3 scripts/ci/check_content_version.py
# alembic revision 链的结构性约束（Issue #53）：head 唯一、无孤儿 revision、
# downgrade 不是静默空实现、README 的 revision id 未过期。**不连数据库**，因此
# 没有容器的环境里也照跑——这几类缺陷恰恰最容易在"本机没起容器"时溜过去。
# 没装 alembic 时它明确失败而不是跳过。
python3 scripts/ci/check_alembic_revisions.py
# 断言 V-部署-08（Issue #62）：运行时依赖全部在 pyproject 声明、安全边界组件锁精确版本。
# 这条断言此前被标为「已认领」却**没有执行脚本**（Issue #58 遗留），因此从未真正守住过。
# 扫描覆盖**函数内的延迟导入**——本仓库的第三方 import 全都写在函数体里，
# 只扫模块级等于没扫。
python3 scripts/ci/check_runtime_dependencies.py
# V-部署-10 / Issue #76：反向枚举 src/lingxi/，核对正式制品与各进程依赖清单；
# 这里只跑源码清单，不假设工作树已经 pip install，完整制品检查仍由 CI / 镜像门禁执行。
python3 scripts/ci/check_installed_package.py --source-only
# 部署编排的静态契约（Issue #62）：停止宽限期与源码常量联动、凭据路径落在持久卷、
# 生产 compose 零构建定义、镜像 tag 不可变、非 root。刻意不依赖 docker 与 YAML 库，
# 这样一台没装 docker 的开发机也能跑出与 CI 相同的结论。
python3 scripts/ci/check_deploy_contract.py

# 半开状态守卫：有容器却没有 DSN 时，Python 真库断言会静默跳过、门禁却照样绿。
# 这种「看起来跑了真库」的假信心必须直接失败（PR #48 独立复查发现）。
if [[ -n "${LINGXI_POSTGRES_CONTAINER:-}" && -z "${LINGXI_POSTGRES_DSN:-}" ]]; then
  printf '设置了 LINGXI_POSTGRES_CONTAINER 却没有 LINGXI_POSTGRES_DSN：真库断言会被静默跳过。\n' >&2
  exit 1
fi

if [[ -d tests ]]; then
  PYTHONPATH=src python3 -m unittest discover -s tests -v
  printf 'Python 自动测试：通过\n'
fi

if [[ -n "${LINGXI_POSTGRES_CONTAINER:-}" ]]; then
  tests/test_identity_postgres.sh
  printf 'PostgreSQL 自动测试：通过\n'

  # 生产表结构在过渡期有两条血统：顶层编号 SQL（旧库按它建成）与 alembic revision 链
  # （新库按它建）。这一步把两条都实际建出来并要求 pg_dump 逐字节相等，然后 alembic
  # 继续前滚到 head（Issue #53；migrations/testing/ 是测试资产，不属于生产链，
  # 见 migrations/README.md）。只测其中一条时，两条血统的对象名字可以悄悄分岔。
  scripts/ci/check_migration_chain.sh
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
