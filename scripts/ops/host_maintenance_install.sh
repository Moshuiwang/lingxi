#!/usr/bin/env bash
# host_maintenance_install.sh —— 宿主巡检单元本体、巡检脚本与备份脚本的一次性维护安装（#884 第 2–7 条宿主侧、
# #891 宿主告警卡片化；Trace #898 W4）。控制包不带 scripts/ops/ 与宿主单元，这几份文件不经本脚本就到不了主机。
# 装什么（按此顺序，前一步下一整分轮不成功就不做下一步）：
#   ① lingxi-host-monitor.service 本体换成仓库版（解释器走注入点 /opt/lingxi/bin/python3、TimeoutStartSec=50），
#      删掉临时换解释器的 drop-in 20-python312.conf；等下一整分轮 Result=success ExecMainStatus=0。
#   ② host_health_alert.py 换成仓库版（#884 第 4–6 条、#891 卡片化；需 3.11 以上）；再等下一整分轮成功。
#      先①后②是 2026-09-20 事故的教训：脚本先升、单元仍走系统 3.9，装完即坏 9 小时无人察觉。
#   ③ db_backup.sh 换成仓库版（#884 第 2–3 条）；不手动触发备份，只做静态回读（sha、bash -n、单元 ExecStart 指向），
#      下一次自然轮（UTC 18:30）的只读回读命令在收尾打印。
#   ④ #886 第 1 处：只读回读部署用户 ~/backups 下例行 dump 的权限位，不改。
# 为什么不并进 db_backup_install.sh：那份的 monitor-script 只换脚本、不换单元本体、不等下一轮、失败不自动回装；
# 它的输入文件还带异机地址，本次维护用不上也不该经手。沿用它的约定：umask 077、覆盖前 cp -p 留副本、
# 输入副本按清单 sha 判、测试形「非 root + 假根」、systemctl 可注入桩；宿主契约读法与 db_switch_to_local.sh 同一个 json_str。
# 输入：本脚本同目录放 lingxi-host-monitor.service、host_health_alert.py、db_backup.sh 与清单 SHA256SUMS
#（sha256sum 格式，由编排者从正式 tag 的仓库内容生成）；任一文件与清单不符即停，不动主机。
# 路径不写死：环境取自宿主契约的 environment（契约路径依次取 LINGXI_S30_HOST_CONTRACT、拉取代理单元有效 ExecStart 的
# --host-contract、缺省 /opt/lingxi/control/host-contract.json，打印来源）；单元本体位置取自
# systemctl show -p FragmentPath；巡检脚本位置取自装后有效 ExecStart 的第二段；备份脚本位置取自
# lingxi-db-backup.service 的 ExecStart；注入点取自候选单元自己的 ExecStart 第一段。
# 生产与预发的差异（数据驱动，脚本同一份）：生产在位单元本体须是已知旧版（#859 5757269798 回读的 c14989d1…）
# 或已是候选版，本体与候选的差异里不许有 User=；预发不钉旧版 sha，本体里的 User= 允许：10-local.conf 在位须同值，
# 不在位则 apply 先写一份只含该 User= 的 10-local.conf（纳入备份清单，回装即删）。本体差异白名单：注释 / ExecStart /
# TimeoutStartSec / User / Description / After / Wants / WorkingDirectory / StandardOutput / StandardError，后六个逐键打印旧值 → 新值。
# 用法：sudo -n bash host_maintenance_install.sh <子命令>                                     到终态预计时长
#   check          只读：前提逐项核对 + 计划（每步 already / install），零写入                         < 5 秒
#   apply --yes    前提 → 对齐到整分后约 10 秒 → 备份 → ① → 等轮 → ② → 等轮 → ③ → ④ → 汇总；
#                  任一步不符自动 restore 并以非零退出；各步已是目标 sha 的打印 already 跳过          ≤ 3 分钟
#   restore --yes  按最近一份完整备份回装（逐字节、保留属主与权限）→ daemon-reload → 回读                 < 5 秒
#   status         只读汇总：在位 sha、单元有效解释器、最近一轮结果、备份份数、#886 第 1 处回读          < 5 秒
# 输出只含 sha / 计数 / 状态字段与路径；不读、不打印任何凭据文件内容。
# 测试形：非 root + LINGXI_HM_ROOT=<假根>：路径加前缀、跳过属主设置，systemctl 走 LINGXI_HM_SYSTEMCTL 桩；
# LINGXI_HM_ALIGN=0 跳过整分对齐，LINGXI_HM_POLL_SECONDS / LINGXI_HM_ROUND_TIMEOUT 调轮询间隔与等轮上限。
set -euo pipefail
export LC_ALL=C
umask 077

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUB="${1:-}"; CONFIRM="${2:-}"
[[ -n "$SUB" ]] || { sed -n '/^# 用法：/,/^#   status /p' "${BASH_SOURCE[0]}" >&2; exit 2; }

say() { printf 'hm %s\n' "$*"; }
die() { printf 'hm 错误：%s\n' "$*" >&2; exit 1; }
sha_of() { if [[ -f "$1" ]]; then sha256sum -- "$1" | cut -d' ' -f1; else echo absent; fi; }
stamp() { date -u +%Y%m%dT%H%M%SZ; }
json_str() { grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$1" | head -n1 | sed 's/^[^:]*:[[:space:]]*"//; s/"$//'; }

ROOT="${LINGXI_HM_ROOT:-}"
OWNER_ARGS=(-o root -g root)
if [[ "$(id -u)" -ne 0 ]]; then
  [[ -n "$ROOT" ]] || die "必须以 root 运行（sudo -n bash $0 $SUB）；假根目录测试请设 LINGXI_HM_ROOT"
  OWNER_ARGS=(); say "测试形：非 root + LINGXI_HM_ROOT=$ROOT，跳过属主设置"
fi
SYSTEMCTL="${LINGXI_HM_SYSTEMCTL:-systemctl}"
UNIT_DIR="$ROOT/etc/systemd/system"
MONITOR_UNIT=lingxi-host-monitor.service; MONITOR_TIMER=lingxi-host-monitor.timer; BACKUP_UNIT=lingxi-db-backup.service
PY_DROPIN_NAME="${LINGXI_HM_PY_DROPIN:-20-python312.conf}"
BACKUP_ROOT="$ROOT${LINGXI_HM_BACKUP_ROOT:-/root/lingxi-884-backup}"
PROD_OLD_UNIT_SHA="${LINGXI_HM_PROD_OLD_UNIT_SHA:-c14989d1d43e9eba62c423c70a9333de17102be9401f329f112b2569dfcac33b}"
ROUND_TIMEOUT="${LINGXI_HM_ROUND_TIMEOUT:-150}"; POLL="${LINGXI_HM_POLL_SECONDS:-2}"; ALIGN="${LINGXI_HM_ALIGN:-1}"
MANIFEST="$SELF_DIR/SHA256SUMS"
CAND_UNIT="$SELF_DIR/$MONITOR_UNIT"; CAND_MON="$SELF_DIR/host_health_alert.py"; CAND_BAK="$SELF_DIR/db_backup.sh"
[[ "$ROUND_TIMEOUT" =~ ^[0-9]+$ && "$POLL" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "LINGXI_HM_ROUND_TIMEOUT 须为整数、LINGXI_HM_POLL_SECONDS 须为数字"
command -v "$SYSTEMCTL" >/dev/null 2>&1 || die "找不到 $SYSTEMCTL"

sv() { "$SYSTEMCTL" show -p "$1" --value "${2:-$MONITOR_UNIT}" 2>/dev/null || true; }  # 单个属性值
exec_field() { # $1=field(path|argv) $2=unit：systemctl show ExecStart 的 { path=… ; argv[]=… ; … }
  local raw; raw="$(sv ExecStart "$2")"
  case "$1" in
    path) printf '%s' "$raw" | sed -n 's/^{ path=\([^ ;]*\) ;.*/\1/p' ;;
    argv) printf '%s' "$raw" | sed -n 's/.* argv\[\]=\(.*\) ; ignore_errors=.*/\1/p' ;;
  esac; }
# 宿主契约路径（编排者 2026-09-27 预发实跑后裁定）：环境变量 LINGXI_S30_HOST_CONTRACT（与 db_switch_to_local.sh 同一约定）
# → 拉取代理单元有效 ExecStart 的 --host-contract 参数（预发的 drop-in 把它改到了别处，缺省路径并不存在）→ 缺省路径
resolve_contract() {
  local argv tok prev="" found=""
  if [[ -n "${LINGXI_S30_HOST_CONTRACT:-}" ]]; then HOST_CONTRACT="$LINGXI_S30_HOST_CONTRACT"; CONTRACT_SOURCE="环境变量 LINGXI_S30_HOST_CONTRACT"; return 0; fi
  argv="$(exec_field argv lingxi-release-pull.service)"
  for tok in $argv; do
    if [[ "$prev" == --host-contract ]]; then found="$tok"; elif [[ "$tok" == --host-contract=* ]]; then found="${tok#--host-contract=}"; fi
    prev="$tok"; done
  if [[ -n "$found" ]]; then HOST_CONTRACT="$ROOT$found"; CONTRACT_SOURCE="lingxi-release-pull.service 有效 ExecStart 的 --host-contract"
  else HOST_CONTRACT="$ROOT/opt/lingxi/control/host-contract.json"; CONTRACT_SOURCE="缺省路径"; fi; }
exec_count() { sv ExecStart "$1" | grep -o '{ path=' | wc -l; }
strip_prefix() { local v="$1"; while [[ "$v" == [-@:+!]* ]]; do v="${v:1}"; done; printf '%s' "$v"; }
effective_from_files() { # 依次读单元与 drop-in：空 ExecStart= 清空、非空覆盖；输出最后的有效命令行
  local f line val=""
  for f in "$@"; do [[ -f "$f" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^ExecStart=(.*)$ ]] && val="$(strip_prefix "${BASH_REMATCH[1]}")"
    done < "$f"; done
  printf '%s' "$val"; }
last_key() { [[ -f "$2" ]] && grep -E "^$1=" "$2" | tail -n1 | cut -d= -f2- || true; }
# 本体差异白名单：注释 / ExecStart / TimeoutStartSec / User，加上编排者预发实跑后裁定放行的六个说明性键（逐键打印旧值 → 新值）
EXTRA_KEYS=(Description After Wants WorkingDirectory StandardOutput StandardError)
unit_core() { grep -vE '^[[:space:]]*([#;]|$)' "$1" | grep -vE "^(ExecStart|TimeoutStartSec|User|$(IFS='|'; echo "${EXTRA_KEYS[*]}"))=" || true; }
diff_keys() { # 两份单元里除注释 / 空行外逐行不同的键名（只打键名，不打值）
  diff <(grep -vE '^[[:space:]]*([#;]|$)' "$1") <(grep -vE '^[[:space:]]*([#;]|$)' "$2") \
    | sed -n 's/^[<>] \([A-Za-z]*\)=.*/\1/p; s/^[<>] \(\[.*\]\)$/\1/p' | sort -u | tr '\n' ' ' || true; }

# ---------------- 前提（check / apply / status 共用；只读） ----------------
FAIL=(); declare -A WANT=()
NEED_LOCAL=0; LOCAL_USER=""; HOST_CONTRACT=""; CONTRACT_SOURCE=""; ENVIRONMENT=""; INJECT=""; FRAG=""; PRE_USER=""; PRE_ARGS=""; PY_DROPIN=""; MON_DST=""; BAK_DST=""; BK=""; DROPINS=()
preflight() {
  local line name file py ver
  # 宿主契约
  resolve_contract; say "[契约] 来源=$CONTRACT_SOURCE 路径=${HOST_CONTRACT#"$ROOT"}"
  if [[ -f "$HOST_CONTRACT" ]]; then ENVIRONMENT="$(json_str "$HOST_CONTRACT" environment || true)"; else ENVIRONMENT=""; fi
  [[ "$ENVIRONMENT" == production || "$ENVIRONMENT" == stage ]] || FAIL+=("宿主契约 $HOST_CONTRACT 缺失或 environment 不是 production / stage")
  say "[契约] environment=${ENVIRONMENT:-unknown}"
  # 输入清单：三份必备，清单里列出的每一份都要逐一相符
  if [[ -f "$MANIFEST" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" ]] && continue
      [[ "$line" =~ ^([0-9a-f]{64})[[:space:]]+\*?([A-Za-z0-9_.-]+)$ ]] || { FAIL+=("清单 SHA256SUMS 有不认识的行"); continue; }
      name="${BASH_REMATCH[2]}"; WANT[$name]="${BASH_REMATCH[1]}"; file="$SELF_DIR/$name"
      if [[ "$(sha_of "$file")" == "${WANT[$name]}" ]]; then say "[输入] $name sha=${WANT[$name]} = 清单 → ok"
      else say "[输入] $name 在位 sha=$(sha_of "$file") ≠ 清单 ${WANT[$name]} → 不符"; FAIL+=("输入 $name 与清单不符"); fi
    done < "$MANIFEST"
    for name in "$MONITOR_UNIT" host_health_alert.py db_backup.sh; do [[ -n "${WANT[$name]:-}" ]] || FAIL+=("清单缺 $name"); done
  else FAIL+=("同目录缺清单 SHA256SUMS"); fi
  [[ -f "$CAND_UNIT" && -f "$CAND_MON" && -f "$CAND_BAK" ]] || { FAIL+=("同目录缺输入文件"); return 0; }
  # 注入点：候选单元自己的 ExecStart 第一段；须是链接且 3.11 以上
  INJECT="$(effective_from_files "$CAND_UNIT")"; INJECT="${INJECT%% *}"
  [[ "$INJECT" == /* ]] || FAIL+=("候选单元 ExecStart 第一段不是绝对路径")
  py="$ROOT$INJECT"; ver=""
  if [[ -L "$py" && -x "$py" ]]; then ver="$("$py" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"; fi
  if [[ "$ver" =~ ^3\.([0-9]+)\. ]] && (( BASH_REMATCH[1] >= 11 )); then say "[注入点] $INJECT 是链接，版本 $ver → ok"
  else say "[注入点] $INJECT 链接=$([[ -L "$py" ]] && echo yes || echo no) 版本=${ver:-unknown} → 不符"; FAIL+=("注入点 $INJECT 不存在、不是链接或低于 3.11"); fi
  # 定时器与单元现状
  [[ "$("$SYSTEMCTL" is-active "$MONITOR_TIMER" 2>/dev/null || true)" == active ]] || FAIL+=("$MONITOR_TIMER 不是 active（不会有下一轮）")
  FRAG="$(sv FragmentPath)"; PRE_USER="$(sv User)"; PRE_ACTIVE="$(sv ActiveState)"; PRE_RESULT="$(sv Result)"
  say "[巡检单元] FragmentPath=$FRAG sha=$(sha_of "$FRAG") 大小=$(stat -c %s -- "$FRAG" 2>/dev/null || echo 0)B ActiveState=$PRE_ACTIVE Result=$PRE_RESULT User=${PRE_USER:-root}"
  [[ -n "$FRAG" && "$(dirname -- "$FRAG")" == "$UNIT_DIR" && -f "$FRAG" ]] || { FAIL+=("$MONITOR_UNIT 本体不在 $UNIT_DIR 下"); return 0; }
  [[ "$PRE_ACTIVE" == inactive && "$PRE_RESULT" == success ]] || FAIL+=("$MONITOR_UNIT 现状不是 inactive + success（基线不干净，无法判下一轮）")
  [[ "$(exec_count "$MONITOR_UNIT")" == 1 ]] || FAIL+=("$MONITOR_UNIT 有效 ExecStart 不是恰好一条")
  DROPINS=(); PY_DROPIN=""; local d
  for d in $(sv DropInPaths); do DROPINS+=("$d"); [[ "$(basename -- "$d")" == "$PY_DROPIN_NAME" ]] && PY_DROPIN="$d"; done
  for d in "${DROPINS[@]}"; do say "[drop-in] $(basename -- "$d") sha=$(sha_of "$d")"; done
  if [[ -n "$PY_DROPIN" ]]; then
    [[ "$(dirname -- "$PY_DROPIN")" == "$FRAG.d" ]] || FAIL+=("$PY_DROPIN_NAME 不在 $FRAG.d 下")
    ! grep -vE '^[[:space:]]*([#;]|$)|^\[Service\]$|^ExecStart=' "$PY_DROPIN" >/dev/null || FAIL+=("$PY_DROPIN_NAME 含 ExecStart 以外的设置，删掉会丢配置")
  fi
  # 换装前后的有效命令行：只许换解释器，参数逐字不变，且换后第一段必须是注入点
  local rest=() pre post
  for d in "${DROPINS[@]}"; do [[ "$d" == "$PY_DROPIN" ]] || rest+=("$d"); done
  pre="$(effective_from_files "$FRAG" "${DROPINS[@]}")"; post="$(effective_from_files "$CAND_UNIT" "${rest[@]}")"
  PRE_ARGS="${pre#* }"; POST_ARGS="${post#* }"; MON_DST="$ROOT$(printf '%s' "$POST_ARGS" | cut -d' ' -f1)"
  say "[有效命令行] 换装前解释器=${pre%% *} 换装后解释器=${post%% *} 参数一致=$([[ "$PRE_ARGS" == "$POST_ARGS" ]] && echo yes || echo no)"
  [[ "${post%% *}" == "$INJECT" ]] || FAIL+=("换装后有效 ExecStart 不走注入点（有别的 drop-in 覆盖了解释器）")
  [[ "$PRE_ARGS" == "$POST_ARGS" ]] || FAIL+=("换装前后 ExecStart 参数不一致（只许换解释器）")
  [[ "$(basename -- "$MON_DST")" == host_health_alert.py ]] || FAIL+=("有效 ExecStart 第二段不是 host_health_alert.py")
  # 本体与候选的差异：只许注释 / ExecStart / TimeoutStartSec /（预发）User=
  if [[ "$(sha_of "$FRAG")" != "${WANT[$MONITOR_UNIT]:-x}" ]]; then
    say "[本体差异] 键：$(diff_keys "$FRAG" "$CAND_UNIT")"
    local k ov nv
    for k in "${EXTRA_KEYS[@]}"; do ov="$(last_key "$k" "$FRAG")"; nv="$(last_key "$k" "$CAND_UNIT")"
      [[ "$ov" == "$nv" ]] || say "[本体差异] $k：${ov:-（无）} → ${nv:-（无）}"; done
    [[ "$(unit_core "$FRAG")" == "$(unit_core "$CAND_UNIT")" ]] || FAIL+=("在位单元本体与候选有意外差异（注释 / ExecStart / TimeoutStartSec / User / ${EXTRA_KEYS[*]} 以外）")
    [[ -z "$(last_key User "$CAND_UNIT")" ]] || FAIL+=("候选单元本体不应含 User=")
    local body_user local_user; body_user="$(last_key User "$FRAG")"; local_user="$(last_key User "$FRAG.d/10-local.conf")"
    if [[ "$ENVIRONMENT" == production ]]; then
      [[ "$(sha_of "$FRAG")" == "$PROD_OLD_UNIT_SHA" ]] || FAIL+=("生产在位单元本体 sha 既不是已知旧版也不是候选版")
      [[ -z "$body_user" ]] || FAIL+=("生产在位单元本体含 User=（生产不许这类差异）")
    elif [[ -n "$body_user" && ! -e "$FRAG.d/10-local.conf" ]]; then
      NEED_LOCAL=1; LOCAL_USER="$body_user"; say "[预发] 本体带 User=、无 10-local.conf：apply 先写 10-local.conf（User= 同值）再换本体"
    elif [[ -n "$body_user" ]]; then
      [[ "$body_user" == "$local_user" ]] || FAIL+=("预发在位本体 User= 与 10-local.conf 不同值")
    fi
  fi
  # 候选巡检脚本能被注入点解释器导入（避免 09-20 那种装完即坏）；备份脚本语法与单元指向
  if [[ -x "$py" ]] && ! "$py" "$CAND_MON" --help >/dev/null 2>&1; then FAIL+=("候选 host_health_alert.py 在注入点解释器下起不来（--help 非零）"); fi
  bash -n "$CAND_BAK" 2>/dev/null || FAIL+=("候选 db_backup.sh bash -n 不通过")
  local bargv; bargv="$(exec_field argv "$BACKUP_UNIT")"; BAK_DST="$ROOT${bargv##* }"
  [[ -n "$(sv FragmentPath "$BACKUP_UNIT")" && "$BAK_DST" == */db_backup.sh ]] || FAIL+=("$BACKUP_UNIT 未装或 ExecStart 不指向 db_backup.sh")
  return 0; }

plan_steps() {
  NEED_UNIT=0; NEED_MON=0; NEED_BAK=0
  [[ "$(sha_of "$FRAG")" == "${WANT[$MONITOR_UNIT]:-none}" && -z "$PY_DROPIN" ]] || NEED_UNIT=1
  [[ "$(sha_of "$MON_DST")" == "${WANT[host_health_alert.py]:-none}" ]] || NEED_MON=1
  [[ "$(sha_of "$BAK_DST")" == "${WANT[db_backup.sh]:-none}" ]] || NEED_BAK=1
  say "[计划] ① 单元本体 $FRAG 在位 $(sha_of "$FRAG") → $( ((NEED_UNIT)) && echo "install ${WANT[$MONITOR_UNIT]:-none}$([[ -n "$PY_DROPIN" ]] && echo " 并删 $PY_DROPIN_NAME")$( ((NEED_LOCAL)) && echo " 并先写 10-local.conf")" || echo already)"
  say "[计划] ② 巡检脚本 $MON_DST 在位 $(sha_of "$MON_DST") → $( ((NEED_MON)) && echo "install ${WANT[host_health_alert.py]:-none}" || echo already)"
  say "[计划] ③ 备份脚本 $BAK_DST 在位 $(sha_of "$BAK_DST") → $( ((NEED_BAK)) && echo "install ${WANT[db_backup.sh]:-none}" || echo already)"; }
require_ok() { if ((${#FAIL[@]})); then local f; for f in "${FAIL[@]}"; do say "前提不符：$f"; done; die "前提不符，未做任何改动"; fi; say "前提全部满足"; }

# ---------------- 备份 / 回装 ----------------
make_backup() { # 只备份会被改动的五个目标（含预发可能新写的 10-local.conf）；原本不存在的记 absent，回装时删掉
  local i=0 t
  install -d -m 700 "${OWNER_ARGS[@]}" -- "$BACKUP_ROOT"
  BK="$BACKUP_ROOT/$(stamp)"; install -d -m 700 "${OWNER_ARGS[@]}" -- "$BK"
  "$SYSTEMCTL" cat "$MONITOR_UNIT" > "$BK/systemctl-cat.txt" 2>/dev/null || true
  for t in "$FRAG" "${PY_DROPIN:-$FRAG.d/$PY_DROPIN_NAME}" "$FRAG.d/10-local.conf" "$MON_DST" "$BAK_DST"; do i=$((i + 1))
    if [[ -f "$t" ]]; then cp -p -- "$t" "$BK/f$i"; printf 'present %s f%s %s\n' "$(sha_of "$t")" "$i" "$t" >> "$BK/files.list"
    else printf 'absent - f%s %s\n' "$i" "$t" >> "$BK/files.list"; fi; done
  touch "$BK/complete"; say "[备份] $BK（0700）$(wc -l < "$BK/files.list") 项，drop-in 原文另存 systemctl-cat.txt"; }
do_restore() { # $1 = 备份目录
  local state sha key path bad=0
  while read -r state sha key path; do
    if [[ "$state" == present ]]; then cp -p -- "$1/$key" "$path.tmp-hm"; mv -f -- "$path.tmp-hm" "$path"
      if [[ "$(sha_of "$path")" == "$sha" ]]; then say "[回装] $path sha=$sha 逐字节一致"; else say "[回装] $path sha=$(sha_of "$path") ≠ 备份 $sha"; bad=1; fi
    else rm -f -- "$path"; say "[回装] $path 原本不存在 → 已删（$( [[ -e "$path" ]] && echo 仍在 || echo absent)）"; fi
  done < "$1/files.list"
  "$SYSTEMCTL" daemon-reload; date -u +%FT%TZ > "$1/restored"
  say "[回装后] 解释器=$(exec_field path "$MONITOR_UNIT") DropInPaths=$(for d in $(sv DropInPaths); do basename -- "$d"; done | tr '\n' ' ')User=$(sv User) 最近一轮 Result=$(sv Result) ExecMainStatus=$(sv ExecMainStatus)"
  (( bad == 0 )) || die "回装后 sha 与备份不一致，请人工核对 $1"; }
latest_backup() { local d last=""; shopt -s nullglob; for d in "$BACKUP_ROOT"/*/; do [[ -f "$d/complete" ]] && last="${d%/}"; done; shopt -u nullglob; printf '%s' "$last"; }
auto_restore() { say "第 $1 步不符 → 自动回装 $BK"; do_restore "$BK"; die "第 $1 步不符，已自动回装到安装前状态（见上方回装行）"; }

# ---------------- 等下一整分轮 ----------------
align_minute() { # 整分后约 10 秒动手：巡检每分钟 :00 触发，动手时刻落在两轮之间
  (( ALIGN )) || { say "[对齐] 跳过（LINGXI_HM_ALIGN=0）"; return 0; }
  local s; s="$((10#$(date -u +%S)))"
  if (( s < 8 )); then say "[对齐] 等 $((10 - s)) 秒"; sleep "$((10 - s))"; elif (( s > 40 )); then say "[对齐] 等 $((70 - s)) 秒"; sleep "$((70 - s))"; fi
  [[ "$(sv ActiveState)" != activating ]] || { sleep 5; [[ "$(sv ActiveState)" != activating ]] || die "巡检单元在跑，未动手"; }; }
wait_round() { # $1 = 装后记录的 InvocationID：等一条更新的、已结束的轮次，上限 ROUND_TIMEOUT 秒
  local base="$1" inv act deadline=$((SECONDS + ROUND_TIMEOUT))
  say "[等轮] 基线 InvocationID=${base:-none}，上限 ${ROUND_TIMEOUT} 秒"
  while (( SECONDS < deadline )); do
    inv="$(sv InvocationID)"; act="$(sv ActiveState)"
    if [[ -n "$inv" && "$inv" != "$base" && "$act" != activating ]]; then
      say "[等轮] 新轮 ExecMainStartTimestamp=$(sv ExecMainStartTimestamp) Result=$(sv Result) ExecMainStatus=$(sv ExecMainStatus) ActiveState=$act"
      [[ "$(sv Result)" == success && "$(sv ExecMainStatus)" == 0 ]]; return; fi
    sleep "$POLL"; done
  say "[等轮] ${ROUND_TIMEOUT} 秒内没有新一轮结束"; return 1; }

# ---------------- 三步 ----------------
place_file() { # src dst mode
  install -m "$3" "${OWNER_ARGS[@]}" -- "$1" "$2.tmp-hm" && mv -f -- "$2.tmp-hm" "$2"; }
step_unit() {
  if (( ! NEED_UNIT )); then say "① already 单元本体 sha=$(sha_of "$FRAG")，无 $PY_DROPIN_NAME"; return 0; fi
  # 本函数在「|| auto_restore」左侧被调用，bash 在此不触发 set -e：每条写入显式判返回值
  if (( NEED_LOCAL )); then # 预发：User= 先落进 10-local.conf，再换掉带 User= 的本体
    install -d -m 755 "${OWNER_ARGS[@]}" -- "$FRAG.d" || return 1
    printf '[Service]\nUser=%s\n' "$LOCAL_USER" > "$FRAG.d/.10-local.hm" || return 1
    place_file "$FRAG.d/.10-local.hm" "$FRAG.d/10-local.conf" 644 || return 1; rm -f -- "$FRAG.d/.10-local.hm"
    say "① 已写 10-local.conf sha=$(sha_of "$FRAG.d/10-local.conf")（User= 与本体同值）"; fi
  place_file "$CAND_UNIT" "$FRAG" 644 || return 1
  if [[ -n "$PY_DROPIN" ]]; then rm -f -- "$PY_DROPIN" || return 1; say "① 已删 $PY_DROPIN_NAME"; fi
  "$SYSTEMCTL" daemon-reload || return 1
  local path argv user tmo want_tmo drops ok=1; path="$(exec_field path "$MONITOR_UNIT")"; argv="$(exec_field argv "$MONITOR_UNIT")"
  user="$(sv User)"; tmo="$(sv TimeoutStartUSec)"; want_tmo="$(last_key TimeoutStartSec "$CAND_UNIT")s"
  drops="$(for d in $(sv DropInPaths); do basename -- "$d"; done | tr '\n' ' ')"
  say "① 回读 FragmentPath=$(sv FragmentPath) sha=$(sha_of "$FRAG") DropInPaths=${drops}ExecStart 解释器=$path User=${user:-root} TimeoutStartUSec=$tmo"
  [[ "$(sv FragmentPath)" == "$FRAG" && "$(sha_of "$FRAG")" == "${WANT[$MONITOR_UNIT]}" ]] || { say "① 本体回读不符"; ok=0; }
  [[ " $drops" != *" $PY_DROPIN_NAME "* ]] || { say "① $PY_DROPIN_NAME 仍在 DropInPaths"; ok=0; }
  [[ "$path" == "$INJECT" && "${argv#* }" == "$PRE_ARGS" ]] || { say "① 有效 ExecStart 不是「注入点 + 原参数」"; ok=0; }
  [[ "$user" == "$PRE_USER" ]] || { say "① User 由 ${PRE_USER:-root} 变成 ${user:-root}"; ok=0; }
  [[ "$tmo" == "$want_tmo" ]] || { say "① TimeoutStartUSec 期望 $want_tmo"; ok=0; }
  (( ok )) || return 1
  wait_round "$(sv InvocationID)"; }
step_mon() {
  if (( ! NEED_MON )); then say "② already 巡检脚本 sha=$(sha_of "$MON_DST")"; return 0; fi
  [[ "$(exec_field path "$MONITOR_UNIT")" == "$INJECT" ]] || { say "② 单元有效解释器不是注入点，不装脚本"; return 1; }
  place_file "$CAND_MON" "$MON_DST" 755 || return 1
  say "② 回读 $MON_DST sha=$(sha_of "$MON_DST") mode=$(stat -c %a -- "$MON_DST")"
  [[ "$(sha_of "$MON_DST")" == "${WANT[host_health_alert.py]}" ]] || return 1
  wait_round "$(sv InvocationID)"; }
step_bak() {
  if (( ! NEED_BAK )); then say "③ already 备份脚本 sha=$(sha_of "$BAK_DST")"; return 0; fi
  place_file "$CAND_BAK" "$BAK_DST" 755 || return 1
  local argv; argv="$(exec_field argv "$BACKUP_UNIT")"
  say "③ 回读 $BAK_DST sha=$(sha_of "$BAK_DST") mode=$(stat -c %a -- "$BAK_DST") bash -n=$(bash -n "$BAK_DST" 2>/dev/null && echo ok || echo fail) 单元 ExecStart 末段=${argv##* }"
  [[ "$(sha_of "$BAK_DST")" == "${WANT[db_backup.sh]}" ]] && bash -n "$BAK_DST" 2>/dev/null && [[ "$ROOT${argv##* }" == "$BAK_DST" ]]; }

umask_readback() { # #886 第 1 处：部署用户 ~/backups 例行 dump 的权限位，只读
  local dir="${LINGXI_HM_ROUTINE_DUMP_DIR:-}" home n bad newest
  if [[ -z "$dir" ]]; then home="$(getent passwd "${PRE_USER:-root}" 2>/dev/null | cut -d: -f6 || true)"; dir="$ROOT${home:-/nonexistent}/backups"; fi
  if [[ ! -d "$dir" ]]; then say "[#886 第 1 处] $dir absent"; return 0; fi
  n="$(find "$dir" -maxdepth 1 -type f -name '*.dump' | wc -l)"; bad="$(find "$dir" -maxdepth 1 -type f -name '*.dump' ! -perm 600 | wc -l)"
  newest="$(find "$dir" -maxdepth 1 -type f -name '*.dump' -printf '%T@ %p\n' | sort -n | tail -n1 | cut -d' ' -f2-)"
  say "[#886 第 1 处] 目录 $dir mode=$(stat -c %a -- "$dir") owner=$(stat -c %U -- "$dir") dump 共 $n 份，非 0600 的 $bad 份"
  [[ -z "$newest" ]] || say "[#886 第 1 处] 最新 $(stat -c '%n mode=%a owner=%U size=%s mtime=%y' -- "$newest")"; }
summary() {
  say "[汇总] 单元本体 sha=$(sha_of "$FRAG") 巡检脚本 sha=$(sha_of "$MON_DST") 备份脚本 sha=$(sha_of "$BAK_DST")"
  say "[汇总] $("$SYSTEMCTL" show -p Result -p ExecMainStatus "$MONITOR_UNIT" 2>/dev/null | tr '\n' ' ')解释器=$(exec_field path "$MONITOR_UNIT")"
  say "[下次备份轮回读] UTC 18:30 之后只读执行：systemctl show -p Result,ExecMainStatus,ExecMainStartTimestamp $BACKUP_UNIT"
  say "[下次备份轮回读] 以及：sha256sum ${BAK_DST#"$ROOT"}（期望 ${WANT[db_backup.sh]}）"; }

case "$SUB" in
  check) preflight; plan_steps; require_ok ;;
  status) preflight; plan_steps || true
    say "[巡检] 最近一轮 Result=$(sv Result) ExecMainStatus=$(sv ExecMainStatus) 解释器=$(exec_field path "$MONITOR_UNIT")"
    say "[备份目录] $BACKUP_ROOT 完整备份 $(find "$BACKUP_ROOT" -mindepth 2 -maxdepth 2 -name complete 2>/dev/null | wc -l) 份，最近 $(latest_backup | xargs -r basename)"
    umask_readback
    if ((${#FAIL[@]})); then for f in "${FAIL[@]}"; do say "前提不符：$f"; done; fi ;;
  apply)
    [[ "$CONFIRM" == --yes ]] || die "apply 会改主机，须写 apply --yes"
    preflight; plan_steps; require_ok
    if (( NEED_UNIT + NEED_MON + NEED_BAK == 0 )); then
      say "三步全部 already，未改动；最近一轮 Result=$(sv Result) ExecMainStatus=$(sv ExecMainStatus)"; umask_readback; summary; exit 0; fi
    align_minute; make_backup
    step_unit || auto_restore ①
    step_mon || auto_restore ②
    step_bak || auto_restore ③
    umask_readback; summary; say "apply 完成（备份留在 $BK，需要时 restore --yes）" ;;
  restore)
    [[ "$CONFIRM" == --yes ]] || die "restore 会改主机，须写 restore --yes"
    BK="$(latest_backup)"; [[ -n "$BK" ]] || die "$BACKUP_ROOT 下没有完整备份"
    say "[回装] 取最近备份 $BK"; do_restore "$BK"; say "restore 完成" ;;
  *) die "未知子命令 $SUB（check|apply --yes|restore --yes|status）" ;;
esac
