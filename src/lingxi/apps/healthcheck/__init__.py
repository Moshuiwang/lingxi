"""``python -m lingxi.apps.healthcheck``：容器内自我健康检查入口。

不开放任何入站端口：本命令由 Docker Compose 以 ``docker exec`` 语义在
同一容器内执行，共享文件系统与网络命名空间。四段独立判定都要过才算
健康，按诊断成本从低到高排序：临时目录还写得进去（盘满会污染后面两段
的信号）；cgroup ``pids`` 配额还起得来子进程；依赖（数据库）可达；
主循环仍在跳动（只测依赖可达测不出主循环已死锁停止消费）。任何一段
失败都以非零退出码结束，原因写到 stderr。单独判可用空间：活性文件是
覆盖写的十几字节小文件，盘 100% 满时它照样成功，会让磁盘写满的真实
故障被一路报绿；阈值按比例而非固定字节，另设绝对上界避免开发机误红。
依赖可达判定引入极短有效期的成功结果缓存（``import psycopg`` 是单次
探测的主要开销），只在探测成功时写入、失败绝不刷新。TTL 公式见
:func:`_compute_db_cache_ttl_seconds`；不改变
``deploy/监控告警.md``「五、时延估算」的结论，本模块不复制推导。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from lingxi.apps.liveness import liveness_path, read_liveness_age_seconds, touch_liveness

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

# 三个角色对应的 docker healthcheck.interval（`deploy/compose.yaml`）：改这里
# 任何数字必须同步改 compose.yaml 对应服务的 interval，反之亦然——没有自动化
# 门禁守着这条一致性，是人工核对纪律。worker 的 compose 服务名是
# worker-queue，healthcheck 命令传 `--role worker`。
_HEALTHCHECK_INTERVAL_SECONDS_BY_ROLE: Mapping[str, float] = {
    "scheduler": 30.0,
    "worker": 29.0,
    "gateway": 23.0,
}


def _compute_db_cache_ttl_seconds(role: str) -> float:
    """依赖可达判定的成功结果缓存有效期公式：``TTL = liveness_max_age − 2 × interval``，下限 0（=禁用缓存）。

    减两倍 interval 而非一倍：
    健康检查只在自己被调用的离散节拍上发现缓存过期，最坏情况下还要再
    等一个 interval 才会触发真实探测，减一倍刚好卡线，减两倍才留出
    显式安全余量，保证"依赖不可达"的最坏发现时延不超过"主循环停摆"
    的最坏发现时延——由 ``HealthcheckTtlLatencyContractTests`` 钉成
    断言，``deploy/监控告警.md``「五、时延估算」登记了每个角色的数字。
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

# ---- 临时目录可用空间阈值 ----------------------------------------------------
#: 可用空间低于总量的这个比例即判不健康。10% 对 worker/worker-queue 的 256MB
#: 内存盘约 25.6MiB，对 scheduler/gateway 那块 16MB 的盘约 1.6MiB，都够写活性
#: 文件与临时文件；与 `apps/worker/session_cleanup.py` 的回收预算隔着一个
#: 数量级，正常运行绝不会贴线。
_DEFAULT_MIN_FREE_RATIO = 0.10
#: 可用空间达到这个绝对值时一律判够用，不再看比例。开发机 / CI 的 `/tmp` 常常落在
#: 几百 GB 的根文件系统上，"可用不足 10%" 在那里是常态而不是故障；容器里的 tmpfs
#: 上限只有 16m/256m，永远够不到这个数，因此这条上界只影响非容器场景，不会让容器
#: 里真实的写满逃过判定。传 ``0`` 显式关掉这条上界（一切以比例为准）。
_SUFFICIENT_FREE_BYTES = 512 * 1024 * 1024
#: 只用于把字节数渲染成人读得懂的 MiB，不参与任何判定。
_MIB = 1024 * 1024

#: 进程数占 cgroup ``pids`` 上限的这个比例即判不健康。打满之后的现场极难认：
#: `docker exec` 仍进得去、健康检查也一路报绿（它自己不 fork），但 worker
#: 起 Claude CLI 与 MCP 子进程会直接失败——判的是这条资源本身还剩多少，不是
#: 这次操作成不成功。0.90 的余量：正常峰值远低于上限，贴到 90% 说明有东西在
#: 漏进程，此刻判红仍来得及重启。
_DEFAULT_MAX_PIDS_RATIO = 0.90

#: cgroup v2 与 v1 的 pids 控制器读数路径。容器内看到的是自己那一层的命名空间视图。
#: 顺序即优先级：先 v2（本仓库部署的 Docker 29.x 是 v2），再 v1。
_PIDS_CGROUP_PATHS: tuple[tuple[Path, Path], ...] = (
    (Path("/sys/fs/cgroup/pids.current"), Path("/sys/fs/cgroup/pids.max")),
    (Path("/sys/fs/cgroup/pids/pids.current"), Path("/sys/fs/cgroup/pids/pids.max")),
)


class HealthcheckError(RuntimeError):
    """健康检查判定失败；``reason`` 是安全的分类文本，不含业务正文或凭据。"""


def _db_cache_liveness_key(role: str) -> str:
    return f"{role}-db"


def _check_free_space(
    role: str,
    *,
    directory: Path | None = None,
    min_free_ratio: float = _DEFAULT_MIN_FREE_RATIO,
    sufficient_free_bytes: int = _SUFFICIENT_FREE_BYTES,
    statvfs: Callable[[str], os.statvfs_result] | None = None,
) -> None:
    """判定活性文件所在的那块盘（容器里就是 ``/tmp`` 那块 tmpfs）还写得进去。

    看的是这块盘本身而不是某个文件写得成不成——活性文件是十几字节的
    覆盖写，盘 100% 满时它照样成功（模块文档「单独判可用空间」有细节）。
    判红条件是"可用比例低于阈值"且"可用绝对值也不够多"：前者让阈值随
    各服务的 tmpfs 上限缩放，后者避免在开发机误红。``statvfs`` 本身
    失败（目录不存在、盘不可读）同样判不健康：连自己的临时目录都 stat
    不了的容器不该被称作健康。
    """
    # `os.statvfs` 现取而不是绑成默认参数：默认参数在函数定义那一刻就求值，
    # 之后任何替换（测试注入、平台适配）都不会生效，那正是"注入了却没生效"这类
    # 假绿的来源。
    probe = statvfs if statvfs is not None else os.statvfs
    path = liveness_path(role, directory=directory).parent
    try:
        stats = probe(str(path))
    except OSError as error:
        raise HealthcheckError(f"临时目录不可用：{type(error).__name__}") from error
    total = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    if total <= 0:
        # 读得到但总量为 0：无法据此判定，不假装健康也不编一个阈值。
        raise HealthcheckError("临时目录容量读数为 0，无法判定可用空间")
    if sufficient_free_bytes > 0 and free >= sufficient_free_bytes:
        # `<= 0` 显式表示"关掉这条绝对上界，一切以比例为准"——不是"0 字节也算够"。
        return
    threshold = min_free_ratio * total
    if free < threshold:
        raise HealthcheckError(
            f"临时目录可用空间 {free / _MIB:.1f}MiB 低于阈值 "
            f"{threshold / _MIB:.1f}MiB（总量 {total / _MIB:.1f}MiB，"
            f"下限比例 {min_free_ratio:.0%}）"
        )


def _read_pids_usage(cgroup_paths: Sequence[tuple[Path, Path]]) -> tuple[int, int] | None:
    """读 cgroup 的 ``(pids.current, pids.max)``；读不出可判定的数就返回 ``None``。

    返回 ``None`` 的三种情况都不判红：文件不存在（非容器环境）、
    ``pids.max`` 是 ``"max"``（没设上限）、读到的内容不是正整数。这与
    ``_check_free_space`` 对 ``statvfs`` 失败判红的取舍刻意不同：临时
    目录必然存在，stat 不了就是真出事；cgroup pids 控制器在容器外根本
    不存在，判成不健康只是噪声。
    """
    for current_path, max_path in cgroup_paths:
        try:
            current_raw = current_path.read_text(encoding="utf-8").strip()
            max_raw = max_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if max_raw == "max":
            return None
        try:
            current = int(current_raw)
            limit = int(max_raw)
        except ValueError:
            return None
        if limit <= 0 or current < 0:
            return None
        return current, limit
    return None


def _check_pids(
    role: str,
    *,
    max_pids_ratio: float = _DEFAULT_MAX_PIDS_RATIO,
    cgroup_paths: Sequence[tuple[Path, Path]] | None = None,
) -> None:
    """判定这个容器的 ``pids`` 配额还起得来子进程（报告 R6-D3）。

    ``role`` 只进错误文本，不参与判定：六个服务用同一条比例判据，因为
    ``pids`` 上限本来就逐服务配置，比例天然按各自的上限缩放（同
    ``_check_free_space`` 用比例而不是固定字节的理由）。
    """
    usage = _read_pids_usage(_PIDS_CGROUP_PATHS if cgroup_paths is None else cgroup_paths)
    if usage is None:
        return
    current, limit = usage
    if max_pids_ratio <= 0:
        # 显式关掉这条判据（`--max-pids-ratio 0`），用于确实不想要它的场景。
        return
    if current >= max_pids_ratio * limit:
        raise HealthcheckError(
            f"进程数 {current} 已达 pids 上限 {limit} 的 "
            f"{current / limit:.0%}（阈值 {max_pids_ratio:.0%}）："
            "此刻起不了新的 CLI 或 MCP 子进程，用户任务会失败"
        )


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
    except Exception as error:  # 健康检查只需要区分"能不能连上"
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
        # 文档投递独立消费循环是可选的第三条 gateway 线程，只在配置了
        # LINGXI_GATEWAY_TENANT_DOMAIN 时才会被装配并起跑。不能无条件加进
        # _LIVENESS_KEYS_BY_ROLE：没配这项能力的部署永远不会写这个活性文件，
        # 无条件检查会让它们恒为不健康。只在这个进程自己的配置表明它该起这条
        # 线程时才纳入检查，直接读这一个变量足够，不构造完整 GatewayConfig。
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


def _build_parser() -> argparse.ArgumentParser:
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
            "依赖可达判定的成功结果缓存有效期（秒）；缺省按角色取合理默认值。"
            "0 等价于关闭缓存，每轮都做真实探测"
        ),
    )
    parser.add_argument(
        "--min-free-ratio",
        type=float,
        default=None,
        help=(
            "临时目录（活性文件所在盘）可用空间的下限比例；缺省 "
            f"{_DEFAULT_MIN_FREE_RATIO:.0%}。按比例而不是固定字节，因为六个服务的 "
            "tmpfs 上限本就不同"
        ),
    )
    parser.add_argument(
        "--sufficient-free-bytes",
        type=int,
        default=None,
        help=(
            "可用空间达到这个绝对值即判够用、不再看比例；缺省 "
            f"{_SUFFICIENT_FREE_BYTES // (1024 * 1024)}MiB，0 表示关掉这条上界"
        ),
    )
    parser.add_argument(
        "--max-pids-ratio",
        type=float,
        default=None,
        help=(
            "进程数占 cgroup pids 上限的比例，达到即判不健康；"
            f"缺省 {_DEFAULT_MAX_PIDS_RATIO:.0%}，0 表示关掉这条判据。"
            "非容器环境读不到 cgroup 时本判据自动跳过"
        ),
    )
    return parser


class _ResolvedOptions(NamedTuple):
    """命令行参数与按角色默认值合并后最终生效的判定参数。"""

    max_age: float
    db_cache_ttl: float
    min_free_ratio: float
    sufficient_free_bytes: int
    max_pids_ratio: float


def _resolve_options(args: argparse.Namespace) -> _ResolvedOptions:
    return _ResolvedOptions(
        max_age=(
            args.max_age_seconds
            if args.max_age_seconds is not None
            else _DEFAULT_MAX_LIVENESS_AGE_SECONDS[args.role]
        ),
        db_cache_ttl=(
            args.db_cache_ttl_seconds
            if args.db_cache_ttl_seconds is not None
            else _DEFAULT_DB_CACHE_TTL_SECONDS[args.role]
        ),
        min_free_ratio=(
            args.min_free_ratio if args.min_free_ratio is not None else _DEFAULT_MIN_FREE_RATIO
        ),
        sufficient_free_bytes=(
            args.sufficient_free_bytes
            if args.sufficient_free_bytes is not None
            else _SUFFICIENT_FREE_BYTES
        ),
        max_pids_ratio=(
            args.max_pids_ratio if args.max_pids_ratio is not None else _DEFAULT_MAX_PIDS_RATIO
        ),
    )


def _resolve_directory(source: Mapping[str, str]) -> Path | None:
    """活性/DB 缓存目录必须从传入的 ``source``（而不是真实进程 ``os.environ``）解出来，再显式下传给各判定函数，否则调用方传入的隔离环境会被静默忽略（真实生产路径行为不变）。"""
    liveness_dir_override = (source.get("LINGXI_LIVENESS_DIR") or "").strip()
    return Path(liveness_dir_override) if liveness_dir_override else None


def _run_checks(
    role: str, options: _ResolvedOptions, source: Mapping[str, str], directory: Path | None
) -> None:
    """依次跑四段判定，按诊断成本从低到高排序；任一段失败即抛 ``HealthcheckError``。"""
    _check_free_space(
        role,
        directory=directory,
        min_free_ratio=options.min_free_ratio,
        sufficient_free_bytes=options.sufficient_free_bytes,
    )
    _check_pids(role, max_pids_ratio=options.max_pids_ratio)
    _check_database(role, options.db_cache_ttl, source, directory=directory)
    _check_liveness(role, options.max_age, source, directory=directory)


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stderr: object = None,
) -> int:
    """跑一轮健康检查，打印结果并返回 CLI 退出码（0=健康，1=不健康）。"""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    source = os.environ if env is None else env
    err = sys.stderr if stderr is None else stderr
    options = _resolve_options(args)
    directory = _resolve_directory(source)

    started = time.monotonic()
    try:
        _run_checks(args.role, options, source, directory)
    except HealthcheckError as error:
        print(f"unhealthy role={args.role} reason={error}", file=err)
        return 1
    elapsed = time.monotonic() - started
    print(f"healthy role={args.role} checked_in={elapsed:.3f}s", file=err)
    return 0


def main() -> int:  # pragma: no cover - 由 __main__.py 与真实 CLI 调用
    """入口封装，交给 `__main__.py` 与真实 CLI 调用。"""
    return run()
