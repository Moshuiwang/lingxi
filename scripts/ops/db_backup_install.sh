#!/usr/bin/env bash
# db_backup_install.sh —— 本地库每日备份单元的宿主安装脚本。来源：Issue #809 定稿、2026-09-22 生产实跑的那一版
# （原名 s31_db_backup_install.sh），#896 脱敏入仓；与实跑版的行为差异只有三处，见 #896 留痕：缺省输入文件名、
# 用法打印取段方式、回读判据（下文「回读判据」，#886 第 2 处）。
# 用途：把 deploy/监控告警.md「十、本地数据库」2 / 2.1 / 3 三段手工命令做成幂等脚本：装 db_backup.sh + 两只 systemd
# 单元 + db-backup.env + 备份目录，探通异机通道，给宿主巡检打开本地库检查项。
# 不动 lingxi-db 容器、不动任何应用单元；对库的读写全部在 db_backup.sh（经 docker exec，只读）里。
# 适用场景：生产（2026-09-22 已装）/ 预发（#896 与生产同构）/ 恢复演练（只用 status / run-once 取一份新备份）。
# 取用：脚本与来源副本一律从仓库同一提交取（不得从个人目录、scratchpad 或聊天记录取）；把本脚本、scripts/ops/db_backup.sh、
# scripts/ops/host_health_alert.py、deploy/monitoring-units/lingxi-db-backup.{service,timer} 与输入文件
# db_backup_install.env（样例 deploy/db_backup_install.env.example）放进宿主同一个 0700 目录再跑。
# 不可逆步骤：无——覆盖在位文件前一律 cp -p 进 BACKUP_ROOT，rollback 还原；备份目录与 dump 永不删除。
# 停止条件：任一「前置不满足」「错误：」行即停，脚本已 die 且未做后续改动；apply 回读 sha 不等 → 立即 rollback。
# 回读判据（#886 第 2 处）：脚本 / 两只单元 / 巡检脚本是「输入副本」，在位 sha 须等于输入文件里声明的 LINGXI_S31_*_SHA
# （= 仓库该文件的 sha256sum）；db-backup.env 与巡检 drop-in 是「现场生成」，其 sha 必然不同于输入文件的 sha，
# 判据改为「在位 sha = 本次 apply / monitor-enable 打印的 installed 或 already_in_place 值」——两类逐行打在「[回读判据]」行。
# 用法：sudo -n bash db_backup_install.sh <子命令>                                            到终态预计时长
#   dry               只读：逐项打印现状 vs 期望（sha / mode / owner），不写任何文件               < 5 秒
#   apply             幂等：装脚本 + 两只单元 + db-backup.env + 备份目录 → daemon-reload → enable --now timer；
#                     已相等项打印 already_in_place；覆盖在位文件前先 cp -p 到 BACKUP_ROOT；不跑首轮     < 10 秒
#   run-once          systemctl start 一轮并等结束 → 回读 Result / ExecMainStatus / 状态文件全文 / 三件 / sha256sum -c
#                                                                                              < 60 秒（27–120 MB 库）
#   probe-remote      只读：以当前用户 ssh -o BatchMode=yes … <REMOTE 的 user@host> true → reachable / unreachable
#   ensure-remote-dir ssh 'install -d -m 700 <远端目录>'，幂等
#   monitor-script    把同目录 host_health_alert.py（仓库版，含 --db-* 五项）装到 MONITOR_SCRIPT_DST（在位旧版若
#                     不认识 --db-*，先装它再 monitor-enable）；覆盖前 cp -p 到 BACKUP_ROOT；已相等 already_in_place   < 5 秒
#   monitor-enable    自检在位巡检脚本支持 --db-*（否则拒绝）→ 新建 drop-in：ExecStart= 清空 + 从当前有效 ExecStart 派生并
#                     追加 --db-* 五项 → daemon-reload → 回读 show -p ExecStart 含 --db-container；已相等 already_in_place < 5 秒
#   monitor-disable   删该 drop-in → daemon-reload → 回读不含 --db-container
#   rollback          disable --now timer → 删两只单元 → daemon-reload → 删脚本 → 删 db-backup.env → monitor-disable → 巡检脚本
#                     只还原不删（BACKUP_ROOT 有副本则还原，没有则 kept：它可能是 s29 装的）；其余有旧副本的文件也还原而不是删；
#                     备份目录及其 dump、状态文件一律保留不删
#   status            只读汇总；cleanup 只删本脚本在备份目录外留下的临时物（*.tmp-s31 / .s31.*），没有就打印 nothing
# 输入全走 LINGXI_S31_* 环境变量，缺省从同目录 db_backup_install.env 读（KEY=VALUE，不执行；只认 LINGXI_S31_[A-Z_]+）；清单见文件末尾。
# 脱敏：REMOTE（user@host:path）只写进 db-backup.env；所有输出只用 REMOTE_LABEL 代替，ssh / scp 的错误文本经 mask。
# 测试形：非 root + LINGXI_S31_ROOT=<假根>：所有路径加前缀、跳过属主设置、systemctl / ssh / scp 走桩（桩目录进 PATH，
# 让 db_backup.sh 里裸调用的 ssh / scp 也走桩）；run-once 直接 bash <假根>/opt/lingxi/scripts/db_backup.sh 并把生成的
# env 文件里的 LINGXI_DB_BACKUP_* 注入（等价 systemd EnvironmentFile）。
set -euo pipefail
export LC_ALL=C
umask 077  # 落盘缺省私有；需要 0644 / 0755 的地方显式 install -m

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUB="${1:-}"
[[ -n "$SUB" ]] || { sed -n '/^# 用法：/,/^#   status /p' "${BASH_SOURCE[0]}" >&2; exit 2; }

say() { printf 's31 %s\n' "$*"; }
die() { printf 's31 错误：%s\n' "$*" >&2; exit 1; }
sha_of() { if [[ -f "$1" ]]; then sha256sum -- "$1" | cut -d' ' -f1; else echo absent; fi; }
sha_pipe() { sha256sum | cut -d' ' -f1; }
stamp() { date -u +%Y%m%dT%H%M%SZ; }
plan() { if [[ "$1" == "$2" ]]; then echo already_in_place; else echo "$3"; fi; }
show_file() { # path 名称
  if [[ -e "$1" ]]; then say "  [$2] $1 sha=$(sha_of "$1") mode=$(stat -c %a -- "$1") owner=$(stat -c %U:%G -- "$1")"
  else say "  [$2] $1 absent"; fi; }

# --- 读输入文件（缺省 db_backup_install.env）：只接受 LINGXI_S31_* 的 KEY=VALUE 行，环境里已设的值优先 ---
ENV_FILE="${LINGXI_S31_ENV_FILE:-$SELF_DIR/db_backup_install.env}"
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"; line="${line#"${line%%[![:space:]]*}"}"
    [[ "$line" =~ ^(LINGXI_S31_[A-Z_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"; val="${val%\"}"; val="${val#\"}"
    [[ -n "${!key:-}" ]] || export "$key=$val"
  done < "$ENV_FILE"
fi

ROOT="${LINGXI_S31_ROOT:-}"
SYSTEMCTL="${LINGXI_S31_SYSTEMCTL:-systemctl}"; SSH="${LINGXI_S31_SSH:-ssh}"; SCP="${LINGXI_S31_SCP:-scp}"
SCRIPT_SRC="${LINGXI_S31_SCRIPT_SRC:-$SELF_DIR/db_backup.sh}"; SCRIPT_SHA="${LINGXI_S31_SCRIPT_SHA:-}"
UNIT_SRC_DIR="${LINGXI_S31_UNIT_SRC_DIR:-$SELF_DIR}"; SERVICE_SHA="${LINGXI_S31_SERVICE_SHA:-}"; TIMER_SHA="${LINGXI_S31_TIMER_SHA:-}"
SCRIPT_DST="$ROOT${LINGXI_S31_SCRIPT_DST:-/opt/lingxi/scripts/db_backup.sh}"
UNIT_DIR="$ROOT${LINGXI_S31_UNIT_DIR:-/etc/systemd/system}"
ENV_DST="$ROOT${LINGXI_S31_ENV_DST:-/opt/lingxi/monitoring/db-backup.env}"
BACKUP_DIR="$ROOT${LINGXI_S31_BACKUP_DIR:-/var/lib/lingxi/backups}"
STATUS_FILE="$ROOT${LINGXI_S31_STATUS_FILE:-/var/lib/lingxi/db-backup-status.json}"
CONTAINER="${LINGXI_S31_CONTAINER:-lingxi-db}"; KEEP="${LINGXI_S31_KEEP:-14}"
TRANSFER="${LINGXI_S31_TRANSFER:-none}"; REMOTE="${LINGXI_S31_REMOTE:-}"; REMOTE_LABEL="${LINGXI_S31_REMOTE_LABEL:-offsite}"
SSH_OPTS_RAW="${LINGXI_S31_SSH_OPTS:--o BatchMode=yes -o ConnectTimeout=20}"
MONITOR_UNIT="${LINGXI_S31_MONITOR_UNIT:-lingxi-host-monitor.service}"
MONITOR_DROPIN="${LINGXI_S31_MONITOR_DROPIN:-30-lingxi-db-checks.conf}"
MONITOR_SCRIPT_SRC="${LINGXI_S31_MONITOR_SCRIPT_SRC:-$SELF_DIR/host_health_alert.py}"; MONITOR_SCRIPT_SHA="${LINGXI_S31_MONITOR_SCRIPT_SHA:-}"
MONITOR_SCRIPT_DST="$ROOT${LINGXI_S31_MONITOR_SCRIPT_DST:-/opt/lingxi/scripts/host_health_alert.py}"
MON_MAX_AGE_HOURS="${LINGXI_S31_MON_MAX_AGE_HOURS:-26}"; MON_CONN_PERCENT="${LINGXI_S31_MON_CONN_PERCENT:-80}"
MON_WAL_BYTES="${LINGXI_S31_MON_WAL_BYTES:-2147483648}"
BACKUP_ROOT="$ROOT${LINGXI_S31_BACKUP_ROOT:-/root/lingxi-s31-backup}"
RUN_TIMEOUT="${LINGXI_S31_RUN_TIMEOUT:-1800}"
SERVICE=lingxi-db-backup.service; TIMER=lingxi-db-backup.timer
SERVICE_SRC="$UNIT_SRC_DIR/$SERVICE"; TIMER_SRC="$UNIT_SRC_DIR/$TIMER"
SERVICE_DST="$UNIT_DIR/$SERVICE"; TIMER_DST="$UNIT_DIR/$TIMER"
DROPIN_DIR="$UNIT_DIR/$MONITOR_UNIT.d"; DROPIN_DST="$DROPIN_DIR/$MONITOR_DROPIN"
# 状态文件所在目录（与备份目录的父目录，常为同一个 /var/lib/lingxi）：巡检以部署用户读状态文件，须 o+rx（十.2「0755」）；
# s30 preflight 在 umask 077 下可能已把它建成 root 0700，apply 只改 mode 不改属主
STATUS_DIRS=("$(dirname -- "$STATUS_FILE")")
[[ "$(dirname -- "$BACKUP_DIR")" == "${STATUS_DIRS[0]}" ]] || STATUS_DIRS+=("$(dirname -- "$BACKUP_DIR")")
others_rx() { local m; m="$(stat -c %a -- "$1")"; (( (8#$m & 5) == 5 )); }
read -r -a SSH_OPTS <<< "$SSH_OPTS_RAW"; SSH_OPTS+=(-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR)

# --- 运行身份：root 为正式形；非 root 且设了 ROOT 假根为测试形（跳过属主设置、桩目录进 PATH） ---
OWNER_ARGS=(-o root -g root); TEST_FORM=0; WANT_OWNER=root
if [[ "$(id -u)" -ne 0 ]]; then
  [[ -n "$ROOT" ]] || die "必须以 root 运行（sudo -n bash $0 $SUB）；假根目录测试请设 LINGXI_S31_ROOT"
  OWNER_ARGS=(); TEST_FORM=1; WANT_OWNER="$(id -un)"
  say "测试形：非 root + LINGXI_S31_ROOT=$ROOT，跳过属主设置；systemctl=$SYSTEMCTL ssh=$SSH scp=$SCP"
  for t in "$SSH" "$SCP" "$SYSTEMCTL"; do [[ "$t" == */* ]] && PATH="$(dirname -- "$t"):$PATH"; done; export PATH
fi

mask() { # 远端地址整串 / user@host / host / 路径 → <remote>；连接串口令段 → <creds>（与 db_backup.sh 同口径）
  local text="$1" seg userhost="${REMOTE%%:*}"
  if [[ -n "$REMOTE" ]]; then for seg in "$REMOTE" "$userhost" "${userhost#*@}" "${REMOTE#*:}"; do
    [[ ${#seg} -ge 2 ]] && text="${text//"$seg"/<remote>}"; done; fi
  printf '%s' "$text" | sed -E 's#://[^@[:space:]]*@#://<creds>@#g'; }
one_line() { tr '\n' ' ' | cut -c1-240; }

# --- 输入格式校验（纯字符串检查，不碰任何文件；任何输出不含 REMOTE 值） ---
[[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -ge 1 ]] || die "LINGXI_S31_KEEP 须为正整数"
[[ "$TRANSFER" == none || "$TRANSFER" == scp ]] || die "LINGXI_S31_TRANSFER 只接受 none / scp"
[[ "$TRANSFER" == none || -n "$REMOTE" ]] || die "scp 模式须设 LINGXI_S31_REMOTE=user@host:/abs/or/~/path"
[[ -z "$REMOTE" || "$REMOTE" =~ ^[^:@[:space:]]+@[^:[:space:]]+:(/|~)[^[:space:]]*$ ]] || die "LINGXI_S31_REMOTE 须形如 user@host:/abs/or/~/path（值不回显）"
[[ "$REMOTE_LABEL" =~ ^[A-Za-z0-9_.-]{1,32}$ ]] || die "LINGXI_S31_REMOTE_LABEL 只接受 1–32 位字母数字 ._-"
[[ "$CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || die "LINGXI_S31_CONTAINER 含非法字符"
[[ "$MON_MAX_AGE_HOURS" =~ ^[0-9]+(\.[0-9]+)?$ && "$MON_CONN_PERCENT" =~ ^[0-9]+(\.[0-9]+)?$ && "$MON_WAL_BYTES" =~ ^[0-9]+$ ]] \
  || die "LINGXI_S31_MON_MAX_AGE_HOURS / MON_CONN_PERCENT 须为数字，MON_WAL_BYTES 须为整数"
[[ "$MONITOR_UNIT" == *.service && "$MONITOR_DROPIN" == *.conf ]] || die "MONITOR_UNIT 须以 .service、MONITOR_DROPIN 须以 .conf 结尾"
command -v "$SYSTEMCTL" >/dev/null 2>&1 || die "找不到 $SYSTEMCTL"

# --- 来源副本与期望 sha（dry / status 只报告；apply / run-once / monitor-enable 不满足即拒绝） ---
FAIL=()
check_src() { # path 期望sha 名称
  [[ "$2" =~ ^[0-9a-f]{64}$ ]] || { FAIL+=("$3 的期望 sha 缺失或不是 64 位十六进制（LINGXI_S31_*_SHA 必填）"); return; }
  [[ -f "$1" ]] || { FAIL+=("$3 不存在：$1"); return; }
  [[ "$(sha_of "$1")" == "$2" ]] || FAIL+=("$3 sha ≠ 期望 sha：$1 在位 $(sha_of "$1")"); }
resolve() {
  check_src "$SCRIPT_SRC" "$SCRIPT_SHA" 脚本副本; check_src "$SERVICE_SRC" "$SERVICE_SHA" "service 副本"; check_src "$TIMER_SRC" "$TIMER_SHA" "timer 副本"
  check_src "$MONITOR_SCRIPT_SRC" "$MONITOR_SCRIPT_SHA" 巡检脚本副本
  command -v docker >/dev/null 2>&1 || FAIL+=("缺少 docker 命令（db_backup.sh 经 docker exec 备份）"); }
monitor_supports_db() { [[ -f "$MONITOR_SCRIPT_DST" ]] && grep -q -- '--db-container' "$MONITOR_SCRIPT_DST"; }
require_ok() { if ((${#FAIL[@]})); then for f in "${FAIL[@]}"; do say "前置不满足：$f"; done; die "前置不满足，未做任何改动"; fi; }

env_content() { # db-backup.env：只含 LINGXI_DB_BACKUP_*（六键；scp 形另加 REMOTE 与 SSH_OPTS 两键）；内容确定，便于幂等比对
  printf '# 由 s31_db_backup_install.sh apply 生成；只放 LINGXI_DB_BACKUP_*（deploy/监控告警.md 十.2 / 2.1），root 0600\n'
  printf 'LINGXI_DB_BACKUP_CONTAINER=%s\nLINGXI_DB_BACKUP_DIR=%s\nLINGXI_DB_BACKUP_KEEP=%s\nLINGXI_DB_BACKUP_STATUS_FILE=%s\nLINGXI_DB_BACKUP_TRANSFER=%s\nLINGXI_DB_BACKUP_REMOTE_LABEL=%s\n' \
    "$CONTAINER" "$BACKUP_DIR" "$KEEP" "$STATUS_FILE" "$TRANSFER" "$REMOTE_LABEL"
  if [[ "$TRANSFER" == scp ]]; then printf 'LINGXI_DB_BACKUP_REMOTE=%s\nLINGXI_DB_BACKUP_SSH_OPTS=%s\n' "$REMOTE" "$SSH_OPTS_RAW"; fi; }
env_keys() { if [[ -f "$1" ]]; then grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" | tr -d '=' | tr '\n' ' '; else echo absent; fi; }
base_execstart() { # 当前有效 ExecStart：systemctl cat 最后一条非空 ExecStart=（跳过本脚本自己的 drop-in，保证幂等）
  local line cur skip=0 val=""
  while IFS= read -r line; do
    if [[ "$line" == "# /"* ]]; then cur="${line#\# }"; skip=0; [[ "$cur" == */"$MONITOR_UNIT.d/$MONITOR_DROPIN" ]] && skip=1; continue; fi
    (( skip )) && continue
    [[ "$line" =~ ^ExecStart=(.+)$ ]] && val="${BASH_REMATCH[1]}"
  done < <("$SYSTEMCTL" cat "$MONITOR_UNIT" 2>/dev/null || true)
  printf '%s' "$val"; }
dropin_content() { # $1 = 派生基线；先一行空 ExecStart= 清掉再整行重抄（监控告警.md 十.3 写法）
  printf '[Service]\n# 由 s31_db_backup_install.sh monitor-enable 生成：本地库四项检查（deploy/监控告警.md 十.3）；monitor-disable 删除\nExecStart=\n'
  printf 'ExecStart=%s --db-container %s --db-backup-status-file %s --db-backup-max-age-hours %s --db-connections-alert-percent %s --db-wal-alert-bytes %s\n' \
    "$1" "$CONTAINER" "$STATUS_FILE" "$MON_MAX_AGE_HOURS" "$MON_CONN_PERCENT" "$MON_WAL_BYTES"; }
unit_state() { printf '%s %s' "$("$SYSTEMCTL" is-enabled "$1" 2>/dev/null || true)" "$("$SYSTEMCTL" is-active "$1" 2>/dev/null || true)"; }
effective_has_db() { "$SYSTEMCTL" show -p ExecStart "$MONITOR_UNIT" 2>/dev/null | grep -q -- '--db-container'; }

file_line() { # target 期望sha 期望mode 动作词 → "在位 … 期望 … → 动作"
  local cur; cur="$(sha_of "$1")"
  if [[ -f "$1" ]]; then printf '在位 sha=%s mode=%s owner=%s' "$cur" "$(stat -c %a -- "$1")" "$(stat -c %U:%G -- "$1")"; else printf '在位 absent'; fi
  printf ' 期望 sha=%s mode=%s owner=%s → %s' "$2" "$3" "$WANT_OWNER" "$(plan "$cur" "$2" "$4")"; }
report() { # 现状 vs 期望；$1=full 附运行态。只读
  local base dcur
  say "[脚本] $SCRIPT_DST $(file_line "$SCRIPT_DST" "$SCRIPT_SHA" 755 install)"
  say "[service] $SERVICE_DST $(file_line "$SERVICE_DST" "$SERVICE_SHA" 644 install)"
  say "[timer] $TIMER_DST $(file_line "$TIMER_DST" "$TIMER_SHA" 644 install)"
  say "[env] $ENV_DST $(file_line "$ENV_DST" "$(env_content | sha_pipe)" 600 write) 在位键=$(env_keys "$ENV_DST")（值不回显；期望 $(env_content | grep -c '^LINGXI_') 键；现场生成，期望 sha 由当前输入推算，不是输入文件的 sha）"
  if [[ -d "$BACKUP_DIR" ]]; then say "[备份目录] $BACKUP_DIR 在位 mode=$(stat -c %a -- "$BACKUP_DIR") owner=$(stat -c %U -- "$BACKUP_DIR") 期望 700 $WANT_OWNER → $(plan "$(stat -c '%U %a' -- "$BACKUP_DIR")" "$WANT_OWNER 700" fix_by_hand)"
  else say "[备份目录] $BACKUP_DIR 在位 absent 期望 700 $WANT_OWNER → create"; fi
  if [[ -f "$STATUS_FILE" ]]; then say "[状态文件] $STATUS_FILE 在位 $(stat -c %s -- "$STATUS_FILE") B（备份脚本写，本脚本不管理）"; else say "[状态文件] $STATUS_FILE absent（首轮后出现）"; fi
  for d in "${STATUS_DIRS[@]}"; do
    if [[ -d "$d" ]]; then say "[状态文件目录] $d mode=$(stat -c %a -- "$d") owner=$(stat -c %U -- "$d") 期望 o+rx → $(if others_rx "$d"; then echo ok; else echo fix; fi)"
    else say "[状态文件目录] $d absent 期望 755 → create"; fi; done
  say "[timer] $TIMER is-enabled/is-active=$(unit_state "$TIMER") 期望 enabled active"
  say "[巡检脚本] $MONITOR_SCRIPT_DST $(file_line "$MONITOR_SCRIPT_DST" "$MONITOR_SCRIPT_SHA" 755 monitor-script) 在位支持 --db-*：$(if monitor_supports_db; then echo yes; else echo no; fi)"
  base="$(base_execstart)"; dcur="$(sha_of "$DROPIN_DST")"
  if [[ -z "$base" ]]; then say "[巡检 drop-in] $DROPIN_DST 在位 sha=$dcur；派生失败：systemctl cat $MONITOR_UNIT 无非空 ExecStart=（monitor-enable 会拒绝）"
  else say "[巡检 drop-in] $DROPIN_DST 在位 sha=$dcur 期望 sha=$(dropin_content "$base" | sha_pipe) → $(plan "$dcur" "$(dropin_content "$base" | sha_pipe)" monitor-enable)；基线=${base%% *} …"; fi
  if effective_has_db; then say "[巡检] 有效 ExecStart 含 --db-container：yes"; else say "[巡检] 有效 ExecStart 含 --db-container：no"; fi
  say "[BACKUP_ROOT] $BACKUP_ROOT 副本 $(if [[ -d "$BACKUP_ROOT" ]]; then find "$BACKUP_ROOT" -maxdepth 1 -type f | wc -l; else echo 0; fi) 份"
  say "[传输] mode=$TRANSFER label=$REMOTE_LABEL remote=$(if [[ -n "$REMOTE" ]]; then echo set; else echo unset; fi)（值只进 env 文件）"
  if [[ "${1:-}" == full ]]; then
    say "[service] $("$SYSTEMCTL" show -p Result -p ExecMainStatus "$SERVICE" 2>/dev/null | one_line)"
    "$SYSTEMCTL" list-timers "$TIMER" --no-pager 2>/dev/null | sed 's/^/  /' || true
    if [[ -f "$STATUS_FILE" ]]; then say "[状态文件全文] $(cat -- "$STATUS_FILE")"; fi
    if [[ -d "$BACKUP_DIR" ]]; then say "[备份目录] dump 组数=$(find "$BACKUP_DIR" -maxdepth 1 -name 'lingxi-db-*.dump' | wc -l) 最新=$(find "$BACKUP_DIR" -maxdepth 1 -name 'lingxi-db-*.dump' -printf '%f\n' | sort | tail -n1)"; fi
  fi
  if ((${#FAIL[@]})); then for f in "${FAIL[@]}"; do say "前置不满足：$f"; done; return 1; fi
  say "前置全部满足"; }

# --- 落位原语：目录不存在才建；文件 = 临时文件 + install -m + mv -f 原子落位 + 回读 sha；覆盖前副本进 BACKUP_ROOT ---
ensure_dir() { # path mode 名称
  if [[ -d "$1" ]]; then say "[$3] already_in_place $1 mode=$(stat -c %a -- "$1") owner=$(stat -c %U -- "$1")（已存在不动）"
  else install -d -m "$2" "${OWNER_ARGS[@]}" -- "$1"; say "[$3] created $1 mode=$2 owner=$WANT_OWNER"; fi; }
backup_existing() { # 在位文件 cp -p 到 BACKUP_ROOT/<basename>.<UTC 时间戳>
  [[ -f "$1" ]] || return 0
  install -d -m 700 "${OWNER_ARGS[@]}" -- "$BACKUP_ROOT"
  local dst; dst="$BACKUP_ROOT/$(basename -- "$1").$(stamp)"; cp -p -- "$1" "$dst"; say "  在位文件先备份 → $dst"; }
place() { # src target mode 期望sha 名称
  local src="$1" target="$2" mode="$3" want="$4" name="$5" cur; cur="$(sha_of "$target")"
  if [[ "$cur" == "$want" ]]; then
    say "[$name] already_in_place sha=$cur"
    [[ "$(stat -c %a -- "$target")" == "$mode" ]] || { chmod "$mode" -- "$target"; say "[$name] mode 修正 → $mode"; }; return; fi
  backup_existing "$target"
  install -m "$mode" "${OWNER_ARGS[@]}" -- "$src" "$target.tmp-s31"; mv -f -- "$target.tmp-s31" "$target"
  [[ "$(sha_of "$target")" == "$want" ]] || die "[$name] 回读 sha ≠ 期望，请立即 rollback"
  say "[$name] installed sha=$want mode=$mode（原状 $cur）"; }
place_gen() { # 内容经 stdin；target mode 期望sha 名称
  local tmp; tmp="$(mktemp -- "$(dirname -- "$1")/.s31.XXXXXX")"; cat > "$tmp"; place "$tmp" "$1" "$2" "$3" "$4"; rm -f -- "$tmp"; }

# --- 回读判据（#886 第 2 处）：输入副本比输入文件声明的 sha；现场生成的内容（db-backup.env / 巡检 drop-in）比本次
#     打印的 installed / already_in_place 值，不与输入文件的 sha 比对（后者必然不同，照它核必假红，且停在改完之后） ---
RB_FAIL=0
rb_input() { # target 声明sha 名称
  local cur; cur="$(sha_of "$1")"
  if [[ "$cur" == "$2" ]]; then say "[回读判据] $3 输入副本：在位 sha=$cur = 输入文件声明的 sha → ok"
  else say "[回读判据] $3 输入副本：在位 sha=$cur ≠ 输入文件声明的 sha=$2 → 不符"; RB_FAIL=1; fi; }
rb_generated() { # target 本次打印的sha 名称
  local cur; cur="$(sha_of "$1")"
  if [[ "$cur" == "$2" ]]; then say "[回读判据] $3 现场生成：在位 sha=$cur = 本次打印的 installed / already_in_place 值（不与输入文件 sha 比对）→ ok"
  else say "[回读判据] $3 现场生成：在位 sha=$cur ≠ 本次打印的值 $2 → 不符"; RB_FAIL=1; fi; }
rb_verdict() { (( RB_FAIL == 0 )) || die "回读判据不符（见上方「[回读判据] … 不符」行），请立即 rollback"; }

do_apply() { local d m env_want; env_want="$(env_content | sha_pipe)"
  ensure_dir "$(dirname -- "$SCRIPT_DST")" 755 脚本目录; ensure_dir "$UNIT_DIR" 755 单元目录; ensure_dir "$(dirname -- "$ENV_DST")" 755 "env 目录"
  for d in "${STATUS_DIRS[@]}"; do ensure_dir "$d" 755 状态文件目录
    if ! others_rx "$d"; then m="$(stat -c %a -- "$d")"; chmod 755 -- "$d"; say "[状态文件目录] mode 修正 $m → 755（巡检以部署用户读状态文件；属主不动）"; fi; done
  ensure_dir "$BACKUP_DIR" 700 备份目录
  [[ "$(stat -c '%U %a' -- "$BACKUP_DIR")" == "$WANT_OWNER 700" ]] || die "备份目录属主 / 权限须为「$WANT_OWNER 700」（db_backup.sh 前置会拒绝），请人工核对后重跑"
  place "$SCRIPT_SRC" "$SCRIPT_DST" 755 "$SCRIPT_SHA" 脚本
  place "$SERVICE_SRC" "$SERVICE_DST" 644 "$SERVICE_SHA" service
  place "$TIMER_SRC" "$TIMER_DST" 644 "$TIMER_SHA" timer
  env_content | place_gen "$ENV_DST" 600 "$env_want" env
  "$SYSTEMCTL" daemon-reload; say "[systemd] daemon-reload 完成"
  [[ "$("$SYSTEMCTL" cat "$SERVICE" | grep '^User=' | tail -n1)" == "User=root" ]] || die "systemctl cat $SERVICE 的 User= 不是 root（备份不得以部署用户跑），请立即 rollback"
  say "[service] systemctl cat 回读 User=root"
  if [[ "$(unit_state "$TIMER")" == "enabled active" ]]; then say "[timer] already_in_place enabled active"
  else "$SYSTEMCTL" enable --now "$TIMER"; say "[timer] enable --now → $(unit_state "$TIMER")"; fi
  [[ "$(unit_state "$TIMER")" == "enabled active" ]] || die "timer 回读不是 enabled active"
  say "回读："; show_file "$SCRIPT_DST" 脚本; show_file "$SERVICE_DST" service; show_file "$TIMER_DST" timer; show_file "$ENV_DST" env; show_file "$BACKUP_DIR" 备份目录
  rb_input "$SCRIPT_DST" "$SCRIPT_SHA" 脚本; rb_input "$SERVICE_DST" "$SERVICE_SHA" service; rb_input "$TIMER_DST" "$TIMER_SHA" timer
  rb_generated "$ENV_DST" "$env_want" env; rb_verdict
  say "apply 完成（未跑首轮；首轮用 run-once）"; }

do_run_once() {
  local rc=0 n=0 newest line
  [[ -f "$SCRIPT_DST" && -f "$ENV_DST" ]] || die "脚本或 db-backup.env 未安装，先 apply"
  [[ "$(sha_of "$ENV_DST")" == "$(env_content | sha_pipe)" ]] || die "在位 db-backup.env 与当前输入不一致，先 apply（本轮以 env 文件为准，不猜）"
  if (( TEST_FORM )); then
    say "测试形：直接 bash $SCRIPT_DST，注入 $ENV_DST 的 LINGXI_DB_BACKUP_*（等价 EnvironmentFile）"
    while IFS= read -r line; do [[ "$line" =~ ^(LINGXI_DB_BACKUP_[A-Z_]+)=(.*)$ ]] && export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"; done < "$ENV_DST"
    bash "$SCRIPT_DST" || rc=$?
    say "[备份脚本] rc=$rc"
  else
    "$SYSTEMCTL" start "$SERVICE" || rc=$?
    until [[ "$("$SYSTEMCTL" is-active "$SERVICE" 2>/dev/null || true)" != activating ]] || (( n >= RUN_TIMEOUT )); do n=$((n + 1)); sleep 1; done
    say "[service] $("$SYSTEMCTL" show -p Result -p ExecMainStatus "$SERVICE" | one_line) start rc=$rc（等待 ${n} 秒）"
    journalctl -u "$SERVICE" -n 12 --no-pager -o cat 2>/dev/null | sed 's/^/  journal: /' || true
  fi
  if [[ -f "$STATUS_FILE" ]]; then say "[状态文件全文] $STATUS_FILE"; sed 's/^/  /' -- "$STATUS_FILE"; else say "[状态文件] $STATUS_FILE absent"; fi
  newest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'lingxi-db-*.dump' -printf '%f\n' 2>/dev/null | sort | tail -n1)"
  say "[备份目录] $BACKUP_DIR dump 组数=$(find "$BACKUP_DIR" -maxdepth 1 -name 'lingxi-db-*.dump' 2>/dev/null | wc -l) 最新=${newest:-none}"
  if [[ -n "$newest" ]]; then
    stat -c '  %A %U:%G %s %n' -- "$BACKUP_DIR/$newest" "$BACKUP_DIR/$newest.sha256" "$BACKUP_DIR/$newest.counts.json"
    (cd "$BACKUP_DIR" && sha256sum -c "$newest.sha256") | sed 's/^/  sha256sum -c: /'
  fi
  (( rc == 0 )) || die "run-once 失败（rc=$rc）：看状态文件 error 与 journal"
  say "run-once 成功"; }

remote_parts() { [[ -n "$REMOTE" ]] || die "未设 LINGXI_S31_REMOTE（user@host:path）"; USERHOST="${REMOTE%%:*}"; RPATH="${REMOTE#*:}"; }
do_probe_remote() { local out rc=0; remote_parts
  out="$("$SSH" "${SSH_OPTS[@]}" "$USERHOST" true 2>&1)" || rc=$?
  if (( rc == 0 )); then say "[远端 $REMOTE_LABEL] reachable rc=0（ssh 身份 $(id -un)；未建目录）"
  else say "[远端 $REMOTE_LABEL] unreachable rc=$rc：$(mask "$(printf '%s' "$out" | one_line)")"; exit 1; fi; }
do_ensure_remote_dir() { local out rc=0; remote_parts
  out="$("$SSH" "${SSH_OPTS[@]}" "$USERHOST" "install -d -m 700 $RPATH && stat -c %a $RPATH" 2>&1)" || rc=$?
  [[ "$rc" == 0 && "$out" == 700 ]] || die "[远端 $REMOTE_LABEL] 建目录失败 rc=$rc：$(mask "$(printf '%s' "$out" | one_line)")"
  say "[远端 $REMOTE_LABEL] 目录已就位 mode=700（幂等）"; }

check_status_readable() { # 巡检用户（单元 User=，空视为 root）必须读得到状态文件：真形 sudo -u test -r；测试形 / 文件未生成按目录 o+rx 判
  local u d; u="$("$SYSTEMCTL" show -p User --value "$MONITOR_UNIT" 2>/dev/null || true)"; u="${u:-root}"
  if [[ -f "$STATUS_FILE" && "$TEST_FORM" == 0 ]]; then
    if [[ "$u" != root ]]; then sudo -n -u "$u" test -r "$STATUS_FILE" || die "巡检用户 $u 读不到状态文件 $STATUS_FILE，先 apply 修正目录权限；未写 drop-in"; fi
    say "[巡检] 用户 $u 读得到状态文件（sudo -u test -r）"; return; fi
  if [[ -f "$STATUS_FILE" ]]; then say "[巡检] 测试形跳过 sudo -u $u test -r，按目录权限判"; else say "[巡检] 状态文件尚未生成，按目录权限判（巡检用户 $u）"; fi
  for d in "${STATUS_DIRS[@]}"; do
    if ! { [[ -d "$d" ]] && others_rx "$d"; }; then die "巡检用户 $u 读不到状态文件（目录 $d mode=$(stat -c %a -- "$d" 2>/dev/null || echo absent) 缺 o+rx），先 apply 修正目录权限；未写 drop-in"; fi
    say "[状态文件目录] $d mode=$(stat -c %a -- "$d") o+rx ok"; done; }
do_monitor_script() { # 在位旧版巡检脚本可能不认识 --db-*：装仓库版（覆盖前副本进 BACKUP_ROOT，rollback 还原）
  ensure_dir "$(dirname -- "$MONITOR_SCRIPT_DST")" 755 巡检脚本目录
  place "$MONITOR_SCRIPT_SRC" "$MONITOR_SCRIPT_DST" 755 "$MONITOR_SCRIPT_SHA" 巡检脚本
  monitor_supports_db || die "装后的巡检脚本仍不含 --db-container，副本不对"
  say "回读："; show_file "$MONITOR_SCRIPT_DST" 巡检脚本; rb_input "$MONITOR_SCRIPT_DST" "$MONITOR_SCRIPT_SHA" 巡检脚本; rb_verdict
  say "monitor-script 完成（巡检 timer 下一轮即用新脚本；drop-in 另由 monitor-enable 写）"; }
do_monitor_enable() { local base want
  base="$(base_execstart)"
  [[ -n "$base" ]] || die "systemctl cat $MONITOR_UNIT 没有任何非空 ExecStart=，无法派生，未写 drop-in"
  [[ " $base " == *" ${MONITOR_SCRIPT_DST#"$ROOT"} "* ]] || die "有效 ExecStart 引用的脚本不是 ${MONITOR_SCRIPT_DST#"$ROOT"}（LINGXI_S31_MONITOR_SCRIPT_DST），不写 drop-in"
  check_status_readable
  monitor_supports_db || die "在位 host_health_alert.py 不支持 --db-*，先 monitor-script；未写 drop-in"
  [[ "$base" != *--db-container* ]] || die "$MONITOR_UNIT 现有 ExecStart 已含 --db-container（来自别的 drop-in），不重复追加，未写 drop-in"
  want="$(dropin_content "$base" | sha_pipe)"
  ensure_dir "$DROPIN_DIR" 755 "drop-in 目录"
  dropin_content "$base" | place_gen "$DROPIN_DST" 644 "$want" "巡检 drop-in"
  rb_generated "$DROPIN_DST" "$want" "巡检 drop-in"; rb_verdict
  "$SYSTEMCTL" daemon-reload
  effective_has_db || die "回读 systemctl show -p ExecStart 不含 --db-container，请 monitor-disable"
  say "[巡检] 有效 ExecStart 已含本地库五项：容器 $CONTAINER、状态文件 $STATUS_FILE、过期 ${MON_MAX_AGE_HOURS}h、连接 ${MON_CONN_PERCENT}%、WAL ${MON_WAL_BYTES}B（timer 下一轮生效，不需 restart）"; }
do_monitor_disable() {
  if [[ -f "$DROPIN_DST" ]]; then rm -f -- "$DROPIN_DST"; say "[巡检 drop-in] removed $DROPIN_DST"; else say "[巡检 drop-in] absent_skip"; fi
  "$SYSTEMCTL" daemon-reload
  if effective_has_db; then die "回读 systemctl show -p ExecStart 仍含 --db-container（来自别的 drop-in？），请人工核对"; fi
  say "[巡检] 有效 ExecStart 不含 --db-container"; }

restore_or_remove() { # target 名称 [keep]：BACKUP_ROOT 里最新副本还原；没有副本则删除，keep 形不删只打印 kept（可能非本脚本所装）
  local newest="" b; shopt -s nullglob; for b in "$BACKUP_ROOT/$(basename -- "$1")".*; do newest="$b"; done; shopt -u nullglob
  if [[ -n "$newest" ]]; then cp -p -- "$newest" "$1"; say "[$2] restored_from=${newest##*/} sha=$(sha_of "$1")"; RESTORED=1
  elif [[ ! -e "$1" ]]; then say "[$2] absent_skip"
  elif [[ "${3:-}" == keep ]]; then say "[$2] kept sha=$(sha_of "$1")（BACKUP_ROOT 无副本：不是本脚本覆盖的，不删）"
  else rm -f -- "$1"; say "[$2] removed $1"; fi; }
do_rollback() { RESTORED=0
  if [[ "$(unit_state "$TIMER")" != "disabled inactive" ]]; then "$SYSTEMCTL" disable --now "$TIMER" 2>/dev/null || true; fi
  say "[timer] $TIMER → $(unit_state "$TIMER")"
  restore_or_remove "$SERVICE_DST" service; restore_or_remove "$TIMER_DST" timer
  "$SYSTEMCTL" daemon-reload; say "[systemd] daemon-reload 完成"
  restore_or_remove "$SCRIPT_DST" 脚本; restore_or_remove "$ENV_DST" env
  do_monitor_disable
  restore_or_remove "$MONITOR_SCRIPT_DST" 巡检脚本 keep
  (( RESTORED )) && say "注意：还原了 BACKUP_ROOT 里的旧副本，timer 保持 disabled，需要时手动 enable"
  say "rollback 完成：备份目录 $BACKUP_DIR 及其 dump、状态文件 $STATUS_FILE 一律保留未动"; }
do_cleanup() { local d f n=0; shopt -s nullglob
  for d in "$(dirname -- "$SCRIPT_DST")" "$UNIT_DIR" "$(dirname -- "$ENV_DST")" "$DROPIN_DIR" "$(dirname -- "$MONITOR_SCRIPT_DST")"; do
    for f in "$d"/*.tmp-s31 "$d"/.s31.??????; do rm -f -- "$f"; say "removed $f"; n=$((n + 1)); done; done
  shopt -u nullglob; (( n )) || say nothing; }

case "$SUB" in
  dry) resolve; report ;;
  status) resolve; report full || true ;;
  apply) resolve; report >/dev/null || true; require_ok; do_apply ;;
  run-once) resolve; require_ok; do_run_once ;;
  probe-remote) do_probe_remote ;;
  ensure-remote-dir) do_ensure_remote_dir ;;
  monitor-script) resolve; require_ok; do_monitor_script ;;
  monitor-enable) do_monitor_enable ;;
  monitor-disable) do_monitor_disable ;;
  rollback) do_rollback ;;
  cleanup) do_cleanup ;;
  *) die "未知子命令 $SUB（dry|apply|run-once|probe-remote|ensure-remote-dir|monitor-script|monitor-enable|monitor-disable|rollback|status|cleanup）" ;;
esac
# 变量（LINGXI_S31_*；预发 / 生产两形差异只在输入文件）：ROOT（假根，测试）SYSTEMCTL SSH SCP（可注入桩）ENV_FILE
# SCRIPT_SRC SCRIPT_SHA（必填）UNIT_SRC_DIR SERVICE_SHA TIMER_SHA（必填）SCRIPT_DST UNIT_DIR ENV_DST BACKUP_DIR STATUS_FILE
# CONTAINER KEEP TRANSFER(none|scp) REMOTE(user@host:/abs/or/~/path，只进 env 文件) REMOTE_LABEL SSH_OPTS
# MONITOR_UNIT MONITOR_DROPIN MONITOR_SCRIPT_SRC MONITOR_SCRIPT_SHA（必填）MONITOR_SCRIPT_DST MON_MAX_AGE_HOURS MON_CONN_PERCENT
# MON_WAL_BYTES BACKUP_ROOT（本脚本的覆盖前副本目录）RUN_TIMEOUT
