#!/usr/bin/env bash
# 只读验证：不输出 App Secret、tenant_access_token、姓名或 open_user_id。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace_dir=$(cd -- "${script_dir}/.." && pwd)
env_file="${workspace_dir}/.env"

if [[ ! -f "${env_file}" ]]; then
  echo "未找到 ${env_file}。请先创建并填写配置。" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

for required_var in FEISHU_APP_ID FEISHU_APP_SECRET; do
  if [[ -z "${!required_var:-}" ]]; then
    echo "${required_var} 尚未填写。" >&2
    exit 1
  fi
done

for required_command in curl jq mktemp; do
  command -v "${required_command}" >/dev/null || {
    echo "缺少命令：${required_command}" >&2
    exit 1
  }
done

token_response=$(mktemp)
share_response=$(mktemp)
tenant_response=$(mktemp)
trap 'rm -f "${token_response}" "${share_response}" "${tenant_response}"' EXIT

curl --silent --show-error \
  --request POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"app_id\":\"${FEISHU_APP_ID}\",\"app_secret\":\"${FEISHU_APP_SECRET}\"}" \
  --output "${token_response}"

token_code=$(jq -r '.code // empty' "${token_response}")
tenant_access_token=$(jq -r '.tenant_access_token // empty' "${token_response}")
if [[ "${token_code}" != '0' || -z "${tenant_access_token}" ]]; then
  jq '{code, msg}' "${token_response}"
  exit 1
fi

printf '中心 Bot 令牌验证：通过\n'

curl --silent --show-error \
  --get 'https://open.feishu.cn/open-apis/tenant/v2/tenant/query' \
  --header "Authorization: Bearer ${tenant_access_token}" \
  --output "${tenant_response}"

printf '中心 Bot 所在租户：\n'
jq '{code, msg, tenant: ((.tenant // .data.tenant // .data // {}) | {name, tenant_key})}' "${tenant_response}"

if [[ -z "${FEISHU_TARGET_TENANT_KEY:-}" ]]; then
  curl --silent --show-error \
    --get 'https://open.feishu.cn/open-apis/trust_party/v1/collaboration_tenants' \
    --header "Authorization: Bearer ${tenant_access_token}" \
    --data-urlencode 'page_size=10' \
    --output "${share_response}"

  printf '关联租户列表（请将目标项的 tenant_key 填入 .env 后重新运行）：\n'
  jq '{
    code,
    msg,
    response_fields: (.data | keys),
    tenants: ((.data.target_tenant_list // .data.items // .data.collaboration_tenants // .data.tenants // []) | map({name, tenant_key}))
  }' "${share_response}"

  curl --silent --show-error \
    --get 'https://open.feishu.cn/open-apis/directory/v1/collaboration_tenants' \
    --header "Authorization: Bearer ${tenant_access_token}" \
    --data-urlencode 'page_size=10' \
    --output "${share_response}"

  printf 'Directory 关联租户列表：\n'
  jq '{
    code,
    msg,
    response_fields: (.data | keys),
    tenants: ((.data.target_tenant_list // .data.items // .data.collaboration_tenants // .data.tenants // []) | map({name, tenant_key}))
  }' "${share_response}"
  exit 0
fi

for select_subject in false true; do
  share_query_args=(
    --data-urlencode "target_tenant_key=${FEISHU_TARGET_TENANT_KEY}"
    --data-urlencode "is_select_subject=${select_subject}"
    --data-urlencode 'page_size=100'
  )
  if [[ -n "${FEISHU_TARGET_DEPARTMENT_ID:-}" ]]; then
    share_query_args+=(--data-urlencode "target_department_id=${FEISHU_TARGET_DEPARTMENT_ID}")
  fi

  curl --silent --show-error \
    --get 'https://open.feishu.cn/open-apis/directory/v1/share_entities' \
    --header "Authorization: Bearer ${tenant_access_token}" \
    "${share_query_args[@]}" \
    --output "${share_response}"

  printf '关联组织共享范围（is_select_subject=%s，department=%s）：\n' "${select_subject}" "${FEISHU_TARGET_DEPARTMENT_ID:-根目录}"
  jq '{
    code,
    msg,
    share_users: (.data.share_users // [] | length),
    share_departments: (.data.share_departments // [] | length),
    share_department_ids: [.data.share_departments[]?.open_department_id],
    share_groups: (.data.share_groups // [] | length),
    has_open_user_id: any(.data.share_users[]?; has("open_user_id")),
    has_more: (.data.has_more // false)
  }' "${share_response}"
done
