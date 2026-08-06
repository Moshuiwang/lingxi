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
# 为什么是个常数而不是从代码里读出来的：`psycopg.connect()` 在
# adapters/delegated_credentials.py 里没有超时参数，libpq 的 connect_timeout 默认是
# **无限等待**，真正的上界来自部署时 DSN 里的 `connect_timeout`——那个值不在仓库里，
# 本检查看不见它。deploy/.env.example 因此把 `connect_timeout=5` 列为必填，
# 这里按最坏情况 5 次往返（4 次 save 重试 + 1 次 revoke）× 5 秒 = 25 秒计。
DATABASE_ROUNDTRIP_BUDGET_SECONDS = 25.0

# 安全系数。宽限期不是"刚好够"就行：`_FileLock` 用的是阻塞式 flock，DSN 的
# connect_timeout 又是本检查看不见的部署侧配置，两者都可能把实际耗时推高。
# 乘 1.5 是给这两项不可见量留的余量，也让"把超时改大却不动 compose"必然变红。
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

    worst_case = float(http_timeout) + float(sum(backoff)) + DATABASE_ROUNDTRIP_BUDGET_SECONDS
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


def check_compose_contract() -> list[str]:
    failures: list[str] = []
    base = strip_comments(read(COMPOSE_BASE))

    # M2-62-14：生产只拉镜像，不构建。三个 compose 文件里一个 `build:` 键都不许有。
    for path in (COMPOSE_BASE, COMPOSE_STAGE, COMPOSE_PROD):
        text = strip_comments(read(path))
        if re.search(r"^\s*build:\s*$|^\s*build:\s+\S", text, re.MULTILINE):
            failures.append(
                f"{path.relative_to(REPOSITORY_ROOT)} 含 `build:` 键。生产只拉镜像、不构建"
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
                continue
            failures.append(
                f"{path.relative_to(REPOSITORY_ROOT)}:{line_number} 的镜像引用 "
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
    required = {
        ".git": "版本控制目录会把整个历史带进构建上下文",
        ".claude": "代理工作现场不属于制品",
        ".venv": "本机虚拟环境内嵌宿主机绝对路径",
        "__pycache__": "宿主机编译的 .pyc 内嵌构建目录绝对路径，同时破坏可复现构建与路径不泄漏",
        ".env": "凭据文件绝不进构建上下文",
    }
    failures = []
    for entry, reason in required.items():
        if not any(item == entry or item.startswith(entry) for item in entries):
            failures.append(f".dockerignore 没有排除 `{entry}`：{reason}")
    if not any(item.startswith("*.py") and "c" in item for item in entries):
        failures.append(".dockerignore 没有排除 `*.py[cod]` 一类的字节码文件")
    if not any(item.startswith(".env") for item in entries):
        failures.append(".dockerignore 没有排除 `.env*`")
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
    worst_case = float(http_timeout) + float(sum(backoff)) + DATABASE_ROUNDTRIP_BUDGET_SECONDS
    print(
        "部署编排契约：通过（Dockerfile、deploy/ 下 3 个 compose、.dockerignore、ci.yml）\n"
        f"  停止宽限期：最坏 {worst_case:.1f}s"
        f"（HTTP {http_timeout}s + 退避 {sum(backoff)}s + 数据库 {DATABASE_ROUNDTRIP_BUDGET_SECONDS}s）"
        f" × {SAFETY_FACTOR} → 要求 ≥ {math.ceil(worst_case * SAFETY_FACTOR)}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
