#!/bin/sh
set -eu

# 由安装人替换为本机固定配置；这些值不写入代理配置或提交内容。
# GUARD_ABSOLUTE_PATH 应指向控制包 current/deploy/permission_table_guard.py 的绝对路径。
PYTHON_ABSOLUTE_PATH="<PYTHON_ABSOLUTE_PATH>"
GUARD_ABSOLUTE_PATH="<GUARD_ABSOLUTE_PATH>"
SCHEDULER_ENV_FILE="<SCHEDULER_ENV_FILE>"
GUARD_WORK_DIR="<GUARD_WORK_DIR>"

# pre_apply_hooks 会传入 --host-contract、--config、--state-directory、--plan 四个参数，
# 本包装脚本不使用它们。脚本自身及父目录须为 root:root、0755；绝对路径约束与
# pre_apply_hooks 的 _safe_path 一致，避免钩子路径被其他主体替换。
exec "$PYTHON_ABSOLUTE_PATH" -B "$GUARD_ABSOLUTE_PATH" \
    --env-file "$SCHEDULER_ENV_FILE" \
    --work-dir "$GUARD_WORK_DIR" backup
