"""``python -m lingxi.apps.healthcheck``：容器内自我健康检查入口（Issue #153）。

**不开放任何入站端口**（合同第 5 条："healthcheck 使用进程/数据库心跳或受控命令，
不为健康检查扩大网络攻击面"）——本命令由 Docker Compose 的 ``healthcheck.test``
以 ``docker exec`` 语义在同一个容器内执行，与被检查进程共享同一份文件系统与网络
命名空间，不需要监听端口，也不产生任何新的入站面。

两段独立判定，**都要过**才算健康：

1. **依赖可达**：用与业务代码同一个连接工厂（``lingxi.adapters.postgres.connect``，
   带同一套受限超时）尝试连接数据库并跑一条 ``SELECT 1``。数据库不可用时——
   无论是网络问题、凭据错误还是数据库本身宕机——这一步必然如实失败，不存在
   "假健康"的空间（合同第 5 条："依赖（数据库等）不可用时健康检查必须如实
   变红"）。
2. **主循环仍在跳动**：读取 ``lingxi.apps.liveness`` 写下的活性文件，年龄超过
   阈值即判不健康。这一段单独存在的理由：只测依赖可达测不出"进程 PID 还在、
   数据库也连得上，但主循环因为一次未捕获异常或死锁已经停止消费"——这正是
   `healthcheck 只能证明 PID 存活时不接受为完成` 这条要求要挡住的假健康形状。

任何一段失败都以非零退出码结束，原因写到 stderr（不写业务正文，只写类别与
耗时/年龄这类低敏诊断信息）。

## 依赖可达判定的成本与缓存（Issue #409）

每次探测都是 ``docker exec`` 起一个全新 Python 进程；``import psycopg`` 本身
（拉 ``asyncio``/``logging``/``importlib.metadata``/C 扩展 ``psycopg_binary``
等一整条依赖链）在本机实测约 **450ms**，是单次探测里压倒性的主要开销——冷
解释器启动只有约 20-30ms，``argparse`` 只有约 11ms，两者都不是主因（Issue
#409 root cause 取证）。这笔成本不能靠"精简 import"消掉：只要每次探测都要
真的证明数据库可达，就绕不开这次 import 与随之而来的新建连接。

因此依赖可达这一段判定引入一层**有效期极短的成功结果缓存**：一次真实探测
成功后，把结果戳进 ``{role}-db`` 这个活性文件键（复用 ``apps/liveness`` 的
通用机制，与"主循环是否跳动"用的活性文件键各自独立、不冲突）；只要缓存年龄
未超过 ``db_cache_ttl_seconds``，后续探测直接信任缓存、跳过 ``import
psycopg`` 与新建连接这整段开销。**缓存只在成功时写入**：一旦真实探测失败，
缓存不刷新，之后每一轮都会立刻重新做真实探测直到恢复——不存在"故障期间反而
不再检查"的空间。

**缓存不弱化"依赖不可达必须如实变红"这条硬约束，只是把发现窗口从"每一轮都
探测"改成"每一轮都可能探测，最坏情况下最多晚 ``db_cache_ttl_seconds`` 再加上
一次网格量化余量"**——与既有的"主循环活性阈值"是同一种权衡（那也是"最多容忍
一段陈旧"而不是"每次都重新证明"）。每个角色的 ``db_cache_ttl_seconds`` 默认值
由 :func:`_compute_db_cache_ttl_seconds` 按 ``活性阈值 − 2 × 检查间隔`` 算出
（下限 0 即禁用缓存），使得"依赖不可达"新的最坏发现时延仍然低于既有的"主循环
停摆"最坏发现时延——**旧版本按活性阈值固定 80% 折算，撞上了"网格量化"缺陷**
（独立审查 P1-4：gateway 的 TTL 24s 只比自己的检查间隔 23s 多 1 秒，实际最坏
发现时延是 145s 而不是天真假设的 123s，超过了同角色 B 类的 129s，打破了"A 类
不超过 B 类"这条不变量），详见 :func:`_compute_db_cache_ttl_seconds` 文档字符串
的完整推导。不改变 ``deploy/监控告警.md``「五、时延估算」表格里已经登记的、以
主循环停摆为准的端到端最坏时延结论，该文件已按新公式重新推导缓存 TTL 与
"依赖不可达"分项数字（该文件是时延数值的唯一事实源，本模块不复制推导）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from lingxi.apps.liveness import read_liveness_age_seconds, touch_liveness

# 三个进程读数据库连接串用的环境变量名不统一（scheduler/worker 用不带前缀的
# LINGXI_POSTGRES_DSN；gateway 的配置整体加了 LINGXI_GATEWAY_ 前缀，见
# apps/gateway/config.py 的 ENV_PREFIX）。healthcheck 与业务代码一样按角色映射，
# 不引入第三套变量名。
_DSN_ENV_VAR_BY_ROLE: Mapping[str, str] = {
    "scheduler": "LINGXI_POSTGRES_DSN",
    "worker": "LINGXI_POSTGRES_DSN",
    "gateway": "LINGXI_GATEWAY_POSTGRES_DSN",
}

# 各角色的默认最大心跳年龄（秒）：与各自主循环轮询间隔的量级匹配，不是同一个
# 数字硬套三个进程。scheduler 默认 60s 一轮，worker 默认 2s 一轮（`process_once`
# 每轮都会 touch），gateway 的投递消费循环默认 1s 一轮；三者都留出充分的连续
# 错过轮次余量，避免正常抖动触发误报，同时仍然能在数分钟内发现真正的停摆。
_DEFAULT_MAX_LIVENESS_AGE_SECONDS: Mapping[str, float] = {
    "scheduler": 180.0,
    "worker": 60.0,
    "gateway": 30.0,
}

# 三个角色对应的 docker healthcheck.interval（`deploy/compose.yaml`，与 TTL
# 公式同源登记——改这里任何一个数字，必须同步改 compose.yaml 里对应服务的
# `healthcheck.interval`，反之亦然；没有自动化门禁守着这条一致性，是人工核对
# 纪律，见 `deploy/监控告警.md`「五、时延估算」同一条既有纪律）：
#   scheduler   30s（deploy/compose.yaml 的 scheduler.healthcheck.interval）
#   worker      29s（deploy/compose.yaml 的 worker-queue.healthcheck.interval
#               ——compose 服务名是 worker-queue，healthcheck 命令传的
#               `--role worker`，见该服务定义旁注）
#   gateway     23s（deploy/compose.yaml 的 gateway.healthcheck.interval）
_HEALTHCHECK_INTERVAL_SECONDS_BY_ROLE: Mapping[str, float] = {
    "scheduler": 30.0,
    "worker": 29.0,
    "gateway": 23.0,
}


def _compute_db_cache_ttl_seconds(role: str) -> float:
    """依赖可达判定的成功结果缓存有效期公式（P1-4，独立审查修订；Issue #409
    原始设计见模块说明「依赖可达判定的成本与缓存」）。

    **旧公式（按活性阈值的固定 80% 折算）撞上了"网格量化"缺陷**：旧值对
    gateway 是 24s——只比它自己的检查间隔 23s 多 1 秒。缓存是否过期只在
    healthcheck 自己被调用的那个离散节拍上被"发现"，不是过期那一刻就立刻触发
    真实探测：缓存在 t=0 被刷新后，下一次调用发生在 t=23（此时年龄 23s ≤
    TTL 24s，仍命中缓存，跳过真实探测），再下一次在 t=46（此时年龄才终于超过
    24s，触发第一次真实探测）——TTL 只比 interval 大 1 秒，换来的却是几乎整整
    多等一个 interval（46s 而不是天真假设的 24s）才开始重试。叠加上后续
    `retries × (interval + timeout)` = 99s，gateway 的 A 类最坏时延实测应为
    46+99=145s，超过了同角色 B 类的 129s——直接打破"A 类不超过 B 类"这条
    `deploy/监控告警.md` 表格结论依赖的不变量（独立审查 P1-4 发现）。

    **新公式**：``TTL = liveness_max_age − 2 × interval``，下限 0（=禁用
    缓存，见 `_check_database` 里 `db_cache_ttl_seconds <= 0` 分支——禁用后
    每一轮都做真实探测，退化成没有 TTL 项的 `retries × (interval + timeout)`，
    比任何非零 TTL 都更快发现故障，不需要单独验证安全性）。选"减两倍
    interval"而不是一倍：网格量化让"缓存过期后第一次真实探测"最坏要等
    `TTL + interval`（`ceil(TTL/interval)*interval < TTL + interval`，是比精确
    值更宽松也更容易人工验证的安全上界），要让新 A 类公式
    `(TTL + interval) + retries × (interval + timeout)` 不超过 B 类
    `liveness_max_age + retries × (interval + timeout)`，只需要
    `TTL + interval ≤ liveness_max_age`，即 `TTL ≤ liveness_max_age − interval`
    ——减两倍 interval 比这个必要条件更保守，多留出整整一个 interval 的显式
    安全余量，不是刚好卡线。三个角色实际算出：scheduler 180−60=120s，
    worker 60−58=2s（interval 58s 已经逼近活性阈值，缓存收益本就微乎其微），
    gateway 30−46<0 → 0（禁用）。`deploy/监控告警.md`「五、时延估算」按这个
    新公式重新推导过全部数字，与这里逐一对账；`tests/test_liveness_and_
    healthcheck.py` 的 `HealthcheckTtlLatencyContractTests` 把"各角色
    TTL 情形下的 A 类最坏时延 ≤ B 类最坏时延"钉成断言，回归时会在测试阶段
    炸掉，不必等到真实环境才发现下一次类似的网格量化缺陷。
    """

    liveness_max_age = _DEFAULT_MAX_LIVENESS_AGE_SECONDS[role]
    interval = _HEALTHCHECK_INTERVAL_SECONDS_BY_ROLE[role]
    return max(0.0, liveness_max_age - 2 * interval)


_DEFAULT_DB_CACHE_TTL_SECONDS: Mapping[str, float] = {
    role: _compute_db_cache_ttl_seconds(role) for role in _DEFAULT_MAX_LIVENESS_AGE_SECONDS
}

# gateway 一个进程里跑两条独立循环（长连接主线程、投递消费后台线程，见
# apps/gateway/__init__.py 模块说明），任一条停摆都是"进程活着但消费停止"的
# 真实形状——只测其中一条会让另一条静默死亡时仍然报健康。因此 gateway 对应
# **两个**活性文件键，都必须新鲜；scheduler/worker 各自只有一条主循环。
_LIVENESS_KEYS_BY_ROLE: Mapping[str, tuple[str, ...]] = {
    "scheduler": ("scheduler",),
    "worker": ("worker",),
    "gateway": ("gateway-longconn", "gateway-delivery"),
}


class HealthcheckError(RuntimeError):
    """健康检查判定失败；``reason`` 是安全的分类文本，不含业务正文或凭据。"""


def _db_cache_liveness_key(role: str) -> str:
    return f"{role}-db"


def _check_database(
    role: str,
    db_cache_ttl_seconds: float,
    env: Mapping[str, str],
    *,
    directory: Path | None = None,
) -> None:
    # DSN 校验永远先做、永远不受缓存影响：这是免费的输入校验，不是"证明可达"
    # 本身要缓存的那部分开销，且必须在任何环境下都能准确报告"读错了哪个变量名"
    # （不能被一份恰好新鲜的缓存戳悄悄掩盖）。
    dsn_var = _DSN_ENV_VAR_BY_ROLE[role]
    dsn = (env.get(dsn_var) or "").strip()
    if not dsn:
        raise HealthcheckError(f"缺少数据库连接串环境变量 {dsn_var}")

    cache_key = _db_cache_liveness_key(role)
    # `db_cache_ttl_seconds <= 0` 显式判"永不信任缓存"，不依赖"age > 0"这类
    # 计时巧合——两次探测理论上可能落在同一个墙钟秒内，`age` 会精确为 0.0。
    if db_cache_ttl_seconds > 0:
        cached_age = read_liveness_age_seconds(cache_key, directory=directory)
        if cached_age is not None and cached_age <= db_cache_ttl_seconds:
            # 近期已经真实证明过可达，本轮跳过——省下的正是 `import psycopg`
            # 这一整条依赖链（模块说明「依赖可达判定的成本与缓存」实测约
            # 450ms）。
            return

    from lingxi.adapters.postgres import connect

    try:
        with connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
    except Exception as error:  # noqa: BLE001 - 健康检查只需要区分"能不能连上"
        raise HealthcheckError(f"数据库不可达：{type(error).__name__}") from error
    # 只在真正探测成功时刷新缓存——探测失败绝不写入，保证故障期间每一轮都会
    # 重新做真实探测，直到真的恢复为止。
    touch_liveness(cache_key, directory=directory)


def _check_liveness(
    role: str,
    max_age_seconds: float,
    env: Mapping[str, str],
    *,
    directory: Path | None = None,
) -> None:
    keys = _LIVENESS_KEYS_BY_ROLE[role]
    if role == "gateway" and (env.get("LINGXI_GATEWAY_TENANT_DOMAIN") or "").strip():
        # 文档投递独立消费循环（Issue #341 S-ES-3，P3 顺手）是**可选**的第三条
        # gateway 线程——只在配置了 `LINGXI_GATEWAY_TENANT_DOMAIN` 时才会被装配
        # 并起跑（`assemble_document_delivery_consumer`，见该函数文档）。因此不
        # 能像 `gateway-longconn`/`gateway-delivery` 那样无条件加进
        # `_LIVENESS_KEYS_BY_ROLE`——没配这项能力的 gateway 部署永远不会写这个
        # 活性文件，无条件检查会让它们的健康检查恒为不健康。只在**这个进程自己
        # 的配置**表明它应该起这条线程时，才把它纳入检查——与业务代码判断"要不要
        # 装配"用的是同一个变量，healthcheck 不构造完整 `GatewayConfig`（避免
        # 拖进飞书凭据校验等与健康检查无关的前置），直接读这一个变量足够。
        keys = keys + ("gateway-document-delivery",)
    for key in keys:
        age = read_liveness_age_seconds(key, directory=directory)
        if age is None:
            raise HealthcheckError(f"没有找到活性文件 {key}（进程可能尚未完成首轮启动）")
        if age > max_age_seconds:
            raise HealthcheckError(
                f"{key} 活性年龄 {age:.1f}s 超过阈值 {max_age_seconds:.1f}s，"
                "进程可能仍存活但已停止消费"
            )


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stderr: object = None,
) -> int:
    import os

    parser = argparse.ArgumentParser(prog="python -m lingxi.apps.healthcheck")
    parser.add_argument("--role", required=True, choices=sorted(_DSN_ENV_VAR_BY_ROLE))
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
        help="主循环活性文件的最大可接受年龄（秒）；缺省按角色取合理默认值",
    )
    parser.add_argument(
        "--db-cache-ttl-seconds",
        type=float,
        default=None,
        help=(
            "依赖可达判定的成功结果缓存有效期（秒，Issue #409）；缺省按角色取"
            "合理默认值。0 等价于关闭缓存，每轮都做真实探测"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    source = os.environ if env is None else env
    err = sys.stderr if stderr is None else stderr
    max_age = args.max_age_seconds
    if max_age is None:
        max_age = _DEFAULT_MAX_LIVENESS_AGE_SECONDS[args.role]
    db_cache_ttl = args.db_cache_ttl_seconds
    if db_cache_ttl is None:
        db_cache_ttl = _DEFAULT_DB_CACHE_TTL_SECONDS[args.role]

    # P1-6（独立审查）：活性/DB 缓存目录必须从传入的 `env`（而不是真实进程
    # `os.environ`）解出来，再显式往下传——`_check_database`/`_check_liveness`
    # 内部调用 `lingxi.apps.liveness` 时若不传 `directory`，那两个函数会各自
    # 再退回去读一次真实 `os.environ.get("LINGXI_LIVENESS_DIR")`，完全绕开这里
    # 已经拿到的 `env=`。调用方（测试、或未来任何想要隔离环境跑一次判定的场景）
    # 传入的 `env` 因此会被静默忽略——`env=` 里的其它变量（DSN 等）确实生效，
    # 唯独这一个目录变量不生效，容易造成"以为传了隔离环境，实际上仍在读写真实
    # /tmp"的错觉（真实生产路径 `env=None` 时 `source is os.environ`，这里算出
    # 的目录与此前隐式读取到的值逐字相同，行为不变）。
    liveness_dir_override = (source.get("LINGXI_LIVENESS_DIR") or "").strip()
    directory = Path(liveness_dir_override) if liveness_dir_override else None

    started = time.monotonic()
    try:
        _check_database(args.role, db_cache_ttl, source, directory=directory)
        _check_liveness(args.role, max_age, source, directory=directory)
    except HealthcheckError as error:
        print(f"unhealthy role={args.role} reason={error}", file=err)
        return 1
    elapsed = time.monotonic() - started
    print(f"healthy role={args.role} checked_in={elapsed:.3f}s", file=err)
    return 0


def main() -> int:  # pragma: no cover - 由 __main__.py 与真实 CLI 调用
    return run()
