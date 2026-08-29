#!/usr/bin/env bash
# 增量上推本机监控样本文件到云端监控库（S-RC20-410，Issue #410，数据落地两层
# 设计第 2 点）。
#
# 只做"增量追加"：每个源文件（`resource-YYYYMMDD.log` / `db_business-YYYYMMDD.log`）
# 用同目录 `.push-state/<文件名>.cursor` 记住已经成功推送的行数，只把新增的行发
# 给监控库；本机文件是源头兜底（数据落地两层设计第 1 点），上推失败不影响它继续
# 增长，下一轮据 cursor 继续重试——不会漏行，也不会重复计数：`lingxi_monitoring.
# sample` 表上 `(layer, host, sampled_at)` 唯一约束是第二道防线，即使 cursor 因为
# 某种异常重推了已经推过的行，数据库端 `ON CONFLICT DO NOTHING` 也会吞掉重复。
#
# 上推失败按设计"静默"：只记日志，退出码仍为 0，不产生飞书告警噪声——本机文件
# 没有丢数据，只是云端可读性延后了一轮，不是需要人立即介入的故障。连续多轮拿不
# 到监控库连接这件事，由 `host_health_alert.py` 的"采样文件停更"阈值检查间接
# 兜底（那条检查看的是本机采样文件是否还在更新，不是云端有没有收到——两者是不
# 同层面的"活性"，各自独立判断，互不替代）。
#
# 为什么不用 psql `\copy`：COPY 的 TEXT/CSV 格式都需要对分隔符、引号、反斜杠转义
# 做额外处理才能安全承载"任意一行 JSON 文本"；改用把每一行原样塞进一条
# dollar-quoted `INSERT ... VALUES ($tag$...$tag$)` 语句，只要样本内容本身不包含
# 字面量 `$lingxi_push$`（我们自己生成的 JSON，只含数值/容器名/状态字符串,这个
# 假设成立)，就不需要考虑任何转义规则，同时仍然只用 bash + psql，零新依赖。
set -euo pipefail

if [[ -z "${MONITORING_DSN:-}" ]]; then
  echo "缺少环境变量 MONITORING_DSN：push_to_monitoring.sh 需要监控库连接串才能上推，未设置时拒绝启动" >&2
  exit 2
fi

command -v psql >/dev/null 2>&1 || {
  echo "缺少命令：psql（需要 postgresql-client，安装步骤见 deploy/监控告警.md）" >&2
  exit 2
}

OUTPUT_DIR="${LINGXI_MONITORING_DIR:-/var/log/lingxi/monitoring}"
STATE_DIR="${OUTPUT_DIR}/.push-state"
LOG_FILE="${LINGXI_MONITORING_PUSH_LOG:-${STATE_DIR}/push.log}"

install -d -m 750 "${OUTPUT_DIR}" "${STATE_DIR}"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "${LOG_FILE}"
}

# 把一个源文件里"上一次已推送行数之后"的新增行推给监控库；只有 psql 成功执行
# 完这整段 SQL（`ON_ERROR_STOP=1` + 显式事务）才前移 cursor。
push_file() {
  local file="$1"
  local base cursor_file total_lines already_pushed new_lines_file sql_file pushed_lines

  base=$(basename "${file}")
  cursor_file="${STATE_DIR}/${base}.cursor"
  total_lines=$(wc -l < "${file}" | tr -d ' ')

  already_pushed=0
  if [[ -f "${cursor_file}" ]]; then
    already_pushed=$(cat "${cursor_file}")
    [[ "${already_pushed}" =~ ^[0-9]+$ ]] || already_pushed=0
  fi

  if (( total_lines <= already_pushed )); then
    return 0
  fi

  new_lines_file=$(mktemp)
  sql_file=$(mktemp)

  tail -n "+$((already_pushed + 1))" "${file}" > "${new_lines_file}"
  pushed_lines=$(wc -l < "${new_lines_file}" | tr -d ' ')

  {
    echo "BEGIN;"
    echo "CREATE TEMP TABLE _lingxi_push_staging (raw text) ON COMMIT DROP;"
    while IFS= read -r line; do
      [[ -z "${line}" ]] && continue
      printf 'INSERT INTO _lingxi_push_staging (raw) VALUES ($lingxi_push$%s$lingxi_push$);\n' "${line}"
    done < "${new_lines_file}"
    cat <<'SQL'
INSERT INTO lingxi_monitoring.sample (layer, host, sampled_at, payload)
SELECT (raw::jsonb) ->> 'layer', (raw::jsonb) ->> 'host', ((raw::jsonb) ->> 'ts')::timestamptz, raw::jsonb
  FROM _lingxi_push_staging
 ON CONFLICT (layer, host, sampled_at) DO NOTHING;
SQL
    echo "COMMIT;"
  } > "${sql_file}"

  if psql "${MONITORING_DSN}" -v ON_ERROR_STOP=1 -X -q -f "${sql_file}" >> "${LOG_FILE}" 2>&1; then
    echo "${total_lines}" > "${cursor_file}"
    log "推送成功 file=${base} new_lines=${pushed_lines} cursor=${total_lines}"
  else
    log "推送失败 file=${base} new_lines=${pushed_lines}（cursor 不前移，下一轮重试；本机文件未受影响）"
  fi
  rm -f "${new_lines_file}" "${sql_file}"
}

shopt -s nullglob
files=("${OUTPUT_DIR}"/resource-*.log "${OUTPUT_DIR}"/db_business-*.log)
shopt -u nullglob

if (( ${#files[@]} == 0 )); then
  log "没有发现待推送的本机样本文件（${OUTPUT_DIR}），本轮跳过"
  exit 0
fi

for source_file in "${files[@]}"; do
  push_file "${source_file}"
done

exit 0
