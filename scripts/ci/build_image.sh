#!/usr/bin/env bash
# 构建一个 Lingxi 生产镜像并按不可变 tag 命名（Issue #62 / S11）。
#
#   scripts/ci/build_image.sh scheduler
#   scripts/ci/build_image.sh worker --no-cache
#
# tag 语义（deploy/README.md 是正文，这里是实现）：
#
#     <仓库前缀>/lingxi-<服务>:<YYYYMMDD>-<commit sha 前 12 位>
#     例：ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
#
# 两段各有职责：日期标识**发布批次**，sha 标识**源码提交**。带上 sha 才是不可变的——
# 同一个 tag 永远指向同一份源码，于是"回滚 = 切回上一个 tag"这件事才有意义。
# 只打 `latest` 或分支名的镜像无法回滚：那个名字明天指向什么，没有人能保证。
#
# 刻意只用 plain `docker build`：本机验收环境没有 buildx（2026-08-06 编排者裁定⑤），
# 而"能不能构建"不该取决于构建机装了哪个插件。不使用 BuildKit 专有语法与缓存后端。

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

service=${1:-}
shift || true

case "${service}" in
  scheduler | worker | migrate) ;;
  *)
    printf '用法：%s <scheduler|worker|migrate> [docker build 的额外参数...]\n' "$0" >&2
    printf '（gateway 与 admin 进程尚未建立，不为它们构建镜像——未实现的入口不得用占位进程冒充）\n' >&2
    exit 2
    ;;
esac

# 仓库前缀默认为空，构建出本地名字（lingxi-scheduler:...）。CI 与部署显式传 GHCR 前缀。
registry=${LINGXI_IMAGE_REGISTRY:-}

# 发布批次：默认取 UTC 当天。CI 可以传入以保证矩阵各腿拿到同一个批次号。
batch=${LINGXI_IMAGE_BATCH:-$(date -u +%Y%m%d)}

# 源码提交：优先用 CI 提供的 sha，本地回落到 git。
commit=${LINGXI_IMAGE_COMMIT:-$(git rev-parse HEAD)}
short_commit=${commit:0:12}

if [[ ! "${batch}" =~ ^[0-9]{8}$ ]]; then
  printf '发布批次 `%s` 不是 8 位日期。\n' "${batch}" >&2
  exit 1
fi
if [[ ! "${short_commit}" =~ ^[0-9a-f]{12}$ ]]; then
  printf '提交 sha `%s` 取不出 12 位十六进制。\n' "${commit}" >&2
  exit 1
fi

tag="${batch}-${short_commit}"
if [[ -n "${registry}" ]]; then
  reference="${registry}/lingxi-${service}:${tag}"
else
  reference="lingxi-${service}:${tag}"
fi

printf '构建 %s（target=%s）\n' "${reference}" "${service}"
docker build --target "${service}" -t "${reference}" "$@" .

# 引用写到调用方指定的文件，**默认不写**。往仓库目录里落文件会让 gate 的
# 「CI 没有改写受版本控制的文件」那一步变红——`git status --porcelain` 把未跟踪文件
# 也算在内。构建产物不该污染工作树。
if [[ -n "${LINGXI_IMAGE_REFERENCE_FILE:-}" ]]; then
  printf '%s\n' "${reference}" > "${LINGXI_IMAGE_REFERENCE_FILE}"
fi
printf '已构建：%s\n' "${reference}"
