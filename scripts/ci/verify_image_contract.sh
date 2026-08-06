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

failures=0
note() { printf '  %s\n' "$1"; }
fail() { printf '  判否：%s\n' "$1" >&2; failures=$((failures + 1)); }
step() { printf '\n=== %s ===\n' "$1"; }

# rootfs 全量文件清单。用 `docker export`（展平后的容器文件系统）而不是逐 layer：
# 判定"最终镜像里有没有这个文件"就该看最终状态，被上层删掉的中间产物不该算数。
rootfs_listing() {
  local image=$1 container listing
  container=$(docker create "${image}")
  listing=$(docker export "${container}" | tar -t 2>/dev/null)
  docker rm -f "${container}" >/dev/null
  printf '%s\n' "${listing}"
}

step "1/8 非 root 运行（V-部署-07 / M2-62-29）"
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
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

step "2/8 解释器版本满足 pyproject 声明（V-部署-09 镜像面 / M2-62-09）"
declared=$(sed -n 's/^requires-python = ">=\([0-9]\+\.[0-9]\+\)"$/\1/p' pyproject.toml)
[[ -n "${declared}" ]] || fail "读不到 pyproject.toml 的 requires-python"
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if docker run --rm --entrypoint python "${image}" -c \
      "import sys;d='${declared}'.split('.');sys.exit(0 if sys.version_info[:2]>=(int(d[0]),int(d[1])) else 1)"; then
    version=$(docker run --rm --entrypoint python "${image}" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')
    note "${name}: python ${version} ≥ ${declared}"
  else
    fail "${name} 的 python 低于 pyproject 声明的 ${declared}"
  fi
done

step "3/8 bot-test 依赖不进生产镜像（M2-62-10）"
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  for module in lark_oapi websockets; do
    if docker run --rm --entrypoint python "${image}" -c "import ${module}" >/dev/null 2>&1; then
      fail "${name} 镜像里能 import ${module}——Bot-Test 受控验证资产混进了生产镜像"
    fi
  done
  note "${name}: lark_oapi 与 websockets 均不可导入（符合预期）"
done
# 迁移工具链不进业务进程镜像（V-迁移-04 的镜像面）。
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if docker run --rm --entrypoint python "${image}" -c 'import sqlalchemy' >/dev/null 2>&1; then
    fail "${name} 镜像装了 SQLAlchemy——迁移工具链不得进业务进程镜像"
  fi
done
note "scheduler 与 worker 均未装 SQLAlchemy"

step "4/8 非生产资产不进镜像（M2-62-11）与构建上下文不泄漏（M2-62-06 / M2-62-08）"
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  listing=$(rootfs_listing "${image}")
  # 路径**锚定**在 rootfs 根与 /opt/lingxi 下，不做全局子串匹配：第三方包里带
  # `tests/` 目录是常见的（`docker export | tar -t` 输出不含前导斜杠），
  # 全局匹配会在某次依赖升级后凭空变红，然后有人把这条检查删掉。
  # 这里要问的是"**我们仓库的**非生产目录有没有被 COPY 进去"，只有这两处可能。
  for forbidden in 'scripts/' 'tests/' 'experiments/' 'workers/' 'docs/' '.git/'; do
    if printf '%s\n' "${listing}" | grep -qE "^(${forbidden}|opt/lingxi/${forbidden})"; then
      fail "${name} 的 rootfs 含 ${forbidden}"
    fi
  done
  # 测试资产迁移不属于生产链，绝不能随 migrate 镜像发出去。
  if printf '%s\n' "${listing}" | grep -qE '^opt/lingxi/migrations/testing/'; then
    fail "${name} 的 rootfs 含 migrations/testing/（测试资产不属于生产链）"
  fi
  # 构建目录路径不得出现在 rootfs 的文件名里。
  if printf '%s\n' "${listing}" | grep -q '^build/'; then
    fail "${name} 的 rootfs 含构建上下文目录 /build"
  fi
  # 宿主机编译的 .pyc 会内嵌构建目录绝对路径；镜像内自己编的不会。
  if printf '%s\n' "${listing}" | grep -q '\.pyc$'; then
    if docker run --rm --entrypoint sh "${image}" -c \
        'grep -rl "/home/\|/tmp/lingxi\|second-copy" /usr/local/lib/python3.12/site-packages --include="*.pyc" 2>/dev/null | head -1' \
        | grep -q .; then
      fail "${name} 的 .pyc 里内嵌了宿主机路径"
    fi
  fi
  note "${name}: rootfs 无非生产资产、无构建上下文、无宿主机路径泄漏"
done

step "5/8 镜像里不预置任何凭据（M2-62-16）"
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
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

step "6/8 已安装制品与进程运行依赖（V-部署-10 / M2-62-12）"
# 关键：跑的是**未经修改的** check_installed_package.py，工作目录在源码树之外，
# 仓库以只读挂载。改脚本或放宽 _INSTALL_MARKERS 来"跑通"就等于把这条断言作废。
for pair in "scheduler:${scheduler_image}" "worker:${worker_image}" "migrate:${migrate_image}"; do
  name=${pair%%:*}; image=${pair#*:}
  if docker run --rm -v "${repository_root}":/repo:ro -w /tmp --entrypoint python "${image}" \
      /repo/scripts/ci/check_installed_package.py --process "${name}" > /tmp/lingxi-installed-$$.log 2>&1; then
    note "${name}: $(tail -1 /tmp/lingxi-installed-$$.log)"
  else
    fail "${name} 的已安装制品检查未通过：$(cat /tmp/lingxi-installed-$$.log)"
  fi
  rm -f /tmp/lingxi-installed-$$.log
done

step "7/8 迁移随镜像进制品且与提交逐字节一致（M2-62-21）"
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

step "8/8 迁移入口不依赖工作目录（M2-62-22）与失败语义（M2-62-23）"
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

printf '\n'
if [[ "${failures}" -gt 0 ]]; then
  printf '镜像部署契约：%s 项未通过\n' "${failures}" >&2
  exit 1
fi
printf '镜像部署契约：全部通过\n'
