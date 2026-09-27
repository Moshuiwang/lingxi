#!/usr/bin/env bash
# db_switch_to_local.sh —— 数据库切换脚本：托管库（Supabase）→ 同主机本地 PostgreSQL 17 容器（deploy/compose.db.yaml）。
# 来源：Issue #809 v2.2 定稿、2026-09-22 生产实跑的那一版（原名 s30_p2_db_switch.sh），#896 脱敏入仓；与实跑版的行为
# 差异只有四处，见 #896 留痕：缺省输入文件名、用法打印取段方式、来源连接串文件可改（LINGXI_S30_SOURCE_ENV_FILE，
# 不设时与实跑版逐字同一文件）、install-pg 末尾加打「[回读判据]」行（#886 第 2 处）。
# 对来源库永远只读：不写、不删、不改来源侧任何对象。
# 适用场景：生产（2026-09-22 已迁完，不再跑）/ 预发（#896：来源 = Supabase stage，或其保底导出装进的临时替身库）/
# 恢复演练（只用 install-pg + restore + verify 形，不跑 stop-write / switch-dsn）。
# 取用：脚本、deploy/compose.db.yaml 与输入文件 db_switch_to_local.env（样例 deploy/db_switch_to_local.env.example）
# 一律从仓库同一提交取，放进宿主同一个 0700 目录再跑；不得从个人目录、scratchpad 或聊天记录取。
# 不可逆步骤：cleanup --yes（删工作目录里的 dump 三件）。其余每步开头打印「可逆性 / 回退子命令」：install-pg 起的容器
# 与数据目录只停不删；switch-dsn 改的八份文件先逐份备份，rollback-dsn 逐字节还原；hosts 行只追加、首次写前备份一次。
# 停止条件：任一「前置不满足」「错误：」行即停（脚本已 die，未做后续改动）；verify 出「差异 N 项」不得 switch-dsn；
# switch-dsn 回读发现旧主机段残留 → 先 start-services 再 rollback-dsn；Promotion 之后出错不得直接 rollback-dsn，单独请示。
# 回读判据（#886 第 2 处）：compose.db.yaml 是「输入副本」，在位 sha 须等于 LINGXI_S30_COMPOSE_SHA（= 仓库该文件
# sha256sum）；compose.env / .env.db / hosts 行 / 改写后的 env 文件是「现场生成」，不与任何输入文件 sha 比对——
# compose.env 比本次打印的 sha，.env.db 与 hosts 行按存在性，env 文件按「旧主机段 0 命中」。
# 用法：sudo -n bash db_switch_to_local.sh <子命令> [--yes]
# 子命令按停写窗口剧本顺序（每个子命令开头打印「本步是否可逆 / 回退子命令」；预计时长按 27 MB 库）：
#   preflight     只读。来源库事实 + 本机前置 → <工作目录>/preflight.json + preflight.env      < 1 分
#   install-pg    幂等。compose.env / .env.db / 复制 compose → up -d → healthy + 宿主 hosts 行 → 读写探针
#                 → 预建恢复前置（extensions + pg_trgm、占位角色、public 处置）                < 2 分
#   stop-write    停拉取 timer + 三容器（gateway 60 / worker-queue 90 / scheduler 150 秒）+ 采样 timer；
#                 记 stop_write_at（停写窗口开始）                                              < 5 分
#   dump          经一次性 postgres:17 容器 pg_dump -Fc -n public（保留属主 / ACL）+ sha256 + 来源快照 < 1 分
#   restore       校验 sha → 目标必须为空 → pg_restore --exit-on-error（不带 --no-owner / --no-privileges） < 2 分
#   verify        目标 vs 来源快照逐项比对：对象计数 / 逐表行数 / 序列 / 触发器 / pg_trgm / 属主 /
#                 ACL / SECURITY DEFINER / alembic / 编码与 locale 五项 / 角色级 search_path；任一差异非 0 退出 < 1 分
#   switch-dsn    八份文件原子替换 DSN（先备份到独立目录）→ 回读；须 verify 通过且同一 run_id      < 1 分
#   start-timers  起拉取 timer + 采样 timer（Promotion 之后跑，让拉取代理部署新版本）
#   postcheck     等三容器 healthy（Promotion 后由部署器起）→ 容器内只读回读 → 记 postcheck_at、打印停写时长
#   rollback-dsn  备份目录里的文件原子还原 + 回读 + 起两个 timer（不动本地库容器、不删数据）
#   start-services 起三个已停的旧容器（旧 DSN 已烙进容器：等于回到 Supabase，切换前回退用）
#   smoke-test    仅测试形：以 postgres 调 lingxi_retention_cleanup(now(), 1)，证 SECURITY DEFINER 属主链
#   status        只读汇总；cleanup --yes 只删工作目录下的 dump 三件
# 停写窗口预算 30 分：stop-write → dump → restore → verify → switch-dsn → Promotion → start-timers → 部署 → postcheck。
# 输入全走 LINGXI_S30_* 环境变量，缺省从同目录 db_switch_to_local.env 读（KEY=VALUE，不执行）；清单见文件末尾。
# 测试形：非 root + LINGXI_S30_ROOT=<假根>（路径全部加前缀、跳过属主设置；systemctl / docker 可注入桩）。
set -euo pipefail
export LC_ALL=C
umask 077  # 本脚本落盘的都是私有材料；需要 0644 的地方显式 install -m

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUB="${1:-}"; FLAG="${2:-}"
[[ -n "$SUB" ]] || { sed -n '/^# 用法：/,/^#   smoke-test /p' "${BASH_SOURCE[0]}" >&2; exit 2; }

say() { printf 's30 %s\n' "$*"; }
die() { printf 's30 错误：%s\n' "$*" >&2; exit 1; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
sha_of() { if [[ -f "$1" ]]; then sha256sum -- "$1" | cut -d' ' -f1; else echo absent; fi; }
banner() { say "== $SUB：可逆性=$1；回退=$2"; }

# --- 读输入文件（缺省 db_switch_to_local.env）：只接受 LINGXI_S30_* 的 KEY=VALUE 行，环境里已设的值优先 ---
ENV_FILE="${LINGXI_S30_ENV_FILE:-$SELF_DIR/db_switch_to_local.env}"
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"; line="${line#"${line%%[![:space:]]*}"}"
    [[ "$line" =~ ^(LINGXI_S30_[A-Z_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"; val="${val%\"}"; val="${val#\"}"
    [[ -n "${!key:-}" ]] || export "$key=$val"
  done < "$ENV_FILE"
fi

ROOT="${LINGXI_S30_ROOT:-}"
OWNER_ARGS=(-o root -g root); TEST_FORM=0
if [[ "$(id -u)" -ne 0 ]]; then
  [[ -n "$ROOT" ]] || die "必须以 root 运行（sudo -n bash $0 $SUB）；假根目录测试请设 LINGXI_S30_ROOT"
  OWNER_ARGS=(); TEST_FORM=1; say "测试形：非 root + LINGXI_S30_ROOT=$ROOT，跳过属主设置"
fi

json_str() { grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$1" | head -n1 | sed 's/^[^:]*:[[:space:]]*"//; s/"$//'; }
HOST_CONTRACT="${LINGXI_S30_HOST_CONTRACT:-$ROOT/opt/lingxi/control/host-contract.json}"
[[ -f "$HOST_CONTRACT" ]] || die "宿主契约不存在：$HOST_CONTRACT"
PROJECT="${LINGXI_S30_PROJECT:-$(json_str "$HOST_CONTRACT" project)}"
CONFIG_ROOT="$ROOT${LINGXI_S30_CONFIG_ROOT:-$(json_str "$HOST_CONTRACT" config_root)}"
HOST_ENV="$(json_str "$HOST_CONTRACT" environment)"
SUF="${LINGXI_S30_ENV_SUFFIX:-$([[ "$HOST_ENV" == production ]] && echo prod || echo stage)}"
DOCKER="${LINGXI_S30_DOCKER:-$(json_str "$HOST_CONTRACT" docker)}"; DOCKER="${DOCKER:-docker}"
SYSTEMCTL="${LINGXI_S30_SYSTEMCTL:-systemctl}"
APP_NET="${LINGXI_S30_APP_NETWORK:-${PROJECT}_default}"
DB_PROJECT="${LINGXI_S30_DB_PROJECT:-lingxi-db}"
DB_CONTAINER="${LINGXI_S30_DB_CONTAINER:-lingxi-db}"
DB_HOST_PORT="${LINGXI_S30_DB_HOST_PORT:-5432}"
DB_CONFIG_DIR="$ROOT${LINGXI_S30_DB_CONFIG_DIR:-/etc/lingxi/db}"
DB_INSTALL_DIR="$ROOT${LINGXI_S30_DB_INSTALL_DIR:-/opt/lingxi/db}"
DB_DATA_DIR="$ROOT${LINGXI_S30_DB_DATA_DIR:-/var/lib/lingxi/pgdata}"
WORK_ROOT="$ROOT${LINGXI_S30_WORK_ROOT:-/var/lib/lingxi/migrate}"
BACKUP_ROOT="$ROOT${LINGXI_S30_BACKUP_ROOT:-/root/lingxi-s30-backup}"
MONITOR_ENV="$ROOT${LINGXI_S30_MONITOR_ENV:-/opt/lingxi/monitoring/db-business.env}"
HOSTS_FILE="$ROOT${LINGXI_S30_HOSTS_FILE:-/etc/hosts}"  # F-W4-5：部署器 --network host 只读探针靠宿主 hosts 解析本地库容器名
COMPOSE_SRC="${LINGXI_S30_COMPOSE_SRC:-$SELF_DIR/compose.db.yaml}"
COMPOSE_SHA="${LINGXI_S30_COMPOSE_SHA:-}"
PG_IMAGE="${LINGXI_S30_PG_IMAGE:-postgres:17@sha256:f4c66b820c6f974249089d3d16d86a3698eae11e8746eb6644b2271031e91232}"
MIN_FREE_GB="${LINGXI_S30_MIN_FREE_GB:-10}"
# public 处置缺省 toc：drop 形实测会丢掉 initdb 给 public 的缺省 ACL（PUBLIC USAGE），verify 判红
PUBLIC_MODE="${LINGXI_S30_PUBLIC_MODE:-toc}"
ALLOW_NONEMPTY="${LINGXI_S30_ALLOW_NONEMPTY_RESTORE:-0}"
POSTCHECK_TIMEOUT="${LINGXI_S30_POSTCHECK_TIMEOUT:-1200}"
PULL_SETTLE_TIMEOUT="${LINGXI_S30_PULL_SETTLE_TIMEOUT:-600}"  # F4：stop-write 等在途拉取单轮退出的上限（秒）
PULL_TIMER="${LINGXI_S30_PULL_TIMER:-lingxi-release-pull.timer}"
SAMPLER_TIMER="${LINGXI_S30_SAMPLER_TIMER:-lingxi-db-business-sample.timer}"
APP_SERVICES=(gateway:60 worker-queue:90 scheduler:150)
ENV_FILES=("$CONFIG_ROOT/.env.$SUF")
for s in scheduler gateway worker worker-queue migrate reauthorize; do ENV_FILES+=("$CONFIG_ROOT/.env.$SUF.$s"); done
ENV_FILES+=("$MONITOR_ENV")
# 来源连接串所在文件（#896）：缺省 = 实跑版同一文件 .env.<env>.migrate 的 LINGXI_MIGRATION_DSN；预发来源不可读、改用保底
# 导出装进的临时替身库时，指向一份只含 LINGXI_MIGRATION_DSN=<替身库连接串> 的 0600 文件（不进八份改写清单，switch-dsn 不动它）
SOURCE_ENV_FILE="${LINGXI_S30_SOURCE_ENV_FILE:-$CONFIG_ROOT/.env.$SUF.migrate}"
PARAM_OVERRIDE_RE='[?&](host|hostaddr|user|password|dbname|port|service|passfile)='  # F9：查询参数带这些键会覆盖 authority 里的新地址 / 账号（service / passfile 经 libpq 服务文件 / 口令文件间接覆盖）
DSN_LINE_RE='^(LINGXI_[A-Z_]*_DSN=)(["'"'"']?)(postgres(ql)?(\+psycopg)?)://([^@[:space:]]*)@([^/?"'"'"'[:space:]]*)/([^?"'"'"'[:space:]]*)(\?[^"'"'"'[:space:]]*)?(["'"'"']?)[[:space:]]*$'

# --- 工作目录：preflight 新建 <WORK_ROOT>/<run_id>/ 并把路径记入 <WORK_ROOT>/current；其余子命令沿用 ---
WORK_DIR="${LINGXI_S30_WORK_DIR:-}"
if [[ -z "$WORK_DIR" && -f "$WORK_ROOT/current" ]]; then WORK_DIR="$(cat "$WORK_ROOT/current")"; fi
need_work_dir() { [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]] || die "没有工作目录：先跑 preflight（或设 LINGXI_S30_WORK_DIR）"; }
state_get() { [[ -f "$WORK_DIR/state.env" ]] && sed -n "s/^$1=//p" "$WORK_DIR/state.env" | tail -n1 || true; }
state_set() { touch "$WORK_DIR/state.env"; grep -v "^$1=" "$WORK_DIR/state.env" > "$WORK_DIR/state.env.tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$WORK_DIR/state.env.tmp"; mv -f "$WORK_DIR/state.env.tmp" "$WORK_DIR/state.env"; }
run_id() { need_work_dir; state_get S30_RUN_ID; }
mkdir_private() { install -d -m 700 ${OWNER_ARGS[@]+"${OWNER_ARGS[@]}"} -- "$1"; }
write_private() { # 内容经 stdin → 0600 文件（先临时文件再 mv）
  local tmp; tmp="$(mktemp -- "$(dirname -- "$1")/.s30.XXXXXX")"; cat > "$tmp"
  install -m 600 ${OWNER_ARGS[@]+"${OWNER_ARGS[@]}"} -- "$tmp" "$1.tmp-s30"; rm -f -- "$tmp"; mv -f -- "$1.tmp-s30" "$1"; }

# --- 来源库访问：DSN 只从 SOURCE_ENV_FILE（缺省 .env.<env>.migrate）读，去引号、去 +psycopg 方言后缀，经 0600 临时 env 文件注入
#     一次性 postgres:17 容器（--network host），不进宿主 argv、不打印、不落日志。容器内 psql / pg_dump 的
#     argv 会含 DSN（同 backup_restore_drill.sh 的取舍：libpq 的 -d 是官方姿势，容器随即销毁）。---
source_dsn_file() {
  local raw; raw="$(sed -n 's/^LINGXI_MIGRATION_DSN=//p' "$SOURCE_ENV_FILE" | tail -n1)"
  [[ -n "$raw" ]] || die "读不到 $SOURCE_ENV_FILE 的 LINGXI_MIGRATION_DSN"
  raw="${raw%"${raw##*[![:space:]]}"}"  # 行尾空白先去掉（与 switch-dsn 的容忍一致），再去引号与方言后缀
  raw="${raw%\'}"; raw="${raw#\'}"; raw="${raw%\"}"; raw="${raw#\"}"; raw="${raw/#postgresql+psycopg:\/\//postgresql://}"
  SRC_ENV="$(mktemp -- "${TMPDIR:-/tmp}/.s30-src.XXXXXX")"; chmod 600 "$SRC_ENV"; printf 'PGDSN=%s\n' "$raw" > "$SRC_ENV"
}
src_run() { # 用法：src_run <容器内命令串>（stdin 透传；PGDSN 只在容器环境里）
  source_dsn_file
  local rc=0
  if ! "$DOCKER" run --rm -i --network host --env-file "$SRC_ENV" "$PG_IMAGE" sh -c "$1"; then rc=1; fi
  rm -f -- "$SRC_ENV"; return $rc
}
json_sql() { # 用法：json_sql <标签> <json 文件>：把文件内容以 dollar-quoting 嵌进 SQL（不经 bash 插值，内容不被改写）
  printf '$%s$' "$1"; cat -- "$2"; printf '$%s$::jsonb' "$1"; }
src_psql() { src_run 'exec psql -X -Atq -v ON_ERROR_STOP=1 -d "$PGDSN" -f -'; }
tgt_psql() { "$DOCKER" exec -i "$DB_CONTAINER" psql -U postgres -d postgres -X -Atq -v ON_ERROR_STOP=1 -f -; }
tgt_sql() { printf '%s\n' "$1" | tgt_psql; }

FACTS_SQL=$(cat <<'SQL'
WITH cls AS (
  SELECT c.oid, c.relname, c.relkind, c.relowner, c.relacl
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'
), tbl AS (SELECT * FROM cls WHERE relkind IN ('r', 'p')),
rowcounts AS (
  SELECT relname, (xpath('/row/cnt/text()', query_to_xml(format('SELECT count(*) AS cnt FROM public.%I', relname),
         false, true, '')))[1]::text::bigint AS cnt FROM tbl
), procs AS (
  SELECT p.oid, p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' AS sig, p.proowner, p.proacl, p.prosecdef
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'
), trg AS (SELECT c.relname, t.tgname FROM pg_trigger t JOIN cls c ON c.oid = t.tgrelid WHERE NOT t.tgisinternal),
owned AS (
  SELECT 'relation:' || relname AS k, relowner AS o, relacl AS a FROM cls WHERE relkind IN ('r', 'p', 'S', 'v', 'm')
  UNION ALL SELECT 'function:' || sig, proowner, proacl FROM procs
  UNION ALL SELECT 'schema:public', nspowner, nspacl FROM pg_namespace WHERE nspname = 'public'
), acls AS (SELECT k, (SELECT json_agg(y ORDER BY y) FROM (SELECT regexp_replace(x::text, '/[^/]*$', '') AS y FROM unnest(a) x) n) AS acl,
                    (SELECT json_agg(x::text ORDER BY x::text) FROM unnest(a) x) AS acl_raw FROM owned)
SELECT json_build_object(
  'schema', 1, 'server_version', current_setting('server_version'), 'database', current_database(),
  'encoding', (SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()),
  'datcollate', (SELECT datcollate FROM pg_database WHERE datname = current_database()),
  'datctype', (SELECT datctype FROM pg_database WHERE datname = current_database()),
  'datlocprovider', (SELECT datlocprovider::text FROM pg_database WHERE datname = current_database()),
  'datlocale', (SELECT datlocale FROM pg_database WHERE datname = current_database()),
  'datcollversion', (SELECT datcollversion FROM pg_database WHERE datname = current_database()),
  'role_search_path', (SELECT substr(c, 13) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole, unnest(s.setconfig) c
                         WHERE r.rolname = 'postgres' AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))
                           AND c LIKE 'search_path=%' ORDER BY s.setdatabase DESC LIMIT 1),
  'db_size_bytes', pg_database_size(current_database()),
  'alembic_head', (SELECT version_num FROM public.alembic_version LIMIT 1),
  'counts', json_build_object('tables', (SELECT count(*) FROM tbl), 'indexes', (SELECT count(*) FROM cls WHERE relkind IN ('i', 'I')),
    'sequences', (SELECT count(*) FROM cls WHERE relkind = 'S'), 'views', (SELECT count(*) FROM cls WHERE relkind IN ('v', 'm')),
    'functions', (SELECT count(*) FROM procs), 'triggers', (SELECT count(*) FROM trg)),
  'tables', coalesce((SELECT json_object_agg('public.' || relname, cnt ORDER BY relname) FROM rowcounts), '{}'::json),
  'sequences', coalesce((SELECT json_object_agg('public.' || sequencename, last_value ORDER BY sequencename)
                          FROM pg_sequences WHERE schemaname = 'public'), '{}'::json),
  'triggers', coalesce((SELECT json_object_agg(relname, tg ORDER BY relname)
                         FROM (SELECT relname, json_agg(tgname ORDER BY tgname) AS tg FROM trg GROUP BY relname) g), '{}'::json),
  'extensions', coalesce((SELECT json_agg(json_build_object('name', extname, 'version', extversion, 'schema', nspname) ORDER BY extname)
                           FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace), '[]'::json),
  'app_extensions', coalesce((SELECT json_object_agg(extname, extversion || '@' || nspname)
                               FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE extname = 'pg_trgm'), '{}'::json),
  'owners', coalesce((SELECT json_object_agg(k, o::regrole::text ORDER BY k) FROM owned), '{}'::json),
  'acls', coalesce((SELECT json_object_agg(k, coalesce(acl, '[]'::json) ORDER BY k) FROM acls), '{}'::json),
  'acls_raw', coalesce((SELECT json_object_agg(k, coalesce(acl_raw, '[]'::json) ORDER BY k) FROM acls), '{}'::json),
  'retention_cleanup', (SELECT json_build_object('owner', proowner::regrole::text, 'prosecdef', prosecdef)
                          FROM procs WHERE sig LIKE 'lingxi_retention_cleanup(%'),
  'roles_referenced', coalesce((SELECT json_agg(r ORDER BY r) FROM (SELECT DISTINCT r FROM (
      SELECT o::regrole::text AS r FROM owned UNION SELECT split_part(x::text, '=', 1) FROM owned, unnest(a) x
      UNION SELECT split_part(x::text, '/', 2) FROM owned, unnest(a) x
      UNION SELECT d.defaclrole::regrole::text FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace WHERE n.nspname = 'public'
      UNION SELECT split_part(x::text, '=', 1) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace, unnest(d.defaclacl) x WHERE n.nspname = 'public'
      UNION SELECT split_part(x::text, '/', 2) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace, unnest(d.defaclacl) x WHERE n.nspname = 'public'
      UNION SELECT rolname FROM pg_roles WHERE rolname LIKE 'lingxi\_%') u WHERE r <> '') d), '[]'::json),
  'settings', json_build_object('idle_in_transaction_session_timeout', current_setting('idle_in_transaction_session_timeout'),
    'idle_session_timeout', current_setting('idle_session_timeout'), 'max_connections', current_setting('max_connections')))::text
SQL
)
# 来源 KV（供 bash 直接消费，不解析 JSON）：datcollate / datctype / encoding / 角色清单 / 版本
KV_SQL="SELECT 'S30_DATCOLLATE=''' || datcollate || E'''\nS30_DATCTYPE=''' || datctype || E'''\nS30_ENCODING=''' || pg_encoding_to_char(encoding)
 || E'''\nS30_DATLOCPROVIDER=''' || datlocprovider::text || E'''\nS30_DATLOCALE=''' || coalesce(datlocale, '') || '''' FROM pg_database WHERE datname = current_database();
SELECT 'S30_SRC_VERSION=''' || current_setting('server_version') || '''';
SELECT 'S30_ROLE_SEARCH_PATH=''' || coalesce((SELECT substr(c, 13) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole, unnest(s.setconfig) c
  WHERE r.rolname = 'postgres' AND s.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database())) AND c LIKE 'search_path=%'
  ORDER BY s.setdatabase DESC LIMIT 1), '') || '''';
SELECT 'S30_ROLES=''' || string_agg(r, ' ' ORDER BY r) || '''' FROM (SELECT DISTINCT r FROM (
  SELECT c.relowner::regrole::text AS r FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'
  UNION SELECT p.proowner::regrole::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'
  UNION SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'
  UNION SELECT split_part(x::text, '=', 1) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, unnest(c.relacl) x WHERE n.nspname = 'public'
  UNION SELECT split_part(x::text, '=', 1) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, unnest(p.proacl) x WHERE n.nspname = 'public'
  UNION SELECT split_part(x::text, '=', 1) FROM pg_namespace, unnest(nspacl) x WHERE nspname = 'public'
  UNION SELECT split_part(x::text, '/', 2) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, unnest(c.relacl) x WHERE n.nspname = 'public'
  UNION SELECT split_part(x::text, '/', 2) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, unnest(p.proacl) x WHERE n.nspname = 'public'
  UNION SELECT split_part(x::text, '/', 2) FROM pg_namespace, unnest(nspacl) x WHERE nspname = 'public'
  UNION SELECT d.defaclrole::regrole::text FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace WHERE n.nspname = 'public'
  UNION SELECT split_part(x::text, '=', 1) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace, unnest(d.defaclacl) x WHERE n.nspname = 'public'
  UNION SELECT split_part(x::text, '/', 2) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace, unnest(d.defaclacl) x WHERE n.nspname = 'public'
  UNION SELECT rolname FROM pg_roles WHERE rolname LIKE 'lingxi\_%') u WHERE r <> '') d;"

# F6：preflight.env 只许 S30_键='值' 或安全裸值（S30_RUN_ID）；任一行不合即拒绝 source，只打印键名不打印值。
#     bash 单引号串里除单引号外一切字面（反斜杠、反引号、$ 都不解释），所以只拒单引号与换行（换行会把值拆成不合形状的下一行）；
#     预发来源的角色级 search_path 实际存储为 "\$user", public, extensions（含反斜杠），必须原样放行、原样镜像。
ENV_LINE_RE="^S30_[A-Z_]+=('[^']*'|[0-9A-Za-z_.:-]*)\$"
check_preflight_env() { local l n=0 k; while IFS= read -r l || [[ -n "$l" ]]; do n=$((n + 1)); [[ -z "$l" ]] && continue
    [[ "$l" =~ $ENV_LINE_RE ]] || { k="${l%%=*}"; [[ "$l" =~ ^S30_[A-Z_]+= ]] || k="第 $n 行（非键值行）"; die "来源元数据含不安全字符：$k"; }; done < "$1"; }
void_verify() { [[ -f "$WORK_DIR/verify.json" ]] || return 0; rm -f -- "$WORK_DIR/verify.json"; say "[verify.json] 旧通过记录已作废（$SUB 重跑后须重新 verify）"; }  # F5
db_health() { local h; h="$("$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null)" || h=""; printf '%s\n' "${h:-absent}"; }
wait_healthy() { local n=0; until [[ "$(db_health "$1")" == healthy ]]; do (( n++ >= $2 / 5 )) && return 1; sleep 5; done; }
tgt_public_objects() { tgt_sql "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p','S','v','m'))
  + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public')"; }
HOSTS_RE="^127\.0\.0\.1[[:space:]]+$DB_CONTAINER([[:space:]]|\$)"  # F-W4-5：目标行 127.0.0.1 <容器名>（后随空白或行尾；行尾可带注释）
hosts_count() { if [[ -f "$HOSTS_FILE" ]]; then grep -cE "$HOSTS_RE" "$HOSTS_FILE" || true; else echo 0; fi; }

# ============================ preflight ============================
do_preflight() {
  banner 只读 无需回退
  local id fails=() free_gb port_busy hn
  if [[ -z "${LINGXI_S30_WORK_DIR:-}" && -n "$WORK_DIR" && -n "$(state_get S30_SWITCH_DSN_AT)" && -z "$(state_get S30_POSTCHECK_AT)" ]]; then  # F10：别把 current 从进行中的一轮挪走
    die "上一轮切换进行中（run_id=$(state_get S30_RUN_ID)）：先 rollback-dsn 或 postcheck，或显式 LINGXI_S30_WORK_DIR=<旧目录>"; fi
  id="$(date -u +%Y%m%dT%H%M%SZ)"; WORK_DIR="$WORK_ROOT/$id"
  mkdir_private "$WORK_ROOT"; mkdir_private "$WORK_DIR"; printf '%s\n' "$WORK_DIR" > "$WORK_ROOT/current"
  state_set S30_RUN_ID "$id"; state_set S30_PREFLIGHT_AT "$(now_utc)"
  say "工作目录 $WORK_DIR（run_id=$id）；宿主契约 project=$PROJECT environment=$HOST_ENV → env 后缀 .$SUF"
  command -v "$DOCKER" >/dev/null || fails+=("docker 不可用：$DOCKER")
  "$DOCKER" compose version >/dev/null 2>&1 || fails+=("docker compose v2 不可用")
  [[ "$COMPOSE_SHA" =~ ^[0-9a-f]{64}$ ]] || fails+=("LINGXI_S30_COMPOSE_SHA 缺失或不是 64 位十六进制")
  [[ "$(sha_of "$COMPOSE_SRC")" == "$COMPOSE_SHA" ]] || fails+=("compose 副本 sha ≠ 期望：$COMPOSE_SRC")
  [[ "$PUBLIC_MODE" == drop || "$PUBLIC_MODE" == toc ]] || fails+=("LINGXI_S30_PUBLIC_MODE 只接受 drop / toc")
  for f in "${ENV_FILES[@]}"; do [[ -f "$f" ]] || fails+=("env 文件不存在：$f"); done
  [[ -f "$SOURCE_ENV_FILE" ]] || fails+=("来源连接串文件不存在：$SOURCE_ENV_FILE")
  say "来源连接串文件：$SOURCE_ENV_FILE（只读其中 LINGXI_MIGRATION_DSN，值不打印）"
  "$DOCKER" network inspect "$APP_NET" >/dev/null 2>&1 || fails+=("应用网络不存在：$APP_NET")
  mkdir -p -- "$(dirname -- "$DB_DATA_DIR")"
  free_gb=$(( $(df --output=avail -B1 "$(dirname -- "$DB_DATA_DIR")" | tail -n1) / 1073741824 ))
  (( free_gb >= MIN_FREE_GB )) || fails+=("数据目录所在盘余量 ${free_gb} GB < ${MIN_FREE_GB} GB")
  if [[ "$(db_health "$DB_CONTAINER")" == absent ]]; then
    port_busy=0; (exec 3<>"/dev/tcp/127.0.0.1/$DB_HOST_PORT") 2>/dev/null && port_busy=1
    (( port_busy == 0 )) || fails+=("回环端口 $DB_HOST_PORT 已被占用且本地库容器不在位")
  else say "本地库容器已在位：$DB_CONTAINER（$(db_health "$DB_CONTAINER")）"; fi
  say "本机前置：余盘 ${free_gb} GB、compose sha 相符、public 处置=$PUBLIC_MODE"
  hn="$(hosts_count)"  # F-W4-5：只读打印，不判红（install-pg 负责写）
  if [[ ! -f "$HOSTS_FILE" ]]; then say "[hosts] 127.0.0.1 $DB_CONTAINER：文件不存在 $HOSTS_FILE（install-pg 会判红）"
  elif (( hn >= 1 )); then say "[hosts] 127.0.0.1 $DB_CONTAINER：在位（匹配行 $hn）"; else say "[hosts] 127.0.0.1 $DB_CONTAINER：absent（install-pg 会写）"; fi
  say "读取来源库事实（只读）…"
  printf '%s\n' "$FACTS_SQL" | src_psql > "$WORK_DIR/preflight.json.tmp" || { rm -f "$WORK_DIR/preflight.json.tmp"; fails+=("来源库事实查询失败（连接或权限）"); }
  if [[ -f "$WORK_DIR/preflight.json.tmp" ]]; then
    write_private "$WORK_DIR/preflight.json" < "$WORK_DIR/preflight.json.tmp"; rm -f "$WORK_DIR/preflight.json.tmp"
    { printf 'S30_RUN_ID=%s\n' "$id"; printf '%s\n' "$KV_SQL" | src_psql; } | write_private "$WORK_DIR/preflight.env"
    check_preflight_env "$WORK_DIR/preflight.env"  # F6
    # shellcheck disable=SC1091
    source "$WORK_DIR/preflight.env"
    say "来源：版本 ${S30_SRC_VERSION%% *}；locale $S30_ENCODING $S30_DATCOLLATE/$S30_DATCTYPE provider=$S30_DATLOCPROVIDER icu=${S30_DATLOCALE:-无}；角色级 search_path=${S30_ROLE_SEARCH_PATH:-无}；角色占位清单：$S30_ROLES"
    say "来源摘要：$(grep -oE '"(alembic_head|counts|app_extensions|retention_cleanup|db_size_bytes)" ?: ?("[^"]*"|\{[^}]*\}|[0-9]+)' "$WORK_DIR/preflight.json" | tr '\n' ' ')"
    say "来源 idle 超时现值：$(grep -oE '"settings" ?: ?\{[^}]*\}' "$WORK_DIR/preflight.json")"
  fi
  if ((${#fails[@]})); then for f in "${fails[@]}"; do say "前置不满足：$f"; done; return 1; fi
  say "preflight 通过 → $WORK_DIR/preflight.json（0600）"
}

# ============================ install-pg ============================
# 回读判据（#886 第 2 处）：输入副本比输入 sha；现场生成的内容不与任何输入文件 sha 比对（后者必然不同）
install_pg_readback() { # $1 = 本次按输入推算并打印的 compose.env sha
  local bad=0 cur
  cur="$(sha_of "$DB_INSTALL_DIR/compose.db.yaml")"
  if [[ "$cur" == "$COMPOSE_SHA" ]]; then say "[回读判据] compose.db.yaml 输入副本：在位 sha=$cur = LINGXI_S30_COMPOSE_SHA → ok"
  else say "[回读判据] compose.db.yaml 输入副本：在位 sha=$cur ≠ LINGXI_S30_COMPOSE_SHA → 不符"; bad=1; fi
  cur="$(sha_of "$DB_CONFIG_DIR/compose.env")"
  if [[ "$cur" == "$1" ]]; then say "[回读判据] compose.env 现场生成：在位 sha=$cur = 本次打印值（不与输入文件 sha 比对）→ ok"
  else say "[回读判据] compose.env 现场生成：在位 sha=$cur ≠ 本次打印值 $1 → 不符"; bad=1; fi
  if [[ -s "$DB_CONFIG_DIR/.env.db" && "$(stat -c %a -- "$DB_CONFIG_DIR/.env.db")" == 600 ]]; then say "[回读判据] .env.db 现场生成：存在、非空、0600（内容与 sha 不打印）→ ok"
  else say "[回读判据] .env.db 现场生成：缺失、为空或权限不是 0600 → 不符"; bad=1; fi
  cur="$(hosts_count)"
  if (( cur >= 1 )); then say "[回读判据] hosts 行 现场生成：127.0.0.1 $DB_CONTAINER 匹配行 $cur → ok"
  else say "[回读判据] hosts 行 现场生成：匹配行 0 → 不符"; bad=1; fi
  (( bad == 0 )) || die "install-pg 回读判据不符（见上方「[回读判据] … 不符」行）"
}
do_install_pg() {
  banner "可逆（容器可停可删，数据目录保留）" "docker compose … down（不带 -v）"
  need_work_dir; [[ -f "$WORK_DIR/preflight.env" ]] || die "缺 preflight.env，先跑 preflight"
  check_preflight_env "$WORK_DIR/preflight.env"  # F6
  # shellcheck disable=SC1091
  source "$WORK_DIR/preflight.env"
  # 目录只在缺失时新建：数据目录首次 initdb 后属主变为容器内 postgres，重跑不得再 chown / chmod 它
  local d; for d in "$DB_CONFIG_DIR" "$DB_INSTALL_DIR" "$DB_DATA_DIR"; do [[ -d "$d" ]] || mkdir_private "$d"; done
  local pw compose_env
  if [[ -f "$DB_CONFIG_DIR/.env.db" ]]; then say "[口令] already_in_place（沿用 $DB_CONFIG_DIR/.env.db）"
  else pw="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)"; [[ ${#pw} -eq 32 ]] || die "口令生成失败"
    printf 'POSTGRES_PASSWORD=%s\n' "$pw" | write_private "$DB_CONFIG_DIR/.env.db"; say "[口令] 已生成 → $DB_CONFIG_DIR/.env.db（0600，不打印）"; fi
  # initdb 参数按来源 locale provider 派生：ICU（i）带 --locale-provider=icu --icu-locale=<datlocale>，libc（c）只给 lc_collate / lc_ctype
  local initdb="--encoding=$S30_ENCODING --lc-collate=$S30_DATCOLLATE --lc-ctype=$S30_DATCTYPE"
  if [[ "$S30_DATLOCPROVIDER" == i ]]; then [[ -n "$S30_DATLOCALE" ]] || die "来源 provider=icu 但读不到 datlocale"; initdb+=" --locale-provider=icu --icu-locale=$S30_DATLOCALE"
  elif [[ "$S30_DATLOCPROVIDER" != c ]]; then die "来源 locale provider=$S30_DATLOCPROVIDER 不支持自动派生（只认 i / c）"; fi
  compose_env="$(printf 'LINGXI_DB_ENV_FILE=%s\nLINGXI_DB_DATA_DIR=%s\nLINGXI_DB_CONTAINER_NAME=%s\nLINGXI_DB_APP_NETWORK=%s\nLINGXI_DB_HOST_PORT=%s\nLINGXI_DB_INITDB_ARGS="%s --data-checksums"\n' \
    "$DB_CONFIG_DIR/.env.db" "$DB_DATA_DIR" "$DB_CONTAINER" "$APP_NET" "$DB_HOST_PORT" "$initdb")"
  local v; for v in $(compgen -v LINGXI_S30_DBVAR_ || true); do compose_env+=$(printf '\nLINGXI_DB_%s=%s' "${v#LINGXI_S30_DBVAR_}" "${!v}"); done
  if [[ -f "$DB_CONFIG_DIR/compose.env" && "$(cat "$DB_CONFIG_DIR/compose.env")" == "$compose_env" ]]; then say "[compose.env] already_in_place sha=$(sha_of "$DB_CONFIG_DIR/compose.env")"
  else printf '%s\n' "$compose_env" | write_private "$DB_CONFIG_DIR/compose.env"; say "[compose.env] written sha=$(sha_of "$DB_CONFIG_DIR/compose.env")（initdb 参数派生自来源：$initdb）"; fi
  local compose_env_sha; compose_env_sha="$(printf '%s\n' "$compose_env" | sha256sum | cut -d' ' -f1)"
  if [[ "$(sha_of "$DB_INSTALL_DIR/compose.db.yaml")" == "$COMPOSE_SHA" ]]; then say "[compose] already_in_place sha=$COMPOSE_SHA"
  else install -m 644 ${OWNER_ARGS[@]+"${OWNER_ARGS[@]}"} -- "$COMPOSE_SRC" "$DB_INSTALL_DIR/compose.db.yaml.tmp-s30"; mv -f -- "$DB_INSTALL_DIR/compose.db.yaml.tmp-s30" "$DB_INSTALL_DIR/compose.db.yaml"
    [[ "$(sha_of "$DB_INSTALL_DIR/compose.db.yaml")" == "$COMPOSE_SHA" ]] || die "compose 回读 sha 不符"; say "[compose] installed sha=$COMPOSE_SHA"; fi
  "$DOCKER" compose --project-name "$DB_PROJECT" --env-file "$DB_CONFIG_DIR/compose.env" -f "$DB_INSTALL_DIR/compose.db.yaml" up -d > "$WORK_DIR/compose-up.log" 2>&1 \
    || die "docker compose up 失败：见 $WORK_DIR/compose-up.log（同一条命令换 config 可排查，其输出含口令，不要贴留痕）"
  wait_healthy "$DB_CONTAINER" 120 || die "本地库 120 秒内未 healthy：$DB_CONTAINER（docker logs 排查）"
  say "[容器] $DB_CONTAINER healthy；端口 $("$DOCKER" port "$DB_CONTAINER" 5432/tcp | tr '\n' ' ')"
  # F-W4-5：部署器的宿主网络只读探针（docker run --network host）继承宿主 hosts，本地库容器名须在宿主解析到回环
  [[ -f "$HOSTS_FILE" ]] || die "宿主 hosts 文件不存在：$HOSTS_FILE"
  local hn; hn="$(hosts_count)"
  if (( hn >= 1 )); then say "[hosts] already_in_place（匹配行 $hn）"
  else
    [[ -e "$HOSTS_FILE.bak-s30" ]] || cp -p -- "$HOSTS_FILE" "$HOSTS_FILE.bak-s30"  # 只备份一次：首次写前的原文
    local nl=""; [[ -z "$(tail -c1 -- "$HOSTS_FILE")" ]] || nl=$'\n'  # 原文无尾换行时先补换行，不粘连上一行
    printf '%s127.0.0.1 %s  # lingxi-s30：本地库容器名在宿主网络的解析（部署器 --network host 只读探针用）\n' "$nl" "$DB_CONTAINER" >> "$HOSTS_FILE"
    hn="$(hosts_count)"; (( hn >= 1 )) || die "宿主 hosts 追加后回读 0 行：$HOSTS_FILE（备份 $HOSTS_FILE.bak-s30）"
    say "[hosts] added（备份 $HOSTS_FILE.bak-s30；匹配行 $hn）"
  fi
  if (( TEST_FORM == 0 )); then [[ "$(getent hosts "$DB_CONTAINER" 2>/dev/null | awk 'NR == 1 { print $1 }')" == 127.0.0.1 ]] || die "宿主解析 $DB_CONTAINER 不是 127.0.0.1"; fi  # 测试形跳过（桩不可信）
  [[ "$(tgt_sql 'SELECT 1')" == 1 ]] || die "空转探针 SELECT 1 失败"
  [[ "$(tgt_sql "CREATE TEMP TABLE s30_probe(x int); INSERT INTO s30_probe VALUES (1); SELECT count(*) FROM s30_probe; DROP TABLE s30_probe;")" == 1 ]] || die "读写探针失败"
  say "[探针] 空转 + 读写各一次通过；参数：$(tgt_sql "SELECT 'max_connections=' || current_setting('max_connections') || ' idle_in_txn=' || current_setting('idle_in_transaction_session_timeout') || ' checksums=' || current_setting('data_checksums')")"
  tgt_sql "CREATE SCHEMA IF NOT EXISTS extensions; CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;" >/dev/null
  say "[前置] extensions schema + pg_trgm：$(tgt_sql "SELECT extversion || '@' || nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE extname = 'pg_trgm'")"
  local r created=0 kept=0
  for r in $S30_ROLES; do
    if [[ "$(tgt_sql "SELECT count(*) FROM pg_roles WHERE rolname = '$r'")" == 1 ]]; then kept=$((kept + 1))
    else tgt_sql "CREATE ROLE \"$r\" NOLOGIN" >/dev/null; created=$((created + 1)); fi
  done
  say "[前置] 占位角色（NOLOGIN、无成员、无属性）：新建 $created、已在位 $kept（清单：$S30_ROLES）"
  if [[ -n "$S30_ROLE_SEARCH_PATH" ]]; then # 来源角色级 search_path（托管方给 postgres 设了 "$user", public, extensions）镜像到本地同名角色
    local cur; cur="$(tgt_sql "SELECT substr(c, 13) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole, unnest(s.setconfig) c WHERE r.rolname = 'postgres' AND s.setdatabase = 0 AND c LIKE 'search_path=%' LIMIT 1")"
    if [[ "$cur" == "$S30_ROLE_SEARCH_PATH" ]]; then say "[前置] 角色级 search_path already_in_place"
    else tgt_sql "ALTER ROLE postgres SET search_path = $S30_ROLE_SEARCH_PATH" >/dev/null; say "[前置] 角色级 search_path 已镜像来源：$S30_ROLE_SEARCH_PATH"; fi
  fi
  local objects; objects="$(tgt_public_objects)"
  if [[ "$PUBLIC_MODE" == drop ]]; then
    if [[ "$(tgt_sql "SELECT count(*) FROM pg_namespace WHERE nspname = 'public'")" == 0 ]]; then say "[前置] public schema already_dropped（dump 自带 CREATE SCHEMA public）"
    elif [[ "$objects" == 0 ]]; then tgt_sql "DROP SCHEMA public" >/dev/null; say "[前置] public schema 已删除（空；dump 自带 CREATE SCHEMA public，恢复时重建并还原属主 / ACL）"
    else die "public 已有 $objects 个对象，不删除；恢复目标必须是新 initdb 形（或显式 LINGXI_S30_ALLOW_NONEMPTY_RESTORE=1 并自行清理）"; fi
  else say "[前置] public 处置=toc（restore 时从 TOC 去掉 CREATE SCHEMA public 条目，保留 initdb 缺省 ACL）；现有对象 $objects"; fi
  install_pg_readback "$compose_env_sha"
  state_set S30_INSTALL_PG_AT "$(now_utc)"; say "install-pg 完成"
}

# ============================ stop-write ============================
do_stop_write() {
  banner "可逆" "start-services + start-timers（切换前）"
  need_work_dir; local svc name timeout unit waited=0 st
  "$SYSTEMCTL" stop "$PULL_TIMER"; say "[timer] $PULL_TIMER 已停（拉取代理停写窗口内不部署）"
  unit="${PULL_TIMER%.timer}.service"  # F4：停 timer 不等于停已在途的单轮（单元允许 3600 秒），等它退出再停容器
  while st="$("$SYSTEMCTL" is-active "$unit" || true)"; [[ "$st" == active || "$st" == activating ]]; do
    (( waited < PULL_SETTLE_TIMEOUT )) || die "$unit 仍 $st（已等 ${waited} 秒 ≥ LINGXI_S30_PULL_SETTLE_TIMEOUT=$PULL_SETTLE_TIMEOUT）：在途部署未结束，未停容器、未记 stop_write_at"
    sleep 5; waited=$((waited + 5))
  done
  say "[service] $unit is-active=${st:-unknown}（等待 ${waited} 秒）"
  for svc in "${APP_SERVICES[@]}"; do name="$PROJECT-${svc%%:*}-1"; timeout="${svc##*:}"
    if [[ "$("$DOCKER" inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo absent)" == running ]]; then
      "$DOCKER" stop --time "$timeout" "$name" >/dev/null; say "[容器] $name 已停（宽限 ${timeout}s）"
    else say "[容器] $name already_stopped"; fi
  done
  "$SYSTEMCTL" stop "$SAMPLER_TIMER"; say "[timer] $SAMPLER_TIMER 已停"
  state_set S30_STOP_WRITE_AT "$(now_utc)"; say "停写窗口开始 stop_write_at=$(state_get S30_STOP_WRITE_AT)（预算 30 分钟）"
}

# ============================ dump ============================
do_dump() {
  banner "只读（来源）" "无需回退"
  need_work_dir; void_verify; [[ -n "$(state_get S30_STOP_WRITE_AT)" ]] || die "尚未 stop-write：dump 必须在停写窗口内（快照才是终态）"
  local svc name running=""
  for svc in "${APP_SERVICES[@]}"; do name="$PROJECT-${svc%%:*}-1"
    [[ "$("$DOCKER" inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false)" == true ]] && running+="$name "; done
  [[ -z "$running" ]] || die "应用容器仍在运行：${running}—— 先 stop-write（start-services 回退过之后 dump 必须重新停写）"
  local dump="$WORK_DIR/source.dump"
  say "pg_dump -Fc -n public（保留属主与 ACL）…"
  src_run 'exec pg_dump -Fc -n public --no-password -d "$PGDSN"' > "$dump.part" || { rm -f "$dump.part"; die "pg_dump 失败（现场保留）"; }
  mv -f "$dump.part" "$dump"; (cd "$WORK_DIR" && sha256sum source.dump > source.dump.sha256)
  printf '%s\n' "$FACTS_SQL" | src_psql | write_private "$WORK_DIR/source-facts.json"
  local dumped_at; dumped_at="$(now_utc)"
  { printf "SELECT json_build_object('schema', 1, 'dumped_at', '%s', 'database', j->'database', 'alembic_head', j->'alembic_head', 'tables', j->'tables', 'sequences', j->'sequences', 'extensions', (SELECT json_agg((e->>'name') || '@' || (e->>'version')) FROM jsonb_array_elements(j->'extensions') e))::text FROM (SELECT " "$dumped_at"
    json_sql s30j "$WORK_DIR/source-facts.json"; printf ' AS j) s\n'; } | tgt_psql | write_private "$WORK_DIR/source.dump.counts.json"
  state_set S30_DUMP_AT "$dumped_at"
  say "dump 完成：$(stat -c %s "$dump") B，sha=$(cut -d' ' -f1 "$WORK_DIR/source.dump.sha256")；来源快照 source-facts.json / source.dump.counts.json 已写"
}

# ============================ restore ============================
do_restore() {
  banner "半可逆（只写本地库；失败留现场）" "清空本地库 public 后重跑 restore"
  need_work_dir; void_verify; local dump="$WORK_DIR/source.dump" objects
  [[ -f "$dump" && -f "$dump.sha256" ]] || die "缺 source.dump / .sha256，先跑 dump"
  (cd "$WORK_DIR" && sha256sum -c --quiet source.dump.sha256) || die "dump 校验和不符，拒绝恢复"
  [[ "$(db_health "$DB_CONTAINER")" == healthy ]] || die "本地库不 healthy：$DB_CONTAINER"
  objects="$(tgt_public_objects)"
  if [[ "$objects" != 0 ]]; then
    [[ "$ALLOW_NONEMPTY" == 1 ]] || die "目标 public 已有 $objects 个对象，拒绝恢复到非空库（LINGXI_S30_ALLOW_NONEMPTY_RESTORE=1 可强制）"
    say "警告：LINGXI_S30_ALLOW_NONEMPTY_RESTORE=1，目标非空（$objects 个对象）仍继续"
  fi
  local extra=()
  if [[ "$PUBLIC_MODE" == toc ]]; then
    "$DOCKER" exec -i "$DB_CONTAINER" pg_restore -l < "$dump" | grep -Ev '^[0-9]+; [0-9]+ [0-9]+ (SCHEMA - public |COMMENT - SCHEMA public )' > "$WORK_DIR/restore.toc"
    "$DOCKER" cp "$WORK_DIR/restore.toc" "$DB_CONTAINER:/tmp/s30-restore.toc"; extra=(-L /tmp/s30-restore.toc)
    say "[toc] 已去掉 CREATE SCHEMA public / COMMENT 条目，保留 ACL - SCHEMA public"
  fi
  say "pg_restore --exit-on-error（不带 --no-owner / --no-privileges）…"
  if ! "$DOCKER" exec -i "$DB_CONTAINER" pg_restore -U postgres -d postgres --exit-on-error "${extra[@]}" < "$dump" 2> "$WORK_DIR/restore.log"; then
    sed 's/^/  /' "$WORK_DIR/restore.log" | head -n 8; die "pg_restore 失败（现场保留：$WORK_DIR/restore.log；重跑前先清空目标 public）"
  fi
  if [[ "$PUBLIC_MODE" == toc ]]; then # 跳过的 SCHEMA 条目也带着属主：来源 public 属主若不同于 initdb 缺省，这里补齐
    "$DOCKER" exec "$DB_CONTAINER" rm -f /tmp/s30-restore.toc
    local src_owner; src_owner="$(grep -oE '"schema:public" ?: ?"[^"]*"' "$WORK_DIR/source-facts.json" | head -n1 | sed 's/.*: *"//; s/"$//')"
    if [[ -n "$src_owner" && "$(tgt_sql "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'")" != "$src_owner" ]]; then
      tgt_sql "ALTER SCHEMA public OWNER TO \"$src_owner\"" >/dev/null; say "[toc] public 属主已改为来源的 $src_owner"; fi
  fi
  state_set S30_RESTORE_AT "$(now_utc)"
  say "restore 完成：public 对象 $(tgt_public_objects)、表 $(tgt_sql "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p')")、alembic $(tgt_sql 'SELECT version_num FROM public.alembic_version')"
}

# ============================ verify ============================
do_verify() {
  banner 只读 无需回退
  need_work_dir; [[ -f "$WORK_DIR/source-facts.json" ]] || die "缺 source-facts.json，先跑 dump"
  local id diffs expected dsha; id="$(run_id)"; dsha="$(cut -d' ' -f1 "$WORK_DIR/source.dump.sha256" 2>/dev/null || true)"  # F5：通过记录绑定到本次 dump
  printf '%s\n' "$FACTS_SQL" | tgt_psql | write_private "$WORK_DIR/target-facts.json"
  diffs="$({ printf 'WITH s AS (SELECT '; json_sql s30j "$WORK_DIR/source-facts.json"; printf ' AS j), t AS (SELECT '; json_sql s30t "$WORK_DIR/target-facts.json"; printf ' AS j)\n'
    cat <<'SQL'
    SELECT 'top' AS sec, k, (s.j->k)::text, (t.j->k)::text FROM s, t, unnest(ARRAY['encoding','datcollate','datctype','datlocprovider','datlocale','role_search_path','alembic_head','counts','app_extensions','retention_cleanup']) k
     WHERE s.j->k IS DISTINCT FROM t.j->k
    UNION ALL
    SELECT sec, key, (s.j->sec->key)::text, (t.j->sec->key)::text FROM s, t, unnest(ARRAY['tables','sequences','triggers','owners','acls']) sec,
      LATERAL (SELECT jsonb_object_keys(s.j->sec) AS key UNION SELECT jsonb_object_keys(t.j->sec)) keys
     WHERE s.j->sec->key IS DISTINCT FROM t.j->sec->key ORDER BY 1, 2
SQL
    } | tgt_psql)"
  expected="$({ printf "SELECT string_agg((e->>'name') || '@' || (e->>'version') || '/' || (e->>'schema'), ', ' ORDER BY e->>'name') FROM jsonb_array_elements("; json_sql s30j "$WORK_DIR/source-facts.json"
    printf -- "->'extensions') e WHERE e->>'name' NOT IN ('pg_trgm', 'plpgsql')\n"; } | tgt_psql)"
  local collv_src collv_tgt acl_raw; collv_src="$(grep -oE '"datcollversion" ?: ?"[^"]*"' "$WORK_DIR/source-facts.json" | sed 's/.*: *"//; s/"$//')"; collv_tgt="$(grep -oE '"datcollversion" ?: ?"[^"]*"' "$WORK_DIR/target-facts.json" | sed 's/.*: *"//; s/"$//')"
  # ACL 比对按「grantee + 权限位」归一（去掉 /grantor 段）；含 grantor 的原文两侧都记进 verify.json 供人工核对
  acl_raw="$({ printf 'SELECT json_build_object(%s, ' "'source'"; json_sql s30j "$WORK_DIR/source-facts.json"; printf -- "->'acls_raw', 'target', "; json_sql s30t "$WORK_DIR/target-facts.json"; printf -- "->'acls_raw')::text\n"; } | tgt_psql)"
  say "预期差异（只列不判红）：来源平台扩展 = ${expected:-无}；来源平台角色不比对（只比 public 对象引用到的角色，已占位）；datcollversion 来源 ${collv_src:-无} / 目标 ${collv_tgt:-无}（随本机 ICU 版本，索引已重建）"
  say "比对项：encoding / datcollate / datctype / datlocprovider / datlocale / 角色级 search_path / alembic_head / 对象计数 / pg_trgm 版本与 schema / SECURITY DEFINER 属主 / 逐表行数 / 序列 / 触发器 / 逐对象属主 / 逐对象 ACL（grantee + 权限位，grantor 不比对、原文记 verify.json）"
  if [[ -n "$diffs" ]]; then
    say "差异 $(printf '%s\n' "$diffs" | wc -l) 项（段|键|来源|目标）："; printf '%s\n' "$diffs" | sed 's/^/  /'
    printf '{"schema":1,"run_id":"%s","ok":false,"differences":%s,"dump_sha256":"%s","datcollversion":{"source":"%s","target":"%s"},"acls_raw":%s,"verified_at":"%s"}\n' "$id" "$(printf '%s\n' "$diffs" | wc -l)" "$dsha" "$collv_src" "$collv_tgt" "$acl_raw" "$(now_utc)" | write_private "$WORK_DIR/verify.json"
    die "verify 未通过，不得进入 switch-dsn"
  fi
  printf '{"schema":1,"run_id":"%s","ok":true,"differences":0,"dump_sha256":"%s","datcollversion":{"source":"%s","target":"%s"},"acls_raw":%s,"verified_at":"%s"}\n' "$id" "$dsha" "$collv_src" "$collv_tgt" "$acl_raw" "$(now_utc)" | write_private "$WORK_DIR/verify.json"
  say "verify 零差异：$(grep -oE '"(counts|retention_cleanup|app_extensions)" ?: ?\{[^}]*\}' "$WORK_DIR/target-facts.json" | tr '\n' ' ') → $WORK_DIR/verify.json"
}

# ============================ switch-dsn / rollback-dsn ============================
rewrite_dsn_file() { # 文件 新主机:端口 口令 [dry] → REWRITE_RESULT=changed|already_in_place|no_dsn_line|unparsed_dsn_line；旧主机记入 OLD_HOSTS（直接调用，不经 $(…)）
  local file="$1" newhost="$2" pw="$3" dry="${4:-}" line out="" changed=0 lines=0 dsn_lines=0 raw_dsn=0 override=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    lines=$((lines + 1))
    [[ "$line" =~ ^LINGXI_[A-Z_]*_DSN= ]] && raw_dsn=$((raw_dsn + 1))
    if [[ "$line" =~ $DSN_LINE_RE ]]; then
      local key="${BASH_REMATCH[1]}" q="${BASH_REMATCH[2]}" scheme="${BASH_REMATCH[3]}" oldhost="${BASH_REMATCH[7]}" params="${BASH_REMATCH[9]}"
      [[ "${params,,}" =~ $PARAM_OVERRIDE_RE ]] && { override=$((override + 1)); out+="$line"$'\n'; continue; }  # F9
      dsn_lines=$((dsn_lines + 1))
      params="$(printf '%s' "$params" | sed -E 's/sslmode=[A-Za-z-]+/sslmode=disable/')"
      local new="${key}${q}${scheme}://postgres:${pw}@${newhost}/postgres${params}${q}"
      [[ "$new" == "$line" ]] || { changed=1; [[ " ${OLD_HOSTS[*]:-} " == *" $oldhost "* ]] || OLD_HOSTS+=("$oldhost"); }
      line="$new"
    fi
    out+="$line"$'\n'
  done < "$file"
  (( override == 0 )) || { REWRITE_RESULT="unparsed_dsn_line(查询参数含连接覆盖键 $override 行)"; return; }  # F9
  (( raw_dsn == dsn_lines )) || { REWRITE_RESULT="unparsed_dsn_line($((raw_dsn - dsn_lines)) 行 DSN 不合形状)"; return; }
  (( dsn_lines > 0 )) || { REWRITE_RESULT=no_dsn_line; return; }
  (( changed )) || { REWRITE_RESULT=already_in_place; return; }
  [[ -z "$dry" ]] || { REWRITE_RESULT="changed($dsn_lines 行 DSN)"; return; }
  mkdir_private "$BACKUP_DIR"  # F8：只留本 run_id 首次切换前的副本，重跑不覆盖（rollback-dsn 永远还原到最初内容）
  if [[ -e "$BACKUP_DIR/$(basename -- "$file")" ]]; then say "[backup] $(basename -- "$file") 已有首份备份，保留"; else cp -p -- "$file" "$BACKUP_DIR/$(basename -- "$file")"; fi
  printf '%s' "$out" > "$file.tmp-s30"; chmod --reference="$file" "$file.tmp-s30"; mv -f -- "$file.tmp-s30" "$file"
  [[ "$(wc -l < "$file")" == "$lines" ]] || die "$file 行数变化，请立即 rollback-dsn"
  REWRITE_RESULT="changed($dsn_lines 行 DSN)"
}
do_switch_dsn() {
  banner "可逆（每份先备份）" "rollback-dsn"
  need_work_dir; local id; id="$(run_id)"; BACKUP_DIR="$BACKUP_ROOT/$id"; OLD_HOSTS=()
  local hn; hn="$(hosts_count)"  # F-W4-5 前置门：dry 遍之前、改任何文件之前
  (( hn >= 1 )) || die "宿主 hosts 缺 127.0.0.1 $DB_CONTAINER 行：先 install-pg（部署器宿主网络探针需要）"
  say "[hosts] 127.0.0.1 $DB_CONTAINER 在位（匹配行 $hn）"
  [[ -f "$WORK_DIR/verify.json" ]] || die "缺 verify.json：verify 未跑或未通过"
  grep -q "\"run_id\":\"$id\",\"ok\":true" "$WORK_DIR/verify.json" || die "verify.json 不是本 run_id（$id）的通过记录，拒绝切换"
  local dsha; dsha="$(cut -d' ' -f1 "$WORK_DIR/source.dump.sha256" 2>/dev/null || true)"; [[ -n "$dsha" ]] || die "缺 source.dump.sha256（dump 未跑或已 cleanup），拒绝切换"  # F5
  grep -q "\"dump_sha256\":\"$dsha\"" "$WORK_DIR/verify.json" || die "verify.json 不是当前 dump 的通过记录（dump / restore 重跑过：先重新 verify）"
  local pw; pw="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$DB_CONFIG_DIR/.env.db" | tail -n1)"; [[ -n "$pw" ]] || die "读不到 $DB_CONFIG_DIR/.env.db 的口令"
  local f all_ok=1 hits=0 bad="" pass
  for pass in dry write; do # 先只读扫一遍：任一文件的 DSN 行不合形状、或八份无一命中，都在改任何文件之前判红
    for f in "${ENV_FILES[@]}"; do
      if [[ "$f" == "$MONITOR_ENV" ]]; then rewrite_dsn_file "$f" "127.0.0.1:$DB_HOST_PORT" "$pw" "$([[ $pass == dry ]] && echo dry)"; else rewrite_dsn_file "$f" "$DB_CONTAINER:5432" "$pw" "$([[ $pass == dry ]] && echo dry)"; fi
      [[ $pass == dry ]] || say "[$(basename -- "$f")] $REWRITE_RESULT"
      [[ $pass == write ]] || case "$REWRITE_RESULT" in changed*|already_in_place) hits=$((hits + 1)) ;; unparsed*) bad+="$(basename -- "$f")=$REWRITE_RESULT " ;; esac
    done
    [[ $pass == write ]] && break
    OLD_HOSTS=()
    [[ -z "$bad" ]] || die "DSN 行不合形状、未改任何文件：${bad}（形状：KEY=[引号]postgres[ql][+psycopg]://user:pass@host[:port]/db[?参数][引号]，行尾只许空白）"
    (( hits > 0 )) || die "八份文件无一命中 DSN 行（KEY 形如 LINGXI_*_DSN=），未改任何文件：核对 config_root 与 env 后缀"
  done
  for f in "${ENV_FILES[@]}"; do for h in "${OLD_HOSTS[@]:-}"; do [[ -z "$h" ]] || [[ "$(grep -c -F -- "$h" "$f")" == 0 ]] || { say "回读失败：$f 仍含旧主机段"; all_ok=0; }; done; done
  (( all_ok )) || die "回读发现旧主机段残留，请立即 rollback-dsn"
  say "回读：旧主机段 ${OLD_HOSTS[*]:-（无变化）} 在八份文件中 0 命中；应用侧主机 $DB_CONTAINER:5432、宿主采样侧 127.0.0.1:$DB_HOST_PORT；sslmode 若有已改 disable"
  state_set S30_SWITCH_DSN_AT "$(now_utc)"; [[ -d "$BACKUP_DIR" ]] && say "备份目录 $BACKUP_DIR（0700）"
  say "switch-dsn 完成 → 产品负责人点 Release Promotion → start-timers → 部署 → postcheck"
}
do_rollback_dsn() {
  banner "可逆动作本身" "再次 switch-dsn"
  need_work_dir; local id f b restored=0 same=0; id="$(run_id)"; BACKUP_DIR="$BACKUP_ROOT/$id"
  [[ -d "$BACKUP_DIR" ]] || die "没有备份目录 $BACKUP_DIR（switch-dsn 未改过任何文件）"
  for f in "${ENV_FILES[@]}"; do b="$BACKUP_DIR/$(basename -- "$f")"; [[ -f "$b" ]] || continue
    if cmp -s -- "$b" "$f"; then same=$((same + 1)); say "[$(basename -- "$f")] already_in_place"; continue; fi
    cp -p -- "$b" "$f.tmp-s30"; mv -f -- "$f.tmp-s30" "$f"; cmp -s -- "$b" "$f" || die "$f 还原后与备份不一致"
    restored=$((restored + 1)); say "[$(basename -- "$f")] restored（逐字节等于备份）"
  done
  say "rollback-dsn：还原 $restored、已一致 $same；本地库容器与数据目录未动"
  do_start_timers
}
do_start_timers() { "$SYSTEMCTL" start "$SAMPLER_TIMER" "$PULL_TIMER"; say "[timer] $SAMPLER_TIMER / $PULL_TIMER 已起（is-active：$("$SYSTEMCTL" is-active "$SAMPLER_TIMER" "$PULL_TIMER" | tr '\n' ' ')）"; }
do_start_services() {
  banner "可逆" "stop-write"
  local svc name; for svc in "${APP_SERVICES[@]}"; do name="$PROJECT-${svc%%:*}-1"; "$DOCKER" start "$name" >/dev/null; say "[容器] $name started（沿用容器创建时的 DSN）"; done
}

# ============================ postcheck ============================
do_postcheck() {
  banner 只读 无需回退
  need_work_dir; local svc name n=0 pending
  say "等待三容器 healthy（≤ ${POSTCHECK_TIMEOUT}s；由 Promotion 后的部署器起）…"
  while :; do pending=""
    for svc in "${APP_SERVICES[@]}"; do name="$PROJECT-${svc%%:*}-1"; [[ "$(db_health "$name")" == healthy ]] || pending+="$name=$(db_health "$name") "; done
    [[ -z "$pending" ]] && break
    (( n += 10 )); (( n <= POSTCHECK_TIMEOUT )) || die "超时：$pending"
    (( n % 60 == 0 )) && say "  仍在等：$pending"; sleep 10
  done
  say "[容器] 三容器 healthy"
  local dbip view; dbip="$("$DOCKER" inspect -f "{{(index .NetworkSettings.Networks \"$APP_NET\").IPAddress}}" "$DB_CONTAINER")"
  view="$("$DOCKER" exec "$PROJECT-scheduler-1" python3 -c 'import os,psycopg
c=psycopg.connect(os.environ["LINGXI_POSTGRES_DSN"],autocommit=True,application_name="s30-postcheck");c.read_only=True
print("server_addr", c.execute("select inet_server_addr()").fetchone()[0])
print("alembic", c.execute("select version_num from public.alembic_version").fetchone()[0])
print("tables", c.execute("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname=%s and c.relkind in (%s,%s)",("public","r","p")).fetchone()[0])
print("cleanup_owner", *c.execute("select proowner::regrole::text, prosecdef from pg_proc where proname=%s",("lingxi_retention_cleanup",)).fetchone())')"
  say "[scheduler 容器视角] $(printf '%s' "$view" | tr '\n' ' ')（本地库在应用网络的地址 $dbip）"
  [[ "$view" == *"server_addr $dbip"* ]] || die "scheduler 连的不是本地库（inet_server_addr ≠ $dbip）"
  [[ "$view" == *"cleanup_owner lingxi_retention_owner True"* ]] || die "保留清理函数属主 / SECURITY DEFINER 不符"
  [[ "$("$SYSTEMCTL" is-active "$SAMPLER_TIMER" "$PULL_TIMER" | tr '\n' ' ')" == "active active " ]] || die "timer 未起：先 start-timers"
  state_set S30_POSTCHECK_AT "$(now_utc)"
  local t0 t1; t0="$(date -u -d "$(state_get S30_STOP_WRITE_AT)" +%s 2>/dev/null || echo 0)"; t1="$(date -u -d "$(state_get S30_POSTCHECK_AT)" +%s)"
  say "postcheck 通过；停写窗口 $(state_get S30_STOP_WRITE_AT) → $(state_get S30_POSTCHECK_AT) = $(( (t1 - t0) / 60 )) 分 $(( (t1 - t0) % 60 )) 秒"
}

# ============================ smoke-test / status / cleanup ============================
do_smoke_test() {
  banner "写本地库（删到期行）" "仅测试形"
  (( TEST_FORM )) || die "smoke-test 只在测试形运行（生产 postcheck 只查属主与 prosecdef）"
  local out; out="$(tgt_sql "SELECT target_table || ':' || deleted_rows || ':' || blocked FROM public.lingxi_retention_cleanup(now(), 1)" | tr '\n' ' ')"
  say "以 postgres 调 lingxi_retention_cleanup(now(), 1) 成功：$out（SECURITY DEFINER 以 lingxi_retention_owner 执行，触发器放行）"
}
do_status() {
  banner 只读 无需回退
  say "工作目录 ${WORK_DIR:-无}；$( [[ -n "$WORK_DIR" && -f "$WORK_DIR/state.env" ]] && tr '\n' ' ' < "$WORK_DIR/state.env")"
  say "本地库 $DB_CONTAINER=$(db_health "$DB_CONTAINER")；compose sha=$(sha_of "$DB_INSTALL_DIR/compose.db.yaml") compose.env=$(sha_of "$DB_CONFIG_DIR/compose.env") 口令文件=$( [[ -f "$DB_CONFIG_DIR/.env.db" ]] && echo present || echo absent)"
  say "hosts 行=$(hosts_count)（$HOSTS_FILE）"
  [[ -n "$WORK_DIR" && -f "$WORK_DIR/verify.json" ]] && say "verify：$(cat "$WORK_DIR/verify.json")"
  [[ -n "$WORK_DIR" && -f "$WORK_DIR/source.dump.sha256" ]] && say "dump：$(cat "$WORK_DIR/source.dump.sha256")"
  local svc name; for svc in "${APP_SERVICES[@]}"; do name="$PROJECT-${svc%%:*}-1"; say "应用容器 $name=$(db_health "$name")"; done
  say "timer：$SAMPLER_TIMER=$("$SYSTEMCTL" is-active "$SAMPLER_TIMER" || true) $PULL_TIMER=$("$SYSTEMCTL" is-active "$PULL_TIMER" || true)"
  local f; for f in "${ENV_FILES[@]}"; do say "env $(basename -- "$f")：DSN 行 $(grep -cE '^LINGXI_[A-Z_]*_DSN=' "$f" 2>/dev/null || true)、指向本地库 $(grep -cE "^LINGXI_[A-Z_]*_DSN=.*@($DB_CONTAINER:5432|127\.0\.0\.1:$DB_HOST_PORT)/" "$f" 2>/dev/null || true)"; done
}
do_cleanup() {
  banner "不可逆（删 dump）" "无"
  need_work_dir; [[ "$FLAG" == --yes ]] || die "cleanup 需显式 --yes（只删 $WORK_DIR 下的 source.dump 三件）"
  rm -f -- "$WORK_DIR/source.dump" "$WORK_DIR/source.dump.sha256" "$WORK_DIR/source.dump.counts.json" "$WORK_DIR/restore.toc"; say "cleanup 完成：$WORK_DIR 下 dump 三件已删（preflight / facts / verify / state 保留）"
}

case "$SUB" in
  preflight) do_preflight ;; install-pg) do_install_pg ;; stop-write) do_stop_write ;; dump) do_dump ;;
  restore) do_restore ;; verify) do_verify ;; switch-dsn) do_switch_dsn ;; start-timers) banner 可逆 "systemctl stop"; do_start_timers ;;
  postcheck) do_postcheck ;; rollback-dsn) do_rollback_dsn ;; start-services) do_start_services ;;
  smoke-test) do_smoke_test ;; status) do_status ;; cleanup) do_cleanup ;;
  *) die "未知子命令 $SUB" ;;
esac
# 变量（LINGXI_S30_*；预发 / 生产两形差异只在输入文件）：ROOT（假根，测试）HOST_CONTRACT PROJECT CONFIG_ROOT ENV_SUFFIX(stage|prod)
# DOCKER SYSTEMCTL（可注入桩）APP_NETWORK DB_PROJECT DB_CONTAINER DB_HOST_PORT DB_CONFIG_DIR DB_INSTALL_DIR DB_DATA_DIR
# WORK_ROOT WORK_DIR BACKUP_ROOT MONITOR_ENV SOURCE_ENV_FILE（缺省 <config_root>/.env.<env>.migrate）HOSTS_FILE（缺省 /etc/hosts）COMPOSE_SRC COMPOSE_SHA（必填）PG_IMAGE MIN_FREE_GB PUBLIC_MODE(drop|toc)
# ALLOW_NONEMPTY_RESTORE POSTCHECK_TIMEOUT PULL_SETTLE_TIMEOUT PULL_TIMER SAMPLER_TIMER ENV_FILE；DBVAR_<NAME>=值 → compose.env 的 LINGXI_DB_<NAME>
