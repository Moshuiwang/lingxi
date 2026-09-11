#!/usr/bin/env bash
# 备份恢复真实演练：DSN 版，面向云托管库（Trace #373 H2 批 S-H2-5 首版；
# Trace #754 S-1-3 改为 DSN 版，见 Issue #693）。
#
# 演练什么：从**源库**（任意可达的 Postgres，含 Supabase 等云托管库，用连接串
# 而不是本机容器识别）只读 `pg_dump -Fc` 一次 → 恢复进一个全新建立、不发布任何
# 宿主端口的隔离 `postgres:17` 实例 → 前滚 `alembic upgrade head` 到链头 → 核对
# 恢复后行数与源库一致 → 向隔离实例（不是源库）注入原始写入时间已超过 90 天的
# 合成行，同时保留恢复出来的真实未到期行作对照 → 用 scheduler 镜像里真实装配的
# 保留清理职责（不是手写 SQL）对隔离实例补跑一次 → 核对「按原始写入时间计算
# 到期，恢复不重置保留起点」→ 销毁隔离实例与全部临时产物。断言依据：
# docs/技术设计/验收矩阵-交付与投递.md `V-投递-07`；恢复后先补清理再对外服务的
# 要求见 deploy/生产部署runbook.md 第八节。
#
# **本版与旧版（334 行，仅 Trace #373 版本）的关键差异**：源库不再是本机容器
# （`docker exec pg_dump`），而是 DSN——stage 与生产库都是 Supabase 云托管，没有
# 可 `exec` 的容器（#693 核实到的事实）。连带地：预发主机的 `pg_dump` /
# `pg_restore` / `pg_isready` / `psql` 客户端是 16.15，Supabase 服务端是
# 17.6，`server version mismatch` 直接失败（编排者 2026-09-11 实测）；因此**凡是
# 需要连接源库的客户端调用，一律经一次性 `postgres:17` 容器执行，不依赖主机
# 客户端**——这是本脚本对源库的唯一接触方式。恢复目标（隔离实例）本身也是
# `postgres:17`，其内建客户端天然是同版本，`docker exec` 到它身上不受此限制。
#
# **破坏半径（先读这一段再执行）**：
#   - 对源库**只有**三次只读连接：`pg_isready` 预检、`pg_dump -Fc` 备份、
#     恢复并迁移之后的一次逐表行数回读（`step_verify_row_counts`，只读
#     `count(*)`）；本脚本不建立任何写连接，不 stop/restart 任何服务，不重启
#     任何容器，不触碰源库所在主机（此前版本头注释漏记了行数回读这一次，
#     外审 codex gpt-5.6-sol 2026-09-11 指出）。
#   - **行数回读假设演练窗口内源库静默（无并发写入）**：`pg_dump` 与行数回读
#     是两次独立连接，不共享同一快照——源库若在这两次连接之间有正常业务写入
#     （新增/更新/删除任何一行），逐表计数就会出现差异，脚本按 `set -euo
#     pipefail` 的一贯纪律判定失败退出。**这不代表恢复出了问题**，只代表
#     源库在演练窗口内不是静止的；见 `step_verify_row_counts` 失败信息与下方
#     「前提」。在预发/生产执行前，建议挑一个源库写入极少的窗口（回读语句本身
#     耗时通常在秒级），或在差异出现后人工核对差异行是否对应这段时间内的正常
#     业务写入。
#   - 全部有副作用的操作只发生在本脚本新建的隔离容器/网络里（默认
#     `lingxi-drill-db` / `lingxi-drill-net`），随脚本收尾一并销毁；隔离数据库
#     密码现场随机生成，只作为该容器的环境变量存在，容器销毁即失效，不落盘、
#     不打印、不进日志。
#   - 注入的到期样本是固定化名合成数据（`drill-synthetic-*`/`演练*`），不使用
#     真实业务数据（验证与门禁 §十三）。
#   - 补跑的保留清理职责只接数据库连接串（隔离实例的临时 DSN），**不带任何
#     外部凭据**——不注入飞书应用凭据、不接凭据轮换、不起常驻 scheduler/gateway
#     进程，因此不会消费出站流量或接管任何长连接（合同 E-3 ⑤）。
#   - **任何一步失败都会因 `set -euo pipefail` 立即停止，脚本不做自动清理**——
#     现场（隔离容器、网络、dump 文件）原样保留供取证，失败信息会指出如何手动
#     核对与之后如何清理（见下方“失败后如何处理”）。这是有意的：先取证、
#     再销毁，不为了让脚本看起来"跑完"而在失败时静默擦掉现场。
#   - **源库 DSN 只从环境变量读，本脚本不接受、也不解析任何命令行参数**；DSN
#     本身不打印、不落日志——唯一例外是源库操作失败时，会把捕获到的错误文本
#     经 `mask()` 脱敏后打到 stderr 供排障，覆盖 URI 形态（`user:pass@`，含
#     `postgresql+psycopg://`）与 keyword/URI 查询参数形态（`password=...`）。
#
# **前提**：
#   - 在能以只读身份连到源库 DSN、且能 `docker run`（含 `--network host`）/
#     `docker network create` 的宿主机上执行；当前验证过的环境是 tz 本机专属
#     测试容器（Trace #754 S-1-3 本机跑通一次）；预发主机 `biai-stage` 由 S-2-5
#     执行验证。
#   - `postgres:17`、指定的 scheduler 镜像与 migrate 镜像已经在本机，或本机能
#     `docker pull` 到（脚本不会静默重试拉取失败；缺镜像直接失败）。
#   - 磁盘要有余量：dump 与隔离实例的数据量级与源库相当，加上三个镜像的本地
#     层；执行前后各 `df -h` 一次自行核对，不由脚本代为判断磁盘是否够用。
#   - **源库在整个演练窗口内应当基本静默**（见上方「破坏半径」行数回读一条）：
#     `pg_dump` 与行数回读不在同一快照里，源库若持续有写入，行数比对会假红。
#     这不是本脚本能替调用方判断的事，执行前自行确认窗口，执行中出现差异先
#     核对是不是这段时间内的正常业务写入，不要不看原因就重跑。
#
# **失败后如何处理**：先用 `docker logs "${LINGXI_DRILL_DB_CONTAINER:-lingxi-drill-db}"`、
# 查看恢复日志（脚本打印的 `RESTORE_LOG` 路径）和上面打印的最后一步取证；确认
# 证据留存完毕后手动执行（**`docker rm` 必须带 `-v`**——隔离库装的是恢复出来的
# 全部数据，不带 `-v` 删容器不删匿名卷，等于把这份数据原样留在宿主机上，外审
# codex gpt-5.6-sol 2026-09-11 指出的真实缺口）：
#   docker rm -f -v "${LINGXI_DRILL_DB_CONTAINER:-lingxi-drill-db}"
#   docker network rm "${LINGXI_DRILL_NETWORK:-lingxi-drill-net}"
#   rm -f <脚本打印的 dump/日志临时文件路径>
#   docker volume ls --filter "label=com.docker.compose.project" # 仅供比对，本脚本不用 compose
# `docker rm -v` 会清掉该容器挂的**全部**匿名卷（不止一个也一样，Docker 语义是
# 按容器一次性回收，不是逐卷计数）；`step_isolate_and_restore` 探测到多于一个
# 匿名卷时已经先行报错退出（见下方 P2-14 登记），那种情况下容器还没做任何
# 恢复动作，直接按上面这条 `docker rm -f -v` 处理即可，不需要另外逐个
# `docker volume rm`。若不放心，`docker inspect -f '{{ range .Mounts }}{{ if eq
# .Type "volume" }}{{ .Name }}{{ "\n" }}{{ end }}{{ end }}' <容器名>` 先列出卷名，
# `docker rm -f -v` 之后用同一条命令或 `docker volume inspect <卷名>` 回读确认
# 不存在（`step_destroy` 成功路径就是这么核对的）。
#
# **可覆盖的环境变量**：
#   LINGXI_DRILL_SOURCE_DSN           源库连接串，优先读取；未设时回落
#                                     LINGXI_MIGRATION_DSN（部署 env 里已有的
#                                     迁移 DSN，二者选一即可，不要求同时设置）。
#                                     两者都缺时脚本快速失败，只报变量名。值可以
#                                     像 `deploy/.env.stage.migrate` 里那样带一对
#                                     包裹单引号、协议是 `postgresql+psycopg://`
#                                     ——脚本会自动去引号并把协议改写成
#                                     `postgresql://`，调用方不需要手工改 env。
#   LINGXI_DRILL_NETWORK              隔离网络名，默认 lingxi-drill-net
#   LINGXI_DRILL_DB_CONTAINER         隔离数据库容器名，默认 lingxi-drill-db
#   LINGXI_DRILL_POSTGRES_IMAGE       隔离数据库镜像与源库只读工具镜像共用同一个
#                                     引用，默认 postgres:17（对齐 Supabase 服务端
#                                     大版本；见文件头「关键差异」段）
#   LINGXI_DRILL_SCHEDULER_IMAGE      承载真实保留清理代码路径的 scheduler 镜像
#                                     引用，**必须显式指定**（例如
#                                     ghcr.io/moshuiwang/lingxi-scheduler:<tag>）。
#   LINGXI_DRILL_MIGRATE_IMAGE        承载 `alembic upgrade head` 的 migrate 镜像
#                                     引用，**必须显式指定**（例如
#                                     ghcr.io/moshuiwang/lingxi-migrate:<tag>）。
#                                     以上两个镜像均不设默认值：用哪个候选镜像
#                                     跑迁移/清理，必须是调用方的显式选择，不能
#                                     悄悄落到 latest。
#   LINGXI_DRILL_DUMP_PATH            dump 文件落盘路径，默认 mktemp 生成，权限
#                                     显式收紧到 0600。`-Fc` 是自定义压缩格式、
#                                     不是明文 SQL 文本，但同样未加密——不需要
#                                     密码即可用 `pg_restore` 还原出全部业务
#                                     数据，风险等同明文，同等对待。成功路径下
#                                     step_destroy 会立即删除它；失败路径下按
#                                     上面"失败后如何处理"留作取证，不依赖操作者
#                                     记得手动清理（独立审查 P2-15 的登记延续）。
#                                     **若未来把本脚本用于生产环境**，落盘前须
#                                     补加密，本次改动不改变这一条。
#   LINGXI_DRILL_INJECT_SYNTHETIC     是否注入合成到期样本并核对保留清理语义。
#                                     **不设置、或设为空字符串**时按默认 `1`
#                                     处理（shell 的 `${VAR:-1}` 语义——空字符串
#                                     等同未设置，不是一个"合法取值"）；**一旦
#                                     显式设成非空值，只接受 `0` 或 `1` 这两个
#                                     字面值，其余一律预检时响亮失败**，不静默
#                                     当成"跳过"处理（例如误写成 `true`/`yes`
#                                     会直接报错，不会悄悄退化成 0 那条最简
#                                     路径；外审 codex gpt-5.6-sol 2026-09-11
#                                     指出的真实缺口，外审复核 2026-09-11 核对
#                                     本段措辞与 `${LINGXI_DRILL_INJECT_SYNTHETIC:-1}`
#                                     的空字符串行为一致）。`1` 时做完整核对；
#                                     `0` 时只做备份/恢复/迁移/行数完整性检查
#                                     （生产 runbook 里最简的"确认能恢复"子集，
#                                     不核对保留语义）——两种模式下行数回读都
#                                     会执行。
#
# 用法（预发/生产正式执行必须显式带 `LINGXI_DRILL_INJECT_SYNTHETIC=1`，不依赖
# 默认值——命令本身就是这次演练做没做完整核对的凭据）：
#   LINGXI_DRILL_SOURCE_DSN=postgresql://... \
#   LINGXI_DRILL_INJECT_SYNTHETIC=1 \
#   LINGXI_DRILL_SCHEDULER_IMAGE=ghcr.io/moshuiwang/lingxi-scheduler:<tag> \
#   LINGXI_DRILL_MIGRATE_IMAGE=ghcr.io/moshuiwang/lingxi-migrate:<tag> \
#     scripts/ops/backup_restore_drill.sh
set -euo pipefail

DRILL_NETWORK="${LINGXI_DRILL_NETWORK:-lingxi-drill-net}"
DRILL_DB_CONTAINER="${LINGXI_DRILL_DB_CONTAINER:-lingxi-drill-db}"
DRILL_POSTGRES_IMAGE="${LINGXI_DRILL_POSTGRES_IMAGE:-postgres:17}"
DRILL_SCHEDULER_IMAGE="${LINGXI_DRILL_SCHEDULER_IMAGE:?必须指定用于执行真实保留清理代码路径的 scheduler 镜像引用，例如 ghcr.io/moshuiwang/lingxi-scheduler:<tag>}"
DRILL_MIGRATE_IMAGE="${LINGXI_DRILL_MIGRATE_IMAGE:?必须指定用于执行 alembic upgrade head 的 migrate 镜像引用，例如 ghcr.io/moshuiwang/lingxi-migrate:<tag>}"
INJECT_SYNTHETIC="${LINGXI_DRILL_INJECT_SYNTHETIC:-1}"
DUMP_PATH="${LINGXI_DRILL_DUMP_PATH:-}"

# 隔离实例自己的引导身份：与源库的用户/库名彻底解耦（源库现在是一个不透明的
# DSN，脚本不假设、也不需要知道它的用户名或库名叫什么）。pg_restore 只需要
# 连进**某个**已存在的数据库，dump 内容本身带着完整的表/schema 定义。
DRILL_DB_NAME="lingxi_drill"
DRILL_DB_USER="lingxi_drill"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "缺少命令：$1" >&2; exit 1; }
}

# 连接信息脱敏：覆盖两种 DSN 形态各自暴露口令的位置。① URI 形态
# `postgresql://user:pass@host/db`（含转换前的 `postgresql+psycopg://`）——
# 口令在「用户:口令@」这一段；② keyword/value 形态 `host=... password=...`
# 与 URI 查询参数形态 `postgresql://host/db?password=...`——两者口令都以
# 字面 `password=` 开头，到下一个 `&` 或空白为止。只盖第一种曾经是外审
# codex gpt-5.6-sol 2026-09-11 指出的真实缺口：libpq 三种 DSN 写法都合法，
# 调用方给哪种形态本脚本不作限制，脱敏必须覆盖全部三种，不能只认 URI 的
# user:pass@ 这一种。失败文本一律先过这一道再落 stderr。
mask() {
  sed -E 's#://[^@[:space:]]*@#://<creds>@#g; s#[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]=[^&[:space:]]*#password=<creds>#g'
}

# 源库连接串解析：优先 LINGXI_DRILL_SOURCE_DSN，缺省回落 LINGXI_MIGRATION_DSN
# （部署 env 已有的同一份迁移 DSN，二选一，不要求同时设置）。两者都缺时只报
# 变量名，不回显任何取到的值。
resolve_source_dsn() {
  local raw=""
  if [[ -n "${LINGXI_DRILL_SOURCE_DSN:-}" ]]; then
    raw="${LINGXI_DRILL_SOURCE_DSN}"
  elif [[ -n "${LINGXI_MIGRATION_DSN:-}" ]]; then
    raw="${LINGXI_MIGRATION_DSN}"
  else
    echo "缺少源库连接串：设置 LINGXI_DRILL_SOURCE_DSN，或复用已有的 LINGXI_MIGRATION_DSN，二选一（不回显取到的值）" >&2
    exit 1
  fi
  # deploy/.env.stage.migrate 里的 DSN 字面带一对包裹单引号、协议是
  # postgresql+psycopg://（SQLAlchemy 方言后缀）；libpq 系工具（pg_dump/
  # pg_restore/pg_isready/psql）既不认识这个方言后缀也不会自己去引号，这里
  # 统一处理一次，调用方不需要为了跑这个脚本手工改 env 文件里的值。
  raw="${raw%\'}"
  raw="${raw#\'}"
  printf '%s' "${raw}" | sed -E 's#^postgresql\+psycopg://#postgresql://#'
}

# 对源库的全部只读客户端调用：统一经一次性 postgres:17 容器 + 宿主机网络
# 执行，不依赖本机/预发主机安装的 libpq 客户端版本（见文件头「关键差异」段）。
# DSN 以 -d 参数直接传给目标命令（libpq 系工具的 -d/--dbname 原生支持完整
# 连接串，实测确认）——与本文件对隔离实例随机口令的处理（经 -e 环境变量、
# 不进容器主进程 argv）不是同一等级的值：那是脚本自建、生命周期只到本次
# 演练收尾的临时口令；这里是调用方持有的源库凭据，风险等级更高，因此仍然
# 只从环境变量读入、绝不接受命令行参数、绝不打印或写日志（脚本头「破坏
# 半径」段）——这条边界在脚本的对外接口层面成立，`run_source_tool` 只是把
# 已经校验过的 SOURCE_DSN 转发给一次性容器内的 pg_dump/pg_isready/psql 三个
# 短生命周期进程，不做二次持久化。
run_source_tool() {
  docker run --rm --network host "${DRILL_POSTGRES_IMAGE}" "$@" -d "${SOURCE_DSN}"
}

step_preflight() {
  log "== 预检 =="
  # 只接受 0 或 1，其余值响亮失败——`true`/`yes`等一律不算"跳过"，避免调用方
  # 笔误把本该核对保留语义的一次真实执行悄悄降级成最简子集（外审 codex
  # gpt-5.6-sol 2026-09-11 指出的真实缺口）。**空字符串不会走到这条判断**：
  # 上面 `INJECT_SYNTHETIC="${LINGXI_DRILL_INJECT_SYNTHETIC:-1}"` 用的是 shell
  # `:-` 语义，未设置或设为空字符串在赋值这一步就已经落回默认值 `1`，不是
  # "0/1 之外的第三种取值"（外审复核 2026-09-11 核对头注释与这里的措辞一致）。
  if [[ "${INJECT_SYNTHETIC}" != "0" && "${INJECT_SYNTHETIC}" != "1" ]]; then
    echo "配置错误：LINGXI_DRILL_INJECT_SYNTHETIC 只接受 0 或 1，收到「${INJECT_SYNTHETIC}」——不是这两个字面值时不当成 0 处理，避免笔误静默跳过保留语义核对" >&2
    exit 1
  fi
  SOURCE_DSN=$(resolve_source_dsn)
  # 命名碰撞断言（独立审查 P2-12 的登记延续）：DRILL_DB_CONTAINER 与
  # DRILL_NETWORK 是脚本同时创建、同时存在的两个不同类型对象，同名会让不显式
  # 声明对象类型的排查命令（`docker inspect <名字>`）产生歧义。纯字符串比较，
  # 不依赖任何 docker 调用，放在最前面先查。
  if [[ "${DRILL_DB_CONTAINER}" == "${DRILL_NETWORK}" ]]; then
    echo "配置错误：LINGXI_DRILL_DB_CONTAINER 与 LINGXI_DRILL_NETWORK 同名（${DRILL_DB_CONTAINER}）——容器与网络是两类不同 docker 对象，但同名会让不显式声明对象类型的 docker 命令产生歧义" >&2
    exit 1
  fi
  require_cmd docker
  require_cmd openssl
  df -h /
  docker ps
  if docker ps -a --format '{{.Names}}' | grep -qx "${DRILL_DB_CONTAINER}"; then
    echo "残留容器 ${DRILL_DB_CONTAINER}，请先 docker rm -f 再重跑" >&2
    exit 1
  fi
  if docker network ls --format '{{.Name}}' | grep -qx "${DRILL_NETWORK}"; then
    echo "残留网络 ${DRILL_NETWORK}，请先 docker network rm 再重跑" >&2
    exit 1
  fi
  if [[ -z "${DUMP_PATH}" ]]; then
    DUMP_PATH=$(mktemp /tmp/lingxi-drill-dump-XXXXXX.dump)
  fi
  chmod 600 "${DUMP_PATH}"
  RESTORE_LOG=$(mktemp /tmp/lingxi-drill-restore-XXXXXX.log)
  log "dump 文件：${DUMP_PATH}（0600，pg_dump -Fc 自定义格式）；恢复日志：${RESTORE_LOG}"
}

step_precheck_source() {
  log "== 预检源库连通性（pg_isready，经 postgres:17 一次性容器，对 DSN）=="
  local output
  if ! output=$(run_source_tool pg_isready 2>&1); then
    printf '%s\n' "${output}" | mask >&2
    echo "源库 pg_isready 预检失败（已脱敏，见上）" >&2
    exit 1
  fi
  log "源库 pg_isready 预检通过"
}

step_backup() {
  log "== 备份（对源库唯一的写前接触：一次只读 pg_dump -Fc）=="
  local dump_stderr
  dump_stderr=$(mktemp /tmp/lingxi-drill-dump-stderr-XXXXXX.log)
  if ! run_source_tool pg_dump -Fc > "${DUMP_PATH}" 2>"${dump_stderr}"; then
    mask <"${dump_stderr}" >&2
    rm -f "${dump_stderr}"
    echo "源库 pg_dump 失败（已脱敏，见上）" >&2
    exit 1
  fi
  rm -f "${dump_stderr}"
  log "备份写入 ${DUMP_PATH}（$(du -h "${DUMP_PATH}" | cut -f1)）"
}

step_isolate_and_restore() {
  log "== 建立隔离实例（postgres:17，不发布任何宿主端口）=="
  docker network create "${DRILL_NETWORK}" >/dev/null
  local password
  password=$(openssl rand -hex 24)
  docker run -d --name "${DRILL_DB_CONTAINER}" --network "${DRILL_NETWORK}" \
    -e "POSTGRES_USER=${DRILL_DB_USER}" -e "POSTGRES_PASSWORD=${password}" \
    -e "POSTGRES_DB=${DRILL_DB_NAME}" "${DRILL_POSTGRES_IMAGE}" >/dev/null
  DRILL_DSN="postgresql://${DRILL_DB_USER}:${password}@${DRILL_DB_CONTAINER}:5432/${DRILL_DB_NAME}"
  # 官方 postgres 镜像在 Dockerfile 里声明了 `VOLUME /var/lib/postgresql/data`：
  # 即使这里没有传 `-v`，`docker run` 仍会为它悄悄建一个匿名卷。`docker rm -f`
  # 不带 `-v` 不会删这个匿名卷——数据库容器没了，装数据的卷还留在宿主机上，
  # 是一种不会报错的残留（旧版演练时实测踩到，登记在 PR #391 里）。这里记下
  # 卷名，销毁步骤按名核对它确实被删干净，不只信 `docker rm -v` 的退出码。
  #
  # go-template 输出加一个换行分隔（独立审查 P2-14 的登记延续）：不加分隔符时，
  # 如果这个容器意外挂了不止一个匿名卷，多个卷名会被原样拼接成一整段无法拆分
  # 的字符串——静默把"这里其实有两个卷"读成"这里只有一个奇怪名字的卷"。加
  # 分隔符后逐行判空，发现多于一个非空行就是需要人工确认的异常，不沿用"当作
  # 只有一个卷"继续往下跑。
  drill_volume_raw=$(docker inspect -f '{{ range .Mounts }}{{ if eq .Type "volume" }}{{ .Name }}{{ "\n" }}{{ end }}{{ end }}' "${DRILL_DB_CONTAINER}")
  drill_volume_count=$(printf '%s\n' "${drill_volume_raw}" | grep -c . || true)
  if (( drill_volume_count > 1 )); then
    echo "隔离数据库容器 ${DRILL_DB_CONTAINER} 挂了 ${drill_volume_count} 个匿名卷，超出预期的至多一个，需要人工确认后再继续：" >&2
    printf '%s\n' "${drill_volume_raw}" >&2
    exit 1
  fi
  DRILL_VOLUME=$(printf '%s\n' "${drill_volume_raw}" | grep . || true)

  # 官方 postgres 镜像首次初始化会起两轮服务端（本机 S-1-3 实测踩到、旧版
  # 没有测出来是因为这个脚本此前从未真正跑通过一次，见脚本头「关键差异」段
  # 引用的 #693 事实）：第一轮只监听本地 socket、专供 initdb 阶段的
  # `POSTGRES_DB` 建库脚本使用，跑完就地关闭；第二轮才是真正常驻、对外提供
  # 服务的实例。只判一次 `pg_isready` 会在第一轮的窗口期判定"就绪"，随后
  # 角色预建/恢复等真实连接会撞上"database system is shutting down"——这不是
  # 偶发抖动，是这个镜像的既定启动顺序。改成等日志里出现两次
  # "database system is ready to accept connections"（一次临时实例、一次
  # 常驻实例）才判定就绪，直接对应镜像自身的可观察行为，不是猜测的等待时长。
  local tries=0
  until [[ "$(docker logs "${DRILL_DB_CONTAINER}" 2>&1 | grep -c 'database system is ready to accept connections')" -ge 2 ]]; do
    tries=$((tries + 1))
    if (( tries > 60 )); then
      echo "隔离实例 60 次探测仍未完成两轮启动（initdb 临时实例 + 常驻实例）：${DRILL_DB_CONTAINER}" >&2
      exit 1
    fi
    sleep 1
  done
  # 双重确认：常驻实例的端口已经在监听、能真正应答 pg_isready，不只是日志行数够了。
  if ! docker exec "${DRILL_DB_CONTAINER}" pg_isready -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" >/dev/null 2>&1; then
    echo "隔离实例日志显示已完成两轮启动，但 pg_isready 仍未通过：${DRILL_DB_CONTAINER}" >&2
    exit 1
  fi
  log "隔离实例就绪：${DRILL_DB_CONTAINER}（常驻实例，非 initdb 临时实例）"

  log "== pg_restore --list 可读性检查（隔离实例自带 postgres:17 客户端，无需连库）=="
  local list_lines
  list_lines=$(docker exec -i "${DRILL_DB_CONTAINER}" pg_restore --list < "${DUMP_PATH}" | wc -l)
  log "dump 目录条目数：${list_lines}"

  log "== 恢复：预建角色骨架 + pg_restore --no-owner --no-privileges -n public =="
  # dump 是单库内容 dump，不含 CREATE ROLE（角色是集群级对象）。即使
  # --no-owner/--no-privileges 已经让 pg_restore 跳过 OWNER 与 GRANT 语句，
  # schema 里仍可能有其它引用角色名的对象（如行级安全策略的 TO 子句）；四个
  # lingxi_* 角色预先建好、建成 NOLOGIN，多余也无害——隔离实例里没有任何东西
  # 需要用它们登录，跟源库的角色定位一致。
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_app') THEN CREATE ROLE lingxi_app NOLOGIN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_scheduler') THEN CREATE ROLE lingxi_scheduler NOLOGIN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_retention_owner') THEN CREATE ROLE lingxi_retention_owner NOLOGIN; END IF;
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lingxi_migrate') THEN CREATE ROLE lingxi_migrate NOLOGIN; END IF;
    END
    \$\$;
  " >/dev/null

  # --exit-on-error：pg_restore 默认遇错继续跑完、只在汇总里报警告，退出码仍可能
  # 是 0；本文件全程靠 `set -e` 判定成败，没有这个开关会让恢复过程中的错误被
  # `set -e` 漏掉（同「其余步骤靠 psql -v ON_ERROR_STOP=1 保证失败即停」的一贯
  # 纪律）。
  docker exec -i "${DRILL_DB_CONTAINER}" pg_restore --no-owner --no-privileges -n public --exit-on-error -v \
    -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" \
    < "${DUMP_PATH}" > "${RESTORE_LOG}" 2>&1
  log "恢复完成（日志见 ${RESTORE_LOG}，$(wc -l < "${RESTORE_LOG}") 行输出；--exit-on-error 保证非零退出即代表有错误，这里退出码是 0）"

  # 属主与授权修复（S-1-3 本机跑通一次时实测踩到，登记在此）：`--no-owner
  # --no-privileges` 让 dump 里全部对象的属主都变成执行 pg_restore 的连接角色
  # （这里是 DRILL_DB_USER），且不带任何 GRANT 语句。这对绝大多数对象只影响
  # "whose row shows up in \dt"，但迁移 0054 建立的 `lingxi_retention_cleanup`
  # 是唯一一个 SECURITY DEFINER 函数（全仓只此一处，已核对其余四条保留职责
  # 均为应用层读写、不走这个机制）：它必须属主是无登录角色
  # `lingxi_retention_owner`，因为 `galaxy_import_batch`/`feishu_org_sync_run`
  # 上的 `BEFORE DELETE` 触发器 `lingxi_reject_premature_delete()` 专门只放行
  # `current_user = 'lingxi_retention_owner'` 的删除、拒绝其它一切角色（含
  # 超级用户）直接删；而 `lingxi_retention_owner` 本身要能读写这两张表，还需要
  # 迁移 0054 原本随属主一起下发、但被 `--no-privileges` 一并跳过的
  # SELECT/DELETE 授权。这不是本次改动引入的缺陷，是 `--no-owner
  # --no-privileges`（E-3 ③ 明确要求）与 0054 的属主强绑定安全设计天然冲突，
  # 只能在恢复后针对这一个函数与两张表显式补上。DRILL_DB_USER 是隔离实例的
  # 引导超级用户，`ALTER FUNCTION ... OWNER TO` 的两道成员关系检查对超级用户
  # 整体跳过（迁移 0054 注释原文），因此这里不需要先 GRANT 角色再 ALTER 属主
  # 的那套生产迁移专用降权流程，直接改属主、直接授权即可。
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    DO \$\$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.proname = 'lingxi_retention_cleanup'
      ) THEN
        ALTER FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) OWNER TO lingxi_retention_owner;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'galaxy_import_batch') THEN
        GRANT SELECT, DELETE ON public.galaxy_import_batch TO lingxi_retention_owner;
      END IF;
      IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'feishu_org_sync_run') THEN
        GRANT SELECT, DELETE ON public.feishu_org_sync_run TO lingxi_retention_owner;
      END IF;
      GRANT USAGE ON SCHEMA public TO lingxi_retention_owner;
    END
    \$\$;
  " >/dev/null
  log "属主与授权修复完成：lingxi_retention_cleanup（若存在）已改回属主 lingxi_retention_owner 并补回其读写两张受限表所需的 GRANT"
}

step_migrate() {
  log "== 迁移：恢复出的实例前滚到链头（alembic upgrade head，经 migrate 镜像）=="
  # migrate 镜像 ENTRYPOINT 已固定为 `python -m alembic -c /opt/lingxi/alembic.ini`
  # （见 Dockerfile），这里显式带 `upgrade head` 只为可读，不依赖镜像默认 CMD。
  # 若源库在演练当下已经处于链头（本批合同顺序：S-2-4 先把预发迁移到链头，
  # S-2-5 才跑本演练），这一步是空操作、退出码仍是 0，属预期，不是脚本没起作用。
  docker run --rm --network "${DRILL_NETWORK}" -e "LINGXI_MIGRATION_DSN=${DRILL_DSN}" \
    "${DRILL_MIGRATE_IMAGE}" upgrade head
  log "迁移完成：已到链头"
}

# 行数回读用的 SQL：对 public 下每张基表跑一次精确 count(*)（不是
# pg_stat_user_tables 的估算值——刚恢复完、还没 ANALYZE 时那个估算可能是 0 或
# 陈旧值，达不到"逐表核对"的确定性）。用 query_to_xml + xpath 在纯 SQL 里做
# 动态计数，不需要 PL/pgSQL 循环。heredoc 引号定界符 'SQL' 让整段原样传递，
# 内部单引号不需要任何转义。
row_counts_sql() {
  cat <<'SQL'
SELECT table_name || '|' ||
       (xpath('/row/cnt/text()',
              query_to_xml(format('SELECT count(*) AS cnt FROM public.%I', table_name), false, true, '')
       ))[1]::text
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
SQL
}

step_verify_row_counts() {
  log "== 行数回读：恢复且迁移之后（清理前）与源库逐表一致 =="
  local source_counts target_counts
  if ! source_counts=$(run_source_tool psql -A -t -F '|' -c "$(row_counts_sql)" 2>&1); then
    printf '%s\n' "${source_counts}" | mask >&2
    echo "源库行数回读失败（已脱敏，见上）" >&2
    exit 1
  fi
  target_counts=$(docker exec "${DRILL_DB_CONTAINER}" psql -A -t -F '|' -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" \
    -c "$(row_counts_sql)")
  if [[ "${source_counts}" != "${target_counts}" ]]; then
    echo "核对失败：恢复并迁移到链头之后的逐表行数与源库不一致。两种已知原因，先按下面排查，" >&2
    echo "不要不看原因就重跑：" >&2
    echo "① 源库在演练窗口内有正常业务写入——pg_dump 与本次行数回读是两次独立连接，不共享同一" >&2
    echo "   快照，源库若持续写入，逐表计数会随之出现差异；这不代表恢复出了问题，代表源库不是" >&2
    echo "   静止的（见脚本头「破坏半径」「前提」两处登记）。" >&2
    echo "② 源库在演练时落后链头——本批合同顺序里 S-2-4 先迁移预发到链头、S-2-5 才跑本演练，" >&2
    echo "   但若源库确实落后，迁移会新增源库没有的表，这里如实报告差异，不做静默容错。" >&2
    echo "--- 源库 ---" >&2
    printf '%s\n' "${source_counts}" >&2
    echo "--- 恢复并迁移后 ---" >&2
    printf '%s\n' "${target_counts}" >&2
    exit 1
  fi
  log "行数回读通过：$(printf '%s\n' "${target_counts}" | grep -c .) 张表逐表一致"
}

# 真实（非本脚本合成）行的主键快照：按「安全边际未到期」（expires_at 比清理
# 那一刻还有 1 小时以上余量，这一轮清理不该碰）与「已到期」（清理这一轮理应
# 删掉，这正是 V-投递-07 的本意——到期的低敏事实按原始写入时间回收，不因为
# 它是恢复出来的真实数据就网开一面）两类分别核对，不再只看总数（外审 codex
# gpt-5.6-sol 2026-09-11 指出的真实缺口：旧版把全部非合成行当「真实且未
# 到期」，源库里本就到期的真实行被正常清掉会被误判为故障；且只核对计数，
# 删错一行、多留一行也可能净数不变而被放过）。1 小时余量避免把"采样时还有
# 富余、但恰好在演练这几秒内跨过到期线"的边界行错判进"必须保留"集合——这类
# 边界行本就该由下一轮清理处理，不属于本次演练断言的范围；处于安全边际与
# 已到期之间的灰区同理不作断言。只覆盖被 `lingxi_retention_cleanup` 处理的
# 两张表（galaxy_import_batch / feishu_org_sync_run）。
real_ids() {
  # $1 = 表名，$2 = 本脚本合成数据的 id 前缀（LIKE 模式，如 'gib_drill_%'），
  # $3 = safe（安全边际未到期）｜expired（已到期）
  local cmp
  if [[ "$3" == "safe" ]]; then
    cmp="expires_at > now() + interval '1 hour'"
  else
    cmp="expires_at <= now()"
  fi
  docker exec "${DRILL_DB_CONTAINER}" psql -At -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -c \
    "SELECT id FROM ${1} WHERE id NOT LIKE '${2}' AND ${cmp} ORDER BY id;"
}

# 把换行分隔的 ID 列表转成安全的 SQL IN (...) 值列表：单引号按 SQL 字面量规则
# 加倍转义，不依赖 ID 内容不含特殊字符的假设。
sql_in_list() {
  local id out=""
  while IFS= read -r id; do
    [[ -z "${id}" ]] && continue
    out+="'${id//\'/\'\'}',"
  done <<<"$1"
  printf '%s' "${out%,}"
}

step_inject_synthetic() {
  log "== 注入合成样本（只写隔离实例，不碰源库）=="
  # 到期时间由不可后移触发器从 started_at 派生，这里传的 expires_at 会被覆盖，
  # 与调用方能不能自己指定到期时间无关（V-保留-07 同一条不变式）。
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at, completed_at)
    VALUES ('gib_drill_synthetic_expired', 'drill-synthetic-export', 'digest-drill-synthetic-expired',
            'complete', now() - interval '100 days', now() - interval '100 days');
    INSERT INTO galaxy_import_batch (id, source_label, source_digest, status, started_at, completed_at)
    VALUES ('gib_drill_synthetic_fresh', 'drill-synthetic-export-fresh', 'digest-drill-synthetic-fresh',
            'complete', now() - interval '5 days', now() - interval '5 days');
  " >/dev/null
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -v ON_ERROR_STOP=1 -c "
    BEGIN;
    INSERT INTO feishu_org_sync_run (id, source_app_id, status, started_at, completed_at, expires_at, tenant_count, department_count, member_count)
    VALUES ('orgsync_drill_expired', 'cli_fake', 'complete', now() - interval '100 days', now() - interval '100 days', now() - interval '100 days', 1, 1, 1);
    INSERT INTO feishu_org_tenant_snapshot (id, sync_run_id, tenant_key, visible_to_user_identity, member_count)
    VALUES ('orgsync_drill_expired_tenant', 'orgsync_drill_expired', 'tenant_drill', true, 1);
    INSERT INTO feishu_org_department_snapshot (id, sync_run_id, tenant_key, department_key, name)
    VALUES ('orgsync_drill_expired_dept', 'orgsync_drill_expired', 'tenant_drill', 'dept_drill', '演练部门');
    INSERT INTO feishu_org_member_snapshot (id, sync_run_id, tenant_key, member_key, open_id, user_id, union_id, display_name)
    VALUES ('orgsync_drill_expired_member', 'orgsync_drill_expired', 'tenant_drill', 'm1', 'ou_drill_placeholder', 'user_drill_placeholder', 'union_drill_placeholder', '演练化名');
    COMMIT;
  " >/dev/null
  log "合成样本已写入：galaxy_import_batch × 2（过期/未过期各一），feishu_org_sync_run × 1（过期）"

  log "-- 清理前快照 --"
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" \
    -c "SELECT id, started_at, expires_at, (expires_at <= now()) AS is_expired FROM galaxy_import_batch ORDER BY started_at;" \
    -c "SELECT id, started_at, expires_at, (expires_at <= now()) AS is_expired FROM feishu_org_sync_run ORDER BY started_at;"

  # 真实（不含本步注入的合成行）主键快照，按「安全边际未到期」与「已到期」
  # 两类分别记录，供清理后核对（见 real_ids() 函数文档；不是硬编码固定数字或
  # 只看总数——源库会持续写入新的组织快照，写成常量或只比总数会让脚本在下一
  # 次真实运行时假红或假绿）。四个变量是脚本全局状态，供 step_verify 使用。
  REAL_GALAXY_SAFE_BEFORE=$(real_ids galaxy_import_batch 'gib_drill_%' safe)
  REAL_SYNC_SAFE_BEFORE=$(real_ids feishu_org_sync_run 'orgsync_drill_%' safe)
  REAL_GALAXY_EXPIRED_BEFORE=$(real_ids galaxy_import_batch 'gib_drill_%' expired)
  REAL_SYNC_EXPIRED_BEFORE=$(real_ids feishu_org_sync_run 'orgsync_drill_%' expired)
  log "真实主键快照：galaxy_import_batch 安全边际未到期 $(printf '%s\n' "${REAL_GALAXY_SAFE_BEFORE}" | grep -c .) 行／已到期 $(printf '%s\n' "${REAL_GALAXY_EXPIRED_BEFORE}" | grep -c .) 行；feishu_org_sync_run 安全边际未到期 $(printf '%s\n' "${REAL_SYNC_SAFE_BEFORE}" | grep -c .) 行／已到期 $(printf '%s\n' "${REAL_SYNC_EXPIRED_BEFORE}" | grep -c .) 行（已到期这组预期清理后消失）"
}

step_run_real_cleanup() {
  log "== 用真实清理代码路径补跑一次保留清理（scheduler 镜像，进程内跑 scheduler 已装配的全部保留职责，非手写 SQL）=="
  # DRILL_DSN（含隔离实例的现场随机密码）改用 `-e` 环境变量传入容器，不拼进
  # `-c` 后面的 Python 源码文本（独立审查 P2-11 的登记延续）：后者会让密码原样
  # 出现在容器自身进程的 argv 里；改成环境变量后，读取面收紧到需要读该进程
  # `/proc/<pid>/environ`（同用户或 root）的账户，与本文件头部"密码只作为该
  # 容器的环境变量存在，容器销毁即失效，不落盘、不打印"的既有边界一致。
  #
  # 直接调用 scheduler 自己的装配函数 `_build_cleanup_duties`：哪天 scheduler
  # 新增下一项清理职责，这里自动跟上，不需要在演练脚本里另外维护一份职责
  # 清单（此前版本只手写调用了 PostgresRetentionCleaner 一项，见 S-1-3 改动
  # 说明）。SchedulerConfig 除 postgres_dsn 外的必填字段全部填占位值——
  # `_build_cleanup_duties` 不读它们，构造这个 dataclass 只是满足其必填字段
  # 校验，不代表这次演练用到任何外部凭据；mcp_token_encrypt_key 留空（默认
  # None）会让权限链清理里 mcp_sync_check 那一面按既有设计优雅跳过（记一条
  # 审计后继续），这正是"不带任何外部凭据"要求下的预期形态，不是缺陷。
  # 结果判定不只打印 summary（外审 codex gpt-5.6-sol 2026-09-11 指出的真实
  # 缺口：此前 run_once() 的返回值只打印，锁等待让路、权限链某一面没装配这类
  # "看起来跑完但没做完"的状态不会让脚本非零退出）。先读各职责报告类型的
  # 结构化字段（见 src/lingxi/apps/scheduler/retention.py 与
  # adapters/retention.py 的 dataclass 定义），逐项按字段判定，不猜字段名：
  #   - report is None：本轮未执行——本次演练用全新 threading.Event() 且从不
  #     置位，正常路径不可能发生，出现即判失败。
  #   - RetentionReport.blocked_tables 非空：保留清理有表因锁等待超时整批
  #     让路，这一轮没做完，判失败（不是"删了 0 行"那种正常空转）。
  #   - PermissionRetentionReport.checks_wired：唯一允许的"因缺凭据预期跳过"
  #     白名单，且只白名单这一项——本次演练确定性不传
  #     LINGXI_MCP_TOKEN_ENCRYPT_KEY（E-3 ⑤"不带任何外部凭据"），
  #     mcp_sync_check 那一面因此确定性地不装配，checks_wired 必为 False；
  #     不是 False 反而是异常（说明这次调用方式下密钥不知怎么被配置上了）。
  #   - 其余三类（IdleConversationSweepDuty/ContentCaptureRetentionDuty 返回
  #     裸 int；ExpiredCarrierRetentionDuty 内部四面互相独立重试，任一面失败
  #     已经在职责自己的 run_once() 里 raise，不会安静返回）没有额外结构化
  #     字段要看——它们的失败路径已经是 Python 异常，被本脚本 `set -e` 接住，
  #     不需要在这里另外判定。
  docker run --rm --network "${DRILL_NETWORK}" -e "LINGXI_DRILL_DSN=${DRILL_DSN}" \
    --entrypoint python "${DRILL_SCHEDULER_IMAGE}" -c "
import os
import sys
import threading

from lingxi.apps.scheduler.assembly import _build_cleanup_duties
from lingxi.apps.scheduler.audit import StructuredLogAuditSink
from lingxi.apps.scheduler.config import SchedulerConfig

config = SchedulerConfig(
    postgres_dsn=os.environ['LINGXI_DRILL_DSN'],
    credential_key='drill-unused',
    credential_path='/nonexistent/drill-unused',
    feishu_app_id='drill-unused',
    feishu_app_secret='drill-unused',
    feishu_base_url='https://open.feishu.cn/open-apis',
    interval_seconds=60,
)
duties = _build_cleanup_duties(config, threading.Event(), StructuredLogAuditSink())
print(f'已装配 {len(duties)} 项保留职责：' + '、'.join(duty.name for duty in duties))

failures = []
for duty in duties:
    report = duty.run_once()
    summary = getattr(report, 'summary', None)
    print(f'{duty.name}：{summary() if callable(summary) else report}')
    if report is None:
        failures.append(f'{duty.name}：本轮未执行（report is None），但本次演练从未请求停止，这是异常状态')
        continue
    blocked = getattr(report, 'blocked_tables', None)
    if blocked:
        failures.append(f'{duty.name}：以下表因锁等待超时整批让路，本轮未做完：' + '、'.join(blocked))
    if hasattr(report, 'checks_wired') and report.checks_wired is not False:
        failures.append(f'{duty.name}：checks_wired={report.checks_wired!r}，与本次演练确定性不配置 MCP 主密钥的预期（应为 False）不符')

if failures:
    print('保留职责结果核对失败：')
    for line in failures:
        print(f'  - {line}')
    sys.exit(1)
print(f'{len(duties)} 项保留职责结果核对通过：无阻塞表、无非白名单跳过。')
"
}

step_verify() {
  log "== 核对：过期合成行已清，未过期行（含真实恢复行）逐行保留 =="
  docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" \
    -c "SELECT id, started_at, expires_at FROM galaxy_import_batch ORDER BY started_at;" \
    -c "SELECT id, started_at, expires_at FROM feishu_org_sync_run ORDER BY started_at;"

  # 孤儿行核对（独立审查 P2-13 的登记延续）：此前这两条只用 `-c` 打印计数，人不
  # 盯着看就会漏掉——计数非零并不会让脚本非零退出。改成取值断言：清理逻辑如果
  # 破坏了引用完整性（子表还在、父行已被删），这里必须让脚本失败退出,而不是
  # 只在输出里留一行容易被忽略的数字。
  local orphan_galaxy_children
  orphan_galaxy_children=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -Atc \
    "SELECT count(*) FROM galaxy_user WHERE batch_id NOT IN (SELECT id FROM galaxy_import_batch);")
  if [[ "${orphan_galaxy_children}" != "0" ]]; then
    echo "核对失败：galaxy_user 出现 ${orphan_galaxy_children} 行孤儿数据（batch_id 指向已不存在的 galaxy_import_batch），清理逻辑破坏了引用完整性" >&2
    exit 1
  fi
  local orphan_org_children
  orphan_org_children=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -Atc \
    "SELECT count(*) FROM feishu_org_member_snapshot WHERE sync_run_id NOT IN (SELECT id FROM feishu_org_sync_run);")
  if [[ "${orphan_org_children}" != "0" ]]; then
    echo "核对失败：feishu_org_member_snapshot 出现 ${orphan_org_children} 行孤儿数据（sync_run_id 指向已不存在的 feishu_org_sync_run），清理逻辑破坏了引用完整性" >&2
    exit 1
  fi

  local still_present
  still_present=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -Atc \
    "SELECT count(*) FROM galaxy_import_batch WHERE id = 'gib_drill_synthetic_expired'
       UNION ALL
     SELECT count(*) FROM feishu_org_sync_run WHERE id = 'orgsync_drill_expired'")
  if printf '%s' "${still_present}" | grep -qv '^0$'; then
    echo "核对失败：过期合成行未被清理，见上面的查询输出" >&2
    exit 1
  fi
  local missing
  missing=$(docker exec "${DRILL_DB_CONTAINER}" psql -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -Atc \
    "SELECT count(*) FROM galaxy_import_batch WHERE id = 'gib_drill_synthetic_fresh'")
  if [[ "${missing}" != "1" ]]; then
    echo "核对失败：未过期合成行不应被清理，但它不见了" >&2
    exit 1
  fi

  # 真实行按主键集合核对，不只看总数（见 step_inject_synthetic 里 real_ids()
  # 的登记：只比总数会让"删错一行、多留一行、净数不变"这类问题被放过）。
  #
  # 安全边际集合的清理后核对**不重算时间谓词**，改成与下面「已到期」核对
  # 同型的按主键存在性查询（外审复核 2026-09-11 指出的真实缺口）：若沿用
  # `real_ids ... safe`（`expires_at > now() + interval '1 hour'`），清理后
  # `now()` 已经推进到之后的时刻，阈值本身跟着后移——落在
  # `(清理前 now()+1h, 清理后 now()+1h]` 这个窗口内、根本没被清理动过的真实行
  # 会从重算出的"安全边际"集合里静默消失，被误判成"数据变化"而判故障。
  # 直接按 `step_inject_synthetic` 里已经固定下来的那批主键查存在性，不重新
  # 用任何时间点判断"现在算不算安全边际"，比对结果与耗时、与两次采样之间
  # `now()` 走了多远都无关。
  local real_galaxy_safe_after real_sync_safe_after
  if [[ -n "${REAL_GALAXY_SAFE_BEFORE}" ]]; then
    real_galaxy_safe_after=$(docker exec "${DRILL_DB_CONTAINER}" psql -At -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -c \
      "SELECT id FROM galaxy_import_batch WHERE id IN ($(sql_in_list "${REAL_GALAXY_SAFE_BEFORE}")) ORDER BY id;")
    if [[ "${real_galaxy_safe_after}" != "${REAL_GALAXY_SAFE_BEFORE}" ]]; then
      echo "核对失败：galaxy_import_batch 以下真实且未到期（安全边际）主键理应逐行保留，但集合发生变化：" >&2
      echo "--- 清理前 ---" >&2
      printf '%s\n' "${REAL_GALAXY_SAFE_BEFORE}" >&2
      echo "--- 清理后仍存在的 ---" >&2
      printf '%s\n' "${real_galaxy_safe_after}" >&2
      exit 1
    fi
  fi
  if [[ -n "${REAL_SYNC_SAFE_BEFORE}" ]]; then
    real_sync_safe_after=$(docker exec "${DRILL_DB_CONTAINER}" psql -At -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -c \
      "SELECT id FROM feishu_org_sync_run WHERE id IN ($(sql_in_list "${REAL_SYNC_SAFE_BEFORE}")) ORDER BY id;")
    if [[ "${real_sync_safe_after}" != "${REAL_SYNC_SAFE_BEFORE}" ]]; then
      echo "核对失败：feishu_org_sync_run 以下真实且未到期（安全边际）主键理应逐行保留，但集合发生变化：" >&2
      echo "--- 清理前 ---" >&2
      printf '%s\n' "${REAL_SYNC_SAFE_BEFORE}" >&2
      echo "--- 清理后仍存在的 ---" >&2
      printf '%s\n' "${real_sync_safe_after}" >&2
      exit 1
    fi
  fi

  # 真实已到期行理应被清理函数删掉——这正是 V-投递-07 的本意：到期的低敏事实
  # 按原始写入时间回收，不因为它是恢复出来的真实数据就网开一面。逐主键核对
  # 存在性，两张表分别处理；这组快照在 step_inject_synthetic 里为空是常态
  # （生产/预发上保留清理正常按分钟跑，源库里通常不会积压真实已到期行），
  # 非空时才有断言意义。
  local remaining
  if [[ -n "${REAL_GALAXY_EXPIRED_BEFORE}" ]]; then
    remaining=$(docker exec "${DRILL_DB_CONTAINER}" psql -At -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -c \
      "SELECT id FROM galaxy_import_batch WHERE id IN ($(sql_in_list "${REAL_GALAXY_EXPIRED_BEFORE}")) ORDER BY id;")
    if [[ -n "${remaining}" ]]; then
      echo "核对失败：galaxy_import_batch 以下真实已到期主键理应被本轮清理，但仍存在：" >&2
      printf '%s\n' "${remaining}" >&2
      exit 1
    fi
  fi
  if [[ -n "${REAL_SYNC_EXPIRED_BEFORE}" ]]; then
    remaining=$(docker exec "${DRILL_DB_CONTAINER}" psql -At -U "${DRILL_DB_USER}" -d "${DRILL_DB_NAME}" -c \
      "SELECT id FROM feishu_org_sync_run WHERE id IN ($(sql_in_list "${REAL_SYNC_EXPIRED_BEFORE}")) ORDER BY id;")
    if [[ -n "${remaining}" ]]; then
      echo "核对失败：feishu_org_sync_run 以下真实已到期主键理应被本轮清理，但仍存在：" >&2
      printf '%s\n' "${remaining}" >&2
      exit 1
    fi
  fi
  log "核对通过：过期合成行已清除；真实未到期（安全边际）主键集合逐行不变；真实已到期主键（若有）已被清理。"
}

step_destroy() {
  log "== 销毁隔离实例与临时产物 =="
  docker rm -f -v "${DRILL_DB_CONTAINER}" >/dev/null
  docker network rm "${DRILL_NETWORK}" >/dev/null
  rm -f "${DUMP_PATH}" "${RESTORE_LOG}"
  log "已删除容器 ${DRILL_DB_CONTAINER}（含其匿名数据卷 ${DRILL_VOLUME}）、网络 ${DRILL_NETWORK}、dump 与恢复日志临时文件"
  log "-- 残留盘点 --"
  docker ps -a --filter "name=${DRILL_DB_CONTAINER}"
  docker network ls --filter "name=${DRILL_NETWORK}"
  if [[ -n "${DRILL_VOLUME}" ]] && docker volume inspect "${DRILL_VOLUME}" >/dev/null 2>&1; then
    echo "残留盘点失败：匿名数据卷 ${DRILL_VOLUME} 仍然存在" >&2
    exit 1
  fi
  log "匿名数据卷 ${DRILL_VOLUME} 已确认不存在"
  df -h /
}

step_preflight
step_precheck_source
step_backup
step_isolate_and_restore
step_migrate
step_verify_row_counts
if [[ "${INJECT_SYNTHETIC}" == "1" ]]; then
  step_inject_synthetic
  step_run_real_cleanup
  step_verify
fi
step_destroy
log "演练完成。"
