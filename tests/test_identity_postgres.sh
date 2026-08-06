#!/usr/bin/env bash
# 针对**测试资产** migrations/testing/001-003 的历史约束测试；不访问任何业务系统。
# 正式表（006-008）的真库断言在 tests/test_identity_postgres_records.py。
# 本脚本断言的旧 app_user 形状（无 permission_* 列等）只对 001 成立；
# 随测试资产清退一并退休（migrations/README.md）。
set -euo pipefail

container_name=${LINGXI_POSTGRES_CONTAINER:-lingxi-test-postgres}
database_name=${LINGXI_POSTGRES_DATABASE:-lingxi_test}
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

psql_in_container() {
  docker exec "${container_name}" psql -q -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" "$@"
}

psql_in_container -c 'DROP TABLE IF EXISTS feishu_user_refresh_token CASCADE; DROP TABLE IF EXISTS onboarding_progress CASCADE; DROP TABLE IF EXISTS inbound_event CASCADE; DROP TABLE IF EXISTS app_user CASCADE;'
docker exec -i "${container_name}" psql -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" \
  < "${repository_root}/migrations/testing/001_create_app_user.sql"
docker exec -i "${container_name}" psql -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" \
  < "${repository_root}/migrations/testing/002_create_onboarding_progress.sql"
docker exec -i "${container_name}" psql -v ON_ERROR_STOP=1 -U postgres -d "${database_name}" \
  < "${repository_root}/migrations/testing/003_create_feishu_user_refresh_token.sql"

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

# #19 测试凭据库只允许一个密文续期凭据：不依赖 app_user，也不为短期令牌留字段。
psql_in_container -c "
  INSERT INTO feishu_user_refresh_token
    (feishu_open_id, encrypted_refresh_token, scope, refresh_at, refresh_token_expires_at)
  VALUES
    ('ou_probe', decode('0102', 'hex'), 'offline_access', now() - interval '1 minute', now() + interval '1 hour');
  INSERT INTO feishu_user_refresh_token
    (feishu_open_id, encrypted_refresh_token, scope, refresh_at, refresh_token_expires_at)
  VALUES
    ('ou_probe', decode('0304', 'hex'), 'offline_access', now() + interval '10 minutes', now() + interval '2 hours')
  ON CONFLICT (feishu_open_id) DO UPDATE SET
    encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
    refresh_at = EXCLUDED.refresh_at,
    refresh_token_expires_at = EXCLUDED.refresh_token_expires_at;
" >/dev/null

[[ "$(psql_in_container -At -c "SELECT count(*) FROM feishu_user_refresh_token WHERE feishu_open_id = 'ou_probe';")" == '1' ]] || {
  printf '测试续期凭据没有保持为唯一一条。\n' >&2
  exit 1
}

[[ "$(psql_in_container -At -c "SELECT encode(encrypted_refresh_token, 'hex') FROM feishu_user_refresh_token WHERE feishu_open_id = 'ou_probe';")" == '0304' ]] || {
  printf '测试续期凭据轮换没有替换旧密文。\n' >&2
  exit 1
}

[[ "$(psql_in_container -At -c "
  SELECT count(*)
    FROM information_schema.columns
   WHERE table_name = 'feishu_user_refresh_token'
     AND column_name ~ '(access_token|authorization_code|client_secret|plain)';
")" == '0' ]] || {
  printf '测试续期凭据表不应保存短期令牌、授权码或明文字段。\n' >&2
  exit 1
}

[[ "$(psql_in_container -At -c "
  SELECT count(*)
    FROM information_schema.table_constraints
   WHERE table_name = 'feishu_user_refresh_token' AND constraint_type = 'FOREIGN KEY';
")" == '0' ]] || {
  printf '测试续期凭据不应依赖正式用户建档。\n' >&2
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
