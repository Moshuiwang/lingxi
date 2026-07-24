#!/usr/bin/env bash
# 在关联组织的已共享范围内精确查找一名用户，并由中心 Bot 发送一条验证消息。
# 不输出 App Secret、访问令牌、open_user_id 或成员名单。
set -euo pipefail
trap 'printf "验证在第 %s 行提前结束；未确认消息已发送。\\n" "$LINENO" >&2' ERR

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace_dir=$(cd -- "${script_dir}/.." && pwd)
env_file="${workspace_dir}/.env"
target_name="${FEISHU_TARGET_USER_NAME:-王志鹏}"

if [[ ! -f "${env_file}" ]]; then
  echo '未找到 .env。' >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

for required_var in FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_TARGET_TENANT_KEY; do
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

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

token_response="${work_dir}/token.json"
curl --silent --show-error \
  --request POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"app_id\":\"${FEISHU_APP_ID}\",\"app_secret\":\"${FEISHU_APP_SECRET}\"}" \
  --output "${token_response}"

tenant_access_token=$(jq -r '.tenant_access_token // empty' "${token_response}")
if [[ "$(jq -r '.code // empty' "${token_response}")" != '0' || -z "${tenant_access_token}" ]]; then
  jq '{code, msg}' "${token_response}"
  exit 1
fi

declare -A visited_departments=()
if [[ -n "${FEISHU_SCAN_ROOT_DEPARTMENT_IDS:-}" ]]; then
  IFS=',' read -r -a department_queue <<<"${FEISHU_SCAN_ROOT_DEPARTMENT_IDS}"
else
  department_queue=("${FEISHU_SCAN_ROOT_DEPARTMENT_ID:-0}")
fi
matched_ids=()
checked_departments=0

while ((${#department_queue[@]} > 0)); do
  department_id=${department_queue[0]}
  department_queue=("${department_queue[@]:1}")
  [[ -n "${visited_departments[${department_id}]:-}" ]] && continue
  visited_departments["${department_id}"]=1
  ((checked_departments+=1))

  response_file="${work_dir}/share-${checked_departments}.json"
  curl --silent --show-error \
    --get 'https://open.feishu.cn/open-apis/directory/v1/share_entities' \
    --header "Authorization: Bearer ${tenant_access_token}" \
    --data-urlencode "target_tenant_key=${FEISHU_TARGET_TENANT_KEY}" \
    --data-urlencode "target_department_id=${department_id}" \
    --data-urlencode 'is_select_subject=false' \
    --data-urlencode 'page_size=100' \
    --output "${response_file}"

  # Directory 接口会触发租户级频率限制；默认以较低频率遍历共享组织树。
  sleep "${FEISHU_REQUEST_INTERVAL_SECONDS:-1}"

  if [[ "$(jq -r '.code // empty' "${response_file}")" != '0' ]]; then
    jq '{code, msg}' "${response_file}"
    exit 1
  fi

  if [[ "${FEISHU_RECURSE:-true}" == 'true' ]]; then
    while IFS= read -r child_id; do
      [[ -n "${child_id}" ]] && department_queue+=("${child_id}")
    done < <(jq -r '.data.share_departments[]?.open_department_id // empty' "${response_file}")
  fi

  while IFS= read -r user_id; do
    [[ -n "${user_id}" ]] && matched_ids+=("${user_id}")
  done < <(jq -r --arg target_name "${target_name}" \
    '.data.share_users[]? | select(.name == $target_name) | .open_user_id // empty' "${response_file}")
done

# 同一成员可能出现在多个共享部门，去重后才判断唯一性。
mapfile -t unique_matched_ids < <(printf '%s\n' "${matched_ids[@]:-}" | awk 'NF && !seen[$0]++')
if ((${#unique_matched_ids[@]} == 0)); then
  printf '查询完成：在已共享范围内未找到目标姓名；未发送消息。已检查 %s 个部门。\n' "${checked_departments}"
  exit 2
fi
if ((${#unique_matched_ids[@]} != 1)); then
  printf '查询完成：目标姓名匹配到多名成员；为避免误发，未发送消息。已检查 %s 个部门。\n' "${checked_departments}"
  exit 3
fi

if [[ "${FEISHU_SEND_ON_MATCH:-true}" != 'true' ]]; then
  printf '验证成功：已定位到唯一目标成员；按当前设置未发送消息。已检查 %s 个部门。\n' "${checked_departments}"
  exit 0
fi

message_response="${work_dir}/message.json"
curl --silent --show-error \
  --request POST 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id' \
  --header "Authorization: Bearer ${tenant_access_token}" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"receive_id\":\"${unique_matched_ids[0]}\",\"msg_type\":\"text\",\"content\":\"{\\\"text\\\":\\\"灵犀验证：已通过关联组织定位到你。这是一条由 StarTimes 中心机器人发送的测试消息。\\\"}\"}" \
  --output "${message_response}"

if [[ "$(jq -r '.code // empty' "${message_response}")" != '0' ]]; then
  printf '已定位到唯一目标成员，但消息发送失败：\n'
  jq '{code, msg}' "${message_response}"
  exit 4
fi

printf '验证成功：已定位到唯一目标成员，并由中心 Bot 发送消息。已检查 %s 个部门。\n' "${checked_departments}"
