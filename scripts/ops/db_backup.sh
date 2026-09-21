#!/usr/bin/env bash
# 本地数据库每日备份单轮（Issue #809；由 deploy/monitoring-units/lingxi-db-backup.timer 触发）。
#
# 做什么：对本机 compose 起的 PostgreSQL 容器（deploy/compose.db.yaml）经 `docker exec`
# 跑一次 `pg_dump -Fc` → 写校验和 → `pg_restore --list` 核完整性 → 逐表精确行数清单
# → 按份数保留 → 可选把三件产物传到异机 → 写状态文件。**对库只读**：全部连接都是容器内
# 本地套接字的 `pg_dump` / `psql -c SELECT`，不持有连接串与口令，不建任何写连接。
#
# 为什么 `docker exec` 而不是宿主 pg_dump：宿主客户端版本与容器服务端可能不同大版本
# （预发主机 16 对 17 直接拒绝），容器自带同版本工具链，且不需要在宿主装 postgresql-client。
#
# 产物（都在 LINGXI_DB_BACKUP_DIR，目录须 root 0700；文件 0600）：
#   lingxi-db-<UTC 时间戳>.dump          pg_dump 自定义格式（压缩但未加密：不需口令即可
#                                        还原全部业务数据，与明文同等对待，目录权限即防线）
#   lingxi-db-<UTC 时间戳>.dump.sha256   `sha256sum` 输出（相对文件名，可在目录内 `-c` 回读）
#   lingxi-db-<UTC 时间戳>.dump.counts.json  行数清单，schema 固定（恢复后逐表比对用）：
#     {"schema":1,"dumped_at":"…Z","database":"postgres","alembic_head":"…",
#      "tables":{"public.<表>":<count(*)>…},"sequences":{"public.<序列>":<last_value>…},
#      "extensions":["pg_trgm@1.6",…]}
#   状态文件 LINGXI_DB_BACKUP_STATUS_FILE（0644，原子写，供宿主健康巡检读；不含路径 /
#   主机 / 用户 / 连接串），schema 固定：
#     {"schema":1,"ok":true|false,"started_at":"…Z","finished_at":"…Z","duration_seconds":N,
#      "dump_file":"<仅文件名>","dump_bytes":N,"dump_sha256":"<hex>","restore_list_ok":bool,
#      "tables":N,"retention_kept":N,"transfer":{"mode":"none|scp","ok":true|false|null,
#      "label":"<标签>"},"error":null|"<错误码>"}
#   失败时尚未得到的字段为 null；任一步失败都先写状态（ok=false + 错误码）再以非 0 退出，
#   systemd 记 Result=failed，宿主巡检据状态文件告警。
#
# 已知边界：dump 与行数清单是两次连接、不共享快照——定时器落在低峰（北京 02:30），
# 两者之间若有写入，恢复后比对会出现可解释的差异；切换脚本的 verify 在停写窗口内跑，
# 不受此影响。
#
# 输入（全部环境变量，由单元的 EnvironmentFile 注入；不接受命令行参数）：
#   LINGXI_DB_BACKUP_CONTAINER     容器名，缺省 lingxi-db
#   LINGXI_DB_BACKUP_DIR           备份目录，缺省 /var/lib/lingxi/backups（须已存在、0700、属主 = 运行账户）
#   LINGXI_DB_BACKUP_KEEP          保留份数，缺省 14（按文件名排序只留最近 N 组）
#   LINGXI_DB_BACKUP_STATUS_FILE   状态文件，缺省 /var/lib/lingxi/db-backup-status.json
#   LINGXI_DB_BACKUP_TRANSFER      none | scp，缺省 none
#   LINGXI_DB_BACKUP_REMOTE        scp 模式必填：user@host:/path（目录须已存在）
#   LINGXI_DB_BACKUP_REMOTE_LABEL  进状态文件的可公开短标签，缺省 offsite
#   LINGXI_DB_BACKUP_SSH_OPTS      ssh / scp 共用选项，缺省 "-o BatchMode=yes -o ConnectTimeout=20"
#
# 退出码：0 成功；1 某一步失败（状态文件已写明错误码）；2 输入不合法。
set -euo pipefail
# 文件名按 UTC 时间戳排序做保留判断，排序不随语言环境变化。
export LC_ALL=C

CONTAINER="${LINGXI_DB_BACKUP_CONTAINER:-lingxi-db}"
BACKUP_DIR="${LINGXI_DB_BACKUP_DIR:-/var/lib/lingxi/backups}"
KEEP="${LINGXI_DB_BACKUP_KEEP:-14}"
STATUS_FILE="${LINGXI_DB_BACKUP_STATUS_FILE:-/var/lib/lingxi/db-backup-status.json}"
TRANSFER="${LINGXI_DB_BACKUP_TRANSFER:-none}"
REMOTE="${LINGXI_DB_BACKUP_REMOTE:-}"
REMOTE_LABEL="${LINGXI_DB_BACKUP_REMOTE_LABEL:-offsite}"
SSH_OPTS_RAW="${LINGXI_DB_BACKUP_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=20}"
# 库名 / 角色与 deploy/compose.db.yaml 的 POSTGRES_DB / POSTGRES_USER 同值。
DB_NAME="postgres"
DB_USER="postgres"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_EPOCH="$(date +%s)"
DUMP_FILE="null"; DUMP_BYTES="null"; DUMP_SHA="null"; RESTORE_LIST_OK="false"
TABLES="null"; RETENTION_KEPT="null"; TRANSFER_OK="null"; STATUS_WRITTEN=0

# 日志只到 stderr（journal），远端地址与任何连接串形态一律脱敏。
mask() {
  local text="$1"
  if [[ -n "${REMOTE}" ]]; then text="${text//"${REMOTE}"/<remote>}"; fi
  printf '%s' "${text}" | sed -E 's#://[^@[:space:]]*@#://<creds>@#g'
}
log() { printf '%s db_backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(mask "$*")" >&2; }

write_status() {
  local ok="$1" error="$2" finished_at duration tmp
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  duration=$(( $(date +%s) - STARTED_EPOCH ))
  mkdir -p "$(dirname "${STATUS_FILE}")"
  tmp="${STATUS_FILE}.tmp.$$"
  printf '{"schema":1,"ok":%s,"started_at":"%s","finished_at":"%s","duration_seconds":%s,"dump_file":%s,"dump_bytes":%s,"dump_sha256":%s,"restore_list_ok":%s,"tables":%s,"retention_kept":%s,"transfer":{"mode":"%s","ok":%s,"label":"%s"},"error":%s}\n' \
    "${ok}" "${STARTED_AT}" "${finished_at}" "${duration}" "${DUMP_FILE}" "${DUMP_BYTES}" \
    "${DUMP_SHA}" "${RESTORE_LIST_OK}" "${TABLES}" "${RETENTION_KEPT}" "${TRANSFER}" \
    "${TRANSFER_OK}" "${REMOTE_LABEL}" "${error}" > "${tmp}"
  chmod 0644 "${tmp}"
  mv -f "${tmp}" "${STATUS_FILE}"
  STATUS_WRITTEN=1
}

# 任一步失败：写状态（错误码进 error）后非 0 退出；本地产物一律保留供取证。
fail() {
  local code="$1" exit_code="$2"; shift 2
  log "失败（${code}）：$*"
  write_status false "\"${code}\""
  exit "${exit_code}"
}

# 兜底：脚本因未预料的错误（set -e 触发、未定义变量）中途退出时也要留一份状态。
on_exit() {
  local rc=$?
  if (( rc != 0 && STATUS_WRITTEN == 0 )); then
    log "意外退出（退出码 ${rc}），写兜底状态"
    write_status false '"unexpected_failure"' || true
  fi
}
trap on_exit EXIT

# ---- 输入校验 --------------------------------------------------------------
[[ "${KEEP}" =~ ^[0-9]+$ && "${KEEP}" -ge 1 ]] || fail invalid_input 2 "LINGXI_DB_BACKUP_KEEP 须为正整数"
[[ "${TRANSFER}" == none || "${TRANSFER}" == scp ]] || fail invalid_input 2 "LINGXI_DB_BACKUP_TRANSFER 只接受 none / scp"
if [[ "${TRANSFER}" == scp ]]; then
  [[ "${REMOTE}" =~ ^[^:[:space:]]+@[^:[:space:]]+:[^[:space:]]+$ ]] \
    || fail invalid_input 2 "scp 模式须设 LINGXI_DB_BACKUP_REMOTE=user@host:/path"
fi
[[ "${REMOTE_LABEL}" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || fail invalid_input 2 "LINGXI_DB_BACKUP_REMOTE_LABEL 只接受 1–32 位字母数字 ._-"
command -v docker >/dev/null 2>&1 || fail invalid_input 2 "缺少 docker 命令"
read -r -a SSH_OPTS <<< "${SSH_OPTS_RAW}"

# ---- ① 前置：容器 healthy、目录 0700 且属主为运行账户、磁盘余量 ≥ 库体量 × 3 -------
health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER}" 2>/dev/null)" \
  || fail container_missing 1 "容器不存在或 docker 不可用"
[[ "${health}" == healthy ]] || fail container_unhealthy 1 "容器健康状态为 ${health}，不备份"

[[ -d "${BACKUP_DIR}" ]] || fail backup_dir_invalid 1 "备份目录不存在（须预先建好，0700）"
dir_stat="$(stat -c '%u %a' "${BACKUP_DIR}")"
[[ "${dir_stat}" == "$(id -u) 700" ]] || fail backup_dir_invalid 1 "备份目录属主 / 权限为「${dir_stat}」，要求「$(id -u) 700」"

exec 9>"${BACKUP_DIR}/.lock"
flock -n 9 || fail already_running 1 "上一轮尚未结束（目录锁被占）"

db_bytes="$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -Atc \
  "SELECT pg_database_size(current_database())" 2>/dev/null)" \
  || fail preflight_query_failed 1 "读库体量失败"
[[ "${db_bytes}" =~ ^[0-9]+$ ]] || fail preflight_query_failed 1 "库体量不是整数：${db_bytes}"
avail_bytes="$(df --output=avail -B1 "${BACKUP_DIR}" | tail -n1 | tr -d ' ')"
if (( avail_bytes < db_bytes * 3 )); then
  fail disk_space_low 1 "备份目录剩余 ${avail_bytes} B，低于库体量 ${db_bytes} B × 3"
fi
log "前置通过：容器 healthy，库体量 ${db_bytes} B，目录余量 ${avail_bytes} B"

# ---- ② pg_dump -Fc 经 stdout 落盘（先写 .part 再改名，目录里只会出现完整文件） ----
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dumped_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
name="lingxi-db-${stamp}.dump"
dump_path="${BACKUP_DIR}/${name}"
if ! docker exec "${CONTAINER}" pg_dump -U "${DB_USER}" -Fc --no-password "${DB_NAME}" > "${dump_path}.part"; then
  rm -f "${dump_path}.part"
  fail dump_failed 1 "pg_dump 失败"
fi
mv -f "${dump_path}.part" "${dump_path}"
DUMP_FILE="\"${name}\""
DUMP_BYTES="$(stat -c %s "${dump_path}")"
log "dump 完成：${name}（${DUMP_BYTES} B）"

# ---- ③ 校验和（相对文件名，目录内 `sha256sum -c` 可直接回读） ---------------------
sha_line="$(cd "${BACKUP_DIR}" && sha256sum "${name}")" || fail checksum_failed 1 "sha256sum 失败"
printf '%s\n' "${sha_line}" > "${dump_path}.sha256"
DUMP_SHA="\"${sha_line%% *}\""

# ---- ④ 完整性：pg_restore --list 只读目录（不连库、不写库） ------------------------
if docker exec -i "${CONTAINER}" pg_restore --list < "${dump_path}" > /dev/null; then
  RESTORE_LIST_OK="true"
else
  fail restore_list_failed 1 "pg_restore --list 读不出目录，dump 不可用"
fi

# ---- ⑤ 行数清单：一次 psql 往返，逐表精确 count(*)（query_to_xml 动态计数） ----------
counts_sql="
WITH t AS (
  SELECT c.relname AS name,
         (xpath('/row/cnt/text()', query_to_xml(
            format('SELECT count(*) AS cnt FROM %I.%I', n.nspname, c.relname), false, true, ''))
         )[1]::text::bigint AS cnt
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
), s AS (
  SELECT sequencename AS name, last_value FROM pg_sequences WHERE schemaname = 'public'
), e AS (
  SELECT extname || '@' || extversion AS ext FROM pg_extension
)
SELECT json_build_object(
  'schema', 1,
  'dumped_at', '${dumped_at}',
  'database', current_database(),
  'alembic_head', (SELECT version_num FROM public.alembic_version LIMIT 1),
  'tables', coalesce((SELECT json_object_agg('public.' || name, cnt ORDER BY name) FROM t), '{}'::json),
  'sequences', coalesce((SELECT json_object_agg('public.' || name, last_value ORDER BY name) FROM s), '{}'::json),
  'extensions', coalesce((SELECT json_agg(ext ORDER BY ext) FROM e), '[]'::json)
)::text;"
if ! printf '%s\n' "${counts_sql}" | docker exec -i "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" \
    -X -Atq -v ON_ERROR_STOP=1 -f - > "${dump_path}.counts.json.part"; then
  rm -f "${dump_path}.counts.json.part"
  fail counts_failed 1 "行数清单查询失败"
fi
mv -f "${dump_path}.counts.json.part" "${dump_path}.counts.json"
TABLES="$(docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -X -Atq -c \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')")" \
  || fail counts_failed 1 "表计数查询失败"
[[ "${TABLES}" =~ ^[0-9]+$ ]] || fail counts_failed 1 "表计数不是整数"
log "行数清单完成：${TABLES} 张表"

# ---- ⑥ 保留：按文件名（= UTC 时间戳）排序只留最近 KEEP 组，三件成组删 -----------------
shopt -s nullglob
dumps=("${BACKUP_DIR}"/lingxi-db-*.dump)
shopt -u nullglob
if (( ${#dumps[@]} > KEEP )); then
  for old in "${dumps[@]:0:${#dumps[@]}-KEEP}"; do
    rm -f "${old}" "${old}.sha256" "${old}.counts.json"
    log "保留策略：已删 $(basename "${old}") 及其校验和 / 行数清单"
  done
fi
shopt -s nullglob
kept=("${BACKUP_DIR}"/lingxi-db-*.dump)
shopt -u nullglob
RETENTION_KEPT="${#kept[@]}"

# ---- ⑦ 传输（scp 模式）：三件到远端，远端 sha256sum -c 回读；失败不删本地 --------------
if [[ "${TRANSFER}" == scp ]]; then
  remote_host="${REMOTE%%:*}"
  remote_path="${REMOTE#*:}"
  transfer_log="$(mktemp)"
  if scp "${SSH_OPTS[@]}" -q "${dump_path}" "${dump_path}.sha256" "${dump_path}.counts.json" \
       "${REMOTE}/" >"${transfer_log}" 2>&1 \
     && ssh "${SSH_OPTS[@]}" "${remote_host}" "cd ${remote_path} && sha256sum -c --quiet ${name}.sha256" \
       >>"${transfer_log}" 2>&1; then
    TRANSFER_OK="true"
    rm -f "${transfer_log}"
    log "传输完成并在远端校验通过（${REMOTE_LABEL}）"
  else
    TRANSFER_OK="false"
    log "传输失败（${REMOTE_LABEL}）：$(tr '\n' ' ' < "${transfer_log}" | cut -c1-300)"
    rm -f "${transfer_log}"
    fail transfer_failed 1 "本地三件已保留，待下一轮或人工重传"
  fi
fi

# ---- ⑧ 状态文件 ------------------------------------------------------------
write_status true null
log "完成：${name}，保留 ${RETENTION_KEPT} 组，耗时 $(( $(date +%s) - STARTED_EPOCH )) 秒"
