# 生产部署 Runbook（biplus-prod / Bot-Prod）

> **定位声明**：本文件是未来 `biplus-prod` 生产部署的操作正文，写给到时执行部署的人照做；**它本身不构成执行授权**。任何一次真实生产执行（首次部署、后续升级、回滚、恢复演练）都必须先有一个独立的 `[ops]` Issue 完成开工前事实确认与授权，本文件只提供步骤、判据和边界，不替代那次授权。`[ops]` Issue 的产生方式（直接立项，或作为某个生产部署 Trace 的输出）按 [Issue #369](https://github.com/Moshuiwang/lingxi/issues/369) 的「建议去向」处理，本文件不预设具体路径。
>
> 本 Trace（#373 H2 批 S-H2-4）只交付这份 runbook 与配套决策记录，不执行任何生产操作，也不接触 `biplus-prod` 或 `Bot-Prod`。
>
> **2026-09-02 起本文件已有一次真实执行记录**：首次生产部署当日与正文不一致的地方逐条登记在「[十一、实录偏差（2026-09-02 首发）](#十一实录偏差2026-09-02-首发)」。**下一次执行前先读那一节**——其中若干条（判据形态、`runtime-config` 卷内容、内测名单写两份）不照做会导致静默失败。
>
> **2026-09-04 第二次真实执行（rc25 升级到 `20260903-d67f772a3a6f`，git tag `v2.0.1`，Trace [#544](https://github.com/Moshuiwang/lingxi/issues/544)）的勘误已直接改进正文各节**，不另立实录节：步 0 的 `.git/index` 属主检查（§2.0）、digest 回读必须用钉住形态（§三 / §六）、监控与日志单元的实际安装终态（§10.1）、预开通的名单投放只剩 stdin 一条路、内测白名单前置、专用授权令牌间隔、MCP 就绪节奏（§十二）。每一条都是 stage 或生产真机实测，出处见 #544 块 Z 评论。

## 一、前置阅读

执行部署前，先确认以下文件均为当前生效版本，出现矛盾以更晚更新的为准：

- [`deploy/README.md`](README.md)：Compose 编排本身的操作正文（准备、preflight、安装、回滚、恢复入口的逐字命令均以它为准，本文件不重复照抄，只在它之上补生产专属的判据与纪律）。
- [`deploy/验收前部署配置清单.md`](验收前部署配置清单.md)：验收前要配齐什么、缺一项会死在哪里。
- [架构设计「八、部署与发布」](../docs/技术设计/架构设计.md#八部署与发布)与[「九、容量与资源」](../docs/技术设计/架构设计.md#九容量与资源)：部署方案定稿与容量选型依据。
- [验证与门禁「十二、部署对代码的约束」](../docs/技术设计/验证与门禁.md#十二部署对代码的约束)与[验收矩阵「部署与迁移」分册](../docs/技术设计/验收矩阵-部署与迁移.md#三部署与迁移断言)：`V-部署-*` 断言的认领状态与证据链接。
- [决策：生产 secret 注入与凭据隔离](../docs/决策记录/2026-08-28-生产secret注入与凭据隔离.md)：本文件「九、secret 注入与凭据隔离」一节的裁定依据。
- [Issue #135](https://github.com/Moshuiwang/lingxi/issues/135)：生产凭据类别与百炼凭据复用的产品负责人澄清。
- [Issue #369](https://github.com/Moshuiwang/lingxi/issues/369)：首次生产部署已知缺口台账；本文件覆盖其中第 7、8、12 条以及追加评论第 13-15 条对应事项，其余条目（备份演练、资源限制、监控告警落地、L5/L6 启动条件等）仍是独立未闭合缺口，本文件不重复承接。

## 二、首次部署步骤

以下步骤是对 `deploy/README.md` 既有机制的生产执行顺序汇总，逐字命令以该文件为准；这里只标注生产专属的差异点。

### 2.0 步 0：把生产机仓库工作副本更新到候选（升级必做；rc25 审核③ ❌B 补入）

本文件与 `deploy/README.md` 的**每一条** compose 命令都以相对路径引用生产机仓库工作副本里的 `deploy/compose*.yaml`（工作副本路径：`/home/bi-ai-deploy/projects/lingxi`）。**只改 `.env.prod` 的 tag/digest 就 `up -d` 是一个不报错的错法**：命令全部成功、三容器全 healthy、观察期全绿，但 compose 文件还是旧的——本批新增的 `LINGXI_WORKER_WORKSPACE`（W-1 令牌隔离）、`LINGXI_DEPLOY_ENVIRONMENT`（生产判据兜底）等 `environment:` 行全部缺席，修复零报错地不生效；单元文件、运维脚本的更新同样依赖这一步。CI 门禁只看仓库文件，对「生产机没更新」零报错。2026-09-03 只读实测：生产工作副本 HEAD 停在 `eea6b1215cb9…`（rc24 时代提交），这条缺口是既成事实，不是假设。

```bash
# ① 更新到本批候选（<候选 SHA> = 本批合入 main 的提交，其前 12 位就是镜像 tag 的 sha 段）
git -C /home/bi-ai-deploy/projects/lingxi fetch origin
git -C /home/bi-ai-deploy/projects/lingxi checkout --detach <候选 SHA>

# ② 回读：输出必须逐字等于候选 SHA，不等不得继续
git -C /home/bi-ai-deploy/projects/lingxi rev-parse HEAD

# ③ 渲染回读（必须带 --no-env-resolution，硬前提见 §2.1）：本批 compose 侧修复必须出现
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  config --no-env-resolution worker-queue \
  | grep -E 'LINGXI_WORKER_WORKSPACE|LINGXI_DEPLOY_ENVIRONMENT'
# 期望恰好两行（键按字母序，值可能带引号）：
#   LINGXI_DEPLOY_ENVIRONMENT: prod
#   LINGXI_WORKER_WORKSPACE: /tmp/lingxi-workspace
```

checkout 之前先确认工作副本干净（`git -C /home/bi-ai-deploy/projects/lingxi status --porcelain` 输出为空；有本地改动先由产品负责人裁定处置，不得 `checkout -f` 抹掉）。`deploy/.env.prod*` 七个凭据文件全部匹配 `.gitignore` 的 `.env.*` 规则、不入库也不受 checkout 影响——这一步**不触碰任何凭据文件**。

**步 0 检查项：`.git/index` 的属主（2026-09-04 生产实测补入）**。工作副本曾以 root 跑过一次 git，`.git/index` 变成了 `root:root`，部署用户的 `fetch` 与 `checkout` 一律 `Permission denied`，连 `status` 都读不到。修法**不需要 root**：以部署用户 `rm .git/index && git reset --quiet`——index 只是工作树的缓存，删掉后 `reset` 从 HEAD 重建，**树零改动**；随后 `status --porcelain` 回读为空才继续步 ①。先查再动：

```bash
stat -c '%U:%G %n' /home/bi-ai-deploy/projects/lingxi/.git/index   # 期望 bi-ai-deploy:bi-ai-deploy
```

### 2.1 准备七个 env 文件

按 [`deploy/README.md`「准备」](README.md#准备)为 `.env.prod`、`.env.prod.scheduler`、`.env.prod.gateway`、`.env.prod.worker`、`.env.prod.worker-queue`、`.env.prod.migrate`、`.env.prod.reauthorize` 七个文件写入生产凭据。生产凭据集与研发/Stage 凭据集完全隔离，任何一个值都不得与 `.env.stage.*` 系列重复（百炼模型端点凭据除外，见本文件「九」）；不得把研发环境的任何文件复制改名后当作生产文件使用。

其中**数据库连接串不手工编辑**：生产数据库为 Supabase `lingxi-prod`（eu-west-1，Issue #411），凭据事实源是 `biplus-prod` 上的私有文件 `/home/bi-ai-deploy/.config/lingxi/supabase-prod.env`（0600，已就位并验证过登录），用同步脚本按服务写入——scheduler/gateway/worker-queue/reauthorize 得运行 DSN、migrate 得迁移 DSN、worker 零数据库凭据：

```bash
scripts/ops/sync_db_env_from_credentials.sh \
  /home/bi-ai-deploy/.config/lingxi/supabase-prod.env deploy .env.prod
```

机制详见 [`deploy/README.md`「数据库凭据源」](README.md#数据库凭据源supabase-私有凭据文件issue-411)（含 session 模式连接约束）。**截至 2026-08-29 生产尚未切换/部署**：本节只登记同构加载方式，实际执行在生产发布的独立 ops 卡内。

**资源与并发（Issue #494/#496/#502）**：生产机器型号与 stage 一致。在目标机外部根
`deploy/.env.prod`（不入库、不写入本仓库）核对并写入以下**七行**：

```dotenv
LINGXI_WORKER_MAX_CONCURRENCY=4
LINGXI_WORKER_QUEUE_CPU_LIMIT=1.5
LINGXI_WORKER_QUEUE_MEM_LIMIT=2G
LINGXI_WORKER_QUEUE_PIDS_LIMIT=512
LINGXI_WORKER_QUEUE_TMPFS_SIZE=256m
LINGXI_SCHEDULER_MEM_LIMIT=512M
LINGXI_GATEWAY_MEM_LIMIT=512M
```

**七行全部是硬前置：任一行缺失，`docker compose config`/`up` 直接失败，服务起不来。**

| 行 | `compose.prod.yaml` 形态 | 漏配会怎样 |
| --- | --- | --- |
| 前五行（worker-queue 并发与资源） | `${VAR:?}` **无默认值** | `docker compose config`/`up` **直接失败**，起不来 |
| 后两行（`scheduler`/`gateway` 内存） | `${VAR:?}` **无默认值**（rc25 S-3b 报告 R6-D4 起；`compose.prod.yaml:42`/`:66`） | 同上，**直接失败**并指名变量：`error while interpolating services.scheduler.deploy.resources.limits.memory: required variable LINGXI_SCHEDULER_MEM_LIMIT is missing a value` |

> **这两行的语气自 rc25 S-3b 起从「要求」升为「硬前置」。** 此前它们是
> `${VAR:-1G}` 有默认值形态：漏配不报错，只会静默退回 `1G`，把三个常驻服务的加总
> 从 3G 推到 4G、越过主机 3.74GiB，而 `docker compose config` 一声不响——那时这张表
> 靠的是执行纪律，不是闸门。现在漏配起不来，本节的渲染回读从「唯一防线」降为
> **值的复核**（它仍然要做：无默认值只能挡住「没写」，挡不住「写错值」）。

后两行把 `scheduler`/`gateway` 钉成与 stage 同值 `512M`，三个常驻服务加总
512M + 512M + 2G = **3G ≤ 3.74GiB**；这与「生产与 stage 同型」是同一件事。加总核对与推导见
[`deploy/README.md`「三个常驻服务的加总核对」](README.md#三个常驻服务的加总核对)，此处不复述。

**一次性 `worker`（`job` profile）的内存默认值同批由 `4G` 降到 `2G`**（报告 P3）：
`4G` 大于宿主机可用内存（约 3.9GiB），这条"上限"在默认取值下永远不会生效，比没有
上限更糟。它**保留 `:-` 默认值形态**而不是改成 `${VAR:?}`——compose 的插值对整份文件
求值、不分 profile，改成无默认值会让每一次 `mvp` 的 `up -d` 都多要求一个变量；要不要
把它也收紧成显式声明属部署配置决定，需连本 runbook 与 `.env.prod` 一起改，**尚未裁定**。
不需要为它在 `.env.prod` 加行。

部署前用以下两条只读命令确认渲染后的**取值**与上表一致（不创建或修改真实生产
文件）。**漏没漏现在渲染会自己报错，但值写错了只有回读看得出来**：

**硬前提（rc25 审核③ ❌A；先核对、后执行，不核对不得跑）**：`docker compose config`
**默认会把各服务 `env_file:` 的文件内容展开内联进输出**（仓库自证：
`scripts/ci/verify_compose_structure.sh` 头部注释明确记录了这一行为）——在生产上不带
开关照跑这条「只读」命令，等于把 `.env.prod.worker-queue`/`.env.prod.scheduler`/
`.env.prod.gateway` 里的数据库 DSN（含口令）、`LINGXI_MCP_TOKEN_ENCRYPT_KEY`、飞书
app secret 全部打进终端与会话记录。因此下面两条命令**必须带 `--no-env-resolution`**，
且执行前先用这条只读命令确认本机 compose 支持该开关（2026-09-03 已在生产实测一次返回
`1`，窗口内执行前仍复核）：

```bash
docker compose config --help | grep -c -- '--no-env-resolution'   # 期望输出 1
```

**输出不是 `1` 时，下面两条 `config` 命令一条都不得执行**——没有「先跑了再说」的选项。
替代做法：跳过渲染回读，只做不展开 env 的核对（`docker compose … ps`、
`docker image inspect`，以及 `up -d` 之后
`docker inspect --format '{{.HostConfig.Memory}}' <容器>` 事后核值）。资源限值来自
`--env-file` 根文件的插值，不受 `--no-env-resolution` 影响，带开关的回读结论与不带时
完全相同。

```bash
# ① worker-queue：并发与四项资源
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  config --no-env-resolution worker-queue

# ② scheduler / gateway：确认 memory 渲染值（漏配已由 ${VAR:?} 直接报错；判读按
#    11.3 第 1 条用字节数：512M → "536870912"，出现 "1073741824" 就是错退回 1G）
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  config --no-env-resolution scheduler gateway
```

### 2.2 部署前置检查（preflight）

逐条通过 [`deploy/README.md`「部署前置检查」](README.md)：七个文件均 `0600` 且属主为部署用户。**「部署机已登录 GHCR」这一项当前不适用**——四个镜像包公开、两台机器都靠匿名拉取（见 README「主机读取身份」的现状与已知边界）；包改为 private 之后它才重新成立。第一项不符不得执行下一步。

**外加一项就绪检查（[Issue #499](https://github.com/Moshuiwang/lingxi/issues/499)，rc23；**rc25 起这个变量管的是哪条路已经变了**，见下表）**：确认 `.env.prod.gateway` 里 `LINGXI_DOCX_MARKDOWN_CONVERT` 的**实际取值**与本仓库当前结论一致——

```bash
# 只列这一行配置本身，不打印文件其余内容；该变量不是凭据
grep -n '^LINGXI_DOCX_MARKDOWN_CONVERT' deploy/.env.prod.gateway || echo '未设置（= 开启，代码默认值）'
```

判据（三选一，都是合法结论，但必须**是有意选的**，不是漏配的结果）：

| 实际取值 | 生效状态 | 用户可见后果 |
|---|---|---|
| 未设置 或 `1` | 正文交付走飞书**服务端一次建档写全文**（rc25 S-7c 起；[#467](https://github.com/Moshuiwang/lingxi/issues/467) 起的代码默认值，翻转前唯一的开启值 `1` 继续解析成开启） | 正文由飞书排版，标题 / 列表 / 表格都能一次写出；命中两道前置守卫（正文超 20 000 字符、标题含尖括号）时退回纯文本段落路径并**如实告知格式已简化**；飞书服务端自陈简化时文档照常交付，用另一条文案告知、**不担保内容没有删减** |
| `0` | 一次建档**关闭**（止损闸） | 正文一律走纯文本段落路径，没有排版，也不会出现降级提示 |
| 其余任何非空值 | **启动即失败** | gateway 起不来——错配不是未配 |

不符合上表任一行，或取值与本次发布意图不一致时，先改配置再继续，不得带着未确认的取值起服务。

**rc25 换机制带来的两点，核这一项时一并知道**：① **这个开关管的路变了**——它从「要不要调飞书的客户端 markdown→blocks 转换接口」变成「要不要走服务端一次建档」；保留它是因为 `docs_ai` 接口在飞书开放平台没有公开文档页、限流与长度上限官方无契约，留一个不改代码、不重建镜像就能退回纯段落路径的止损闸（生产排版出问题时把它设成 `0` 重启 gateway 即可）。② **不再需要 `docx:document.block:convert` 这条权限**——一次建档用应用身份令牌直接可用（stage 受控探针实测，无 scope 类错误码），代码侧已不调用那个端点。**但本次发布不做后台回收**：飞书开发者后台把这条权限收掉是一次独立的运维动作，建议**发版并观察一轮之后再收**（回收失败也不影响交付），本 runbook 不把它列进发布步骤。

### 2.3 迁移

先完成「三、镜像 digest 固定」的**①显式拉取与②逐份比对**（rc25 起提前到迁移之前），
全部一致后再跑迁移：

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --profile job run --rm migrate
```

**硬规则（rc25 补入）：`run --rm migrate` 退出码非 0 → 当场停止，不得带伤 `up -d`。**
本批迁移 0085（`app_user` 部分唯一索引）、0086（`publish_outbox` 两个可空列＋回填）、
0087（`app_user` 三个可空列）**全部是加法且可重跑**：`migrations/alembic/env.py` 的
`transaction_per_migration=True` 让任一条失败只回滚该条、版本停在前一条，**失败不需要
downgrade**，处置完原因后重跑原命令即可。已知失败形态：0085 撞上重复邮箱时
`CREATE UNIQUE INDEX` 报错、版本停在 0084——按迁移文件与 `migrations/README.md` 的
说明处置重复数据后重跑，**不得改成非唯一索引绕过**。

```bash
# 迁移后回读版本头：migrate 前应为 0084_management_card_state_cas (head)，
# 之后必须是 0087_preprovision_seams (head)，不符不得起服务
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --profile job run --rm migrate current
```

### 2.3.5 起服务前的两道前置（对抗审查 R6-D7 补入）

**这两道前置都在 `up -d` 之前，缺一不可。** `--profile mvp up -d` 一旦执行，scheduler 立即开始消费 `publish_outbox` 往正式权限表写、gateway 立即接管 Bot-Prod 长连接——两件事都是对外部系统的真实写入，事后无法撤销。本节此前只有「2.3 迁移 → 2.4 起服务」，这两道前置一条也没写；2026-09-02 首发是靠 Trace [#521](https://github.com/Moshuiwang/lingxi/issues/521) 的执行合同（`up -d` 只能在 G2 之后）兜住的，仓库正文没有可复现记录。

**① 外部写入方停写门（首发记为 G2）**——只在「本产品之外还有别的写入方在写同一张正式权限表」时适用（首发的旧系统即是）：

1. 旧系统**全部**写路径逐条停止并取得回执（定时任务、机器人、API、文件发送四类都要点到）；
2. 跨一个**最长旧写入周期**之后复读正式表，逐行不变。首发的窗口取「全部能写表的写入方下次触发时刻」的最大值再 +5 分钟，实测跨 15 分钟、跨两个旧 timer 整点边界；
3. 判据是行数 / distinct / 重复 / 空键 / 空令牌 / 键与邮箱不等六项计数与停写前快照逐字相同，且整表摘要 `rows_digest` 相同。

**这一门不过就不许 `up -d`**：两个写入方并存即双写，而正式权限表是外部表、写错不可回滚。

**② 凭据与运行时配置落位（首发记为 E8）**——`up -d` 之前必须完成：

- **`runtime-config` 卷内容自带**（详见 11.2）：`system_prompt.md` 到位并设 `LINGXI_WORKER_SYSTEM_PROMPT_FILE`。**没有安全的「留空」选项**——文件缺失时 worker 静默降级为无提示词执行，三服务照样全 `healthy`、观察期照样全绿，只有答案质量与验证过的不是一回事；
- **首次授权走通**（详见 11.5）：`reauthorize` 完成、专用授权凭据落到凭据卷。凭据不到位时 scheduler 起来也同步不了花名册，首批用户一个都开不通。

### 2.3.6 升级窗口注意事项（rc25 补入：挑静默窗口，先查在途）

**① 重启/替换 gateway 之前先确认没有在途任务与在途文档交付。** 只读计数（DSN 从
`/home/bi-ai-deploy/.config/lingxi/supabase-prod.env` 加载进环境变量后引用，值不回显、
不粘贴进命令行参数或聊天记录）：

```bash
psql "$LINGXI_POSTGRES_DSN" -Atc "SELECT (SELECT count(*) FROM task WHERE status='running'), (SELECT count(*) FROM task WHERE status='awaiting_delivery'), (SELECT count(*) FROM task_document_delivery_request WHERE status='processing');"
```

期望 `0|0|0`；任一项非零就等一轮再查（在途文档投递的认领回收周期为 180 秒量级），
非零不 `up -d`。

**② 在途 docs_ai 建档若仍被重启打断：不需要任何处置动作，但绝不重试建档。** 一次建档
请求已发出、响应没等到时，飞书服务端可能已经把整篇文档建了出来；重启后该投递行由
180 秒回收机制退回队列自行续投，**用户仍只收到一份文档、不会重复交付**。代价是机器人
云空间可能多出一篇**带全文**的孤儿文档（从未授权给任何人、不在九十天擦除范围）——这是
**留存面的运维卫生**，不是用户可见故障：不要人工重试建档，不要为找回孤儿去翻机器人
云空间，如实登记发生过一次打断即可。规避办法就是 ①：挑静默窗口执行整个升级。

### 2.4 起服务（`--profile mvp`）

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --profile mvp up -d
```

`mvp` profile 同时拉起 `scheduler`、`gateway`、常驻 `worker-queue` 三个常驻服务，语义与 `deploy/README.md` 中 Stage 使用的 profile 完全一致，只替换了 `--env-file` 与 compose 覆盖文件。

**三条命令形态纪律（2026-09-02 首发实测，均属「不报错的错法」，详见 11.3）**：

1. **`--profile mvp` 不能省**：裸 `up -d` 只起 `scheduler`，`gateway` 与 `worker-queue` 都在 `mvp` profile 里；漏掉它 Bot-Prod 长连接根本不接，而命令是成功返回的。
2. **`-f compose.yaml -f compose.prod.yaml` 两个都不能省**，且对**每一条** compose 命令都成立（含 `migrate`、`reauthorize` 这类临时作业与单服务操作）：各服务的 `env_file` **只声明在覆盖文件里**，只带 `compose.yaml` 时命令照样能跑，跑出来的却是一个没有任何凭据与配置的容器。
3. **`run --rm <服务> <参数…>` 的参数会替换掉 compose 里声明的 `command`**：需要传参时必须把入口写全，例如 `… run --rm reauthorize python -m lingxi.apps.reauthorize <参数…>`；直接在服务名后追加参数会把入口一起丢掉。

### 2.5 健康回读

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml ps
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml logs scheduler gateway worker-queue
```

判定标准见本文件「七、观察期」。

## 三、镜像 digest 固定

`deploy/compose.yaml` 的六个服务镜像引用固定为
`${LINGXI_IMAGE_REGISTRY:?}/lingxi-<服务>:${LINGXI_IMAGE_TAG:?}${LINGXI_<服务组>_IMAGE_DIGEST:-}`
——`Trace #373 H2` 批 P1-1 修复：每个服务的 `image:` 行在既有共用 `LINGXI_IMAGE_TAG` 之后
追加了一个**可选**的 digest 后缀变量，默认空，不设时渲染结果与本节修订前逐字节一致。
按镜像分**四个**变量（六个服务只对应四份镜像，`scheduler`/`reauthorize` 共用一个镜像，
`worker`/`worker-queue` 共用一个镜像）：

| 变量 | 覆盖的 compose service |
| --- | --- |
| `LINGXI_SCHEDULER_IMAGE_DIGEST` | `scheduler`、`reauthorize`（同一镜像） |
| `LINGXI_GATEWAY_IMAGE_DIGEST` | `gateway` |
| `LINGXI_WORKER_IMAGE_DIGEST` | `worker`、`worker-queue`（同一镜像） |
| `LINGXI_MIGRATE_IMAGE_DIGEST` | `migrate` |

**这条修复的由来（本节修订前的版本有一个未闭合的执行缺口，见 [Issue #369](https://github.com/Moshuiwang/lingxi/issues/369) 第 7 条）**：上一版本节让运维把单个 digest 直接写进共用的 `LINGXI_IMAGE_TAG`（`LINGXI_IMAGE_TAG=<tag>@sha256:<digest>`），但四份镜像各有独立 digest、compose 里却只暴露一个共用的 tag 变量——固定其中一个镜像的 digest 之后，另外三份镜像的引用会解析成"这个 tag 对应的是另一份镜像的 digest"，实际拉取时报 `manifest unknown`。改成按镜像分四个变量之后，`LINGXI_IMAGE_TAG` 继续只承载对人可读的 `<日期>-<commit sha 前 12 位>`，四个 digest 变量各自独立锁定各自的镜像。

值的形状是 `@sha256:<digest>`（**注意前导 `@`**）——Docker 镜像引用语法允许在 `tag` 之后再接 `@digest`（`tag@digest` 是合法镜像引用，digest 优先生效，tag 仍供人读），因此不需要改动 `LINGXI_IMAGE_TAG` 本身，也不需要改动 compose 文件结构。

**把 tag 解析为 digest（二选一，不需要本机预先 `docker pull`；对四份镜像各做一次）**：

```bash
# 方式一：直接查询远端 registry（推荐，不占本机磁盘）
docker buildx imagetools inspect ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
docker buildx imagetools inspect ghcr.io/moshuiwang/lingxi-gateway:20260806-7a9bcf3fac4a
docker buildx imagetools inspect ghcr.io/moshuiwang/lingxi-worker:20260806-7a9bcf3fac4a
docker buildx imagetools inspect ghcr.io/moshuiwang/lingxi-migrate:20260806-7a9bcf3fac4a
# 每条输出的 Digest: sha256:... 即为该 tag 当前指向的 manifest digest

# 方式二：已经 pull 到本机时，从本地元数据回读（以 scheduler 为例，其余三份同构）
docker pull ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/moshuiwang/lingxi-scheduler:20260806-7a9bcf3fac4a
# 输出形如 ghcr.io/moshuiwang/lingxi-scheduler@sha256:...，取 @ 之后的部分
```

写入 `.env.prod`（四行，覆盖六个服务；**前提是四份镜像确实来自同一次 `Epic Full / image` 构建**——`deploy/README.md`「安装与升级」已保证生产只拉不建，不会出现分别对应不同批次的情况）：

```bash
LINGXI_SCHEDULER_IMAGE_DIGEST=@sha256:<scheduler 镜像本批次 digest>
LINGXI_GATEWAY_IMAGE_DIGEST=@sha256:<gateway 镜像本批次 digest>
LINGXI_WORKER_IMAGE_DIGEST=@sha256:<worker 镜像本批次 digest>
LINGXI_MIGRATE_IMAGE_DIGEST=@sha256:<migrate 镜像本批次 digest>
```

**部署时核对（2026-09-03 修订，rc25 审核③：核对从事后判据提前为事前闸）**：此前这一步
只能在 `up -d` 之后做——镜像到起服务才被拉下来，「digest 不符不得判成功」因此只能事后
判。现改为**「①显式拉取 → ②逐份比对 → 才 migrate / up -d」**，顺序不得颠倒；11.3
第 2 条记录的首发临时办法（用 `config` 看渲染引用）不再需要。

```bash
# ① 显式拉取本批全部镜像（job + mvp 两个 profile 一起给即覆盖四份镜像；
#    .env.prod 已写四个 digest 变量时拉的就是钉住的 digest，值写错会当场报错）
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  --profile job --profile mvp pull

# ② 逐份镜像回读 digest（scheduler / gateway / worker / migrate 同构，各一次）。
#    引用必须写成钉住形态 <name>:<tag>@<digest>（digest 取 .env.prod 里对应变量的值）：
#    digest 钉住拉取后本机只有 tag@digest 这一个引用、裸 tag 引用不存在，
#    `docker image inspect <name>:<tag>` 会空输出（2026-09-04 stage 与生产均实测）
docker image inspect --format='{{index .RepoDigests 0}}' \
  ghcr.io/moshuiwang/lingxi-scheduler:<本批 tag>@sha256:<scheduler 本批 digest>
```

② 的输出形如 `ghcr.io/moshuiwang/lingxi-scheduler@sha256:<64 位十六进制>`：从 `@` 起
（含 `@sha256:` 前缀）与 `.env.prod` 里对应的 `LINGXI_<服务组>_IMAGE_DIGEST` 值**逐字
相同**才算通过。① 失败或 ② 任一份不一致 → **停止，不得进入 §2.3 迁移与 §2.4 起服务**；
不一致说明本机存在同 tag 不同 digest 的缓存污染，先 `docker image prune` 或显式
`docker pull` 该 digest，重新走 ①②，四份全部一致后才继续。tag→digest 的解析（往
`.env.prod` 写四行时用）仍按上文「把 tag 解析为 digest」二选一执行。

> **为什么不用 `docker compose … images`**（2026-09-02 首发实测，实录见 11.3 第 2 条）：那条命令列的是**本项目当前存在的容器**，`migrate` 这类 `--profile job run --rm` 的一次性作业跑完不留容器，输出为空，`migrate` 这一步因此核不到任何东西。`docker image inspect` 读的是本机镜像元数据，不依赖容器是否存在，`up -d` 前后都能用。

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
# 1. 把 deploy/.env.prod 的 LINGXI_IMAGE_TAG 改回上一个候选的 tag，并把四个
#    LINGXI_<服务组>_IMAGE_DIGEST 变量改回该批次各自的 digest（见「三、镜像
#    digest 固定」的变量对照表——四个变量、不是把 digest 塞进 LINGXI_IMAGE_TAG）

# 2. 执行 up -d，compose 按「四、单实例纪律」的替换流程逐个服务先停旧再起新
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d

# 3. 回读确认实际运行 digest 与目标一致（见「三、镜像 digest 固定」；四份镜像各一次；
#    钉住拉取的镜像没有裸 tag 引用，必须写 tag@digest 形态）
docker image inspect --format='{{index .RepoDigests 0}}' \
  ghcr.io/moshuiwang/lingxi-scheduler:<回滚目标 tag>@sha256:<回滚目标 digest>
```

**回滚不触碰数据库与持久卷**：不执行 `down` 移除 volumes，不执行任何迁移降级操作。这一前提成立的条件是迁移遵守「先加后删」——破坏性变更必须拆成两次发布，否则回滚就从「切镜像重启」变成「恢复数据库备份」；该前提由 `V-部署-05`（[验收矩阵「部署与迁移」分册](../docs/技术设计/验收矩阵-部署与迁移.md#三部署与迁移断言)）在每次 CI 上机械核对，2026-08-23 已有 `biai-stage` 真实回滚演练证据（整队切回旧候选、三容器在当前库结构上全部 healthy）。

> **已知边界（rc25 登记，本批不修）**：rc25 这一批（迁移 0085-0087）的回滚安全性是
> **静态论证**——三条迁移全为加法、旧镜像 `20260902-5500bfb725aa` 不读不写新列与新索引，
> 据此判定「切回旧 tag 不碰库」成立；**未针对本批做真实回滚演练**。且 `V-部署-05` 的
> CI 实测（`scripts/ci/verify_old_image_new_schema.sh`）用的是运行时**合成**的加列迁移、
> 不是本批真实的 0085-0087，CI 绿不构成对本批的实测覆盖。

## 七、观察期

> **时延数值的单一事实源是 [`deploy/监控告警.md`「五、时延估算：从故障发生到告警送达」](监控告警.md#五时延估算从故障发生到告警送达单一事实源)**——公式（A 类/B 类）、三服务的 `interval`/`timeout`/`retries`/活性阈值现值与逐项算出的最坏时延都在那张表，本文件不维护第二份数字。改健康检查参数或活性阈值时，只需要更新那一份表，这里的判据不受影响（判据只引用"最坏时延"这个结论，不复制推导过程）。

**部署后观察窗口**：至少持续观察 **15 分钟**（覆盖三服务里最坏的 B 类端到端时延——scheduler 约 6 分钟——的两倍以上，留出多个健康检查周期的余量），期间：

- 每隔 1-2 分钟 `docker compose ... ps` 一次，确认三个常驻服务的状态列均为 `healthy`、`restarts` 计数不再增长（部署本身触发的一次容器创建不计入异常重启）；
- 用 `docker inspect --format='{{json .State.Health.Log}}' <容器>` 抽查最近若干次健康检查记录，确认没有非预期的失败探测；
- 核对应用层信号：`scheduler` 日志无持续报错、`gateway` 日志显示长连接已建立、`worker-queue` 日志显示能正常进入领取循环（不要求已经真实处理业务任务）。

**判定标准（两条判据分属不同窗口，不要互相替代——独立审查，时延口径统一）**：

1. **转 healthy**：部署刚执行完时，各服务应在自身 `start_period` 加一个探测周期（`interval + timeout`）内转为 `healthy`（gateway `start_period` 20s、worker-queue 32s、scheduler 30s，见 `deploy/compose.yaml`；判据推导见 `deploy/监控告警.md`「转 healthy 判据」小节）——**这条判据用的是 `start_period`，不是「五、时延估算」里"服务持续 unhealthy 需要多久才被发现"的 A/B 类数字**；本节修订前的版本曾把 A 类发现时延（`gateway` ≥ 69s、`worker-queue` ≥ 119s）当成"多久应该转 healthy"的期限，这是两个不同判定窗口的概念混用，已更正。
2. **观察窗口内保持 healthy**：15 分钟观察窗口结束时三项常驻服务全部 `healthy` 且无非预期重启，判定本次部署成功，转入正常运行；观察期内出现非预期重启，或任一服务转回非 `healthy` 状态，判定部署失败，进入「六、回滚判据与步骤」。

## 八、恢复演练要求

数据库从备份恢复后，在重新对外提供服务（即允许新的问数流量、重新拉起三个常驻服务处理正常任务）之前，**必须先按内容原始写入时间重新执行一轮保留清理**（`V-投递-07` 语义，见[验收矩阵「交付与投递」分册](../docs/技术设计/验收矩阵-交付与投递.md#问数结果投递与正文生命周期)）：备份或 WAL 中无法逐条即时删除的内容（含尚未结束的会话保留窗口）按原写入时间计算到期，不能把恢复完成的时间当成新的保留起点，也不能借这次恢复重新打开一个已经结束的会话窗口。恢复历史备份可能重新带回备份时已有的用户内容，这是已经接受的灾备取舍，但「重新提供服务前先补清理」是这条取舍成立的前提，不是可选步骤。

一次真实的数据库恢复演练目前尚未执行（[Issue #369 第 4 条](https://github.com/Moshuiwang/lingxi/issues/369)）；生产首次部署前应至少完成一次真实恢复演练，核对上述补清理动作确实被执行且结果符合预期，本 runbook 只登记要求，不代为执行。**已知阻碍（对抗审查 P3-1）**：现有的 `scripts/ops/backup_restore_drill.sh` **对生产不可用**——它只会对本机容器 `docker exec pg_dump`，而 stage 与生产的库都是 Supabase 云托管，没有可 exec 的容器；目前**没有可用于生产的替代演练脚本**。换句话说这条要求当前没有可执行载体，补齐脚本是做这次演练的前置。

**2026-09-02 首发的实际处置**：产品负责人显式豁免了本节要求（首发为空库），演练仍未执行；备份侧的过渡安排与撤销条件见「十一、实录偏差」11.6。**该豁免只对首发有效**，不构成后续升级的常设豁免。

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

### 10.1 systemd 单元安装（本节 rc25 补入；此前正文里一条 `systemctl` 都没有）

**为什么必须写在这里**：2026-09-02 首发时 `biplus-prod` 上那三个 timer 是**现场手工装的**，仓库正文没有任何安装步骤，也就没有可复现的记录——下一次换机器或重装，只能靠人回忆。本节补齐这条缺口，具体单元内容与凭据文件仍以 `deploy/监控告警.md`、`deploy/日志留存.md` 为准，这里只固定**装什么、什么顺序、怎么回读**。

**时点：三个常驻服务 `healthy`、观察窗口通过之后再装并 `enable`**。提前 `enable` 会在服务还没起来的窗口里向管理群发假告警（首发实录 11.6 已登记）。

```bash
# 1. 脚本与凭据到位（路径、权限、文件内容见两份文档；凭据值不进本文件）
#    /opt/lingxi/scripts/{host_health_alert.py,collect-container-logs.sh,monitoring/*}
#    /opt/lingxi/monitoring/{host-monitor.env,db-business.env,push.env}   均 0600
#    升级时把运维脚本更新到发布版本（以部署用户执行，旧版原地留 .bak-rc25 之类后缀）：
#    scripts/ops/monitoring/*.sh 与 deploy/collect-container-logs.sh → /opt/lingxi/scripts/…
#    2026-09-04 实际更新的四个：resource_sample / db_business_sample / push_to_monitoring / collect-container-logs

# 1a.（root 级，列入产品负责人亲手放行清单）logrotate 配置到位：
#     lingxi-logrotate.service 的 ExecStart 指向这个固定路径，配置不在时该 timer
#     每小时 failed 一次、容器日志无上限增长（deploy/日志留存.md「已知限制」）
sudo install -d -m 755 /etc/lingxi/logrotate.d
sudo cp deploy/lingxi-container-logs.logrotate /etc/lingxi/logrotate.d/lingxi-container-logs

# 1b.（root 级，同上）/var/log/lingxi 属主必须是本机部署用户。
#     2026-09-03 生产实测：当前是 root:root 755，只有 monitoring/ 子目录是
#     bi-ai-deploy 750——而收集脚本第一步就 chmod 0750 该目录，属主不对时
#     set -e 直接退出、一行日志都不收（deploy/collect-container-logs.sh）：
sudo chown bi-ai-deploy:bi-ai-deploy /var/log/lingxi
sudo chmod 750 /var/log/lingxi

# 1c. 回读（期望第一行 bi-ai-deploy:bi-ai-deploy 750，第二行同属主 750；
#     以及 logrotate 配置文件存在）
stat -c '%n %U:%G %a' /var/log/lingxi /var/log/lingxi/monitoring
ls -l /etc/lingxi/logrotate.d/lingxi-container-logs

# 2. 装单元本体（仓库单元里没有 User=）
sudo install -m 644 deploy/monitoring-units/*.service \
  deploy/monitoring-units/*.timer /etc/systemd/system/

# 3. 每个单元各配一份本机 drop-in 提供 User=（模板：
#    deploy/monitoring-units/10-local.conf.example，prod 填 bi-ai-deploy）
sudo install -d -m 755 /etc/systemd/system/<单元名>.service.d
sudo install -m 644 <本机 10-local.conf> /etc/systemd/system/<单元名>.service.d/10-local.conf

sudo systemctl daemon-reload

# 4. 逐个 enable --now。monitoring-push 的单元与 timer 可以先装，但
#    /opt/lingxi/monitoring/push.env 不存在（监控库最小权限角色未建）时不 enable——
#    2026-09-04 即如此，维持观察期项
sudo systemctl enable --now lingxi-host-monitor.timer lingxi-resource-sample.timer \
  lingxi-db-business-sample.timer lingxi-log-collect.timer lingxi-logrotate.timer

# 5. 回读三项，缺一不可
systemctl list-timers 'lingxi-*'                       # 每个 timer 的 NEXT/LAST 在前进
systemctl cat lingxi-host-monitor.service | grep '^User='   # 解析成本机部署用户
ls -la /var/log/lingxi                                  # 收集目录里开始出现容器日志
```

**装之前先 `systemctl cat <单元>` 与仓库版本逐行比对**：现装的那几份是首发现场手写的，不保证与仓库版本等价；差异逐条确认后再覆盖，不要盲覆盖。

**2026-09-04 实际安装终态（rc25 升级窗口；取代上一段 2026-09-02 的「未装齐」状态）**：

- **投放姿势**：单元文件、`10-local.conf`（`User=bi-ai-deploy`）与 logrotate 配置先以部署用户写进 `/home/bi-ai-deploy/rc25-units/`（12 个单元＋drop-in＋配置），root 级安装一律 `install` 自该目录取文件，32 条扁平命令逐条执行、逐条回读（产品负责人当场放行，权限规则路径钉死）。
- **终态回读**：5 个 timer enabled/active——`lingxi-host-monitor`、`lingxi-resource-sample`、`lingxi-db-business-sample`、`lingxi-log-collect`、`lingxi-logrotate`；**6 个服务的 `User=` 全部经 drop-in `10-local.conf` 解析为 `bi-ai-deploy`**（`systemctl cat` 逐个核对），仓库单元与生产实体不再分叉；`/etc/lingxi/logrotate.d/lingxi-container-logs` 0644 root；`/var/log/lingxi` `bi-ai-deploy:bi-ai-deploy 750`、其下 `.state`（logrotate `--state` 路径，由部署用户建）755、`monitoring/` 750；`lingxi-log-collect` 与 `lingxi-logrotate` 首轮 `Result=success NRestarts=0`。
- **`lingxi-monitoring-push` 单元与 timer 已装但 disabled/inactive**：`/opt/lingxi/monitoring/push.env` 不存在，按上文步 4 不 enable，维持观察期项。
- **回滚**：`sudo systemctl disable --now lingxi-*`；首发手写的三个旧单元与新版差异只有 `User=` 内联 vs drop-in。

> **已知边界（rc25 登记，本批不修）**：`deploy/monitoring-units/` 的六个 `.service` 单元
> 都**没有 `OnFailure=` 告警接线**——timer 或单元失败只留在 systemd 状态里，不会向任何
> 渠道推送告警（告警目标单元本批不建；对应缺口 W0-12 因 D-25 缓议，由后续卡承接）。
> 唯一的间接补偿是 host-monitor 的「采样停更」判定。补上之前，`systemctl list-timers
> 'lingxi-*'` 的人工巡检是发现 timer 静默失败的唯一途径。

## 十一、实录偏差（2026-09-02 首发）

> 本节登记 **2026-09-02 首次真实生产部署**与本文件正文（含 `deploy/README.md`）不一致的地方，逐条写清「正文怎么写 / 实际怎么做 / 为什么」。执行记录与逐条回读见 [Issue #519](https://github.com/Moshuiwang/lingxi/issues/519)（生产执行卡）与 [Issue #263](https://github.com/Moshuiwang/lingxi/issues/263)（硬切卡）。下一次生产执行以本节为准，正文与本节冲突时**以本节更晚的实录为准**。

### 11.1 环境与主机准备

| # | 正文怎么写 | 实际怎么做 | 为什么 |
| --- | --- | --- | --- |
| 1 | 默认宿主机已备好 Docker 与 Compose 插件 | `biplus-prod`（AL2023）**没有** `docker-compose-plugin` 包：`docker` 从 dnf 现有版本装（server 25.0.16，stage 是 25.0.14，补丁号差异已登记）；Compose 以二进制装入 `/usr/libexec/docker/cli-plugins/`，**版本钉成与 stage 同一个 `v5.2.0`**，下载后 `sha256sum` 与官方 `.sha256` 逐字节比对 | AL2023 的仓库里没有 compose 插件包；compose 的渲染行为影响判据（见 11.3 第 1 条），必须与 stage 同版才能沿用 stage 的验收结论 |
| 2 | 未提及部署用户的 docker 组 | 部署用户加入 `docker` 组（`usermod -aG docker`） | 让部署用户能直接执行 compose；**docker 组约等于 root，属权限扩大**，本次由产品负责人显式点名放行（R9），下次执行仍需单独授权，不得视为常设 |
| 3 | 未提及运维脚本的 Python 版本 | prod 系统 Python 是 3.9，而两个受控运维脚本（银河导出导入 `scripts/import_galaxy_permission_export.py`、差集导入 `scripts/ops/import_local_permission_override.py`）跟随本仓库的 `requires-python >=3.12`：装 `python3.12` 并建独立虚拟环境 `lingxi-ops-venv`（只装 `psycopg[binary]`），按脚本文档的 `PYTHONPATH=src` 姿势运行 | **不在生产现场安装或构建 `lingxi` 包**——生产只跑冻结制品，现场构建是明令禁止的路径；`PYTHONPATH=src` 让脚本以源码树方式运行而不引入安装态 |
| 4 | 未提及 swap | prod 无 swap 而 stage 有 2G：首发前补一个 2 GiB swapfile 并写入 `/etc/fstab`，实测 `swapoff` → `swapon -a` 可由 fstab 行拉起 | rc24 三张资源类卡（#494/#496/#499）的 L4a 全部跑在有 swap 的 stage 上；不补齐这条不对称，那些结论在内存维度不可迁移（R11）。该 swapfile 属长期保留项，不是临时资源 |

### 11.2 `runtime-config` 挂载卷：内容必须自带

`compose.prod.yaml` 把宿主目录 `/opt/lingxi/runtime-config` 只读挂进 scheduler 与 worker 容器，但**正文里没有任何一步负责创建它或往里放东西**——docker 会自动建一个空目录，挂载成功但内容为空。首发的处置：

- **`system_prompt.md` 必须从 stage 传入**（首发 sha256 前缀 `84741170e3c2…`，与 stage 逐字节一致），并设 `LINGXI_WORKER_SYSTEM_PROMPT_FILE` 指向容器内路径。**没有安全的「留空」选项**：提示词按 2026-08-23 裁定不进代码、不进镜像，镜像里没有可回落的随包版本，文件缺失时 worker **静默降级为无提示词执行**——服务全 `healthy`、观察期全绿、digest 全对、用户也能问出答案，但每次问数的质量与 stage 上验证过的完全不是一回事。该变量与 `LINGXI_WORKER_SYSTEM_PROMPT`、`LINGXI_WORKER_OUTPUT_SAFETY_CANARY` **互斥，同时配置启动即失败**，从 stage 复制 env 时须确认另两个没被一起抄过来。
- **`LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH` 刻意不设**：外置映射文件与随包默认经结构化逐键比对为 **354 键零差异**，留空即用随包默认，等价且少一个需要长期同步的带外文件。反过来，若照「非敏感配置抄自 stage」的做法把这个路径变量抄进 prod 而文件不在，会按既定语义**响亮失败**（权限发布整轮拒绝，一条发布意图都不排），首批用户全部开不通。**将来真要启用外置文件时的两条硬前提**（Trace #544 S-2c）：① 这个变量自 rc25 起 `scheduler` 与 `gateway` **两侧同读**，要配就两份 env 一起配、值相同，要么两边都不配——只配一边会让管理动作按一份映射发布、次日每日重算按另一份翻回来，每次翻转都是用户可见的真实权限变化；② **先补 `gateway` 服务的 `runtime-config` 只读挂载再配变量**——`compose.prod.yaml` 目前只给 `scheduler` 挂了它，gateway 配了却读不到时三处管理动作全部失败关闭（安全但不可用）。详见 [`deploy/验收前部署配置清单.md`「闸① 外置路径」](验收前部署配置清单.md)。
- **验收判据（首发已执行）**：首批用户第一次问数之后，读该轮 worker 终态审计的 `system_prompt_digest`，必须与 stage 同值、且降级计数为 0。该字段是**提示词文件 strip 之后 sha256 的前 12 位**（首发实测 `2272bd4d40ae`），不是文件本身的 sha256，核对时别拿错值。
- 同一份 env 里，内测轮的**内容级采集**两个变量在生产禁止配置，且 CI 守卫读不到未入库的生产 env 文件——这条只能靠部署纪律兑现：从 stage 的 worker-queue env 生成 prod 版本时必须**显式剔除**这两项（它们恰恰只存在于 stage 那一份里）。

### 11.3 判据与命令形态

| # | 正文怎么写 | 实际怎么做 | 为什么 |
| --- | --- | --- | --- |
| 1 | §2.1 要求确认 memory「渲染成 `512M` 而不是默认 `1G`」 | Compose v5.2.0 的 `config` 输出的是**字节数**，字面 `512M` 根本不会出现。判据按字节读：`512M` → `"536870912"`、`2G` → `"2147483648"`；**`"1073741824"` 就是漏配退回 1G 的那个错**，正是要抓的 | 照字面判据核会把**正确的配置判成不通过**；这两行是 `${VAR:-1G}` 有默认值形态，看 env 文件看不出漏没漏，只能靠渲染回读 |
| 2 | §三「部署时核对」用 `docker compose … images` 回读 digest | 该判据只在 `up -d` 之后成立。首发在 `migrate` 这一步（`run --rm`，跑完不留容器）改用 `config` 看渲染出的镜像引用是否带正确的 `@sha256:`；`images` 的逐服务核对留到起服务之后 | `run --rm` 不留容器，`images` 输出为空 |
| 3 | §2.3 / §2.4 的两条示例带齐了 `-f compose.yaml -f compose.prod.yaml`，但正文没有说明为什么两个都不能省 | 首发把它当硬纪律执行：**每一条** compose 命令（含 `migrate`、`reauthorize` 这类临时 job 与单服务操作）都同时带这两个 `-f`，一个都不能省 | 各服务的 `env_file` **只声明在覆盖文件** `compose.prod.yaml` 里；只带 `compose.yaml` 时命令照样能跑，但跑出来的是一个没有任何凭据与配置的容器——这是个不报错的错法 |
| 4 | `deploy/README.md`「凭据丢失或过期的保底」给的重授权示例是裸的 `--profile job run --rm reauthorize`（不带参数） | 需要传参时（如首次建立授权主体）必须把入口**写全**：`… run --rm reauthorize python -m lingxi.apps.reauthorize <参数…>` | `run --rm <service> <args>` 的 args 会**替换**掉 compose 里声明的 `command`；直接在服务名后面追加参数，会把 `python -m lingxi.apps.reauthorize` 这个入口一起丢掉 |
| 5 | 未提及 profile | 起服务固定用 `--profile mvp up -d`；裸 `up -d` 只起 scheduler，Bot-Prod 长连接根本不接 | `gateway` 与 `worker-queue` 都在 `mvp` profile 里 |

### 11.4 内测名单：必须写两份，且要在正确的应用作用域下解析

`LINGXI_INNERTEST_ROSTER_OPEN_IDS` 在任何 compose `environment:` 块里都没有声明，**写进根 `.env.prod` 静默无效**。它必须同时写进 `.env.prod.scheduler` 与 `.env.prod.gateway` **两份、逐字一致**——两个服务各读各的，只配一处时另一处按默认空值 = 全拒，首批用户会全部收到「内测未开放」。

名单里的 `open_id` 必须是 **Bot-Prod 作用域**下解析出来的（`open_id` 随应用变，Bot-Test 的名单对 Bot-Prod 全拒）。首发的解析链路是：正式权限表里的用户邮箱 → 花名册人员 ID → 组织快照 `user_id` → Bot-Prod 的 `open_id`。**不要指望用邮箱直接调批量 ID 接口**：该接口在本租户解析不到这些邮箱。

### 11.5 首次授权（`reauthorize` bootstrap）

- **授权链接 10 分钟一次性**，两次发起之间不可复用旧链接。首发第一次发起就是因为点了已过期的链接而在回调等待上超时（未写入任何东西），第二次用新链接才成功。执行时应在链接生成后立即点击。
- 首次建立授权主体用 `--bootstrap-subject <主体 open_id> --confirm-bootstrap`，该 `open_id` 不必写进 env 文件。首发的主体账号**不在组织快照里**，其 `open_id` 是经 `union_id` 跨应用解析得到的。
- 凭据落地后 **scheduler 无需重启**即会用新凭据自动同步花名册（首发实测：授权完成约 30 秒后 1223 行落库）。组织快照若恰好在上一轮失败后的退避期内，需要重启 scheduler 才会立即触发。
- §2.2「登录 GHCR」一项本次**不适用**：镜像包当日维持公开，宿主机无需登录即可拉取（D-1 裁定）。**后续更新**：产品负责人 2026-09-03 裁定「无法实现的 GHCR 就保持原状」，私有化不再是观察期项，四个包**保持公开**是终态；三条外部原因与将来重启用的前置见 `deploy/README.md`「主机读取身份」。公开镜像的暴露面见 11.7。

### 11.6 监控与备份

- **（2026-09-03 更新：本条前半句的旧做法已作废，不得再照做。）** 首发当日 `deploy/monitoring-units/` 的 `.service` 单元 `User=` 硬编码为 stage 用户，当时的处置是安装到 prod 后手改单元文件；rc25 起仓库单元**已不含 `User=`**，该值一律由 §10.1 步 3 的本机 drop-in（`10-local.conf`）提供——**不得手工修改 `/etc/systemd/system` 下的单元文件**（红线三：不人工修改生产文件），单元安装与 `User=` 注入以 §10.1 为准。本条仍然成立的后半句：这些单元用 systemd timer 触发（不是 cron），且 `enable` 必须放在服务起来之后——提前装会向管理群发假告警。
- `monitoring-push` 需要一个数据库监控角色，首发时未创建，**登记为观察期项**（2026-09-04 rc25 升级时单元已装、仍未 enable，见 §10.1 终态）；三个采样 timer（主机健康、资源、数据库业务）已安装并各跑通一轮。
- 容器日志留存（`deploy/日志留存.md`）首发未安装，留观察期补——**已于 2026-09-04 rc25 升级装齐**（收集与轮转两个 timer，见 §10.1 终态）。
- **`lingxi-prod` 没有备份点**（产品负责人 R8：首发豁免恢复演练）。过渡方案是由部署用户每日一次 `pg_dump -Fc` 到自己的目录（0600），首次 dump 在首批用户开通后立即执行；**产品负责人在托管控制台开启每日备份 / PITR 之后即撤销这个手工过渡**。`scripts/ops/backup_restore_drill.sh` 是演练脚本、不是备份计划，不能顶用。

### 11.7 `deploy/README.md` 的一处表述已更正

README 「主机读取身份」一节原写「把镜像包设为公开的代价是……源码本身不在镜像里」——**这一句不成立**。公开镜像里包含全部源码、用户可见文案版本文件、公司与职能到指标的映射 TOML 以及迁移 SQL。该表述已在 README 与 `Dockerfile` 的对应注释里更正；本次首发在知情前提下接受镜像公开。**2026-09-03 更新**：改回 private 不再是观察期项——产品负责人裁定保持原状，保持公开是有裁定的终态（原因见 README 同一节）。

## 十二、#541 预开通批量执行姿势（生产；rc25 补入）

> **前提与去留**：本节只在「预开通名单（A-3）已到、且产品负责人裁定纳入本次窗口」时
> 执行；**名单未到则本节整体移出本次升级窗口（块 Z），由产品负责人裁定顺延**，不影响
> 本 runbook 其余步骤。执行时点在 §2.0-§2.5 升级完成、三服务 healthy、观察期通过之后
> ——脚本走的开通链要用到本批新镜像与迁移 0087 的停滞收口接缝，旧镜像上不得执行。
>
> **2026-09-04 实录**：本节已在生产真实执行一次（金丝雀单人：dry-run → 单字 → `--apply` → 激活），此前同日先在 stage 用真实名单演练三次才走通——三次里前两次分别被 §12.0 的内测白名单闸与 §12.2 的令牌间隔规则挡住、零写入。**新入口复用旧链的改动，stage 真实名单演练必须早于生产窗口一天**，不能放在窗口当刻。

**登记一处脚本自述错误（照 docstring 跑必然失败）**：`scripts/ops/preprovision.py` 的
docstring 与 CLI 描述给出的运行姿势指向 `/app/scripts/ops/preprovision.py`，但
`.dockerignore` 排除了 `scripts/`、生产镜像里既没有 `/app` 目录也没有该文件——照抄
docstring 执行会得到 `No such file`，退出码 2、名单一人未动。正确姿势见本节：**脚本
本体经 stdin 喂给 scheduler 容器内的 Python**（脚本只依赖标准库与镜像内已装的
`lingxi.*` 包），不进镜像、不在生产现场构建、不修改任何生产文件。docstring 本身的修正
属代码面，不在本节范围。

### 12.0 前置：名单内的人必须先在内测白名单里（2026-09-04 stage 实测补入）

内测闸按 `open_id` 比对、挡在开通链 `_run` 的最前端，预开通逐字继承这道闸：名单内的人不在白名单里，脚本对他逐人报 `innertest_roster_rejected`、**零写入**（stage 演练首跑即此）。所以名单到手后、跑 §12.2 之前，先把名单内每个人的 **Bot-Prod `open_id`** 追加进 `.env.prod.scheduler` 与 `.env.prod.gateway` 的 `LINGXI_INNERTEST_ROSTER_OPEN_IDS`（**两文件同值**，写法与 11.4 相同），随 §2.4 的 `up -d` 一并生效、不额外重启；回读两份文件的条目数一致且各命中名单人数。`open_id` **按应用隔离**：从生产组织快照按显示名解析、**每人必须唯一**，解析不唯一的人不进白名单也不进名单。两份 env 改动前各留一份 `before-<本批 tag>` 备份。2026-09-04 实录：九人名单让白名单从 25 扩到 34。

### 12.1 输入投放：名单进容器

名单 CSV（三列 `email,position,company_scope`，形态与整份拒绝规则见脚本 docstring）
先落到部署用户目录——**输入投放，允许**；名单含邮箱等人员数据，目录 0700、文件 0600：

```bash
umask 077
mkdir -p /home/bi-ai-deploy/rc25-preprovision
# 名单由产品负责人提供后写入 /home/bi-ai-deploy/rc25-preprovision/roster.csv（0600）
```

再把名单以 **stdin** 投放进 scheduler 容器的 `/tmp`（16m tmpfs：够放名单，不在生产留持久
文件）。**这是唯一可用的投放姿势**（2026-09-04 stage 与生产实测）：`docker compose cp`
在只读 rootfs 的容器上直接被拒（`container rootfs is marked read-only`），不要再试；
stdin 投放以容器默认用户 uid 10001 写入，也就没有 `docker cp` 保留宿主 0600 属主、
落进容器后 uid 10001 读不到的问题。不动任何生产文件：

```bash
cd /home/bi-ai-deploy/projects/lingxi
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  exec -T scheduler sh -c 'umask 077; cat > /tmp/roster.csv' \
  < /home/bi-ai-deploy/rc25-preprovision/roster.csv

# 可读性回读（必做）：以容器默认用户读一次行数，期望 = 名单数据行数 + 1（表头）
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  exec -T scheduler wc -l /tmp/roster.csv
```

**容器重建会清空 `/tmp`**：§2.4 的 `up -d` 或任何导致 scheduler 容器重建的动作之后，
名单已经不在了，须重新投放再跑 §12.2。

### 12.2 dry-run → 产品负责人过目计数 → `--apply` → 幂等复跑

脚本本体经 stdin 执行（`python -B -` 读标准输入）。参数按脚本真实 argparse 接口：
位置参数 = 容器内名单路径；`--initiated-by` 必填，且必须是 `admin_registry` 里一位
生效的已登记管理员（不是则退出码 2、零写入，dry-run 也过这一关）；DSN 缺省读容器
环境的 `LINGXI_POSTGRES_DSN`（scheduler 容器已有，不需要传 `--dsn`）：

```bash
# ① dry-run（默认即 dry-run：不带 --apply 就是零写入，结构上不调用开通入口）
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  exec -T scheduler python -B - /tmp/roster.csv --initiated-by <管理员 open_id> \
  < scripts/ops/preprovision.py
```

产品负责人逐行核对 dry-run 清单（逐人「邮箱 → 职位 → 公司范围 → 将获得的公司×指标
条数」与末尾合计）**无误并放行后**，同一条命令追加 `--apply` 真正执行。**参数写全称
`--apply`**：脚本的 argparse 已关闭前缀缩写（`allow_abbrev=False`，rc25 修复包 F5），
`--ap` 这类前缀会被拒绝而不是当成 `--apply` 执行（2026-09-04 stage 实测缩写被拒）；
窗口内仍只接受逐字的 `--apply`。

**运行规则：专用授权令牌的最小续期间隔（2026-09-04 stage 实测）**。脚本是常驻
scheduler 之外的**第二个进程**，而在职实时回读要领取的是**独占的**专用授权派生令牌，
按需续期有**最小间隔 5 分钟**（当日上限 100 次）。常驻进程刚续期过（日报、权限重算都会
触发）的 5 分钟内起脚本，脚本会对名单逐人报
`employment_read_failed_AccessTokenUnavailable`（原因 `refresh_min_interval_not_elapsed`）、
**零写入、EXIT=0**——**这不是故障**，失败的领取不计入当日次数，等 5 分钟原样重跑即可。
起脚本前先看 scheduler 日志最近一条「专用授权续期成功」的时刻，距今不足 5 分钟就等。

**MCP 就绪节奏（2026-09-04 stage 与生产实测）**。外部 MCP **每 15 分钟**拉一次正式表
（产品负责人给的周期）。MCP 尚不认识的新人，发布后的就绪探针会一直 `waiting/http_401`
直到被拉上；脚本的 15 分钟预算内没等到 → 该人停在 `mcp_syncing`、脚本报
`skipped reason=mcp_sync_timeout`（**不算失败**，EXIT=0），常驻 scheduler 的迟到就绪
恢复每 900 秒回看一次、拉到即**静默**激活并挂起首聊补一句。生产实测（金丝雀）：发布后
约 9 分钟第 4 次探针 `ready`。另一条实测事实：**首次开通必然产生一条 `first_onboarding`
发布并在正式表新建一行**（`created_record_id` 有值），哪怕名单权限与此人银河已有权限
完全相同——「只在变化时回写」管的是之后的每日重算，不是首次开通。

**时长预期（rc25 审核裁定，写给窗口排程）**：逐人**同步**执行，单人最长约 **17 分钟**
——大头是发布后的 MCP 就绪确认（t=0 起每 180 秒一次、总预算 900 秒，stage 实测 7 次
探针、单次探针超时 20 秒，见 `src/lingxi/core/permission/mcp_readiness.py`），加上身份
定位/建档/权限发布等其余同步步骤。名单 N 人的最坏上界按 N×17 分钟排；正常路径远快于
此（就绪即回）。**多人名单不要挂在交互终端里裸等**：用 nohup 后台执行（或在 tmux 窗口
内前台跑，等价），把输出落进部署用户目录——日志含邮箱与逐人结果，先 `umask 077`：

```bash
cd /home/bi-ai-deploy/projects/lingxi
umask 077
nohup docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  exec -T scheduler python -B - /tmp/roster.csv --initiated-by <管理员 open_id> --apply \
  < scripts/ops/preprovision.py \
  > /home/bi-ai-deploy/rc25-preprovision/apply-$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
# 之后 tail -f 该日志观察进度
```

**日志落点即失败清单**：脚本结尾打印逐人终态（`provisioned` / `skipped` /
`failed_<异常类型>`；名单里**已开通**的用户按产品负责人 2026-09-04 裁定（#544）报
「已开通、名单权限未应用」的独立状态、不计入成功数——预开通不是存量用户的批量授权
入口，这类人要调权限走管理卡既有授权路径）与计数，并明言「跑完即散、请自行保存」
——这份日志就是唯一的逐人报告，**失败清单可据以重跑**。复跑同一份名单是安全的：重复预授权在库内撞唯一约束
按 `already_present` 降级、开通链对已完成的人按既有幂等语义处理；更干净的做法是把名单
裁剪到失败者，重新走一轮 dry-run → `--apply`。**同一时刻只允许一份名单在跑**，不并行
起第二个批次——脚本的在职回读会消费当天专用凭据续期预算里的一次（常驻 scheduler 日报
侧那一次可能因此推迟到下一窗口，已知代价），并行没有收益还违反单写入者纪律。

跑完清理（谁建谁清；容器 `/tmp` 是 tmpfs、重启也会自清，这里显式清是不把人员数据留给
下一步）：

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/compose.yaml -f deploy/compose.prod.yaml \
  exec -T scheduler rm -f /tmp/roster.csv
# 宿主侧名单与日志按产品负责人指示保存或删除，不长期留在部署用户目录
```

### 12.3 窗口前真机确认清单（只读，未确认不执行本节）

| # | 要证明什么 | 只读命令（在 `/home/bi-ai-deploy/projects/lingxi` 下） | 期望 |
| --- | --- | --- | --- |
| 1 | 名单内每人的 Bot-Prod `open_id` 已在两份 env 白名单里（§12.0） | `grep -c` 两份 `.env.prod.{scheduler,gateway}` 的 `LINGXI_INNERTEST_ROSTER_OPEN_IDS` 行内各 `open_id` | 两文件条目数一致、名单人数全部命中 |
| 2 | 容器内 Python 可执行 stdin 脚本 | `echo 'print("stdin-ok")' \| docker compose --env-file deploy/.env.prod -f deploy/compose.yaml -f deploy/compose.prod.yaml exec -T scheduler python -B -` | 输出 `stdin-ok` |
| 3 | 名单 stdin 投放后对容器用户可读 | 12.1 的 `wc -l` 回读 | 行数 = 名单数据行数 + 1（表头） |
| 4 | `--initiated-by` 是生效管理员 | 由 ① dry-run 自查（不是则退出 2、零写入） | dry-run 正常打印清单与计数 |
| 5 | 专用授权令牌不在 5 分钟续期间隔内（§12.2） | scheduler 日志最近一条「专用授权续期成功」的时刻 | 距今 ≥ 5 分钟 |
