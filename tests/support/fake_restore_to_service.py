"""宿主工具桩：给 ``scripts/ops/restore_to_service.sh`` 的假根目录用例用（#885）。

与 ``fake_db_switch.py`` 分开（同批并行卡不共用支撑文件）：这里的 docker 桩要回放 dump 目录 / schema SQL /
容器时间记录，切换脚本整个换成桩（``LINGXI_RESTORE_SWITCH_SCRIPT``）——``db_switch_to_local.sh`` 自己的行为由
``test_db_switch_to_local.py`` 覆盖，这里只断言本脚本在哪一步调了它的哪个子命令。状态落在 ``FAKE_STATE``
目录，全部调用逐行追加到 ``FAKE_STATE/calls.log``。没模拟的调用形状一律非零退出。
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

# pg_restore --schema-only -f - 的输出节选：取自一次真实 pg_dump（postgres 16）后原样保留的那几类行——
# TOC 注释头的 Owner 字段、OWNER TO、GRANT / REVOKE（带各自的 ACL 注释头）、默认权限、策略、会话身份。
# verify 的授权内容比对读目标库 pg_dump --schema-only 的同形输出：桩回放 target_schema.sql（缺省与本节选相同）。
SCHEMA_SQL = """--
-- Name: extensions; Type: SCHEMA; Schema: -; Owner: postgres
--
CREATE SCHEMA extensions;
ALTER SCHEMA extensions OWNER TO postgres;
-- Name: public; Type: SCHEMA; Schema: -; Owner: pg_database_owner
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner:
-- Name: lingxi_retention_cleanup(timestamp with time zone, integer); Type: FUNCTION; Schema: public; Owner: lingxi_retention_owner
ALTER FUNCTION public.lingxi_retention_cleanup(p_now timestamp with time zone, p_days integer) OWNER TO lingxi_retention_owner;
ALTER TABLE public.t1 OWNER TO lingxi_app;
-- Name: t1 p1; Type: POLICY; Schema: public; Owner: lingxi_app
CREATE POLICY p1 ON public.t1 TO anon USING (true);
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: pg_database_owner
GRANT USAGE ON SCHEMA public TO anon;
-- Name: FUNCTION lingxi_retention_cleanup(p_now timestamp with time zone, p_days integer); Type: ACL; Schema: public; Owner: lingxi_retention_owner
REVOKE ALL ON FUNCTION public.lingxi_retention_cleanup(p_now timestamp with time zone, p_days integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.lingxi_retention_cleanup(p_now timestamp with time zone, p_days integer) TO lingxi_app;
-- Name: TABLE t1; Type: ACL; Schema: public; Owner: lingxi_app
GRANT SELECT,DELETE ON TABLE public.t1 TO lingxi_retention_owner;
GRANT SELECT ON TABLE public.t1 TO "Weird Role";
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO anon;
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT SELECT ON SEQUENCES TO "Mixed Owner";
"""
# "Mixed Owner" 只在默认权限行的 TO 之后出现（带引号大小写名）：钉住「默认权限行的被授权角色」这条派生路径
SCHEMA_ROLES = [
    "Mixed Owner",
    "Weird Role",
    "anon",
    "lingxi_app",
    "lingxi_retention_owner",
    "pg_database_owner",
    "postgres",
    "supabase_admin",
]

TOC = """;
; Archive created at 2026-09-27 11:46:54 UTC
;
6; 2615 16390 SCHEMA - extensions postgres
3517; 0 0 ACL - SCHEMA public pg_database_owner
2; 3079 16391 EXTENSION - pg_trgm
3518; 0 0 COMMENT - EXTENSION pg_trgm
251; 1255 16489 FUNCTION public lingxi_retention_cleanup(timestamp with time zone, integer) lingxi_retention_owner
3519; 0 0 ACL public FUNCTION lingxi_retention_cleanup(p_now timestamp with time zone, p_days integer) lingxi_retention_owner
218; 1259 16473 TABLE public t1 lingxi_app
3520; 0 0 ACL public TABLE t1 lingxi_app
3508; 0 16473 TABLE DATA public t1 lingxi_app
"""

TARGET_JSON = (
    '{"alembic_head": "0098_x", "tables": {"public.t1": 2}, "sequences": {}, "extensions": ["pg_trgm@1.6", "plpgsql@1.0"],'
    ' "rel_owners": {"t1": "lingxi_app"}, "rel_acl": ["t1"], "func_owners": {"lingxi_retention_cleanup": ["lingxi_retention_owner"]},'
    ' "func_acl": ["lingxi_retention_cleanup"], "schema_owners": {"public": "pg_database_owner", "extensions": "postgres"},'
    ' "schema_acl": ["public"], "retention_cleanup": {"owner": "lingxi_retention_owner", "prosecdef": true}}'
)
COUNTS_JSON = (
    '{"schema":1,"dumped_at":"2026-09-27T11:46:54Z","database":"postgres","alembic_head":"0098_x",'
    '"tables":{"public.t1":2},"sequences":{},"extensions":["pg_trgm@1.6","plpgsql@1.0"]}'
)

HEALTH_LOG_DB = (
    '[{"Start":"2026-09-27T10:00:01.5Z","End":"2026-09-27T10:00:01.9Z","ExitCode":1},'
    '{"Start":"2026-09-27T10:00:06.5Z","End":"2026-09-27T10:00:07.2Z","ExitCode":0}]'
)
HEALTH_LOG_APP = (
    '[{"Start":"2026-09-27T10:05:{s}.0Z","End":"2026-09-27T10:05:{s}.5Z","ExitCode":1},'
    '{"Start":"2026-09-27T10:05:{e}.0Z","End":"2026-09-27T10:05:{e}.4Z","ExitCode":0}]'
)

FAKE_DOCKER = r"""#!__PYTHON__
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
with (state / "calls.log").open("a", encoding="utf-8") as log:
    log.write("docker " + " ".join(args) + "\n")
db = os.environ.get("FAKE_DB_CONTAINER", "lingxi-db")
support = Path(os.environ["FAKE_SUPPORT"])
up = state / "db.up"


def text(name):
    return (support / name).read_text(encoding="utf-8")


def drain():
    sys.stdin.buffer.read()


cmd = args[0] if args else ""
if cmd == "compose":
    if "down" in args:
        up.unlink(missing_ok=True)
    elif "up" in args:
        up.touch()
    else:
        sys.exit(64)
    sys.exit(0)
if cmd == "inspect":
    fmt, name = args[args.index("-f") + 1], args[-1]
    app = {"lingxi-gateway-1": ("10", "40"), "lingxi-scheduler-1": ("12", "44"), "lingxi-worker-queue-1": ("11", "42")}
    if name == db:
        if not up.exists():
            sys.exit(1)
        if "StartedAt" in fmt:
            print("2026-09-27T10:00:00.123456789Z")
        elif "Health.Log" in fmt:
            print(text("health_db.json"))
        else:
            print("healthy")
        sys.exit(0)
    if name in app:
        s, e = app[name]
        if "StartedAt" in fmt:
            print("2026-09-27T10:05:%s.000000001Z" % s)
        elif "Health.Log" in fmt:
            print(text("health_app.json").replace("{s}", s).replace("{e}", e))
        else:
            print("healthy")
        sys.exit(0)
    sys.exit(1)
if cmd == "run" and "pg_restore" in args:
    drain()
    print(text("schema.sql"), end="")
    sys.exit(0)
if cmd == "exec":
    rest = [a for a in args[1:] if a != "-i"]
    if rest[1:3] == ["rm", "-f"]:
        sys.exit(0)
    if rest[1] == "pg_dump" and "--schema-only" in rest:
        print(text("target_schema.sql"), end="")
        sys.exit(0)
    if rest[1] == "pg_restore":
        drain()
        if "--schema-only" in rest:
            print(text("schema.sql"), end="")
        elif "-l" in rest:
            print(text("toc.txt"), end="")
        else:
            (state / "restored").touch()
        sys.exit(0)
    if rest[1] == "psql":
        sql = sys.stdin.read()
        if "FROM pg_roles WHERE rolname = '" in sql:
            role = sql.split("rolname = '", 1)[1].rsplit("'", 1)[0].replace("''", "'")
            print(1 if role in ("postgres", "pg_database_owner") or (state / ("role_" + role)).exists() else 0)
        elif sql.startswith("CREATE ROLE "):
            role = sql[len("CREATE ROLE "):].rsplit(" NOLOGIN", 1)[0]
            role = role[1:-1].replace('""', '"')
            (state / ("role_" + role)).touch()
            with (state / "created_roles.log").open("a", encoding="utf-8") as fh:
                fh.write(role + "\n")
        elif "rel_owners" in sql:
            print(text("target.json"))
        elif "relkind IN ('r','p','S','v','m')) +" in sql:
            print(5 if (state / "restored").exists() else 0)
        elif "string_agg(nspname" in sql:
            print("pg_catalog,public,extensions")
        elif "string_agg(extname" in sql:
            print("plpgsql,pg_trgm")
        elif "nspowner::regrole::text FROM pg_namespace WHERE nspname" in sql:
            print("postgres")
        elif sql.startswith("ALTER SCHEMA"):
            with (state / "alter_schema.log").open("a", encoding="utf-8") as fh:
                fh.write(sql)
        elif "c.relkind IN ('r','p')" in sql:
            print(1)
        elif "alembic_version" in sql:
            print("0098_x")
        else:
            print("fake docker：未模拟的 SQL " + sql[:60], file=sys.stderr)
            sys.exit(64)
        sys.exit(0)
if cmd == "cp":
    sys.exit(0)
if cmd == "rm":
    up.unlink(missing_ok=True)
    sys.exit(0)
print("fake docker：未模拟的调用 " + " ".join(args[:3]), file=sys.stderr)
sys.exit(64)
"""

# 切换脚本桩：只记子命令；install-pg 按 db_switch 的真实效果在假根里建出口令 / compose / 数据目录 / hosts 行并「起」容器
FAKE_SWITCH = r"""#!/usr/bin/env bash
set -u
st="${FAKE_STATE:?}"; r="${LINGXI_S30_ROOT:-}"
printf 'switch %s work=%s\n' "$*" "${LINGXI_S30_WORK_DIR:-}" >> "$st/calls.log"
[[ "${FAKE_SWITCH_FAIL:-}" == "$1" ]] && { echo "s30 错误：桩按要求失败" >&2; exit 1; }
case "$1" in
  stop-write|recreate-services) ;;
  install-pg)
    [[ -f "$LINGXI_S30_WORK_DIR/preflight.env" ]] || { echo "缺 preflight.env" >&2; exit 1; }
    cp "$LINGXI_S30_WORK_DIR/preflight.env" "$st/preflight.env.seen"
    install -d -m 700 "$r/etc/lingxi/db" "$r/opt/lingxi/db" "$r/var/lib/lingxi/pgdata"
    printf 'POSTGRES_PASSWORD=%s\n' "${FAKE_NEW_PW:?}" > "$r/etc/lingxi/db/.env.db"
    echo 'LINGXI_DB_ENV_FILE=new' > "$r/etc/lingxi/db/compose.env"
    echo 'services: {}' > "$r/opt/lingxi/db/compose.db.yaml"
    echo new > "$r/var/lib/lingxi/pgdata/PG_VERSION"
    printf '127.0.0.1 lingxi-db  # lingxi-s30\n' >> "$r/etc/hosts"
    touch "$st/db.up" ;;
  *) echo "fake switch：未模拟的子命令 $1" >&2; exit 64 ;;
esac
"""


def write_executable(path: Path, body: str) -> Path:
    """写一个 0755 可执行文件（父目录不存在则建）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def install_fakes(bin_dir: Path, support_dir: Path) -> dict[str, Path]:
    """装 docker / 切换脚本两个桩，并把回放素材写进 ``support_dir``（用例可改写其中任一份）。"""
    support_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("schema.sql", SCHEMA_SQL),
        ("target_schema.sql", SCHEMA_SQL),
        ("toc.txt", TOC),
        ("target.json", TARGET_JSON),
        ("health_db.json", HEALTH_LOG_DB),
        ("health_app.json", HEALTH_LOG_APP),
    ):
        (support_dir / name).write_text(body, encoding="utf-8")
    return {
        "docker": write_executable(
            bin_dir / "docker", FAKE_DOCKER.replace("__PYTHON__", sys.executable)
        ),
        "switch": write_executable(bin_dir / "db_switch_fake.sh", FAKE_SWITCH),
    }
