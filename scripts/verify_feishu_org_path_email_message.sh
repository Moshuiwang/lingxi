#!/usr/bin/env bash
# 通过关联组织共享范围按部门路径定位成员；仅在其邮箱与指定邮箱一致时发送验证消息。
# 不输出邮箱、成员 ID、访问令牌或密钥。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace_dir=$(cd -- "${script_dir}/.." && pwd)
env_file="${workspace_dir}/.env"
target_email="${FEISHU_TARGET_EMAIL:-}"
target_department='用户产品运营部'
target_subdepartment='OTT业务部'
target_user_keyword='王志鹏'

[[ -n "${target_email}" ]] || { echo '请通过 FEISHU_TARGET_EMAIL 提供待核验邮箱。' >&2; exit 1; }
[[ -f "${env_file}" ]] || { echo '未找到 .env。' >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

for required_var in FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_TARGET_TENANT_KEY; do
  [[ -n "${!required_var:-}" ]] || { echo "${required_var} 尚未填写。" >&2; exit 1; }
done

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

token_file="${work_dir}/token.json"
curl --silent --show-error --request POST \
  'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"app_id\":\"${FEISHU_APP_ID}\",\"app_secret\":\"${FEISHU_APP_SECRET}\"}" \
  --output "${token_file}"
tenant_access_token=$(jq -r '.tenant_access_token // empty' "${token_file}")
if [[ "$(jq -r '.code // empty' "${token_file}")" != '0' || -z "${tenant_access_token}" ]]; then
  jq '{code, msg}' "${token_file}"
  exit 1
fi

get_shared_entities() {
  local department_id=$1 output_file=$2
  curl --silent --show-error --get \
    'https://open.feishu.cn/open-apis/directory/v1/share_entities' \
    --header "Authorization: Bearer ${tenant_access_token}" \
    --data-urlencode "target_tenant_key=${FEISHU_TARGET_TENANT_KEY}" \
    --data-urlencode "target_department_id=${department_id}" \
    --data-urlencode 'is_select_subject=false' \
    --data-urlencode 'page_size=100' \
    --output "${output_file}"
  if [[ "$(jq -r '.code // empty' "${output_file}")" != '0' ]]; then
    jq '{code, msg}' "${output_file}"
    exit 2
  fi
}

root_file="${work_dir}/root.json"
get_shared_entities '0' "${root_file}"
department_id=$(jq -r --arg name "${target_department}" \
  '.data.share_departments[]? | select((.name.default_value // .name) == $name) | .open_department_id' "${root_file}" | head -n 1)
if [[ -z "${department_id}" ]]; then
  printf '未在“四达集团”已共享的一级部门中找到截图所示部门；未发送消息。\n'
  exit 3
fi

department_file="${work_dir}/department.json"
get_shared_entities "${department_id}" "${department_file}"
subdepartment_id=$(jq -r --arg name "${target_subdepartment}" \
  '.data.share_departments[]? | select((.name.default_value // .name) == $name) | .open_department_id' "${department_file}" | head -n 1)
if [[ -z "${subdepartment_id}" ]]; then
  printf '已找到一级部门，但未找到截图所示子部门；未发送消息。\n'
  exit 4
fi

subdepartment_file="${work_dir}/subdepartment.json"
get_shared_entities "${subdepartment_id}" "${subdepartment_file}"
mapfile -t candidate_user_ids < <(jq -r --arg keyword "${target_user_keyword}" \
  '.data.share_users[]? | select((.name.default_value // .name) | contains($keyword)) | .open_user_id' "${subdepartment_file}")
if ((${#candidate_user_ids[@]} != 1)); then
  printf '已找到截图中的部门路径，但“王志鹏”名称匹配不唯一或不存在；未发送消息。\n'
  exit 5
fi
user_id=${candidate_user_ids[0]}

user_file="${work_dir}/user.json"
email_verified=false
curl --silent --show-error --get \
  "https://open.feishu.cn/open-apis/trust_party/v1/collaboration_tenants/${FEISHU_TARGET_TENANT_KEY}/collaboration_users/${user_id}" \
  --header "Authorization: Bearer ${tenant_access_token}" \
  --data-urlencode 'target_user_id_type=open_id' \
  --output "${user_file}"
if [[ "$(jq -r '.code // empty' "${user_file}")" != '0' ]]; then
  printf '已按截图找到唯一成员，但关联组织成员详情接口调用失败：\n'
  jq '{code, msg}' "${user_file}"
  exit 6
else
  returned_email=$(jq -r '.data.target_user.email // empty' "${user_file}")
  if [[ -z "${returned_email}" ]]; then
    printf '关联组织成员详情接口调用成功，但返回成员信息不包含邮箱字段；未发送消息。字段：\n'
    jq '(.data.target_user // {}) | keys' "${user_file}"
    exit 7
  fi
  if [[ "${returned_email}" != "${target_email}" ]]; then
    printf '已按截图找到唯一成员，但其邮箱未返回或与提供邮箱不一致；未发送消息。\n'
    exit 8
  fi
  email_verified=true
fi

if [[ "${FEISHU_SEND_ON_MATCH:-true}" != 'true' ]]; then
  if [[ "${email_verified}" == 'true' ]]; then
    printf '验证成功：已按组织架构定位唯一成员，且邮箱核验一致；按当前设置未发送消息。\n'
    exit 0
  fi
  printf '已按组织架构定位唯一成员；邮箱未能核验，按当前设置未发送消息。\n'
  exit 9
fi

message_file="${work_dir}/message.json"
curl --silent --show-error --request POST \
  'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id' \
  --header "Authorization: Bearer ${tenant_access_token}" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"receive_id\":\"${user_id}\",\"msg_type\":\"text\",\"content\":\"{\\\"text\\\":\\\"灵犀验证：已按关联组织架构和工作邮箱确认你的身份。这是一条由 StarTimes 中心机器人发送的测试消息。\\\"}\"}" \
  --output "${message_file}"
if [[ "$(jq -r '.code // empty' "${message_file}")" != '0' ]]; then
  printf '已按组织架构及邮箱确认身份，但消息发送失败：\n'
  jq '{code, msg}' "${message_file}"
  exit 8
fi

if [[ "${email_verified}" == 'true' ]]; then
  printf '验证成功：已按组织架构和邮箱确认身份，并由中心 Bot 发送消息。\n'
else
  printf '验证成功：已按组织架构定位唯一成员，并由中心 Bot 发送跨租户消息；邮箱未能由接口核验。\n'
fi
