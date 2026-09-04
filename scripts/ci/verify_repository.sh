#!/usr/bin/env bash
# 无网络、无业务系统副作用的仓库基础质量门禁。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

for required_command in git python3 shellcheck ruff; do
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
done < <(git ls-files 'scripts/*.sh' 'tests/*.sh' 'deploy/*.sh')

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

# ruff 锁精确版本 0.16.4：本机与 CI 装了不同版本会静默改变判定结果，同
# ShellCheck 那条纪律。规则集本身在 pyproject.toml [tool.ruff.lint]，本脚本
# 不重复声明一份规则清单。
actual_ruff_version=$(ruff --version | awk '{print $2}')
printf 'ruff 版本：%s\n' "${actual_ruff_version}"
# 只打印版本不校验 == 没校验：PATH 上排在前面的可能是另一个 ruff（例如用户
# 自己装的最新版）。用 gate_spec.py 现读的、工作流真正锁定的版本做期望值
# 交叉核对——本机 check.sh 已经把装了正确版本的 venv/bin 放进 PATH 最前面，
# CI 由安装 step 保证版本，这一步防的是两者之外的第三种情况：PATH 上还有一个
# 未预期的 ruff 抢先命中。
expected_ruff_version=$(python3 scripts/dev/gate_spec.py gate | sed -n 's/^RUFF_VERSION=//p')
if [[ -z "${expected_ruff_version}" ]]; then
  printf '从 scripts/dev/gate_spec.py 读不到 RUFF_VERSION，无法核对 ruff 版本。\n' >&2
  exit 1
fi
if [[ "${actual_ruff_version}" != "${expected_ruff_version}" ]]; then
  printf 'ruff 版本不一致：PATH 上实际是 %s，工作流锁定的是 %s——PATH 上可能还有另一个 ruff。\n' \
    "${actual_ruff_version}" "${expected_ruff_version}" >&2
  exit 1
fi

# git ls-files -z + mapfile -d ''：NUL 分隔，文件名里出现空格或换行也不会被
# 拆散成多个数组元素（独立审查 P2-2）。
mapfile -d '' -t tracked_python_files < <(git ls-files -z '*.py')
if ((${#tracked_python_files[@]} == 0)); then
  printf '没有找到受版本控制的 Python 文件。\n' >&2
  exit 1
fi
# --config pyproject.toml：显式指定配置文件来源，防止仓库里另外放一份
# ruff.toml/.ruff.toml 抢先生效（ruff 的配置发现顺序是就近优先，不显式指定时
# 一个写着 `select = []` 的旁路配置文件能让检查整体放行——独立审查实测坐实）。
# tests/test_ruff_config.py 另有断言禁止这两种文件名进入版本控制，两道防线。
ruff check --config pyproject.toml --no-cache "${tracked_python_files[@]}"
printf 'ruff check：通过\n'

# --force-exclude：exclude 只在 ruff 自己遍历目录时生效，显式传入的文件路径
# 默认会被无视——不带这个参数，六个贴线/冻结文件会被当成"待格式化"而不是
# "跳过"，[tool.ruff.format].exclude 形同虚设（实测坐实）。
ruff format --config pyproject.toml --check --force-exclude "${tracked_python_files[@]}"
printf 'ruff format --check：通过\n'

# src/lingxi/ 不允许任何抑制注释，唯一合法入口是 pyproject.toml 的
# per-file-ignores。大小写不敏感、`#` 后有无空格都要命中；除 noqa/fmt/ruff 的
# 裸/限定形态外，还要命中 ruff 实际认得的其他生成器抑制指令——独立审查实测
# 坐实 `# flake8: noqa`、`# ruff : noqa`（冒号前带空格）、
# `# isort: skip_file/off/skip/split`、`# yapf: disable` 都能让 ruff 真的放行
# 对应检查，而旧正则一条都不命中。
#
# 字符串字面量里恰好出现 "noqa" 等词会被一并命中——这是刻意接受的误伤：
# 失败关闭的方向是"该判红的一条都不能漏"，不是"一个字都不多判"；真的遇到这种
# 误伤，走 per-file-ignores 或改写那行字面量，不放宽这条 grep。
suppression_pattern='#\s*(noqa|fmt\s*:\s*(off|skip)|(ruff|flake8)\s*:\s*noqa|isort\s*:\s*(skip_file|skip|off|split)|yapf\s*:\s*disable)'
set +e
suppression_comments=$(git grep -rniE "${suppression_pattern}" -- 'src/lingxi/*.py')
suppression_grep_exit=$?
set -e
# git grep 退出码：0=有命中、1=无命中、>=2=grep 自身出错（例如正则写坏了）。
# 此前的 `|| true` 把这三种情况全部压成"当作没命中"，一条命中的抑制注释和
# 一次 grep 崩溃在门禁眼里长得一模一样——独立审查发现的问题。
if ((suppression_grep_exit == 0)); then
  printf 'src/lingxi/ 内发现门禁抑制注释，不允许（合法入口是 pyproject.toml 的 per-file-ignores）：\n%s\n' \
    "${suppression_comments}" >&2
  exit 1
elif ((suppression_grep_exit > 1)); then
  printf 'noqa/fmt 抑制注释扫描本身失败（git grep 退出码 %s），无法确认仓库是否干净。\n' \
    "${suppression_grep_exit}" >&2
  exit 1
fi
printf 'noqa/fmt 抑制注释扫描：通过\n'

python3 scripts/ci/check_markdown_links.py
python3 scripts/ci/check_project_skills.py
# 验收矩阵的三态状态列与合同条款覆盖清单。这两样此前只是散文约定：
# 断言可以没人认领、合同可以新增一节而没有任何断言，门禁照样全绿。
python3 scripts/ci/check_acceptance_matrix.py
# 代码框架第一节「文件体量棘轮」（Issue #238）：已超过阈值（1500 行）的文件登记在
# scripts/ci/size_ratchet_baseline.txt 里，只许变小、不许变大；未超阈值的文件不得
# 新超过阈值。基线由 --refresh 生成，拒绝被手工调大——见该脚本头注释。
python3 scripts/ci/check_size_ratchet.py
python3 scripts/ci/check_matrix_row_size_ratchet.py
# 函数体量与注释卫生两条棘轮，与文件体量棘轮同一纪律（只许收紧）：前者挡
# src/lingxi/ 下单个函数继续变长，后者挡三类"来历腐烂"信号继续变多。
python3 scripts/ci/check_function_size_ratchet.py
python3 scripts/ci/check_comment_ratchet.py
# 代码框架「二、三层之间的 import 规则」第一条（Issue #238）：core/ 不得 import
# adapters/、apps/ 或任何外部 SDK。用 ast 遍历整棵树，含函数内延迟导入。
python3 scripts/ci/check_core_layering.py
# 代码框架「三、横切约定」的归属核对（Issue #238）：把某句规则的权威记成产品合同
# 本身的断言必须能在 docs/产品合同与外部边界.md 正文里找到对应，找不到就红——这道
# 门禁挡的正是"凭据不进用户环境"这类被错记成产品权威的笔误（代码框架第三节，
# 2026-08-19 归属核对更正）；核对的是「归属已登记、登记与源句均未过期」，
# 不是判定这句话在语义上是否真的成立（见 check_contract_attribution.py 头注释）。
python3 scripts/ci/check_contract_attribution.py
# 开工必读集体量预算（产品负责人 2026-08-24）：代码框架 + 验证与门禁合计字节数
# 设硬上限，超限即红——防膨胀的结构性门禁，与代码体量棘轮同一思路。
python3 scripts/ci/check_docs_size_budget.py
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
# MCP 令牌加解密的互操作向量（Issue #156 / S-C-02）：那几组 AES 断言在
# `unittest discover` 里带着 skipUnless（缺 cryptography 就跳过，这对无依赖机器是对的），
# 但**门禁不能跟着跳过**——缺库时整组断言一条都没跑，输出却是绿的。本脚本缺库即明确失败，
# 并要求那几组的实际执行数非零、跳过数为零。纪律同上面的 alembic 检查。
python3 scripts/ci/check_crypto_vectors.py
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
  # 2026-08-23 #146 清退：tests/test_identity_postgres.sh 随 migrations/testing/
  # 001-003（Bot-Test 资产）一并删除；001 相关断言已由 tests/test_identity_postgres_records.py
  # 对正式表（006-008）覆盖，见 migrations/README.md。

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
