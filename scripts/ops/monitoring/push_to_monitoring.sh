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
#
# 口令不进 argv（P1-3，独立审查，与 `db_business_sample.sh` 同一条纪律与同一份
# 实现）：MONITORING_DSN 读进环境变量本身是安全的，但此前脚本会把整串 DSN
# （含明文口令）原样交给 psql 当位置参数——同机其它账号不需要同用户或 root
# 权限就能读任意进程的 `/proc/<pid>/cmdline`，整串带口令的 DSN 一旦进 argv 就
# 等于把数据库密码摊给同机所有账号。做法是"拆分"：只把密码段从 DSN 里剥离，
# 经 `PGPASSWORD` 环境变量传给 psql，DSN 其余部分（host/port/dbname/
# connect_timeout/options 等非密参数）原样保留、继续作为 psql 的位置参数。
set -euo pipefail

# 见上方脚本头注「口令不进 argv」。DSN 没有 `user[:password]@` 形状时（例如
# 测试用的占位 DSN）原样透传，不强行要求密码存在。结果写进全局变量
# `PG_SAFE_DSN`（而不是经 `$(...)` 捕获返回值）：命令替换会在子 shell 里跑，
# 函数内部对 `PGPASSWORD` 的 `export` 不会传回调用它的这个 shell，因此调用方
# 必须以普通语句形式调用本函数，不能包 `$(...)`。
_pg_urldecode() {
  # 密码段按 URI 规则可能被 percent-encode 过；PGPASSWORD 要的是解码后的原始
  # 字节，不能把 %XX 逐字符传给 psql，否则真实密码含特殊字符时会连接失败。
  printf '%b' "${1//%/\\x}"
}

_pg_dsn_without_argv_password() {
  local dsn="$1"
  if [[ "${dsn}" =~ ^(postgres(ql)?://)([^:@/]*)(:([^@/]*))?@(.*)$ ]]; then
    local scheme="${BASH_REMATCH[1]}" user="${BASH_REMATCH[3]}"
    local password="${BASH_REMATCH[5]}" rest="${BASH_REMATCH[6]}"
    if [[ -n "${password}" ]]; then
      PGPASSWORD="$(_pg_urldecode "${password}")"
      export PGPASSWORD
    fi
    PG_SAFE_DSN="${scheme}${user}@${rest}"
  else
    PG_SAFE_DSN="${dsn}"
  fi
}

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

_pg_dsn_without_argv_password "${MONITORING_DSN}"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "${LOG_FILE}"
}

# 把一个源文件里"上一次已推送行数之后"的新增行推给监控库；只有 psql 成功执行
# 完整段 SQL（`ON_ERROR_STOP=1` + 显式外层事务）才前移 cursor。
#
# P1-7（独立审查）：此前整批新增行共享一个 `INSERT ... SELECT FROM 临时表`
# 语句——任何一行解析失败（例如某行不是合法 JSON、或 `layer`/`host`/`ts` 缺失
# 导致 `->>`/`::timestamptz` 转换报错）都会让 `ON_ERROR_STOP` 中止整个外层
# 事务，COMMIT 不会发生，cursor 因此永不前移，变成"永久卡死"：往后每一轮都在
# 同一批坏行上原样重试、永远推不过去，连带这批新增行里其它本来合法的行也一起
# 卡住，不是"坏行拖累好行"应有的隔离粒度。
#
# 改为每一行各自包一个 PL/pgSQL `DO $$ ... EXCEPTION WHEN OTHERS ... END $$;`
# 块：Postgres 给 `EXCEPTION` 子句自动开一个隐式子事务（等价于自动
# SAVEPOINT/ROLLBACK TO SAVEPOINT），一行失败只回滚这一行自己的写入，不影响
# 同一批里其它行、也不影响外层事务——外层事务仍然整体 COMMIT，cursor 按"本轮
# 处理过的总行数"前移，坏行被跳过、不会被下一轮重复读取。失败原因记进临时表
# `_lingxi_push_errors`，本轮结束前用一条打了 `LINGXI_PUSH_ERROR_COUNT=` 前缀
# 的单行 SQL 输出汇总数量（临时切 `\pset` 到 tuples-only/unaligned，用完复原，
# 不影响其它输出的可读性），bash 侧解析这一行、连同"本轮跳过了几行坏数据"一起
# 记进 push.log（本地错误计数行）。
push_file() {
  local file="$1"
  local base cursor_file total_lines already_pushed new_lines_file sql_file pushed_lines
  local psql_output psql_status error_count line_no line

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
    echo "CREATE TEMP TABLE _lingxi_push_errors (line_no int, err text) ON COMMIT DROP;"
    line_no=0
    while IFS= read -r line; do
      line_no=$((line_no + 1))
      [[ -z "${line}" ]] && continue
      printf -- '-- lingxi_push_row %s\n' "${line_no}"
      cat <<'SQLHEAD'
DO $lingxi_do$
BEGIN
  INSERT INTO lingxi_monitoring.sample (layer, host, sampled_at, payload)
  SELECT (raw::jsonb) ->> 'layer', (raw::jsonb) ->> 'host', ((raw::jsonb) ->> 'ts')::timestamptz, raw::jsonb
    FROM (VALUES ($lingxi_push$
SQLHEAD
      printf '%s' "${line}"
      cat <<'SQLTAIL'
$lingxi_push$)) AS _lingxi_row(raw)
   ON CONFLICT (layer, host, sampled_at) DO NOTHING;
EXCEPTION WHEN OTHERS THEN
SQLTAIL
      printf '  INSERT INTO _lingxi_push_errors (line_no, err) VALUES (%s, SQLERRM);\n' "${line_no}"
      cat <<'SQLEND'
END;
$lingxi_do$;
SQLEND
    done < "${new_lines_file}"
    cat <<'SQL'
\pset tuples_only on
\pset format unaligned
SELECT 'LINGXI_PUSH_ERROR_COUNT=' || count(*)::text FROM _lingxi_push_errors;
\pset tuples_only off
\pset format aligned
SQL
    echo "COMMIT;"
  } > "${sql_file}"

  psql_status=0
  psql_output=$(psql "${PG_SAFE_DSN}" -v ON_ERROR_STOP=1 -X -q -f "${sql_file}" 2>&1) || psql_status=$?
  printf '%s\n' "${psql_output}" >> "${LOG_FILE}"

  if (( psql_status == 0 )); then
    error_count=$(printf '%s\n' "${psql_output}" \
      | sed -n 's/^LINGXI_PUSH_ERROR_COUNT=\([0-9]\{1,\}\)$/\1/p' | tail -n1)
    error_count="${error_count:-0}"
    echo "${total_lines}" > "${cursor_file}"
    log "推送成功 file=${base} new_lines=${pushed_lines} skipped_bad_rows=${error_count} cursor=${total_lines}"
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
