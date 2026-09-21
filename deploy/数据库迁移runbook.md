# 数据库迁移 runbook：Supabase → 生产主机本地 PostgreSQL 17（2.6.0，Issue #809）

> 本文是停写窗口的操作剧本，配套切换脚本 `s30_p2_db_switch.sh`（**不入仓库**，全文与 sha256 贴在 Issue #809；预发形与生产形脚本本体逐字相同，差异只在同目录 `s30.env`）。本地库容器、备份单元与巡检检查项的安装见 [`监控告警.md`「十、本地数据库」](监控告警.md#十本地数据库备份单元与检查项issue-809260-迁库版)；本文不重复。

## 一、前提与已裁定

- **D-4**：动机 = 停付 Supabase 订阅 + 数据自主；本地 PostgreSQL 大版本 17（不借迁库升级）。**D-5**：RPO 24 小时、RTO 4 小时（工作时段）、异机副本落 `biai-stage`、不做 PITR。**D-6**：切换 = 30 分钟停写 + 观察 7 天；观察期 Supabase 只停写不退役、可回切；迁移方式 = 停写窗口内 `pg_dump` / `pg_restore`（27 MB 以分钟计）。
- 进入生产切换前必须齐：rc.C 验收记录 PR 已合；预发按本文完整演练四项全过（切换、备份、副本恢复、回退切回）；生产余盘 ≥ 10 GB；Supabase 付费档自动备份最近一份的时间已实读；脚本 sha 两端相等；来源库参数与 locale 现值已由产品负责人只读回读（Issue #859 评论 5753938437）并钉进 compose 缺省与 `s30.env`（idle 两项 0 / 0、`statement_timeout` 120 s、`TimeZone` UTC、ICU locale `en-US`），`install-pg` 按 `preflight` 实读自动派生 initdb 参数、不手填。
- **属主与权限必须原样保留**：迁移 `0054` 的九十天清理函数是 `SECURITY DEFINER`、属主无登录角色 `lingxi_retention_owner`，内容表的删除触发器按 `current_user` 放行。`pg_restore --no-owner` 会把属主改成 `postgres`，迁完后每一轮保留清理都被触发器拒绝——因此脚本先按来源事实预建占位角色，`restore` 不带 `--no-owner` / `--no-privileges`，`verify` 逐对象比属主与 ACL。

## 二、角色与通路

| 环境 | 谁跑 | 通路 |
| --- | --- | --- |
| 预发 | 编排者 | 脚本投放到白名单路径 `/tmp/lingxi-s26/`，按剧本逐条跑；被分类器拦下即回退为「幂等脚本 + 逐步回读」交产品负责人 |
| 生产 | 产品负责人 | 在守望者会话用 `!` 亲跑，**一次一条子命令**，每条先看编排者给的逐字预期与停止条件；编排者只读回读 |

脚本对 Supabase 永远只读（只 `pg_dump` 与只读 SQL）；生产写动作只有本地库容器、八份 env 文件与两个 timer。

## 三、脚本投放与 sha 核对

1. 编排者把 `s30_p2_db_switch.sh`、`s30.env`（按环境的 example 填好）、`compose.db.yaml`（仓库 `deploy/compose.db.yaml` 原样副本）三件放到同一目录（root 0700）。
2. 两端核对：`sha256sum s30_p2_db_switch.sh compose.db.yaml` 与 Issue #809 贴出的值逐字相等；`s30.env` 里 `LINGXI_S30_COMPOSE_SHA` = 该 compose 副本的 sha。
3. `bash s30_p2_db_switch.sh` 不带子命令即打印用法；`status` 只读，随时可跑。

## 四、停写窗口剧本

预算 30 分钟（D-6）。前两步在窗口外先做，第 3 步开始记时。

| # | 命令 | 预期输出（关键行） | 失败停止点 | 可逆 / 回退 |
| --- | --- | --- | --- | --- |
| 1 | `preflight` | `preflight 通过 → …/preflight.json`；打印来源版本、locale、对象计数、角色占位清单、idle 现值 | 任一「前置不满足」→ 不进下一步 | 只读 |
| 2 | `install-pg` | `[容器] lingxi-db healthy`、`[探针] 空转 + 读写各一次通过`、`[前置] extensions schema + pg_trgm`、占位角色新建 N、`public 处置=toc` | 120 秒未 healthy → `docker logs lingxi-db` | 可逆：`docker compose … down`（不带 `-v`，数据目录保留） |
| 3 | `stop-write` | 拉取 timer 已停、三容器已停（60 / 90 / 150 s）、采样 timer 已停、`stop_write_at=…` | — | 可逆：`start-services` + `start-timers` |
| 4 | `dump` | `dump 完成：N B，sha=…`；来源快照 `source-facts.json` / `source.dump.counts.json` 已写 | pg_dump 失败 → `rollback-dsn` 不需要，直接 `start-services` + `start-timers` 恢复旧服务 | 只读 |
| 5 | `restore` | `[toc] 已去掉 CREATE SCHEMA public …`、`restore 完成：public 对象 N、表 N、alembic 0098_…` | 校验和不符 / 目标非空 / pg_restore 报错 → 停，现场保留（`restore.log`） | 只写本地库 |
| 6 | `verify` | `verify 零差异：…` 且写出 `verify.json`（`ok:true`） | 任一差异行（段\|键\|来源\|目标）→ **不切换**：`start-services` + `start-timers`，登记差异 | 只读 |
| 7 | `switch-dsn` | 八份文件各 `changed(N 行 DSN)` / `no_dsn_line`；`回读：旧主机段 … 0 命中` | 回读残留 → `rollback-dsn` | 可逆：`rollback-dsn` |
| 8 | 产品负责人点 **Release Promotion v2.6.0** | 发布列表出现正式版 | — | 回滚单独请示 |
| 9 | `start-timers` | 两个 timer `active` | — | `systemctl stop` |
| 10 | 等拉取代理部署（≤ 5 分钟一轮 + 部署） | 代理日志 `verified` | 部署失败 → 回滚单独请示（见六） | — |
| 11 | `postcheck` | 三容器 healthy；`[scheduler 容器视角] server_addr <本地库容器地址> alembic 0098_… cleanup_owner lingxi_retention_owner True`；`停写窗口 … = N 分 N 秒` | 连的不是本地库 / 属主不符 → 回滚单独请示 | 只读 |
| 12 | 产品负责人 Bot-Prod 一次真实问数 | 任务 `succeeded` | 失败 → 回滚单独请示 | — |

停写窗口 = 第 3 步 `stop_write_at` 到第 11 步 `postcheck_at`，脚本自己打印；实测必须 ≤ 30 分钟（E-6）。步骤 3–7 在假根目录实测合计 11 秒（27 MB 量级，不含 Promotion 与部署）。

## 五、`verify` 零差异的判据与预期差异

- **判红项**（任一不等即退出非 0、打印逐项差异）：`encoding` / `datcollate` / `datctype` / `datlocprovider` / `datlocale` 五项；角色级 `postgres` 的 `search_path`（来源 `"$user", public, extensions`，`install-pg` 镜像同一条）；`alembic_version`；`public` 对象计数（表 / 索引 / 序列 / 视图 / 函数 / 非内部触发器）；逐表精确行数；序列 `last_value`；逐表触发器名；`pg_trgm` 版本与 schema（`extensions`）；`lingxi_retention_cleanup` 属主 = `lingxi_retention_owner` 且 `prosecdef = true`；`public` 下全部表 / 序列 / 视图 / 函数与 schema 本身的属主逐个相等；同一集合的 ACL 逐对象相等（grantee + 权限位）。
- **预期差异（只列不判红）**：ACL 条目的 grantor（`grantee=权限位/grantor` 里 `/` 后那一段）——比对只按 grantee + 权限位归一，含 grantor 的原文两侧都记进 `verify.json` 的 `acls_raw` 供人工核对（实测 `pg_restore` 以 `SET SESSION AUTHORIZATION` 重放非属主授权、grantor 原样保留，归一只为托管平台侧可能的差异兜底）；`datcollversion`（来源 153.121，本地 `postgres:17` 实测 153.128——同 ICU 73 系，恢复后索引在本地重建，脚本只把两值记进 `verify.json`）；来源的平台扩展（`supabase_vault` / `pg_stat_statements` / `pgcrypto` / `uuid-ossp` 等）不在目标——应用只依赖 `pg_trgm`；来源的平台角色（`supabase_*` / `authenticator` 等）不比对，只比 `public` 对象引用到的角色，它们已在 `install-pg` 按来源清单占位（`NOLOGIN`、无成员、无属性）。
- 比对基准是 `dump` 时（已停写）写下的来源快照，不是 `preflight`，也不是任何人工抄写的常量：迁移链头在研发机实得 `public` 表 46，与早先只读盘点表上的 85 不一致，`verify` 只认实时读数。

## 六、回退

- **切换前**（第 3–7 步内失败）：`rollback-dsn`（若 `switch-dsn` 已改过文件；逐字节还原并回读）→ `start-services`（三个旧容器沿用创建时的旧 DSN，等于回到 Supabase）→ `start-timers`。本地库容器与数据目录保留供取证，不删。
- **切换后**（Promotion / 部署 / 问数失败）：**单独请示**（合同 §2）。回切 = `rollback-dsn` 还原八份文件 → 拉取代理按 `recover` 独立批准或重新走正常发布把三容器重建为旧 DSN；**切换后新增写入的回退处置** = 先在本地库 `pg_dump -Fc` 导出（或直接用当日 `lingxi-db-backup` 产物），待人工回放到 Supabase——旧只读源不含这段写入，不能假设改回连接串即无损。
- 观察期内任一天出现数据差异或告警：先 `status` 与备份状态文件取证，再按上一条请示。

## 七、观察期退出条件与退役

7 天（W6）每日一行留痕：`lingxi-db-backup.timer` 成功 + 副本到达 `biai-stage`（sha 相等）+ 宿主巡检本地库检查项零告警 + 零数据差异；第 3 天从当天副本在预发恢复到独立实例一次（`backup_restore_drill.sh` 本地库形）零差异；第 1 天核对 scheduler 的保留清理一次成功（`lingxi_retention_cleanup` 属主链在生产成立）。达标后产品负责人裁定；Supabase 只停写不退役，退订由产品负责人在控制台执行（D-10.12），Trace 只到「可退」。

## 八、已知边界

- 应用沿用来源角色名 `postgres` 连库，在本地实例上它是超级用户；收紧为专用低权角色另立工作项。
- 容器网络无 TLS：`switch-dsn` 把连接串里的 `sslmode=require` 改为 `sslmode=disable`，其余参数原样保留。
- 数据库与三个应用容器同机同盘：共同故障域，靠每日备份 + 异机副本兜底（D-5）。
- 应用栈的 `docker compose down` 会因本地库容器仍挂在 `lingxi_default` 网络上而删网失败（部署器从不 `down`，只 `stop` + `up -d`，不受影响）；本地库自己的 `compose down` 不删外部网络、可正常执行，日常停库只 `docker stop lingxi-db`，升级镜像走「改摘要 → `up -d`」。
- 占位角色只为保住属主与 ACL 结构：来源平台角色若在 `public` 对象的 ACL 里出现（`anon` / `authenticated` / `service_role`），目标上会以同名 `NOLOGIN` 空角色存在，无任何成员与属性；平台角色自己的设置（`statement_timeout` / `app.settings.*` 等）不照搬。
- `wal_level` 本地取 `replica`（来源 `logical` 是托管方 realtime 所需）：可复核断言 = `replication slot|pg_logical|logical decoding|pgoutput|wal2json|CREATE PUBLICATION|CREATE SUBSCRIPTION|pg_create_logical` 在 `src/` `migrations/` `scripts/` `deploy/` 命中 0，`LISTEN` / `NOTIFY` 在任何 `wal_level` 都工作；D-5 不做逻辑复制。
- 角色级 `search_path`：来源给 `postgres` 设了 `"$user", public, extensions`，应用与迁移零处裸引用 `extensions` 下的对象（0098 DDL 用限定名），功能上不依赖；为「与迁移前一致」零差异且未来迁移行为相同，`install-pg` 镜像同一条 `ALTER ROLE postgres SET search_path`，`verify` 比对。
- `public` 处置用 TOC 过滤（跳过 dump 里的 `CREATE SCHEMA public` 条目、保留其 ACL 条目；来源属主不同时补一句 `ALTER SCHEMA public OWNER TO`）：先删再由 dump 重建的做法实测会丢掉 initdb 给 `public` 的缺省 ACL（PUBLIC 的 USAGE），`verify` 判红。
- `dump` 与 `verify` 的行数快照来自停写后的两次只读连接；停写窗口外跑 `verify` 必然有差异，不是故障。
