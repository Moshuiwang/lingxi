#!/usr/bin/env bash
# 从目标机器的私有 Supabase 凭据文件，把数据库连接串同步进各服务的 env 文件
# （Issue #411：stage/prod 数据库切换 Supabase 云托管）。
#
# 凭据文件是目标机器上的唯一数据库凭据事实源，**不在版本库里、不复制到研发机**：
#   stage: /home/wangzhipeng/.config/lingxi/supabase-stage.env   （biai-stage）
#   prod:  /home/bi-ai-deploy/.config/lingxi/supabase-prod.env   （biplus-prod）
# 文件须为 0600，含以下变量（本脚本只按行复制，不解析、不导出、不回显任何值）：
#   LINGXI_POSTGRES_DSN / LINGXI_GATEWAY_POSTGRES_DSN / LINGXI_MIGRATION_DSN
#
# 为什么不直接把凭据文件挂成 compose 的 env_file：那份文件同时含运行 DSN 与迁移
# DSN（DDL 权限），整文件挂给业务服务会把迁移 DSN 送进业务进程（violates
# V-迁移-05 的分权前提）；挂给 worker 更是踩 PR #173 复核 P1-2 的既有红线。因此
# 保持「按服务分文件」的既有结构，本脚本只把每个服务应得的那一个变量写进它自己
# 的 env 文件：
#   <前缀>.scheduler     ← LINGXI_POSTGRES_DSN
#   <前缀>.gateway       ← LINGXI_GATEWAY_POSTGRES_DSN
#   <前缀>.worker-queue  ← LINGXI_POSTGRES_DSN
#   <前缀>.reauthorize   ← LINGXI_POSTGRES_DSN
#   <前缀>.migrate       ← LINGXI_MIGRATION_DSN
#   <前缀>.worker        ← 零数据库凭据（只核对，发现 DSN 即报错退出）
#
# 输出只报「哪个文件、哪个变量名」，永不打印值。运行前先按 deploy/README.md
# 「安装与升级」做 env 文件备份。
set -euo pipefail
umask 077

usage() {
    echo "用法: $0 <凭据文件> <env目录> <env文件前缀>" >&2
    echo "示例(stage): $0 ~/.config/lingxi/supabase-stage.env deploy .env.stage" >&2
    echo "示例(prod):  $0 ~/.config/lingxi/supabase-prod.env deploy .env.prod" >&2
    exit 64
}

[ "$#" -eq 3 ] || usage
creds=$1
dir=$2
prefix=$3

[ -f "$creds" ] || { echo "凭据文件不存在: $creds" >&2; exit 66; }
perm=$(stat -c '%a' "$creds")
if [ "$perm" != "600" ]; then
    echo "凭据文件权限必须为 0600（实际 $perm），拒绝执行" >&2
    exit 77
fi

# 取凭据文件中某变量的整行（同名多行取最后一行）；缺失即失败，不做默认值。
line_of() {
    local var=$1 line
    line=$(grep -E "^${var}=" "$creds" | tail -n 1 || true)
    [ -n "$line" ] || { echo "凭据文件缺少变量 ${var}" >&2; exit 65; }
    printf '%s' "$line"
}

# 把变量行写入目标 env 文件：删除旧行、末尾追加新行；同目录临时文件 0600 后
# 原子替换（不经历任何可读窗口，与 preflight 的 install 姿势同一初衷）。
apply() {
    local var=$1 target=$2 line tmp
    if [ ! -f "$target" ]; then
        echo "目标 env 文件不存在: $target（先按 deploy/README.md preflight 建好）" >&2
        exit 66
    fi
    line=$(line_of "$var")
    tmp=$(mktemp "${target}.sync.XXXXXX")
    grep -vE "^${var}=" "$target" > "$tmp" || true
    printf '%s\n' "$line" >> "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$target"
    echo "已更新 ${target} ← ${var}"
}

apply LINGXI_POSTGRES_DSN "${dir}/${prefix}.scheduler"
apply LINGXI_GATEWAY_POSTGRES_DSN "${dir}/${prefix}.gateway"
apply LINGXI_POSTGRES_DSN "${dir}/${prefix}.worker-queue"
apply LINGXI_POSTGRES_DSN "${dir}/${prefix}.reauthorize"
apply LINGXI_MIGRATION_DSN "${dir}/${prefix}.migrate"

# 红线核对：一次性 worker 不得获得任何数据库凭据（Agent SDK 会把环境继承给模型
# 执行环境与 MCP 子进程，deploy/compose.stage.yaml 头注 / PR #173 复核 P1-2）。
worker_env="${dir}/${prefix}.worker"
if [ -f "$worker_env" ] && grep -qE '^[A-Za-z_]*(POSTGRES|MIGRATION)[A-Za-z_]*DSN[A-Za-z_]*=' "$worker_env"; then
    echo "红线违规: ${worker_env} 含数据库 DSN 变量，请手工移除后重跑" >&2
    exit 78
fi

echo "完成：scheduler/gateway/worker-queue/reauthorize/migrate 五份 env 已同步；${prefix}.worker 零数据库凭据（已核对）。"
