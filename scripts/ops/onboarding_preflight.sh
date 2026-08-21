#!/usr/bin/env bash
# 首次开通链就绪自检（只读）。
#
# 用途：把首次开通链每一条装配/数据前提，映射到它会让链路卡在第几步；一次
# 运行给出全链路 ✅/❌ 速览，避免"日志里其实写着原因，但没人把它和步骤对上"
# 这类事故——此前已发生两次真实发消息失败，原因都能在 scheduler 启动日志
# 里找到，但排查时没人把日志内容和"链会在第几步停下"连起来。
#
# 出处：2026-08-21 Epic D 验收批次，产品负责人明确要求建立的机制。原脚本先
# 在 biai-stage 上以 ops-epicd-20260821/preflight.sh 形式验证过；本文件是同
# 一份判定逻辑的入库版——容器名、数据库用户/库名改为可覆盖的环境变量，其余
# 判定逻辑不变，不读取、不打印任何凭据或令牌的明文。
#
# 规则（原文）：「自检不全绿，就不要请产品负责人发消息或做任何操作」。
#
# 步骤 -> 首次开通链映射（详见 docs/技术设计/架构设计.md 6.4/6.9 与
# docs/决策记录/2026-08-18-首次开通编排住在scheduler.md）：
#   1  身份定位（组织快照）     feishu_org_member_snapshot 有数据，否则定位不到
#                              在职员工，链路从第一步就起不来
#   2a 花名册快照               roster_snapshot_row 有数据，否则建档信息不全
#   2b 银河有效批次             galaxy_import_batch 有数据，否则权限来源缺失
#   3  范围聚合                 纯计算，无外部前提，恒 ✅
#   4  翻译闸（公司+职能→指标） 启动日志里"翻译映射配置不可用"次数为 0；这道闸
#                              不过，首次开通编排在 LX-ONBOARD-001 收口，一条
#                              发布意图都不排（建档之前拦截）
#   5  MCP 令牌签发             LINGXI_MCP_TOKEN_ENCRYPT_KEY 已配置，否则签发不
#                              了用户的问数访问令牌
#   6  用户环境 .mcp.json       LINGXI_USER_ENV_ROOT 已配置，否则该步以
#                              user_environment_failed_<errno> 失败关闭
#   7  权限发布（Outbox→表）    LINGXI_PERMISSION_BITABLE_APP_TOKEN /
#                              LINGXI_PERMISSION_BITABLE_TABLE_ID 均已配置，且
#                              启动日志里 publish_wired=False 次数为 0
#   8  MCP 就绪确认             LINGXI_QUERY_MCP_ENDPOINT 已配置（真实同步仍依
#                              赖正式表，硬切前不可达，此项只做接线检查）
#
# 使用前提：在部署宿主机上运行（当前唯一验证过的环境是 biai-stage 的
# Bot-Test 部署）；需要能对下列容器执行 docker exec / docker logs 的权限；
# 只读，不修改任何数据、配置或容器状态。
#
# 可覆盖的环境变量（默认值对应 biai-stage 当前 Bot-Test 部署的容器命名，其他
# 部署按自己的 compose 项目名/容器名覆盖）：
#   LINGXI_PREFLIGHT_DB_CONTAINER         数据库容器名，默认 lingxi-test-db
#   LINGXI_PREFLIGHT_DB_USER              数据库用户名，默认 lingxi
#   LINGXI_PREFLIGHT_DB_NAME              数据库名，默认 lingxi
#   LINGXI_PREFLIGHT_SCHEDULER_CONTAINER  scheduler 容器名，默认 lingxi-scheduler-1
set -uo pipefail

db_container="${LINGXI_PREFLIGHT_DB_CONTAINER:-lingxi-test-db}"
db_user="${LINGXI_PREFLIGHT_DB_USER:-lingxi}"
db_name="${LINGXI_PREFLIGHT_DB_NAME:-lingxi}"
scheduler_container="${LINGXI_PREFLIGHT_SCHEDULER_CONTAINER:-lingxi-scheduler-1}"

psql_count() {
  docker exec "${db_container}" psql -U "${db_user}" -d "${db_name}" -Atc \
    "select count(*) from $1" 2>/dev/null || echo "ERR"
}

env_has() {
  docker exec "${scheduler_container}" sh -c "[ -n \"\${$1:-}\" ]" 2>/dev/null \
    && echo yes || echo no
}

echo "==================== 首次开通链就绪自检 ===================="
printf "%-4s %-26s %-10s %s\n" "步" "环节" "判定" "依据"
echo "-----------------------------------------------------------"

v=$(psql_count feishu_org_member_snapshot)
[ "$v" -gt 0 ] 2>/dev/null && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "1" "身份定位（组织快照）" "$s" "feishu_org_member_snapshot=$v"

v=$(psql_count roster_snapshot_row)
[ "$v" -gt 0 ] 2>/dev/null && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "2a" "花名册快照" "$s" "roster_snapshot_row=$v"

v=$(psql_count galaxy_import_batch)
[ "$v" -gt 0 ] 2>/dev/null && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "2b" "银河有效批次" "$s" "galaxy_import_batch=$v"

printf "%-4s %-26s %-10s %s\n" "3" "范围聚合" "✅" "纯计算，无外部前提"

r=$(docker logs "${scheduler_container}" 2>&1 | grep -c "翻译映射配置不可用" || true)
[ "${r:-0}" = "0" ] && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "4" "翻译闸（公司+职能→指标）" "$s" "启动期'翻译映射不可用'告警=${r:-0} 次"

a=$(env_has LINGXI_MCP_TOKEN_ENCRYPT_KEY)
[ "$a" = yes ] && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "5" "MCP 令牌签发" "$s" "LINGXI_MCP_TOKEN_ENCRYPT_KEY=$a"

a=$(env_has LINGXI_USER_ENV_ROOT)
[ "$a" = yes ] && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "6" "用户环境 .mcp.json" "$s" "LINGXI_USER_ENV_ROOT=$a"

a=$(env_has LINGXI_PERMISSION_BITABLE_APP_TOKEN)
b=$(env_has LINGXI_PERMISSION_BITABLE_TABLE_ID)
w=$(docker logs "${scheduler_container}" 2>&1 | grep -c "publish_wired=False" || true)
{ [ "$a" = yes ] && [ "$b" = yes ]; } && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "7" "权限发布（Outbox→表）" "$s" "APP_TOKEN=$a TABLE_ID=$b；日志 publish_wired=False 出现 ${w:-0} 次"

a=$(env_has LINGXI_QUERY_MCP_ENDPOINT)
[ "$a" = yes ] && s="✅" || s="❌"
printf "%-4s %-26s %-10s %s\n" "8" "MCP 就绪确认" "$s" "LINGXI_QUERY_MCP_ENDPOINT=$a（真实同步仍依赖正式表，硬切前不可达）"

echo "-----------------------------------------------------------"
echo "启动期职责注册（未注册的都会在某一步卡住）："
docker logs "${scheduler_container}" 2>&1 | grep -E "duty_not_registered|未装配|不注册" | tail -5 | sed 's/^/  /'
echo "==========================================================="
