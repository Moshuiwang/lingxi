#!/usr/bin/env bash
# 备份恢复真实演练：stage 一次性隔离实例（Trace #373 H2 批 S-H2-5）。
#
# 演练什么：从**运行库**只读 pg_dump 一次 → 恢复进一个全新建立、不发布任何宿主
# 端口的隔离 Postgres 实例 → 向隔离实例（不是运行库）注入原始写入时间已超过
# 90 天的合成行，同时保留恢复出来的真实未到期行作对照 → 用当前 scheduler 镜像
# 里真实的 `lingxi.adapters.retention` 清理代码路径（不是手写 SQL）对隔离实例
# 补跑一次清理 → 核对「按原始写入时间计算到期，恢复不重置保留起点」→ 销毁隔离
# 实例与全部临时产物。断言依据：docs/技术设计/验收矩阵-审计与保留.md `V-投递-07`。
#
# **破坏半径（先读这一段再执行）**：
#   - 对运行库（默认容器 `lingxi-test-db`）**唯一**的操作是一次只读 `pg_dump`；
#     本脚本不 stop/restart 任何服务，不向运行库写一个字节，不重启任何容器。
#   - 全部有副作用的操作只发生在本脚本新建的隔离容器/网络里（默认
#     `lingxi-drill-db` / `lingxi-drill-net`），随脚本收尾一并销毁；隔离数据库
#     密码现场随机生成，只作为该容器的环境变量存在，容器销毁即失效，不落盘、
#     不打印、不进日志。
#   - 注入的到期样本是固定化名合成数据（`drill-synthetic-*`/`演练*`），不使用
#     真实业务数据（验证与门禁 §十三）。
#   - **任何一步失败都会因 `set -euo pipefail` 立即停止，脚本不做自动清理**——
#     现场（隔离容器、网络、dump 文件）原样保留供取证，失败信息会指出如何手动
#     核对与之后如何清理（见下方“失败后如何处理”）。这是有意的：先取证、
#     再销毁，不为了让脚本看起来"跑完"而在失败时静默擦掉现场。
#
# **前提**：
#   - 在能对 `LINGXI_DRILL_SOURCE_DB_CONTAINER` 执行 `docker exec`、且能
#     `docker run` / `docker network create` 的宿主机上执行；当前唯一验证过的
#     环境是 `biai-stage`。
#   - 运行库容器与隔离数据库镜像已经在本机（脚本不会替调用方拉镜像；缺镜像
#     直接失败,不静默 `docker pull`）。
#   - 磁盘要有余量：dump 与隔离实例的数据量级与源库相当（biai-stage 当前源库
#     19MB 量级，дump 出来约几 MB），加上隔离数据库镜像的本地层；执行前后各
#     `df -h` 一次自行核对，不由脚本代为判断磁盘是否够用。
#
# **失败后如何处理**：先用 `docker logs "${LINGXI_DRILL_DB_CONTAINER:-lingxi-drill-db}"`、
# 查看恢复日志（脚本打印的 `RESTORE_LOG` 路径）和上面打印的最后一步取证；确认
# 证据留存完毕后手动执行：
#   docker rm -f "${LINGXI_DRILL_DB_CONTAINER:-lingxi-drill-db}"
#   docker network rm "${LINGXI_DRILL_NETWORK:-lingxi-drill-net}"
#   rm -f <脚本打印的 dump/日志临时文件路径>
#
# **可覆盖的环境变量**（默认值对应 biai-stage 当前 `lingxi-test-db` 部署）：
#   LINGXI_DRILL_SOURCE_DB_CONTAINER  运行库容器名，默认 lingxi-test-db
#   LINGXI_DRILL_SOURCE_DB_USER       运行库用户名，默认 lingxi
#   LINGXI_DRILL_SOURCE_DB_NAME       运行库库名，默认 lingxi
#   LINGXI_DRILL_NETWORK              隔离网络名，默认 lingxi-drill-net
#   LINGXI_DRILL_DB_CONTAINER         隔离数据库容器名，默认 lingxi-drill-db
#   LINGXI_DRILL_POSTGRES_IMAGE       隔离数据库镜像，默认 postgres:16-alpine
#   LINGXI_DRILL_SCHEDULER_IMAGE      承载真实清理代码路径的 scheduler 镜像
#                                     引用，**必须显式指定**（例如
#                                     ghcr.io/moshuiwang/lingxi-scheduler:<tag>）。
#                                     不设默认值：用哪个候选镜像跑清理，必须是
#                                     调用方的显式选择，不能悄悄落到 latest。
#   LINGXI_DRILL_DUMP_PATH            dump 文件落盘路径，默认 mktemp 生成——
#                                     这是明文 SQL dump，含全部业务数据；本脚本
#                                     当前只在 stage 验证过，成功路径下 step_destroy
#                                     会删除它，失败路径下按上面"失败后如何处理"
#                                     留作取证。**若未来把本脚本用于生产环境**，
#                                     dump 落盘必须加密（不能沿用 stage 这条明文
#                                     落盘路径），且用毕（核对完成或确认不再需要）
#                                     必须立即销毁，不依赖操作者记得手动清理
#                                     （独立审查 P2-15；本次改动只补这条登记，不
#                                     改变 stage 现行行为）。
#   LINGXI_DRILL_INJECT_SYNTHETIC     是否注入合成到期样本并核对清理语义，
#                                     默认 1；设为 0 时只做备份/恢复完整性检查
#                                     （生产 runbook 里最简的"确认能恢复"子集，
#                                     不核对保留语义）
#
# 用法：
#   LINGXI_DRILL_SCHEDULER_IMAGE=ghcr.io/moshuiwang/lingxi-scheduler:<tag> \
#     scripts/ops/backup_restore_drill.sh
set -euo pipefail

SOURCE_DB_CONTAINER="${LINGXI_DRILL_SOURCE_DB_CONTAINER:-lingxi-test-db}"
SOURCE_DB_USER="${LINGXI_DRILL_SOURCE_DB_USER:-lingxi}"
SOURCE_DB_NAME="${LINGXI_DRILL_SOURCE_DB_NAME:-lingxi}"
DRILL_NETWORK="${LINGXI_DRILL_NETWORK:-lingxi-drill-net}"
DRILL_DB_CONTAINER="${LINGXI_DRILL_DB_CONTAINER:-lingxi-drill-db}"
DRILL_POSTGRES_IMAGE="${LINGXI_DRILL_POSTGRES_IMAGE:-postgres:16-alpine}"
DRILL_SCHEDULER_IMAGE="${LINGXI_DRILL_SCHEDULER_IMAGE:?必须指定用于执行真实清理代码路径的 scheduler 镜像引用，例如 ghcr.io/moshuiwang/lingxi-scheduler:<tag>}"
INJECT_SYNTHETIC="${LINGXI_DRILL_INJECT_SYNTHETIC:-1}"
DUMP_PATH="${LINGXI_DRILL_DUMP_PATH:-}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "缺少命令：$1" >&2; exit 1; }
}

step_preflight() {
  log "== 预检 =="
  # 命名碰撞断言（独立审查 P2-12）：本脚本后续步骤会对 DRILL_DB_CONTAINER 做
  # 注入、跑清理代码路径、最终 `docker rm -f -v` 销毁——这些动作全部假定这个
  # 容器是脚本自己新建的一次性隔离实例，绝不能是运行库容器本身。同理
  # DRILL_DB_CONTAINER 与 DRILL_NETWORK 是脚本同时创建、同时存在的两个不同类型
  # 对象，同名会让不显式声明对象类型的排查命令（`docker inspect <名字>`）产生
  # 歧义。两条都是纯字符串比较，不依赖任何 docker 调用，放在最前面先查。
  if [[ "${DRILL_DB_CONTAINER}" == "${SOURCE_DB_CONTAINER}" ]]; then
    echo "配置错误：LINGXI_DRILL_DB_CONTAINER 与运行库容器名 ${SOURCE_DB_CONTAINER} 相同——后续注入/清理/销毁步骤会直接操作这个容器，一旦两者同名，动的就是运行库本身" >&2
    exit 1
  fi
  if [[ "${DRILL_DB_CONTAINER}" == "${DRILL_NETWORK}" ]]; then
    echo "配置错误：LINGXI_DRILL_DB_CONTAINER 与 LINGXI_DRILL_NETWORK 同名（${DRILL_DB_CONTAINER}）——容器与网络是两类不同 docker 对象，但同名会让不显式声明对象类型的 docker 命令产生歧义" >&2
    exit 1
  fi
  require_cmd docker
  require_cmd openssl
  df -h /
  docker ps
  if docker ps -a --format '{{.Names}}' | grep -qx "${DRILL_DB_CONTAINER}"; then
    echo "残留容器 ${DRILL_DB_CONTAINER}，请先 docker rm -f 再重跑" >&2
    exit 1
  fi
  if docker network ls --format '{{.Name}}' | grep -qx "${DRILL_NETWORK}"; then
    echo "残留网络 ${DRILL_NETWORK}，请先 docker network rm 再重跑" >&2
    exit 1
  fi
  if ! docker inspect -f '{{.State.Status}}' "${SOURCE_DB_CONTAINER}" >/dev/null 2>&1; then
    echo "运行库容器 ${SOURCE_DB_CONTAINER} 不存在或不可达" >&2
    exit 1
  fi
  if [[ -z "${DUMP_PATH}" ]]; then
    DUMP_PATH=$(mktemp /tmp/lingxi-drill-dump-XXXXXX.sql)
  fi
  RESTORE_LOG=$(mktemp /tmp/lingxi-drill-restore-XXXXXX.log)
  log "dump 文件：${DUMP_PATH}；恢复日志：${RESTORE_LOG}"
}

step_backup() {
  log "== 备份（对运行库唯一的接触：一次只读 pg_dump）=="
  docker exec "${SOURCE_DB_CONTAINER}" pg_dump -U "${SOURCE_DB_USER}" "${SOURCE_DB_NAME}" > "${DUMP_PATH}"
  log "备份写入 ${DUMP_PATH}（$(du -h "${DUMP_PATH}" | cut -f1)）"
}

step_isolate_and_restore() {
  log "== 建立隔离实例（不发布任何宿主端口）=="
  docker network create "${DRILL_NETWORK}" >/dev/null
  local password
  password=$(openssl rand -hex 24)
  docker run -d --name "${DRILL_DB_CONTAINER}" --network "${DRILL_NETWORK}" \
    -e "POSTGRES_USER=${SOURCE_DB_USER}" -e "POSTGRES_PASSWORD=${password}" \
    -e "POSTGRES_DB=${SOURCE_DB_NAME}" "${DRILL_POSTGRES_IMAGE}" >/dev/null
  DRILL_DSN="postgresql://${SOURCE_DB_USER}:${password}@${DRILL_DB_CONTAINER}:5432/${SOURCE_DB_NAME}"
  # 官方 postgres 镜像在 Dockerfile 里声明了 `VOLUME /var/lib/postgresql/data`：
  # 即使这里没有传 `-v`，`docker run` 仍会为它悄悄建一个匿名卷。`docker rm -f`
  # 不带 `-v` 不会删这个匿名卷——数据库容器没了，装数据的卷还留在宿主机上，
  # 是一种不会报错的残留（本 Story 演练时实测踩到，登记在 PR 里）。这里记下卷名，
  # 销毁步骤按名核对它确实被删干净，不只信 `docker rm -v` 的退出码。
  #
  # go-template 输出加一个换行分隔（独立审查 P2-14）：不加分隔符时，如果这个
  # 容器意外挂了不止一个匿名卷，多个卷名会被原样拼接成一整段无法拆分的字符串
  # ——静默把"这里其实有两个卷"读成"这里只有一个奇怪名字的卷"。加分隔符后逐行
  # 判空，发现多于一个非空行就是需要人工确认的异常，不沿用"当作只有一个卷"
  # 继续往下跑。
  drill_volume_raw=$(docker inspect -f '{{ range .Mounts }}{{ if eq .Type "volume" }}{{ .Name }}{{ "\n" }}{{ end }}{{ end }}' "${DRILL_DB_CONTAINER}")
  drill_volume_count=$(printf '%s\n' "${drill_volume_raw}" | grep -c . || true)
  if (( drill_volume_count > 1 )); then
    echo "隔离数据库容器 ${DRILL_DB_CONTAINER} 挂了 ${drill_volume_count} 个匿名卷，超出预期的至多一个，需要人工确认后再继续：" >&2
    printf '%s\n' "${drill_volume_raw}" >&2
    exit 1
  fi
  DRILL_VOLUME=$(printf '%s\n' "${drill_volume_raw}" | grep . || true)

  local tries=0
  until docker exec "${DRILL_DB_CONTAINER}" pg_isready -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if (( tries > 30 )); then
      echo "隔离实例 30 次探测仍未就绪：${DRILL_DB_CONTAINER}" >&2
      exit 1
    fi
    sleep 1
  done
  log "隔离实例就绪：${DRILL_DB_CONTAINER}"

  log "== 恢复：预建角色骨架 + 灌入 dump =="
  # dump 是单库内容 dump，不含 CREATE ROLE（角色是集群级对象）；ALTER ... OWNER TO
  # 与 GRANT 语句引用的四个 lingxi_* 角色必须预先存在，否则恢复会因角色不存在报错。
  # 全部建成 NOLOGIN：隔离实例里没有任何东西需要用它们登录，跟源库的角色定位一致。
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_app') THEN CREATE ROLE lingxi_app NOLOGIN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_scheduler') THEN CREATE ROLE lingxi_scheduler NOLOGIN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_retention_owner') THEN CREATE ROLE lingxi_retention_owner NOLOGIN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_migrate') THEN CREATE ROLE lingxi_migrate NOLOGIN; END IF;
    END
    \$\$;
  " >/dev/null

  docker exec -i "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -v ON_ERROR_STOP=1 \
    < "${DUMP_PATH}" > "${RESTORE_LOG}" 2>&1
  log "恢复完成（日志见 ${RESTORE_LOG}，$(wc -l < "${RESTORE_LOG}") 行输出，0 条错误——ON_ERROR_STOP 保证）"
}

step_inject_synthetic() {
  log "== 注入合成样本（只写隔离实例，不碰运行库）=="
  # 到期时间由不可后移触发器从 started_at 派生，这里传的 expires_at 会被覆盖，
  # 与调用方能不能自己指定到期时间无关（V-保留-07 同一条不变式）。
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at, completed_at)
    VALUES ('gib_drill_synthetic_expired', 'drill-synthetic-export', 'digest-drill-synthetic-expired',
            'complete', now() - interval '100 days', now() - interval '100 days');
    INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at, completed_at)
    VALUES ('gib_drill_synthetic_fresh', 'drill-synthetic-export-fresh', 'digest-drill-synthetic-fresh',
            'complete', now() - interval '5 days', now() - interval '5 days');
  " >/dev/null
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    BEGIN;
    INSERT INTO feishu_org_sync_run (id, source_app_id, status, started_at, completed_at, expires_at, tenant_count, department_count, member_count)
    VALUES ('orgsync_drill_expired', 'cli_fake', 'complete', now() - interval '100 days', now() - interval '100 days', now() - interval '100 days', 1, 1, 1);
    INSERT INTO feishu_org_tenant_snapshot (id, sync_run_id, tenant_key, visible_to_user_identity, member_count)
    VALUES ('orgsync_drill_expired_tenant', 'orgsync_drill_expired', 'tenant_drill', true, 1);
    INSERT INTO feishu_org_department_snapshot (id, sync_run_id, tenant_key, department_key, name)
    VALUES ('orgsync_drill_expired_dept', 'orgsync_drill_expired', 'tenant_drill', 'dept_drill', '演练部门');
    INSERT INTO feishu_org_member_snapshot (id, sync_run_id, tenant_key, member_key, open_id, user_id, union_id, display_name)
    VALUES ('orgsync_drill_expired_member', 'orgsync_drill_expired', 'tenant_drill', 'm1', 'ou_drill_placeholder', 'user_drill_placeholder', 'union_drill_placeholder', '演练化名');
    COMMIT;
  " >/dev/null
  log "合成样本已写入：galaxy_import_batch × 2（过期/未过期各一），feishu_org_sync_run × 1（过期）"

  log "-- 清理前快照 --"
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" \
    -c "SELECT id, started_at, expires_at, (expires_at <= now()) AS is_expired FROM galaxy_import_batch ORDER BY started_at;" \
    -c "SELECT id, started_at, expires_at, (expires_at <= now()) AS is_expired FROM feishu_org_sync_run ORDER BY started_at;"

  # 真实恢复出来的对照行数（不含本步注入的合成行），供清理后原样核对——不是硬编码
  # 的固定数字：源库会持续写入新的组织快照，写成常量会让脚本在下一次真实运行时
  # 假红。
  REAL_SYNC_RUN_COUNT_BEFORE=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -Atc \
    "SELECT count(*) FROM feishu_org_sync_run WHERE id NOT LIKE 'orgsync_drill_%'")
  log "真实恢复且未过期的 feishu_org_sync_run 对照行数：${REAL_SYNC_RUN_COUNT_BEFORE}"
}

step_run_real_cleanup() {
  log "== 用真实清理代码路径补跑一次保留清理（scheduler 镜像，非手写 SQL）=="
  # DRILL_DSN（含隔离实例的现场随机密码）改用 `-e` 环境变量传入容器，不拼进
  # `-c` 后面的 Python 源码文本（独立审查 P2-11）：后者会让密码原样出现在
  # `docker run` 这条命令自身的 argv 里——本机 `ps aux`/`/proc/<pid>/cmdline`
  # 能看到完整命令行的任何账户都会看到明文密码；改成环境变量后，读取面收紧到
  # 需要读该进程 `/proc/<pid>/environ`（同用户或 root）的账户，与本文件头部
  # "密码只作为该容器的环境变量存在，容器销毁即失效，不落盘、不打印"的既有
  # 边界一致——DRILL_DSN 本来就已经是环境变量语义，这里只是不再多绕一圈把它
  # 变成 argv。
  docker run --rm --network "${DRILL_NETWORK}" -e "LINGXI_DRILL_DSN=${DRILL_DSN}" \
    --entrypoint python "${DRILL_SCHEDULER_IMAGE}" -c "
import os
from lingxi.adapters.retention import PostgresRetentionCleaner
cleaner = PostgresRetentionCleaner(os.environ[\"LINGXI_DRILL_DSN\"])
report = cleaner.run_once()
print(report.summary())
for t in report.tables:
    print(t.table, t.deleted, t.oldest_expires_at, t.newest_expires_at, t.blocked)
"
}

step_verify() {
  log "== 核对：过期合成行已清，未过期行（含真实恢复行）逐行保留 =="
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" \
    -c "SELECT id, started_at, expires_at FROM galaxy_import_batch ORDER BY started_at;" \
    -c "SELECT id, started_at, expires_at FROM feishu_org_sync_run ORDER BY started_at;"

  # 孤儿行核对（独立审查 P2-13）：此前这两条只用 `-c` 打印计数，人不盯着看
  # 就会漏掉——计数非零并不会让脚本非零退出。改成取值断言：清理逻辑如果破坏了
  # 引用完整性（子表还在、父行已被删），这里必须让脚本失败退出,而不是只在
  # 输出里留一行容易被忽略的数字。
  local orphan_galaxy_children
  orphan_galaxy_children=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -Atc \
    "SELECT count(*) FROM galaxy_user WHERE batch_id NOT IN (SELECT id FROM galaxy_import_batch);")
  if [[ "${orphan_galaxy_children}" != "0" ]]; then
    echo "核对失败：galaxy_user 出现 ${orphan_galaxy_children} 行孤儿数据（batch_id 指向已不存在的 galaxy_import_batch），清理逻辑破坏了引用完整性" >&2
    exit 1
  fi
  local orphan_org_children
  orphan_org_children=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -Atc \
    "SELECT count(*) FROM feishu_org_member_snapshot WHERE sync_run_id NOT IN (SELECT id FROM feishu_org_sync_run);")
  if [[ "${orphan_org_children}" != "0" ]]; then
    echo "核对失败：feishu_org_member_snapshot 出现 ${orphan_org_children} 行孤儿数据（sync_run_id 指向已不存在的 feishu_org_sync_run），清理逻辑破坏了引用完整性" >&2
    exit 1
  fi

  local still_present
  still_present=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -Atc \
    "SELECT count(*) FROM galaxy_import_batch WHERE id = 'gib_drill_synthetic_expired'
       UNION ALL
     SELECT count(*) FROM feishu_org_sync_run WHERE id = 'orgsync_drill_expired'")
  if printf '%s' "${still_present}" | grep -qv '^0$'; then
    echo "核对失败：过期合成行未被清理，见上面的查询输出" >&2
    exit 1
  fi
  local missing
  missing=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -Atc \
    "SELECT count(*) FROM galaxy_import_batch WHERE id = 'gib_drill_synthetic_fresh'")
  if [[ "${missing}" != "1" ]]; then
    echo "核对失败：未过期合成行不应被清理，但它不见了" >&2
    exit 1
  fi

  local real_sync_run_count_after
  real_sync_run_count_after=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${SOURCE_DB_USER}" -d "${SOURCE_DB_NAME}" -Atc \
    "SELECT count(*) FROM feishu_org_sync_run WHERE id NOT LIKE 'orgsync_drill_%'")
  if [[ "${real_sync_run_count_after}" != "${REAL_SYNC_RUN_COUNT_BEFORE}" ]]; then
    echo "核对失败：真实恢复的 feishu_org_sync_run 对照行清理前 ${REAL_SYNC_RUN_COUNT_BEFORE} 行、清理后 ${real_sync_run_count_after} 行，理应逐行不变" >&2
    exit 1
  fi
  log "核对通过：过期合成行已清除；未过期合成行与真实恢复行（含 feishu_org_sync_run 全部真实行）逐行保留。"
}

step_destroy() {
  log "== 销毁隔离实例与临时产物 =="
  docker rm -f -v "${DRILL_DB_CONTAINER}" >/dev/null
  docker network rm "${DRILL_NETWORK}" >/dev/null
  rm -f "${DUMP_PATH}" "${RESTORE_LOG}"
  log "已删除容器 ${DRILL_DB_CONTAINER}（含其匿名数据卷 ${DRILL_VOLUME}）、网络 ${DRILL_NETWORK}、dump 与恢复日志临时文件"
  log "-- 残留盘点 --"
  docker ps -a --filter "name=${DRILL_DB_CONTAINER}"
  docker network ls --filter "name=${DRILL_NETWORK}"
  if [[ -n "${DRILL_VOLUME}" ]] && docker volume inspect "${DRILL_VOLUME}" >/dev/null 2>&1; then
    echo "残留盘点失败：匿名数据卷 ${DRILL_VOLUME} 仍然存在" >&2
    exit 1
  fi
  log "匿名数据卷 ${DRILL_VOLUME} 已确认不存在"
  df -h /
}

step_preflight
step_backup
step_isolate_and_restore
if [[ "${INJECT_SYNTHETIC}" == "1" ]]; then
  step_inject_synthetic
  step_run_real_cleanup
  step_verify
fi
step_destroy
log "演练完成。"
