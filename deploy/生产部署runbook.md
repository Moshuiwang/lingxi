# 生产部署 Runbook（biplus-prod / Bot-Prod）

> **定位声明**：本文件是未来 `biplus-prod` 生产部署的操作正文，写给到时执行部署的人照做；**它本身不构成执行授权**。任何一次真实生产执行（首次部署、后续升级、回滚、恢复演练）都必须先有一个独立的 `[ops]` Issue 完成开工前事实确认与授权，本文件只提供步骤、判据和边界，不替代那次授权。`[ops]` Issue 的产生方式（直接立项，或作为某个生产部署 Trace 的输出）按 [Issue #369](https://github.com/Moshuiwang/lingxi/issues/369) 的「建议去向」处理，本文件不预设具体路径。
>
> 本 Trace（#373 H2 批 S-H2-4）只交付这份 runbook 与配套决策记录，不执行任何生产操作，也不接触 `biplus-prod` 或 `Bot-Prod`。

## 一、前置阅读

执行部署前，先确认以下文件均为当前生效版本，出现矛盾以更晚更新的为准：

- [`deploy/README.md`](README.md)：Compose 编排本身的操作正文（准备、preflight、安装、回滚、恢复入口的逐字命令均以它为准，本文件不重复照抄，只在它之上补生产专属的判据与纪律）。
- [`deploy/验收前部署配置清单.md`](验收前部署配置清单.md)：验收前要配齐什么、缺一项会死在哪里。
- [架构设计「八、部署与发布」](../docs/技术设计/架构设计.md#八部署与发布)与[「九、容量与资源」](../docs/技术设计/架构设计.md#九容量与资源)：部署方案定稿与容量选型依据。
- [验证与门禁「十二、部署对代码的约束」](../docs/技术设计/验证与门禁.md#十二部署对代码的约束)与[验收矩阵第三节](../docs/技术设计/验收矩阵.md#三部署与迁移断言)：`V-部署-*` 断言的认领状态与证据链接。
- [决策：生产 secret 注入与凭据隔离](../docs/决策记录/2026-08-28-生产secret注入与凭据隔离.md)：本文件「九、secret 注入与凭据隔离」一节的裁定依据。
- [Issue #135](https://github.com/Moshuiwang/lingxi/issues/135)：生产凭据类别与百炼凭据复用的产品负责人澄清。
- [Issue #369](https://github.com/Moshuiwang/lingxi/issues/369)：首次生产部署已知缺口台账；本文件覆盖其中第 7、8、12 条以及追加评论第 13-15 条对应事项，其余条目（备份演练、资源限制、监控告警落地、L5/L6 启动条件等）仍是独立未闭合缺口，本文件不重复承接。

## 二、首次部署步骤

以下步骤是对 `deploy/README.md` 既有机制的生产执行顺序汇总，逐字命令以该文件为准；这里只标注生产专属的差异点。

### 2.1 准备七个 env 文件

按 [`deploy/README.md`「准备」](README.md#准备)为 `.env.prod`、`.env.prod.scheduler`、`.env.prod.gateway`、`.env.prod.worker`、`.env.prod.worker-queue`、`.env.prod.migrate`、`.env.prod.reauthorize` 七个文件写入生产凭据。生产凭据集与研发/Stage 凭据集完全隔离，任何一个值都不得与 `.env.stage.*` 系列重复（百炼模型端点凭据除外，见本文件「九」）；不得把研发环境的任何文件复制改名后当作生产文件使用。

### 2.2 部署前置检查（preflight）

逐条通过 [`deploy/README.md`「部署前置检查」](README.md)的两项：七个文件均 `0600` 且属主为部署用户；部署机已用只读拉取身份登录 GHCR。任一项不符不得执行下一步。

### 2.3 迁移

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --profile job run --rm migrate
```

### 2.4 起服务（`--profile mvp`）

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --profile mvp up -d
```

`mvp` profile 同时拉起 `scheduler`、`gateway`、常驻 `worker-queue` 三个常驻服务，语义与 `deploy/README.md` 中 Stage 使用的 profile 完全一致，只替换了 `--env-file` 与 compose 覆盖文件。

### 2.5 健康回读

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml ps
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml logs scheduler gateway worker-queue
```

判定标准见本文件「七、观察期」。

## 三、镜像 digest 固定

`deploy/compose.yaml` 的镜像引用固定为 `${LINGXI_IMAGE_REGISTRY:?}/lingxi-<服务>:${LINGXI_IMAGE_TAG:?}`；`LINGXI_IMAGE_TAG` 的值本身允许写成 `<日期>-<commit sha 前 12 位>@sha256:<digest>` ——Docker 镜像引用语法允许 tag 与 digest 同时出现（tag 供人读，digest 才是实际拉取依据），因此不需要改动 compose 文件结构即可把生产引用锁定到具体 digest，而不是浮动 tag。

**把 tag 解析为 digest（二选一，不需要本机预先 `docker pull`）**：

```bash
# 方式一：直接查询远端 registry（推荐，不占本机磁盘）
docker buildx imagetools inspect ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
# 输出的 Digest: sha256:... 即为该 tag 当前指向的 manifest digest

# 方式二：已经 pull 到本机时，从本地元数据回读
docker pull ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
# 输出形如 ghcr.io/moshuiwang/lingxi-scheduler@sha256:...
```

对四个服务镜像（`scheduler`、`gateway`、`worker`、`migrate`）各解析一次 digest，写入 `.env.prod`：

```bash
LINGXI_IMAGE_TAG=20260806-7a9bcf3fac4a@sha256:<该批次实际 digest>
```

四个服务共用同一批次 tag，因此共用同一个 `LINGXI_IMAGE_TAG` 值——**前提是四个镜像确实来自同一次 `Epic Full / image` 构建**（`deploy/README.md`「安装与升级」已保证生产只拉不建，不会出现四个服务分别对应不同批次的情况）。

**部署时核对**：`up -d` 完成后执行 `docker compose --env-file deploy/.env.prod -f deploy/compose.yaml -f deploy/compose.prod.yaml images`，逐行核对每个服务实际运行的镜像 digest 与 `.env.prod` 中固定的值一致；不一致说明本机存在同 tag 不同 digest 的缓存污染，必须先 `docker image prune` 或显式 `docker pull` 该 digest 后重新 `up -d`，不得在 digest 不匹配的状态下继续判定部署成功。

## 四、单实例纪律

`scheduler`、`gateway`、`worker-queue` 三个常驻服务在生产环境必须始终**单实例**：

- **`scheduler`** 已有代码级互斥：进程间靠凭据目录里的 `flock` 文件锁互斥，多副本会互相阻塞（`deploy/README.md`「安装与升级」前文说明）。
- **`gateway` 与 `worker-queue` 目前没有等价的代码级互斥**（[Issue #369 追加评论第 14 条](https://github.com/Moshuiwang/lingxi/issues/369)：数据库级并发保护靠 `FOR UPDATE SKIP LOCKED`/CAS/唯一约束扎实，但 scheduler 的通知类职责在双实例并发下会重复发送，飞书 uuid 去重是最后防线且未经真实验证；`compose.yaml` 没有 `deploy.replicas` 或容器数量的硬约束）。单实例因此是**运维纪律**，不是被程序强制的边界：任意时刻只允许一台机器、一个操作者持有对 `biplus-prod` 的 compose 命令执行权，不得从两处并行对同一服务发起 `up`/`scale`。
- `gateway` 承载飞书长连接接入，这类共享外部通道同一时刻只允许一个客户端——AGENTS.md 记录的 2026-08-08 事故就是一个为诊断临时起的进程与正式入口抢占同一条 OAuth Bridge 通道，被平台「新连接踢掉旧连接」的语义静默劫持并烧掉了一次性授权码；生产 `gateway` 重部署时如果新旧容器出现并存窗口，会重演同一类风险，只是承载的通道换成了飞书长连接。

**重部署纪律**：先停旧再起新，不做并行叠加。

- 固定使用普通 `docker compose --env-file deploy/.env.prod -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d`（不加 `--scale`、不使用 `--no-deps` 跳过依赖顺序）：compose 对已在运行的服务默认走「先让旧容器完全退出、再创建并启动新容器」的单容器替换流程，本身就满足单实例。
- 怀疑上一次部署未完全收口时，先 `docker compose ... ps` 核对没有残留的旧容器（例如状态卡在 `Restarting` 或存在两个同服务容器），确认干净后再执行 `up -d`；不得在残留未清的状态下直接叠加新容器。
- 需要手工介入单个服务（例如单独重启 `gateway`）时，使用 `docker compose ... stop gateway` 等待其完全退出后再 `docker compose ... up -d gateway`，不使用 `restart`（`restart` 在部分场景下不保证严格的先停后起顺序）。

## 五、首发不灰度

架构设计「八、部署与发布」记录的灰度机制（双 worker 版本分流，[Issue #45](https://github.com/Moshuiwang/lingxi/issues/45)）目前只有设计正文，没有代码或 Compose 载体（[Issue #369 追加评论第 15 条](https://github.com/Moshuiwang/lingxi/issues/369)）。**生产首次发布按整体切换执行**：`--profile mvp up -d` 一次性替换全部常驻服务，不启用、也不能启用架构设计中的灰度分流——因为它当前不存在可运行的实现。

灰度留待后续真实需求触发（例如需要按小流量放量验证新版本行为，而不是每次发布都全量切换）；触发时按架构设计文档指向的 Issue #45 单独实施与验收，不在本 runbook 预支。

## 六、回滚判据与步骤

**判据**：本文件「七、观察期」判定为不健康（时延或最终状态不达标），或部署后短时间内出现明确的业务异常（例如 scheduler 日志持续报错、gateway 无法建立长连接、worker-queue 无法认领任务）。

**回滚 = 回退镜像引用后 `up -d`，不重建**（[Issue #369 第 12 条](https://github.com/Moshuiwang/lingxi/issues/369)）：本仓库不锁传递依赖（代码框架既有选择），同一 commit 在不同日期重新构建不保证逐字节一致；镜像制品本身才是可信的回滚单位，回滚绝不能通过「回退代码 + 现场重新 `docker build`」实现。

```bash
# 1. 把 deploy/.env.prod 的 LINGXI_IMAGE_TAG 改回上一个候选的完整引用
#    （含 digest，例如 20260805-abcdef012345@sha256:<上一批次 digest>）

# 2. 执行 up -d，compose 按「四、单实例纪律」的替换流程逐个服务先停旧再起新
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d

# 3. 回读确认实际运行 digest 与目标 tag 一致（见「三、镜像 digest 固定」）
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml images
```

**回滚不触碰数据库与持久卷**：不执行 `down` 移除 volumes，不执行任何迁移降级操作。这一前提成立的条件是迁移遵守「先加后删」——破坏性变更必须拆成两次发布，否则回滚就从「切镜像重启」变成「恢复数据库备份」；该前提由 `V-部署-05`（[验收矩阵](../docs/技术设计/验证与门禁.md#十二部署对代码的约束)）在每次 CI 上机械核对，2026-08-23 已有 `biai-stage` 真实回滚演练证据（整队切回旧候选、三容器在当前库结构上全部 healthy）。

## 七、观察期

`biai-stage` 2026-08-28 实测（S-W0-1 取证 + S-H1-6 修复后，[Issue #359](https://github.com/Moshuiwang/lingxi/issues/359) 关闭评论、opus 审查 P2-3 留痕项）：错峰调整健康检查间隔后，从「最后一次健康」到「判定 unhealthy」的发现时延变长——

| 服务 | 发现时延（实测） | 健康检查参数（`deploy/compose.yaml`） |
| --- | --- | --- |
| `gateway` | 45s → **69s** | interval 23s |
| `worker-queue` | 65s → **119s** | interval 29s，start_period 32s |
| `scheduler` | 未改变（本次调整不涉及） | interval 30s |

**部署后观察窗口**：至少持续观察 **15 分钟**（覆盖 worker-queue 最坏发现时延 119s 的数倍，留出多个健康检查周期的余量），期间：

- 每隔 1-2 分钟 `docker compose ... ps` 一次，确认三个常驻服务的状态列均为 `healthy`、`restarts` 计数不再增长（部署本身触发的一次容器创建不计入异常重启）；
- 用 `docker inspect --format='{{json .State.Health.Log}}' <容器>` 抽查最近若干次健康检查记录，确认没有非预期的失败探测；
- 核对应用层信号：`scheduler` 日志无持续报错、`gateway` 日志显示长连接已建立、`worker-queue` 日志显示能正常进入领取循环（不要求已经真实处理业务任务）。

**判定标准**：观察窗口结束时三项常驻服务全部 `healthy` 且无非预期重启，判定本次部署成功，转入正常运行；若在对应服务的最坏发现时延内（`gateway` ≥ 69s、`worker-queue` ≥ 119s）仍未转为 `healthy`，或观察期内出现非预期重启，判定部署失败，进入「六、回滚判据与步骤」。

## 八、恢复演练要求

数据库从备份恢复后，在重新对外提供服务（即允许新的问数流量、重新拉起三个常驻服务处理正常任务）之前，**必须先按内容原始写入时间重新执行一轮保留清理**（`V-投递-07` 语义，[验收矩阵](../docs/技术设计/验证与门禁.md#十二部署对代码的约束)）：备份或 WAL 中无法逐条即时删除的内容（含尚未结束的会话保留窗口）按原写入时间计算到期，不能把恢复完成的时间当成新的保留起点，也不能借这次恢复重新打开一个已经结束的会话窗口。恢复历史备份可能重新带回备份时已有的用户内容，这是已经接受的灾备取舍，但「重新提供服务前先补清理」是这条取舍成立的前提，不是可选步骤。

一次真实的数据库恢复演练目前尚未执行（[Issue #369 第 4 条](https://github.com/Moshuiwang/lingxi/issues/369)）；生产首次部署前应至少完成一次真实恢复演练，核对上述补清理动作确实被执行且结果符合预期，本 runbook 只登记要求，不代为执行。

## 九、secret 注入与凭据隔离

规则依据见[决策：生产 secret 注入与凭据隔离](../docs/决策记录/2026-08-28-生产secret注入与凭据隔离.md)，这里只给操作要点：

- **完全隔离**：生产凭据集与研发/Stage 凭据集不共用任何值；唯一的产品负责人明确授权例外是百炼模型端点凭据，且该例外只注入 worker/模型执行路径，不给 gateway、scheduler 或无关进程继承（2026-08-14 澄清，[Issue #135](https://github.com/Moshuiwang/lingxi/issues/135)）。
- **Stage 不变**：`biai-stage` 继续维持现有 `0600` env 文件注入机制，本决定不改动 Stage。
- **生产首发起步**：docker compose secrets 或 `0600` env 文件二选一，均可满足首发要求；本 runbook 「二、首次部署步骤」按 `0600` env 文件路径给出命令（与 `deploy/README.md` 既有机制一致），选择 compose secrets 时按 Compose 官方 `secrets:` 顶层键与逐服务 `secrets:` 挂载改写对应命令，凭据不进 `--env-file` 明文的效果与 `0600` env 文件等价，实施细节在真正切换该路径的独立工作项中确定。
- **OS 级密钥管理迁移路线**：作为后续路线书面登记，不在本次落地。触发条件（满足任一即评估启动迁移）：出现监管或审计对密钥管理提出强制要求；需要频繁轮换密钥且当前手工替换 `0600` 文件的方式不可持续；需要向多主机分发同一份生产凭据。大致形态：候选包括 systemd credentials（宿主机原生、不引入新的常驻服务）、云厂商托管密钥服务（如 KMS/Secrets Manager 类产品，取决于最终生产主机所在的云环境）；具体产品与工具选型在触发时由独立 `[ops]` 或 `[task]` Issue 决定，本文件不预先选定供应商。

## 十、监控与日志

生产部署后的持续可观测性不在本文件展开，指向同批次另外两张卡交付的文档：

- 基础设施层监控与告警：`deploy/监控告警.md`（Trace #373 H2 批另一张卡交付；本文件写作时该文件尚未创建，链接名字已按批次合同冻结）。
- 容器日志留存：`deploy/日志留存.md`（同上）。

这两份文件到位后，「七、观察期」之外的长期运行监控与故障发现路径以它们为准；本文件不重复维护监控阈值或日志保留期限的具体数值。
