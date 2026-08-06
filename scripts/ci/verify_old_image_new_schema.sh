#!/usr/bin/env bash
# 断言 V-部署-05 的本地演法：**旧版本镜像能在新库结构上正常启动并完成一轮工作。**
#
#   scripts/ci/verify_old_image_new_schema.sh [scheduler 镜像引用] [migrate 镜像引用]
#
# 这条断言是回滚策略的地基，也是"最容易漏、代价最高"的一条（验证与门禁第十二节）：
# 「切 tag 回滚」只有在旧镜像能在新库上工作时才成立。一旦某次破坏性迁移合并了，
# 回滚就从"切 tag 重启"变成"恢复数据库备份"——而那是一个完全不同量级的事故。
#
# ---------------------------------------------------------------------------
# 引导悖论与它的解法（2026-08-06 编排者裁定①）
#
# 本仓库此刻只有一个 revision（基线），因此不存在"上一版镜像"可用来对比。裁定采纳的
# 判据是：**「旧镜像」= 迁移前那个提交构建出来的镜像**。本脚本里，合成迁移是运行时
# 生成的、从不进入版本库，所以**当前构建出来的镜像按定义就是"迁移前的镜像"**——
# 它的代码完全不知道那条迁移的存在。这不是取巧，这正是回滚时的真实处境：
# 库已经前滚了，镜像还是旧的。
#
# 合成迁移刻意做成**可前滚的第一段**（新增可空列 + 新增表），也就是"先加后删"里的
# "先加"。这一段之后旧镜像必须照常工作；只有在它工作正常的前提下，第二段（删除）
# 才允许在下一次发布里进行。
#
# 变异对照（证明本脚本会变红）：把合成迁移换成 `DROP COLUMN`——旧镜像读那一列的 SQL
# 立刻失败，脚本判红。用 --destructive 跑一次即可看到。
# ---------------------------------------------------------------------------

set -euo pipefail

# 本脚本全程用镜像与容器工作，不读仓库文件，因此不需要 repository_root。
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
: "${script_dir}"

scheduler_image=${1:-}
migrate_image=${2:-}
destructive=false
for argument in "$@"; do
  [[ "${argument}" == "--destructive" ]] && destructive=true
done
[[ "${scheduler_image}" == "--destructive" ]] && scheduler_image=""
[[ "${migrate_image}" == "--destructive" ]] && migrate_image=""
scheduler_image=${scheduler_image:-lingxi-scheduler:probe}
migrate_image=${migrate_image:-lingxi-migrate:probe}

# 端口避开本仓库其它受控测试占用的 15433 / 15436 / 15437 / 15438。
port=${LINGXI_DEMO_PGPORT:-15440}
container=lingxi62-vd05-pg
volume=lingxi62-vd05-credentials
workspace=$(mktemp -d -t lingxi62-vd05-XXXXXX)

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
  docker rm -f lingxi62-vd05-scheduler >/dev/null 2>&1 || true
  docker volume rm -f "${volume}" >/dev/null 2>&1 || true
  rm -rf "${workspace}"
}
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$1"; }

step "1/6 起一个一次性 PostgreSQL（端口 ${port}，结束即删）"
docker run -d --name "${container}" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=lingxi_vd05 \
  -p "${port}:5432" postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do
  docker exec "${container}" pg_isready -U postgres -d lingxi_vd05 >/dev/null 2>&1 && break
  sleep 1
done
docker exec "${container}" pg_isready -U postgres -d lingxi_vd05

dsn="postgresql+psycopg://postgres@127.0.0.1:${port}/lingxi_vd05"
libpq_dsn="postgresql://postgres@127.0.0.1:${port}/lingxi_vd05?connect_timeout=5"

step "2/6 用 migrate 镜像把库升到 head（真实迁移作业，不是手写 SQL）"
docker run --rm --network host -e LINGXI_MIGRATION_DSN="${dsn}" "${migrate_image}" upgrade head
docker exec "${container}" psql -U postgres -d lingxi_vd05 -tAc \
  "select 'alembic_version=' || version_num from alembic_version;"

step "3/6 播种：数据库登记行 + 与之匹配的加密凭据文件"
# **必须在合成迁移之前播种**，而不是之后。这不是顺序洁癖：播种代表"迁移发生前库里
# 就已有的业务数据"，正是回滚场景的真实前提。放在迁移之后播种会让破坏性迁移的变异
# 在播种那一步就失败，于是"旧镜像跑不跑得动"根本没被测到——检查看起来红了，
# 却红在错误的地方（本脚本第一版就踩了这个坑）。
#
# 让旧镜像真的**读到库**：claim_due 会先解密凭据文件，再查 feishu_delegated_subject
# 核对主体。refresh_at 刻意设在未来 → 这一轮判定"还不到期"就收工，
# 因此**不会发起任何飞书调用**，但那条 SELECT 已经实实在在跑过了。
subject=ou_vd05_demo_subject
docker exec "${container}" psql -U postgres -d lingxi_vd05 -v ON_ERROR_STOP=1 -c \
  "INSERT INTO feishu_delegated_subject (purpose, subject_open_id) VALUES ('org_directory_sync', '${subject}')
   ON CONFLICT (purpose) DO UPDATE SET subject_open_id = EXCLUDED.subject_open_id;" >/dev/null

docker volume create "${volume}" >/dev/null
credential_key=$(docker run --rm "${scheduler_image}" \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

docker run --rm -v "${volume}":/var/lib/lingxi/credentials \
  -e LINGXI_DEMO_KEY="${credential_key}" -e LINGXI_DEMO_SUBJECT="${subject}" \
  "${scheduler_image}" python -c '
import json, os, datetime
from cryptography.fernet import Fernet

now = datetime.datetime.now(datetime.timezone.utc)
payload = {
    "generation": "01J00000000000000000000V05",
    "subject_open_id": os.environ["LINGXI_DEMO_SUBJECT"],
    "refresh_token": "demo-not-a-real-token",
    "scope": "contact:user.base:readonly",
    "issued_at": now.isoformat(),
    # 未来到期：本轮判定"还不到期"，因此不会调用飞书，但库已经查过了。
    "refresh_at": (now + datetime.timedelta(days=3)).isoformat(),
    "expires_at": (now + datetime.timedelta(days=7)).isoformat(),
    "consumed_at": None,
}
blob = Fernet(os.environ["LINGXI_DEMO_KEY"].encode()).encrypt(json.dumps(payload).encode())
path = "/var/lib/lingxi/credentials/delegated_credential.enc"
with open(path, "wb") as handle:
    handle.write(blob)
os.chmod(path, 0o600)
print("  已播种加密凭据文件（假令牌，非真实凭据）")
'

step "4/6 追加一条**合成的前滚迁移**（迁移前的镜像对它一无所知）"
# 生成一条真正的 alembic revision，挂在基线之后，用 bind mount 送进 migrate 镜像的
# versions 目录。它从不进入版本库——所以不会影响 check_alembic_revisions.py 的 head 核对。
if [[ "${destructive}" == "true" ]]; then
  printf '  【变异模式】写一条破坏性迁移（DROP COLUMN），期望旧镜像失败\n'
  upgrade_body='op.execute("ALTER TABLE feishu_delegated_subject DROP COLUMN subject_open_id CASCADE")'
else
  printf '  【正常模式】写「先加后删」的第一段：新增可空列 + 新增表\n'
  upgrade_body='op.execute("ALTER TABLE feishu_delegated_subject ADD COLUMN rollout_note text")
    op.execute("CREATE TABLE vd05_added_table (id bigint PRIMARY KEY)")'
fi
cat > "${workspace}/20260807_synthetic.py" <<PYTHON
"""合成前滚迁移（Issue #62 的 V-部署-05 演示，运行时生成，不入库）。"""

from alembic import op

revision = "20260807_synthetic"
down_revision = "20260806_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ${upgrade_body}


def downgrade() -> None:
    raise RuntimeError("演示用合成迁移，不提供 downgrade")
PYTHON

docker run --rm --network host \
  -e LINGXI_MIGRATION_DSN="${dsn}" \
  -v "${workspace}/20260807_synthetic.py":/opt/lingxi/migrations/alembic/versions/20260807_synthetic.py:ro \
  "${migrate_image}" upgrade head
docker exec "${container}" psql -U postgres -d lingxi_vd05 -tAc \
  "select 'alembic_version=' || version_num from alembic_version;"

step "5/6 用**迁移前构建的 scheduler 镜像**在前滚后的库上启动"
docker run -d --name lingxi62-vd05-scheduler --network host \
  --read-only --tmpfs /tmp:mode=1777,size=16m \
  -v "${volume}":/var/lib/lingxi/credentials \
  -e LINGXI_POSTGRES_DSN="${libpq_dsn}" \
  -e LINGXI_DELEGATED_CREDENTIAL_KEY="${credential_key}" \
  -e LINGXI_DELEGATED_CREDENTIAL_PATH=/var/lib/lingxi/credentials/delegated_credential.enc \
  -e LINGXI_FEISHU_APP_ID=cli_demo_vd05 \
  -e LINGXI_FEISHU_APP_SECRET=demo-secret-not-real \
  -e LINGXI_SCHEDULER_INTERVAL_SECONDS=1 \
  "${scheduler_image}" >/dev/null

sleep 6
logs=$(docker logs lingxi62-vd05-scheduler 2>&1)
printf '%s\n' "${logs}" | sed 's/^/    /'

step "6/6 判定"
status=0
if ! printf '%s' "${logs}" | grep -q 'lingxi-scheduler 已启动'; then
  printf '  判否：旧镜像没有启动成功\n' >&2
  status=1
fi
if printf '%s' "${logs}" | grep -q '本轮续期扫描异常'; then
  printf '  判否：旧镜像在新库结构上跑不完一轮续期扫描——回滚不成立\n' >&2
  status=1
fi

# 断言 V-部署-04 / M2-62-31：日志只走 stdout / stderr，不落文件、不自行轮转。
# `docker diff` 列出容器相对镜像的文件系统改动；跑了几轮扫描之后仍不该有任何日志文件。
# 顺带覆盖 V-部署-02：除挂载的持久卷外不写需要持久化的本地状态（rootfs 是 read-only，
# 这里再从改动面确认一次）。
changes=$(docker diff lingxi62-vd05-scheduler || true)
if printf '%s\n' "${changes}" | grep -qE '\.log$|/var/log/'; then
  printf '  判否：容器内出现了日志文件——日志必须走 stdout/stderr\n' >&2
  printf '%s\n' "${changes}" | sed 's/^/      /' >&2
  status=1
else
  note_count=$(printf '%s\n' "${changes}" | grep -c . || true)
  printf '  docker diff：%s 项改动，无任何日志文件（日志走 stdout）\n' "${note_count}"
fi
if [[ -z "${logs}" ]]; then
  printf '  判否：docker logs 为空——进程没有把日志写到 stdout/stderr\n' >&2
  status=1
fi

# 真实 SIGTERM：宽限期内自行退出，退出码不是 137（断言 M2-62-26）。
docker stop --timeout 90 lingxi62-vd05-scheduler >/dev/null
exit_code=$(docker inspect -f '{{.State.ExitCode}}' lingxi62-vd05-scheduler)
final_logs=$(docker logs lingxi62-vd05-scheduler 2>&1)
if ! printf '%s' "${final_logs}" | grep -q '收到信号，停止领取新的到期凭据'; then
  printf '  判否：没有观察到 SIGTERM 的优雅停机日志\n' >&2
  status=1
fi
if [[ "${exit_code}" != "0" ]]; then
  printf '  判否：退出码 %s（137 = 被 SIGKILL，说明宽限期不够）\n' "${exit_code}" >&2
  status=1
fi

if [[ "${status}" == "0" ]]; then
  printf '  通过：旧镜像在前滚后的库上启动、完成续期扫描、收到 SIGTERM 后自行退出（退出码 %s）\n' "${exit_code}"
  if [[ "${destructive}" == "true" ]]; then
    printf '  !! 但本次是 --destructive 变异模式，本应判红——检查失效了 !!\n' >&2
    exit 1
  fi
else
  if [[ "${destructive}" == "true" ]]; then
    printf '  变异模式如期判红：破坏性迁移确实会被这条检查抓到。\n'
    exit 0
  fi
fi
exit "${status}"
