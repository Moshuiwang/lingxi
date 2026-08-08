# 部署编排（S11，[Issue #62](https://github.com/Moshuiwang/lingxi/issues/62)）

本目录是 Lingxi 的部署编排。**它不是部署设计文档**——部署方案在[架构设计「八、部署与发布」](../docs/技术设计/架构设计.md)定稿，部署对代码的约束在[验证与门禁「十二、部署对代码的约束」](../docs/技术设计/验证与门禁.md)。这里只写"怎么执行"。

> **本批交付的是编排与门禁，不是一次已完成的部署。**
> `biai-stage` 的安装、健康回读、Bot-Test 真实入口与切回旧 tag 演练**尚未执行**；`biplus-prod` 一切未动。生产部署另建独立 `[ops]` Issue，固定镜像 digest、备份点、执行窗口、观察期与回滚目标。

## 本版包含哪些进程

| 服务 | 形态 | 入口 | 说明 |
| --- | --- | --- | --- |
| `scheduler` | **常驻服务** | `python -m lingxi.apps.scheduler` | 专用授权凭据（「四达文档会议助手」`refresh_token`）到期轮换扫描 |
| `gateway` | **常驻服务，但默认不启动** | `python -m lingxi.apps.gateway` | 飞书长连接接入，收事件落库成任务（#57 / S4 前半）。放在非默认 profile 里，见下 |
| `worker` | **一次性作业** | `python -m lingxi.apps.worker` | 单回合受控执行。跑一个 Agent SDK 回合、往 stdout 写一个 JSON 报告、退出 |
| `migrate` | **一次性作业** | `python -m alembic -c /opt/lingxi/alembic.ini` | 部署时跑一次的数据库迁移 |
| `reauthorize` | **一次性作业，默认不启动** | `python -m lingxi.apps.reauthorize` | 凭据丢失或过期时的正式重授权；复用 scheduler 镜像，放在 `job` profile |

**worker 与 reauthorize 都是一次性作业，不是已上线的常驻服务。** worker 不领任务、不接飞书、不处理 `SIGTERM`（`grep -rn signal src/lingxi/apps/worker/` 为空）。常驻 worker 需要任务队列与 gateway 入队，属 **S4 下半**，不在本批。`docker compose up -d` 只会启动 `scheduler` 一个进程；重授权必须由运维人员显式 `run`。

**`gateway` 已随 [#57](https://github.com/Moshuiwang/lingxi/issues/57) 落地并纳入编排，但默认不启动。** 它把事件落库成任务，而任务队列此刻**没有消费者**——main 上的 worker 是单回合 CLI，不领任务。启用它等于开始接收真实用户消息、排进一个没人处理的队列，用户看到的是"发了没反应"，比不接入更糟。因此它放在一个非默认 profile 里：

```bash
# 默认：不会启动 gateway
docker compose --env-file deploy/.env.stage -f deploy/compose.yaml -f deploy/compose.stage.yaml up -d

# 显式启用（S4 下半接线前不要这么做）
docker compose --env-file deploy/.env.stage -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile gateway up -d
```

S4 下半（常驻 worker 与任务领取）接线之后，删掉 `compose.yaml` 里 gateway 的 `profiles:` 一行即可转为默认服务。`admin` 进程仍**未建立**，因此没有为它构建镜像——未实现的入口不得用占位进程冒充。

本版**不宣称**首聊、持续同步、建档、权限发布或用户通知已经上线。

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

`LINGXI_POSTGRES_DSN` **必须带 `connect_timeout`、`statement_timeout`、`lock_timeout` 三个参数**，它们共同给出停机上界（见下方）。只设 `connect_timeout` 是不够的：它只约束建连，一条已经发出去的语句可以无限期挂着。

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
卷；state 文件和凭据文件都在该卷内，入口会在启动前拒绝文件或锁文件路径冲突。

以 stage 为例，确认六个 env 文件都已按上面的 preflight 准备好后执行：

```bash
docker compose --env-file deploy/.env.stage \
  -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile job run --rm reauthorize
```

终端只显示授权地址和脱敏结果；按提示在受控浏览器完成同意，再在关闭回显的输入提示中粘贴完整 HTTPS 回跳地址。不要把回跳地址或授权码写入命令行、shell 历史、Issue 或日志。取消、换码失败或保存失败后不要重放旧回跳，重新运行该一次性 job 取得新的 state。

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
| 数据库往返预算 | 55.0 | 5 次操作（4 次 save 重试 + 1 次 revoke）× 11 秒 |
| **最坏合计** | **79.2** | × 1.5 安全系数 = 119 秒 → 取整到 **150 秒** |

其中"每次数据库操作 ≤ 11 秒"= `connect_timeout` 5 秒（建连）+ `statement_timeout` 3 秒（语句）+ 3 秒（提交，提交本身也是语句）。

> **早先的版本在这里写的是 90 秒，而且依据是错的。** 它只把 `connect_timeout` 算进去，但 **`connect_timeout` 只约束建连**——连接建好之后，一条卡住的 `SELECT` / `INSERT` / `COMMIT` 可以无限期挂着，于是"90 秒是上界"这个声称根本不成立。真正的上界必须由 DSN 里的 `statement_timeout` 与 `lock_timeout` 一起给出（`lock_timeout` 不能省：等锁的时间不算在 `statement_timeout` 里）。这三个参数都在配置层，不需要改 `src/`。

**这个不等式由 `scripts/ci/check_deploy_contract.py` 自动守住，不是靠文档。** 它同时断言 `deploy/.env.example` 的示例 DSN 真的带了这三个参数——否则上面这张表就只是一张好看的表。改了 `REQUEST_TIMEOUT_SECONDS` 而不改 compose，门禁会红。

`scheduler` 必须**单副本**：进程间互斥靠的是凭据目录里的 `flock` 文件锁，多副本会互相阻塞。

## 本地验证

```bash
scripts/ci/build_image.sh scheduler                # 构建单个镜像（scheduler|gateway|worker|migrate）
scripts/ci/verify_image_contract.sh <四个镜像引用>  # 镜像契约逐条核对（scheduler worker migrate gateway）
scripts/ci/verify_compose_structure.sh             # stage ↔ 生产结构对照
scripts/ci/verify_old_image_new_schema.sh          # V-部署-05：旧镜像在新库上启动
scripts/ci/verify_old_image_new_schema.sh --destructive   # 同上的变异对照，应判红
```

本机没有 buildx 也能跑：只用 plain `docker build` 支持的语法，不引入 BuildKit 专有语法或缓存后端。
