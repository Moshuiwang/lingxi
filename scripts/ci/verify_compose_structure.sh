#!/usr/bin/env bash
# stage 与生产的 compose 必须**结构相同、只有配置不同**（断言 M2-62-13）。
#
#   scripts/ci/verify_compose_structure.sh
#
# 与 scripts/ci/check_deploy_contract.py 的分工：那边做**文本级**检查（不需要 docker，
# 挂在 verify_repository.sh 上，本机随时能跑）；这边让 `docker compose config` 真的把
# 两套编排**渲染**出来再比对结构。两者不可互相替代——文本检查看不出覆盖文件叠加之后
# 的最终形态，而这正是部署时真正生效的东西。
#
# 允许不同的：镜像 tag 值、env_file 指向、具名卷的实际名字、扫描间隔。
# 摘要里刻意没有 env_file：`compose config` 会把 env_file 的内容展开进 environment，
# 渲染结果里根本没有这个键，比它只会得到恒为 0 的假断言。凭据必须按服务分文件
# 这一条由 check_deploy_contract.py 在**源文件**层面守（codex 审查 P1-1）。
# 不允许不同的：service 名集合、镜像仓库路径、容器内挂载点集合、非 root 与只读设置。
#     一旦这几样在两个环境之间分叉，"stage 验过了所以生产也没问题"这句话就不成立了。

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

workspace=$(mktemp -d -t lingxi62-compose-XXXXXX)

# compose 的 env_file 默认是必需的：文件不存在时 `config` 直接报错。这里造两个**空的**
# 占位文件让渲染能跑完。它们匹配 .gitignore 既有的 `.env.*` 规则，因此不会污染工作树，
# 也不会被 gate 的「CI 未改写受版本控制的文件」那一步看到。
# 凭据按服务分文件（codex 审查 P1-1），因此占位文件也要按服务建齐，
# 否则 `compose config` 会在 env_file 不存在时直接报错。
placeholders=(
  deploy/.env.stage deploy/.env.stage.scheduler deploy/.env.stage.gateway
  deploy/.env.stage.worker deploy/.env.stage.worker-queue
  deploy/.env.stage.migrate deploy/.env.stage.reauthorize
  deploy/.env.prod  deploy/.env.prod.scheduler  deploy/.env.prod.gateway
  deploy/.env.prod.worker  deploy/.env.prod.worker-queue
  deploy/.env.prod.migrate deploy/.env.prod.reauthorize
)
created=()
cleanup() {
  rm -rf "${workspace}"
  if ((${#created[@]})); then
    rm -f "${created[@]}"
  fi
}
trap cleanup EXIT
for path in "${placeholders[@]}"; do
  if [[ ! -e "${path}" ]]; then
    : > "${path}"
    created+=("${path}")
  fi
done

export LINGXI_IMAGE_REGISTRY=ghcr.io/moshuiwang
export LINGXI_IMAGE_TAG=20260806-000000000000

# 摘要脚本单独用带引号的 heredoc 装进变量：直接写 `python3 -c '...'` 时，
# 内层的引号会与外层冲突（第一版就栽在这上面）。
summary_program=$(
  cat <<'PYTHON'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)

lines = []
for name in sorted(document.get("services", {})):
    service = document["services"][name]
    # 只保留仓库路径，丢掉 tag：tag 本就该在两个环境之间不同。
    repository = service.get("image", "").rsplit(":", 1)[0]
    mounts = sorted(volume.get("target", "") for volume in service.get("volumes", []))
    lines.append("service=" + name)
    lines.append("  repository=" + repository)
    lines.append("  user=" + str(service.get("user")))
    lines.append("  read_only=" + str(service.get("read_only")))
    lines.append("  restart=" + str(service.get("restart")))
    lines.append("  stop_grace_period=" + str(service.get("stop_grace_period")))
    lines.append("  mounts=" + str(mounts))
    lines.append("  cap_add=" + str(service.get("cap_add")))
    lines.append("  privileged=" + str(service.get("privileged")))
    lines.append("  security_opt=" + str(sorted(service.get("security_opt") or [])))
    lines.append("  tmpfs=" + str(sorted(service.get("tmpfs") or [])))
    lines.append("  profiles=" + str(sorted(service.get("profiles") or [])))
print("\n".join(lines))
PYTHON
)

build_key_program=$(
  cat <<'PYTHON'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)

offenders = sorted(
    name for name, service in document.get("services", {}).items() if "build" in service
)
if offenders:
    print("生产编排里这些 service 带了构建定义：" + ", ".join(offenders))
    raise SystemExit(1)
raise SystemExit(0)
PYTHON
)

for environment in stage prod; do
  # --profile mvp（Issue #153）：把 worker-queue 也纳入结构对照，否则它只在
  # mvp profile 下才可见，stage/prod 之间的等价性就漏了这个常驻服务一半的检查。
  docker compose -f deploy/compose.yaml -f "deploy/compose.${environment}.yaml" \
    --profile job --profile gateway --profile mvp config --format json > "${workspace}/${environment}.json"
  python3 -c "${summary_program}" "${workspace}/${environment}.json" \
    > "${workspace}/${environment}.summary"
done

printf '=== stage 与生产的结构摘要 ===\n'
cat "${workspace}/stage.summary"

if diff -u "${workspace}/stage.summary" "${workspace}/prod.summary"; then
  printf '\nCompose 结构对照：stage 与生产结构一致（差异仅限 tag 值、env_file 与卷名）\n'
else
  printf '\nCompose 结构对照：stage 与生产**结构**不一致——上面的差异不是配置差异\n' >&2
  exit 1
fi

# 生产侧必须零 `build:` 键。渲染之后再确认一次：覆盖文件有可能把它加回来。
if python3 -c "${build_key_program}" "${workspace}/prod.json"; then
  printf '生产编排零 `build:` 键：生产只拉镜像、不构建\n'
else
  printf '生产编排含构建定义，违反「生产不构建」\n' >&2
  exit 1
fi
