#!/usr/bin/env bash
# 迁移链门禁（Issue #53）：两条血统建库并逐字节对比，然后 alembic 整链前滚。
#
# 过渡期里生产表结构有两条血统：顶层编号 SQL（旧库已经按它建成）与 alembic revision
# 链（新库按它建）。**只测其中一条是不够的**——两条建出结构相同但对象名字不同的库，
# 建库时不会报任何错，等到某条按名字 DROP CONSTRAINT 的迁移落地才炸，而那时两边
# 已经跑了几个月。所以这里每次门禁都实际建两个库，然后要求 pg_dump 逐字节相等。
#
# 这同时保证了基线 revision 里那份「006–012 逐字节副本」不会与原文悄悄分叉：
# 分叉必然改变某个对象，diff 立刻红。
#
# 断言：V-迁移-01（等价）、V-迁移-02（基线在旧库上必须失败）、V-迁移-03（前滚与幂等）、
# V-迁移-05（无默认连接串）、V-迁移-06（只动一次性 scratch 库）。
# 不需要数据库的那半边在 scripts/ci/check_alembic_revisions.py。

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository_root=$(cd -- "${script_dir}/../.." && pwd)
cd "${repository_root}"

if [[ -z "${LINGXI_POSTGRES_CONTAINER:-}" ]]; then
  printf '未设置 LINGXI_POSTGRES_CONTAINER，迁移链检查需要真实 PostgreSQL 容器。\n' >&2
  exit 1
fi
if [[ -z "${LINGXI_POSTGRES_DSN:-}" ]]; then
  printf '未设置 LINGXI_POSTGRES_DSN，无法推导 scratch 库的连接串。\n' >&2
  exit 1
fi

container="${LINGXI_POSTGRES_CONTAINER}"
# 两个一次性库。名字里带 scratch，任何人在容器里看到它们都知道可以直接删。
sql_database='lingxi_migrate_scratch_sql'
alembic_database='lingxi_migrate_scratch_alembic'
# 往返检查用的参考库：每退一步就用它建一个「空库直达该版本」的对照。
reference_database='lingxi_migrate_scratch_ref'
workspace=$(mktemp -d)

psql_do() {
  local database="$1"
  shift
  docker exec "${container}" psql -q -v ON_ERROR_STOP=1 -U postgres -d "${database}" "$@"
}

drop_database() {
  # FORCE 断开残留连接：失败路径上 alembic 可能还没来得及释放。
  docker exec "${container}" psql -q -U postgres -d postgres \
    -c "DROP DATABASE IF EXISTS $1 WITH (FORCE);" >/dev/null 2>&1 || true
}

cleanup() {
  rm -rf "${workspace}"
  drop_database "${sql_database}"
  drop_database "${alembic_database}"
  drop_database "${reference_database}"
}
trap cleanup EXIT

# scratch 库的连接串由业务 DSN 换库名得到：CI 与本机的端口、口令各不相同，
# 硬编码任何一处都会让另一处失效。**全程不打印**，它可能带口令（V-迁移-06）。
scratch_dsn() {
  LINGXI_SCRATCH_DATABASE="$1" python3 - <<'PY'
import os
import sys
from urllib.parse import urlsplit, urlunsplit

parts = urlsplit(os.environ["LINGXI_POSTGRES_DSN"])
sys.stdout.write(urlunsplit(parts._replace(path="/" + os.environ["LINGXI_SCRATCH_DATABASE"])))
PY
}

# 基线与 head 都问 alembic 自己要，不在脚本里抄一份：抄的那份会过期，
# 而过期表现为「对比了一个不存在的 revision」这种沉默失败。
alembic_revision() {
  ALEMBIC_QUERY="$1" python3 - <<'PY'
import os
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

script = ScriptDirectory.from_config(Config("alembic.ini"))
revisions = script.get_bases() if os.environ["ALEMBIC_QUERY"] == "base" else script.get_heads()
if len(revisions) != 1:
    sys.stderr.write(f"revision 链的 {os.environ['ALEMBIC_QUERY']} 不唯一：{revisions}\n")
    raise SystemExit(1)
sys.stdout.write(revisions[0])
PY
}

# pg_dump 归一化：
#   --exclude-table 把 alembic_version 连同它的主键一并排除（实测：排除后 dump 里
#     再无任何 alembic 字样）。这就是验收口径里「白名单仅 alembic_version 表及其 PK」，
#     用排除实现之后，剩下的差异必须为**零**，不需要人去判断哪些差异可以放过。
#   \restrict / \unrestrict 是 pg_dump 每次调用现生成的随机串，与库内容无关；
#     不去掉它，同一个库连续两次 dump 都不相等。
#   -- Dumped 两行含版本信息。
normalized_dump() {
  docker exec "${container}" pg_dump \
    --schema-only --no-owner --no-privileges \
    --schema=public --exclude-table=public.alembic_version \
    -U postgres -d "$1" |
    grep -v -E '^(-- Dumped |\\restrict |\\unrestrict )'
}

baseline_revision=$(alembic_revision base)
head_revision=$(alembic_revision head)

# ---------- 不设连接串必须明确失败（V-迁移-05）----------
# 注意此刻环境里 **LINGXI_POSTGRES_DSN 是有值的**：这一步同时证明 alembic 不会
# 顺手回落到业务 DSN。回落意味着「忘了设变量」会把迁移跑进业务库而无人察觉。
if LINGXI_MIGRATION_DSN='' python3 -m alembic current >"${workspace}/no_dsn.log" 2>&1; then
  printf '未设置 LINGXI_MIGRATION_DSN 时 alembic 仍然成功了：说明存在默认连接串或回落路径。\n' >&2
  exit 1
fi
if ! grep -q 'LINGXI_MIGRATION_DSN' "${workspace}/no_dsn.log"; then
  printf '未设置连接串时的失败信息没有指出是哪个环境变量缺失：\n' >&2
  cat "${workspace}/no_dsn.log" >&2
  exit 1
fi
printf '缺少 LINGXI_MIGRATION_DSN：明确失败（无默认连接串、不回落到业务 DSN）\n'

# ---------- 第一条血统：顶层编号 SQL ----------
drop_database "${sql_database}"
drop_database "${alembic_database}"
psql_do postgres -c "CREATE DATABASE ${sql_database};"
# nullglob：不开这个，glob 没匹配到任何文件时 bash 会把**模式原文**留在数组里，
# 于是长度是 1、下面那个「一个都没有」的分支永远不触发，然后 psql 去读一个叫
# `migrations/*.sql` 的文件报一句难懂的错（内审指出的死代码）。
shopt -s nullglob
migration_files=(migrations/*.sql)
shopt -u nullglob
if ((${#migration_files[@]} == 0)); then
  printf 'migrations/ 下没有编号 SQL，迁移链检查失去对象。\n' >&2
  exit 1
fi
for migration_file in "${migration_files[@]}"; do
  docker exec -i "${container}" psql -q -v ON_ERROR_STOP=1 -U postgres -d "${sql_database}" \
    < "${migration_file}"
done
printf '编号 SQL 链建库：通过（%s）\n' "${migration_files[*]}"

# ---------- 第二条血统：alembic 到基线 ----------
psql_do postgres -c "CREATE DATABASE ${alembic_database};"
alembic_dsn=$(scratch_dsn "${alembic_database}")
LINGXI_MIGRATION_DSN="${alembic_dsn}" python3 -m alembic upgrade "${baseline_revision}"
printf 'alembic 建库至基线：通过（%s）\n' "${baseline_revision}"

# ---------- 等价性：两条血统必须逐字节相等（V-迁移-01）----------
normalized_dump "${sql_database}" > "${workspace}/chain.sql"
normalized_dump "${alembic_database}" > "${workspace}/alembic.sql"
if ! diff -u "${workspace}/chain.sql" "${workspace}/alembic.sql"; then
  printf '\n编号 SQL 链与 alembic 基线建出的表结构不一致（上面是 diff）。\n' >&2
  printf '两条血统的对象名字必须完全相同，否则按名字操作的迁移会一边成功一边失败。\n' >&2
  exit 1
fi
printf '两条血统等价：通过（pg_dump 归一化后 %s 行，零差异）\n' "$(wc -l < "${workspace}/chain.sql")"

# ---------- 旧库未 stamp 直接 upgrade 必须失败（V-迁移-02）----------
# 这是**否定断言**，不是补充测试。基线一旦被人"顺手"加上 IF NOT EXISTS 或改成
# checkfirst，它就能在已有对象的库上"跑得通"——于是 alembic_version 记下"做过了"，
# 而实际什么都没做。响亮的错误换成沉默的错误，之后每一条迁移都建立在假的起点上。
# 旧库的正确接管路径是先 stamp，见 migrations/README.md。
sql_dsn=$(scratch_dsn "${sql_database}")
if LINGXI_MIGRATION_DSN="${sql_dsn}" python3 -m alembic upgrade head \
  >"${workspace}/no_stamp.log" 2>&1; then
  printf '已建成的旧库上直接 alembic upgrade 竟然成功了。\n' >&2
  printf '基线必须在已有对象时失败；若它加了 IF NOT EXISTS 之类的幂等伪装，请去掉。\n' >&2
  exit 1
fi
if ! grep -qi 'already exists' "${workspace}/no_stamp.log"; then
  printf '旧库直接 upgrade 失败了，但原因不是「对象已存在」，请人工确认：\n' >&2
  tail -5 "${workspace}/no_stamp.log" >&2
  exit 1
fi
# 失败之后库必须原样不动：整条 revision 在一个事务里，回滚要彻底。
normalized_dump "${sql_database}" > "${workspace}/chain_after.sql"
if ! diff -u "${workspace}/chain.sql" "${workspace}/chain_after.sql"; then
  printf '\n旧库上失败的 upgrade 改变了表结构（上面是 diff），回滚不彻底。\n' >&2
  exit 1
fi
stray_version=$(psql_do "${sql_database}" -tAc \
  "SELECT count(*) FROM pg_class WHERE relname = 'alembic_version' AND relnamespace = 'public'::regnamespace;")
if [[ "${stray_version}" != "0" ]]; then
  printf '失败的 upgrade 仍然留下了 alembic_version 表：库会被误认为已接管。\n' >&2
  exit 1
fi
printf '旧库未 stamp 直接 upgrade：按预期失败（对象已存在），且库与版本表均未改变\n'

# ---------- 前滚：基线之后的 revision 整链跑到 head（V-迁移-03）----------
# 用单数 head：多 head 时 alembic 自己就会报错。孤儿 revision 由
# scripts/ci/check_alembic_revisions.py 显式断言，那条不需要数据库。
LINGXI_MIGRATION_DSN="${alembic_dsn}" python3 -m alembic upgrade head
recorded=$(psql_do "${alembic_database}" -tAc 'SELECT version_num FROM alembic_version;')
recorded_rows=$(psql_do "${alembic_database}" -tAc 'SELECT count(*) FROM alembic_version;')
if [[ "${recorded}" != "${head_revision}" || "${recorded_rows}" != "1" ]]; then
  printf 'alembic_version 记录的是 %s（%s 行），期望 %s（1 行）。\n' \
    "${recorded}" "${recorded_rows}" "${head_revision}" >&2
  exit 1
fi
printf 'alembic 整链前滚至 head：通过（%s，版本表恰 1 行）\n' "${head_revision}"

# ---------- 重复执行必须是空操作 ----------
# 「已经在 head 上再跑一次」是部署重试与容器重启的常态；它必须什么都不做。
normalized_dump "${alembic_database}" > "${workspace}/head.sql"
LINGXI_MIGRATION_DSN="${alembic_dsn}" python3 -m alembic upgrade head
normalized_dump "${alembic_database}" > "${workspace}/head_again.sql"
if ! diff -u "${workspace}/head.sql" "${workspace}/head_again.sql"; then
  printf '在 head 上重跑 upgrade 改变了表结构。\n' >&2
  exit 1
fi
printf 'head 上重跑 upgrade：通过（结构不变）\n'

# ---------- 逐条 revision 真往返（V-迁移-08）----------
# 静态检查只看得出 `downgrade()` 不是空函数，看不出它做的事对不对——
# `if False: op.drop_table(...)` 一样能过形状检查，而真库从不执行 downgrade，
# 于是「基线之上每条 revision 可真往返」这句承诺一直没有任何机器在守（内审 / 外审
# 共同指出）。这里在真库上走一遍：先一路 downgrade 回基线，每退一步都与
# 「空库直接 upgrade 到那一版」逐字节比对；再一路 upgrade 回 head，每进一步都与
# 下行时同一版本记下的 dump 逐字节比对。
#
# 当前链上只有基线，下面两个循环都是空的、零成本；#54 的第一条 revision 落地即生效。
round_trip_pairs=$(python3 - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory

script = ScriptDirectory.from_config(Config("alembic.ini"))
base = script.get_bases()[0]
head = script.get_heads()[0]
for revision in script.iterate_revisions(head, base):
    if revision.revision == base:
        continue
    parent = revision.down_revision
    if not isinstance(parent, str):
        raise SystemExit(
            f"{revision.revision} 的 down_revision 是 {parent!r}（不是单个 id），"
            "往返检查覆盖不到它，请先处理这条 revision 的形状"
        )
    print(f"{revision.revision} {parent}")
PY
)

if [[ -z "${round_trip_pairs}" ]]; then
  printf '逐条 revision 往返：本次无对象（链上只有基线，基线按设计不可 downgrade）\n'
else
  # 下行：head → base，逐步 downgrade 并与空库直达同一版本的结果比对。
  while read -r revision parent; do
    normalized_dump "${alembic_database}" > "${workspace}/at_${revision}.sql"
    LINGXI_MIGRATION_DSN="${alembic_dsn}" python3 -m alembic downgrade "${parent}"
    normalized_dump "${alembic_database}" > "${workspace}/down_to_${parent}.sql"

    drop_database "${reference_database}"
    psql_do postgres -c "CREATE DATABASE ${reference_database};"
    reference_dsn=$(scratch_dsn "${reference_database}")
    LINGXI_MIGRATION_DSN="${reference_dsn}" python3 -m alembic upgrade "${parent}"
    normalized_dump "${reference_database}" > "${workspace}/fresh_${parent}.sql"

    if ! diff -u "${workspace}/fresh_${parent}.sql" "${workspace}/down_to_${parent}.sql"; then
      printf '\nrevision %s 的 downgrade 没有真正回到 %s（上面是 diff）。\n' "${revision}" "${parent}" >&2
      printf 'downgrade 必须真正逆转 upgrade，否则回滚只是把版本号改小了。\n' >&2
      exit 1
    fi
    stepped=$(psql_do "${alembic_database}" -tAc 'SELECT version_num FROM alembic_version;')
    if [[ "${stepped}" != "${parent}" ]]; then
      printf 'downgrade 之后 alembic_version 是 %s，期望 %s。\n' "${stepped}" "${parent}" >&2
      exit 1
    fi
    printf 'revision %s：downgrade 到 %s 与空库直达结果一致\n' "${revision}" "${parent}"
  done <<< "${round_trip_pairs}"

  # 上行：base → head，逐步 upgrade 并与下行时同一版本记下的 dump 比对。
  tac <<< "${round_trip_pairs}" | while read -r revision _parent; do
    LINGXI_MIGRATION_DSN="${alembic_dsn}" python3 -m alembic upgrade "${revision}"
    normalized_dump "${alembic_database}" > "${workspace}/back_${revision}.sql"
    if ! diff -u "${workspace}/at_${revision}.sql" "${workspace}/back_${revision}.sql"; then
      printf '\nrevision %s 往返之后的结构与往返之前不一致（上面是 diff）。\n' "${revision}" >&2
      exit 1
    fi
    printf 'revision %s：upgrade 回来后结构与往返前一致\n' "${revision}"
  done || exit 1

  drop_database "${reference_database}"
  printf '逐条 revision 真往返：通过（%s 条）\n' "$(wc -l <<< "${round_trip_pairs}")"
fi

# ---------- 主测试库不得被碰（V-迁移-06）----------
# 门禁的迁移检查只动一次性 scratch 库。主测试库里冒出 alembic_version，
# 说明某一步把连接串指错了，而那种错误平时不会有任何症状。
main_database=$(LINGXI_MAIN_DSN="${LINGXI_POSTGRES_DSN}" python3 - <<'PY'
import os
import sys
from urllib.parse import urlsplit

sys.stdout.write(urlsplit(os.environ["LINGXI_MAIN_DSN"]).path.lstrip("/"))
PY
)
if [[ -n "${main_database}" ]]; then
  touched=$(psql_do "${main_database}" -tAc \
    "SELECT count(*) FROM pg_class WHERE relname = 'alembic_version' AND relnamespace = 'public'::regnamespace;")
  if [[ "${touched}" != "0" ]]; then
    printf '主测试库 %s 里出现了 alembic_version：门禁的迁移步骤碰了不该碰的库。\n' "${main_database}" >&2
    exit 1
  fi
  printf '主测试库 %s：未被迁移步骤写入（无 alembic_version）\n' "${main_database}"
fi

printf '迁移链检查：全部通过\n'
