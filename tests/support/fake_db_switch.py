"""宿主工具桩：给 ``scripts/ops/db_switch_to_local.sh`` 与 ``db_backup_install.sh`` 的假根目录用例用（#896）。

两份脚本的测试形（非 root + ``LINGXI_S3x_ROOT=<假根>``）允许把 ``systemctl`` / ``docker`` / ``ssh`` / ``scp``
换成桩。这里的桩都是 bash 小脚本：状态落在 ``FAKE_STATE`` 目录里的标记文件，调用参数逐行追加到
``FAKE_STATE/calls.log``，供用例断言「秘密没有进入命令行参数」这类性质。桩只模拟脚本实际用到的那几种
调用形状，遇到没模拟的形状一律非零退出，让用例红在明处，不静默放行。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
set -u
st="${FAKE_STATE:?}"; ud="${FAKE_UNIT_DIR:?}"
printf 'systemctl %s\n' "$*" >> "$st/calls.log"
cmd="${1:-}"; shift || true
effective_execstart() { # 单元文件 + drop-in 依次处理：空 ExecStart= 清空，非空覆盖
  local u="$1" f line val=""
  for f in "$ud/$u" "$ud/$u.d"/*.conf; do
    [[ -f "$f" ]] || continue
    while IFS= read -r line; do
      [[ "$line" == ExecStart=* ]] && val="${line#ExecStart=}"
    done < "$f"
  done
  printf '%s' "$val"
}
case "$cmd" in
  daemon-reload|list-timers) exit 0 ;;
  is-enabled) rc=0; for u in "$@"; do if [[ -f "$st/$u.enabled" ]]; then echo enabled; else echo disabled; rc=1; fi; done; exit "$rc" ;;
  is-active) rc=0; for u in "$@"; do if [[ -f "$st/$u.active" ]]; then echo active; else echo inactive; rc=3; fi; done; exit "$rc" ;;
  enable) now=0; if [[ "${1:-}" == --now ]]; then now=1; shift; fi
    for u in "$@"; do touch "$st/$u.enabled"; if (( now )); then touch "$st/$u.active"; fi; done ;;
  disable) if [[ "${1:-}" == --now ]]; then shift; fi; for u in "$@"; do rm -f "$st/$u.enabled" "$st/$u.active"; done ;;
  start) for u in "$@"; do touch "$st/$u.active"; done ;;
  stop) for u in "$@"; do rm -f "$st/$u.active"; done ;;
  cat) u="${1:?}"; [[ -f "$ud/$u" ]] || exit 1
    printf '# %s\n' "$ud/$u"; cat "$ud/$u"
    for f in "$ud/$u.d"/*.conf; do [[ -f "$f" ]] || continue; printf '\n# %s\n' "$f"; cat "$f"; done ;;
  show) props=(); value=0; unit=""
    while (( $# )); do case "$1" in -p) props+=("$2"); shift 2 ;; --value) value=1; shift ;; *) unit="$1"; shift ;; esac; done
    for p in "${props[@]}"; do
      case "$p" in
        ExecStart) v="$(effective_execstart "$unit")"; [[ -n "$v" ]] && v="{ path=${v%% *} ; argv[]=$v ; }" ;;
        User) v="" ;;
        Result) v=success ;;
        ExecMainStatus) v=0 ;;
        *) v="" ;;
      esac
      if (( value )); then printf '%s\n' "$v"; else printf '%s=%s\n' "$p" "$v"; fi
    done ;;
  *) echo "fake systemctl：未模拟的调用 $cmd" >&2; exit 64 ;;
esac
"""

# docker 桩：本地库容器「在位」以 FAKE_STATE/db.up 为准（compose up 时建）；来源库查询经 `run … sh -c`
# 回放固定的来源事实；目标库查询经 `exec -i <容器> psql` 按 SQL 片段回放。
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
st="${FAKE_STATE:?}"
printf 'docker %s\n' "$*" >> "$st/calls.log"
cmd="${1:-}"; shift || true
case "$cmd" in
  compose)
    if [[ "${1:-}" == version ]]; then echo "Docker Compose version v2.0.0-fake"; exit 0; fi
    [[ " $* " == *" up -d "* ]] || { echo "fake docker：未模拟的 compose 调用" >&2; exit 64; }
    touch "$st/db.up"; echo "compose up (fake)" ;;
  network) [[ "${1:-}" == inspect && "${2:-}" == "${FAKE_APP_NETWORK:-lingxi_default}" ]] || exit 1; echo '[]' ;;
  inspect) name="${*: -1}"
    if [[ "$name" == "${FAKE_DB_CONTAINER:-lingxi-db}" && -f "$st/db.up" ]]; then echo healthy; else exit 1; fi ;;
  port) echo "127.0.0.1:${FAKE_DB_HOST_PORT:-5432}" ;;
  run) envfile=""
    while (( $# )); do case "$1" in --env-file) envfile="$2"; shift 2 ;; sh) break ;; *) shift ;; esac; done
    [[ -n "$envfile" && -f "$envfile" ]] || { echo "fake docker：run 缺 --env-file" >&2; exit 64; }
    sed -n 's/^PGDSN=//p' "$envfile" > "$st/source_dsn_seen"
    sql="$(cat)"
    if [[ "$sql" == *S30_DATCOLLATE* ]]; then
      printf '%s\n' "S30_DATCOLLATE='en_US.UTF-8'" "S30_DATCTYPE='en_US.UTF-8'" "S30_ENCODING='UTF8'" \
        "S30_DATLOCPROVIDER='i'" "S30_DATLOCALE='en-US'" "S30_SRC_VERSION='17.6'" \
        "S30_ROLE_SEARCH_PATH='\"\$user\", public, extensions'" "S30_ROLES='lingxi_app lingxi_retention_owner postgres'"
    else
      printf '%s\n' '{"alembic_head" : "0098_fake", "counts" : {"tables" : 2}, "db_size_bytes" : 1, "settings" : {"idle_in_transaction_session_timeout" : "0"}}'
    fi ;;
  exec) sql="$(cat)"
    case "$sql" in
      "SELECT 1") echo 1 ;;
      *s30_probe*) echo 1 ;;
      *"max_connections="*) echo "max_connections=100 idle_in_txn=0 checksums=on" ;;
      *"CREATE SCHEMA IF NOT EXISTS extensions"*) : ;;
      *"FROM pg_extension"*) echo "1.6@extensions" ;;
      *"FROM pg_roles WHERE rolname = '"*) r="${sql#*rolname = \'}"; r="${r%%\'*}"; if [[ -f "$st/role_$r" ]]; then echo 1; else echo 0; fi ;;
      "CREATE ROLE "*) r="${sql#CREATE ROLE \"}"; r="${r%%\"*}"; touch "$st/role_$r" ;;
      *pg_db_role_setting*) cat "$st/search_path" 2>/dev/null || true ;;
      "ALTER ROLE postgres SET search_path = "*) printf '%s' "${sql#ALTER ROLE postgres SET search_path = }" > "$st/search_path" ;;
      *"relkind IN ('r','p','S','v','m')"*) echo 0 ;;
      *) echo "fake docker：未模拟的 SQL" >&2; exit 64 ;;
    esac ;;
  *) echo "fake docker：未模拟的调用 $cmd" >&2; exit 64 ;;
esac
"""

FAKE_SSH = r"""#!/usr/bin/env bash
printf 'ssh %s\n' "$*" >> "${FAKE_STATE:?}/calls.log"
exit 0
"""

FAKE_SCP = r"""#!/usr/bin/env bash
printf 'scp %s\n' "$*" >> "${FAKE_STATE:?}/calls.log"
exit 0
"""


def write_executable(path: Path, body: str) -> Path:
    """写一个 0755 可执行文件（父目录不存在则建）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def install_stubs(bin_dir: Path) -> dict[str, Path]:
    """在 ``bin_dir`` 下装齐四个桩，返回名称 → 路径。"""
    return {
        "systemctl": write_executable(bin_dir / "systemctl", FAKE_SYSTEMCTL),
        "docker": write_executable(bin_dir / "docker", FAKE_DOCKER),
        "ssh": write_executable(bin_dir / "ssh", FAKE_SSH),
        "scp": write_executable(bin_dir / "scp", FAKE_SCP),
    }


def base_env(bin_dir: Path, state_dir: Path, unit_dir: Path) -> dict[str, str]:
    """子进程环境：桩目录排在 PATH 最前；不继承调用者的 LINGXI_* 变量。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LINGXI_")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LC_ALL"] = "C"
    env["FAKE_STATE"] = str(state_dir)
    env["FAKE_UNIT_DIR"] = str(unit_dir)
    return env


# --- recreate-services（#896）用的 docker 桩 ---
# 与上面的 FAKE_DOCKER 分开：recreate-services 按部署器口径以 ``env -i`` 调 docker compose，子进程拿不到
# FAKE_STATE，所以本桩从自身同目录的 ``.fake_state`` 文件找状态目录。三容器的现场写在
# ``<state>/world.json``：{"containers": [{"id", "name", "labels", "image", "health"}], "after_health": …}。
# ``compose … up -d --force-recreate …`` 把每个容器换成新 id（标签与镜像不变）、健康状态改为 after_health，
# 并把本次 argv 与完整环境追加进 ``<state>/compose_up.json``，供用例逐项断言。
FAKE_DOCKER_RECREATE = r"""#!__PYTHON__
import json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
state = Path(os.environ.get("FAKE_STATE") or (here / ".fake_state").read_text(encoding="utf-8").strip())
args = sys.argv[1:]
with (state / "calls.log").open("a", encoding="utf-8") as log:
    log.write("docker " + " ".join(args) + "\n")
world_path = state / "world.json"
world = json.loads(world_path.read_text(encoding="utf-8"))


def find(ref):
    for c in world["containers"]:
        if ref in (c["id"], c["name"]):
            return c
    return None


if args[:1] == ["ps"]:
    project = args[-1].split("com.docker.compose.project=", 1)[1]
    for c in world["containers"]:
        if c["labels"].get("com.docker.compose.project") == project:
            print(c["id"])
    sys.exit(0)
if args[:1] == ["inspect"]:
    fmt = args[args.index("--format") + 1] if "--format" in args else args[args.index("-f") + 1]
    c = find(args[-1])
    if c is None:
        sys.exit(1)
    if ".Config.Labels" in fmt:
        print(json.dumps(c["labels"]))
        print(json.dumps(c["image"]))
    elif "Health" in fmt:
        print(c["health"])
    else:
        sys.exit(64)
    sys.exit(0)
if args[:1] == ["compose"]:
    if "up" not in args or "--force-recreate" not in args:
        print("fake docker：未模拟的 compose 调用", file=sys.stderr)
        sys.exit(64)
    record = state / "compose_up.json"
    calls = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
    env = dict(os.environ)
    env.pop("LC_CTYPE", None)  # 空环境下解释器按 PEP 538 自己补的，不是调用方传的
    calls.append({"argv": args, "env": env})
    record.write_text(json.dumps(calls), encoding="utf-8")
    for c in world["containers"]:
        c["id"] = c["id"] + "r"
        c["health"] = world.get("after_health", "healthy")
    world_path.write_text(json.dumps(world), encoding="utf-8")
    sys.exit(0)
print("fake docker：未模拟的调用 " + " ".join(args[:1]), file=sys.stderr)
sys.exit(64)
"""


def install_recreate_docker(bin_dir: Path, state_dir: Path) -> Path:
    """装 recreate-services 用的 docker 桩（``bin_dir/docker-recreate``），并登记状态目录。"""
    import sys

    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / ".fake_state").write_text(str(state_dir), encoding="utf-8")
    body = FAKE_DOCKER_RECREATE.replace("__PYTHON__", sys.executable)
    return write_executable(bin_dir / "docker-recreate", body)
