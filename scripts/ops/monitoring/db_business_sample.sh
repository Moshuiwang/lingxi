#!/usr/bin/env bash
# 数据库/业务层单轮采样（S-RC20-410，Issue #410）：一次调用 = 一行 JSON 样本，
# 追加到按 UTC 日期切分的本机文件。数据库连接数/慢查询/表大小与业务聚合（任务量/
# 成功率/耗时分位/队列深度/task.error_kind 分布/token 用量）合并成一次 psql 往返
# ——它们读的是同一个 Supabase 云托管实例（#40/#411 裁定，stage/生产数据库已迁移
# 云托管，见 `deploy/README.md`「数据库凭据源」），没有理由为了"层"概念上的区分
# 建两条连接；往返延迟本身也是这一层要观测的指标之一（分层采集清单「数据库」行
# 「云库形态下追加端到端探测」）。
#
# 零新依赖：只用 bash 与宿主已装的 psql 客户端（Supabase DSN 直连，不走 docker
# exec 进本地容器——那是旧的本地库测试姿势，见 `scripts/ops/backup_restore_drill.sh`
# 与 `scripts/ops/onboarding_preflight.sh` 的 `lingxi-test-db`，与云托管业务库是
# 两回事）。DSN 只经环境变量 `LINGXI_POSTGRES_DSN` 传入，脚本不接受它作为参数、
# 不打印它、不写进任何日志。
#
# 口令不进 argv（P1-3，独立审查）：DSN 读进环境变量本身是安全的，但此前脚本会
# 把整串 DSN（含明文口令）原样交给 psql 当位置参数——同机其它账号不需要同用户
# 或 root 权限就能读任意进程的 `/proc/<pid>/cmdline`（读 `/proc/<pid>/environ`
# 才需要那道门槛），整串带口令的 DSN 一旦进 argv 就等于把数据库密码摊给同机
# 所有账号。做法是"拆分"：只把密码段从 DSN 里剥离，经 `PGPASSWORD` 环境变量
# 传给 psql（libpq 官方支持的姿势），DSN 其余部分（host/port/dbname/
# connect_timeout/options 等非密参数）原样保留、继续作为 psql 的位置参数——
# 不改变这些参数的既有语义、不需要重新拼装整条连接串，也不需要新增
# `~/.pg_service.conf` 这类额外安装步骤。见下方 `_pg_dsn_without_argv_password`。
#
# 输出格式：一行 JSON，字段 `{ts, host, layer:"db_business", metrics:{database:
# {...}, business:{...}}}`；database/business 合并成一个 `layer` 标签而不是拆成
# 两行，理由见上——两段天然同源同时刻，拆分只会让下游消费者需要额外按时间戳对齐
# 才能拼回一次观测。
#
# 只读性质：全部 SQL 都是 SELECT（含只读 `pg_stat_activity`/`pg_class` 系统视图），
# 不写业务表一个字节；`token_usage`/`error_kind` 等字段本身不含用户消息正文，
# 只有计数、耗时、分位数与按内部 user_id（不透传姓名/open_id）的聚合。
set -euo pipefail

# 见上方脚本头注「口令不进 argv」。DSN 没有 `user[:password]@` 形状时（例如
# 测试用的占位 DSN）原样透传，不强行要求密码存在。
_pg_urldecode() {
  # 密码段按 URI 规则可能被 percent-encode 过；PGPASSWORD 要的是解码后的原始
  # 字节，不能把 %XX 逐字符传给 psql，否则真实密码含特殊字符时会连接失败。
  printf '%b' "${1//%/\\x}"
}

# 结果写进全局变量 `PG_SAFE_DSN`（而不是 `printf` 返回值经 `$(...)` 捕获）：
# 命令替换会在子 shell 里跑，函数内部对 `PGPASSWORD` 的 `export` 不会传回
# 调用它的这个 shell——之前一版就是这样悄悄把密码丢在子 shell 里，PGPASSWORD
# 从未真正被主 shell 看到（本地验证发现，见 PR 描述）。调用方必须以普通语句
# 形式调用本函数（不包 `$(...)`），才能让这里对 PGPASSWORD/PG_SAFE_DSN 的赋值
# 生效在当前 shell。
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

if [[ -z "${LINGXI_POSTGRES_DSN:-}" ]]; then
  echo "缺少环境变量 LINGXI_POSTGRES_DSN：db_business_sample.sh 需要业务库连接串才能采样，未设置时拒绝启动（不静默跳过——那样会造成「看起来在跑但从来没写过数据」的假象）" >&2
  exit 2
fi

command -v psql >/dev/null 2>&1 || {
  echo "缺少命令：psql（需要 postgresql-client，安装步骤见 deploy/监控告警.md）" >&2
  exit 2
}

OUTPUT_DIR="${LINGXI_MONITORING_DIR:-/var/log/lingxi/monitoring}"
STATE_DIR="${OUTPUT_DIR}/.state"
WINDOW_MINUTES="${LINGXI_MONITORING_WINDOW_MINUTES:-15}"

if ! [[ "${WINDOW_MINUTES}" =~ ^[0-9]+$ ]]; then
  echo "LINGXI_MONITORING_WINDOW_MINUTES 必须是正整数分钟数，实际取到：${WINDOW_MINUTES}" >&2
  exit 2
fi

install -d -m 750 "${OUTPUT_DIR}" "${STATE_DIR}"

# ---- 样本文件保留上限（对抗审查 2026-09-02 P3）---------------------------
# `/var/log/lingxi/monitoring` 下的 `resource-YYYYMMDD.log` /
# `db_business-YYYYMMDD.log` 是**每天一个新文件**，此前没有任何东西回收它们：
# `deploy/lingxi-container-logs.logrotate` 只管六个容器日志，这个目录不在它的
# 名单里。采样每分钟一轮、常驻运行，磁盘占用因此单调增长，直到把宿主机写满
# ——而写满宿主机会同时打掉容器日志收集与业务本身。
#
# 这里不引入 logrotate 配置（那要新增一个安装步骤，装没装不可见），改为**谁写
# 谁清**：每轮采样顺手删掉自己那一族里超过保留天数的旧文件。只按文件名前缀匹配
# 本脚本自己产出的那一族，绝不用通配删整个目录。
LINGXI_MONITORING_RETENTION_DAYS="${LINGXI_MONITORING_RETENTION_DAYS:-30}"
if [[ "${LINGXI_MONITORING_RETENTION_DAYS}" =~ ^[0-9]+$ ]] \
  && (( LINGXI_MONITORING_RETENTION_DAYS > 0 )); then
  find "${OUTPUT_DIR}" -maxdepth 1 -type f -name 'db_business-*.log' \
    -mtime "+${LINGXI_MONITORING_RETENTION_DAYS}" -delete 2>/dev/null || true
fi

_pg_dsn_without_argv_password "${LINGXI_POSTGRES_DSN}"

HOST_NAME="$(hostname)"
NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE_UTC="$(date -u +%Y%m%d)"
OUTPUT_FILE="${OUTPUT_DIR}/db_business-${DATE_UTC}.log"
ERROR_LOG="${STATE_DIR}/db_business_last_error.log"

# 端到端往返探测（分层采集清单「数据库」行「云库形态下追加端到端探测」）：单独
# 一次最简查询 `SELECT 1`，用纳秒时间戳前后打点——不解析 psql `\timing` 的输出
# 格式，那个格式跨版本、跨语言环境不稳定，不适合机器解析。
rt_start_ns=$(date +%s%N)
if ! psql "${PG_SAFE_DSN}" -Atc "SELECT 1;" >/dev/null 2>"${ERROR_LOG}"; then
  echo "端到端探测失败：SELECT 1 未成功，业务库当前不可达，本轮不写样本（详见 ${ERROR_LOG}，该文件不含凭据只含 psql 错误文本）" >&2
  exit 1
fi
rt_end_ns=$(date +%s%N)
ROUNDTRIP_MS=$(( (rt_end_ns - rt_start_ns) / 1000000 ))

SQL_QUERY=$(cat <<'SQL'
-- P1-8（独立审查）：DSN 本身沿用与业务连接同一份 Supabase 连接串，携带
-- `lingxi.adapters.postgres` 那套 3 秒 statement_timeout / 2 秒 lock_timeout
-- 启动参数（见上方脚本头注「口令不进 argv」段与 `deploy/README.md`「数据库
-- 凭据源」）——那是给正式业务的短查询定的边界，本脚本这条聚合查询要在同一次
-- 往返里跑多个窗口聚合（状态分布/耗时分位/token 用量按用户聚合等），远比一条
-- 业务语句重，3 秒经常不够、会被 DSN 继承的启动参数打断。这里在批次最前面显式
-- `SET`：会话级设置覆盖启动参数（同一条连接内，后设的会话参数生效），只作用
-- 于这一次 psql 调用、不影响其它任何连接。30 秒是"给一次周期性只读聚合留够
-- 裕量、又不至于长期占着一条慢连接"的量级选择；lock_timeout 保持 5 秒（比默认
-- 2 秒宽松，但聚合查询本身不加任何锁，这里放宽是为了不因为与其它只读会话的
-- 偶发争用而误伤，不是因为这条查询需要真的持锁）。
SET statement_timeout = '30s';
SET lock_timeout = '5s';

WITH window_bounds AS (
  SELECT now() - (:'window_interval')::interval AS window_start, now() AS window_end
),
task_window AS (
  SELECT t.* FROM task t, window_bounds w
   WHERE t.created_at >= w.window_start AND t.created_at < w.window_end
),
delivery_window AS (
  SELECT d.* FROM task_delivery_event d, window_bounds w
   WHERE d.event_type = 'terminal' AND d.created_at >= w.window_start AND d.created_at < w.window_end
)
SELECT jsonb_build_object(
  'ts', :'ts',
  'host', :'host',
  'layer', 'db_business',
  'metrics', jsonb_build_object(
    'window_minutes', (:'window_minutes')::int,
    'database', jsonb_build_object(
      'roundtrip_ms', (:'roundtrip_ms')::int,
      'connections_total', (SELECT count(*) FROM pg_stat_activity),
      'connections_by_state', (
        SELECT COALESCE(jsonb_object_agg(state_key, cnt), '{}'::jsonb)
          FROM (SELECT COALESCE(state, 'unknown') AS state_key, count(*) AS cnt
                  FROM pg_stat_activity GROUP BY 1) s
      ),
      -- "慢查询" 用当前仍在跑、且已经跑了超过 3 秒的 active 会话数近似——与
      -- `lingxi.adapters.postgres` 固定的 statement_timeout=3s 上界呼应：正式
      -- 业务连接理论上不该有会话超过这个边界还在 active，出现即值得关注（迁移/
      -- 运维一次性连接不受这个上界约束，可能造成个别误报，属于近似指标接受的
      -- 代价，不是精确的 pg_stat_statements 慢查询日志）。
      'active_long_running_count', (
        SELECT count(*) FROM pg_stat_activity
         WHERE state = 'active' AND query_start IS NOT NULL AND now() - query_start > interval '3 seconds'
      ),
      'database_size_bytes', pg_database_size(current_database()),
      'table_sizes_bytes', (
        SELECT COALESCE(jsonb_object_agg(c.relname, pg_total_relation_size(c.oid)), '{}'::jsonb)
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND c.relname IN ('task', 'task_delivery_event', 'conversation', 'inbound_event',
                              'queue_failure_notice', 'app_user')
      )
    ),
    'business', jsonb_build_object(
      'window_start', (SELECT window_start FROM window_bounds),
      'window_end', (SELECT window_end FROM window_bounds),
      'task_status_counts', (
        SELECT COALESCE(jsonb_object_agg(status, cnt), '{}'::jsonb)
          FROM (SELECT status, count(*) AS cnt FROM task_window GROUP BY status) s
      ),
      -- task.error_kind 分布：当前仓库尚未落地按百炼限流细分错误码的字段（见
      -- issue #410「百炼层补充」②，属于未来另一批产品代码改动），这里是"零产品
      -- 代码改动"前提下能拿到的最细粒度失败分类信号——飞书发送失败、模型会话
      -- 失败等目前统一落在 session_failed 等既有 error_kind 取值里,不能单独
      -- 拆出"是不是被限流了"。
      'task_error_kind_counts', (
        SELECT COALESCE(jsonb_object_agg(COALESCE(error_kind, 'none'), cnt), '{}'::jsonb)
          FROM (SELECT error_kind, count(*) AS cnt FROM task_window GROUP BY error_kind) s
      ),
      'task_duration_seconds_percentiles', (
        SELECT jsonb_build_object(
          'p50', percentile_cont(0.5) WITHIN GROUP (ORDER BY d),
          'p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY d),
          'p99', percentile_cont(0.99) WITHIN GROUP (ORDER BY d),
          'sample_count', count(d)
        )
        FROM (
          SELECT EXTRACT(EPOCH FROM (ended_at - started_at)) AS d
            FROM task_window
           WHERE started_at IS NOT NULL AND ended_at IS NOT NULL
        ) durations
      ),
      -- 队列深度/最老排队时长是"此刻"快照，不是窗口聚合——排队积压看的是现状，
      -- 不是过去 N 分钟内出现过多少次,与上面几段窗口聚合是两种不同的问法。
      'queue_depth', (SELECT count(*) FROM task WHERE status = 'queued'),
      'queue_oldest_wait_seconds', (
        SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(scheduled_at))), 0) FROM task WHERE status = 'queued'
      ),
      'running_count', (SELECT count(*) FROM task WHERE status = 'running'),
      'awaiting_delivery_count', (SELECT count(*) FROM task WHERE status = 'awaiting_delivery'),
      'delivery_terminal_kind_counts', (
        SELECT COALESCE(jsonb_object_agg(COALESCE(terminal_kind, 'unknown'), cnt), '{}'::jsonb)
          FROM (SELECT terminal_kind, count(*) AS cnt FROM delivery_window GROUP BY terminal_kind) s
      ),
      -- token 用量按用户日聚合（issue #410 分层采集清单「百炼」行）：这里刻意用
      -- "当天 UTC 零点至今"而不是采样窗口，每一轮都是当天累计的最新快照，不是
      -- 窗口内增量——一天里任意一轮读到的都是"目前为止今天全部"，AI 巡检读最后
      -- 一轮即可得到当天完整值,不需要把全天多轮样本再自己累加一次。user_id 是
      -- 内部不透明标识（ULID），不是姓名/open_id/手机号，`postgres_daily_report.py`
      -- 那份"不把 user_id 带出"的收紧是因为那条路径要进飞书群消息正文；这里落的
      -- 是监控库,不面向最终用户展示,按用户归因是 issue 明确要求的能力。
      'token_usage_by_user_today_utc', (
        SELECT COALESCE(jsonb_agg(t), '[]'::jsonb) FROM (
          SELECT user_id,
                 count(*) AS task_count,
                 COALESCE(SUM((token_usage->>'input_tokens')::bigint), 0) AS input_tokens,
                 COALESCE(SUM((token_usage->>'output_tokens')::bigint), 0) AS output_tokens,
                 COALESCE(SUM((token_usage->>'cache_creation_input_tokens')::bigint), 0) AS cache_creation_tokens,
                 COALESCE(SUM((token_usage->>'cache_read_input_tokens')::bigint), 0) AS cache_read_tokens
            FROM task
           WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
             AND token_usage IS NOT NULL
           GROUP BY user_id
        ) t
      )
    )
  )
);
SQL
)

# 用 -f 读临时文件，不用 -c 传整串（本地验证发现的独立缺陷）：psql 文档明确
# `-c`/`--command` 的参数"必须是服务器可以完全解析的命令字符串（即不含任何
# psql 专属特性），或者是单条反斜杠命令"——`:'ts'`/`:'host'` 这类变量替换是
# psql 专属特性，`-c` 从不做这层替换，字面量 `:` 会原样发给服务器变成语法
# 错误。改为把 `SQL_QUERY` 写进一个仅当前用户可读的临时文件、经 `-f` 执行——
# 变量替换只在 psql 读脚本文件/标准输入时才生效，`-c` 单命令模式生来不支持，
# 与本次改动无关，是这条查询此前从未真正跑通过的独立缺陷（P1-8 顺手发现，见
# 本批报告）。临时文件只含 SQL 文本，不含任何凭据，用完立即删除。
SQL_FILE=$(mktemp)
trap 'rm -f "${SQL_FILE}"' EXIT
printf '%s\n' "${SQL_QUERY}" > "${SQL_FILE}"

JSON_LINE=$(psql "${PG_SAFE_DSN}" \
  -v ON_ERROR_STOP=1 -X -q \
  -v "ts=${NOW_UTC}" -v "host=${HOST_NAME}" -v "window_minutes=${WINDOW_MINUTES}" \
  -v "window_interval=${WINDOW_MINUTES} minutes" -v "roundtrip_ms=${ROUNDTRIP_MS}" \
  -At -f "${SQL_FILE}" \
  2>"${ERROR_LOG}") || {
  echo "业务聚合查询失败，本轮不写样本（详见 ${ERROR_LOG}，该文件不含凭据只含 psql 错误文本）" >&2
  exit 1
}

if [[ -z "${JSON_LINE}" ]]; then
  echo "业务聚合查询返回空结果，本轮不写样本（不应发生,聚合查询恒返回一个 JSON 对象）" >&2
  exit 1
fi

printf '%s\n' "${JSON_LINE}" >> "${OUTPUT_FILE}"
