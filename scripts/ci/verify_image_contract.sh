#!/usr/bin/env bash
# 对**已构建的镜像**逐条核对部署契约（Issue #62 / S11）。
#
#   scripts/ci/verify_image_contract.sh <scheduler 引用> <worker 引用> <migrate 引用>
#
# 与 scripts/ci/check_deploy_contract.py 的分工：那边看**源文件写得对不对**（不需要
# docker，挂在 verify_repository.sh 上），这边看**构建出来的东西是不是那么回事**
# （需要 docker，挂在镜像构建 job 上）。两者不可互相替代：Dockerfile 写了 USER 不等于
# 镜像真的以非 root 跑，`.dockerignore` 写了排除不等于 rootfs 里真的没有那些文件。

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

scheduler_image=${1:-lingxi-scheduler:probe}
worker_image=${2:-lingxi-worker:probe}
migrate_image=${3:-lingxi-migrate:probe}
gateway_image=${4:-lingxi-gateway:probe}

# **四个镜像必须先都存在**，否则立刻停下并说清楚缺哪个。
#
# 默认值里的 `:probe` 是本机开发的便利，但它同时是个陷阱：调用方少传一个参数时，
# 脚本会默默回落到一个"本机碰巧有、CI 上从来没有"的 tag。PR #78 的 CI 首跑正是
# 这么失败的——ci.yml 只传了三个参数，gateway 落到 `:probe`，在第 1 步中途炸出一句
# `No such object: lingxi-gateway:probe`；而本机因为留着验收用的 :probe 镜像，
# 同一份脚本全绿。**本机的残留镜像掩盖了参数缺失。**
#
# 前置存在性检查把这类失败从"跑到一半炸一句看不懂的话"变成"开跑就说清楚缺什么"。
missing=0
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}" \
            "migrate:${migrate_image}" "gateway:${gateway_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if ! docker image inspect "${image}" >/dev/null 2>&1; then
    printf '  缺少 %s 镜像：%s（本机不存在）\n' "${name}" "${image}" >&2
    missing=$((missing + 1))
  fi
done
if [[ "${missing}" -gt 0 ]]; then
  printf '\n用法：%s <scheduler 引用> <worker 引用> <migrate 引用> <gateway 引用>\n' "$0" >&2
  printf '四个参数都要显式传；省略时的 :probe 默认值只适用于本机构建过的情况。\n' >&2
  exit 2
fi

failures=0
note() { printf '  %s\n' "$1"; }
fail() { printf '  判否：%s\n' "$1" >&2; failures=$((failures + 1)); }
step() { printf '\n=== %s ===\n' "$1"; }

# rootfs 全量文件清单，**写进文件**再给 grep 用。用 `docker export`（展平后的容器
# 文件系统）而不是逐 layer：判定"最终镜像里有没有这个文件"就该看最终状态。
#
# **为什么必须落文件，而不是 `printf "$listing" | grep -q`**（Issue #62 内审 P1-1）：
# `grep -q` 命中后立即退出，上游 printf 随即拿到 SIGPIPE 而非零退出，`set -o pipefail`
# 于是把**整条管道**判为失败，`if` 把它读成"没命中"。净效果是：**清单里真的有违规项
# 时检查反而不报**，而干净时一切正常——一个只在出事时失灵的检查。
#
# 这不是推测。上一版就是管道写法，实测：往镜像里塞进 /scripts、/tests 和
# /opt/lingxi/migrations/testing 三个违规目录后，`verify_image_contract.sh` 依然
# 全绿退出 0；同一份清单用 `grep -c` 数得到 2 处命中。落文件之后立刻变红。
rootfs_listing_to() {
  local image=$1 destination=$2 container
  container=$(docker create "${image}")
  docker export "${container}" > "${destination}.tar"
  docker rm -f "${container}" >/dev/null
  tar -tf "${destination}.tar" > "${destination}"
  rm -f "${destination}.tar"
}

workspace=$(mktemp -d -t lingxi62-contract-XXXXXX)
trap 'rm -rf "${workspace}"' EXIT

step "1/10 非 root 运行（V-部署-07 / M2-62-29）"
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  configured_user=$(docker inspect --format '{{.Config.User}}' "${image}")
  actual_uid=$(docker run --rm --entrypoint id "${image}" -u)
  if [[ -z "${configured_user}" ]]; then
    fail "${name} 的 .Config.User 为空——镜像会以 root 跑"
  elif [[ "${configured_user%%:*}" == "0" || "${configured_user%%:*}" == "root" ]]; then
    fail "${name} 的 .Config.User 是 ${configured_user}"
  fi
  if [[ "${actual_uid}" == "0" ]]; then
    fail "${name} 容器内 id -u = 0"
  else
    note "${name}: .Config.User=${configured_user} 容器内 uid=${actual_uid}"
  fi
done

step "2/10 解释器版本满足 pyproject 声明（V-部署-09 镜像面 / M2-62-09）"
declared=$(sed -n 's/^requires-python = ">=\([0-9]\+\.[0-9]\+\)"$/\1/p' pyproject.toml)
[[ -n "${declared}" ]] || fail "读不到 pyproject.toml 的 requires-python"
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if docker run --rm --entrypoint python "${image}" -c \
      "import sys;d='${declared}'.split('.');sys.exit(0 if sys.version_info[:2]>=(int(d[0]),int(d[1])) else 1)"; then
    version=$(docker run --rm --entrypoint python "${image}" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')
    note "${name}: python ${version} ≥ ${declared}"
  else
    fail "${name} 的 python 低于 pyproject 声明的 ${declared}"
  fi
done

step "3/10 生产进程依赖隔离（M2-62-10）"
# scheduler 镜像同时承载正式 reauthorize job；OAuthBridgeClient 的 WebSocket 传输
# 因此是该镜像的正式依赖。lark-oapi 仍属于 gateway/Bot-Test，不得随 scheduler 进入。
if docker run --rm --entrypoint python "${scheduler_image}" -c 'import lark_oapi' >/dev/null 2>&1; then
  fail "scheduler 镜像里能 import lark_oapi——gateway/Bot-Test 依赖混进了正式镜像"
fi
if docker run --rm --entrypoint python "${scheduler_image}" -c 'import websockets' >/dev/null 2>&1; then
  note "scheduler: lark_oapi 不可导入，websockets 仅用于正式 reauthorize OAuth Bridge"
else
  fail "scheduler 镜像缺少正式 reauthorize 所需的 websockets"
fi
for module in lark_oapi websockets; do
  if docker run --rm --entrypoint python "${worker_image}" -c "import ${module}" >/dev/null 2>&1; then
    fail "worker 镜像里能 import ${module}——不属于该进程的依赖混进了生产镜像"
  fi
done
note "worker: lark_oapi 与 websockets 均不可导入（符合预期）"
# gateway **合法地**装 lark-oapi 与 websockets：#57 把长连接与发消息的依赖从
# bot-test 组提升成了 gateway 组，两组各自独立声明。所以这里不能照搬上面的否定断言，
# 要问的是另一个问题——bot-test 独有的那些**模块**有没有混进来。
for module in cryptography claude_agent_sdk; do
  if docker run --rm --entrypoint python "${gateway_image}" -c "import ${module}" >/dev/null 2>&1; then
    fail "gateway 镜像里能 import ${module}——它不在 gateway 组的声明里"
  fi
done
note "gateway: 合法持有 lark_oapi / websockets（#57 提升为 gateway 组），未混入 cryptography / claude_agent_sdk"
# 刻意**不**断言"Bot-Test 的模块不可 import"：`pip install .` 装的是整个 lingxi 包，
# adapters/feishu_onboarding.py 这类测试资产模块因此存在于每个镜像里，这是打包方式
# 决定的，不是编排缺陷。M2-62-11 管的是 scripts/ tests/ experiments/ workers/
# migrations/testing/ 这些**目录**别被 COPY 进来（见第 4 步），M2-62-10 管的是
# bot-test 那组**依赖**别被装上（见上）。第一版在这里写了一条"模块不可 import"的断言，
# 四个镜像全红——它测的是一件从来不成立、也不该成立的事。
# 迁移工具链不进业务进程镜像（V-迁移-04 的镜像面）。
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if docker run --rm --entrypoint python "${image}" -c 'import sqlalchemy' >/dev/null 2>&1; then
    fail "${name} 镜像装了 SQLAlchemy——迁移工具链不得进业务进程镜像"
  fi
done
note "scheduler / gateway / worker 均未装 SQLAlchemy"

step "4/10 非生产资产不进镜像（M2-62-11）与构建上下文不泄漏（M2-62-06 / M2-62-08）"
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  listing="${workspace}/${name}.listing"
  rootfs_listing_to "${image}" "${listing}"
  note "${name}: rootfs 清单 $(wc -l < "${listing}") 条"

  # 路径**锚定**在 rootfs 根与 /opt/lingxi 下，不做全局子串匹配：第三方包里带
  # `tests/` 目录是常见的（`docker export | tar -t` 输出不含前导斜杠），
  # 全局匹配会在某次依赖升级后凭空变红，然后有人把这条检查删掉。
  # 这里要问的是"**我们仓库的**非生产目录有没有被 COPY 进去"，只有这两处可能。
  for forbidden in 'scripts/' 'tests/' 'experiments/' 'workers/' 'docs/' '.git/'; do
    if grep -qE "^(${forbidden}|opt/lingxi/${forbidden})" "${listing}"; then
      fail "${name} 的 rootfs 含 ${forbidden}"
    fi
  done
  # 测试资产迁移不属于生产链，绝不能随 migrate 镜像发出去。
  if grep -qE '^opt/lingxi/migrations/testing/' "${listing}"; then
    fail "${name} 的 rootfs 含 migrations/testing/（测试资产不属于生产链）"
  fi
  # 构建目录路径不得出现在 rootfs 里。
  if grep -qE '^build/' "${listing}"; then
    fail "${name} 的 rootfs 含构建上下文目录 /build"
  fi
  # 我们 COPY 进去的代码下面不许有任何 __pycache__ 或字节码：那只可能来自宿主机
  # （镜像内的 compileall 只编 site-packages）。.dockerignore 的 `**/` 前缀就是
  # 为这一条服务的，裸模式匹配不到嵌套目录（内审 P1-2）。
  if grep -qE '^opt/lingxi/.*(__pycache__|\.py[cod]$)' "${listing}"; then
    fail "${name} 的 /opt/lingxi 下有 __pycache__ 或字节码——宿主机产物混进了制品"
  fi

  # 宿主机路径泄漏：只扫**我们自己 COPY 进去的** /opt/lingxi，不扫 site-packages。
  # 上一版拿 `/home/` 去扫整个 site-packages，会被 SQLAlchemy 的 docstring 与
  # SDK 自带 .pyc 里的示例路径误伤（内审点名的反向地雷）。这里改用构建上下文的
  # 特征串——它只可能来自"宿主机编译产物被 COPY 进来"这一种情况。
  if docker run --rm --entrypoint sh "${image}" -c \
      'test -d /opt/lingxi && grep -rlE "/(home|Users)/[a-z]+/|/build/src/lingxi" /opt/lingxi 2>/dev/null | head -1' \
      > "${workspace}/${name}.leak" 2>/dev/null; then
    if [[ -s "${workspace}/${name}.leak" ]]; then
      fail "${name} 的 /opt/lingxi 下有文件内嵌宿主机路径：$(head -1 "${workspace}/${name}.leak")"
    fi
  fi
  note "${name}: 无非生产资产、无构建上下文、无宿主机路径泄漏"
done

step "5/10 镜像里不预置任何凭据（M2-62-16）"
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  env_json=$(docker inspect --format '{{json .Config.Env}}' "${image}")
  for variable in LINGXI_DELEGATED_CREDENTIAL_KEY LINGXI_OAUTH_REFRESH_TOKEN_KEY \
                  LINGXI_FEISHU_APP_SECRET LINGXI_FEISHU_APP_ID LINGXI_POSTGRES_DSN \
                  LINGXI_MIGRATION_DSN; do
    if printf '%s' "${env_json}" | grep -q "${variable}="; then
      fail "${name} 的 .Config.Env 里给 ${variable} 赋了值"
    fi
  done
  note "${name}: .Config.Env 无任何凭据变量赋值"
done

step "6/10 已安装制品与进程运行依赖（V-部署-10 / M2-62-12）"
# 关键：跑的是**未经修改的** check_installed_package.py，工作目录在源码树之外，
# 仓库以只读挂载。改脚本或放宽 _INSTALL_MARKERS 来"跑通"就等于把这条断言作废。
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if docker run --rm -v "${repository_root}":/repo:ro -w /tmp --entrypoint python "${image}" \
      /repo/scripts/ci/check_installed_package.py --process "${name}" > /tmp/lingxi-installed-$$.log 2>&1; then
    note "${name}: $(tail -1 /tmp/lingxi-installed-$$.log)"
  else
    fail "${name} 的已安装制品检查未通过：$(cat /tmp/lingxi-installed-$$.log)"
  fi
  rm -f /tmp/lingxi-installed-$$.log
done

step "7/10 迁移随镜像进制品且与提交逐字节一致（M2-62-21）"
if docker run --rm --entrypoint sh "${migrate_image}" -c 'test -f /opt/lingxi/alembic.ini'; then
  image_sum=$(docker run --rm --entrypoint sha256sum "${migrate_image}" /opt/lingxi/alembic.ini | awk '{print $1}')
  local_sum=$(sha256sum alembic.ini | awk '{print $1}')
  if [[ "${image_sum}" == "${local_sum}" ]]; then
    note "alembic.ini 与提交逐字节一致（${local_sum:0:16}…）"
  else
    fail "alembic.ini 在镜像里与仓库不一致"
  fi
else
  fail "migrate 镜像里没有 /opt/lingxi/alembic.ini"
fi
for revision in migrations/alembic/versions/*.py; do
  base=$(basename "${revision}")
  image_sum=$(docker run --rm --entrypoint sha256sum "${migrate_image}" \
    "/opt/lingxi/migrations/alembic/versions/${base}" 2>/dev/null | awk '{print $1}')
  local_sum=$(sha256sum "${revision}" | awk '{print $1}')
  if [[ "${image_sum}" == "${local_sum}" ]]; then
    note "revision ${base} 逐字节一致"
  else
    fail "revision ${base} 未随镜像进制品或与提交不一致"
  fi
done

step "8/10 迁移入口不依赖工作目录（M2-62-22）与失败语义（M2-62-23）"
# 不设 DSN：必须非零退出、报错明确、且不连任何默认库。
if docker run --rm -w / "${migrate_image}" current >/tmp/lingxi-nodsn-$$.log 2>&1; then
  fail "未设 LINGXI_MIGRATION_DSN 时迁移居然成功了"
else
  if grep -q 'LINGXI_MIGRATION_DSN' /tmp/lingxi-nodsn-$$.log; then
    note "无 DSN：非零退出且报错点名了 LINGXI_MIGRATION_DSN"
  else
    fail "无 DSN 时的报错没有点明缺的是哪个变量"
  fi
fi
rm -f /tmp/lingxi-nodsn-$$.log

step "9/10 worker 内嵌 Claude CLI 真的能在本镜像里跑起来（V-部署-06 平台面）"
# Dockerfile 声称"底座必须是 glibc、构建平台必须与运行平台一致"。声称不是断言：
# claude-agent-sdk 的 wheel 是平台专属的（manylinux_2_17_x86_64），内嵌的 CLI 是
# **动态链接的 ELF**（解释器 /lib64/ld-linux-x86-64.so.2）。换成 alpine/musl 底座会
# 装得上、跑不起来；跨架构构建会拿到另一个（或没有）内嵌 CLI。两种情况都只在
# worker 真的执行一个回合时才暴露——那已经是一次用户请求失败了。
# 本步不调模型、不用凭据、不联网，只让 CLI 自报版本（内审 P2-1，已实测离线可行）。
bundled_cli=/usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude
if docker run --rm --entrypoint sh "${worker_image}" -c "test -x ${bundled_cli}"; then
  if cli_version=$(docker run --rm --entrypoint "${bundled_cli}" "${worker_image}" --version 2>&1); then
    note "worker: 内嵌 Claude CLI 可执行，自报 ${cli_version}"
  else
    fail "worker 的内嵌 Claude CLI 跑不起来（底座不是 glibc？架构不匹配？）：${cli_version}"
  fi
else
  fail "worker 镜像里没有可执行的内嵌 Claude CLI（${bundled_cli}）——SDK 会回落到 npm 安装的 claude，而镜像里没有 Node"
fi

step "10/10 rootfs 内容级凭据扫描（M2-62-16 的另一半）"
# 第 5 步只看 .Config.Env。凭据同样可能以**文件**形式混进镜像：一份误 COPY 的 .env、
# 一个私钥、一段带口令的连接串。借第 4 步已经落盘的清单再做一次内容级扫描
# （内审 P2-5）。用 docker run 在镜像内 grep，不把内容捞到宿主机上。
for pair in "scheduler:${scheduler_image}" "gateway:${gateway_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  listing="${workspace}/${name}.listing"
  # 形态一：.env 类文件根本不该存在于镜像里。
  if grep -qE '(^|/)\.env($|\.)' "${listing}"; then
    fail "${name} 的 rootfs 含 .env 类文件"
  fi
  # 形态二 / 三：私钥与带口令的连接串。只扫我们自己的 /opt/lingxi 与 /etc、/root，
  # 不扫 site-packages——第三方包的测试夹具里有示例私钥是常见的，扫它只会制造噪声。
  if docker run --rm --entrypoint sh "${image}" -c \
      'grep -rlE "BEGIN ([A-Z0-9]+ )?PRIVATE KEY|postgres(ql)?://[^:/@[:space:]]+:[^@[:space:]]+@" \
         /etc /root /opt 2>/dev/null | head -1' > "${workspace}/${name}.secret" 2>/dev/null; then
    if [[ -s "${workspace}/${name}.secret" ]]; then
      fail "${name} 的 rootfs 疑似含私钥或带口令的连接串：$(head -1 "${workspace}/${name}.secret")"
    fi
  fi
  # 形态四：凭据变量名被**赋了真值**写进某个文件（而不是 .Config.Env）。
  #
  # 排除 *.md 并排除占位值（`<...>`、`$...`、空）：migrations/README.md 里就有
  # `export LINGXI_MIGRATION_DSN='postgresql://<用户>@<主机>/<库>'` 这样的操作说明，
  # 那是文档不是凭据。第一次跑这条扫描就被它误伤了——一个会对文档报警的安全检查，
  # 最后一定会被人关掉。
  if docker run --rm --entrypoint sh "${image}" -c \
      'grep -rlE "LINGXI_(DELEGATED_CREDENTIAL_KEY|FEISHU_APP_SECRET|POSTGRES_DSN|MIGRATION_DSN|OAUTH_REFRESH_TOKEN_KEY)=[^<$\"'"'"' ]" \
         --exclude="*.md" --exclude="*.rst" /etc /root /opt 2>/dev/null | head -1' > "${workspace}/${name}.varfile" 2>/dev/null; then
    if [[ -s "${workspace}/${name}.varfile" ]]; then
      fail "${name} 的 rootfs 里有文件给凭据变量赋了值：$(head -1 "${workspace}/${name}.varfile")"
    fi
  fi
  note "${name}: rootfs 内容级凭据扫描通过"
done

printf '\n'
if [[ "${failures}" -gt 0 ]]; then
  printf '镜像部署契约：%s 项未通过\n' "${failures}" >&2
  exit 1
fi
printf '镜像部署契约：全部通过\n'
