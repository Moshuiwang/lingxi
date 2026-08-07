# Lingxi 生产镜像（Issue #62 / S11）。一份 Dockerfile，三个构建目标：
#
#   --target scheduler   常驻服务：专用授权凭据续期扫描（python -m lingxi.apps.scheduler）
#   --target worker      一次性作业：单回合受控执行（python -m lingxi.apps.worker）
#   --target migrate     一次性作业：数据库迁移（python -m alembic -c ... upgrade head）
#
# **worker 不是常驻服务。** main 上的 worker 是单回合 CLI：读 LINGXI_WORKER_* 环境变量、
# 跑一个 Agent SDK 回合、往 stdout 写一个 JSON 报告、按 0/2/3/4/5 退出。它不领任务、
# 不接飞书、不处理 SIGTERM（`grep -rn signal src/lingxi/apps/worker/` 为空）。常驻 worker
# 需要任务队列与 gateway 入队，属 S4 下半，不在本批。因此 compose 里它是 `docker compose
# run` 调起的作业，绝不能配 restart 策略——那会把一个正常退出的作业变成无限重启循环。
#
# 只用 plain `docker build` 支持的语法：不使用 BuildKit 专有的 RUN --mount、heredoc 或
# 缓存后端。本机验收环境没有 buildx（2026-08-06 编排者裁定⑤），而"能不能构建"这件事
# 不该取决于构建机装了什么插件。

# 基础镜像按 **digest** 固定，不按 tag。`python:3.12-slim-bookworm` 是会移动的标签：
# 同一个提交今天和下周构建会拿到不同的底座，V-部署-06 的"两次构建等价"就成了运气。
# 这是 manifest list 的 digest，按构建平台解析出对应架构的镜像。
# 更新底座 = 改这一行 + 重跑镜像门禁，是一次有留痕的显式变更。
FROM python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS base

# 日志走 stdout / stderr 且不缓冲（断言 V-部署-04）。缓冲会让容器日志在崩溃时丢掉
# 最后一段——恰恰是最需要的那一段。
ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1

# 非 root 运行（断言 V-部署-07）。固定 uid/gid 而不是让系统分配：宿主机绑定挂载的
# 属主要能对上，浮动 uid 会让"换个机器部署就写不进卷"成为一种偶发故障。
# 10001 落在系统账号区间之外，不与基础镜像既有账号冲突。
#
# `useradd` 会往 /etc/shadow 的第 3 字段写「最后一次改密码的日子」——一个
# **days-since-epoch 整数**（实测建镜像当天是 20671）。它是构建时刻的函数，于是
# 同一个提交今天建和明天建会得到不同的 /etc/shadow，跨零点的两次构建内容不等价，
# V-部署-06 的"两次构建等价"就在无人察觉的情况下变成一条会偶发变红的断言。
# 把它归零：本镜像的账号不用密码登录（shell 是 nologin、密码位是 `!`），这个字段
# 没有任何运行期意义。（Issue #62 验收 P2-3）
RUN groupadd --gid 10001 lingxi \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lingxi \
 && sed -i 's/^\(lingxi:[^:]*:\)[^:]*:/\10:/' /etc/shadow

# 两个**不同的**持久卷挂载点（断言 V-部署-03 / M2-62-27 / M2-62-28）：
#   credentials/ 专用授权凭据（「四达文档会议助手」refresh_token 的加密文件与锁文件）。
#                必须跨部署持久：镜像替换与重启不得丢失，否则每次部署都要重新授权。
#   users/       用户环境目录（用户产物与会话），架构设计里挂在 worker 上。
# 刻意分成两个卷而不是一个：凭据是安全边界、用户目录是业务数据，备份周期、恢复策略
# 与删除语义都不同，合成一个卷会让"只恢复凭据"或"只清用户目录"变成不可能。
#
# 目录在镜像里就建好并 chown：Docker 创建**空的**具名卷时会把镜像里该路径的内容与
# 属主一并复制进去。不预建的话，卷会以 root:root 出现，非 root 进程写不进去——
# 而这个失败要到第一次真的轮换凭据时才暴露。
RUN mkdir -p /var/lib/lingxi/credentials /var/lib/lingxi/users \
 && chown -R 10001:10001 /var/lib/lingxi \
 && chmod 0700 /var/lib/lingxi/credentials

# ---------------------------------------------------------------------------
# 构建层：装依赖。三个进程各自一层，互不污染。
#
# 依赖按**进程**分组（pyproject.toml 的 optional-dependencies，Issue #56），
# 每个镜像只装它那一组：scheduler 镜像里没有 claude-agent-sdk，worker 镜像里没有
# psycopg，两者都没有 bot-test 那一组（lark-oapi / websockets 不进生产镜像）。
#
# `--no-compile` + 随后的 compileall 是**可复现性**要求，不是性能优化：
# pip 默认生成的 .pyc 用时间戳失效模式，文件头内嵌**源文件 mtime**。同一提交的两个
# clone 其源文件 mtime 是各自的 checkout 时间，于是两次构建的 .pyc 逐字节不同。
# `--invalidation-mode unchecked-hash`（PEP 552）改为内嵌源码哈希，与 mtime 无关，
# 两次构建得到完全相同的字节。镜像是只读交付物，不需要运行期失效检查。
# ---------------------------------------------------------------------------
FROM base AS build-base
WORKDIR /build
# 只 COPY 构建 wheel 真正需要的两样。`.dockerignore` 已排除 scripts/ tests/
# experiments/ workers/ docs/ 与 migrations/testing/，这里再按需精确取用。
COPY pyproject.toml /build/pyproject.toml
COPY src /build/src

# **交付层紧跟各自的构建层，且按代价从小到大排列。** 这不是风格问题：
# plain `docker build`（legacy builder）**不做阶段裁剪**，它线性执行到 `--target`
# 命名的那个阶段为止，途中每个阶段都真的构建。把三个构建层堆在前面、三个交付层
# 堆在后面时，`--target scheduler` 实测要 5 分 47 秒——因为它顺带把 worker 那 275 MB
# 也装了一遍。按当前顺序，`--target scheduler` 只走到第四个阶段。
# worker 排最后：它是唯一昂贵的一个（Agent SDK 的 wheel 内嵌 Claude Code CLI 二进制）。
# BuildKit 会自行裁剪无关阶段，因此这个顺序对它无影响，只是对 legacy 友好。

# ---------------------------------------------------------------------------
# 交付层只从构建层取 site-packages，不带构建上下文。最终 rootfs 里因此没有 /build、
# 没有源码树、没有 pyproject.toml——制品就是 `pip install` 装出来的那一份，与 CI 的
# `check_installed_package.py` 校验的是同一个形态（断言 V-部署-10：测试跑
# PYTHONPATH=src，部署跑已安装制品，两者会分叉，而分叉只在部署时暴露）。
# ---------------------------------------------------------------------------

FROM build-base AS build-scheduler
RUN python -m pip install --no-cache-dir --no-compile '.[scheduler]' \
 && python -m compileall -q -f --invalidation-mode unchecked-hash /usr/local/lib/python3.12/site-packages

FROM base AS scheduler
COPY --from=build-scheduler /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
# ---- 来源身份标签（Issue #62 codex 二轮 P1-2）--------------------------------
# 推送幂等**不能**拿 config digest 当身份：同一提交两次构建的 config 里 `created` 与
# `history` 时间戳必然不同（实测两次 --no-cache 构建得到两个不同的 Id），于是"远端已有
# 同名 tag"会被判成内容冲突，重跑永远卡死；一次部分推送失败之后就再也推不上去了。
#
# 身份改用**来源**：源提交 sha + 源码树哈希。两者对同一提交恒定，因此
#   - 重跑 → 同源 → 跳过该服务，继续推其余（部分失败可恢复）
#   - 真的换了源码 → 异源 → 拒绝，不可变 tag 不得被覆盖
# 标签值对同一提交恒定，所以它不破坏"两次构建内容等价"——两边标签一模一样。
ARG LINGXI_SOURCE_COMMIT=unknown
ARG LINGXI_SOURCE_TREE=unknown
LABEL org.opencontainers.image.revision="${LINGXI_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/Moshuiwang/lingxi" \
      com.moshuiwang.lingxi.source-tree="${LINGXI_SOURCE_TREE}"
USER 10001:10001
WORKDIR /var/lib/lingxi
# 常驻服务。停止语义见 deploy/compose.yaml 的 stop_grace_period：收到 SIGTERM 后
# 停止领取新的到期凭据、把在途的那一次轮换做完再退出。
CMD ["python", "-m", "lingxi.apps.scheduler"]

FROM build-base AS build-migrate
RUN python -m pip install --no-cache-dir --no-compile '.[migrate]' \
 && python -m compileall -q -f --invalidation-mode unchecked-hash /usr/local/lib/python3.12/site-packages

FROM base AS migrate
COPY --from=build-migrate /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
# ---- 来源身份标签（Issue #62 codex 二轮 P1-2）--------------------------------
# 推送幂等**不能**拿 config digest 当身份：同一提交两次构建的 config 里 `created` 与
# `history` 时间戳必然不同（实测两次 --no-cache 构建得到两个不同的 Id），于是"远端已有
# 同名 tag"会被判成内容冲突，重跑永远卡死；一次部分推送失败之后就再也推不上去了。
#
# 身份改用**来源**：源提交 sha + 源码树哈希。两者对同一提交恒定，因此
#   - 重跑 → 同源 → 跳过该服务，继续推其余（部分失败可恢复）
#   - 真的换了源码 → 异源 → 拒绝，不可变 tag 不得被覆盖
# 标签值对同一提交恒定，所以它不破坏"两次构建内容等价"——两边标签一模一样。
ARG LINGXI_SOURCE_COMMIT=unknown
ARG LINGXI_SOURCE_TREE=unknown
LABEL org.opencontainers.image.revision="${LINGXI_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/Moshuiwang/lingxi" \
      com.moshuiwang.lingxi.source-tree="${LINGXI_SOURCE_TREE}"
# 迁移随镜像进制品（migrations/README.md 的书面承诺，2026-08-06 裁定⑥）：
# 迁移作业不在生产机现场构建、不从仓库拉取，与业务进程用同一个镜像 tag，
# 因此"镜像 tag 即冻结版本"对迁移同样成立。
# `.dockerignore` 排除了 migrations/testing/（测试资产，不属于生产链）。
COPY alembic.ini /opt/lingxi/alembic.ini
COPY migrations /opt/lingxi/migrations
USER 10001:10001
WORKDIR /opt/lingxi
# `-c` 写**绝对路径**，不靠 WORKDIR 兜底。`python -m alembic` 不带 -c 时要求当前工作
# 目录含 alembic.ini；一旦调用方带了 `-w /` 或换了工作目录，迁移就会静默地找不到配置
# 或（更糟）找到别的配置。alembic.ini 里的 script_location 用 %(here)s，因此配置文件
# 一旦按绝对路径定位，revision 目录也随之确定，整条链与工作目录无关（断言 M2-62-22）。
ENTRYPOINT ["python", "-m", "alembic", "-c", "/opt/lingxi/alembic.ini"]
CMD ["upgrade", "head"]

FROM build-base AS build-gateway
RUN python -m pip install --no-cache-dir --no-compile '.[gateway]' \
 && python -m compileall -q -f --invalidation-mode unchecked-hash /usr/local/lib/python3.12/site-packages

FROM base AS gateway
COPY --from=build-gateway /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
ARG LINGXI_SOURCE_COMMIT=unknown
ARG LINGXI_SOURCE_TREE=unknown
LABEL org.opencontainers.image.revision="${LINGXI_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/Moshuiwang/lingxi" \
      com.moshuiwang.lingxi.source-tree="${LINGXI_SOURCE_TREE}"
USER 10001:10001
WORKDIR /var/lib/lingxi
# 常驻服务：飞书长连接接入，把事件落库成任务（#57 / S4 前半）。
#
# **本镜像存在 ≠ gateway 可以上线。** 它入队之后没有消费者：worker 目前是单回合 CLI，
# 不领任务。所以 compose 把它放在一个**非默认 profile** 里，`up -d` 不会启动它——
# 详见 deploy/compose.yaml 的说明。
#
# 停机语义（V-部署-03，#57 同型认领）：收到 SIGTERM 后停止接收新事件、把在途事件
# 落库完成再退出，不留"抢占了话题但任务没写进去"的中间态。
CMD ["python", "-m", "lingxi.apps.gateway"]

FROM build-base AS build-worker
RUN python -m pip install --no-cache-dir --no-compile '.[worker]' \
 && python -m compileall -q -f --invalidation-mode unchecked-hash /usr/local/lib/python3.12/site-packages

FROM base AS worker
COPY --from=build-worker /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
# ---- 来源身份标签（Issue #62 codex 二轮 P1-2）--------------------------------
# 推送幂等**不能**拿 config digest 当身份：同一提交两次构建的 config 里 `created` 与
# `history` 时间戳必然不同（实测两次 --no-cache 构建得到两个不同的 Id），于是"远端已有
# 同名 tag"会被判成内容冲突，重跑永远卡死；一次部分推送失败之后就再也推不上去了。
#
# 身份改用**来源**：源提交 sha + 源码树哈希。两者对同一提交恒定，因此
#   - 重跑 → 同源 → 跳过该服务，继续推其余（部分失败可恢复）
#   - 真的换了源码 → 异源 → 拒绝，不可变 tag 不得被覆盖
# 标签值对同一提交恒定，所以它不破坏"两次构建内容等价"——两边标签一模一样。
ARG LINGXI_SOURCE_COMMIT=unknown
ARG LINGXI_SOURCE_TREE=unknown
LABEL org.opencontainers.image.revision="${LINGXI_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/Moshuiwang/lingxi" \
      com.moshuiwang.lingxi.source-tree="${LINGXI_SOURCE_TREE}"
# `useradd --no-create-home` 仍会把 HOME 设成 /home/lingxi，但**那个目录并不存在**，
# 而 compose 给 worker 的是只读根文件系统——Claude Code CLI 与 MCP 子进程往 $HOME
# 写配置或会话时会直接失败（实测：`touch $HOME/x` 报 No such file or directory）。
# 指到 /tmp：compose 给它挂了 tmpfs，可写、随容器消失，天然满足"除用户环境目录外
# 不写需要持久化的本地状态"（V-部署-02）。
# **会话不在这里持久化**——worker 本就是单回合作业；跨回合的用户产物走
# LINGXI_WORKER_WORKSPACE 指向的 /var/lib/lingxi/users 持久卷。
# CLI 在只读根 + tmpfs HOME 下的真实行为属 stage 验证项，见 deploy/README.md。
ENV HOME=/tmp
USER 10001:10001
WORKDIR /var/lib/lingxi
# 一次性作业，不是服务：跑完一个回合就退出。调用方按退出码判定
# （0 正常收口 / 2 未收口 / 3 配置错误 / 4 会话失败 / 5 屏障被绕过）。
#
# 这个镜像**不需要 Node.js**。`claude-agent-sdk==0.2.128` 的 wheel 是平台专属的
# （manylinux_2_17_x86_64），内嵌了 Claude Code CLI 可执行文件
# （`claude_agent_sdk/_bundled/claude`，CLI 版本 2.1.220，约 275 MB）。SDK 运行时
# 优先用这个内嵌副本，找不到才回落到 `shutil.which("claude")` 并提示 npm 安装。
# 两个由此产生的硬约束，已写进 scripts/ci/verify_image_contract.sh 的断言：
#   1. 底座必须是 **glibc**：内嵌 CLI 是动态链接的 ELF，解释器为
#      /lib64/ld-linux-x86-64.so.2。换成 alpine（musl）会装得上、跑不起来。
#   2. 构建平台必须与运行平台一致：pip 按构建机架构挑 wheel，跨架构构建会得到
#      另一个（或没有）内嵌 CLI。
CMD ["python", "-m", "lingxi.apps.worker"]
