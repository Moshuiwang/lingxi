# 部署编排（S11，[Issue #62](https://github.com/Moshuiwang/lingxi/issues/62)）

本目录是 Lingxi 的部署编排。**它不是部署设计文档**——部署方案在[架构设计「八、部署与发布」](../docs/技术设计/架构设计.md)定稿，部署对代码的约束在[验证与门禁「十二、部署对代码的约束」](../docs/技术设计/验证与门禁.md)。这里只写"怎么执行"。

验收前要**配齐什么**、缺一项会死在哪里、配完怎么自检，见[验收前部署配置清单](验收前部署配置清单.md)——本文件负责"怎么跑起来"，那份文件负责"跑起来之前要凑齐哪些东西"。生产首次部署的分步骤、镜像 digest 固定、单实例纪律、首发不灰度、回滚判据与观察期见[生产部署 runbook](生产部署runbook.md)；基础设施监控与告警现状见[监控告警](监控告警.md)；容器日志留存现状见[日志留存](日志留存.md)。

> **本批交付的是编排与门禁；`mvp` profile 已在 `biai-stage` 真实运行，`biplus-prod` 仍一切未动。**
> 自 2026-08-21 起，`scheduler`/`gateway`/`worker-queue` 三容器在 `biai-stage` healthy 运行，其后经历多次滚动升级；健康回读与切回旧 tag 演练均已真实执行（`V-部署-05`：2026-08-23 整队回滚旧候选、三容器全部 healthy 再滚回，见[当前能力](../docs/当前能力.md) 2026-08-21/08-23 记录）。**`biplus-prod` 一切未动**：生产部署另建独立 `[ops]` Issue，固定镜像 digest、备份点、执行窗口、观察期与回滚目标。

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

`mvp` profile 最初是 Epic A 的候选与 Stage 验收对象；**自 2026-08-21 起已在 `biai-stage` 真实运行并多次滚动升级**——安装、健康回读、真实 CardKit 发送与 Bot-Test 真实入口均已真实验证通过 L4a（受控测试身份，非生产；见[当前能力](../docs/当前能力.md) 2026-08-21～2026-08-27 各条记录）。`admin` 进程仍**未建立**，因此没有为它构建镜像——未实现的入口不得用占位进程冒充；MVP 现行管理入口是飞书私聊命令面（方案 A），不经独立进程，详见[产品合同「管理员处理入口与安全确认」](../docs/产品合同与外部边界.md)一节。

本版**不宣称**上述能力已在 `biplus-prod` 上线：`biplus-prod` 一切未动，硬切 [#263](https://github.com/Moshuiwang/lingxi/issues/263) 未动，正式权限表零写入；`biai-stage` 上的全部真实验证均使用 `Bot-Test` 受控测试身份，不代表生产环境已具备同等能力。

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

## 版本号规则

（Issue #417，2.0 发布前置）

- **规则**：[SemVer 2.0.0](https://semver.org/lang/zh-CN/)（`主.次.补丁`）。破坏用户承诺或外部边界升主版本，新增能力升次版本，修复升补丁版本。本仓库此前无对外版本号，首版定 `2.0.0`。
- **粒度**：整仓单一版本，不分服务。四个进程镜像永远同一 commit 同批构建、同批部署（单实例纪律、整体切换回滚），分服务版本只会制造组合矩阵而无独立发布收益；服务级追溯已由上文「镜像 tag 语义」的 `日期-sha` 承担。`content.toml` 用户可见文案版本（`check_content_version.py` 校验，2026-08-17 决策）是独立纪律，**不与本版本号合并**。
- **载体**：`pyproject.toml` 的 `[project] version` 是唯一事实源。发布时打 git tag `v<版本号>`（例：`v2.0.0`），不可变；与源码验收标签 `l4a-accepted-*` 是两套不同用途的 git tag，不混用。镜像 tag 维持现行 `日期-sha` 格式不变（见上文「镜像 tag 语义」），版本号与镜像的对应关系由 tag 指向的提交记录承载，不重造镜像命名。
- **升版时机**：随发布批次由发布 PR 统一升版并打 tag，不逐 PR 升版，避免版本噪音。
- **CHANGELOG**：[`CHANGELOG.md`](../CHANGELOG.md) 按 Keep a Changelog 约定随发布批次维护，条目引用 Issue/PR。

## 准备

一个环境要**七个**文件，不是一个：

```bash
cp deploy/.env.example deploy/.env.stage             # 只放 LINGXI_IMAGE_REGISTRY / LINGXI_IMAGE_TAG
$EDITOR deploy/.env.stage.scheduler                  # 数据库 DSN、Fernet 密钥、飞书应用凭据
$EDITOR deploy/.env.stage.gateway                    # 飞书应用凭据 + 数据库 DSN（#57）
$EDITOR deploy/.env.stage.worker                     # 只有 LINGXI_WORKER_* 与模型端点凭据
$EDITOR deploy/.env.stage.worker-queue               # 常驻队列消费者：含数据库 DSN（#153 PR #173 复核 P1-2）
$EDITOR deploy/.env.stage.migrate                    # 只有迁移 DSN
$EDITOR deploy/.env.stage.reauthorize                # 重授权所需数据库、应用与全部 scope 配置
```

七个名字都匹配 `.gitignore` 既有的 `.env.*` 规则，**不入库**；`scripts/ci/verify_repository.sh` 的敏感配置扫描也已覆盖它们。镜像里不预置任何凭据。

**为什么按服务拆而不是共用一份，且 `worker` 与 `worker-queue` 也必须分开。** worker（一次性 job）跑的是 Claude Agent SDK，而 SDK 会把自己的进程环境**继承给 Claude Code CLI 子进程和每一个 MCP 子进程**。给它挂一份含数据库连接串、Fernet 密钥与飞书密钥的共享 env，等于把这些凭据送进模型执行环境和第三方 MCP 进程——正是产品合同「凭据不进用户环境」要挡住的方向。`worker-queue`（常驻队列消费者）不跑 Agent SDK、不继承环境给任何子进程，但它需要 `LINGXI_POSTGRES_DSN` 才能领任务——这正是它必须有自己独立一份 env 文件、不能借用 `worker` 那份的原因（PR #173 复核 P1-2：早期版本让两者共用 `.env.<环境>.worker`，结果要么 `worker-queue` 拿不到 DSN 无限崩溃重启，要么 `worker` 意外拿到了它不该有的数据库凭据）。scheduler 需要的那些，`worker` 一个都不需要。

`LINGXI_POSTGRES_DSN` 的示例值保留 `connect_timeout`、`statement_timeout`、`lock_timeout` 三个参数，作为与连接工厂默认值的对账基线；它们不是运行时唯一控制点。`src/lingxi/adapters/postgres.py` 的连接工厂会通过 kwargs 覆盖 DSN 同名参数，合法覆盖使用 `LINGXI_POSTGRES_CONNECT_TIMEOUT_SECONDS`、`LINGXI_POSTGRES_STATEMENT_TIMEOUT_SECONDS`、`LINGXI_POSTGRES_LOCK_TIMEOUT_SECONDS`。停机预算见下方，不能只用 DSN 参数推导。

### 数据库凭据源：Supabase 私有凭据文件（Issue #411）

stage 与生产的数据库为 Supabase 云托管（[#40 决策](https://github.com/Moshuiwang/lingxi/issues/40)，2026-08-05）。数据库连接串的**唯一事实源**是目标机器上、仓库工作副本之外的私有凭据文件（0600，属主为部署用户）：

| 环境 | 机器 | 凭据文件 |
| --- | --- | --- |
| stage | `biai-stage` | `/home/wangzhipeng/.config/lingxi/supabase-stage.env` |
| 生产 | `biplus-prod` | `/home/bi-ai-deploy/.config/lingxi/supabase-prod.env` |

文件含 `LINGXI_POSTGRES_DSN`、`LINGXI_GATEWAY_POSTGRES_DSN`、`LINGXI_MIGRATION_DSN`（及项目号/区域两个非敏感说明变量）。**凭据不复制到研发机 tz，不进日志、Git、Issue 或 PR**；`.env.<环境>.*` 各服务文件里的 DSN 行由它派生，用同步脚本写入（先按下文「安装与升级」备份 env 文件再跑）：

```bash
scripts/ops/sync_db_env_from_credentials.sh ~/.config/lingxi/supabase-stage.env deploy .env.stage
# 生产同构：…/supabase-prod.env deploy .env.prod（仅在生产发布 ops 卡内执行）
```

脚本按「每个服务只拿自己那一个变量」写入 scheduler / gateway / worker-queue / reauthorize（运行 DSN）与 migrate（迁移 DSN），并核对 `.env.<环境>.worker` 零数据库凭据（上文红线）；只输出文件名与变量名，永不回显值。不把凭据文件整份挂成 `env_file`：它同时含迁移 DSN（DDL 权限），整挂会送进业务进程。

**连接模式约束**：DSN 必须保持 Supavisor **session 模式**（池化主机 `:5432`）或直连；**禁止改为 `:6543` transaction 模式**——worker 的常驻 `LISTEN` 连接在 transaction 模式下不可用且会占死池连接（Supavisor 维护者答复，见 #411 调研评论）。

## 部署前置检查（preflight，逐条通过才能 `up`）

### 1. 凭据文件权限（P1-3）

代码框架「横切约定」要求凭据不进代码、日志、数据库、用户环境，长期凭据放操作系统级密钥管理。本批用文件注入，因此**文件权限就是这条边界的全部**——一个 0644 的 env 文件等于把生产数据库口令、Fernet 密钥和飞书应用密钥摊给机器上任何一个账号。

七个文件都必须 **0600 且属主为部署用户**——`.env.<环境>.gateway`、`.env.<环境>.worker-queue` 与 `.env.<环境>.reauthorize` 都装有飞书应用密钥或数据库 DSN，与 scheduler 那份同级，一个都不能漏。用 `install` 一步到位，别先 `cp` 再 `chmod`（那中间有一个短暂的可读窗口）：

```bash
umask 077
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.scheduler
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.gateway
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.worker
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.worker-queue
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.migrate
install -m 600 -o "$(id -un)" -g "$(id -gn)" /dev/null deploy/.env.stage.reauthorize
# 然后再往里写内容
```

`up` 之前逐条核对，任一不符就停下：

```bash
# stage
for f in deploy/.env.stage deploy/.env.stage.scheduler deploy/.env.stage.gateway \
         deploy/.env.stage.worker deploy/.env.stage.worker-queue \
         deploy/.env.stage.migrate deploy/.env.stage.reauthorize; do
  stat -c '%n %a %U' "$f"
done

# 生产（同型，把 stage 换成 prod）
for f in deploy/.env.prod deploy/.env.prod.scheduler deploy/.env.prod.gateway \
         deploy/.env.prod.worker deploy/.env.prod.worker-queue \
         deploy/.env.prod.migrate deploy/.env.prod.reauthorize; do
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

> **2026-09-02 更正与现状**：这个选项已被选中——四个镜像包当日为**公开**，`biplus-prod` 首次部署未登录 GHCR 直接匿名拉取，上面这条登录步骤在生产首发中**不适用**。
>
> 同时更正本节此前的一句错误表述：原文写「源码本身不在镜像里，只暴露依赖清单与目录结构」，**这不成立**。镜像里包含**全部源码**、用户可见文案版本文件 `content.toml`、公司与职能到指标的映射 TOML，以及 `migrations/` 下的迁移 SQL（安装态的 `lingxi` 包连同这些随包 TOML 一起进了每个运行阶段的 `site-packages`；`migrations/` 另由 migrate 阶段单独 `COPY`）。因此把包设为公开的真实代价是**这些内容对任何人可读**，不只是依赖清单与目录结构。
>
> 产品负责人在知情前提下裁定上线日维持公开、不加拉取令牌步骤；**「改回 private 并发放只读拉取令牌」登记为上线后的观察期项**（Trace [#521](https://github.com/Moshuiwang/lingxi/issues/521)，实录见 [`生产部署runbook.md`「十一、实录偏差」](生产部署runbook.md)）。改回 private 之后，上面这段登录步骤重新生效。

## 编排者冻结前显式触发镜像构建（Issue #278）

`epic/*` 分支合并到 `main` 走的是一条常驻「跟踪 PR」（整个 Epic 期间只开一次，base 是
`main`，head 是该 epic 分支；每个 Story 合入 epic 分支都会把它 `synchronize` 一次）。
Issue #278 之前，这条跟踪 PR 的**每一次** synchronize 都会自动跑一遍 `Epic Full / image`
（双腿共 8 次镜像构建，约 4 分钟）；Epic D 实测 5 次 synchronize 只有 1 次的镜像制品被
下面「PR 候选镜像下载」验收流程真正下载用过，其余全是陪跑。

**Issue #278 之后**：跟踪 PR（head 为 `epic/**`、base 为 `main`）的**首次自动**
synchronize 不再自动跑 `image` job（`gate` / `extras` 两个快速检查仍然照常自动跑，
不受影响）；直接开给 `main` 的普通 PR（例如某个 `fix/xxx` 分支）不受任何影响，
`image` 仍然照旧每次自动跑——这是"PR 检查阶段的可复现性门"，本 Issue 不动它。

**编排者要固定某个候选提交（准备验收或合并前）时的主姿势——空提交 Story PR，
提交信息末尾带一行 `Image-Candidate: true` trailer**（机器人身份可执行，不需要
人工操作 GitHub 网页）：

```bash
# 0. 前提：`epic/**` 挂了 ruleset「仅 Story Fast 后合并」（pull_request 规则 +
#    current_user_can_bypass: never），机器人身份不能直接 `git push` 空提交到
#    epic 分支——服务端会拒绝。必须走「开 Story PR、Story Fast 通过、squash 合并」
#    这条已有路径（与其余 Story PR 完全一样，只是这个 PR 的 diff 是空的）。

# 1. 从 epic 分支当前 tip 开一个只有空提交的临时分支：
git fetch origin <epic 分支名>
git checkout -b chore/image-candidate-trigger origin/<epic 分支名>
git commit --allow-empty -m "chore: 冻结候选镜像构建触发

Image-Candidate: true"
git-claude-tz push -u origin chore/image-candidate-trigger

# 2. 开 Story PR，base 指向 epic 分支（不是 main）：
gh pr create --repo Moshuiwang/lingxi \
  --base <epic 分支名> --head chore/image-candidate-trigger \
  --title "chore: 冻结候选镜像构建触发" \
  --body "空提交触发，无实际代码改动；用于让编排者显式构建一份候选镜像（Issue #278）。"

# 3. 等 Story Fast 变绿（空 diff 会被 classify_story_changes.py 判成 full——
#    "没有改动路径"和"高风险改动"用同一个兜底分支，见 scripts/ci/
#    classify_story_changes.py 的 classify()——因此这个触发 PR 自己也会跑一遍
#    完整 Epic Full，包含它自己的一次镜像构建；这是空 diff 场景下唯一可选的
#    Story Fast 路径，不能绕开）：
gh pr checks <PR 编号> --repo Moshuiwang/lingxi --watch

# 4. squash 合并，**显式指定提交信息**（默认的 squash 消息不保证带上标记，
#    必须用 --subject/--body 显式设置；trailer 必须独占一行、精确是
#    `Image-Candidate: true`，前后不能有多余字符）：
gh pr merge <PR 编号> --repo Moshuiwang/lingxi --squash \
  --subject "chore: 冻结候选镜像构建触发" --body "Image-Candidate: true"
```

**标记为什么用 trailer 形状而不是行内的 `[image-candidate]` 之类方括号写法**：
方括号写法是子串匹配，任何"提到这个标记本身"的文字（哪怕只是像这段说明一样在
讨论这个机制）都会被误判成"触发"——这不是假设：本 Issue 第一版实现时就在自己的
commit message 里描述这条机制，恰好把 `[image-candidate]` 这个词组原样写了进去，
被自己的检测逻辑命中过一次（未影响该次验证结论，因为那次 PR 的 head 不是
`epic/*`，但足以证明子串匹配这条路子不安全）。trailer 形状要求整行精确匹配
`Image-Candidate: true`，没有人会在说明性文字里恰好独立成行地写这一句，从根上
避免这类自指误判。

第 4 步产生的 squash 提交成为 epic 分支新的 tip，把跟踪 PR（epic 分支 -> `main`）
再 synchronize 一次；`.github/workflows/ci.yml` 的 `classify` job 会用
`git log -1 --format=%B <PR head sha>` 读出这个提交的完整信息（不是 `HEAD`——
`pull_request` 事件默认检出的是 GitHub 生成的临时 merge 提交，其信息是自动生成的
合并说明，不是真正头提交的信息），逐行精确匹配到 `Image-Candidate: true` 就把
`image` job 的按需跳过条件解除，这次 `image` 真正执行，下面「PR 候选镜像下载与
校验」一节描述的 `epic-candidate-images-pr-<PR 编号>-<PR head sha>` artifact 与
`epic-candidate-pr-<PR 编号>-<PR head sha>`（`candidate.json`，`Main Publish`
回读用）都会按原有逻辑产出，两条下游链路都不需要为这次改动单独适配。

**空提交为什么不破坏候选证明的树比对**：`scripts/ci/verify_epic_candidate.py` 的
`validate_document`（第 54-69 行）同时校验 `tree_sha`（第 62 行）与 `head_sha`
（第 61 行，要求等于 `pr["head"]["sha"]`）。空提交不改任何文件，树与它的父提交
完全相同；只要空提交落地之后到编排者实际合并跟踪 PR 之前没有再发生新的提交（既有的
冻结纪律本来就要求这样——最终候选必须全绿且不再变化），`document["head_sha"]`
（此次 synchronize 时的 PR head sha）与合并时 `pr["head"]["sha"]` 相同，
`document["tree_sha"]`（此次 run 检出临时 merge 提交算出的树）与 `Main Publish`
合并后重新算出的树也相同——两个判据都不受空提交影响。

**备选姿势——`gh run rerun`（只有 Actions 写权限的身份能用，机器人不行）**：

```bash
gh run list --repo Moshuiwang/lingxi --workflow=ci.yml --branch <epic 分支名> --limit 5
gh run rerun <run id> --repo Moshuiwang/lingxi   # 全量重跑，不要加 --failed
```

`gh run rerun` 重跑的是**同一个 run**，原样保留它的 `pull_request` 触发身份
（PR 编号、head sha 都不变），只是 `github.run_attempt` 从 `1` 变成 `2`，同样能
解除 `image` job 的按需跳过条件。**但本机机器人身份（GitHub App 令牌）对这个仓库
没有 Actions 写权限**：`gh api user` 与 `gh run rerun` 都实测返回 403
`Resource not accessible by integration`（2026-08-21 PR #285 现场复验；
2026-08-06 已有同样记录，见项目 memory `pm-dev-workflow.md`）。因此
`gh run rerun` 不能作为机器人可执行的主姿势，只能留给产品负责人等确实持有 Actions
写权限的身份通过网页或个人令牌手动操作——本节把它保留为备选，不是首选。

**为什么两条姿势都不能换成 `workflow_dispatch`**：`.github/workflows/ci.yml` 的
`on:` 里一直都有 `workflow_dispatch`，随时可以对任意 ref 手动跑一次 `Epic Full`
（包括 `image`），适合脱离 PR 单独确认某个提交能不能可复现构建。但它产生的 run 在
GitHub API 里 `event` 字段永远是 `workflow_dispatch`，不是 `pull_request`——
`scripts/ci/verify_epic_candidate.py` 查询候选证明时按 `?event=pull_request`
过滤（第 121-122 行；回读它要求"这个 PR 的这次 Epic Full"，而不是任意一次手动
运行），`workflow_dispatch` 的 run 无论怎么传 `--ref`、怎么改产出的 artifact 命名
都不会被这条回读命中——这是 GitHub Actions 的既定行为，无法绕开。因此需要产出候选
证明（`candidate.json` 或 `epic-candidate-images-pr-*`）时必须用上面两条姿势之一，
`workflow_dispatch` 只能用于不需要候选证明的场景（例如只是想手动确认某个提交的
镜像可复现构建）。

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

> **升级/重部署前置步骤**：容器 stdout 日志不会跨重部署持久（旧容器被替换即丢失，
> 见 [#343](https://github.com/Moshuiwang/lingxi/issues/343)）。执行下面的 `up -d`
> 之前，先手动跑一次日志收集脚本把当前容器的最新日志 flush 到宿主机持久目录，
> 完整说明见 [deploy/日志留存.md](日志留存.md)。

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
| `lingxi-{stage,prod}-users` | `/var/lib/lingxi/users` | 用户环境目录（用户产物、会话，**以及每个用户自己的问数 MCP 令牌明文** `<uid>/.mcp.json`） |

**刻意是两个卷**：两者的备份周期、恢复策略与删除语义都不同，合成一个卷会让"只恢复凭据"或"只清用户目录"变成不可能。

用户卷现在有两个读写者（Epic D 闸⑤/⑥）：**scheduler** 的首次开通编排在这里给每个用户建家目录、写入 `.mcp.json`（读写，`compose.stage.yaml`/`compose.prod.yaml` 各自挂载）；**worker-queue** 处理任务时按该任务的 `user_id` 从这里读出对应的 `.mcp.json`，作为这次会话专属的 MCP 配置（只读语义，读不到就失败关闭，绝不回退到任何共用配置）。一次性 `worker` job（受控回合模式）也挂了这个卷，但不参与真实用户任务处理。

> **2026-08-18 修订（重要）**：用户卷**不再是纯业务数据**。产品负责人 2026-08-17 裁定 worker→问数 MCP 的逐用户令牌写进用户环境的 `.mcp.json`（正文见[架构设计 6.9](../docs/技术设计/架构设计.md)与[决策记录](../docs/决策记录/2026-08-18-用户环境持有问数MCP令牌.md)）。因此**用户卷的备份、快照、导出与介质交接必须按凭据级别对待**——它是这次裁定唯一新增的凭据外泄通道。文件本身是 `440`、家目录 `700`、根目录 `750`，但备份介质会把这层权限抹平。
>
> 两个卷的区别因此改成：凭据卷装的是**全租户级**凭据（专用授权主体的 `refresh_token`），泄露等于全体用户的数据面；用户卷装的是**逐用户**凭据，泄露面等于那个人本人已有的数据范围。级别不同，但都不是业务数据。

凭据卷必须**跨部署持久**，镜像替换与重启不得丢失——部署目标是对该凭据「零特殊处理」。文件丢失或过期时的保底按下面的正式 job 流程重新走一次「四达文档会议助手」授权。

## 凭据丢失或过期的保底

正式重授权是一次性运维动作，不新增常驻服务。它使用 scheduler 镜像内随包发布的
`python -m lingxi.apps.reauthorize`，以 uid 10001 挂载同一个 `lingxi-{stage,prod}-credentials`
卷；state 文件和凭据文件都在该卷内，入口会在启动前拒绝文件或锁文件路径冲突。授权回调
由 OAuth Bridge 的主动 WebSocket 回传，job 不接收终端粘贴的回跳地址。

以 stage 为例，确认七个 env 文件都已按上面的 preflight 准备好后执行：

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
| 落盘重试退避等待 | 4.2 | `SAVE_RETRY_BACKOFF_SECONDS=(0.2, 1.0, 3.0)`，`apps/scheduler/credential_rotation.py`（#237 拆分后的新位置） |
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

**这张表必须和 `scripts/ci/check_deploy_contract.py::_worker_worst_case_seconds()`
的实际门禁模型逐项对上**——PR #173 独立复核 P3-2 发现旧版这里另起一套推导
（1.0+30.0+15.0=46.0），算出的余量与门禁实际要求的数字对不上：门禁的模型是
"进程自身停机预算（已经把 `/stop` 检测延迟、SDK 收尾宽限都算进去了）再加一次
终态写库预算"，不是把这些分项在这里重新加一遍。

| 项 | 秒 | 依据 |
| --- | --- | --- |
| 进程自身停机预算 | 45.0 | `LINGXI_WORKER_SHUTDOWN_TIMEOUT_SECONDS` 默认值（`DEFAULT_SHUTDOWN_TIMEOUT_SECONDS`，`apps/worker/config.py`）——它自己的推导已经包含 `/stop` 检测延迟（1.0s）、SDK 收尾宽限（`DEFAULT_DRAIN_GRACE_SECONDS` 30.0s，独立于业务墙钟）与一次终态写库预算，合计约 46s、取整为 45s，见该常量的头部注释 |
| 终态写库预算 | 15.0 | 门禁模型在上面这个已经打包好的预算之外，**再单独留一次**终态写库预算作为 `SIGKILL` 兜底窗口，按连接工厂合法覆盖上界 `MAX_TIMEOUT_SECONDS=5` 建模（5+2×5） |
| **最坏合计** | **60.0** | × 1.5 安全系数 = `math.ceil(60.0 × 1.5)` = **90 秒**（这是门禁要求的下限，不是留了余量后取整——90 秒恰好等于门禁的最低要求，一点富余都没有；`stop_grace_period` 写成低于 90 的任何值都会打红门禁，写成刚好 90 才是精确匹配，不是巧合） |

同样由 `scripts/ci/check_deploy_contract.py` 自动守住：改了
`DEFAULT_SHUTDOWN_TIMEOUT_SECONDS`（`apps/worker/config.py`）而不改 compose，门禁会红；
改这里的 `stop_grace_period` 时，务必先用
`python3 scripts/ci/check_deploy_contract.py` 里的
`_worker_worst_case_seconds()` 现算一次门禁的实际下限，不要凭这张表的旧数字
手动推算——两者一度不一致过。

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

## 资源限制

Trace #373 H2（S-H2-1）、产品负责人 2026-08-28 先行裁定：六个服务在
`deploy/compose.yaml` 都用 `deploy.resources.limits`（`cpus`/`memory`/`pids`）声明
资源上限，此前完全没有限制——一个服务失控（内存泄漏、fork 炸弹式的子进程）能
饿死同一台机器上的其他服务而没有任何机制拦住。

**语法选型**：`deploy.resources.limits`，不是顶层 `mem_limit`/`cpus`/`pids_limit`。
两种写法在 `docker compose up`（非 swarm 模式）下都真实生效——本机 Docker
29.1.3 + Compose 2.40.3 实测核对过两者渲染后的 `HostConfig.Memory` /
`HostConfig.NanoCpus` / `HostConfig.PidsLimit`逐位相同；选 `deploy.resources.limits`
是因为三项限制归在同一个键下，覆盖文件整块替换不容易漏改某一项。stage 实际的
Docker 25.0.14 + Compose v5.2.0 未在本机复现，采用这个选型前请在 `biai-stage`
用下面「结构渲染」加一次真实 `up` + `docker inspect` 回读复核（留作 L4a）。

**数值必须分环境，`compose.yaml`（基线）只给一个安全默认值，真正生效的数值来自
`compose.stage.yaml`/`compose.prod.yaml` 的覆盖，覆盖来自 `--env-file`**（与
`LINGXI_SCHEDULER_INTERVAL_SECONDS` 同一套既有写法）。worker-queue 的并发与生产
资源合同另有显式门禁，见下文：

| 服务 | stage（cpus / mem / pids） | 生产（cpus / mem / pids） | 依据 |
| --- | --- | --- | --- |
| `scheduler` | 0.5 / 512M / 256 | 1.0 / **512M** / 512 | 定时职责、无子进程扇出，量级参照架构设计其余轻量进程。`compose.prod.yaml` 的 `memory` 默认值是 `1G`，**上线由根 `.env.prod` 覆盖为 512M**，理由见下「三个常驻服务的加总核对」 |
| `gateway` | 0.5 / 512M / 256 | 1.0 / **512M** / 512 | 长连接接入 + 事件落库，无子进程扇出。`memory` 与 `scheduler` 同一处置：compose 默认 `1G`，**上线由根 `.env.prod` 覆盖为 512M** |
| `migrate` | 0.5 / 512M / 256 | 1.0 / 1G / 512 | 一次性 DDL 作业，允许留一点余量应对大表迁移 |
| `reauthorize` | 0.5 / 512M / 256 | 1.0 / 1G / 512 | 一次性运维作业，量级同 scheduler |
| `worker`（一次性回合） | 1.5 / 2G / 512 | 2.0 / 4G / 1024 | 单个 Agent SDK 回合：Claude CLI + 多个 MCP 子进程，但同一时刻只跑一个回合 |
| `worker-queue`（常驻队列消费者） | 1.5 / 2G / 512 | **1.5 / 2G / 512** | 产品负责人裁定机器与 stage 同型（Issue #496/#502） |

### 生产 worker-queue 合同（Issue #494/#496/#502，2026-08-31）

产品负责人已裁定生产机器型号与 stage 一致，生产 `worker-queue` 合同固定为 CPU
`1.5`、memory `2G`、pids `512`、tmpfs `256m`，执行并发固定为 `4`。这组值是
仓库可检查的部署合同，不是 E1 的容量上界推断；E1 只证明一次性 1/2/4/6/8 建连
共 `154/154` 成功，不能把 8 说成执行上界，也不触发扩阶或重跑。

生产的五个 worker-queue 值仍遵循 #494 的外部 env 边界：它们只写在目标机器未入库
的根 `deploy/.env.prod`，不在仓库创建或写入真实 `.env.prod`。`compose.prod.yaml`
对并发和四项资源使用 `${VAR:?}` 无默认值；漏配时 `docker compose config`/`up` 直接
失败，不能静默退回默认值。`scripts/ci/check_deploy_contract.py` 与
`scripts/ci/verify_compose_structure.sh` 会分别检查这种 fail-fast 形状及可渲染占位
值；提示语保持纯 ASCII 单行，避免 YAML 先于插值截断 `${...}`。

生产根 `.env.prod` 的 worker-queue 部分应包含以下五行（示例合同，不是要求把文件提交到
仓库）。**这五行不是根 `.env.prod` 的全部**——另有两行 `scheduler`/`gateway` 内存必须
同时写入，见下「三个常驻服务的加总核对」：

```dotenv
LINGXI_WORKER_MAX_CONCURRENCY=4
LINGXI_WORKER_QUEUE_CPU_LIMIT=1.5
LINGXI_WORKER_QUEUE_MEM_LIMIT=2G
LINGXI_WORKER_QUEUE_PIDS_LIMIT=512
LINGXI_WORKER_QUEUE_TMPFS_SIZE=256m
```

其中并发值同时由 worker 配置的直接构造校验、compose stage/prod 环境块和部署门禁
钉住；资源值由 prod 的 `${VAR:?}` 和 runbook/compose 渲染合同钉住。不要创建真实
`.env.prod` 或把凭据写入仓库。

| 变量 | 生产合同 |
| --- | --- |
| `LINGXI_WORKER_MAX_CONCURRENCY` | `4`；stage 与生产执行并发硬上限 |
| `LINGXI_WORKER_QUEUE_CPU_LIMIT` | `1.5` |
| `LINGXI_WORKER_QUEUE_MEM_LIMIT` | `2G` |
| `LINGXI_WORKER_QUEUE_PIDS_LIMIT` | `512` |
| `LINGXI_WORKER_QUEUE_TMPFS_SIZE` | `256m`；tmpfs 消耗宿主内存，不是磁盘配额 |

其余五个服务的生产档仍沿用上表数值：它们都是无子进程扇出的轻量进程，量级判断不依赖主机规格的精确值；`worker`（一次性 job）的 `/tmp` 也仍是基线的 256m——它跑完一个回合就退出、tmpfs 随容器销毁，没有 `worker-queue` 那种跨任务累积的增长（Issue #494 已核实，不单独立项）。

### `/tmp` 内存盘容量（Issue #494）

`worker`/`worker-queue` 的 `/tmp` 是 **256MB 内存盘**（其余四个服务 16m）。镜像把 `HOME` 指到 `/tmp`，Agent 会话转录写在 `$HOME/.claude/projects` 下——rc22 收尾批 S-12 浸泡实测：worker-queue 接负载约 **45 分钟就写满**（32 份转录、单份最大 15MB），清空后 33 分钟又长回 190MB。三条修复缺一不可：

1. **常规回收**：`apps/worker/session_cleanup.py` 的 `reclaim_session_transcripts` 按字节预算删最旧的（默认预算 128MiB、低水位 75%、保护最近 5 分钟写过的）。此前只有 `/new`、权限刷新等触发点排队才会删，**正常问数流程一次都不排**。运维旋钮见 `deploy/.env.example`「文件五」的 `LINGXI_WORKER_SESSION_DISK_*` / `LINGXI_WORKER_SESSION_RECLAIM_*`；预算设 `0` 显式关闭回收（保全取证现场用）。
2. **上限分环境显式**：值由 `LINGXI_WORKER_QUEUE_TMPFS_SIZE` 决定，stage 与生产均钉在 256m（这块盘吃宿主内存）。
3. **健康检查看得见这块盘**：`lingxi.apps.healthcheck` 增加可用空间判定，低于总量 10% 判红。修复前它一路报绿——探针写的是十几字节的活性文件，覆盖写不需要新页，所以"用户在失败、监控显示 healthy"可以无限期持续。时延口径见 `deploy/监控告警.md`「五、时延估算」的 C 类。

### 三个常驻服务的加总核对

**stage 与生产机器采用同一型号（t3.medium，2 vCPU / 3.74GiB）**，因此两边的加总
核对用同一台主机的容量来算。**只有三个常驻服务会同时在跑**（`worker`/`migrate`/
`reauthorize` 是一次性作业，通常不与常驻服务的峰值重叠）：

| `memory` 档 | `scheduler` | `gateway` | `worker-queue` | 三者加总 | 主机 3.74GiB |
| --- | --- | --- | --- | --- | --- |
| stage 实配 | 512M | 512M | 2G | **3G** | 余约 700MB ✅ |
| **生产上线实配** | **512M** | **512M** | 2G | **3G** | 余约 700MB ✅ |
| （对照）`compose.prod.yaml` 未覆盖时的默认值 | 1G | 1G | 2G | 4G | 会超出主机 ❌ |

**第三行是对照，不是上线值。** `compose.prod.yaml` 里 `scheduler`/`gateway` 的
`memory` 写成 `${LINGXI_SCHEDULER_MEM_LIMIT:-1G}` / `${LINGXI_GATEWAY_MEM_LIMIT:-1G}`
——`1G` 只是**不覆盖时的默认值**，而生产从来不裸跑默认值：上线时根 `deploy/.env.prod`
把这两项钉成与 stage 同值 `512M`（元守护 W0-3 盘点结论，执行步骤见 [Issue #519](https://github.com/Moshuiwang/lingxi/issues/519) S-3.5），
加总因此是 **3G ≤ 3.74GiB**。这与 D5「生产与 stage 同型」是同一件事，不是新的资源
合同变更，也**不需要改 `compose.prod.yaml` 的默认值**。所以生产根 `.env.prod` 除了
上面那五行 worker-queue 值，还须包含：

```dotenv
LINGXI_SCHEDULER_MEM_LIMIT=512M
LINGXI_GATEWAY_MEM_LIMIT=512M
```

**漏配这两行的后果是静默的**：它们是 `:-` 有默认值形态（不是 worker-queue 那种
`${VAR:?}` fail-fast），漏了不会报错，只会安静地退回 `1G`、把加总推到 4G。部署前
用渲染回读确认，不要只看 env 文件：
`docker compose --env-file deploy/.env.prod -f deploy/compose.yaml -f deploy/compose.prod.yaml config scheduler gateway`。

**怎么读这张表**：`deploy.resources.limits` 是**上界，不是预留**，三者同时顶格才会
真的冲突。CPU 超订（生产 1.0 + 1.0 + 1.5 = 3.5 vCPU 对 2 vCPU）只会限流变慢，内存
超订撞上去才是被 OOM kill——所以需要逐档核对的是 `memory` 这几行。三个常驻服务里
只有 `worker-queue` 会长期贴近自己的上限（它单独一个就吃掉 2G，占主机一半以上），
`scheduler`/`gateway` 都没有子进程扇出（依据见上表「依据」列）。另需知道：stage 浸泡
实测的宿主可用内存是 1.6~1.7GiB、swap 已用 261MiB，实际余量比"3.74GiB 减去加总"更紧，
因此上线首周仍应观察三者的实测常驻占用；真要腾地方，先动 `scheduler`/`gateway`，
不放松 `worker-queue`。

`worker-queue` 自己的 2G 够不够用，推导见[架构设计「九、容量与资源」](../docs/技术设计/架构设计.md)
（(2048 MiB − 256 MiB tmpfs) ÷ 并发 4 = 每回合 448 MiB）。Agent SDK 一个回合会拉起
Claude CLI 与多个 MCP 子进程，`pids` 撞顶的失败形态是进程被直接 kill，因此执行并发
上限与这组资源值必须一起改、不能单改一边。

**`worker-queue` 的内存限额与并发上限必须同步设，两者是同一个约束的两半**：
`LINGXI_WORKER_MAX_CONCURRENCY`（`apps/worker/config.py`）决定一个容器同一时刻
最多跑几个 Agent SDK 回合，当前默认与硬上限均为 **4**。它不是 E1 建连探针的
推导结果；4 是产品负责人对当前 stage/生产资源合同的明确执行配置。

| 档位 | `memory` 限额 | 并发上界建议 | 推导 |
| --- | --- | --- | --- |
| stage | 2G | **4（硬上限）** | 产品执行配置；不能因 E1 的 8 建连成功而扩阶 |
| 生产 | 2G | **4（硬上限）** | 与 stage 同型机器及同一 worker-queue 资源合同 |

**因此：任何 worker-queue 部署都必须保留 `LINGXI_WORKER_MAX_CONCURRENCY=4`**。
代码、stage compose 与 prod 外部 env 门共同拒绝高于 4 的值；E1 的 8 只是一轮连接
探针结果，不是执行上界或后续扩阶许可。

**改其他资源数值不需要改 compose 文件本身**：全部资源覆盖变量都在
`deploy/.env.example`「文件一」小节登记为可选覆盖，默认值已经内置在
`compose.stage.yaml`/`compose.prod.yaml` 里，只有需要单独调整某个服务时才需要
在 `deploy/.env.stage`/`deploy/.env.prod` 里显式设置对应变量。

**结构渲染核对**（不需要真实镜像，`docker compose config` 只做插值与合并）：

```bash
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml config | grep -A4 'deploy:'
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml config | grep -A4 'deploy:'
```

**门禁只核对结构**（`scripts/ci/check_deploy_contract.py` 的
`check_resource_limits`）：六个服务是否都声明了 `cpus`/`memory`/`pids` 三项键，
不核对具体数值——数值对错是容量判断，不是机械可判定的对错，门禁只保证"没有
任何服务被漏掉、漏配了限制"。

## 本地验证

```bash
scripts/ci/build_image.sh scheduler                # 构建单个镜像（scheduler|gateway|worker|migrate）
scripts/ci/verify_image_contract.sh <四个镜像引用>  # 镜像契约逐条核对（scheduler worker migrate gateway）
scripts/ci/verify_compose_structure.sh             # stage ↔ 生产结构对照
scripts/ci/verify_old_image_new_schema.sh          # V-部署-05：旧镜像在新库上启动
scripts/ci/verify_old_image_new_schema.sh --destructive   # 同上的变异对照，应判红
```

本机没有 buildx 也能跑：只用 plain `docker build` 支持的语法，不引入 BuildKit 专有语法或缓存后端。
