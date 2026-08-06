# 部署编排（S11，[Issue #62](https://github.com/Moshuiwang/lingxi/issues/62)）

本目录是 Lingxi 的部署编排。**它不是部署设计文档**——部署方案在[架构设计「八、部署与发布」](../docs/技术设计/架构设计.md)定稿，部署对代码的约束在[验证与门禁「十二、部署对代码的约束」](../docs/技术设计/验证与门禁.md)。这里只写"怎么执行"。

> **本批交付的是编排与门禁，不是一次已完成的部署。**
> `biai-stage` 的安装、健康回读、Bot-Test 真实入口与切回旧 tag 演练**尚未执行**；`biplus-prod` 一切未动。生产部署另建独立 `[ops]` Issue，固定镜像 digest、备份点、执行窗口、观察期与回滚目标。

## 本版包含哪些进程

| 服务 | 形态 | 入口 | 说明 |
| --- | --- | --- | --- |
| `scheduler` | **常驻服务** | `python -m lingxi.apps.scheduler` | 专用授权凭据（「四达文档会议助手」`refresh_token`）到期轮换扫描 |
| `worker` | **一次性作业** | `python -m lingxi.apps.worker` | 单回合受控执行。跑一个 Agent SDK 回合、往 stdout 写一个 JSON 报告、退出 |
| `migrate` | **一次性作业** | `python -m alembic -c /opt/lingxi/alembic.ini` | 部署时跑一次的数据库迁移 |

**worker 是一次性作业，不是已上线的常驻服务。** 它不领任务、不接飞书、不处理 `SIGTERM`（`grep -rn signal src/lingxi/apps/worker/` 为空）。常驻 worker 需要任务队列与 gateway 入队，属 **S4 下半**，不在本批。`docker compose up -d` 只会启动 `scheduler` 一个进程。

`gateway`（S4 前半，[#57](https://github.com/Moshuiwang/lingxi/issues/57)）与 `admin` 进程**尚未建立**，因此没有为它们构建镜像——未实现的入口不得用占位进程冒充。gateway 落地时在 `compose.yaml` 里新增第三个 service，卷结构无需改动。

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

```bash
cp deploy/.env.example deploy/.env.stage    # 或 .env.prod，然后填真值
```

`.env.stage` / `.env.prod` 匹配 `.gitignore` 既有的 `.env.*` 规则，**不入库**；`scripts/ci/verify_repository.sh` 的敏感配置扫描也已覆盖它们。镜像里不预置任何凭据。

`LINGXI_POSTGRES_DSN` **必须带 `connect_timeout=5`**：`psycopg.connect()` 在代码里没有超时参数，libpq 默认无限等待，不设它就没有可计算的停止上界（见下方停止宽限期）。

## 安装与升级

```bash
# 1. 先跑迁移（一次性作业）
docker compose -f deploy/compose.yaml -f deploy/compose.stage.yaml \
  --profile job run --rm migrate

# 2. 再启动常驻服务
docker compose -f deploy/compose.yaml -f deploy/compose.stage.yaml up -d

# 3. 回读
docker compose -f deploy/compose.yaml -f deploy/compose.stage.yaml ps
docker compose -f deploy/compose.yaml -f deploy/compose.stage.yaml logs scheduler
```

生产把 `compose.stage.yaml` 换成 `compose.prod.yaml`，其余逐字相同——两份覆盖文件**结构完全一致**，只有 env_file、卷名与扫描间隔不同（`scripts/ci/verify_compose_structure.sh` 每次 CI 都比对这一点）。

**生产只拉镜像，不构建**：三个 compose 文件里没有任何 `build:` 键。这不是纪律而是机制。

## 回滚

```bash
# 把 LINGXI_IMAGE_TAG 改回上一个 tag，然后：
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d
```

回滚不触碰数据库，也不触碰两个持久卷。**前提是迁移遵守"先加后删"**：破坏性变更必须拆成两次发布，否则回滚就从"切 tag 重启"变成"恢复数据库备份"。这一条由 `scripts/ci/verify_old_image_new_schema.sh` 在每次 CI 上实测（断言 V-部署-05）。

## 恢复入口

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml down
docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml up -d
```

数据库备份与恢复遵循 Supabase 托管方案。两个持久卷单独备份。

## 两个持久卷

| 卷（stage / 生产） | 挂载点 | 内容 |
| --- | --- | --- |
| `lingxi-{stage,prod}-credentials` | `/var/lib/lingxi/credentials` | 专用授权凭据加密文件与锁文件 |
| `lingxi-{stage,prod}-users` | `/var/lib/lingxi/users` | 用户环境目录（用户产物与会话） |

**刻意是两个卷**：凭据是安全边界、用户目录是业务数据，备份周期、恢复策略与删除语义都不同，合成一个卷会让"只恢复凭据"或"只清用户目录"变成不可能。

凭据卷必须**跨部署持久**，镜像替换与重启不得丢失——部署目标是对该凭据「零特殊处理」。文件丢失或过期时的保底是重新走一次「四达文档会议助手」授权（产品负责人 2026-08-05）；正式重授权入口见 [#67](https://github.com/Moshuiwang/lingxi/issues/67)。

## scheduler 的停止宽限期为什么是 90 秒

Docker 默认 10 秒，**不满足**。`SIGKILL` 若落在"已经向飞书换过新凭据、尚未写回数据库"的窗口里，那条 `refresh_token` 就永久丢失了——飞书侧旧凭据在续期成功那一刻已作废，一次性有效，只能人工重新授权。

| 项 | 秒 | 依据 |
| --- | --- | --- |
| 续期 HTTP 超时 | 20.0 | `REQUEST_TIMEOUT_SECONDS`，`adapters/feishu_directory.py` |
| 落盘重试退避等待 | 4.2 | `SAVE_RETRY_BACKOFF_SECONDS=(0.2, 1.0, 3.0)`，`apps/scheduler/__init__.py` |
| 数据库往返预算 | 25.0 | 4 次 save 重试 + 1 次 revoke，每次按 `connect_timeout=5` 计 |
| **最坏合计** | **49.2** | × 1.5 安全系数 = 74 秒 → 取整到 **90 秒** |

**这个不等式由 `scripts/ci/check_deploy_contract.py` 自动守住，不是靠文档。** 改了 `REQUEST_TIMEOUT_SECONDS` 而不改 compose，门禁会红。

`scheduler` 必须**单副本**：进程间互斥靠的是凭据目录里的 `flock` 文件锁，多副本会互相阻塞。

## 本地验证

```bash
scripts/ci/build_image.sh scheduler                # 构建单个镜像
scripts/ci/verify_image_contract.sh <三个镜像引用>  # 镜像契约逐条核对
scripts/ci/verify_compose_structure.sh             # stage ↔ 生产结构对照
scripts/ci/verify_old_image_new_schema.sh          # V-部署-05：旧镜像在新库上启动
scripts/ci/verify_old_image_new_schema.sh --destructive   # 同上的变异对照，应判红
```

本机没有 buildx 也能跑：只用 plain `docker build` 支持的语法，不引入 BuildKit 专有语法或缓存后端。
