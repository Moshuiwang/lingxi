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
# 不打印它、不写进任何日志——不进 argv/日志是与 `host_health_alert.py` 凭据边界
# 同一条纪律。
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

HOST_NAME="$(hostname)"
NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE_UTC="$(date -u +%Y%m%d)"
OUTPUT_FILE="${OUTPUT_DIR}/db_business-${DATE_UTC}.log"
ERROR_LOG="${STATE_DIR}/db_business_last_error.log"

# 端到端往返探测（分层采集清单「数据库」行「云库形态下追加端到端探测」）：单独
# 一次最简查询 `SELECT 1`，用纳秒时间戳前后打点——不解析 psql `\timing` 的输出
# 格式，那个格式跨版本、跨语言环境不稳定，不适合机器解析。
rt_start_ns=$(date +%s%N)
if ! psql "${LINGXI_POSTGRES_DSN}" -Atc "SELECT 1;" >/dev/null 2>"${ERROR_LOG}"; then
  echo "端到端探测失败：SELECT 1 未成功，业务库当前不可达，本轮不写样本（详见 ${ERROR_LOG}，该文件不含凭据只含 psql 错误文本）" >&2
  exit 1
fi
rt_end_ns=$(date +%s%N)
ROUNDTRIP_MS=$(( (rt_end_ns - rt_start_ns) / 1000000 ))

SQL_QUERY=$(cat <<'SQL'
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

JSON_LINE=$(psql "${LINGXI_POSTGRES_DSN}" \
  -v ON_ERROR_STOP=1 -X -q \
  -v "ts=${NOW_UTC}" -v "host=${HOST_NAME}" -v "window_minutes=${WINDOW_MINUTES}" \
  -v "window_interval=${WINDOW_MINUTES} minutes" -v "roundtrip_ms=${ROUNDTRIP_MS}" \
  -Atc "${SQL_QUERY}" \
  2>"${ERROR_LOG}") || {
  echo "业务聚合查询失败，本轮不写样本（详见 ${ERROR_LOG}，该文件不含凭据只含 psql 错误文本）" >&2
  exit 1
}

if [[ -z "${JSON_LINE}" ]]; then
  echo "业务聚合查询返回空结果，本轮不写样本（不应发生,聚合查询恒返回一个 JSON 对象）" >&2
  exit 1
fi

printf '%s\n' "${JSON_LINE}" >> "${OUTPUT_FILE}"
