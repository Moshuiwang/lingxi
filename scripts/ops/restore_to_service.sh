#!/usr/bin/env bash
# restore_to_service.sh —— 从一份每日备份恢复到「三服务可问数」（Issue #885；Trace #898 S-2-1）。
# 与 backup_restore_drill.sh 的分工：那份脚本只把副本恢复进**隔离实例**、不碰服务与宿主（破坏半径写死在它的头注里）；
# 本脚本恢复的是**正在服务的那个本地库**（deploy/compose.db.yaml），要重建宿主侧文件、改写连接串、重建三服务，
# 破坏半径不同，因此另起一份，不把宿主写入塞进那份只读演练脚本。
# 输入只允许：一份日备份三件（<名>.dump / .dump.sha256 / .dump.counts.json，db_backup.sh 产出）+ 仓库文件
# （本脚本、同目录的 db_switch_to_local.sh / compose.db.yaml / db_switch_to_local.env）。不读旧口令、旧 compose.env、
# 旧数据目录；角色骨架从 dump 实际引用派生，locale 取仓库 compose.db.yaml 的 initdb 缺省值。
# 用法：sudo -n bash restore_to_service.sh <子命令> [参数]
#   wipe --yes            只限预发：造「主机侧空白」。停拉取 / 采样 / 备份 timer 与三服务（db_switch stop-write）
#                         → 本地库 compose down（不带 -v）→ 数据目录、/etc/lingxi/db、/opt/lingxi/db 移进带时间戳的
#                         保留目录、hosts 行摘出另存（都不删）。绝不触碰每日备份目录与异机副本目录。
#   run <dump> [--from=<步>] [--until=<步>]   从空白恢复；步骤依次为
#                         host（空白核对 → 仓库 compose / 新口令 / hosts 行 / 容器 healthy，经 db_switch install-pg）
#                         → roles（角色骨架：从 dump 的属主 / ACL / 默认权限 / 策略引用派生，不写死个数）
#                         → restore（目标须空；TOC 去掉目标已在位的 SCHEMA / EXTENSION 条目；不带 --no-owner）
#                         → verify（.counts.json 逐表行数 / 序列 / alembic / 扩展 + TOC 属主 / ACL + 清理函数属主链）
#                         → dsn（八份 env 的本地库连接串改用新口令；先备份；指向别处即拒绝）
#                         → services（db_switch recreate-services，三服务 healthy）→ timers（起三个 timer）。
#                         每步打印 UTC 时刻；计时以 docker（容器 StartedAt / 健康日志）与 systemd 记录为准，末尾打分段时长表。
#   rollback --yes        演练失败回退：停三服务 → 本次新建的库容器 down、新目录移进保留目录的 failed-run/ →
#                         原目录与 hosts 行移回 → env 文件逐字节还原 → 原库 up → recreate-services → 起 timer。
#   roles <dump>          只读：打印从 dump 派生的角色骨架清单。
#   timing                只读：重打本轮分段时长表。
#   status                只读：本轮状态与空白核对。
# 停止条件：任一「错误：」行即停（已做的步骤不自动撤销，现场保留）；verify 出「差异」不得进入 dsn；
# services 超时 → 先看 docker ps -a / 容器日志，再决定重跑 `run <dump> --from=services` 或 rollback --yes。
# 保留目录（<LINGXI_RESTORE_ROOT>/<轮次>/wiped/）本脚本从不删除，演练收尾由编排者决定清理。
# 测试形：非 root + LINGXI_S30_ROOT=<假根>（与 db_switch_to_local.sh 同一约定；docker / systemctl / 切换脚本可注入桩）。
set -euo pipefail
export LC_ALL=C
umask 077

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUB="${1:-}"; shift || true
[[ -n "$SUB" ]] || { sed -n '/^# 用法：/,/^#   status /p' "${BASH_SOURCE[0]}" >&2; exit 2; }

say() { printf 'rts %s\n' "$*"; }
die() { printf 'rts 错误：%s\n' "$*" >&2; exit 1; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
step_say() { say "[$(now_utc)] $*"; }

# --- 输入：与 db_switch_to_local.sh 共用同一份 LINGXI_S30_* 输入文件（路径事实只有一处）；环境里已设的值优先 ---
ENV_FILE="${LINGXI_S30_ENV_FILE:-$SELF_DIR/db_switch_to_local.env}"
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"; line="${line#"${line%%[![:space:]]*}"}"
    [[ "$line" =~ ^(LINGXI_S30_[A-Z_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"; val="${val%\"}"; val="${val#\"}"
    [[ -n "${!key:-}" ]] || export "$key=$val"
  done < "$ENV_FILE"
fi
export LINGXI_S30_ENV_FILE="$ENV_FILE"

ROOT="${LINGXI_S30_ROOT:-}"
if [[ "$(id -u)" -ne 0 ]]; then
  [[ -n "$ROOT" ]] || die "必须以 root 运行（sudo -n bash $0 $SUB）；假根目录测试请设 LINGXI_S30_ROOT"
  say "测试形：非 root + LINGXI_S30_ROOT=$ROOT"
fi
json_str() { grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$1" | head -n1 | sed 's/^[^:]*:[[:space:]]*"//; s/"$//'; }
HOST_CONTRACT="${LINGXI_S30_HOST_CONTRACT:-$ROOT/opt/lingxi/control/host-contract.json}"
[[ -f "$HOST_CONTRACT" ]] || die "宿主契约不存在：$HOST_CONTRACT"
PROJECT="${LINGXI_S30_PROJECT:-$(json_str "$HOST_CONTRACT" project)}"
CONFIG_ROOT="$ROOT${LINGXI_S30_CONFIG_ROOT:-$(json_str "$HOST_CONTRACT" config_root)}"
HOST_ENV="$(json_str "$HOST_CONTRACT" environment || true)"
SUF="${LINGXI_S30_ENV_SUFFIX:-$([[ "$HOST_ENV" == production ]] && echo prod || echo stage)}"
DOCKER="${LINGXI_S30_DOCKER:-$(json_str "$HOST_CONTRACT" docker || true)}"; DOCKER="${DOCKER:-docker}"
SYSTEMCTL="${LINGXI_S30_SYSTEMCTL:-systemctl}"
DB_PROJECT="${LINGXI_S30_DB_PROJECT:-lingxi-db}"
DB_CONTAINER="${LINGXI_S30_DB_CONTAINER:-lingxi-db}"
DB_HOST_PORT="${LINGXI_S30_DB_HOST_PORT:-5432}"
DB_CONFIG_DIR="$ROOT${LINGXI_S30_DB_CONFIG_DIR:-/etc/lingxi/db}"
DB_INSTALL_DIR="$ROOT${LINGXI_S30_DB_INSTALL_DIR:-/opt/lingxi/db}"
DB_DATA_DIR="$ROOT${LINGXI_S30_DB_DATA_DIR:-/var/lib/lingxi/pgdata}"
MONITOR_ENV="$ROOT${LINGXI_S30_MONITOR_ENV:-/opt/lingxi/monitoring/db-business.env}"
HOSTS_FILE="$ROOT${LINGXI_S30_HOSTS_FILE:-/etc/hosts}"
COMPOSE_SRC="${LINGXI_S30_COMPOSE_SRC:-$SELF_DIR/compose.db.yaml}"
PG_IMAGE="${LINGXI_S30_PG_IMAGE:-postgres:17@sha256:f4c66b820c6f974249089d3d16d86a3698eae11e8746eb6644b2271031e91232}"
PULL_TIMER="${LINGXI_S30_PULL_TIMER:-lingxi-release-pull.timer}"
SAMPLER_TIMER="${LINGXI_S30_SAMPLER_TIMER:-lingxi-db-business-sample.timer}"
# 本脚本自有输入（LINGXI_RESTORE_*）
DRILL_ROOT="$ROOT${LINGXI_RESTORE_ROOT:-/var/lib/lingxi/restore-drill}"
SWITCH="${LINGXI_RESTORE_SWITCH_SCRIPT:-$SELF_DIR/db_switch_to_local.sh}"
BACKUP_TIMER="${LINGXI_RESTORE_BACKUP_TIMER:-lingxi-db-backup.timer}"
# 受保护目录：每日备份目录与异机副本目录（任何账户家目录下的 lingxi-backups）；wipe / rollback 移动的路径不得与之重叠
PROTECT_DIRS=("$ROOT${LINGXI_RESTORE_BACKUP_DIR:-/var/lib/lingxi/backups}" "$ROOT/root/lingxi-backups")
for d in "$ROOT"/home/*/lingxi-backups; do [[ -e "$d" ]] && PROTECT_DIRS+=("$d"); done
# 角色级 search_path：pg_dump 不带角色设置，按迁库时镜像来源的同一条（数据库迁移runbook「verify 判红项」）
ROLE_SEARCH_PATH="${LINGXI_RESTORE_ROLE_SEARCH_PATH:-\"\$user\", public, extensions}"
APP_SERVICES=(gateway scheduler worker-queue)
ENV_FILES=("$CONFIG_ROOT/.env.$SUF")
for s in scheduler gateway worker worker-queue migrate reauthorize; do ENV_FILES+=("$CONFIG_ROOT/.env.$SUF.$s"); done
ENV_FILES+=("$MONITOR_ENV")
STEPS=(host roles restore verify dsn services timers)
HOSTS_RE="^127\.0\.0\.1[[:space:]]+$DB_CONTAINER([[:space:]]|\$)"

PY="${LINGXI_S30_PYTHON:-}"
if [[ -z "$PY" ]]; then if [[ -x /opt/lingxi/bin/python3 ]]; then PY=/opt/lingxi/bin/python3; else PY=python3; fi; fi

# --- 轮次目录：<DRILL_ROOT>/<轮次>/，current 指向进行中的一轮；state.env 与 db_switch 共用（本脚本的键以 RTS_ 开头）---
DRILL=""
[[ -f "$DRILL_ROOT/current" ]] && DRILL="$(cat "$DRILL_ROOT/current")"
state_get() { [[ -n "$DRILL" && -f "$DRILL/state.env" ]] && sed -n "s/^$1=//p" "$DRILL/state.env" | tail -n1 || true; }
state_set() { touch "$DRILL/state.env"; grep -v "^$1=" "$DRILL/state.env" > "$DRILL/state.env.tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$DRILL/state.env.tmp"; mv -f "$DRILL/state.env.tmp" "$DRILL/state.env"; }
drill_open() { [[ -n "$DRILL" && -d "$DRILL" && -z "$(state_get RTS_RUN_DONE_AT)" && -z "$(state_get RTS_ROLLBACK_AT)" ]]; }
new_drill() {
  local id; id="$(date -u +%Y%m%dT%H%M%SZ)"; DRILL="$DRILL_ROOT/$id"
  install -d -m 700 -- "$DRILL_ROOT" "$DRILL"; printf '%s\n' "$DRILL" > "$DRILL_ROOT/current"
  state_set S30_RUN_ID "rts-$id"; say "轮次目录 $DRILL"
}
switch() { LINGXI_S30_WORK_DIR="$DRILL" bash "$SWITCH" "$@"; }

db_state() { local h; h="$("$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null)" || h=""; printf '%s\n' "${h:-absent}"; }
tgt_psql() { "$DOCKER" exec -i "$DB_CONTAINER" psql -U postgres -d postgres -X -Atq -v ON_ERROR_STOP=1 -f -; }
tgt_sql() { printf '%s\n' "$1" | tgt_psql; }
hosts_count() { if [[ -f "$HOSTS_FILE" ]]; then grep -cE "$HOSTS_RE" "$HOSTS_FILE" || true; else echo 0; fi; }
iso_of() { date -u -d "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown; }

# ============================ 解析助手（只用标准库；兼容 3.9）============================
HELPER_PY=$(cat <<'PY'
import json, re, sys
from datetime import datetime, timedelta, timezone

IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
NOT_ROLES = {"public", "current_user", "session_user", "current_role", "group"}


def unquote(tok):
    return tok[1:-1].replace('""', '"') if tok.startswith('"') else tok.lower()


def idents(text):
    return [unquote(t) for t in re.findall(IDENT, text)]


def roles(sql):
    """dump 的 SQL 文本（pg_restore --schema-only -f -）里全部被引用的角色：属主字段 + 授权 / 默认权限 / 策略 / 会话身份。"""
    found = set()
    for line in sql.splitlines():
        m = re.match(r"^-- Name: .*; Owner: ?(.*)$", line)
        if m:  # 每个 TOC 条目的注释头：Owner 字段原样（不加引号）
            if m.group(1).strip() not in ("", "-"):
                found.add(m.group(1).strip())
            continue
        if line.startswith("--"):
            continue
        for m in re.finditer(r"\bOWNER TO (" + IDENT + ")", line):
            found.add(unquote(m.group(1)))
        for m in re.finditer(r"\bSET SESSION AUTHORIZATION (" + IDENT + ")", line):
            found.add(unquote(m.group(1)))
        m = re.match(r"^ALTER DEFAULT PRIVILEGES FOR ROLE (.+?) (?:IN SCHEMA .+? )?(GRANT|REVOKE) (.*)$", line)
        if m:
            found.update(idents(m.group(1)))
            line = m.group(2) + " " + m.group(3)
        m = re.match(r"^(GRANT|REVOKE) .* (?:TO|FROM) (.+?);$", line)
        if m:
            tail = re.split(r" (?:WITH GRANT OPTION|WITH ADMIN OPTION|GRANTED BY|CASCADE|RESTRICT)\b", m.group(2))
            found.update(idents(tail[0]))
            g = re.search(r" GRANTED BY (" + IDENT + ")", m.group(2))
            if g:
                found.add(unquote(g.group(1)))
        m = re.match(r"^CREATE POLICY .*? TO (.+?)(?: USING | WITH CHECK |;$)", line)
        if m:
            found.update(idents(m.group(1)))
    return sorted(r for r in found if r.lower() not in NOT_ROLES)


TOC_REL = re.compile(r"^\d+; \d+ \d+ (TABLE|SEQUENCE|VIEW|MATERIALIZED VIEW) public (\S+) (\S+)$")
TOC_FUNC = re.compile(r"^\d+; \d+ \d+ FUNCTION public ([^(\s]+)\(.*\) (\S+)$")
TOC_SCHEMA = re.compile(r"^\d+; \d+ \d+ SCHEMA - (\S+) (\S+)$")
TOC_EXT = re.compile(r"^\d+; \d+ \d+ EXTENSION - (\S+)\s*$")
ACL_REL = re.compile(r"^\d+; 0 0 ACL public (?:TABLE|SEQUENCE|VIEW|MATERIALIZED VIEW) (\S+) \S+$")
ACL_FUNC = re.compile(r"^\d+; 0 0 ACL public FUNCTION ([^(\s]+)\(")
ACL_SCHEMA = re.compile(r"^\d+; 0 0 ACL - SCHEMA (\S+) \S+$")


def tocfilter(schemas, exts, out):
    """去掉目标已在位的 SCHEMA / EXTENSION 创建条目（其余原序保留）；被去掉的 SCHEMA 打印属主供恢复后补齐。"""
    have_s, have_e = set(filter(None, schemas.split(","))), set(filter(None, exts.split(",")))
    kept = []
    for line in sys.stdin.read().splitlines():
        s, e = TOC_SCHEMA.match(line), TOC_EXT.match(line)
        if s and s.group(1) in have_s:
            print("SCHEMA %s %s" % (s.group(1), s.group(2)))
            continue
        if e and e.group(1) in have_e:
            print("EXTENSION %s" % e.group(1))
            continue
        kept.append(line)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")


def manifest(path):
    data = json.load(open(path, encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("tables"), dict):
        print("ERR 行数清单不合 schema=1 形状")
        sys.exit(3)
    print("%s %d %d" % (data.get("alembic_head"), len(data["tables"]), sum(data["tables"].values())))


def verify(counts_path, toc_path, target_path):
    want = json.load(open(counts_path, encoding="utf-8"))
    have = json.load(open(target_path, encoding="utf-8"))
    toc = open(toc_path, encoding="utf-8").read().splitlines()
    diffs = []

    def diff(sec, key, a, b):
        diffs.append("%s|%s|清单=%s|目标=%s" % (sec, key, json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False)))

    if want.get("alembic_head") != have.get("alembic_head"):
        diff("alembic", "head", want.get("alembic_head"), have.get("alembic_head"))
    for sec in ("tables", "sequences"):
        w, h = want.get(sec) or {}, have.get(sec) or {}
        for key in sorted(set(w) | set(h)):
            if w.get(key) != h.get(key):
                diff(sec, key, w.get(key), h.get(key))
    if sorted(want.get("extensions") or []) != sorted(have.get("extensions") or []):
        diff("extensions", "*", sorted(want.get("extensions") or []), sorted(have.get("extensions") or []))
    rel_owner, func_owner, schema_owner, acl_rel, acl_func, acl_schema = {}, {}, {}, set(), set(), set()
    for line in toc:
        m = TOC_REL.match(line)
        if m:
            rel_owner[m.group(2)] = m.group(3)
        m = TOC_FUNC.match(line)
        if m:
            func_owner.setdefault(m.group(1), []).append(m.group(2))
        m = TOC_SCHEMA.match(line)
        if m:
            schema_owner[m.group(1)] = m.group(2)
        for pat, bucket in ((ACL_REL, acl_rel), (ACL_FUNC, acl_func), (ACL_SCHEMA, acl_schema)):
            m = pat.match(line)
            if m:
                bucket.add(m.group(1))
    for name, owner in sorted(rel_owner.items()):
        if (have.get("rel_owners") or {}).get(name) != owner:
            diff("owner", "relation:" + name, owner, (have.get("rel_owners") or {}).get(name))
    for name, owners in sorted(func_owner.items()):
        if sorted(owners) != sorted((have.get("func_owners") or {}).get(name) or []):
            diff("owner", "function:" + name, sorted(owners), (have.get("func_owners") or {}).get(name))
    for name, owner in sorted(schema_owner.items()):
        if (have.get("schema_owners") or {}).get(name) != owner:
            diff("owner", "schema:" + name, owner, (have.get("schema_owners") or {}).get(name))
    for bucket, key, label in ((acl_rel, "rel_acl", "relation"), (acl_func, "func_acl", "function"), (acl_schema, "schema_acl", "schema")):
        for name in sorted(bucket - set(have.get(key) or [])):
            diff("acl", label + ":" + name, "有授权条目", "无 ACL")
    if "lingxi_retention_cleanup" in func_owner and not (have.get("retention_cleanup") or {}).get("prosecdef"):
        diff("retention_cleanup", "prosecdef", True, (have.get("retention_cleanup") or {}).get("prosecdef"))
    for line in diffs:
        print("DIFF " + line)
    print("SUMMARY 表 %d 张（逐表行数）、序列 %d、alembic %s、扩展 %d、属主 %d 项、ACL %d 项、清理函数属主链 %s" % (
        len(want.get("tables") or {}), len(want.get("sequences") or {}), want.get("alembic_head"),
        len(want.get("extensions") or []), len(rel_owner) + len(func_owner) + len(schema_owner),
        len(acl_rel) + len(acl_func) + len(acl_schema),
        json.dumps(have.get("retention_cleanup"), ensure_ascii=False)))
    sys.exit(1 if diffs else 0)


def parse_ts(text):
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", text.strip())
    if not m:
        return None
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    if m.group(3) != "Z":
        sign = 1 if m.group(3)[0] == "+" else -1
        base -= sign * timedelta(hours=int(m.group(3)[1:3]), minutes=int(m.group(3)[4:6]))
    return base


def health(started):
    """容器健康日志里、本次启动之后第一条成功检查的结束时刻（整秒）；日志只保留最近 5 条，若首条就已成功则标 rolled。"""
    log = json.loads(sys.stdin.read() or "null") or []
    start = parse_ts(started)
    runs = [e for e in log if parse_ts(e.get("Start", "")) and (start is None or parse_ts(e["Start"]) >= start)]
    for index, entry in enumerate(runs):
        if entry.get("ExitCode") == 0:
            mark = "rolled" if index == 0 and len(log) >= 5 else "exact"
            print(parse_ts(entry["End"]).strftime("%Y-%m-%dT%H:%M:%SZ") + " " + mark)
            return
    print("unknown none")


mode, args = sys.argv[1], sys.argv[2:]
if mode == "roles":
    print("\n".join(roles(sys.stdin.read())))
elif mode == "tocfilter":
    tocfilter(*args)
elif mode == "manifest":
    manifest(*args)
elif mode == "verify":
    verify(*args)
elif mode == "health":
    health(*args)
else:
    print("ERR 未知模式 " + mode)
    sys.exit(3)
PY
)
helper() { "$PY" -B -c "$HELPER_PY" "$@"; }

# ============================ 守门 ============================
need_stage() {
  [[ "$HOST_ENV" == stage && "$SUF" == stage ]] || die "$SUB 只限预发：宿主契约 environment=${HOST_ENV:-缺失}、env 后缀=$SUF（须均为 stage），拒绝"
}
need_yes() { [[ "${1:-}" == --yes ]] || die "$SUB 需显式 --yes（会移动本地库数据目录与配置；只移动不删除）"; }
# 被移动的路径不得与受保护目录重叠（相等 / 祖先 / 后代），也不得含有任何备份三件
guard_move_target() {
  local t="$1" p
  for p in "${PROTECT_DIRS[@]}"; do
    [[ "$t" == "$p" || "$p" == "$t"/* || "$t" == "$p"/* ]] && die "拒绝移动 $t：与受保护的备份目录 $p 重叠（不触碰每日备份与异机副本）"
  done
  if [[ -d "$t" ]] && [[ -n "$(find "$t" -maxdepth 4 -name 'lingxi-db-*.dump*' -print -quit 2>/dev/null)" ]]; then
    die "拒绝移动 $t：其中含备份文件 lingxi-db-*.dump*（不触碰每日备份）"
  fi
}
move_targets() { printf '%s\n' "pgdata	$DB_DATA_DIR" "db-config	$DB_CONFIG_DIR" "db-install	$DB_INSTALL_DIR"; }
compose_db() { "$DOCKER" compose --project-name "$DB_PROJECT" --env-file "$DB_CONFIG_DIR/compose.env" -f "$DB_INSTALL_DIR/compose.db.yaml" "$@"; }
stop_timers() { local t; for t in "$PULL_TIMER" "$SAMPLER_TIMER" "$BACKUP_TIMER"; do "$SYSTEMCTL" stop "$t" || true; done; }
start_timers() {
  "$SYSTEMCTL" start "$SAMPLER_TIMER" "$PULL_TIMER" "$BACKUP_TIMER"
  say "[timer] 已起：$("$SYSTEMCTL" is-active "$SAMPLER_TIMER" "$PULL_TIMER" "$BACKUP_TIMER" | tr '\n' ' ')"
}

# ============================ wipe ============================
do_wipe() {
  need_yes "${1:-}"; need_stage
  say "== wipe：可逆（只移动不删除）；回退 = rollback --yes"
  if drill_open && [[ -n "$(state_get RTS_WIPE_AT)" ]]; then die "上一轮（$DRILL）已 wipe 且未收口：先 run 完成或 rollback --yes"; fi
  local name path
  while IFS=$'\t' read -r name path; do guard_move_target "$path"; done < <(move_targets)
  [[ -d "$DB_DATA_DIR" ]] || die "数据目录不存在：$DB_DATA_DIR（已是空白？不重复 wipe）"
  [[ -f "$DB_CONFIG_DIR/compose.env" && -f "$DB_INSTALL_DIR/compose.db.yaml" ]] || die "缺 $DB_CONFIG_DIR/compose.env 或 $DB_INSTALL_DIR/compose.db.yaml：无法按原参数 down"
  new_drill; install -d -m 700 -- "$DRILL/wiped"
  step_say "停拉取 / 采样 timer 与三服务（db_switch stop-write）"
  switch stop-write
  "$SYSTEMCTL" stop "$BACKUP_TIMER" || true; say "[timer] $BACKUP_TIMER 已停（空白期间不跑备份）"
  step_say "本地库 compose down（不带 -v）"
  compose_db down > "$DRILL/compose-down.log" 2>&1 || die "compose down 失败：见 $DRILL/compose-down.log"
  [[ "$(db_state "$DB_CONTAINER")" == absent ]] || die "down 之后本地库容器仍在：$DB_CONTAINER"
  : > "$DRILL/wiped/manifest.tsv"
  while IFS=$'\t' read -r name path; do
    if [[ -e "$path" ]]; then mv -- "$path" "$DRILL/wiped/$name"; printf '%s\t%s\n' "$name" "$path" >> "$DRILL/wiped/manifest.tsv"; say "[移走] $path → $DRILL/wiped/$name"
    else say "[移走] $path 不存在，跳过"; fi
  done < <(move_targets)
  if [[ -f "$HOSTS_FILE" ]]; then
    cp -p -- "$HOSTS_FILE" "$DRILL/wiped/hosts.orig"
    grep -E "$HOSTS_RE" "$HOSTS_FILE" > "$DRILL/wiped/hosts.lines" || true
    grep -vE "$HOSTS_RE" "$HOSTS_FILE" > "$DRILL/wiped/hosts.new" || true
    cat "$DRILL/wiped/hosts.new" > "$HOSTS_FILE"; rm -f -- "$DRILL/wiped/hosts.new"
    say "[hosts] 摘出 $(wc -l < "$DRILL/wiped/hosts.lines") 行 → $DRILL/wiped/hosts.lines；回读匹配行 $(hosts_count)"
  fi
  local p; for p in "${PROTECT_DIRS[@]}"; do [[ -e "$p" ]] && say "[未触碰] $p（$(find "$p" -maxdepth 1 -type f | wc -l) 个文件）"; done
  state_set RTS_WIPE_AT "$(now_utc)"
  step_say "wipe 完成：主机侧空白（容器 absent、三目录已移走、hosts 行已摘出）；下一步 run <dump>"
}

# ============================ run ============================
DUMP=""
dump_sql() { # dump 的 schema-only SQL 文本：本地库容器在跑就借它的 pg_restore，否则一次性容器（不联网）
  if [[ "$(db_state "$DB_CONTAINER")" != absent ]]; then "$DOCKER" exec -i "$DB_CONTAINER" pg_restore --schema-only -f - < "$DUMP"
  else "$DOCKER" run --rm -i --network none "$PG_IMAGE" pg_restore --schema-only -f - < "$DUMP"; fi
}
check_dump() {
  [[ -n "$DUMP" && -f "$DUMP" ]] || die "dump 不存在：${DUMP:-未给}"
  [[ -f "$DUMP.sha256" && -f "$DUMP.counts.json" ]] || die "缺随附文件：$DUMP.sha256 / $DUMP.counts.json（日备份三件须放在一起）"
  local want have; want="$(cut -d' ' -f1 < "$DUMP.sha256")"; have="$(sha256sum -- "$DUMP" | cut -d' ' -f1)"
  [[ "$want" =~ ^[0-9a-f]{64}$ && "$want" == "$have" ]] || die "dump 校验和不符（.sha256 记 ${want:-空}，实算 $have），拒绝恢复"
  local m; m="$(helper manifest "$DUMP.counts.json")" || die "行数清单不可用：$m"
  say "输入：$(basename -- "$DUMP")（sha256 相符 $have）；清单 alembic / 表数 / 总行数 = $m"
}
locale_args() { # 仓库 compose.db.yaml 的 LINGXI_DB_INITDB_ARGS 缺省值 → preflight.env 的 locale 五项
  local def enc loc prov icu
  # shellcheck disable=SC2016 # 匹配的是 compose 文件里的字面 ${…}，不是 shell 展开
  def="$(sed -n 's/.*\${LINGXI_DB_INITDB_ARGS:-\(.*\)}.*/\1/p' "$COMPOSE_SRC" | head -n1)"
  enc="$(grep -oE -- '--encoding=[^ ]+' <<< "$def" | cut -d= -f2)"; loc="$(grep -oE -- '--locale=[^ ]+' <<< "$def" | cut -d= -f2)"
  prov="$(grep -oE -- '--locale-provider=[^ ]+' <<< "$def" | cut -d= -f2)"; icu="$(grep -oE -- '--icu-locale=[^ ]+' <<< "$def" | cut -d= -f2)"
  [[ -n "$enc" && -n "$loc" ]] || die "读不出 $COMPOSE_SRC 的 initdb 缺省参数（--encoding / --locale）"
  [[ "$prov" == icu ]] && prov=i || prov=c
  printf "S30_DATCOLLATE='%s'\nS30_DATCTYPE='%s'\nS30_ENCODING='%s'\nS30_DATLOCPROVIDER='%s'\nS30_DATLOCALE='%s'\n" "$loc" "$loc" "$enc" "$prov" "$icu"
}
record_container() { # $1 容器 $2 键前缀：StartedAt 与首次 healthy 均取 docker 记录
  local started log hl; started="$("$DOCKER" inspect -f '{{.State.StartedAt}}' "$1")"
  log="$("$DOCKER" inspect -f '{{json .State.Health.Log}}' "$1" 2>/dev/null || echo null)"
  hl="$(printf '%s' "$log" | helper health "$started")"
  printf '%s %s %s\n' "$(iso_of "$started")" "${hl% *}" "${hl##* }"
}

step_host() {
  local objects=""
  [[ "$(db_state "$DB_CONTAINER")" == absent ]] || die "不是空白：本地库容器 $DB_CONTAINER 在位（先 wipe --yes，或确认这不是要恢复的主机）"
  [[ ! -e "$DB_DATA_DIR" ]] || objects="$(find "$DB_DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
  [[ -z "$objects" ]] || die "不是空白：数据目录非空 $DB_DATA_DIR"
  [[ ! -e "$DB_CONFIG_DIR/.env.db" && ! -e "$DB_CONFIG_DIR/compose.env" ]] || die "不是空白：$DB_CONFIG_DIR 下仍有口令 / compose.env（不复用旧口令）"
  say "空白核对通过：容器 absent、数据目录空、无口令文件；hosts 行现有 $(hosts_count)"
  { printf 'S30_RUN_ID=%s\n' "$(state_get S30_RUN_ID)"; locale_args; printf "S30_SRC_VERSION='backup'\nS30_ROLE_SEARCH_PATH='%s'\nS30_ROLES=''\n" "$ROLE_SEARCH_PATH"; } > "$DRILL/preflight.env"
  chmod 600 "$DRILL/preflight.env"
  say "preflight.env：locale 取自仓库 compose.db.yaml 缺省（$(grep -E '^S30_(ENCODING|DATCOLLATE|DATLOCPROVIDER|DATLOCALE)=' "$DRILL/preflight.env" | tr '\n' ' ')）；角色骨架留给 roles 步"
  switch install-pg
  local rec; rec="$(record_container "$DB_CONTAINER")"
  state_set RTS_DB_STARTED "${rec%% *}"; rec="${rec#* }"; state_set RTS_DB_HEALTHY "${rec% *}"; state_set RTS_DB_HEALTHY_MARK "${rec#* }"
  say "[docker] $DB_CONTAINER StartedAt=$(state_get RTS_DB_STARTED) 首次 healthy=$(state_get RTS_DB_HEALTHY)（$(state_get RTS_DB_HEALTHY_MARK)）"
}
step_roles() {
  local roles r created=0 kept=0 builtin=0
  roles="$(dump_sql | helper roles)" || die "从 dump 派生角色失败"
  [[ -n "$roles" ]] || die "dump 里读不到任何角色引用（dump 不是 pg_dump 产物？）"
  while IFS= read -r r; do
    local q="${r//\'/\'\'}" ident="\"${r//\"/\"\"}\""
    if [[ "$(tgt_sql "SELECT count(*) FROM pg_roles WHERE rolname = '$q'")" == 1 ]]; then
      if [[ "$r" == pg_* ]]; then builtin=$((builtin + 1)); else kept=$((kept + 1)); fi
    else
      [[ "$r" != pg_* ]] || die "dump 引用的内建角色 $r 在目标不存在（目标大版本不对？）"
      tgt_sql "CREATE ROLE $ident NOLOGIN" >/dev/null; created=$((created + 1))
    fi
  done <<< "$roles"
  say "[角色骨架] 从 dump 引用派生 $(wc -l <<< "$roles") 个：新建 NOLOGIN $created、已在位 $kept、内建 $builtin；清单：$(tr '\n' ' ' <<< "$roles")"
}
step_restore() {
  local objects removed schemas exts owner
  objects="$(tgt_sql "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p','S','v','m')) + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public')")"
  [[ "$objects" == 0 ]] || die "目标 public 已有 $objects 个对象，拒绝恢复到非空库（半成品：rollback --yes 后重来）"
  schemas="$(tgt_sql "SELECT string_agg(nspname, ',') FROM pg_namespace")"; exts="$(tgt_sql "SELECT string_agg(extname, ',') FROM pg_extension")"
  "$DOCKER" exec -i "$DB_CONTAINER" pg_restore -l < "$DUMP" > "$DRILL/dump.toc" || die "pg_restore -l 读不出 dump 目录"
  removed="$(helper tocfilter "$schemas" "$exts" "$DRILL/restore.toc" < "$DRILL/dump.toc")"
  say "[toc] 去掉目标已在位的创建条目：$(tr '\n' ';' <<< "${removed:-无}")（其余 $(grep -cvE '^;|^$' "$DRILL/restore.toc") 条原序保留）"
  "$DOCKER" cp "$DRILL/restore.toc" "$DB_CONTAINER:/tmp/rts-restore.toc"
  step_say "pg_restore --exit-on-error（不带 --no-owner / --no-privileges）…"
  if ! "$DOCKER" exec -i "$DB_CONTAINER" pg_restore -U postgres -d postgres --exit-on-error -L /tmp/rts-restore.toc < "$DUMP" 2> "$DRILL/restore.log"; then
    sed 's/^/  /' "$DRILL/restore.log" | head -n 8; die "pg_restore 失败（现场保留：$DRILL/restore.log）"
  fi
  "$DOCKER" exec "$DB_CONTAINER" rm -f /tmp/rts-restore.toc
  local kind sname
  while read -r kind sname owner; do [[ "$kind" == SCHEMA ]] || continue  # SCHEMA <名> <属主>
    if [[ "$(tgt_sql "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = '$sname'")" != "$owner" ]]; then
      tgt_sql "ALTER SCHEMA \"$sname\" OWNER TO \"$owner\"" >/dev/null; say "[toc] schema $sname 属主改为 dump 记录的 $owner"; fi
  done <<< "$removed"
  say "restore 完成：public 表 $(tgt_sql "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p')")、alembic $(tgt_sql 'SELECT version_num FROM public.alembic_version LIMIT 1')"
}
TARGET_SQL=$(cat <<'SQL'
WITH t AS (
  SELECT c.relname AS name, (xpath('/row/cnt/text()', query_to_xml(format('SELECT count(*) AS cnt FROM %I.%I', n.nspname, c.relname), false, true, '')))[1]::text::bigint AS cnt
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
), rel AS (SELECT c.relname, c.relowner, c.relacl FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'v', 'm')),
fn AS (SELECT p.proname, p.proowner, p.proacl, p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public')
SELECT json_build_object(
  'alembic_head', (SELECT version_num FROM public.alembic_version LIMIT 1),
  'tables', coalesce((SELECT json_object_agg('public.' || name, cnt ORDER BY name) FROM t), '{}'::json),
  'sequences', coalesce((SELECT json_object_agg('public.' || sequencename, last_value ORDER BY sequencename) FROM pg_sequences WHERE schemaname = 'public'), '{}'::json),
  'extensions', coalesce((SELECT json_agg(extname || '@' || extversion ORDER BY extname) FROM pg_extension), '[]'::json),
  'rel_owners', coalesce((SELECT json_object_agg(relname, relowner::regrole::text) FROM rel), '{}'::json),
  'rel_acl', coalesce((SELECT json_agg(relname) FROM rel WHERE relacl IS NOT NULL), '[]'::json),
  'func_owners', coalesce((SELECT json_object_agg(proname, o) FROM (SELECT proname, json_agg(proowner::regrole::text ORDER BY proowner::regrole::text) AS o FROM fn GROUP BY proname) f), '{}'::json),
  'func_acl', coalesce((SELECT json_agg(DISTINCT proname) FROM fn WHERE proacl IS NOT NULL), '[]'::json),
  'schema_owners', coalesce((SELECT json_object_agg(nspname, nspowner::regrole::text) FROM pg_namespace), '{}'::json),
  'schema_acl', coalesce((SELECT json_agg(nspname) FROM pg_namespace WHERE nspacl IS NOT NULL), '[]'::json),
  'retention_cleanup', (SELECT json_build_object('owner', proowner::regrole::text, 'prosecdef', prosecdef) FROM fn WHERE proname = 'lingxi_retention_cleanup' LIMIT 1)
)::text
SQL
)
step_verify() {
  [[ -f "$DRILL/dump.toc" ]] || "$DOCKER" exec -i "$DB_CONTAINER" pg_restore -l < "$DUMP" > "$DRILL/dump.toc"
  tgt_sql "$TARGET_SQL" > "$DRILL/target.json"
  local out rc=0; out="$(helper verify "$DUMP.counts.json" "$DRILL/dump.toc" "$DRILL/target.json")" || rc=$?
  printf '%s\n' "$out" | sed -n 's/^SUMMARY /核对项：/p' | sed 's/^/rts /'
  if (( rc != 0 )); then
    say "差异 $(grep -c '^DIFF ' <<< "$out") 项（段|键|清单|目标）："; grep '^DIFF ' <<< "$out" | sed 's/^DIFF /  /'
    die "verify 未通过，不得进入 dsn（清单与 dump 不是同一快照时，差异应只落在那段时间有写入的表上；先核对再决定）"
  fi
  say "verify 零差异（alembic / 逐表行数 / 序列 / 扩展 / TOC 属主 / ACL / 清理函数 SECURITY DEFINER）"
}
DSN_RE='^(LINGXI_[A-Z_]*_DSN=)(["'"'"']?)(postgres(ql)?(\+psycopg)?)://([^:@/[:space:]]+):([^@[:space:]]*)@([^/?"'"'"'[:space:]]+)/([^?"'"'"'[:space:]]*)(\?[^"'"'"'[:space:]]*)?(["'"'"']?)[[:space:]]*$'
# 查询串里带这些键会经 libpq 覆盖 authority 里的主机 / 账号 / 库（service / passfile 经服务文件 / 口令文件间接覆盖）：
# 与 db_switch_to_local.sh 的 PARAM_OVERRIDE_RE 同义，命中即按「指向别处」拒绝
PARAM_OVERRIDE_RE='[?&](host|hostaddr|user|password|dbname|port|service|passfile)='
dsn_scan() { # $1 文件 $2 期望主机:端口（空格分隔多个） $3 新口令 $4 dry|write → DSN_RESULT
  local file="$1" want=" $2 " pw="$3" mode="$4" line out="" n=0 stale=0 bad="" params
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^LINGXI_[A-Z_]*_DSN= ]]; then
      n=$((n + 1))
      if [[ "$line" =~ $DSN_RE ]]; then
        local user="${BASH_REMATCH[6]}" oldpw="${BASH_REMATCH[7]}" host="${BASH_REMATCH[8]}" db="${BASH_REMATCH[9]}"
        local head="${BASH_REMATCH[1]}${BASH_REMATCH[2]}${BASH_REMATCH[3]}" tail="${BASH_REMATCH[11]}"; params="${BASH_REMATCH[10]}"
        if [[ "$want" != *" $host "* || "$user" != postgres || "$db" != postgres ]]; then bad="第 $n 条 DSN 不指向本地库 postgres@{$2}/postgres"
        elif [[ "${params,,}" =~ $PARAM_OVERRIDE_RE ]]; then bad="第 $n 条 DSN 的查询参数会覆盖连接目标（host / dbname 等）"
        elif [[ "$oldpw" != "$pw" ]]; then stale=$((stale + 1))
          line="${head}://${user}:${pw}@${host}/${db}${params}${tail}"; fi
      else bad="第 $n 条 DSN 形状不可解析"; fi
    fi
    out+="$line"$'\n'
  done < "$file"
  [[ -z "$bad" ]] || { DSN_RESULT="bad($bad)"; return; }
  (( n > 0 )) || { DSN_RESULT=no_dsn_line; return; }
  (( stale > 0 )) || { DSN_RESULT="already_in_place($n 条)"; return; }
  [[ "$mode" == write ]] || { DSN_RESULT="stale($stale/$n 条)"; return; }
  install -d -m 700 -- "$DRILL/env-backup"
  [[ -e "$DRILL/env-backup/$(basename -- "$file")" ]] || cp -p -- "$file" "$DRILL/env-backup/$(basename -- "$file")"
  printf '%s' "$out" > "$file.tmp-rts"; chmod --reference="$file" "$file.tmp-rts"; mv -f -- "$file.tmp-rts" "$file"
  DSN_RESULT="rewritten($stale/$n 条，原件备份在 $DRILL/env-backup/)"
}
step_dsn() {
  local pw f want bad="" pass; pw="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$DB_CONFIG_DIR/.env.db" | tail -n1)"; [[ -n "$pw" ]] || die "读不到 $DB_CONFIG_DIR/.env.db 的口令"
  for pass in dry write check; do
    for f in "${ENV_FILES[@]}"; do
      [[ -f "$f" ]] || die "env 文件不存在：$f"
      if [[ "$f" == "$MONITOR_ENV" ]]; then want="127.0.0.1:$DB_HOST_PORT localhost:$DB_HOST_PORT"; else want="$DB_CONTAINER:5432"; fi
      dsn_scan "$f" "$want" "$pw" "$([[ $pass == write ]] && echo write || echo dry)"
      case "$pass:$DSN_RESULT" in
        dry:bad*) bad+="$(basename -- "$f")=$DSN_RESULT " ;;
        write:*) say "[$(basename -- "$f")] $DSN_RESULT" ;;
        check:stale*|check:bad*) die "回读：$(basename -- "$f") 仍 $DSN_RESULT" ;;
      esac
    done
    [[ $pass != dry || -z "$bad" ]] || die "连接串核对不符、未改任何文件：${bad}（恢复只换口令、不改连接目标）"
  done
  say "连接串核对：八份文件的 DSN 全部指向本地库、口令与新 .env.db 一致（值不打印）"
}
step_services() {
  switch recreate-services
  local svc rec started=0 healthy=0 s h t mark=exact
  for svc in "${APP_SERVICES[@]}"; do
    rec="$(record_container "$PROJECT-$svc-1")"; s="${rec%% *}"; rec="${rec#* }"; h="${rec% *}"; [[ "${rec#* }" == exact ]] || mark="${rec#* }"
    say "[docker] $PROJECT-$svc-1 StartedAt=$s 首次 healthy=$h"
    t="$(date -u -d "$s" +%s 2>/dev/null || echo 0)"; (( t > started )) && { started=$t; state_set RTS_SVC_STARTED "$s"; }
    t="$(date -u -d "$h" +%s 2>/dev/null || echo 0)"; (( t > healthy )) && { healthy=$t; state_set RTS_SVC_HEALTHY "$h"; }
  done
  state_set RTS_SVC_HEALTHY_MARK "$mark"
}
step_timers() {
  start_timers
  local ts; ts="$("$SYSTEMCTL" show -p ActiveEnterTimestamp --value "$PULL_TIMER" || true)"
  state_set RTS_TIMERS_ACTIVE "$(iso_of "${ts:-unknown}")"
}

print_timing() {
  local rows=(
    "RTS_RUN_START_AT|恢复开始（run 首次启动）|脚本时钟"
    "RTS_DB_STARTED|本地库容器启动|docker StartedAt"
    "RTS_DB_HEALTHY|本地库首次 healthy|docker 健康日志"
    "RTS_T_host|主机侧重建完成（install-pg 回读判据通过）|脚本时钟"
    "RTS_T_roles|角色骨架就位|脚本时钟"
    "RTS_T_restore|restore 完成|脚本时钟"
    "RTS_T_verify|核对零差异|脚本时钟"
    "RTS_T_dsn|连接串改写与回读完成|脚本时钟"
    "RTS_SVC_STARTED|三服务最晚一个启动|docker StartedAt"
    "RTS_SVC_HEALTHY|三服务最晚一个首次 healthy|docker 健康日志"
    "RTS_TIMERS_ACTIVE|拉取 timer 重新激活|systemd ActiveEnterTimestamp")
  local row key label src v t prev="" first="" last="" svc_h
  say "分段时长表（轮次 $(basename -- "$DRILL")；时刻 UTC；距上一行 / 距开始，秒）"
  printf 'rts   %-44s %-22s %-24s %8s %8s\n' 时刻点 UTC 依据 段 累计
  for row in "${rows[@]}"; do IFS='|' read -r key label src <<< "$row"; v="$(state_get "$key")"
    if [[ -z "$v" || "$v" == unknown ]]; then printf 'rts   %-44s %-22s %-24s %8s %8s\n' "$label" "${v:-未到}" "$src" - -; continue; fi
    t="$(date -u -d "$v" +%s)"; [[ -n "$first" ]] || first=$t
    printf 'rts   %-44s %-22s %-24s %8s %8s\n' "$label" "$v" "$src" "$([[ -n "$prev" ]] && echo $((t - prev)) || echo 0)" "$((t - first))"
    prev=$t; last=$t
  done
  [[ "$(state_get RTS_DB_HEALTHY_MARK)$(state_get RTS_SVC_HEALTHY_MARK)" != *rolled* ]] || say "注：标 rolled 的 healthy 时刻取自已滚动的健康日志，是上界"
  svc_h="$(state_get RTS_SVC_HEALTHY)"
  if [[ -n "$first" && -n "$svc_h" && "$svc_h" != unknown ]]; then
    say "RTO（恢复开始 → 三服务全部 healthy，不含人工首次问数）= $(( $(date -u -d "$svc_h" +%s) - first )) 秒；首次问数时刻由执行人记录后补进留痕"
  elif [[ -n "$first" ]]; then say "尚未到三服务 healthy：最后一个记录点距开始 $((last - first)) 秒"; fi
}

do_run() {
  DUMP="${1:-}"; shift || true
  local from=host until=timers a
  for a in "$@"; do case "$a" in --from=*) from="${a#--from=}" ;; --until=*) until="${a#--until=}" ;; *) die "run 不认识的参数：$a" ;; esac; done
  [[ " ${STEPS[*]} " == *" $from "* && " ${STEPS[*]} " == *" $until "* ]] || die "--from / --until 只认：${STEPS[*]}"
  check_dump
  if drill_open; then say "沿用进行中的轮次 $DRILL"; else new_drill; fi
  [[ -n "$(state_get RTS_RUN_START_AT)" ]] || state_set RTS_RUN_START_AT "$(now_utc)"
  state_set RTS_DUMP "$(basename -- "$DUMP")"
  local s on=0
  for s in "${STEPS[@]}"; do
    [[ "$s" == "$from" ]] && on=1
    (( on )) || continue
    step_say "== 步骤 $s 开始"
    "step_$s"
    state_set "RTS_T_$s" "$(now_utc)"; step_say "== 步骤 $s 完成"
    [[ "$s" == "$until" ]] && break
  done
  if [[ "$s" == timers ]]; then state_set RTS_RUN_DONE_AT "$(now_utc)"; say "恢复完成：下一步由执行人在 Bot-Test 发一条真实问数，记下首次正确回答的时刻"; fi
  print_timing
}

# ============================ rollback ============================
do_rollback() {
  need_yes "${1:-}"; need_stage
  say "== rollback：可逆（本次新建的库与目录移进 failed-run/，不删）；回退 = 再 wipe"
  [[ -n "$DRILL" && -f "$DRILL/wiped/manifest.tsv" ]] || die "没有可回退的轮次（缺 wiped/manifest.tsv）"
  [[ -z "$(state_get RTS_ROLLBACK_AT)" ]] || die "本轮已回退过（$(state_get RTS_ROLLBACK_AT)）"
  [[ -z "$(state_get RTS_RUN_DONE_AT)" ]] || say "注意：本轮 run 已完成，回退会换回 wipe 前的原库（恢复出的库移进 failed-run/）"
  local name path fr="$DRILL/failed-run"
  while IFS=$'\t' read -r name path; do guard_move_target "$path"; done < "$DRILL/wiped/manifest.tsv"
  step_say "停三服务与 timer（db_switch stop-write）"
  switch stop-write
  "$SYSTEMCTL" stop "$BACKUP_TIMER" || true
  if [[ "$(db_state "$DB_CONTAINER")" != absent ]]; then
    if [[ -f "$DB_CONFIG_DIR/compose.env" && -f "$DB_INSTALL_DIR/compose.db.yaml" ]]; then compose_db down > "$DRILL/rollback-down.log" 2>&1 || die "本次新建的库 compose down 失败：见 $DRILL/rollback-down.log"
    else "$DOCKER" rm -f "$DB_CONTAINER" >/dev/null || die "删除本次新建的库容器失败：$DB_CONTAINER"; fi
    say "[容器] 本次新建的 $DB_CONTAINER 已下线（数据目录移进 failed-run/，不删）"
  fi
  install -d -m 700 -- "$fr"
  while IFS=$'\t' read -r name path; do
    if [[ -e "$path" ]]; then mv -- "$path" "$fr/$name"; say "[移开] 本次新建 $path → $fr/$name"; fi
    mv -- "$DRILL/wiped/$name" "$path"; say "[移回] $DRILL/wiped/$name → $path"
  done < "$DRILL/wiped/manifest.tsv"
  if [[ -s "$DRILL/wiped/hosts.lines" && "$(hosts_count)" == 0 ]]; then cat "$DRILL/wiped/hosts.lines" >> "$HOSTS_FILE"; fi
  say "[hosts] 匹配行 $(hosts_count)"
  local f b restored=0
  if [[ -d "$DRILL/env-backup" ]]; then
    for f in "${ENV_FILES[@]}"; do b="$DRILL/env-backup/$(basename -- "$f")"; [[ -f "$b" ]] || continue
      cp -p -- "$b" "$f.tmp-rts"; mv -f -- "$f.tmp-rts" "$f"; cmp -s -- "$b" "$f" || die "$f 还原后与备份不一致"; restored=$((restored + 1)); done
  fi
  say "[env] 逐字节还原 $restored 份"
  compose_db up -d > "$DRILL/rollback-up.log" 2>&1 || die "原库 compose up 失败：见 $DRILL/rollback-up.log"
  local n=0; until [[ "$(db_state "$DB_CONTAINER")" == healthy ]]; do (( n++ < 36 )) || die "原库 180 秒内未 healthy"; sleep 5; done
  say "[容器] 原库 $DB_CONTAINER healthy"
  switch recreate-services
  start_timers
  state_set RTS_ROLLBACK_AT "$(now_utc)"; step_say "rollback 完成：原库、原配置、三服务已回到 wipe 前；failed-run/ 与轮次目录留待编排者清理"
}

# ============================ roles / timing / status ============================
do_roles() { DUMP="${1:-}"; [[ -f "$DUMP" ]] || die "dump 不存在：${DUMP:-未给}"
  local roles; roles="$(dump_sql | helper roles)" || die "从 dump 派生角色失败"
  say "从 dump 引用派生的角色 $(wc -l <<< "$roles") 个：$(tr '\n' ' ' <<< "$roles")"; }
do_status() {
  say "轮次 ${DRILL:-无}；$( [[ -n "$DRILL" && -f "$DRILL/state.env" ]] && grep '^RTS_' "$DRILL/state.env" | tr '\n' ' ')"
  say "本地库 $DB_CONTAINER=$(db_state "$DB_CONTAINER")；数据目录 $( [[ -e "$DB_DATA_DIR" ]] && echo present || echo absent)；口令文件 $( [[ -e "$DB_CONFIG_DIR/.env.db" ]] && echo present || echo absent)；hosts 行 $(hosts_count)"
}

case "$SUB" in
  wipe) do_wipe "$@" ;; run) do_run "$@" ;; rollback) do_rollback "$@" ;; roles) do_roles "$@" ;;
  timing) [[ -n "$DRILL" ]] || die "没有轮次"; print_timing ;; status) do_status ;;
  *) die "未知子命令 $SUB" ;;
esac
