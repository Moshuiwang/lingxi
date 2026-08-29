#!/usr/bin/env bash
# 监控脚本的 dry-run 校验（S-RC20-410，Issue #410）：不连真库、不需要真实
# docker 环境即可跑通的部分——环境变量/命令缺失时的拒绝启动行为，以及
# resource_sample.sh 用伪造 docker 可执行文件走一遍完整采样落盘路径。真实
# psql/Supabase 连接、真实容器统计属 L4a，留给 biai-stage 受控验收。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/.." && pwd)
monitoring_dir="${repository_root}/scripts/ops/monitoring"

pass_count=0
fail_count=0

expect_exit_code() {
  local description="$1" expected="$2"
  shift 2
  local actual=0
  "$@" >/tmp/lingxi-monitoring-test-stdout.$$ 2>/tmp/lingxi-monitoring-test-stderr.$$ || actual=$?
  if [[ "${actual}" == "${expected}" ]]; then
    printf 'PASS: %s（退出码 %s）\n' "${description}" "${actual}"
    pass_count=$((pass_count + 1))
  else
    printf 'FAIL: %s（期望退出码 %s，实际 %s）\n' "${description}" "${expected}" "${actual}" >&2
    cat /tmp/lingxi-monitoring-test-stderr.$$ >&2
    fail_count=$((fail_count + 1))
  fi
  rm -f "/tmp/lingxi-monitoring-test-stdout.$$" "/tmp/lingxi-monitoring-test-stderr.$$"
}

# --- db_business_sample.sh：缺 DSN / 缺 psql 拒绝启动（退出码 2） ---------

expect_exit_code "db_business_sample.sh 缺 LINGXI_POSTGRES_DSN 时拒绝启动" 2 \
  env -i PATH="${PATH}" bash "${monitoring_dir}/db_business_sample.sh"

if command -v psql >/dev/null 2>&1; then
  printf 'SKIP: db_business_sample.sh 缺 psql 场景（本机已装 psql，见 deploy/监控告警.md 安装步骤对该场景的说明）\n'
else
  expect_exit_code "db_business_sample.sh DSN 已设但 psql 不可用时拒绝启动" 2 \
    env -i PATH="${PATH}" LINGXI_POSTGRES_DSN="postgresql://fake" \
    bash "${monitoring_dir}/db_business_sample.sh"
fi

# --- push_to_monitoring.sh：缺 MONITORING_DSN / 缺 psql 拒绝启动 ---------

expect_exit_code "push_to_monitoring.sh 缺 MONITORING_DSN 时拒绝启动" 2 \
  env -i PATH="${PATH}" bash "${monitoring_dir}/push_to_monitoring.sh"

if command -v psql >/dev/null 2>&1; then
  printf 'SKIP: push_to_monitoring.sh 缺 psql 场景（本机已装 psql，见 deploy/监控告警.md 安装步骤对该场景的说明）\n'
else
  expect_exit_code "push_to_monitoring.sh DSN 已设但 psql 不可用时拒绝启动" 2 \
    env -i PATH="${PATH}" MONITORING_DSN="postgresql://fake" \
    bash "${monitoring_dir}/push_to_monitoring.sh"
fi

# --- resource_sample.sh：用伪造 docker 走一遍完整采样落盘路径 -----------

work_dir=$(mktemp -d)
cleanup() { rm -rf "${work_dir}"; }
trap cleanup EXIT

fake_bin_dir="${work_dir}/bin"
mkdir -p "${fake_bin_dir}"
cat > "${fake_bin_dir}/docker" <<'FAKE_DOCKER'
#!/bin/sh
# docker stats --no-stream --format '{{json .}}' <name>：$5 是容器名。
if [ "$1" = "stats" ]; then
  name="$5"
  if [ "$name" = "present-container" ]; then
    echo '{"Name":"present-container","CPUPerc":"1.23%","MemUsage":"12.3MiB / 512MiB","MemPerc":"2.4%","NetIO":"1kB / 2kB","BlockIO":"3MB / 4MB","PIDs":"7"}'
    exit 0
  fi
  echo "Error: No such container: ${name}" >&2
  exit 1
fi
exit 1
FAKE_DOCKER
chmod +x "${fake_bin_dir}/docker"

out_dir="${work_dir}/out"
mkdir -p "${out_dir}"

if env -i PATH="${fake_bin_dir}:${PATH}" \
    LINGXI_MONITORING_DIR="${out_dir}" \
    LINGXI_MONITORING_CONTAINERS="present-container missing-container" \
    bash "${monitoring_dir}/resource_sample.sh" >/tmp/lingxi-monitoring-test-stdout.$$ 2>&1; then
  today=$(date -u +%Y%m%d)
  output_file="${out_dir}/resource-${today}.log"
  if [[ -f "${output_file}" ]] && [[ "$(wc -l < "${output_file}")" == "1" ]] \
     && grep -q '"name": *"present-container"' "${output_file}" \
     && grep -q '"containers_unavailable": *\["missing-container"\]' "${output_file}"; then
    printf 'PASS: resource_sample.sh 用伪造 docker 采到一行样本，缺失容器进 containers_unavailable\n'
    pass_count=$((pass_count + 1))
  else
    printf 'FAIL: resource_sample.sh 输出文件内容不符合预期\n' >&2
    cat "${output_file}" >&2 2>/dev/null || true
    fail_count=$((fail_count + 1))
  fi
else
  printf 'FAIL: resource_sample.sh 用伪造 docker 运行失败\n' >&2
  cat /tmp/lingxi-monitoring-test-stdout.$$ >&2
  fail_count=$((fail_count + 1))
fi
rm -f "/tmp/lingxi-monitoring-test-stdout.$$"

# --- push_to_monitoring.sh：伪造 psql 走一遍增量上推 + cursor 幂等路径 -----

push_work_dir=$(mktemp -d)
push_bin_dir="${push_work_dir}/bin"
mkdir -p "${push_bin_dir}"
cat > "${push_bin_dir}/psql" <<'FAKE_PSQL'
#!/bin/sh
# 伪造 psql：把传给 -f 的 SQL 文件另存一份供测试断言，总是成功退出。
prev=""
sqlfile=""
for arg in "$@"; do
  if [ "${prev}" = "-f" ]; then
    sqlfile="${arg}"
  fi
  prev="${arg}"
done
if [ -n "${sqlfile}" ]; then
  cp "${sqlfile}" "${LINGXI_TEST_LAST_SQL}"
fi
exit 0
FAKE_PSQL
chmod +x "${push_bin_dir}/psql"

push_data_dir="${push_work_dir}/data"
mkdir -p "${push_data_dir}"
printf '{"a":1}\n{"a":2}\n' > "${push_data_dir}/resource-20260829.log"
last_sql_file="${push_work_dir}/last_sql_seen.sql"

env -i PATH="${push_bin_dir}:${PATH}" MONITORING_DSN="postgresql://fake" \
  LINGXI_MONITORING_DIR="${push_data_dir}" LINGXI_TEST_LAST_SQL="${last_sql_file}" \
  bash "${monitoring_dir}/push_to_monitoring.sh" >/dev/null 2>&1

cursor_file="${push_data_dir}/.push-state/resource-20260829.log.cursor"
insert_count=$(grep -c "INSERT INTO _lingxi_push_staging" "${last_sql_file}" 2>/dev/null || echo 0)
push_log_lines_after_first=$(wc -l < "${push_data_dir}/.push-state/push.log" 2>/dev/null || echo 0)

if [[ "$(cat "${cursor_file}" 2>/dev/null)" == "2" ]] && [[ "${insert_count}" == "2" ]]; then
  printf 'PASS: push_to_monitoring.sh 首轮把 2 行新样本各生成一条 INSERT，cursor 前移到 2\n'
  pass_count=$((pass_count + 1))
else
  printf 'FAIL: push_to_monitoring.sh 首轮增量推送结果不符合预期（cursor=%s insert_count=%s）\n' \
    "$(cat "${cursor_file}" 2>/dev/null)" "${insert_count}" >&2
  fail_count=$((fail_count + 1))
fi

# 第二轮：源文件没有新增行，cursor 已经等于总行数，应该直接跳过、不再调用
# psql——push.log 行数应该与第一轮之后完全一致。
env -i PATH="${push_bin_dir}:${PATH}" MONITORING_DSN="postgresql://fake" \
  LINGXI_MONITORING_DIR="${push_data_dir}" LINGXI_TEST_LAST_SQL="${last_sql_file}" \
  bash "${monitoring_dir}/push_to_monitoring.sh" >/dev/null 2>&1
push_log_lines_after_second=$(wc -l < "${push_data_dir}/.push-state/push.log" 2>/dev/null || echo 0)

if [[ "${push_log_lines_after_second}" == "${push_log_lines_after_first}" ]]; then
  printf 'PASS: push_to_monitoring.sh 第二轮无新增行时跳过，不重复推送\n'
  pass_count=$((pass_count + 1))
else
  printf 'FAIL: push_to_monitoring.sh 第二轮在没有新增行时仍然推送了一次\n' >&2
  fail_count=$((fail_count + 1))
fi

rm -rf "${push_work_dir}"

printf '\n%s 通过，%s 失败\n' "${pass_count}" "${fail_count}"
if (( fail_count > 0 )); then
  exit 1
fi
