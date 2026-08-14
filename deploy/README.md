# 部署编排（S11，[Issue #62](https://github.com/Moshuiwang/lingxi/issues/62)）

本目录是 Lingxi 的部署编排。**它不是部署设计文档**——部署方案在[架构设计「八、部署与发布」](../docs/技术设计/架构设计.md)定稿，部署对代码的约束在[验证与门禁「十二、部署对代码的约束」](../docs/技术设计/验证与门禁.md)。这里只写"怎么执行"。

> **本批交付的是编排与门禁，不是一次已完成的部署。**
> `biai-stage` 的安装、健康回读、Bot-Test 真实入口与切回旧 tag 演练**尚未执行**；`biplus-prod` 一切未动。生产部署另建独立 `[ops]` Issue，固定镜像 digest、备份点、执行窗口、观察期与回滚目标。**`mvp` profile（见下）仅在本地 `docker compose config` 结构层面验证过，未在 `biai-stage`/`biplus-prod` 实际安装或跑通。**

## 本版包含哪些进程

| 服务 | 形态 | 入口 | 说明 |
| --- | --- | --- | --- |
| `scheduler` | **常驻服务** | `python -m lingxi.apps.scheduler` | 专用授权凭据续期扫描、九十天保留清理、空闲会话到点清理、花名册审计日报、运行告警（[#153](https://github.com/Moshuiwang/lingxi/issues/153) 起 `main()` 真实装配 `AlertingDuty`） |
| `gateway` | **常驻服务，但默认不启动** | `python -m lingxi.apps.gateway` | 飞书长连接接入，收事件落库成任务（#57）；同进程后台线程跑投递消费循环（#152）。放在非默认 profile 里，见下 |
| `worker` | **一次性作业** | `python -m lingxi.apps.worker` | 单回合受控执行。跑一个 Agent SDK 回合、往 stdout 写一个 JSON 报告、退出 |
| `worker-queue` | **常驻服务，仅 `mvp` profile**（[#153](https://github.com/Moshuiwang/lingxi/issues/153)） | `python -m lingxi.apps.worker`（`LINGXI_WORKER_MODE=queue`） | 与 `worker` **同一镜像**，长期领取队列任务；收到 `SIGTERM` 后停止领取、在途任务在一个轮询周期量级内被请求中断、有界预算内收口；同一收口周期消费 Agent 会话 JSONL 物理清理队列 |
| `migrate` | **一次性作业** | `python -m alembic -c /opt/lingxi/alembic.ini` | 部署时跑一次的数据库迁移 |
| `reauthorize` | **一次性作业，默认不启动** | `python -m lingxi.apps.reauthorize` | 凭据丢失或过期时的正式重授权；复用 scheduler 镜像，放在 `job` profile |

**`worker`（一次性）与 `worker-queue`（常驻）是两个不同的 compose service，共用同一个镜像。** `worker` 不领任务、不接飞书、不处理 `SIGTERM`；`worker-queue` 才是常驻消费者（`LINGXI_WORKER_MODE=queue` 切换 `apps/worker/cli.py` 的长期循环分支）。`reauthorize` 仍是一次性作业，必须由运维人员显式 `run`。

**`gateway` 已随 [#57](https://github.com/Moshuiwang/lingxi/issues/57)/[#152](https://github.com/Moshuiwang/lingxi/issues/152) 落地，但默认仍不启动。** 裸 `docker compose up -d`（不带任何 profile）只启动 `scheduler`——这条默认行为本批**没有**改变，改变它需要显式部署合同说明并重新审核（[#153](https://github.com/Moshuiwang/lingxi/issues/153) 完成标准第一条）。启用 gateway 有两条路径：

```bash
# 默认：只启动 scheduler
docker compose --env-file deploy/.env.stage -f deploy/compose.yaml -f deploy/compose.stage.yaml up -d

# 只启用 gateway（沿用 #57 既有用法，投递队列仍无消费者——不建议单独使用）
docker compose --env-file deploy/.env.stage -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile gateway up -d

# 明确命名的 Stage/MVP 受控部署 profile（Issue #153）：同时拉起 scheduler、
# gateway、常驻 queue worker——这是本仓库第一次能把三者串起来的部署形态
docker compose --env-file deploy/.env.stage -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile mvp up -d
```

`mvp` profile 是 Epic A 的候选与 Stage 验收对象，不是已完成的部署——`biai-stage` 的安装、健康回读、真实 CardKit 发送与 Bot-Test 真实入口仍待后置 Ops Story。`admin` 进程仍**未建立**，因此没有为它构建镜像——未实现的入口不得用占位进程冒充。

本版**不宣称**首聊、持续同步、建档、权限发布或用户通知已经上线；`mvp` profile 也**不宣称**员工能真实收到问数结果——真实 CardKit 发送、真实告警发进管理群均未过 L4a。

## 镜像 tag 语义

```
<仓库前缀>/lingxi-<服务>:<YYYYMMDD>-<commit sha 前 12 位>

例：ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
```

- **日期段**标识发布批次，**sha 段**标识源码提交。
- 带上 sha 才是**不可变**的：同一个 tag 永远指向同一份源码，"回滚 = 切回上一个 tag"这件事才有意义。
- **禁止 `latest` 或分支名**。`scripts/ci/check_deploy_contract.py` 会拦住它们。
- 与源码验收标签 `l4a-accepted-*` **不混用**：那是 git tag，只固定源码，不是镜像 tag。

镜像仓库为 GHCR（2026-08-06 产品负责人拍板，见 Issue #62 决策登记），CI 用 Actions 原生 `GITHUB_TOKEN` 推送，不新增外部账号凭据。

## 准备

一个环境要**六个**文件，不是一个：

```bash
cp deploy/.env.example deploy/.env.stage             # 只放 LINGXI_IMAGE_REGISTRY / LINGXI_IMAGE_TAG
$EDITOR deploy/.env.stage.scheduler                  # 数据库 DSN、Fernet 密钥、飞书应用凭据
$EDITOR deploy/.env.stage.gateway                    # 飞书应用凭据 + 数据库 DSN（#57）
$EDITOR deploy/.env.stage.worker                     # 只有 LINGXI_WORKER_* 与模型端点凭据
$EDITOR deploy/.env.stage.migrate                    # 只有迁移 DSN
$EDITOR deploy/.env.stage.reauthorize                # 重授权所需数据库、应用与全部 scope 配置
```

六个名字都匹配 `.gitignore` 既有的 `.env.*` 规则，**不入库**；`scripts/ci/verify_repository.sh` 的敏感配置扫描也已覆盖它们。镜像里不预置任何凭据。

**为什么按服务拆而不是共用一份。** worker 跑的是 Claude Agent SDK，而 SDK 会把自己的进程环境**继承给 Claude Code CLI 子进程和每一个 MCP 子进程**。给 worker 挂一份含数据库连接串、Fernet 密钥与飞书密钥的共享 env，等于把这些凭据送进模型执行环境和第三方 MCP 进程——正是产品合同「凭据不进用户环境」要挡住的方向。scheduler 需要的那些，worker 一个都不需要。

`LINGXI_POSTGRES_DSN` 的示例值保留 `connect_timeout`、`statement_timeout`、`lock_timeout` 三个参数，作为与连接工厂默认值的对账基线；它们不是运行时唯一控制点。`src/lingxi/adapters/postgres.py` 的连接工厂会通过 kwargs 覆盖 DSN 同名参数，合法覆盖使用 `LINGXI_POSTGRES_CONNECT_TIMEOUT_SECONDS`、`LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS`、`LINGXI_POSTGRES_LOCK_TIMEOUT_SECONDS`。停机预算见下方，不能只用 DSN 参数推导。

## 部署前置检查（preflight，逐条通过才能 `up`）

### 1. 凭据文件权限（P1-3）

代码框架「横切约定」要求凭据不进代码、日志、数据库、用户环境，长期凭据放操作系统级密钥管理。本批用文件注入，因此**文件权限就是这条边界的全部**——一个 0644 的 env 文件等于把生产数据库口令、Fernet 密钥和飞书应用密钥摊给机器上任何一个账号。

六个文件都必须 **0600 且属主为部署用户**——`.env.<环境>.gateway` 与 `.env.<环境>.reauthorize` 都装有飞书应用密钥和数据库 DSN，与 scheduler 那份同级，一个都不能漏。用 `install` 一步到位，别先 `cp` 再 `chmod`（那中间有一个短暂的可读窗口）：

```bash
umask 077
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.scheduler
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.gateway
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.worker
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.migrate
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.reauthorize
# 然后再往里写内容
```

`up` 之前逐条核对，任一不符就停下：

```bash
# stage
for f in deploy/.env.stage deploy/.env.stage.scheduler deploy/.env.stage.gateway \
         deploy/.env.stage.worker deploy/.env.stage.migrate deploy/.env.stage.reauthorize; do
  stat -c '%n %a %U' "$f"
done

# 生产（同型，把 stage 换成 prod）
for f in deploy/.env.prod deploy/.env.prod.scheduler deploy/.env.prod.gateway \
         deploy/.env.prod.worker deploy/.env.prod.migrate deploy/.env.prod.reauthorize; do
  stat -c '%n %a %U' "$f"
done
# 期望每行都是 `<文件> 600 <部署用户>`
```

> `.env.<环境>.gateway` 最初漏在这份清单外（#62 终核 P2）。它装飞书应用密钥与数据库 DSN，
> 漏掉它等于把这份凭据留在 README 自己定义的权限边界之外——而这一节的全部意义就是那条边界。

> **登记：长期凭据迁移到操作系统级密钥管理**（Docker secrets / systemd credentials / 云托管密钥服务）尚未落地，本批只立机制与边界，不做真实部署。迁移方案与执行窗口随 stage `[ops]` Issue 决定。在那之前，文件权限是唯一的保护，因此上面这一步不是建议而是前置条件。

### 2. 主机读取身份（P1-4）

GHCR 上的镜像包**默认是私有的**，宿主机不登录就拉不下来——`docker compose up` 会停在
`failed to authorize ... 403 Forbidden`（本批实测过这个报错）。因此部署机必须先有一份**只读**拉取身份：

```bash
# 令牌由 stage [ops] Issue 供给（GitHub App / 组织级 read:packages 令牌），
# **不得使用任何个人 GitHub 凭据**，也不得复用 CI 的 GITHUB_TOKEN。
echo "<LINGXI_GHCR_READ_TOKEN，由 ops 供给>" \
  | docker login ghcr.io -u "<LINGXI_GHCR_READ_USER，由 ops 供给>" --password-stdin
```

两个变量本批只登记名字与来源，**没有真实值**：镜像还没推上去，拉取身份的发放属于 stage `[ops]` Issue 的范围。

> 另有一个待产品负责人决定的选项：把镜像包设为**公开**，宿主机就完全不需要登录，也就没有这份长期驻留在部署机上的凭据。代价是镜像层对外可见（源码本身不在镜像里，但依赖清单与目录结构会暴露）。本批不替产品负责人做这个选择，两条路都可行。

## PR 候选镜像下载与校验（Issue #150；仅用于 #102 验收，不是发布路径）

> **这一节只在验收某个未合并 PR 时使用。** 合并后的正式部署仍按上面「主机读取身份」「安装与升级」走 GHCR 拉取。**严禁在 `biai-stage` 现场用 `docker build` 重新构建这四个镜像，也不得用合并后 `Main Publish` 推送 GHCR 的镜像替代未合并 PR 的候选**——两者是不同对象：合并后的镜像是从合并树重新构建的，不保证与 PR 验收时的树逐位相同（Issue #62 决策登记、[验证与门禁](../docs/技术设计/验证与门禁.md)「五、CI 分层」）。

**为什么需要单独下载**：`biai-stage` 要验收的是"这个 PR 这一次 `Epic Full` 构建出的镜像"，而不是合并后重新构建的另一份对象——`Epic Full / image` job 构建出的四个镜像只存在于那次 CI runner 上，job 结束就消失，因此必须先落地成可下载 artifact。

**去哪下载、保存多久**：`Epic Full` 每次对一个 PR 跑通 `image` job（纯文档 PR 不会有这一步），会产出一个名为

```
epic-candidate-images-pr-<PR 编号>-<PR head sha>
```

的 GitHub Actions artifact，内容是 `manifest.json` + 四个 `lingxi-<service>-<批次>-<commit 短码>.tar`（scheduler / migrate / gateway / worker 各一个）。**这与既有的 `epic-candidate-pr-<PR 编号>-<PR head sha>` 是两个不同的 artifact**——那个只装 `candidate.json`，`Main Publish` 的 `verify_epic_candidate.py` 断言其文件列表严格等于 `["candidate.json"]`，混进镜像 tar 会直接破坏合并后的候选回读，因此镜像制品独立开一个 artifact，不合并进去。

保存期 **14 天**，与 `publish.yml`「降级留证：上传镜像 tar」一致——同类产物同一保存期。超过保存期后无法再下载；需要验收更旧的候选时，让对应 PR 重新跑一次 `Epic Full` 产出新候选，**不能**用合并后的 GHCR 镜像顶替。

**先确认这个 PR 的 base 分支，再选对应的 workflow 名字去找 run**——`image` job 定义在
`ci.yml`（工作流名 `Epic Full`）里，但它在两种触发路径下实际运行在不同的工作流 run 上：

- **base 是 `main`**：PR 直接触发 `ci.yml`，run 就挂在 `ci.yml` 名下：
  ```bash
  gh run list --repo Moshuiwang/lingxi --workflow=ci.yml --branch <PR 分支名> --limit 5
  ```
- **base 是 `epic/**`**（Stage 验收 #102 这一类场景）：PR 触发的是 `story.yml`
  （工作流名 `Story Fast`），`ci.yml` 的 `image` job 由 `story.yml` 的 `full` job 以
  `workflow_call` 方式嵌套执行——**不产生独立的 `ci.yml` run**，artifact 挂在
  `story.yml` 的 run 下。用 `--workflow=ci.yml` 查会得到空列表（实测：本 PR
  #167 base 为 `epic/a-trusted-delivery`，`gh run list --workflow=ci.yml
  --branch claude/150-candidate-artifacts` 返回空；`--workflow=story.yml` 才能查到
  run `31729418864`，其 `workflowName` 为 `Story Fast`，且该 run 确实持有两份
  artifact，含 `epic-candidate-images-pr-167-…`）：
  ```bash
  gh run list --repo Moshuiwang/lingxi --workflow=story.yml --branch <PR 分支名> --limit 5
  ```

拿到 run id 后下载：

```bash
gh run download <run id> --repo Moshuiwang/lingxi \
  --name "epic-candidate-images-pr-<PR 编号>-<PR head sha>" \
  --dir /path/to/bundle-dir
```

**下载后先校验，不导入没核对过的东西**：

```bash
python3 scripts/ci/verify_epic_candidate_bundle.py /path/to/bundle-dir \
  --expect-repository Moshuiwang/lingxi \
  --expect-pr-number <PR 编号> \
  --expect-head-sha <PR head sha> \
  --expect-run-id <run id>
```

manifest 结构、四镜像齐全性（scheduler/migrate/gateway/worker 缺一不可、多一不可）、
每个 tar 的大小与 sha256、以及传入的 PR 身份，任一项不符，脚本非零退出并一次性列出全部
问题——不会因为查到第一个问题就停下让人漏看第二个。

**导入并核对镜像 digest**（追加 `--import`，需要本机 docker）：

```bash
python3 scripts/ci/verify_epic_candidate_bundle.py /path/to/bundle-dir --import
```

这一步会对四个 tar 逐个 `docker load`，再回读每个镜像的 `.Id`，核对与 manifest 记录的
`image_digest` 一致——证明"导入到本机 docker 的东西"确实是这次构建产出的那个，不是
半截下载或被替换的对象。

**接入 compose 使用候选镜像**：候选镜像的本地引用是 `lingxi-<service>:build-a`（CI 构建时
打的本地 tag，不含仓库前缀），而 `deploy/compose.yaml` 的镜像引用要求
`${LINGXI_IMAGE_REGISTRY:?}/lingxi-<service>:${LINGXI_IMAGE_TAG:?}`（`LINGXI_IMAGE_REGISTRY`
不允许留空）。导入后重新打 tag 让两者对上——**tag 必须带上 head sha 前 12 位，不能只用
PR 编号**（manifest.json 的 `head_sha` 字段就是这个值）：同一个 PR 出现新提交是「候选替换
登记要求」一节明确要处理的常见场景，只用 `pr-<PR 编号>` 会让新旧候选打到同一个 tag 上，
`docker load` 一执行旧候选在本机就不再有任何 tag 指向它（悬空、可被 `image prune`
清掉），紧接着的「最小回滚」在这种最常见的场景下反而无对象可回：

```bash
head_sha_short=<PR head sha 前 12 位，取自 manifest.json 的 head_sha>
for service in scheduler migrate gateway worker; do
  docker tag "lingxi-${service}:build-a" \
    "epic-candidate/lingxi-${service}:pr-<PR 编号>-${head_sha_short}"
done
# deploy/.env.stage 对应设置：
#   LINGXI_IMAGE_REGISTRY=epic-candidate
#   LINGXI_IMAGE_TAG=pr-<PR 编号>-<head sha 前 12 位>
```

此后按下面「安装与升级」正常执行；区别只是镜像来自候选 artifact 而不是 GHCR 拉取，
**上面「主机读取身份」那一步（GHCR 登录）此时不需要**——镜像已经在本机 load 过。

**最小回滚**：在 `biai-stage` 上切换候选或回退到上一个候选，只需要把
`deploy/.env.stage` 的 `LINGXI_IMAGE_TAG` 改回上一个候选对应的本地 tag（`pr-<PR 编号>-<上一个
候选的 head sha 前 12 位>`；前提是那个候选镜像仍在本机 docker 里、未被 `docker rmi`），
再执行 `up -d` 即可切回——机制与下面「回滚」一节生产环境切 tag 相同，只是候选场景下 tag
指向本机而不是 GHCR。因为 tag 带了 head sha，新候选导入不会覆盖旧候选的 tag，两者在本机
可以同时存在，回滚才有对象可切。候选之间的切换不触碰生产数据库或生产持久卷；真实生产
的回滚与恢复仍以「回滚」「恢复入口」两节为准，本节不重复。

**候选替换登记要求**：同一个 PR 出现新提交，或旧候选对应的 Actions run 已过期，之前
下载导入的候选立即失效。切换到新候选时，必须在对应验收 Issue（如 #102）里登记：

- **旧候选**：PR head sha、run id、四个 `image_digest`（可从旧 `manifest.json` 摘）。
- **新候选**：同样四项。
- **失效证据**：为什么旧候选不再代表当前 PR（新提交 sha、CI 重跑、run 过期等）。
- **重验范围**：哪些已经在旧候选上做过的验收动作需要在新候选上重跑——不能只换镜像
  不换证据，那样"验收通过"这句话就对不上实际跑过的对象。

## 安装与升级

**`--env-file` 不能省。** `env_file:` 只把变量注入**容器**，它**不参与 compose 文件自身的 `${VAR:?}` 插值**。省掉它，compose 会直接报 `LINGXI_IMAGE_REGISTRY` 未设并退出——下面每条命令都逐字执行验证过。

```bash
# 1. 先跑迁移（一次性作业）
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile job run --rm migrate

# 2. 再启动常驻服务
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml up -d

# 3. 回读
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml ps
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml logs scheduler
```

**上面两项 preflight 未通过时不要执行 `up`。**

生产把 `.env.stage` 换成 `.env.prod`、`compose.stage.yaml` 换成 `compose.prod.yaml`，其余逐字相同——两份覆盖文件**结构完全一致**，只有 env_file、卷名与扫描间隔不同（`scripts/ci/verify_compose_structure.sh` 每次 CI 都比对这一点）。

**生产只拉镜像，不构建**：三个 compose 文件里没有任何 `build:` 键。这不是纪律而是机制。

## 回滚

```bash
# 把 deploy/.env.prod 里的 LINGXI_IMAGE_TAG 改回上一个 tag，然后：
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d
```

回滚不触碰数据库，也不触碰两个持久卷。**前提是迁移遵守"先加后删"**：破坏性变更必须拆成两次发布，否则回滚就从"切 tag 重启"变成"恢复数据库备份"。这一条由 `scripts/ci/verify_old_image_new_schema.sh` 在每次 CI 上实测（断言 V-部署-05）。

## 恢复入口

```bash
docker compose --env-file deploy/.env.prod -f deploy/compose.yaml -f deploy/compose.prod.yaml down
docker compose --env-file deploy/.env.prod -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d
```

数据库备份与恢复遵循 Supabase 托管方案。两个持久卷单独备份。

## 两个持久卷

| 卷（stage / 生产） | 挂载点 | 内容 |
| --- | --- | --- |
| `lingxi-{stage,prod}-credentials` | `/var/lib/lingxi/credentials` | 专用授权凭据加密文件与锁文件 |
| `lingxi-{stage,prod}-users` | `/var/lib/lingxi/users` | 用户环境目录（用户产物与会话） |

**刻意是两个卷**：凭据是安全边界、用户目录是业务数据，备份周期、恢复策略与删除语义都不同，合成一个卷会让"只恢复凭据"或"只清用户目录"变成不可能。

凭据卷必须**跨部署持久**，镜像替换与重启不得丢失——部署目标是对该凭据「零特殊处理」。文件丢失或过期时的保底按下面的正式 job 流程重新走一次「四达文档会议助手」授权。

## 凭据丢失或过期的保底

正式重授权是一次性运维动作，不新增常驻服务。它使用 scheduler 镜像内随包发布的
`python -m lingxi.apps.reauthorize`，以 uid 10001 挂载同一个 `lingxi-{stage,prod}-credentials`
卷；state 文件和凭据文件都在该卷内，入口会在启动前拒绝文件或锁文件路径冲突。授权回调
由 OAuth Bridge 的主动 WebSocket 回传，job 不接收终端粘贴的回跳地址。

以 stage 为例，确认六个 env 文件都已按上面的 preflight 准备好后执行：

```bash
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile job run --rm reauthorize
```

终端只显示授权地址和脱敏结果；按提示在受控浏览器完成同意，授权码由 Worker 即时转发到
已经认证的 biai-stage WebSocket。不要把回跳地址或授权码写入命令行、shell 历史、Issue 或日志。
取消、换码失败或保存失败后不要重放旧回跳，重新运行该一次性 job 取得新的 state。

成功退出后回读 scheduler，并确认凭据文件仍由 uid 10001 可读：

```bash
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml up -d scheduler
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml logs scheduler
```

生产环境将 `.env.stage`、`compose.stage.yaml` 和 `lingxi-stage-credentials` 对应替换为 prod 配置；真实生产操作仍须另建并批准 `[ops]` Issue，不在本 Story 执行。

## gateway 的停止宽限期为什么是 60 秒

收到 `SIGTERM` 后 gateway 停止接收新事件、把在途事件**落库完成**再退出。中途被 `SIGKILL` 会留下"抢占了话题但任务没写进去"的中间态——用户发了消息、系统记得占了坑、却没有任何任务在跑。

| 项 | 秒 | 依据 |
| --- | --- | --- |
| 停机超时 | 20.0 | `LINGXI_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS` 默认值，`apps/gateway/config.py` |
| 出站 HTTP 超时 | 5.0 | `apps/gateway/__init__.py` 取停机超时的 1/4 |
| 数据库往返预算 | 11.0 | 一个在途事件事务，与 scheduler 同一口径 |
| **最坏合计** | **36.0** | × 1.5 安全系数 = 54 秒 → 取整到 **60 秒** |

三项**全部来自 gateway 自己的配置**，不是拍脑袋。同样由 `check_deploy_contract.py` 守住：改了 `SHUTDOWN_TIMEOUT_SECONDS` 的默认值而不改 compose，门禁会红（已实测）。

## scheduler 的停止宽限期为什么是 150 秒

Docker 默认 10 秒，**不满足**。`SIGKILL` 若落在"已经向飞书换过新凭据、尚未写回数据库"的窗口里，那条 `refresh_token` 就永久丢失了——飞书侧旧凭据在续期成功那一刻已作废，一次性有效，只能人工重新授权。

| 项 | 秒 | 依据 |
| --- | --- | --- |
| 续期 HTTP 超时 | 20.0 | `REQUEST_TIMEOUT_SECONDS`，`adapters/feishu_directory.py` |
| 落盘重试退避等待 | 4.2 | `SAVE_RETRY_BACKOFF_SECONDS=(0.2, 1.0, 3.0)`，`apps/scheduler/__init__.py` |
| 数据库往返预算 | 75.0 | 5 次操作（4 次 save 重试 + 1 次 revoke）× 15 秒，按合法覆盖最坏值 |
| **最坏合计** | **99.2** | × 1.5 安全系数 = 148.8 秒 → 取整要求 149 秒，compose 保留 **150 秒** |

其中合法覆盖下"每次数据库操作 ≤ 15 秒"= 合法 `MAX_TIMEOUT_SECONDS=5` 下的 5 秒（建连）+ 5 秒（语句）+ 5 秒（提交，提交本身也是语句）。示例 DSN 的默认值对应名义预算 5+3+3=11 秒。

> **早先的版本在这里写的是 90 秒，而且依据是错的。** 它只把 `connect_timeout` 算进去，但 **`connect_timeout` 只约束建连**——连接建好之后，一条卡住的 `SELECT` / `INSERT` / `COMMIT` 可以无限期挂着。当前运行时边界由 `src/lingxi/adapters/postgres.py` 的连接工厂 kwargs 提供，默认值为 5/3/2 秒；DSN 同名参数会被覆盖，不能作为唯一事实源。合法覆盖必须使用 `LINGXI_POSTGRES_*_TIMEOUT_SECONDS`，不能通过修改 DSN 绕过工厂上界。

**这个不等式由 `scripts/ci/check_deploy_contract.py` 自动守住，不是靠文档。** 门禁从连接工厂读取默认值与 `MAX_TIMEOUT_SECONDS` 建模，并把示例 DSN 的三个参数与默认值对账；修改合法上界或 `REQUEST_TIMEOUT_SECONDS` 而不满足 150 秒宽限期，门禁会红。

`scheduler` 必须**单副本**：进程间互斥靠的是凭据目录里的 `flock` 文件锁，多副本会互相阻塞。

## worker-queue 的停止宽限期为什么是 90 秒

收到 `SIGTERM` 后停止 `claim()` 领取新任务；在途任务的 `_monitor` 把这次停机等同
用户 `/stop`，请求 Agent SDK 中断当前回合，在预算内收口为 `stopped` 终态。中途被
`SIGKILL` 只是把"优雅收口"换成"任务留在 `running`、等未来一次心跳超时被回收"——
不丢结果、不重复副作用（依托 outbox 语义），但会晚一轮才被发现。

| 项 | 秒 | 依据 |
| --- | --- | --- |
| `/stop` 检测延迟 | 1.0 | `stop_poll_interval_seconds` 默认值，`apps/worker/config.py` |
| SDK 收尾宽限 | 30.0 | `DEFAULT_DRAIN_GRACE_SECONDS`，独立于业务墙钟 |
| 终态写库预算 | 15.0 | 按连接工厂合法覆盖上界 `MAX_TIMEOUT_SECONDS=5` 建模（5+2×5） |
| **最坏合计** | **46.0** | × 1.5 安全系数 = 69 秒 → 取整到 **90 秒**（进程自身的 `LINGXI_WORKER_SHUTDOWN_TIMEOUT_SECONDS` 默认 45 秒，90 秒只是 `SIGKILL` 兜底，留了约两倍余量） |

同样由 `scripts/ci/check_deploy_contract.py` 自动守住：改了
`DEFAULT_SHUTDOWN_TIMEOUT_SECONDS`（`apps/worker/config.py`）而不改 compose，门禁会红。

## 健康检查：不开放端口，`docker exec` 语义

三个常驻服务（`scheduler`、`gateway`、`worker-queue`）的 `healthcheck.test` 都是
`python -m lingxi.apps.healthcheck --role <角色>`——**不监听任何端口**，与被检查
的主进程共享同一个容器的文件系统与网络命名空间（合同第 5 条："healthcheck 使用
进程/数据库心跳或受控命令，不为健康检查扩大网络攻击面"）。

两段独立判定，**都要过**：

1. **依赖可达**：用与业务代码同一个连接工厂尝试连接数据库、跑一条 `SELECT 1`；
   数据库不可用时——无论网络问题、凭据错误还是数据库宕机——这一步必然如实失败。
2. **主循环仍在跳动**：读取 `apps/liveness.py` 写的活性文件（主循环每轮
   `touch_liveness(role)`），年龄超过阈值即判不健康。这一段单独存在是因为只测
   依赖可达测不出"进程 PID 还在、数据库也连得上，但主循环因为一次未捕获异常或
   死锁已经停止消费"。`gateway` 一个进程两条循环（长连接主线程、#152 投递消费
   后台线程），各自有独立的活性键（`gateway-longconn`/`gateway-delivery`），
   任一条停摆都会让健康检查变红，不被另一条仍然新鲜的心跳掩盖。

活性文件写在容器内 `/tmp`（已有 tmpfs 挂载），随容器重启自然清空，不需要跨重启
持久化，也不需要跨容器共享。

## 本地验证

```bash
scripts/ci/build_image.sh scheduler                # 构建单个镜像（scheduler|gateway|worker|migrate）
scripts/ci/verify_image_contract.sh <四个镜像引用>  # 镜像契约逐条核对（scheduler worker migrate gateway）
scripts/ci/verify_compose_structure.sh             # stage ↔ 生产结构对照
scripts/ci/verify_old_image_new_schema.sh          # V-部署-05：旧镜像在新库上启动
scripts/ci/verify_old_image_new_schema.sh --destructive   # 同上的变异对照，应判红
```

本机没有 buildx 也能跑：只用 plain `docker build` 支持的语法，不引入 BuildKit 专有语法或缓存后端。
