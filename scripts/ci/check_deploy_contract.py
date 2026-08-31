#!/usr/bin/env python3
"""部署编排的静态契约检查（Issue #62 / S11）。

守住 Dockerfile、`deploy/` 下的 compose 文件与三层 CI 工作流里那些**改坏了不会有任何东西
报错**的约束：停止宽限期够不够、凭据路径是不是落在持久卷上、生产 compose 里有没有混进
构建定义、镜像 tag 是不是可变的。这些全都属于"部署当天才会暴露"的那一类，而部署当天
暴露的代价是用户结果丢失或服务起不来。

**刻意不依赖 docker，也不依赖 YAML 解析库。**
- 不依赖 docker：本检查挂在 `verify_repository.sh` 上，那是本机与 CI 的同一个入口，
  一台没装 docker 的开发机也必须能跑出与 CI 相同的结论（验证与门禁第十一节）。
- 不依赖 PyYAML：它不是本仓库的声明依赖，为了一个门禁脚本引入依赖是本末倒置。
  需要**结构级**比对（stage 与生产 compose 同构）的那几条断言用 `docker compose config`
  在镜像构建 job 里做，见 scripts/ci/verify_compose_structure.sh——两者是互补的，
  不是重复的：那边证明"渲染出来一样"，这边证明"源文件里没写错"。

判定全部落在**去掉整行注释之后**的文本上。这不是小事：本仓库的 compose 注释里就写着
「本文件没有 `build:` 键」这样的句子，天真的 grep 会把说明文字当成违规。

用法：

    python3 scripts/ci/check_deploy_contract.py
"""

from __future__ import annotations

import ast
import math
import pathlib
import re
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]

DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"
COMPOSE_BASE = REPOSITORY_ROOT / "deploy" / "compose.yaml"
COMPOSE_STAGE = REPOSITORY_ROOT / "deploy" / "compose.stage.yaml"
COMPOSE_PROD = REPOSITORY_ROOT / "deploy" / "compose.prod.yaml"
ENV_EXAMPLE = REPOSITORY_ROOT / "deploy" / ".env.example"
DEPLOY_CHECKLIST = REPOSITORY_ROOT / "deploy" / "验收前部署配置清单.md"
COMPOSE_STRUCTURE_SCRIPT = REPOSITORY_ROOT / "scripts" / "ci" / "verify_compose_structure.sh"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
STORY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "story.yml"
PUBLISH_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish.yml"

FEISHU_DIRECTORY = REPOSITORY_ROOT / "src" / "lingxi" / "adapters" / "feishu_directory.py"
POSTGRES_ADAPTER = REPOSITORY_ROOT / "src" / "lingxi" / "adapters" / "postgres.py"
# #237 拆分后 `SAVE_RETRY_BACKOFF_SECONDS` 与消费它的 `_save_with_retry` 同在
# credential_rotation 子模块，不再是包的 __init__.py（那里现在只重导出这个名字）。
SCHEDULER_CREDENTIAL_ROTATION = (
    REPOSITORY_ROOT / "src" / "lingxi" / "apps" / "scheduler" / "credential_rotation.py"
)
GATEWAY_CONFIG = REPOSITORY_ROOT / "src" / "lingxi" / "apps" / "gateway" / "config.py"
WORKER_CONFIG = REPOSITORY_ROOT / "src" / "lingxi" / "apps" / "worker" / "config.py"
WORKER_MAX_CONCURRENCY_VARIABLE = "LINGXI_WORKER_MAX_CONCURRENCY"
WORKER_QUEUE_PRODUCTION_CONTRACT = {
    WORKER_MAX_CONCURRENCY_VARIABLE: "4",
    "LINGXI_WORKER_QUEUE_CPU_LIMIT": "1.5",
    "LINGXI_WORKER_QUEUE_MEM_LIMIT": "2G",
    "LINGXI_WORKER_QUEUE_PIDS_LIMIT": "512",
    "LINGXI_WORKER_QUEUE_TMPFS_SIZE": "256m",
}

# 停止宽限期的数据库往返预算（秒）由下方的 ``module_constant`` 从统一连接工厂读取。
# 这里不能复制 DSN 或连接工厂的数字：工厂通过 kwargs 覆盖 DSN 同名参数，且合法环境
# 覆盖还会把每一项调到 ``MAX_TIMEOUT_SECONDS``。门禁必须按那个事实源的合法最坏值建模。
POSTGRES_TIMEOUT_SOURCE_NAMES = {
    "connect_timeout": "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "statement_timeout": "DEFAULT_STATEMENT_TIMEOUT_SECONDS",
    "lock_timeout": "DEFAULT_LOCK_TIMEOUT_SECONDS",
}
# 最坏 5 次操作：_save_with_retry 的 4 次尝试 + 失败后的 1 次 revoke。
DATABASE_OPERATION_COUNT = 5

# 安全系数。宽限期不是"刚好够"就行：`_FileLock` 用的是阻塞式 flock，而 DSN 的三个
# 超时及覆盖值来自统一连接工厂。乘 1.5 是给不可见量留的余量，也让"把超时改大却不动
# compose"必然变红。
SAFETY_FACTOR = 1.5

# 凭据类环境变量：镜像里一个都不许赋值，仓库文件里也不许出现真值。
CREDENTIAL_VARIABLES = (
    "LINGXI_DELEGATED_CREDENTIAL_KEY",
    "LINGXI_OAUTH_REFRESH_TOKEN_KEY",
    "LINGXI_FEISHU_APP_SECRET",
    "LINGXI_FEISHU_APP_ID",
    "LINGXI_POSTGRES_DSN",
    "LINGXI_MIGRATION_DSN",
)

# 不可变 tag 的形状：<8 位日期>-<12 位十六进制 commit sha>，见 deploy/README.md。
IMMUTABLE_TAG = re.compile(r"^\d{8}-[0-9a-f]{12}$")


def strip_comments(text: str) -> str:
    """去掉整行注释，保留行号（换成空行）。

    只去整行注释、不碰行内 `#`：YAML 的值里可以合法地出现 `#`，而本仓库需要判定的
    那些键（image / user / restart / stop_grace_period）都不含它。宁可少去一点，
    也不要为了"更干净"引入一个会误伤值的规则。
    """

    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def display(path: pathlib.Path) -> str:
    """报错里显示的路径。仓库外的路径（用例会构造）退化成绝对路径而不是抛异常。"""

    try:
        return str(path.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(path)


def parse_duration_seconds(raw: str) -> float | None:
    """把 compose 的时长写法解析成秒。支持 `90s`、`1m30s`、`2m`、裸数字。"""

    raw = raw.strip().strip("'\"")
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return float(raw)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", raw)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return float(hours or 0) * 3600 + float(minutes or 0) * 60 + float(seconds or 0)


def module_constant(path: pathlib.Path, name: str):
    """从源码里取一个模块级常量的字面量值，不 import 该模块。

    不 import 是有意的：import `apps.scheduler` 会连带触发它的模块级副作用，而门禁
    不该为了读一个数字去跑业务代码。`ast.literal_eval` 只认字面量，读不到就返回 None。
    """

    tree = ast.parse(read(path), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if name in targets and node.value is not None:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def _postgres_timeout_facts() -> tuple[dict[str, int], int]:
    """读取业务连接工厂的默认超时与合法覆盖上界。"""

    defaults: dict[str, int] = {}
    for setting, source_name in POSTGRES_TIMEOUT_SOURCE_NAMES.items():
        value = module_constant(POSTGRES_ADAPTER, source_name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"读不到或非法的 {POSTGRES_ADAPTER}: {source_name}")
        defaults[setting] = value

    max_timeout = module_constant(POSTGRES_ADAPTER, "MAX_TIMEOUT_SECONDS")
    if isinstance(max_timeout, bool) or not isinstance(max_timeout, int) or max_timeout <= 0:
        raise ValueError(f"读不到或非法的 {POSTGRES_ADAPTER}: MAX_TIMEOUT_SECONDS")
    if any(value > max_timeout for value in defaults.values()):
        raise ValueError(
            f"{POSTGRES_ADAPTER} 的默认超时不能超过 MAX_TIMEOUT_SECONDS={max_timeout}"
        )
    return defaults, max_timeout


def _database_operation_seconds() -> float:
    """按合法最坏覆盖计算一次数据库建连、语句、提交的预算。"""

    _, max_timeout = _postgres_timeout_facts()
    # 每次数据库操作 = 建连、语句和提交各按 ``MAX_TIMEOUT_SECONDS`` 取最坏合法覆盖。
    # 当前 MAX=5，因此该模型为 5 + 2×MAX；保留拆开的写法是为了让推导可被门禁读懂。
    return float(max_timeout + 2 * max_timeout)


def _default_database_operation_seconds() -> float:
    """按连接工厂默认值计算模板 DSN 的名义单次操作预算。"""

    defaults, _ = _postgres_timeout_facts()
    return float(defaults["connect_timeout"] + 2 * defaults["statement_timeout"])


def _required_dsn_settings() -> dict[str, str]:
    """返回应与连接工厂默认值相符的示例 DSN 参数。"""

    defaults, _ = _postgres_timeout_facts()
    return {
        "connect_timeout": str(defaults["connect_timeout"]),
        "statement_timeout": str(defaults["statement_timeout"] * 1000),
        "lock_timeout": str(defaults["lock_timeout"] * 1000),
    }


# 保留这些模块级值供测试和成功摘要读取；每个检查函数仍会重新读取事实源，避免检查
# 逻辑与源码在同一进程内被临时改动后继续使用旧快照。
POSTGRES_DEFAULT_TIMEOUTS, POSTGRES_MAX_TIMEOUT_SECONDS = _postgres_timeout_facts()
DATABASE_OPERATION_SECONDS = _database_operation_seconds()
DATABASE_ROUNDTRIP_BUDGET_SECONDS = DATABASE_OPERATION_SECONDS * DATABASE_OPERATION_COUNT
REQUIRED_DSN_SETTINGS = _required_dsn_settings()


def service_block(compose_text: str, service: str) -> str | None:
    """截取某个 service 在 compose 里的那一段（已去注释）。

    compose 的 service 定义是二级缩进块：`services:` 下一级是服务名，再一级是它的键。
    这里按缩进切块，不做完整 YAML 解析——只要能把"这一段属于哪个服务"分清就够了。
    """

    lines = compose_text.splitlines()
    start = None
    indent = 0
    for index, line in enumerate(lines):
        match = re.match(rf"^(\s*){re.escape(service)}:\s*$", line)
        if match and start is None:
            start = index + 1
            indent = len(match.group(1))
            continue
        if start is not None and line.strip() and (len(line) - len(line.lstrip())) <= indent:
            return "\n".join(lines[start:index])
    if start is not None:
        return "\n".join(lines[start:])
    return None


def _gateway_shutdown_timeout() -> float | None:
    """gateway 的停机超时默认值。它是 load_config 里的调用参数，不是模块级常量。"""

    match = re.search(
        r'_number\(\s*env\s*,\s*"SHUTDOWN_TIMEOUT_SECONDS"\s*,\s*([0-9.]+)\s*\)',
        read(GATEWAY_CONFIG),
    )
    return float(match.group(1)) if match else None


def _gateway_worst_case_seconds() -> float:
    """gateway 一次在途停机在最坏情况下还要跑多久（秒）。

    三项都来自 gateway 自己的配置，不是拍脑袋：停机超时是配置默认值，出站 HTTP 超时
    由 apps/gateway/__init__.py 取它的四分之一，数据库那一项按与 scheduler 同一口径。
    """

    shutdown = _gateway_shutdown_timeout()
    if shutdown is None:
        raise ValueError("读不到 gateway 的 SHUTDOWN_TIMEOUT_SECONDS 默认值")
    outbound = max(1.0, shutdown / 4)
    return shutdown + outbound + _database_operation_seconds()


def _worker_worst_case_seconds() -> float:
    """queue worker 一次 SIGTERM 优雅停机在最坏情况下还要跑多久（秒）。

    与 gateway/scheduler 同一模型：进程自己的停机预算（
    ``DEFAULT_SHUTDOWN_TIMEOUT_SECONDS``，读自 apps/worker/config.py）加一次
    数据库终态写入的最坏预算。没有出站 HTTP 这一项——worker 不直接调用飞书。
    """

    shutdown = module_constant(WORKER_CONFIG, "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS")
    if not isinstance(shutdown, (int, float)):
        raise ValueError("读不到 worker 的 DEFAULT_SHUTDOWN_TIMEOUT_SECONDS 默认值")
    return float(shutdown) + _database_operation_seconds()


def _worst_case_seconds() -> float:
    """一次在途轮换在最坏情况下还要跑多久（秒）。读不到常量时抛 ValueError。"""

    http_timeout = module_constant(FEISHU_DIRECTORY, "REQUEST_TIMEOUT_SECONDS")
    backoff = module_constant(SCHEDULER_CREDENTIAL_ROTATION, "SAVE_RETRY_BACKOFF_SECONDS")
    if not isinstance(http_timeout, (int, float)):
        raise ValueError("读不到 REQUEST_TIMEOUT_SECONDS")
    if not isinstance(backoff, (tuple, list)) or not all(isinstance(x, (int, float)) for x in backoff):
        raise ValueError("读不到 SAVE_RETRY_BACKOFF_SECONDS")
    return float(http_timeout) + float(sum(backoff)) + _database_operation_seconds() * DATABASE_OPERATION_COUNT


def check_stop_grace_period() -> list[str]:
    """断言 V-部署-03 / M2-62-24 / M2-62-25：宽限期必须够，而且**与源码常量联动**。

    这一条是本脚本存在的主要理由。`stop_grace_period` 与 `REQUEST_TIMEOUT_SECONDS`
    之间的关系此前只写在注释里——注释不会在有人把超时从 20 秒改到 60 秒时变红。
    SIGKILL 落在"已向飞书换过新凭据、尚未写回数据库"的窗口里等于永久丢失一条一次性
    凭据，没有任何补救手段，只能人工重新授权。
    """

    failures: list[str] = []
    http_timeout = module_constant(FEISHU_DIRECTORY, "REQUEST_TIMEOUT_SECONDS")
    backoff = module_constant(SCHEDULER_CREDENTIAL_ROTATION, "SAVE_RETRY_BACKOFF_SECONDS")
    if not isinstance(http_timeout, (int, float)):
        return [f"读不到 {FEISHU_DIRECTORY.name} 的 REQUEST_TIMEOUT_SECONDS，无法核算停止宽限期"]
    if not isinstance(backoff, (tuple, list)) or not all(isinstance(x, (int, float)) for x in backoff):
        return [f"读不到 {SCHEDULER_CREDENTIAL_ROTATION.name} 的 SAVE_RETRY_BACKOFF_SECONDS，无法核算停止宽限期"]

    try:
        _, max_timeout = _postgres_timeout_facts()
        database_operation_seconds = float(max_timeout + 2 * max_timeout)
        database_roundtrip_budget_seconds = database_operation_seconds * DATABASE_OPERATION_COUNT
        worst_case = (
            float(http_timeout)
            + float(sum(backoff))
            + database_roundtrip_budget_seconds
        )
    except ValueError as error:
        return [str(error)]
    required = math.ceil(worst_case * SAFETY_FACTOR)

    block = service_block(strip_comments(read(COMPOSE_BASE)), "scheduler")
    if block is None:
        return ["deploy/compose.yaml 里找不到 scheduler service"]
    match = re.search(r"^\s*stop_grace_period:\s*(\S+)\s*$", block, re.MULTILINE)
    if match is None:
        failures.append(
            "deploy/compose.yaml 的 scheduler 没有显式 stop_grace_period。"
            f"Docker 默认 10 秒，而本进程最坏需要 {worst_case:.1f} 秒"
            f"（续期 HTTP {http_timeout}s + 落盘退避 {sum(backoff)}s + "
            f"数据库往返预算 {database_roundtrip_budget_seconds:.0f}s，"
            f"合法覆盖上界 {max_timeout}s）。"
            "SIGKILL 落在续期成功、写库未完成的窗口里 = 永久丢失一条一次性凭据。"
        )
        return failures

    actual = parse_duration_seconds(match.group(1))
    if actual is None:
        failures.append(f"stop_grace_period 的写法 `{match.group(1)}` 解析不出秒数")
    elif actual < required:
        failures.append(
            f"scheduler 的 stop_grace_period 是 {match.group(1)}（{actual:.0f} 秒），"
            f"低于要求的 {required} 秒。\n"
            f"      算法：续期 HTTP 超时 {http_timeout}s（feishu_directory.py 的 "
            f"REQUEST_TIMEOUT_SECONDS）+ 落盘重试退避 {sum(backoff)}s"
            f"（scheduler 的 SAVE_RETRY_BACKOFF_SECONDS）+ 数据库往返预算 "
            f"{database_roundtrip_budget_seconds:.0f}s = {worst_case:.1f}s，"
            f"其中合法覆盖按 {max_timeout}s 建模，"
            f"再乘安全系数 {SAFETY_FACTOR} = {required}s。\n"
            "      改了上述任一常量就必须同步改 deploy/compose.yaml——这正是本检查存在的理由。"
        )
    return failures


def check_database_timeouts() -> list[str]:
    """示例 DSN 要带齐并镜像工厂默认值；它不是运行时事实源。

    连接工厂会以 kwargs 覆盖 DSN 同名参数，因此停机预算不再从这个示例 DSN 推导。
    仍检查模板中的三个参数，是为了让示例与工厂默认配置保持可核对的一致；实际合法
    覆盖只能走 ``LINGXI_POSTGRES_*_TIMEOUT_SECONDS``。
    """

    text = read(ENV_EXAMPLE)
    match = re.search(r"^LINGXI_POSTGRES_DSN=(\S+)\s*$", text, re.MULTILINE)
    if match is None:
        return ["deploy/.env.example 里没有 LINGXI_POSTGRES_DSN 示范值"]

    dsn = match.group(1)
    try:
        required_settings = _required_dsn_settings()
    except ValueError as error:
        return [str(error)]
    failures = []
    for setting, expected in sorted(required_settings.items()):
        # DSN 里 options 的空格与等号是 URL 编码的（%20 / %3D），所以两种写法都认。
        pattern = rf"{re.escape(setting)}(=|%3D){re.escape(expected)}\b"
        if not re.search(pattern, dsn, re.IGNORECASE):
            failures.append(
                f"deploy/.env.example 的 LINGXI_POSTGRES_DSN 没有 `{setting}={expected}`。"
                f"\n      这是连接工厂默认值的模板对账；运行时实际由 src/lingxi/adapters/postgres.py"
                " 的 kwargs 覆盖 DSN，合法覆盖请使用 LINGXI_POSTGRES_*_TIMEOUT_SECONDS。"
            )
    return failures


def check_worker_queue_env_example() -> list[str]:
    """`deploy/.env.example` 的 worker-queue 小节必须自带 `LINGXI_POSTGRES_DSN`
    与 `LINGXI_USER_ENV_ROOT`。

    PR #173 复核 P1-2：早期版本让 worker-queue 借用一次性 `worker` job 的
    env 文件，那份文件的示范文本明确写着"这里不放数据库连接串"，worker-queue
    因此照抄部署后拿不到 DSN、以 `restart: unless-stopped` 无限崩溃重启。
    现在两者分文件，这里守住"worker-queue 自己的小节确实示范了 DSN"，防止
    未来又把这两行拆开时安静地漏掉。

    `LINGXI_USER_ENV_ROOT`（Epic D 闸⑥）是同一种失败形状的姊妹项：
    `apps/worker/cli.py` 的队列模式前置检查缺了它同样以 exit=3 拒绝启动，
    配上 `restart: unless-stopped` 同样是无限崩溃重启——示范文件漏了这一行，
    照抄部署的人不会有任何提示。
    """

    text = read(ENV_EXAMPLE)
    match = re.search(
        r"文件五：deploy/\.env\.stage\.worker-queue.*?(?=\n# ={10,}\n# 文件六)",
        text,
        re.DOTALL,
    )
    if match is None:
        return [
            "deploy/.env.example 找不到「文件五：…worker-queue」小节"
            "（或小节顺序/编号被改动，check_worker_queue_env_example 的定位正则需要同步更新）"
        ]
    section = match.group(0)
    failures: list[str] = []
    if not re.search(r"^LINGXI_POSTGRES_DSN=\S+\s*$", section, re.MULTILINE):
        failures.append(
            "deploy/.env.example 的 worker-queue 小节里没有 LINGXI_POSTGRES_DSN 示范值。"
            "worker-queue 是常驻队列消费者，启动期读不到这个变量会以 exit=3 拒绝启动，"
            "配上 restart: unless-stopped 就是无限崩溃重启（PR #173 复核 P1-2）。"
        )
    if not re.search(r"^LINGXI_USER_ENV_ROOT=\S+\s*$", section, re.MULTILINE):
        failures.append(
            "deploy/.env.example 的 worker-queue 小节里没有 LINGXI_USER_ENV_ROOT 示范值。"
            "worker-queue 处理每个任务时都要按 user_id 读这个根目录下的 .mcp.json"
            "（Epic D 闸⑥），启动期读不到会以 exit=3 拒绝启动，配上"
            " restart: unless-stopped 就是无限崩溃重启。"
        )
    return failures


# Epic D 闸⑤（首次开通编排写侧）直接依赖的四个变量：前三个缺任一个都会让
# `onboarding.duty_not_registered`（见 apps/scheduler/assembly.py 的
# `_build_onboarding_duty`）；第四个不影响闸门开关，只影响开通并发吞吐，但
# 同属这一组配置，一并守住文档覆盖度，防止有人不小心删掉示范行而没有任何
# 提示（见 deploy/验收前部署配置清单.md「一、闸⑤」「二、按进程分组」）。
ONBOARDING_GATE_ENV_VARS = (
    "LINGXI_MCP_TOKEN_ENCRYPT_KEY",
    "LINGXI_QUERY_MCP_ENDPOINT",
    "LINGXI_USER_ENV_ROOT",
    "LINGXI_ONBOARDING_WORKERS",
)


def check_onboarding_gate_env_example() -> list[str]:
    """`deploy/.env.example` 的 scheduler 小节（「文件二」）必须示范
    :data:`ONBOARDING_GATE_ENV_VARS` 四个变量（`deploy/验收前部署配置清单.md`
    登记为 Epic D 闸⑤配置项）。

    这四项此前**只活在文档里**：`.env.example` 里确实写着示范值/说明，但没有
    任何检查会在有人不小心删掉某一行示范时变红——本仓库的纪律是"只活在文档
    里的约束不算被守住"（AGENTS.md）。这里补上机械核对，形状照
    :func:`check_worker_queue_env_example`：只判定"变量名的赋值行是否存在"
    （允许被 `#` 注释掉，这四项本就是可选配置，示例文件里默认注释），不校验
    具体示范值——具体值的形状已由各自的运行时校验函数负责（例如
    `LINGXI_QUERY_MCP_ENDPOINT` 必须以 `https://` 开头，见
    `apps/scheduler/config.py` 的 `SchedulerConfig.from_env`）。

    **不用要求"值不含空白"的行级正则**（`check_worker_queue_env_example` 用的
    那种 ``\\S+\\s*$``）：`LINGXI_MCP_TOKEN_ENCRYPT_KEY` 的示范值
    `<32B base64 主密钥>` 本身带空格，套用那种正则会对着当前本就正确的文件
    误判为缺失。这里只判定"这一行确实在给这个变量名赋值"（`#?` 允许注释掉），
    不管值里有没有空格。
    """

    text = read(ENV_EXAMPLE)
    match = re.search(
        r"文件二：deploy/\.env\.stage\.scheduler.*?(?=\n# ={10,}\n# 文件三)",
        text,
        re.DOTALL,
    )
    if match is None:
        return [
            "deploy/.env.example 找不到「文件二：…scheduler」小节"
            "（或小节顺序/编号被改动，check_onboarding_gate_env_example 的定位正则需要同步更新）"
        ]
    section = match.group(0)
    failures: list[str] = []
    for variable in ONBOARDING_GATE_ENV_VARS:
        if not re.search(rf"^#?\s*{re.escape(variable)}=", section, re.MULTILINE):
            failures.append(
                f"deploy/.env.example 的 scheduler 小节（「文件二」）没有示范 {variable}。"
                "它是 Epic D 闸⑤相关的开通配置项，缺失文档示范容易让人不知道这个变量存在"
                "（详见 deploy/验收前部署配置清单.md「一、闸⑤」）。"
            )
    return failures


def _environment_value(service_text: str, variable: str) -> str | None:
    """从 service 的 ``environment:`` 子块取一个变量原文。"""

    environment = service_block(service_text, "environment") or ""
    match = re.search(rf"^\s*{re.escape(variable)}:\s*(.*?)\s*$", environment, re.MULTILINE)
    return match.group(1).strip() if match else None


def _resource_value(service_text: str, key: str) -> str | None:
    """从 service 的 ``deploy.resources.limits`` 子块取一个限制原文。"""

    deploy = service_block(service_text, "deploy") or ""
    resources = service_block(deploy, "resources") or ""
    limits = service_block(resources, "limits") or ""
    match = re.search(rf"^\s*{re.escape(key)}:\s*(.*?)\s*$", limits, re.MULTILINE)
    return match.group(1).strip() if match else None


def _has_env_assignment(text: str, variable: str, value: str, *, allow_comment: bool = False) -> bool:
    """判断文档/env 模板是否出现精确的 ``NAME=value`` 行。"""

    prefix = r"#?\s*" if allow_comment else r"\s*"
    return bool(
        re.search(
            rf"^{prefix}{re.escape(variable)}={re.escape(value)}\s*$",
            text,
            re.MULTILINE,
        )
    )


def check_worker_concurrency_contract() -> list[str]:
    """把 worker-queue 并发上限与生产资源合同钉在代码/compose/文档四层。

    Issue #496 的风险不是一个只写在评论里的容量意见：直接构造、环境 loader、
    stage/prod compose 以及生产 runbook 必须共同表达同一个 `4`。生产资源值仍从
    未入库的外部 `.env.prod` 注入，但它们的合同值和 `${VAR:?}` fail-fast 形状在
    仓库里必须可机械检查；这样既不把真实生产凭据写进仓库，也不让数字漂移。
    """

    failures: list[str] = []
    config_source = read(WORKER_CONFIG)
    default = module_constant(WORKER_CONFIG, "DEFAULT_MAX_CONCURRENCY")
    hard_limit = module_constant(WORKER_CONFIG, "MAX_CONCURRENCY_HARD_LIMIT")
    if default != 4 or hard_limit != 4:
        failures.append(
            "apps/worker/config.py 的 DEFAULT_MAX_CONCURRENCY / "
            f"MAX_CONCURRENCY_HARD_LIMIT 必须都为 4（当前 {default!r}/{hard_limit!r}）。"
        )
    if not re.search(
        r"max_concurrency\s*:\s*int\s*=\s*DEFAULT_MAX_CONCURRENCY",
        config_source,
    ):
        failures.append(
            "apps/worker/config.py 的 WorkerConfig.max_concurrency 必须引用 "
            "DEFAULT_MAX_CONCURRENCY，不能留下漂移的旧默认值。"
        )
    if not re.search(r"max_concurrency\s*=\s*_max_concurrency\(env\)", config_source):
        failures.append(
            "apps/worker/config.py 的 load_config 必须经 _max_concurrency(env) 读取，"
            "不能只在 compose 或评论里声明上限。"
        )
    if "MAX_CONCURRENCY_HARD_LIMIT" not in config_source:
        failures.append("apps/worker/config.py 缺少并发硬上限断言。")

    base = strip_comments(read(COMPOSE_BASE))
    stage = strip_comments(read(COMPOSE_STAGE))
    prod = strip_comments(read(COMPOSE_PROD))
    base_worker_queue = service_block(base, "worker-queue") or ""
    stage_worker_queue = service_block(stage, "worker-queue") or ""
    prod_worker_queue = service_block(prod, "worker-queue") or ""
    base_value = _environment_value(base_worker_queue, WORKER_MAX_CONCURRENCY_VARIABLE)
    if base_value != '"${LINGXI_WORKER_MAX_CONCURRENCY:-4}"':
        failures.append(
            "deploy/compose.yaml 的 worker-queue 必须显式给出 "
            'LINGXI_WORKER_MAX_CONCURRENCY: "${LINGXI_WORKER_MAX_CONCURRENCY:-4}"。'
        )
    stage_value = _environment_value(stage_worker_queue, WORKER_MAX_CONCURRENCY_VARIABLE)
    if stage_value not in {'"4"', "4"}:
        failures.append(
            "deploy/compose.stage.yaml 的 worker-queue 必须显式固定 "
            "LINGXI_WORKER_MAX_CONCURRENCY=4。"
        )
    prod_value = _environment_value(prod_worker_queue, WORKER_MAX_CONCURRENCY_VARIABLE)
    if prod_value is None or not REQUIRED_VARIABLE.match(prod_value):
        failures.append(
            "deploy/compose.prod.yaml 的 worker-queue 并发必须使用 "
            "LINGXI_WORKER_MAX_CONCURRENCY 的 `${VAR:?}` 外部 env 门，不能带默认值。"
        )

    expected_limits = {
        "cpus": '"${LINGXI_WORKER_QUEUE_CPU_LIMIT:-1.5}"',
        "memory": "${LINGXI_WORKER_QUEUE_MEM_LIMIT:-2G}",
        "pids": "${LINGXI_WORKER_QUEUE_PIDS_LIMIT:-512}",
    }
    for path_label, worker_queue in (
        ("deploy/compose.yaml", base_worker_queue),
        ("deploy/compose.stage.yaml", stage_worker_queue),
    ):
        for key, expected in expected_limits.items():
            actual = _resource_value(worker_queue, key)
            if actual != expected:
                failures.append(
                    f"{path_label} 的 worker-queue `{key}` 必须保留批准合同默认值 "
                    f"{expected}（当前 {actual!r}）。"
                )

    env_example = read(ENV_EXAMPLE)
    if not _has_env_assignment(
        env_example, WORKER_MAX_CONCURRENCY_VARIABLE, "4", allow_comment=True
    ):
        failures.append(
            "deploy/.env.example 没有示范 LINGXI_WORKER_MAX_CONCURRENCY=4；"
            "并发合同不能只活在 compose 注释里。"
        )

    readme = read(REPOSITORY_ROOT / "deploy" / "README.md")
    runbook = read(REPOSITORY_ROOT / "deploy" / "生产部署runbook.md")
    checklist = read(DEPLOY_CHECKLIST)
    for path_label, text in (
        ("deploy/README.md", readme),
        ("deploy/生产部署runbook.md", runbook),
    ):
        for variable, value in WORKER_QUEUE_PRODUCTION_CONTRACT.items():
            if not _has_env_assignment(text, variable, value):
                failures.append(
                    f"{path_label} 缺少生产 worker-queue 合同行 `{variable}={value}`；"
                    "真实值仍须写到未入库的外部 deploy/.env.prod。"
                )
    if WORKER_MAX_CONCURRENCY_VARIABLE not in checklist or "硬上限" not in checklist:
        failures.append(
            "deploy/验收前部署配置清单.md 没有登记 worker-queue 并发硬上限合同。"
        )
    return failures


def check_content_capture_prod_guard() -> list[str]:
    """内测轮内容级采集开关（Issue #251/#304 批次 3）的"正式环境不得生效"结构性
    保证：机械核对而不是只活在文档里的约定。

    研究结论（见 ``apps/worker/config.py`` 的 ``_innertest_content_capture`` 与
    迁移 ``0069_innertest_content_capture`` 的模块文档）：``deploy/compose.stage.
    yaml`` 与 ``compose.prod.yaml`` 结构完全相同，代码里不存在任何"这是 stage
    还是生产"的运行期判据，因此选用的方案是"双变量确认 + 精确字面量 + 本检查"，
    而不是声称已有某种运行期环境探测。本检查覆盖这个方案里**唯一可由仓库静态
    核对**的一环：

    1. 两个变量名与第二确认变量要求的精确字面量，一个都不允许出现在任何**入库**
       的 compose 编排文件里（尤其是 ``deploy/compose.prod.yaml``）——它们只能来自
       逐服务、不入库的宿主机本地 env 文件（``.env.<env>.worker-queue``）。这挡住
       "有人把默认值写进 compose 的 ``environment:`` 块"这一类会让开关无论各环境
       env 文件如何配置都统一生效/失效的错误。
    2. ``deploy/.env.example`` 的 worker-queue 小节（「文件五」）确实示范了主开关
       变量名，且文件中出现"生产环境禁止配置"一类的明确警示——照抄
       ``check_worker_queue_env_example``/``check_onboarding_gate_env_example``
       的"变量名的赋值行是否存在"判定形状，具体字面量来自 ``apps/worker/
       config.py`` 的模块级常量（``module_constant``，不 import 业务代码）。

    **本检查不能证明、也不声称证明**"运维不会把两个变量的值一起复制进生产的
    未入库 env 文件"——那是当前 stage/生产共用完全相同镜像与编排结构下，任何
    静态仓库检查都做不到的事，最后一道防线仍是部署操作纪律（见
    ``deploy/验收前部署配置清单.md`` 与迁移文件头部「已知残余风险」的如实登记）。
    """

    flag_var = module_constant(WORKER_CONFIG, "CONTENT_CAPTURE_FLAG_VAR")
    confirm_var = module_constant(WORKER_CONFIG, "CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR")
    confirm_value = module_constant(WORKER_CONFIG, "CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE")
    failures: list[str] = []
    if not flag_var or not confirm_var or not confirm_value:
        return [
            "读不到 apps/worker/config.py 的 CONTENT_CAPTURE_FLAG_VAR / "
            "CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VAR / "
            "CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE 三个模块级常量之一"
            "（常量被重命名或改写成非字面量赋值时，check_content_capture_prod_guard "
            "需要同步更新）"
        ]

    for path in (COMPOSE_BASE, COMPOSE_STAGE, COMPOSE_PROD):
        stripped = strip_comments(read(path))
        for needle in (flag_var, confirm_var, confirm_value):
            if needle in stripped:
                failures.append(
                    f"{display(path)} 中出现 {needle!r}：内测轮内容级采集开关的变量名"
                    "与第二确认变量的精确字面量不得写进任何 compose 编排文件本身"
                    "（尤其不得出现在 deploy/compose.prod.yaml），只能来自逐服务、"
                    "不入库的宿主机本地 env 文件（.env.<env>.worker-queue）"
                )

    env_text = read(ENV_EXAMPLE)
    match = re.search(
        r"文件五：deploy/\.env\.stage\.worker-queue.*?(?=\n# ={10,}\n# 文件六)",
        env_text,
        re.DOTALL,
    )
    if match is None:
        failures.append(
            "deploy/.env.example 找不到「文件五：…worker-queue」小节"
            "（或小节顺序/编号被改动，check_content_capture_prod_guard 的定位正则"
            "需要同步更新）"
        )
    else:
        section = match.group(0)
        if not re.search(rf"^#?\s*{re.escape(flag_var)}=", section, re.MULTILINE):
            failures.append(
                f"deploy/.env.example 的 worker-queue 小节（「文件五」）没有示范 {flag_var}。"
                "内测轮内容级采集开关缺失文档示范容易让人不知道这个变量存在。"
            )
        if "生产" not in section or "禁止" not in section:
            failures.append(
                "deploy/.env.example 的 worker-queue 小节（「文件五」）缺少内测轮内容级"
                "采集开关「生产环境禁止配置」的明确警示行（结构性保证之外，仍须有醒目"
                "的文档提醒——两者互补，不是二选一）。"
            )

    checklist_text = read(DEPLOY_CHECKLIST)
    if flag_var not in checklist_text:
        failures.append(
            f"deploy/验收前部署配置清单.md 未登记 {flag_var}：按本文件既有惯例，"
            "新增的可选配置项须登记在清单里，说明它属于哪一类、缺失时行为如何。"
        )
    return failures


def _volume_mounts(service_text: str) -> list[tuple[str, str, str | None]]:
    """解析某个 service 块下 ``volumes:`` 列表的每一项，返回
    ``(source, target, mode)`` 三元组列表；``mode`` 是 ``:ro``/``:rw`` 这类第三段
    （没有则 ``None``）。

    复用 :func:`service_block` 的同一套缩进定界算法定位 ``volumes:`` 子块——它对
    "key: 后跟同级子项，直到遇到同缩进或更浅的下一个键"这个结构不挑层级，原本
    给 service 名字用，这里直接拿来给 service 块内部的 ``volumes:`` 用。
    这样**只解析真正的 volumes 列表**，不会被 ``environment``/``labels`` 等
    别处偶然出现的形似字符串（例如注释、环境变量值）误判成"已挂载"
    （外部独立审查 F2：此前的整块字符串包含判断存在这个假绿口子）。
    """

    volumes_block = service_block(service_text, "volumes")
    if volumes_block is None:
        return []
    mounts: list[tuple[str, str, str | None]] = []
    for line in volumes_block.splitlines():
        match = re.match(r"^\s*-\s*([\w.-]+):(/[^:\s]+)(?::([\w,-]+))?\s*$", line)
        if match:
            source, target, mode = match.groups()
            mounts.append((source, target, mode))
    return mounts


def check_scheduler_user_volume() -> list[str]:
    """Epic D 闸⑤：scheduler 的首次开通编排要往用户环境目录写每个用户自己的
    ``.mcp.json``（S-D-02，``adapters/user_environment.py``），但 stage/prod
    覆盖文件此前只给它挂了凭据卷，没有挂用户目录卷。

    后果不是"启动时报错"那种容易发现的失败：``LocalUserEnvironment`` 的
    根目录在真实容器里第一次被访问（``sweep_all()``）就会因为不存在而抛
    ``UserEnvironmentError``，装配层据此判定"前置不齐"、**整条首次开通编排
    从不注册**（见 ``apps/scheduler/assembly.py`` 的 ``_build_onboarding_duty``），
    而这只留一条容易被忽略的审计日志——其余定时职责照常运行，容器健康检查照常
    通过，表面上"scheduler 工作正常"。

    只检查 stage/prod（而不是 ``deploy/compose.yaml`` 基线）：与 worker/
    worker-queue 不同，scheduler 的这个挂载点目前只出现在覆盖文件里
    （基线 scheduler 只挂凭据卷，用户目录卷的声明与命名跟着 stage/prod 环境走，
    与 worker 系列的既有结构一致）。

    **精确解析 ``volumes:`` 列表、显式断言不是只读**（外部独立审查 F2 修复）：
    此前用整块字符串包含判断 ``"lingxi-users:/var/lib/lingxi/users" not in
    block``，两种情况都会被误判成"已挂载"：①这个子串恰好出现在
    ``environment``/``labels`` 等别处（不是真的卷挂载）；②挂成了
    ``lingxi-users:/var/lib/lingxi/users:ro``——scheduler 写不进去，
    子串判断依旧命中、门禁照样绿，而首次开通编排会在第一次写 ``.mcp.json``
    时才炸。运维为了"保守"随手给一个后来发现该写的卷加 ``:ro`` 是完全可能
    发生的操作失误，不需要构造出恶意场景就能触发。
    """

    failures: list[str] = []
    for path in (COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        block = service_block(text, "scheduler")
        if block is None:
            failures.append(f"{display(path)} 找不到 scheduler service")
            continue
        matching = [
            (source, target, mode)
            for source, target, mode in _volume_mounts(block)
            if source == "lingxi-users" and target == "/var/lib/lingxi/users"
        ]
        if not matching:
            failures.append(
                f"{display(path)} 的 scheduler 没有挂载 lingxi-users 卷"
                "（挂载点须与 worker/worker-queue 一致：/var/lib/lingxi/users）。"
                "首次开通编排要在这个目录下给每个用户写 .mcp.json，缺了这个挂载，"
                "整条首次开通编排会在启动期判定「前置不齐」而永远不注册"
                "（见 apps/scheduler/assembly.py 的 _build_onboarding_duty），"
                "且这个失败只留一条容易被忽略的审计日志，其余职责与健康检查"
                "照常正常。"
            )
            continue
        if any(mode == "ro" for _, _, mode in matching):
            failures.append(
                f"{display(path)} 的 scheduler 把 lingxi-users 挂成了只读"
                "（`:ro`）。首次开通编排要往这个目录写每个用户的 .mcp.json，"
                "只读挂载会让每一次开通都在第一次写入时失败——表现形式与完全"
                "没挂载相同（前置不齐、编排不注册），但门禁如果只做字符串包含"
                "判断会因为子串仍然命中而误判为通过。"
            )
    return failures


# ---- 资源限制（Trace #373 H2 / S-H2-1，产品负责人 2026-08-28 先行裁定）--------
# stage 主机只有 2 核 / 3.7GiB，不设限制的容器可能互相饿死；限制过低又会在正常
# 负载下杀死真实的 Agent SDK 回合（Claude CLI + 多个 MCP 子进程）。具体数值分
# 环境写在 compose.stage.yaml / compose.prod.yaml（deploy/README.md「资源限制」
# 一节有完整推导），门禁只守「结构」：全部六个服务必须显式声明内存、CPU、pids
# 三项限制，不核对具体数值——数值对错是容量判断，不是机械可判定的对错。
RESOURCE_LIMITED_SERVICES = (
    "scheduler",
    "gateway",
    "worker",
    "worker-queue",
    "migrate",
    "reauthorize",
)


def _extract_positive_default(raw: str) -> float | None:
    """从 compose 一段键值文本里提取"静态可判定的数值"，供 >0 校验使用
    （独立审查 P1-2）。

    本仓库的 `deploy.resources.limits` 全部写成 `${VAR:-默认值}` 形态（基线与
    stage/prod 覆盖文件同一套写法），门禁不起 docker、不渲染 compose，只能从
    源文本里的这个默认值判断"至少这个默认值本身是不是安全的正数"——这不是
    运行时真正生效的值（真正生效的值取决于环境是否覆盖了这个变量），但如果
    连默认值本身都判定不出是正数，静态层面就已经拦不住 `pids: 0` 这类会被
    Compose 解读成"整个不下发这项限制"的写法。

    支持两种形状：`${VAR:-N}`（取 `N`，两侧可选引号）与裸字面量（数字，允许
    `512M`/`16G` 这类 Compose 内存单位后缀，取数字前缀）。其余形状（例如没有
    默认值的 `${VAR:?...}`，或整段都不是数字）返回 ``None``——调用方把
    ``None`` 当"判定不出安全数值"处理，同样判红，不当放行处理。
    """

    value = raw.strip()
    match = re.fullmatch(r'"?\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}"?', value)
    if match:
        value = match.group(1)
    value = value.strip().strip("'\"")
    number_match = re.match(r"-?\d+(?:\.\d+)?", value)
    if number_match is None:
        return None
    try:
        return float(number_match.group(0))
    except ValueError:  # pragma: no cover - re.match 已保证是合法数字文本
        return None


#: `${VAR:?说明}` 形态：**没有默认值**，漏配就 `docker compose up` 报错退出。
#: 这是本仓库既有的"不许静默退回默认"写法（`LINGXI_IMAGE_TAG` 用的就是它）。
REQUIRED_VARIABLE = re.compile(r'^"?\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^}]*\}"?$')

# ---- 生产 worker-queue 外部资源合同（Issue #494/#502，2026-08-31）---------
# 生产值由目标机器外部根 `deploy/.env.prod` 提供，仓库不创建真实文件；prod compose
# 以 `${VAR:?...}` 形态 fail-fast，静态检查同时确认这个形状与合同文档中的值。
PROD_EXTERNAL_HOST_SPEC_LIMITS: dict[str, tuple[str, ...]] = {
    "worker-queue": ("cpus", "memory", "pids"),
}


def _check_resource_limits_in(
    compose_path: pathlib.Path,
    *,
    require_all_services: bool,
    external_keys: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """核对一份 compose 文本里六个服务的 `deploy.resources.limits`：三键齐全、
    且各自能从静态文本判定出一个 >0 的默认值。

    `require_all_services=True` 现在对**三份文件都成立**（Issue #494）：基线、
    stage 覆盖、prod 覆盖都必须逐服务显式声明这个块。此前覆盖文件是可选的
    ——"沿用基线"听起来无害，实际是把一个只为"漏挂 --env-file 时不至于更糟"
    而存在的兜底值当成了部署档；当前基线默认与批准的 worker-queue 合同一致，
    stage 与生产机器采用同型资源。每个环境显式写出自己的一档，是让"这个数字是给哪台机器的"这件事
    在文件里可读、可复核。

    `external_keys` 里登记的服务/键（见 `PROD_EXTERNAL_HOST_SPEC_LIMITS`）反过来
    **必须**是 `${VAR:?...}` 无默认值形态：生产值仍由外部 `.env.prod` 注入，
    留一个能静默生效的默认值会绕过合同。
    """

    failures: list[str] = []
    external = external_keys or {}
    text = strip_comments(read(compose_path))
    for service in RESOURCE_LIMITED_SERVICES:
        block = service_block(text, service)
        if block is None:
            if require_all_services:
                failures.append(f"{display(compose_path)} 找不到 service `{service}`")
            continue
        deploy_block = service_block(block, "deploy") or ""
        resources_block = service_block(deploy_block, "resources") or ""
        limits_block = service_block(resources_block, "limits") or ""
        if not limits_block.strip():
            if require_all_services:
                failures.append(
                    f"{service} 在 {display(compose_path)} 里没有 deploy.resources.limits "
                    "声明。stage 主机只有 2 核 / 3.7GiB，不限制的容器会互相饿死；"
                    "缺一个服务的限制就是缺一个失控风险。覆盖文件不得靠"
                    "「沿用基线」省掉这一段——基线的值只是漏挂 --env-file 时的兜底，"
                    "不是任何一个环境的部署档（Issue #494）。"
                )
            continue
        external_for_service = external.get(service, ())
        for key in ("cpus", "memory", "pids"):
            value_match = re.search(rf"^\s*{key}:\s*(\S.*?)\s*$", limits_block, re.MULTILINE)
            if value_match is None:
                failures.append(
                    f"{service} 的 deploy.resources.limits 在 {display(compose_path)} 缺 `{key}`"
                )
                continue
            raw = value_match.group(1).strip()
            if key in external_for_service:
                if not REQUIRED_VARIABLE.match(raw):
                    failures.append(
                        f"{service} 的 `{key}` 在 {display(compose_path)} 不是 "
                        f"`${{变量:?说明}}` 无默认值形态（原文 `{raw}`）。这一项依赖"
                        "生产外部资源合同（Issue #494/#502）：带默认值意味着漏配时"
                        "静默绕过主机资源确认，那道防线等于没有生效。值只能由外部 "
                        "deploy/.env.prod 提供，或从 "
                        "PROD_EXTERNAL_HOST_SPEC_LIMITS 里摘掉这一项。"
                    )
                continue
            numeric = _extract_positive_default(raw)
            if numeric is None or numeric <= 0:
                failures.append(
                    f"{service} 的 `{key}` 在 {display(compose_path)} 判定不出安全的正数默认值"
                    f"（原文 `{raw}`）。Compose 对 `pids: 0`/`memory: 0`"
                    "这类 0/null/空值的语义是整个不下发这项限制，等于没设——必须是可判定的正数。"
                )
    return failures


def check_resource_limits() -> list[str]:
    """**三份 compose 各自**都要为六个服务显式声明 `deploy.resources.limits`
    的 `cpus`/`memory`/`pids` 三项，且各自的静态默认值 >0（独立审查 P1-2：
    此前只读基线，覆盖文件把某一项悄悄改成 `0`/空——而覆盖文件的值才是真正
    部署时生效的值——不会被这条门禁发现；Issue #494 进一步要求覆盖文件不得
    靠"沿用基线"省掉声明）。

    例外只有一处：`PROD_EXTERNAL_HOST_SPEC_LIMITS` 登记的生产外部合同项必须写成
    `${VAR:?...}`，反过来**不许**带默认值。

    只做结构 + 静态数值核对，不核对具体数值"合不合理"——合理性是容量判断，
    见 `deploy/README.md`「资源限制」一节；这里只保证"没有任何服务被漏掉、
    没有任何一项会被 Docker/Compose 悄悄解读成不限制"。
    """

    failures: list[str] = []
    failures += _check_resource_limits_in(COMPOSE_BASE, require_all_services=True)
    failures += _check_resource_limits_in(COMPOSE_STAGE, require_all_services=True)
    failures += _check_resource_limits_in(
        COMPOSE_PROD,
        require_all_services=True,
        external_keys=PROD_EXTERNAL_HOST_SPEC_LIMITS,
    )
    return failures


# ---- compose 插值占位符必须能被 YAML 解析（PR #506 CI 实测教训）--------------
# compose 的值要先过 **YAML 解析**，再做 `${VAR}` 插值。未加引号的 YAML 标量遇到
# 「空格 + #」就被当成行内注释**从那里截断**——`#494` 里的那个 `#` 把 `}` 连同后半
# 句一起吃掉，剩下一个没有闭合的 `${...`，compose 报的是
# `invalid interpolation format`（而不是我们要的"变量缺失"），整条 fail-fast 语义
# 直接失效。同理「冒号 + 空格」会被 YAML 当成映射指示符。
#
# 这一类此前只有最晚的 `Epic Full / image` 作业里的 `verify_compose_structure.sh`
# （真的起 docker 渲染）才抓得到；本检查把它前移到不需要 docker 的文本层，让
# `scripts/dev/check.sh fast` 与 Story 门禁就能变红。两者不互相替代：那边验的是
# 渲染后的最终结构，这边验的是"能不能渲染得出来"。
_PLACEHOLDER = re.compile(r"\$\{([^{}]*)\}")
#: YAML 纯量里会改变解析结果的两个序列，出现在 `${...}` 内部即判红。
_YAML_HOSTILE_SEQUENCES = (" #", ": ")


def check_compose_interpolation_is_yaml_safe() -> list[str]:
    """三份 compose 里每个 `${...}` 占位符都必须能原样活过 YAML 解析；且每个
    **无默认值**的 `${VAR:?}` 都必须被 `verify_compose_structure.sh` 显式赋一个
    占位值，否则那条渲染门禁会以"变量缺失"红（PR #506 实测：漏了这一步只会在
    最晚的 image 作业里才炸）。
    """

    failures: list[str] = []
    required_variables: set[str] = set()
    for path in (COMPOSE_BASE, COMPOSE_STAGE, COMPOSE_PROD):
        for number, line in enumerate(read(path).splitlines(), start=1):
            if line.lstrip().startswith("#") or "${" not in line:
                continue
            for body in _PLACEHOLDER.findall(line):
                for sequence in _YAML_HOSTILE_SEQUENCES:
                    if sequence in body:
                        failures.append(
                            f"{display(path)}:{number} 的 `${{{body[:40]}…}}` 含 "
                            f"`{sequence.replace(' ', '␠')}`。compose 的值先过 YAML 解析再插值："
                            "未加引号的标量遇到「空格+#」会被当成行内注释从那里截断、"
                            "「冒号+空格」会被当成映射指示符，两者都会把 `}` 甩在解析结果之外，"
                            "compose 只会报 `invalid interpolation format`。提示语请压成短的"
                            "纯 ASCII 单行，详细说明写在 deploy/README.md。"
                        )
                match = re.match(r"([A-Za-z_][A-Za-z0-9_]*):\?", body)
                if match:
                    required_variables.add(match.group(1))
            # 占位符必须在本行闭合：去掉全部完整占位符后不该还剩 `${`。
            if "${" in _PLACEHOLDER.sub("", line):
                failures.append(
                    f"{display(path)}:{number} 有没有闭合的 `${{`，compose 会报 "
                    "`invalid interpolation format`"
                )

    script = read(COMPOSE_STRUCTURE_SCRIPT)
    exported = set(re.findall(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=", script, re.MULTILINE))
    for variable in sorted(required_variables - exported):
        failures.append(
            f"`${{{variable}:?}}` 是无默认值的必填变量，但 "
            f"{display(COMPOSE_STRUCTURE_SCRIPT)} 没有为它 export 占位值。"
            "那条渲染门禁不读 deploy/.env.prod，缺一个就整段渲染不出来——"
            "而它只在最晚的 `Epic Full / image` 作业里跑（PR #506 实测）。"
        )
    return failures


# ---- worker-queue 的 /tmp 内存盘上限（Issue #494）----------------------------
# 这块盘不是磁盘：镜像把 HOME 指到 /tmp，Agent 会话转录写在 $HOME/.claude/projects
# 下，**写满即等量占用宿主内存**。rc22 收尾批 S-12 浸泡实测：容器接负载约 45 分钟
# 就把 256MB 写满（32 份转录、单份最大 15MB），而健康检查一路报绿——探针写的是
# 十几字节的活性文件，覆盖写不需要新页，于是"用户在失败、监控显示 healthy、运维
# 查不出原因"可以无限期持续。
#
# 上限此前只写在基线 compose.yaml 里，`compose.prod.yaml` 与 `compose.stage.yaml`
# 都没有覆盖（grep 零命中）——生产未形成显式合同。这条
# 门禁把三件事钉死：基线不许把值写死回去（必须走变量）、stage 必须显式声明自己
# 那一档、prod 必须显式声明且**不得沿用默认**（生产值由外部合同提供，见
# PROD_EXTERNAL_HOST_SPEC_LIMITS 的同一条理由）。
TMPFS_CAPACITY_SERVICE = "worker-queue"
TMPFS_CAPACITY_VARIABLE = "LINGXI_WORKER_QUEUE_TMPFS_SIZE"


def _tmp_tmpfs_size(compose_path: pathlib.Path, service: str) -> tuple[str | None, str | None]:
    """返回 (size 原文, 失败说明)。两者恒有且只有一个是 ``None``。"""

    text = strip_comments(read(compose_path))
    block = service_block(text, service)
    if block is None:
        return None, f"{display(compose_path)} 找不到 service `{service}`"
    tmpfs_block = service_block(block, "tmpfs")
    if not tmpfs_block or not tmpfs_block.strip():
        return None, (
            f"{service} 在 {display(compose_path)} 没有声明 `tmpfs:`。"
            "`/tmp` 是内存盘、写满即等量占用宿主内存，容量必须分环境显式声明，"
            "不许靠沿用基线默认（Issue #494）。"
        )
    # 挂载项的选项串里可能含空格（`${VAR:?中文说明}` 的说明文本），因此不能按
    # `\S+` 切——那会在 prod 的外部合同占位上找不到这一项而给出误导性的报错。
    entry = re.search(r"^\s*-\s*/tmp:(.+?)\s*$", tmpfs_block, re.MULTILINE)
    if entry is None:
        return None, (
            f"{service} 在 {display(compose_path)} 的 `tmpfs:` 里找不到 `/tmp` 挂载项"
        )
    # `${...}` 整体作为一个值取走：占位说明里可以出现逗号，按逗号硬切会把它腰斩。
    size = re.search(r"(?:^|,)size=(\$\{[^}]*\}|[^,]+)", entry.group(1))
    if size is None:
        return None, (
            f"{service} 在 {display(compose_path)} 的 `/tmp` tmpfs 没有 `size=` 上限"
            f"（原文 `{entry.group(1)}`）。不写 size 时 Docker 默认给宿主内存的一半，"
            "等于这块内存盘完全没有上限。"
        )
    return size.group(1).strip(), None


def check_worker_tmpfs_capacity() -> list[str]:
    """`worker-queue` 的 `/tmp` 内存盘上限必须是分环境显式配置（Issue #494）。

    基线走变量、stage 显式给出自己那一档、prod 显式声明且必须是 `${VAR:?...}`
    无默认值形态——理由见本节头部注释与 `PROD_EXTERNAL_HOST_SPEC_LIMITS`。
    """

    failures: list[str] = []
    service = TMPFS_CAPACITY_SERVICE

    base_size, error = _tmp_tmpfs_size(COMPOSE_BASE, service)
    if error is not None:
        failures.append(error)
    elif f"${{{TMPFS_CAPACITY_VARIABLE}" not in base_size:
        failures.append(
            f"{service} 在 {display(COMPOSE_BASE)} 的 `/tmp` size 写死成了 `{base_size}`，"
            f"没有走 `${{{TMPFS_CAPACITY_VARIABLE}}}`。写死的上限没有办法分环境覆盖，"
            "stage 与生产就只能共用同一个数字（Issue #494）。"
        )

    stage_size, error = _tmp_tmpfs_size(COMPOSE_STAGE, service)
    if error is not None:
        failures.append(error)
    else:
        numeric = _extract_positive_default(stage_size)
        if numeric is None or numeric <= 0:
            failures.append(
                f"{service} 在 {display(COMPOSE_STAGE)} 的 `/tmp` size "
                f"`{stage_size}` 判定不出安全的正数上限。stage 主机是 2 核 / 3.74GiB，"
                "这块内存盘的上限必须是一个看得见、可复核的正数。"
            )

    prod_size, error = _tmp_tmpfs_size(COMPOSE_PROD, service)
    if error is not None:
        failures.append(error)
    elif not REQUIRED_VARIABLE.match(prod_size):
        failures.append(
            f"{service} 在 {display(COMPOSE_PROD)} 的 `/tmp` size `{prod_size}` "
            "不是 `${变量:?说明}` 无默认值形态。生产 worker-queue 资源合同"
            "（Issue #494/#502）要求值由外部 deploy/.env.prod 提供；带默认值意味着"
            "漏配时静默绕过合同，而这块盘写满就等量吃掉宿主内存。"
        )

    return failures


# ---- 日志留存下限（Trace #373 H2 / S-H2-2，Issue #343）-----------------------
# 提高 json-file 驱动内的单容器日志上限，作为宿主侧收集（deploy/collect-
# container-logs.sh + deploy/lingxi-container-logs.logrotate）之外的第一层缓冲：
# 收集脚本按增量追加，即便某一轮收集因故延迟，docker 自己的 json-file 也要留够
# 窗口，不能让还没被收集走的部分先被 docker 自己的轮转吞掉。
LOG_MAX_SIZE_FLOOR_MB = 50.0
LOG_MAX_FILE_FLOOR = 5


def _parse_log_size_mb(raw: str) -> float | None:
    """把 compose 的 `max-size` 写法（如 `"50m"`、`"1g"`、裸数字字节）解析成 MB。"""

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMgG]?)", raw.strip().strip("'\""))
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit in ("k", "K"):
        return value / 1024
    if unit in ("m", "M"):
        return value
    if unit in ("g", "G"):
        return value * 1024
    return value / 1024 / 1024


def _check_log_retention_floor_in(compose_path: pathlib.Path, *, require_all_services: bool) -> list[str]:
    """核对一份 compose 文本里，出现了 `logging.options` 的服务是否达到取证
    留存下限。

    `require_all_services=True`（基线 `compose.yaml`）：全部六个服务必须都
    声明 `logging.options`。`require_all_services=False`（stage/prod 覆盖
    文件）：声明本身可选——覆盖文件当前并不覆盖 `logging`，完全不声明时不
    报错；但**一旦声明了**（哪怕只覆盖其中一个服务），标准同样是不得低于
    下限，不因为"这是覆盖文件"就放宽（独立审查 P1-2：覆盖文件的值才是真正
    部署时生效的值）。
    """

    failures: list[str] = []
    text = strip_comments(read(compose_path))
    for service in RESOURCE_LIMITED_SERVICES:
        block = service_block(text, service)
        if block is None:
            if require_all_services:
                failures.append(f"{display(compose_path)} 找不到 service `{service}`")
            continue
        logging_block = service_block(block, "logging") or ""
        options_block = service_block(logging_block, "options") or ""
        if not options_block.strip():
            if require_all_services:
                failures.append(
                    f"{service} 的 logging.options 缺 max-size 或 max-file（{display(compose_path)}）"
                )
            continue
        size_match = re.search(r'^\s*max-size:\s*"?([\w.]+)"?\s*$', options_block, re.MULTILINE)
        file_match = re.search(r'^\s*max-file:\s*"?(\d+)"?\s*$', options_block, re.MULTILINE)
        if size_match is None or file_match is None:
            failures.append(
                f"{service} 的 logging.options 在 {display(compose_path)} 缺 max-size 或 max-file"
            )
            continue
        size_mb = _parse_log_size_mb(size_match.group(1))
        if size_mb is None or size_mb < LOG_MAX_SIZE_FLOOR_MB:
            failures.append(
                f"{service} 的 json-file max-size 在 {display(compose_path)} 是 {size_match.group(1)}，"
                f"低于取证留存要求的下限 {LOG_MAX_SIZE_FLOOR_MB:.0f}m（Issue #343）"
            )
        file_count = int(file_match.group(1))
        if file_count < LOG_MAX_FILE_FLOOR:
            failures.append(
                f"{service} 的 json-file max-file 在 {display(compose_path)} 是 {file_count}，"
                f"低于取证留存要求的下限 {LOG_MAX_FILE_FLOOR}（Issue #343）"
            )
    return failures


def check_log_retention_floor() -> list[str]:
    """六个服务的 json-file `max-size`/`max-file` 不得低于取证留存下限（#343）；
    `compose.stage.yaml`/`compose.prod.yaml` 一旦也覆盖了 `logging.options`，
    同样不得低于下限（独立审查 P1-2）。

    下限本身是"容器存活期间的第一层缓冲够不够宽"，不是 30 天窗口本身——30 天
    窗口由宿主侧收集脚本 + logrotate 独立保证（deploy/日志留存.md），门禁这里
    只挡"有人把 compose 里的 max-size/max-file 改回了取证要求之前的值"。
    """

    failures: list[str] = []
    failures += _check_log_retention_floor_in(COMPOSE_BASE, require_all_services=True)
    failures += _check_log_retention_floor_in(COMPOSE_STAGE, require_all_services=False)
    failures += _check_log_retention_floor_in(COMPOSE_PROD, require_all_services=False)
    return failures


def check_compose_contract() -> list[str]:
    failures: list[str] = []
    base = strip_comments(read(COMPOSE_BASE))

    # M2-62-14：生产只拉镜像，不构建。三个 compose 文件里一个 `build:` 键都不许有。
    for path in (COMPOSE_BASE, COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        if re.search(r"^\s*build:\s*$|^\s*build:\s+\S", text, re.MULTILINE):
            failures.append(
                f"{display(path)} 含 `build:` 键。生产只拉镜像、不构建"
                "（架构设计「八、部署与发布」）：compose 里存在构建定义，"
                "`biplus-prod` 上就存在「顺手改一行再 build 一下」这条路径，"
                "镜像 tag 就不再是被冻结的版本。"
            )

    # M2-62-15：镜像引用必须不可变。
    for path in (COMPOSE_BASE, COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^\s*image:\s*(\S.*?)\s*$", line)
            if match is None:
                continue
            reference = match.group(1)
            if "@sha256:" in reference:
                continue
            if "${LINGXI_IMAGE_TAG" in reference:
                # `${VAR:-默认}` 带默认值＝漏设变量时**静默**用上那个默认，而默认值
                # 可以是任何东西（包括 latest）。必须是 `${VAR:?...}` 的"缺了就报错退出"
                # 形态（内审 P2-2）。
                if re.search(r"\$\{LINGXI_IMAGE_(TAG|REGISTRY)[^}]*:-", reference):
                    failures.append(
                        f"{display(path)}:{line_number} 的镜像引用 "
                        f"`{reference}` 用了 `${{VAR:-默认}}` 形态。带默认值意味着漏设变量时"
                        "会**静默**拉一个别的镜像；必须用 `${VAR:?说明}`，缺了就报错退出。"
                    )
                continue
            failures.append(
                f"{display(path)}:{line_number} 的镜像引用 "
                f"`{reference}` 不是不可变引用。必须是 `@sha256:...`，"
                "或 tag 取自 ${LINGXI_IMAGE_TAG}（其值形如 <YYYYMMDD>-<12 位 sha>）。"
                "禁止 latest 或分支名——它们会让「切回上一个 tag」这个回滚动作失去意义。"
            )

    # .env.example 里的示范 tag 也必须是不可变形状：它是所有人抄写的模板。
    env_text = read(ENV_EXAMPLE)
    tag_match = re.search(r"^LINGXI_IMAGE_TAG=(\S+)\s*$", env_text, re.MULTILINE)
    if tag_match is None:
        failures.append("deploy/.env.example 里没有 LINGXI_IMAGE_TAG 示范值")
    elif not IMMUTABLE_TAG.match(tag_match.group(1)):
        failures.append(
            f"deploy/.env.example 的 LINGXI_IMAGE_TAG 示范值 `{tag_match.group(1)}` "
            "不是 <YYYYMMDD>-<12 位 sha> 形状。模板里写什么，部署时多半就照抄什么。"
        )

    scheduler = service_block(base, "scheduler") or ""
    gateway = service_block(base, "gateway") or ""
    worker = service_block(base, "worker") or ""
    worker_queue = service_block(base, "worker-queue") or ""
    migrate = service_block(base, "migrate") or ""
    reauthorize = service_block(base, "reauthorize") or ""

    if not reauthorize:
        failures.append("deploy/compose.yaml 缺少正式重授权一次性 job `reauthorize`")
    else:
        if not re.search(r"^\s*profiles:\s*\[.*job.*\]\s*$", reauthorize, re.MULTILINE):
            failures.append("reauthorize 必须放在 job profile，不能作为常驻服务启动")
        if not re.search(r'^\s*restart:\s*["\']?no["\']?\s*$', reauthorize, re.MULTILINE):
            failures.append('reauthorize 是一次性作业，必须显式使用 `restart: "no"`')
        if re.search(r"^\s*stop_grace_period:", reauthorize, re.MULTILINE):
            failures.append("reauthorize 是一次性作业，不应配置 stop_grace_period")
        if not re.search(r"^\s*read_only:\s*true\s*$", reauthorize, re.MULTILINE):
            failures.append("reauthorize 缺 `read_only: true`")
        user = re.search(r"^\s*user:\s*(\S+)\s*$", reauthorize, re.MULTILINE)
        if user is None or user.group(1).strip("'\"") != "10001:10001":
            failures.append("reauthorize 必须与 scheduler 一样以 `10001:10001` 运行")
        if re.search(r"^\s*(stdin_open|tty):", reauthorize, re.MULTILINE):
            failures.append("reauthorize 通过 OAuth Bridge 接收回调，不应开启交互终端")
        if not re.search(
            r'^\s*command:\s*\["python",\s*"-m",\s*"lingxi\.apps\.reauthorize"\]\s*$',
            reauthorize,
            re.MULTILINE,
        ):
            failures.append("reauthorize 没有指向随镜像发布的 `python -m lingxi.apps.reauthorize`")
        if not re.search(r"lingxi-credentials:/var/lib/lingxi/credentials", reauthorize):
            failures.append("reauthorize 必须挂载凭据持久卷")
        for variable in ("LINGXI_DELEGATED_CREDENTIAL_PATH", "LINGXI_DELEGATED_REAUTH_STATE_PATH"):
            if not re.search(
                rf"^\s*{variable}:\s*/var/lib/lingxi/credentials/\S+\s*$",
                reauthorize,
                re.MULTILINE,
            ):
                failures.append(f"reauthorize 必须在凭据卷内声明 {variable}")

    # M2-62-27 / D14：凭据路径必须落在**声明为持久卷**的挂载点下。
    path_match = re.search(
        r"^\s*LINGXI_DELEGATED_CREDENTIAL_PATH:\s*(\S+)\s*$", scheduler, re.MULTILINE
    )
    if path_match is None:
        failures.append("deploy/compose.yaml 的 scheduler 没有设 LINGXI_DELEGATED_CREDENTIAL_PATH")
    else:
        credential_path = path_match.group(1).strip("'\"")
        mount_targets = re.findall(r"^\s*-\s*[\w.-]+:(/\S+?)\s*$", scheduler, re.MULTILINE)
        if not any(credential_path.startswith(target.rstrip("/") + "/") for target in mount_targets):
            failures.append(
                f"专用授权凭据路径 `{credential_path}` 不在 scheduler 的任何持久卷挂载点下"
                f"（当前挂载点：{mount_targets or '无'}）。\n"
                "      凭据文件必须**跨部署持久**：写在容器本地路径上，每次镜像替换都会丢失授权，"
                "而这个后果要到下一次轮换才暴露，届时表现为「续期突然停止工作」。\n"
                "      部署目标是对该凭据「零特殊处理」，重新授权只是保底（产品负责人 2026-08-05）。"
            )

    # M2-62-28：用户目录与凭据必须是**两个不同的卷**。
    credential_sources = re.findall(r"^\s*-\s*([\w.-]+):/var/lib/lingxi/credentials", scheduler, re.MULTILINE)
    user_sources = re.findall(r"^\s*-\s*([\w.-]+):/var/lib/lingxi/users", worker, re.MULTILINE)
    if credential_sources and user_sources and set(credential_sources) & set(user_sources):
        failures.append(
            "凭据卷与用户目录卷是同一个卷。两者的备份周期、恢复策略与删除语义都不同，"
            "合成一个卷会让「只恢复凭据」或「只清用户目录」变成不可能。"
        )

    # M2-62-02：scheduler 只读根文件系统。
    if not re.search(r"^\s*read_only:\s*true\s*$", scheduler, re.MULTILINE):
        failures.append("deploy/compose.yaml 的 scheduler 缺 `read_only: true`（断言 V-部署-02）")

    # M2-62-02 / M2-62-23：一次性作业不得配重启策略。
    for name, block in (("worker", worker), ("migrate", migrate)):
        restart = re.search(r"^\s*restart:\s*(\S+)\s*$", block, re.MULTILINE)
        if restart is None:
            failures.append(f"{name} 没有显式 restart 策略；一次性作业必须写明 `restart: \"no\"`")
        elif restart.group(1).strip("'\"") != "no":
            failures.append(
                f"{name} 的 restart 是 `{restart.group(1)}`，必须是 \"no\"。\n"
                f"      {name} 是**一次性作业**不是常驻服务：worker 跑完一个回合就退出，"
                "migrate 跑完一次迁移就退出。给它们配重启策略会把「作业正常结束」变成无限重启循环，"
                "并让人误以为该服务已经上线。"
            )
        if re.search(r"^\s*stop_grace_period:", block, re.MULTILINE):
            failures.append(f"{name} 是一次性作业，不该有 stop_grace_period（那是常驻服务的概念）")

    # gateway 也必须只读根（断言 V-部署-02）。
    if not re.search(r"^\s*read_only:\s*true\s*$", gateway, re.MULTILINE):
        failures.append("deploy/compose.yaml 的 gateway 缺 `read_only: true`（断言 V-部署-02）")

    # gateway **必须放在非默认 profile 里**：它入队之后没有消费者（worker 是单回合
    # CLI，不领任务）。让 `up -d` 顺手把它拉起来，等于开始接收真实用户消息并排进一个
    # 没人处理的队列——用户看到的是"发了没反应"。S4 下半接线后才可转默认。
    if not re.search(r"^\s*profiles:\s*\[.*gateway.*\]\s*$", gateway, re.MULTILINE):
        failures.append(
            "deploy/compose.yaml 的 gateway 没有放进非默认 profile。"
            "队列此刻没有消费者，`up -d` 顺手启动它等于把真实用户消息排进无人处理的队列。"
        )

    # gateway 的停机宽限期，依据来自它自己的配置。
    gateway_grace = re.search(r"^\s*stop_grace_period:\s*(\S+)\s*$", gateway, re.MULTILINE)
    try:
        gateway_worst_case = _gateway_worst_case_seconds()
        gateway_required = math.ceil(gateway_worst_case * SAFETY_FACTOR)
    except ValueError as error:
        failures.append(str(error))
        gateway_worst_case = None
        gateway_required = None
    if gateway_required is not None:
        if gateway_grace is None:
            failures.append(
                "deploy/compose.yaml 的 gateway 没有显式 stop_grace_period。"
                f"最坏需要 {gateway_worst_case:.1f} 秒（停机超时 + 出站 HTTP + 一次数据库事务）；"
                "中途 SIGKILL 会留下「抢占了话题但任务没写进去」的中间态。"
            )
        else:
            actual = parse_duration_seconds(gateway_grace.group(1))
            if actual is None or actual < gateway_required:
                failures.append(
                    f"gateway 的 stop_grace_period 是 {gateway_grace.group(1)}，"
                    f"低于要求的 {gateway_required} 秒（停机超时 "
                    f"{_gateway_shutdown_timeout()}s + 出站 {max(1.0, (_gateway_shutdown_timeout() or 0)/4)}s "
                    f"+ 数据库 {_database_operation_seconds():.0f}s，再乘 {SAFETY_FACTOR}）。"
                )

    # ---- worker-queue：常驻 queue worker（Issue #153）-------------------------
    if not worker_queue:
        failures.append(
            "deploy/compose.yaml 缺少常驻 queue worker service `worker-queue`"
            "（Issue #153：Stage/MVP 受控部署 profile 需要 scheduler、gateway、"
            "常驻 queue worker 三者同时启动）。"
        )
    else:
        if not re.search(r"^\s*profiles:\s*\[.*mvp.*\]\s*$", worker_queue, re.MULTILINE):
            failures.append(
                "worker-queue 没有放进 `mvp` profile：Stage/MVP 受控部署形态"
                "必须能用一个明确命名的 profile 同时拉起它。"
            )
        if not re.search(r'^\s*restart:\s*unless-stopped\s*$', worker_queue, re.MULTILINE):
            failures.append(
                "worker-queue 是常驻服务，必须 `restart: unless-stopped`"
                "（与一次性 `worker` job 的 `restart: \"no\"` 刻意不同）。"
            )
        if not re.search(
            r'^\s*LINGXI_WORKER_MODE:\s*queue\s*$', worker_queue, re.MULTILINE
        ):
            failures.append("worker-queue 没有设置 LINGXI_WORKER_MODE=queue，会退化成一次性 turn 模式")
        # 精确解析 volumes: 列表，不再用整块字符串包含判断（Issue #261，对齐
        # check_scheduler_user_volume 已经做的 2026-08-19 修复／外部独立审查 F2 同类
        # 假绿口子）：字符串包含判断分不清"真的挂了这个卷"与"这个子串恰好出现在
        # environment/labels 等别处"，也分不清"可写挂载"与"挂成 :ro"。
        worker_queue_user_mounts = [
            (source, target, mode)
            for source, target, mode in _volume_mounts(worker_queue)
            if source == "lingxi-users" and target == "/var/lib/lingxi/users"
        ]
        if not worker_queue_user_mounts:
            failures.append("worker-queue 必须挂载用户环境持久卷（与一次性 worker job 一致）")
        elif any(mode == "ro" for _, _, mode in worker_queue_user_mounts):
            failures.append(
                "worker-queue 把用户环境持久卷 lingxi-users 挂成了只读（`:ro`）。"
                "挂载模式须与 worker job 一致（可写），只读是明显的操作失误信号；"
                "门禁如果只做整块字符串包含判断，会因为 `:ro` 挂载的子串仍然命中"
                "`lingxi-users:/var/lib/lingxi/users` 而误判为通过。"
            )
        if not re.search(r"^\s*read_only:\s*true\s*$", worker_queue, re.MULTILINE):
            failures.append("worker-queue 缺 `read_only: true`（断言 V-部署-02，与 scheduler/gateway 同一要求）")

        worker_queue_grace = re.search(
            r"^\s*stop_grace_period:\s*(\S+)\s*$", worker_queue, re.MULTILINE
        )
        try:
            worker_worst_case = _worker_worst_case_seconds()
            worker_required = math.ceil(worker_worst_case * SAFETY_FACTOR)
        except ValueError as error:
            failures.append(str(error))
            worker_worst_case = None
            worker_required = None
        if worker_required is not None:
            if worker_queue_grace is None:
                failures.append(
                    "deploy/compose.yaml 的 worker-queue 没有显式 stop_grace_period。"
                    f"最坏需要 {worker_worst_case:.1f} 秒（SIGTERM 停机预算 + 一次终态"
                    "写库事务）；中途 SIGKILL 会让一次已经在途的 Agent 回合失去被"
                    "cooperative 中断的机会。"
                )
            else:
                actual = parse_duration_seconds(worker_queue_grace.group(1))
                if actual is None or actual < worker_required:
                    failures.append(
                        f"worker-queue 的 stop_grace_period 是 "
                        f"{worker_queue_grace.group(1)}，低于要求的 {worker_required} 秒"
                        f"（worker 自身 SIGTERM 停机预算 + 数据库终态写入预算，再乘"
                        f" {SAFETY_FACTOR}）。"
                    )

    # ---- healthcheck：三个常驻服务都必须有（Issue #153，合同第 5 条）---------
    for name, block in (
        ("scheduler", scheduler),
        ("gateway", gateway),
        ("worker-queue", worker_queue),
    ):
        if not block:
            continue
        if not re.search(r"^\s*healthcheck:\s*$", block, re.MULTILINE):
            failures.append(
                f"{name} 没有 healthcheck。合同第 5 条要求依赖不可用时健康检查"
                "必须如实变红，不能只靠容器 PID 存活。"
            )
        elif "lingxi.apps.healthcheck" not in block:
            failures.append(
                f"{name} 的 healthcheck 没有调用 `python -m lingxi.apps.healthcheck`，"
                "无法证明它真的探测了依赖可达性与主循环活性，而不是一条摆设命令。"
            )

    # M2-62-29 / M2-62-30：非 root、能力最小。
    for name, block in (("scheduler", scheduler), ("gateway", gateway),
                        ("worker", worker), ("worker-queue", worker_queue),
                        ("migrate", migrate), ("reauthorize", reauthorize)):
        user = re.search(r"^\s*user:\s*(\S+)\s*$", block, re.MULTILINE)
        if user is None:
            failures.append(f"{name} 没有显式 `user:`，无法在 compose 层面核对非 root")
        else:
            value = user.group(1).strip("'\"")
            if value.split(":")[0] in {"0", "root"}:
                failures.append(f"{name} 以 root 运行（user: {value}），违反断言 V-部署-07")
        if re.search(r"^\s*cap_add:", block, re.MULTILINE):
            failures.append(
                f"{name} 有 cap_add。当前实现不需要任何额外能力"
                "（CAP_SETUID 属尚未实现的组件，出现即超出本切片范围）。"
            )
        if re.search(r"^\s*privileged:\s*true", block, re.MULTILINE):
            failures.append(f"{name} 配了 privileged: true")

    # ---- 覆盖文件不得把基线里的安全设置改回去（内审 P2-2）--------------------
    # 上面全部只看 deploy/compose.yaml。compose 的覆盖文件是**后加载后生效**的：
    # 在 compose.prod.yaml 里写一行 `user: root` 或 `privileged: true` 就能把基线的
    # 设置整个盖掉，而基线文件本身一个字没动——静态检查全绿，实际以 root 跑。
    for path in (COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        for service in ("scheduler", "gateway", "worker", "worker-queue", "migrate", "reauthorize"):
            block = service_block(text, service)
            if block is None:
                continue
            user = re.search(r"^\s*user:\s*(\S+)\s*$", block, re.MULTILINE)
            if user is not None and user.group(1).strip("'\"").split(":")[0] in {"0", "root"}:
                failures.append(
                    f"{display(path)} 的 {service} 用覆盖把 user 改成了 root"
                )
            if re.search(r"^\s*cap_add:", block, re.MULTILINE):
                failures.append(f"{display(path)} 的 {service} 用覆盖加了 cap_add")
            if re.search(r"^\s*privileged:\s*true", block, re.MULTILINE):
                failures.append(f"{display(path)} 的 {service} 用覆盖加了 privileged")
            if re.search(r"^\s*read_only:\s*false", block, re.MULTILINE):
                failures.append(f"{display(path)} 的 {service} 用覆盖关掉了 read_only")
            grace = re.search(r"^\s*stop_grace_period:\s*(\S+)\s*$", block, re.MULTILINE)
            if grace is not None:
                seconds = parse_duration_seconds(grace.group(1))
                try:
                    required = math.ceil(_worst_case_seconds() * SAFETY_FACTOR)
                except ValueError as error:
                    failures.append(str(error))
                    required = None
                if required is not None and (seconds is None or seconds < required):
                    failures.append(
                        f"{display(path)} 的 {service} 用覆盖把 "
                        f"stop_grace_period 改成了 {grace.group(1)}，低于要求的 {required} 秒"
                    )
            restart = re.search(r"^\s*restart:\s*(\S+)\s*$", block, re.MULTILINE)
            if restart is not None and service in {"worker", "migrate", "reauthorize"}:
                if restart.group(1).strip("'\"") != "no":
                    failures.append(
                        f"{display(path)} 的 {service} 用覆盖把 restart "
                        f"改成了 {restart.group(1)}；一次性作业必须是 \"no\""
                    )

    # ---- 凭据按服务分文件（codex 审查 P1-1；PR #173 复核 P1-2 收紧）-----------
    # 两个服务共用一个 env_file 时，其中一边会拿到它不该拿到的凭据。这不是"多给
    # 几个变量"：worker / worker-queue 跑 Agent SDK，SDK 把进程环境继承给 Claude
    # CLI 与每个 MCP 子进程，于是那些凭据一路流进模型执行环境——正是产品合同
    # 「凭据不进用户环境」要挡的方向。
    #
    # **不再为 `worker`/`worker-queue` 放行共用**（PR #173 复核 P1-2 撤销了这条
    # 例外）：两者虽是同一镜像、同一套 `LINGXI_WORKER_*` 变量面，但 worker-queue
    # 常驻领任务、必须有 `LINGXI_POSTGRES_DSN`，一次性 `worker` job 从不碰数据库
    # ——共用同一份文件时，要么 worker 意外获得数据库凭据（经 Agent SDK 继承给模型
    # 执行环境），要么 worker-queue 拿不到 DSN 而无限崩溃重启（两种后果都不可接受，
    # 见 deploy/.env.example「文件四/文件五」的说明）。任何两个服务共用 env_file
    # 现在一律判定失败，没有例外。
    for path in (COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        seen: dict[str, list[str]] = {}
        for service in ("scheduler", "gateway", "worker", "worker-queue", "migrate", "reauthorize"):
            block = service_block(text, service)
            if block is None:
                continue
            for env_file in re.findall(r"^\s*-\s*(\./\S+)\s*$", block, re.MULTILINE):
                seen.setdefault(env_file, []).append(service)
        for env_file, services in sorted(seen.items()):
            if len(services) > 1:
                failures.append(
                    f"{display(path)}：{'、'.join(services)} 共用同一个 "
                    f"env_file `{env_file}`。凭据必须按服务分文件——worker 跑 Agent SDK，"
                    "SDK 会把进程环境继承给 Claude CLI 与 MCP 子进程，共享 env 等于把"
                    "数据库与飞书凭据送进模型执行环境。"
                )

    return failures


def check_dockerfile() -> list[str]:
    failures: list[str] = []
    text = strip_comments(read(DOCKERFILE))

    # M2-62-29 / D13：每个交付阶段都必须切非 root。
    delivery_stages = re.findall(r"^FROM\s+\S+\s+AS\s+(scheduler|worker|migrate)\s*$", text, re.MULTILINE)
    for stage in ("scheduler", "worker", "migrate"):
        if stage not in delivery_stages:
            failures.append(f"Dockerfile 里找不到交付阶段 `{stage}`")
    user_directives = re.findall(r"^USER\s+(\S+)\s*$", text, re.MULTILINE)
    if len(user_directives) < len(delivery_stages):
        failures.append(
            f"Dockerfile 有 {len(delivery_stages)} 个交付阶段但只有 {len(user_directives)} 条 USER 指令。"
            "每个交付阶段都必须显式切到非 root（断言 V-部署-07）——"
            "少一条，那个镜像就以 root 跑。"
        )
    for directive in user_directives:
        if directive.split(":")[0] in {"0", "root"}:
            failures.append(f"Dockerfile 的 `USER {directive}` 是 root")

    # M2-62-16 / D4：镜像里不许预置任何凭据。
    for line_number, line in enumerate(text.splitlines(), start=1):
        for variable in CREDENTIAL_VARIABLES:
            if re.search(rf"^\s*(ENV|ARG)\s+.*\b{re.escape(variable)}\s*=", line):
                failures.append(
                    f"Dockerfile:{line_number} 用 ENV/ARG 给凭据变量 `{variable}` 赋了值。"
                    "凭据一律运行期注入：写进镜像等于把它发布到镜像仓库，"
                    "且任何人 `docker inspect` 就能读到。"
                )

    # M2-62-07：构建不依赖构建机本地状态。
    if re.search(r"^\s*ADD\s+https?://", text, re.MULTILINE):
        failures.append("Dockerfile 用 `ADD <url>` 拉取远程内容：版本不受控，两次构建可能不同")
    if re.search(r"apt-get\s+install(?!.*=)", text) and "apt-get install" in text:
        failures.append("Dockerfile 里有不锁版本的 `apt-get install`")

    # 基础镜像必须按 digest 固定：tag 会移动，两次构建就会拿到不同底座。
    for line in text.splitlines():
        match = re.match(r"^FROM\s+(\S+)", line)
        if match and not match.group(1).startswith("python@sha256:") and "AS" in line:
            if not re.match(r"^FROM\s+(base|build-base|build-\w+)\b", line):
                failures.append(
                    f"Dockerfile 的 `{line.strip()}` 不是按 digest 固定的基础镜像。"
                    "tag 会移动，同一提交今天与下周构建会拿到不同底座，V-部署-06 就成了运气。"
                )
    return failures


def check_dockerignore() -> list[str]:
    """M2-62-08：排除清单必须覆盖会破坏可复现性与凭据边界的那几类。"""

    entries = {
        line.strip()
        for line in read(DOCKERIGNORE).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    failures = []

    # 顶层就够的：这几样只会出现在上下文根目录。
    for entry, reason in {
        ".git": "版本控制目录会把整个历史带进构建上下文",
        ".claude": "代理工作现场不属于制品",
        ".env": "凭据文件绝不进构建上下文",
    }.items():
        if not any(item == entry or item.startswith(entry) for item in entries):
            failures.append(f".dockerignore 没有排除 `{entry}`：{reason}")

    # **必须带 `**/` 前缀**（内审 P1-2）。Docker 的排除模式按 Go 的 filepath.Match
    # 语义匹配整条相对路径，其中 `*` 不跨 `/`：裸写 `__pycache__` 只排除上下文根目录下
    # 的那一个，`migrations/alembic/__pycache__` 照样会被打包进去。而本仓库跑一次
    # alembic 门禁就会生成它——排除清单看起来写了、实际没生效，是最难发现的一类。
    for entry, reason in {
        "**/__pycache__": "嵌套的 __pycache__（本仓库跑一次门禁就会在 migrations/alembic/ 下生成）",
        "**/*.py[cod]": "嵌套的字节码文件；它内嵌宿主机绝对路径，同时破坏可复现构建与路径不泄漏",
        "**/.venv": "嵌套的本机虚拟环境",
    }.items():
        if entry not in entries:
            bare = entry[3:]
            if any(item == bare for item in entries):
                failures.append(
                    f".dockerignore 写的是裸 `{bare}`，必须写成 `{entry}`："
                    f"裸模式只匹配上下文根目录下的那一个，不匹配{reason}。"
                )
            else:
                failures.append(f".dockerignore 没有排除 `{entry}`：{reason}")
    return failures


def check_ci_workflow() -> list[str]:
    failures: list[str] = []
    full = read(CI_WORKFLOW)
    story = read(STORY_WORKFLOW)
    publish = read(PUBLISH_WORKFLOW)

    # M2-62-41 / D16：`extra: [...]` 只能有一行。
    #
    # check_installed_package.py 的 `_MATRIX_LINE` 用的是 `re.search`，只取**第一个**
    # 匹配。新增一条构建矩阵若也用 `extra:` 作键，三方对账就会拿错集合去比对，
    # 既有门禁从此失效而且全绿——正是「绿色的测试不等于会变红的测试」。
    extra_lines = re.findall(r"^[ \t]*extra:[ \t]*\[", full, re.MULTILINE)
    if len(extra_lines) != 1:
        failures.append(
            f"ci.yml 里有 {len(extra_lines)} 行 `extra: [...]`，必须恰好 1 行。"
            "check_installed_package.py 的三方对账用 re.search 取首个匹配，"
            "多一行就会让它拿错集合去对账，而且不会报错。"
            "新增的构建矩阵请用别的键名（本仓库用 `service:`）。"
        )

    # main 合并后只发布，不重跑完整门禁；发布触发路径仍必须覆盖全部部署输入。
    for needed in ("Dockerfile", "deploy/**", ".dockerignore"):
        if f"'{needed}'" not in publish and f'"{needed}"' not in publish:
            failures.append(
                f"publish.yml 的 push `paths:` 没有 `{needed}`：合并该输入后不会触发发布。"
            )

    if "workflow_call:" not in full:
        failures.append("ci.yml 缺少 workflow_call，Story 高风险改动无法复用同一份 Epic Full。")
    if re.search(r"^  push:\s*$", strip_comments(full), re.MULTILINE):
        failures.append("ci.yml 仍监听 main push：合并后会重复运行完整门禁。")

    candidate_match = re.search(
        r"^  candidate:\n(.*?)(?=^  \w[\w-]*:\n|\Z)", full, re.MULTILINE | re.DOTALL
    )
    if candidate_match is None:
        failures.append("ci.yml 缺少 candidate job，无法形成稳定的 Epic Full 结论。")
    else:
        candidate = candidate_match.group(1)
        needs = re.search(r"^\s*needs:\s*\[([^\]]*)\]", candidate, re.MULTILINE)
        required = {"classify", "docs", "l1", "gate", "extras", "image"}
        actual = {item.strip() for item in needs.group(1).split(",")} if needs else set()
        if actual != required:
            failures.append(
                f"ci.yml candidate needs 为 {sorted(actual)}，要求恰好 {sorted(required)}。"
            )
        for marker in ("write_epic_candidate.py", "upload-artifact@"):
            if marker not in candidate:
                failures.append(f"ci.yml candidate 缺少 `{marker}`，main 无法回读候选身份。")

    # Issue #150：PR 候选四镜像必须真的导出、自校验并留存成可下载 artifact，
    # 不能只是"构建过、验证过契约"就完事——那些镜像在 job 结束后随 runner 一起消失，
    # biai-stage 拿不到与这次构建逐字节一致的对象。
    image_match = re.search(
        r"^  image:\n(.*?)(?=^  \w[\w-]*:\n|\Z)", full, re.MULTILINE | re.DOTALL
    )
    if image_match is None:
        failures.append("ci.yml 缺少 image job，无法产出 PR 候选四镜像 artifact（Issue #150）。")
    else:
        image_body = image_match.group(1)
        for marker in (
            "write_epic_candidate_images.py",
            "verify_epic_candidate_bundle.py",
            "epic-candidate-images-pr-",
            "upload-artifact@",
        ):
            if marker not in image_body:
                failures.append(
                    f"ci.yml 的 image job 缺少 `{marker}`：PR 候选镜像制品链不完整（Issue #150）。"
                )

    # 纯文档 main PR 的稳定 required check 仍叫 Epic Full，但不得启动真库、extras
    # 或镜像构建。遗漏这些标记会让文档改动又悄悄退化为完整回归。
    for marker in (
        "name: Epic Full / docs",
        "scripts/ci/verify_docs.sh",
        "needs.classify.outputs.mode == 'docs'",
        "needs.classify.outputs.mode != 'docs'",
        "name: Epic Full / l1",
        "scripts/ci/check_l1_assets.py",
        "needs.classify.outputs.risk_level == 'l1'",
        "needs.classify.outputs.risk_level != 'l1'",
        "scripts/ci/check_permission_impact.py",
        "needs.classify.outputs.risk_level == 'l3'",
    ):
        if marker not in full:
            failures.append(f"ci.yml 缺少分级 Epic 路由标记 `{marker}`。")

    for marker in (
        "'epic/**'",
        "classify_story_changes.py",
        "verify_docs.sh",
        "uses: ./.github/workflows/ci.yml",
        "name: Story Fast",
        "name: Story / content l1",
        "scripts/ci/check_l1_assets.py",
        "needs.classify.outputs.risk_level == 'l1'",
    ):
        if marker not in story:
            failures.append(f"story.yml 缺少 `{marker}`，Story Fast 路由不完整。")

    # `packages: write` 只能存在于 main push 工作流，且必须等待候选身份核对。
    if "packages: write" in full or "packages: write" in story:
        failures.append("PR 工作流声明了 packages: write；PR 不得获得镜像仓库写权限。")
    publish_jobs = list(
        re.finditer(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", publish, re.MULTILINE | re.DOTALL)
    )
    for match in publish_jobs:
        job_name, body = match.group(1), match.group(2)
        header = body.split("\n    steps:", 1)[0]
        if not re.search(r"^\s*packages:\s*write\s*$", header, re.MULTILINE):
            continue
        gated = re.search(r"^\s*if:.*github\.event_name\s*==\s*'push'", header, re.MULTILINE) and \
            re.search(r"^\s*if:.*refs/heads/main", header, re.MULTILINE)
        if not gated:
            failures.append(
                f"publish.yml 的 job `{job_name}` 声明了 `packages: write`，但没有 "
                "`if: github.event_name == 'push' && github.ref == 'refs/heads/main'` 限定。\n"
                "      同仓分支的 pull_request 会让这个 job 照样拿到写令牌——"
                "改一行 workflow 就能在 PR 阶段推 / 覆盖 GHCR，绕过合并门禁。"
            )
        needs = re.search(r"^\s*needs:\s*(\[[^\]]*\]|\S.*)$", header, re.MULTILINE)
        if needs is None or "candidate" not in needs.group(1):
            failures.append(
                f"publish.yml 的 job `{job_name}` 声明了 `packages: write`，但它的 `needs` "
                f"不含 `candidate`（当前：{needs.group(1) if needs else '无 needs'}）。"
            )

    if "verify_epic_candidate.py" not in publish:
        failures.append("publish.yml 没有回读 Epic Full 候选证明。")
    if publish.count("scripts/ci/build_image.sh") != 1:
        failures.append("publish.yml 必须只保留一处四镜像构建循环，不得重复构建。")
    for forbidden in (
        "postgres:16",
        "extra: [",
        "--no-cache",
        "verify_image_contract.sh",
        "verify_old_image_new_schema.sh",
        "verify_repository.sh",
    ):
        if forbidden in strip_comments(publish):
            failures.append(f"publish.yml 含完整门禁动作 `{forbidden}`：main 合并后不得重复验收。")

    # M2-62-35 / D15：推送步骤不得吞错。
    if "push_image.py" in publish:
        for line in publish.splitlines():
            if "continue-on-error" in line and not line.lstrip().startswith("#"):
                failures.append(
                    "publish.yml 用了 continue-on-error。GHCR 推送的降级必须是**显式分支**："
                    "权限不足时上传 artifact 并在 summary 写明「未推送」，"
                    "而不是让任何失败都变成绿色。两条路径同样绿色收口 = 无法区分"
                    "「推上去了」与「没推上去」。"
                )
    return failures


def main() -> int:
    checks = (
        ("停止宽限期与源码常量联动", check_stop_grace_period),
        ("数据库超时与停机上界依据", check_database_timeouts),
        ("worker-queue env.example 示范值", check_worker_queue_env_example),
        ("worker-queue 并发上限与生产资源合同", check_worker_concurrency_contract),
        ("闸⑤配置项 .env.example 示范覆盖", check_onboarding_gate_env_example),
        ("内测轮内容级采集正式环境防护", check_content_capture_prod_guard),
        ("scheduler 用户环境卷挂载", check_scheduler_user_volume),
        ("六服务资源限制结构", check_resource_limits),
        ("worker-queue /tmp 内存盘上限分环境显式", check_worker_tmpfs_capacity),
        ("compose 插值占位符 YAML 安全与渲染可行", check_compose_interpolation_is_yaml_safe),
        ("日志留存下限", check_log_retention_floor),
        ("Compose 部署契约", check_compose_contract),
        ("Dockerfile 契约", check_dockerfile),
        (".dockerignore 覆盖面", check_dockerignore),
        ("CI 工作流契约", check_ci_workflow),
    )
    failures: list[str] = []
    for label, check in checks:
        for failure in check():
            failures.append(f"[{label}] {failure}")

    if failures:
        print("部署编排契约：不通过", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    http_timeout = module_constant(FEISHU_DIRECTORY, "REQUEST_TIMEOUT_SECONDS")
    backoff = module_constant(SCHEDULER_CREDENTIAL_ROTATION, "SAVE_RETRY_BACKOFF_SECONDS")
    _, max_timeout = _postgres_timeout_facts()
    default_database_operation_seconds = _default_database_operation_seconds()
    database_operation_seconds = _database_operation_seconds()
    database_roundtrip_budget_seconds = database_operation_seconds * DATABASE_OPERATION_COUNT
    worst_case = float(http_timeout) + float(sum(backoff)) + database_roundtrip_budget_seconds
    required_dsn_settings = _required_dsn_settings()
    print(
        "部署编排契约：通过（Dockerfile、deploy/ 下 3 个 compose、.dockerignore、Story/Epic/Publish）\n"
        f"  停止宽限期：最坏 {worst_case:.1f}s（续期 HTTP {http_timeout}s + 落盘退避 "
        f"{sum(backoff)}s + 数据库 {database_roundtrip_budget_seconds:.0f}s ="
        f" {DATABASE_OPERATION_COUNT} 次操作 × {database_operation_seconds:.0f}s，"
        f"合法覆盖上界 {max_timeout}s；默认单次 {default_database_operation_seconds:.0f}s）"
        f" × {SAFETY_FACTOR} → 要求 ≥ {math.ceil(worst_case * SAFETY_FACTOR)}s\n"
        f"  示例 DSN 已带 {'、'.join(sorted(required_dsn_settings))}，并与工厂默认值对账；"
        "运行时覆盖走 LINGXI_POSTGRES_*_TIMEOUT_SECONDS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
