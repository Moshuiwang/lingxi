#!/usr/bin/env bash
# build_host_maintenance_bundle.sh —— 生成宿主维护包（#908；host_maintenance_install.sh 的输入目录）。
# 用法：bash scripts/ops/build_host_maintenance_bundle.sh --out <目录> [--ref <git 引用，默认 HEAD>] [--known-from <git 引用>]
# 产物（全部取自 <ref> 的仓库内容，不手工拼）：
#   host_maintenance_install.sh、lingxi-host-monitor.service、host_health_alert.py、db_backup.sh  目标版本四份
#   KNOWN_SHAS   已知版本清单，每行「sha256  文件名」：单元本体、巡检脚本、备份脚本三份各自在 <ref> 的 Git 历史与
#                main 历史（--known-from，缺省依次取 origin/main、main，都取不到即失败）里出现过的每个版本的并集；
#                宿主在位文件只要是其中之一，就算「已知旧版」。为什么要并入 main：release/* 由 squash 同步，
#                正式 tag 的历史不含 main 上的中间版本（例如已上生产的 #907 巡检脚本），只取 <ref> 会把它们判成来历不明
#   SHA256SUMS   上述五份文件（含 KNOWN_SHAS）的 sha256；KNOWN_SHAS 被改动，安装脚本即停
# 历史不完整（浅克隆）时已知版本会漏，直接拒绝生成。输出目录须不存在或为空，权限 0700。
set -euo pipefail
export LC_ALL=C
umask 077

REF=HEAD; OUT=""; KNOWN_FROM=""
while (( $# )); do
  case "$1" in
    --ref) [[ $# -ge 2 ]] || { echo "错误：--ref 后须跟 git 引用" >&2; exit 2; }; REF="$2"; shift 2 ;;
    --out) [[ $# -ge 2 ]] || { echo "错误：--out 后须跟目录" >&2; exit 2; }; OUT="$2"; shift 2 ;;
    --known-from) [[ $# -ge 2 ]] || { echo "错误：--known-from 后须跟 git 引用" >&2; exit 2; }; KNOWN_FROM="$2"; shift 2 ;;
    *) echo "用法：$0 --out <目录> [--ref <git 引用>] [--known-from <git 引用>]" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT" ]] || { echo "用法：$0 --out <目录> [--ref <git 引用>] [--known-from <git 引用>]" >&2; exit 2; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
git_() { git -C "$REPO" "$@"; }
git_ rev-parse --verify --quiet "$REF^{commit}" >/dev/null || { echo "错误：找不到 git 引用 $REF" >&2; exit 1; }
[[ "$(git_ rev-parse --is-shallow-repository)" == false ]] || { echo "错误：仓库是浅克隆，历史版本会漏；先 git fetch --unshallow" >&2; exit 1; }
# main 历史来源：显式给的必须解析得出；缺省依次取 origin/main、main，都取不到就失败（不静默退化成只看 <ref>）
if [[ -n "$KNOWN_FROM" ]]; then
  git_ rev-parse --verify --quiet "$KNOWN_FROM^{commit}" >/dev/null || { echo "错误：找不到 --known-from 引用 $KNOWN_FROM" >&2; exit 1; }
else
  for cand in origin/main main; do
    if git_ rev-parse --verify --quiet "$cand^{commit}" >/dev/null; then KNOWN_FROM="$cand"; break; fi
  done
  [[ -n "$KNOWN_FROM" ]] || { echo "错误：取不到 main 历史（origin/main 与 main 都不存在）；先 git fetch origin main，或用 --known-from 指定" >&2; exit 1; }
fi
if [[ -e "$OUT" ]]; then
  [[ -d "$OUT" && -z "$(ls -A "$OUT")" ]] || { echo "错误：输出目录 $OUT 已存在且非空" >&2; exit 1; }
fi
mkdir -p "$OUT"; chmod 700 "$OUT"

# 文件名 → 仓库内路径（安装脚本按文件名认，包内不带目录）
declare -A SRC=(
  [host_maintenance_install.sh]=scripts/ops/host_maintenance_install.sh
  [lingxi-host-monitor.service]=deploy/monitoring-units/lingxi-host-monitor.service
  [host_health_alert.py]=scripts/ops/host_health_alert.py
  [db_backup.sh]=scripts/ops/db_backup.sh
)
for name in "${!SRC[@]}"; do git_ show "$REF:${SRC[$name]}" > "$OUT/$name"; done
chmod 644 "$OUT"/*

: > "$OUT/KNOWN_SHAS.tmp"
declare -A SOURCE_COUNT=()
for src_ref in "$REF" "$KNOWN_FROM"; do
  : > "$OUT/KNOWN_SHAS.part"
  for name in lingxi-host-monitor.service host_health_alert.py db_backup.sh; do
    # --follow 跟改名；--name-only 给出该提交里的路径。合并提交不带差异，其版本由引入它的提交覆盖。
    git_ log --follow --format='C %H' --name-only "$src_ref" -- "${SRC[$name]}" | {
      commit=""
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if [[ "$line" == "C "* ]]; then commit="${line#C }"; continue; fi
        # 该提交里此路径被删除时取不到内容，跳过
        if blob="$(git_ show "$commit:$line" 2>/dev/null | sha256sum | cut -d' ' -f1)" && git_ cat-file -e "$commit:$line" 2>/dev/null; then
          printf '%s  %s\n' "$blob" "$name"
        fi
      done; } >> "$OUT/KNOWN_SHAS.part"
  done
  SOURCE_COUNT["$src_ref"]="$(sort -u "$OUT/KNOWN_SHAS.part" | wc -l)"
  cat "$OUT/KNOWN_SHAS.part" >> "$OUT/KNOWN_SHAS.tmp"
done
rm -f "$OUT/KNOWN_SHAS.part"
sort -u "$OUT/KNOWN_SHAS.tmp" > "$OUT/KNOWN_SHAS"; rm -f "$OUT/KNOWN_SHAS.tmp"
[[ -s "$OUT/KNOWN_SHAS" ]] || { echo "错误：已知版本清单为空" >&2; exit 1; }
for name in lingxi-host-monitor.service host_health_alert.py db_backup.sh; do
  grep -q "  $name\$" "$OUT/KNOWN_SHAS" || { echo "错误：已知版本清单缺 $name 的任何版本" >&2; exit 1; }
done

( cd "$OUT" && sha256sum lingxi-host-monitor.service host_health_alert.py db_backup.sh host_maintenance_install.sh KNOWN_SHAS > SHA256SUMS )
echo "维护包已生成：$OUT（来源 $REF = $(git_ rev-parse "$REF^{commit}")）"
echo "已知版本来源：$REF 历史 ${SOURCE_COUNT[$REF]} 条 ∪ $KNOWN_FROM 历史 ${SOURCE_COUNT[$KNOWN_FROM]} 条（$KNOWN_FROM = $(git_ rev-parse "$KNOWN_FROM^{commit}")）"
echo "已知版本 $(wc -l < "$OUT/KNOWN_SHAS") 条；清单 $(wc -l < "$OUT/SHA256SUMS") 行"
