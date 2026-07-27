#!/usr/bin/env bash
# #16 的真实 PostgreSQL 约束测试；不访问任何业务系统。
set -euo pipefail

container_name=${LINGXI_POSTGRES_CONTAINER:-lingxi-test-postgres}
database_name=${LINGXI_POSTGRES_DATABASE:-lingxi_test}
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

psql_in_container() {
  docker exec "${container_name}" psql -q -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" "$@"
}

psql_in_container -c 'DROP TABLE IF EXISTS onboarding_progress CASCADE; DROP TABLE IF EXISTS inbound_event CASCADE; DROP TABLE IF EXISTS app_user CASCADE;'
docker exec -i "${container_name}" psql -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" \
  < "${repository_root}/migrations/001_create_app_user.sql"
docker exec -i "${container_name}" psql -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" \
  < "${repository_root}/migrations/002_create_onboarding_progress.sql"

first_insert=$(psql_in_container -At -c "
  SELECT record_authorized_identity(
    'evt_authorization_01', 'usr_test_01', 'ou_test_user', 'user_test_user',
    'union_test_user', '测试用户', '测试部门', 'tenant_test', 'zh-CN'
  );
")

[[ "${first_insert}" == 'usr_test_01' ]] || {
  printf '首次授权未返回预期的身份记录。\n' >&2
  exit 1
}

second_insert=$(psql_in_container -At -c "
  SELECT record_authorized_identity(
    'evt_authorization_01', 'usr_test_02', 'ou_test_user', 'user_test_user',
    'union_test_user', '测试用户', '测试部门', 'tenant_test', 'zh-CN'
  );
")

[[ "${second_insert}" == 'usr_test_01' ]] || {
  printf '重复授权回调未返回首次身份结果。\n' >&2
  exit 1
}

[[ "$(psql_in_container -At -c 'SELECT count(*) FROM app_user;')" == '1' ]] || {
  printf '重复授权回调创建了第二条身份记录。\n' >&2
  exit 1
}

if psql_in_container -c "
  INSERT INTO app_user (id, feishu_open_id, provisioning_state)
  VALUES ('usr_incomplete', 'ou_incomplete', 'matching');
" >/dev/null 2>&1; then
  printf '数据库允许写入不完整身份资料。\n' >&2
  exit 1
fi

for provisioning_state in guest authorizing; do
  if psql_in_container -c "
    INSERT INTO app_user (id, feishu_open_id, provisioning_state)
    VALUES ('usr_incomplete_${provisioning_state}', 'ou_incomplete_${provisioning_state}', '${provisioning_state}');
  " >/dev/null 2>&1; then
    printf '数据库允许 %s 状态留下半条身份资料。\n' "${provisioning_state}" >&2
    exit 1
  fi
done

[[ "$(psql_in_container -At -c "
  SELECT count(*)
    FROM information_schema.columns
   WHERE table_name = 'app_user'
     AND column_name IN ('permission_record_id', 'permission_version', 'permission_checked_at');
")" == '0' ]] || {
  printf '身份切片不应预置权限记录字段。\n' >&2
  exit 1
}

[[ "$(psql_in_container -At -c "
  SELECT count(*)
    FROM information_schema.columns
   WHERE table_name = 'onboarding_progress'
     AND column_name IN (
       'subject_hash', 'chat_hash', 'card_nonce', 'step', 'expires_at', 'created_at'
     );
")" == '6' ]] || {
  printf '开通进度表缺少最小状态字段。\n' >&2
  exit 1
}

[[ "$(psql_in_container -At -c "
  SELECT count(*)
    FROM information_schema.columns
   WHERE table_name = 'onboarding_progress'
     AND column_name ~ '(open_id|user_id|union_id|name|permission|token|content)';
")" == '0' ]] || {
  printf '开通进度表不应保存身份、权限、令牌或业务内容。\n' >&2
  exit 1
}

printf 'PostgreSQL 身份约束：通过\n'
