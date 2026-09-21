# 数据库迁移 runbook：Supabase → 生产主机本地 PostgreSQL 17（2.6.0，Issue #809）

> 本文是停写窗口的操作剧本，配套切换脚本 `s30_p2_db_switch.sh`（**不入仓库**，全文与 sha256 贴在 Issue #809；预发形与生产形脚本本体逐字相同，差异只在同目录 `s30.env`）。本地库容器、备份单元与巡检检查项的安装见 [`监控告警.md`「十、本地数据库」](监控告警.md#十本地数据库备份单元与检查项issue-809260-迁库版)；本文不重复。

## 一、前提与已裁定

- **D-4**：动机 = 停付 Supabase 订阅 + 数据自主；本地 PostgreSQL 大版本 17（不借迁库升级）。**D-5**：RPO 24 小时、RTO 4 小时（工作时段）、异机副本落 `biai-stage`、不做 PITR。**D-6**：切换 = 30 分钟停写 + 观察 7 天；观察期 Supabase 只停写不退役、可回切；迁移方式 = 停写窗口内 `pg_dump` / `pg_restore`（27 MB 以分钟计）。
- 进入生产切换前必须齐：rc.C 验收记录 PR 已合；预发按本文完整演练四项全过（切换、备份、副本恢复、回退切回）；生产余盘 ≥ 10 GB；Supabase 付费档自动备份最近一份的时间已实读；脚本 sha 两端相等；来源库参数与 locale 现值已由产品负责人只读回读（Issue #859 评论 5753938437）并钉进 compose 缺省与 `s30.env`（idle 两项 0 / 0、`statement_timeout` 120 s、`TimeZone` UTC、ICU locale `en-US`），`install-pg` 按 `preflight` 实读自动派生 initdb 参数、不手填；上一正式版四镜像（gateway / scheduler / worker 与一次性 `migrate`）按上一正式版 Release 附件 `release-manifest.json` 里的摘要逐一 `docker image inspect <摘要>` 在位（缺任一即先按九拉回，否则 Promotion 之后的部署会在停写窗口内失败）；宿主 hosts 前置成立（下一条）。
- **宿主 hosts 前置（硬判据）**：本地库容器名 `lingxi-db` 必须能在**宿主网络**解析到 `127.0.0.1`——`install-pg` 幂等写入 `/etc/hosts` 一行 `127.0.0.1 lingxi-db`；`switch-dsn` 之前 `getent hosts lingxi-db` 的首字段必须是 `127.0.0.1`，不满足即停止、不进入停写窗口（第四节第 3 步）。原因：部署器用 `docker run --network host` 回读迁移头与业务探针，宿主网络解析不到 compose 网络里的容器名，会在 Promotion 之后、停写窗口之内以 `migration_revision_unknown` 卡住部署。应用容器仍经 compose 网络的内置 DNS 解析同一个名字，两个命名空间各连各的；这一行对回退无害，`rollback-dsn` 不需要删它。
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

预算 30 分钟（D-6）。前三步在窗口外先做，第 4 步开始记时。

| # | 命令 | 预期输出（关键行） | 失败停止点 | 可逆 / 回退 |
| --- | --- | --- | --- | --- |
| 1 | `preflight` | `preflight 通过 → …/preflight.json`；打印来源版本、locale、对象计数、角色占位清单、idle 现值 | 任一「前置不满足」→ 不进下一步 | 只读 |
| 2 | `install-pg` | `[容器] lingxi-db healthy`、`[探针] 空转 + 读写各一次通过`、`[前置] extensions schema + pg_trgm`、占位角色新建 N、`public 处置=toc` | 120 秒未 healthy → `docker logs lingxi-db` | 可逆：`docker compose … down`（不带 `-v`，数据目录保留）；initdb 参数只在首次初始化生效，改参数重试须先清数据目录 |
| 3 | `getent hosts lingxi-db`（宿主上跑，只读） | 首字段 `127.0.0.1` | 首字段不是 `127.0.0.1` 或无输出 → 停止、不进入停写窗口：写入 `/etc/hosts` 一行 `127.0.0.1 lingxi-db`（`install-pg` 的幂等步骤）后重新核对 | 只读 |
| 4 | `stop-write` | 拉取 timer 已停、三容器已停（60 / 90 / 150 s）、采样 timer 已停、`stop_write_at=…` | 任一步失败即停，恢复 = `start-services` + `start-timers` | 可逆：`start-services` + `start-timers` |
| 5 | `dump` | `dump 完成：N B，sha=…`；来源快照 `source-facts.json` / `source.dump.counts.json` 已写 | pg_dump 失败 → `rollback-dsn` 不需要，直接 `start-services` + `start-timers` 恢复旧服务 | 只读 |
| 6 | `restore` | `[toc] 已去掉 CREATE SCHEMA public …`、`restore 完成：public 对象 N、表 N、alembic 0098_…` | 校验和不符 / 目标非空 / pg_restore 报错 → 停，现场保留（`restore.log`） | 只写本地库；中止则 `start-services` + `start-timers`（DSN 未改，不需要 `rollback-dsn`） |
| 7 | `verify` | `verify 零差异：…` 且写出 `verify.json`（`ok:true`） | 任一差异行（段\|键\|来源\|目标）→ **不切换**：`start-services` + `start-timers`，登记差异 | 只读 |
| 8 | `switch-dsn` | 八份文件各 `changed(N 行 DSN)` / `no_dsn_line`；`回读：旧主机段 … 0 命中` | 回读残留 → 中止（见回退栏） | 可逆：中止则 `start-services` → `rollback-dsn`（自带起两只 timer，顺序见六） |
| 9 | 产品负责人点 **Release Promotion v2.6.0** | 发布列表出现正式版 | — | 回滚单独请示 |
| 10 | `start-timers` | 两个 timer `active` | — | `systemctl stop lingxi-release-pull.timer lingxi-db-business-sample.timer` |
| 11 | 等拉取代理部署（≤ 5 分钟一轮 + 部署） | 代理日志 `verified` | 部署失败 → 回滚单独请示（见六） | — |
| 12 | `postcheck` | 三容器 healthy；`[scheduler 容器视角] server_addr <本地库容器地址> alembic 0098_… cleanup_owner lingxi_retention_owner True`；`停写窗口 … = N 分 N 秒` | 连的不是本地库 / 属主不符 → 回滚单独请示 | 只读 |
| 13 | 产品负责人 Bot-Prod 一次真实问数 | 任务 `succeeded` | 失败 → 回滚单独请示 | — |

停写窗口 = 第 4 步 `stop_write_at` 到第 12 步 `postcheck_at`，脚本自己打印；实测必须 ≤ 30 分钟（E-6）。第 4–8 步在假根目录实测合计 11 秒（27 MB 量级，不含 Promotion 与部署）。

**分段计时**（每段起点 = 敲下命令或点击的时刻，终点 = 该段关键行打印的时刻；「预发实测」一栏由编排者在预发纯净窗口演练后回填，回填前按「待回填」读）：

| 段 | 起止 | 预发实测（预发库约 120 MB） | 偏离判据（命中即停止并排查，不进下一步） |
| --- | --- | --- | --- |
| `stop-write` | 敲命令 → `stop_write_at=…` | 待回填 | 超过预发实测 2 倍 → 查 `docker ps`（哪只容器没停）与 `systemctl is-active lingxi-release-pull.service`（是否有在途部署） |
| `dump` | 敲命令 → `dump 完成：N B` | 待回填 | **必须显著快于预发**：生产库约 30 MB、预发库约 120 MB，达到或超过预发实测即异常 |
| `restore` | 敲命令 → `restore 完成：…` | 待回填 | 同 `dump`：达到或超过预发实测即异常 |
| `verify` | 敲命令 → `verify 零差异：…` | 待回填 | 超过预发实测 2 倍 |
| `switch-dsn` | 敲命令 → `回读：… 0 命中` | 待回填 | 超过预发实测 2 倍（八份文件的改写以秒计） |
| 代理部署 | 点 Promotion → 三容器 healthy | 待回填 | 超过「一个轮询周期（5 分钟）+ 预发实测的 2 倍」仍未 healthy → 查代理日志：timer 是否 `active`、`preflight` 是否因旧镜像缺失失败（九）、`prepare` 是否停在 `migration_revision_unknown`（一「宿主 hosts 前置」） |
| `postcheck` | 敲命令 → `postcheck_at=…` | 待回填 | 超过预发实测 2 倍 |

生产库（约 30 MB）的 `dump` / `restore` 应显著快于预发（约 120 MB）：若反而更慢，视为异常（来源连接、目标磁盘或容器资源出了问题），停止并排查，不要靠等。累计（第 4 步起）超过 30 分钟即 E-6 判红，是否继续由产品负责人当场裁定（切换前回退按六）。

**窗口内的预期告警（不作为故障处理）**：第 4 步停掉 `lingxi-release-pull.timer` 后，宿主巡检约 1 分钟内向管理群发一条 `release_pull_timer_inactive`（「拉取 timer 未激活」）告警，第 10 步 `start-timers` 之后再发一条恢复通知；三容器停止同样会触发既有容器检查项的告警（每容器一条「未运行」，部署重建旧容器被删时可能再报一条「不存在」），新容器转 healthy 时各发一条恢复通知。这几条都是停写窗口的预期结果，不按故障处理、不需要回应；只有窗口外出现、或恢复通知迟迟不来才需要查。本地库容器 `lingxi-db` 在整个窗口内保持运行，它的检查项不应告警。

## 五、`verify` 零差异的判据与预期差异

- **判红项**（任一不等即退出非 0、打印逐项差异）：`encoding` / `datcollate` / `datctype` / `datlocprovider` / `datlocale` 五项；角色级 `postgres` 的 `search_path`（来源 `"$user", public, extensions`，`install-pg` 镜像同一条）；`alembic_version`；`public` 对象计数（表 / 索引 / 序列 / 视图 / 函数 / 非内部触发器）；逐表精确行数；序列 `last_value`；逐表触发器名；`pg_trgm` 版本与 schema（`extensions`）；`lingxi_retention_cleanup` 属主 = `lingxi_retention_owner` 且 `prosecdef = true`；`public` 下全部表 / 序列 / 视图 / 函数与 schema 本身的属主逐个相等；同一集合的 ACL 逐对象相等（grantee + 权限位）。
- **预期差异（只列不判红）**：ACL 条目的 grantor（`grantee=权限位/grantor` 里 `/` 后那一段）——比对只按 grantee + 权限位归一，含 grantor 的原文两侧都记进 `verify.json` 的 `acls_raw` 供人工核对（实测 `pg_restore` 以 `SET SESSION AUTHORIZATION` 重放非属主授权、grantor 原样保留，归一只为托管平台侧可能的差异兜底）；`datcollversion`（来源 153.121，本地 `postgres:17` 实测 153.128——同 ICU 73 系，恢复后索引在本地重建，脚本只把两值记进 `verify.json`）；来源的平台扩展（`supabase_vault` / `pg_stat_statements` / `pgcrypto` / `uuid-ossp` 等）不在目标——应用只依赖 `pg_trgm`；来源的平台角色（`supabase_*` / `authenticator` 等）不比对，只比 `public` 对象引用到的角色，它们已在 `install-pg` 按来源清单占位（`NOLOGIN`、无成员、无属性）。
- **参数核对（不在 `verify` 判红项内，`install-pg` 之后人工回读一次）**：`docker exec lingxi-db psql -U postgres -d postgres -Atc 'show idle_in_transaction_session_timeout' -c 'show idle_session_timeout' -c 'show statement_timeout'`，期望 `0` / `0` / `2min`，与来源实读值（一）逐项相等；参数随容器创建生效，切换本身不改它。
- 比对基准是 `dump` 时（已停写）写下的来源快照，不是 `preflight`，也不是任何人工抄写的常量：迁移链头在研发机实得 `public` 表 46，与早先只读盘点表上的 85 不一致，`verify` 只认实时读数。

## 六、回退

- **切换前**（第 4–8 步内失败）：先 `start-services`（三个旧容器沿用创建时的旧 DSN，等于回到 Supabase），再 `rollback-dsn`（逐字节还原八份文件并回读，末尾自带起两只 timer，不必再跑 `start-timers`）。顺序不能反：timer 先于旧容器起，拉取代理会在容器停着时起跑一轮、判不出在位版本；先起容器再起 timer，它只会得到 `already_in_place`。`switch-dsn` 还没跑过（第 4–7 步失败）时不需要 `rollback-dsn`，改为 `start-services` + `start-timers`。本地库容器与数据目录保留供取证，不删。
- **切换后**（Promotion 已点之后：部署 / `postcheck` / 问数失败）：**单独请示**（合同 §2），**不得直接跑 `rollback-dsn`**——它末尾会起拉取 timer，而此时正式版已经是新版本，拉取代理会把新版本部署到还原后的旧连接串（新版本跑在 Supabase 上，既不是回退也不是切换）。回切 = 请示获批后 `rollback-dsn` 还原八份文件 → 拉取代理按 `recover` 独立批准或重新走正常发布把三容器重建为旧 DSN。**切换后新增写入的回退处置**：优先在本地库 `pg_dump -Fc` 导出一份（切换后到回切时刻的全部写入都在里面），待人工回放到 Supabase；当日 `lingxi-db-backup` 定时产物只在导出不可得（本地库已起不来）时才用——定时备份只含备份时刻之前的写入，之后到回切之间的那段会漏掉。旧只读源不含这段写入，不能假设改回连接串即无损。
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

## 九、操作禁忌

- **主机上禁止 `docker image prune` / `docker system prune`**（预发首次真机演练撞到）：部署器的 `preflight` 要求上一正式版四镜像（gateway / scheduler / worker 与一次性 `migrate`）按摘要逐一在位。三只常驻服务镜像有运行容器引用、prune 不会碰；一次性 `migrate` 镜像的迁移容器跑完即删、镜像只剩摘要引用（无标签）= 悬空镜像，prune 会把它删掉。之后的下一次部署在 `preflight` 就以 `fixed_command_failed` 失败，失败命令的输出不进部署账——账上只有这个错误码、阶段账为空，看不出是哪只镜像缺了。清盘只许按名单删明确不再需要的镜像，不许用 prune 一把抓。已经删了的补救：从上一正式版 Release 附件 `release-manifest.json` 取 `migrate` 镜像摘要，`docker pull <migrate 镜像>@<摘要>`（与部署器同一 docker 守护进程、同一登录态）拉回，`docker image inspect <摘要>` 回读退出码 0；拉取代理下一轮接续同一计划重跑即可，不需要人工改计划。发布前自检（一）有对应一条。
