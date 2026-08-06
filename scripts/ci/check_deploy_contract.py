#!/usr/bin/env python3
"""部署编排的静态契约检查（Issue #62 / S11）。

守住 Dockerfile、`deploy/` 下的 compose 文件与 `ci.yml` 里那些**改坏了不会有任何东西
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
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

FEISHU_DIRECTORY = REPOSITORY_ROOT / "src" / "lingxi" / "adapters" / "feishu_directory.py"
SCHEDULER_APP = REPOSITORY_ROOT / "src" / "lingxi" / "apps" / "scheduler" / "__init__.py"

# 停止宽限期的数据库往返预算（秒）。
#
# **上一版这里只算 connect_timeout，那个上界是假的**（Issue #62 codex 审查 P1-3）：
# `connect_timeout` 只约束建连，一条已经发出去的 SELECT / INSERT / commit 可以无限期
# 挂着——连接早就建好了。真正的上界需要 DSN 同时带上 `statement_timeout`（约束语句）
# 与 `lock_timeout`（约束等锁，它不算在 statement_timeout 里）。
#
# 这三个值都在部署侧的 DSN 里，本检查看不见运行时的真实值，因此下面按
# deploy/.env.example 里登记的数字建模，并**另外断言示例 DSN 真的带了这三个参数**
# （见 check_database_timeouts）——否则这段算法就只是一段好看的注释。
DSN_CONNECT_TIMEOUT_SECONDS = 5.0
DSN_STATEMENT_TIMEOUT_SECONDS = 3.0
# 每次数据库操作 = 建连 + 一条语句 + 一次提交（提交本身也是语句，同样受 statement_timeout）。
DATABASE_OPERATION_SECONDS = DSN_CONNECT_TIMEOUT_SECONDS + 2 * DSN_STATEMENT_TIMEOUT_SECONDS
# 最坏 5 次操作：_save_with_retry 的 4 次尝试 + 失败后的 1 次 revoke。
DATABASE_OPERATION_COUNT = 5
DATABASE_ROUNDTRIP_BUDGET_SECONDS = DATABASE_OPERATION_SECONDS * DATABASE_OPERATION_COUNT

# 示例 DSN 必须带的参数 → 期望值。缺任何一个，上面的预算就没有依据。
REQUIRED_DSN_SETTINGS = {
    "connect_timeout": "5",
    "statement_timeout": "3000",
    "lock_timeout": "2000",
}

# 安全系数。宽限期不是"刚好够"就行：`_FileLock` 用的是阻塞式 flock，而 DSN 的三个
# 超时是部署侧配置、本检查只能按示例建模。乘 1.5 是给这些不可见量留的余量，
# 也让"把超时改大却不动 compose"必然变红。
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


def _worst_case_seconds() -> float:
    """一次在途轮换在最坏情况下还要跑多久（秒）。读不到常量时抛 ValueError。"""

    http_timeout = module_constant(FEISHU_DIRECTORY, "REQUEST_TIMEOUT_SECONDS")
    backoff = module_constant(SCHEDULER_APP, "SAVE_RETRY_BACKOFF_SECONDS")
    if not isinstance(http_timeout, (int, float)):
        raise ValueError("读不到 REQUEST_TIMEOUT_SECONDS")
    if not isinstance(backoff, (tuple, list)) or not all(isinstance(x, (int, float)) for x in backoff):
        raise ValueError("读不到 SAVE_RETRY_BACKOFF_SECONDS")
    return float(http_timeout) + float(sum(backoff)) + DATABASE_ROUNDTRIP_BUDGET_SECONDS


def check_stop_grace_period() -> list[str]:
    """断言 V-部署-03 / M2-62-24 / M2-62-25：宽限期必须够，而且**与源码常量联动**。

    这一条是本脚本存在的主要理由。`stop_grace_period` 与 `REQUEST_TIMEOUT_SECONDS`
    之间的关系此前只写在注释里——注释不会在有人把超时从 20 秒改到 60 秒时变红。
    SIGKILL 落在"已向飞书换过新凭据、尚未写回数据库"的窗口里等于永久丢失一条一次性
    凭据，没有任何补救手段，只能人工重新授权。
    """

    failures: list[str] = []
    http_timeout = module_constant(FEISHU_DIRECTORY, "REQUEST_TIMEOUT_SECONDS")
    backoff = module_constant(SCHEDULER_APP, "SAVE_RETRY_BACKOFF_SECONDS")
    if not isinstance(http_timeout, (int, float)):
        return [f"读不到 {FEISHU_DIRECTORY.name} 的 REQUEST_TIMEOUT_SECONDS，无法核算停止宽限期"]
    if not isinstance(backoff, (tuple, list)) or not all(isinstance(x, (int, float)) for x in backoff):
        return [f"读不到 {SCHEDULER_APP.name} 的 SAVE_RETRY_BACKOFF_SECONDS，无法核算停止宽限期"]

    worst_case = _worst_case_seconds()
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
            f"数据库往返预算 {DATABASE_ROUNDTRIP_BUDGET_SECONDS}s）。"
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
            f"{DATABASE_ROUNDTRIP_BUDGET_SECONDS}s = {worst_case:.1f}s，"
            f"再乘安全系数 {SAFETY_FACTOR} = {required}s。\n"
            "      改了上述任一常量就必须同步改 deploy/compose.yaml——这正是本检查存在的理由。"
        )
    return failures


def check_database_timeouts() -> list[str]:
    """示例 DSN 必须带齐三个超时参数（Issue #62 codex 审查 P1-3）。

    停止宽限期的算法把"每次数据库操作 ≤ 11 秒"当成前提。那个前提**只有在 DSN 真的带
    了 statement_timeout 与 lock_timeout 时才成立**——只设 connect_timeout 时，一条卡住
    的语句可以无限期挂着，宽限期再长也拦不住 SIGKILL 落在"已换新凭据、未写回数据库"
    的窗口里。模板里写什么，部署时多半就照抄什么，所以这条断言守的是模板。
    """

    text = read(ENV_EXAMPLE)
    match = re.search(r"^LINGXI_POSTGRES_DSN=(\S+)\s*$", text, re.MULTILINE)
    if match is None:
        return ["deploy/.env.example 里没有 LINGXI_POSTGRES_DSN 示范值"]

    dsn = match.group(1)
    failures = []
    for setting, expected in sorted(REQUIRED_DSN_SETTINGS.items()):
        # DSN 里 options 的空格与等号是 URL 编码的（%20 / %3D），所以两种写法都认。
        pattern = rf"{re.escape(setting)}(=|%3D){re.escape(expected)}\b"
        if not re.search(pattern, dsn, re.IGNORECASE):
            failures.append(
                f"deploy/.env.example 的 LINGXI_POSTGRES_DSN 没有 `{setting}={expected}`。"
                f"\n      停止宽限期按「每次数据库操作 ≤ "
                f"{DATABASE_OPERATION_SECONDS:.0f} 秒」建模，而这个上界正是由这三个参数给出的。"
                "\n      只设 connect_timeout 拦不住已经发出去的语句——那是一个假的上界。"
            )
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
    worker = service_block(base, "worker") or ""
    migrate = service_block(base, "migrate") or ""

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

    # M2-62-29 / M2-62-30：非 root、能力最小。
    for name, block in (("scheduler", scheduler), ("worker", worker), ("migrate", migrate)):
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
        for service in ("scheduler", "worker", "migrate"):
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
                required = math.ceil(_worst_case_seconds() * SAFETY_FACTOR)
                if seconds is None or seconds < required:
                    failures.append(
                        f"{display(path)} 的 {service} 用覆盖把 "
                        f"stop_grace_period 改成了 {grace.group(1)}，低于要求的 {required} 秒"
                    )
            restart = re.search(r"^\s*restart:\s*(\S+)\s*$", block, re.MULTILINE)
            if restart is not None and service in {"worker", "migrate"}:
                if restart.group(1).strip("'\"") != "no":
                    failures.append(
                        f"{display(path)} 的 {service} 用覆盖把 restart "
                        f"改成了 {restart.group(1)}；一次性作业必须是 \"no\""
                    )

    # ---- 凭据按服务分文件（codex 审查 P1-1）---------------------------------
    # 三个服务共用一个 env_file 时，worker 会拿到数据库连接串、Fernet 密钥与飞书密钥。
    # 这不是"多给几个变量"：worker 跑 Agent SDK，SDK 把进程环境继承给 Claude CLI 与
    # 每个 MCP 子进程，于是那些凭据一路流进模型执行环境——正是产品合同「凭据不进用户
    # 环境」要挡的方向。
    for path in (COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        seen: dict[str, list[str]] = {}
        for service in ("scheduler", "worker", "migrate"):
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
    text = read(CI_WORKFLOW)

    # M2-62-41 / D16：`extra: [...]` 只能有一行。
    #
    # check_installed_package.py 的 `_MATRIX_LINE` 用的是 `re.search`，只取**第一个**
    # 匹配。新增一条构建矩阵若也用 `extra:` 作键，三方对账就会拿错集合去比对，
    # 既有门禁从此失效而且全绿——正是「绿色的测试不等于会变红的测试」。
    extra_lines = re.findall(r"^[ \t]*extra:[ \t]*\[", text, re.MULTILINE)
    if len(extra_lines) != 1:
        failures.append(
            f"ci.yml 里有 {len(extra_lines)} 行 `extra: [...]`，必须恰好 1 行。"
            "check_installed_package.py 的三方对账用 re.search 取首个匹配，"
            "多一行就会让它拿错集合去对账，而且不会报错。"
            "新增的构建矩阵请用别的键名（本仓库用 `service:`）。"
        )

    # M2-62-39：新增顶层路径必须进 push 的 paths 触发列表（#53 同型教训）。
    for needed in ("Dockerfile", "deploy/**", ".dockerignore"):
        if f"'{needed}'" not in text and f'"{needed}"' not in text:
            failures.append(
                f"ci.yml 的 push `paths:` 没有 `{needed}`：改坏它直推 main 不会触发完整门禁。"
            )

    # codex 二轮 P1-1：`packages: write` 只能出现在**不随 PR 跑**的 job 上。
    #
    # `permissions` 是按 job 生效的，而同仓分支的 pull_request 事件会让该 job 拿到所
    # 声明的令牌。把写权限放在一个 PR 也会跑的 job 上，等于任何人改一行 workflow 就能
    # 在 PR 阶段推 / 覆盖 GHCR，"合并后才发布"这道门整个绕过去。
    # 判据：含 `packages: write` 的 job 必须同时有 `if:` 限定 push + refs/heads/main。
    for match in re.finditer(r"^  (\w+):\n(.*?)(?=^  \w+:\n|\Z)", text, re.MULTILINE | re.DOTALL):
        job_name, body = match.group(1), match.group(2)
        # 只看 job 头部（steps 之前），避免把步骤里的字符串误当成 job 级声明。
        header = body.split("\n    steps:", 1)[0]
        if not re.search(r"^\s*packages:\s*write\s*$", header, re.MULTILINE):
            continue
        gated = re.search(r"^\s*if:.*github\.event_name\s*==\s*'push'", header, re.MULTILINE) and \
            re.search(r"^\s*if:.*refs/heads/main", header, re.MULTILINE)
        if not gated:
            failures.append(
                f"ci.yml 的 job `{job_name}` 声明了 `packages: write`，但没有 "
                "`if: github.event_name == 'push' && github.ref == 'refs/heads/main'` 限定。\n"
                "      同仓分支的 pull_request 会让这个 job 照样拿到写令牌——"
                "改一行 workflow 就能在 PR 阶段推 / 覆盖 GHCR，绕过合并门禁。"
            )
        # 发布 job 必须 needs 着 image。镜像契约、两次构建等价、V-部署-05 全都跑在
        # image 里；把它从 needs 里拿掉，这些检查一条都不会拦住发布，而 CI 依然全绿。
        needs = re.search(r"^\s*needs:\s*(\[[^\]]*\]|\S.*)$", header, re.MULTILINE)
        if needs is None or "image" not in needs.group(1):
            failures.append(
                f"ci.yml 的 job `{job_name}` 声明了 `packages: write`，但它的 `needs` "
                f"不含 `image`（当前：{needs.group(1) if needs else '无 needs'}）。\n"
                "      镜像契约、两次构建等价与 V-部署-05 都跑在 image job 里；"
                "不 needs 它就等于这些检查一条都不拦发布，而 CI 照样全绿。"
            )

    # M2-62-35 / D15：推送步骤不得吞错。
    if "push_image.py" in text:
        for line in text.splitlines():
            if "continue-on-error" in line and not line.lstrip().startswith("#"):
                failures.append(
                    "ci.yml 用了 continue-on-error。GHCR 推送的降级必须是**显式分支**："
                    "权限不足时上传 artifact 并在 summary 写明「未推送」，"
                    "而不是让任何失败都变成绿色。两条路径同样绿色收口 = 无法区分"
                    "「推上去了」与「没推上去」。"
                )
    return failures


def main() -> int:
    checks = (
        ("停止宽限期与源码常量联动", check_stop_grace_period),
        ("数据库超时与停机上界依据", check_database_timeouts),
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
    backoff = module_constant(SCHEDULER_APP, "SAVE_RETRY_BACKOFF_SECONDS")
    worst_case = _worst_case_seconds()
    print(
        "部署编排契约：通过（Dockerfile、deploy/ 下 3 个 compose、.dockerignore、ci.yml）\n"
        f"  停止宽限期：最坏 {worst_case:.1f}s（续期 HTTP {http_timeout}s + 落盘退避 "
        f"{sum(backoff)}s + 数据库 {DATABASE_ROUNDTRIP_BUDGET_SECONDS:.0f}s ="
        f" {DATABASE_OPERATION_COUNT} 次操作 × {DATABASE_OPERATION_SECONDS:.0f}s）"
        f" × {SAFETY_FACTOR} → 要求 ≥ {math.ceil(worst_case * SAFETY_FACTOR)}s\n"
        f"  示例 DSN 已带 {'、'.join(sorted(REQUIRED_DSN_SETTINGS))}，上界因此有依据"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
