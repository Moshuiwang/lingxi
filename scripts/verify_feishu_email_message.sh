#!/usr/bin/env bash
# 通过邮箱获取用户 ID；唯一匹配时可发送一条验证消息。
# 不输出邮箱、open_id、App Secret 或访问令牌。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
workspace_dir=$(cd -- "${script_dir}/.." && pwd)
env_file="${workspace_dir}/.env"
target_email="${FEISHU_TARGET_EMAIL:-}"

if [[ -z "${target_email}" ]]; then
  echo '请通过 FEISHU_TARGET_EMAIL 提供待验证邮箱。' >&2
  exit 1
fi
if [[ ! -f "${env_file}" ]]; then
  echo '未找到 .env。' >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

for required_var in FEISHU_APP_ID FEISHU_APP_SECRET; do
  [[ -n "${!required_var:-}" ]] || { echo "${required_var} 尚未填写。" >&2; exit 1; }
done

work_dir=$(mktemp -d)
trap 'rm -rf "${work_dir}"' EXIT

token_file="${work_dir}/token.json"
lookup_file="${work_dir}/lookup.json"
message_file="${work_dir}/message.json"

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

curl --silent --show-error --request POST \
  'https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id?user_id_type=open_id' \
  --header "Authorization: Bearer ${tenant_access_token}" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"emails\":[\"${target_email}\"]}" \
  --output "${lookup_file}"

if [[ "$(jq -r '.code // empty' "${lookup_file}")" != '0' ]]; then
  printf '邮箱查找失败：\n'
  jq '{code, msg}' "${lookup_file}"
  exit 2
fi

mapfile -t user_ids < <(jq -r '.data.user_list[]?.user_id // empty' "${lookup_file}")
if ((${#user_ids[@]} != 1)); then
  printf '邮箱查找完成：未得到唯一用户 ID；未发送消息。匹配数：%s\n' "${#user_ids[@]}"
  exit 3
fi

if [[ "${FEISHU_SEND_ON_MATCH:-true}" != 'true' ]]; then
  printf '邮箱查找成功：得到唯一用户 ID；按当前设置未发送消息。\n'
  exit 0
fi

curl --silent --show-error --request POST \
  'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id' \
  --header "Authorization: Bearer ${tenant_access_token}" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data "{\"receive_id\":\"${user_ids[0]}\",\"msg_type\":\"text\",\"content\":\"{\\\"text\\\":\\\"灵犀验证：已通过工作邮箱定位到你。这是一条由 StarTimes 中心机器人发送的测试消息。\\\"}\"}" \
  --output "${message_file}"

if [[ "$(jq -r '.code // empty' "${message_file}")" != '0' ]]; then
  printf '邮箱查找成功，但消息发送失败：\n'
  jq '{code, msg}' "${message_file}"
  exit 4
fi

printf '验证成功：已通过邮箱定位唯一用户，并由中心 Bot 发送消息。\n'
