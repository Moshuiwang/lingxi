#!/usr/bin/env python3
"""归属核对门禁（Issue #238）：凡标注「产品合同明令」「合同要求」的断言必须能对上。

代码框架第三节曾写「凭据：不进代码、日志、数据库、用户环境（产品合同明令）」——
`docs/产品合同与外部边界.md` 正文从未提过"用户环境"这个范围，那其实是架构设计
自己的从紧要求被错记成合同条款。散文约束挡不住这类错误：只有把每一处"标注为
合同"的断言变成一条会核对、会变红的登记，才挡得住下一次同样的笔误。

**为什么不能只靠正则找到"合同要求"就直接核对文字**：本仓库绝大多数归属断言是
**转述**而不是逐字引用（"银行家式复述" vs "逐字复制"），例如"合同要求两者不
一致时不得视为发布完成"转述的是合同原文"数据库记录与飞书多维表格发布结果不
一致…都不能视为 Lingxi 侧发布已经完成"。逐字子串匹配会把几乎所有转述都误判为
查无对应；而放宽成"语义相近就算"又没法用程序判定。因此本脚本采用**登记制**：

1. 本文件内置一份 ``GROUNDED_ATTRIBUTIONS`` 登记表，逐条记录"哪个文件的哪句话
   （用一段能在原文里找到的摘录定位）对应产品合同的哪一节"——这是一次性的人工
   核对结果，2026-08-19 逐条核对产品合同正文写成，不是程序自动推导的。
2. 门禁做三件**机械**的事：(a) 登记表引用的合同章节必须真实存在于
   `docs/产品合同与外部边界.md`；(b) 登记表的摘录必须真的能在它标注的源文件里
   找到（防止摘录过期还挂在表里）；(c) 仓库里每一处标注"产品合同明令"或"合同
   要求"的行，都必须至少被登记表里的一条摘录覆盖，找不到覆盖就是**新出现的、
   未经核对的归属断言**，直接判红。
3. 少数几处归属经核对后仍有疑问（措辞源自具体 Issue 的产品负责人决定，而非
   `产品合同与外部边界.md` 正文本身；见 ``REGISTERED_EXCEPTIONS``），按
   AGENTS.md「宁可让门禁带一个明确登记的例外，也不要偷偷改合同」处理：不静默
   放行，登记为例外并在门禁输出里可见地报出来，留给编排者与产品负责人裁定。

任何人往仓库里新加一句"产品合同明令 XXX"而不登记，门禁直接红；任何人把合同正文
的章节改名或删除导致登记表的引用失效，门禁也直接红——这两条挡的正是"归属只在
写下的那一刻被人读一遍，此后再没有人核对过"的腐烂路径。

扫描失败必须失败关闭：合同文档或任何一个被扫描文件读不出来，都直接判红。
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DOCUMENT = REPOSITORY_ROOT / "docs" / "产品合同与外部边界.md"

# 归属触发词：本仓库里明确"把这句话的权威记成产品合同"的两种写法。
# 只匹配这两个具体短语，不匹配裸的"合同"——本仓库大量使用"合同"表示模块自身的
# 接口/服务合同（软件工程含义，如"OnboardingRunner.start 的服务合同"），
# 那不是在对产品合同文档做归属声明，不属于本门禁的核对范围。
TRIGGER_PATTERN = re.compile("合同要求|合同明令")

# 只扫这三类正式文本文件——与仓库其余 check_*.py 扫描范围一致。
SCAN_SUFFIXES = (".py", ".md", ".sh")
# 合同文档本身是权威源，不对自己做归属核对；tests/ 下的假实现头注释与业务代码
# 混在同一批 tracked 文件里，同样需要核对，不豁免。
# 本脚本自身与它的单元测试是唯一例外：它们的源码里大量出现"合同要求"/"合同明令"
# 字面串——一处是在讨论触发词本身（TRIGGER_PATTERN 的定义、登记表数据、模块
# docstring 里的举例），另一处是单元测试构造的字符串字面量夹具——这些都不是在对
# 产品合同文档做归属声明，扫描自己会把整份登记表和触发词定义当成待核对的断言，
# 那是检查工具在核对自己的实现细节，不是在核对产品事实。
EXCLUDED_PATHS = {
    CONTRACT_DOCUMENT,
    Path(__file__).resolve(),
    REPOSITORY_ROOT / "tests" / "test_contract_attribution_check.py",
}

HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


class AttributionCheckError(ValueError):
    """扫描或解析失败——必须失败关闭，不能当作「没有归属断言」悄悄通过。"""


@dataclass(frozen=True)
class GroundedAttribution:
    """一条已核对的归属：``file`` 里包含 ``excerpt`` 的那句话，对应合同 ``section`` 一节。"""

    file: str
    excerpt: str
    section: str


@dataclass(frozen=True)
class RegisteredException:
    """一条已登记但未核对通过的归属：不静默放行，可见地报出来。"""

    file: str
    excerpt: str
    reason: str


# ---------------------------------------------------------------------------
# 登记表：2026-08-19 对全仓库 41 处「合同要求 / 合同明令」逐条核对产品合同正文
# （docs/产品合同与外部边界.md）写成，见 PR（#238）描述里的逐条对账结果。
# 新增一条归属声明时，先在这里核对它对应合同哪一节、把摘录和章节名登记进来，
# 而不是先写代码再让门禁牵着走——门禁的作用是挡住"忘了核对"，不是代替核对本身。
# ---------------------------------------------------------------------------

GROUNDED_ATTRIBUTIONS: tuple[GroundedAttribution, ...] = (
    GroundedAttribution(
        "docs/参考证据/银河用户权限数据结构.md",
        "合同要求的「公司范围」与「职能范围」是两条互相独立的授权链",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "写操作的审计记录无法可靠保存时，用户状态不得改变",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "会话映射（合同要求逐条落地）",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "路径、归属和访问控制与飞书私聊完全一致",
        "高级工作台",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "合同要求的审计事实由 SDK 回调与 Lingxi 自己的任务编排层共同产生",
        "审核、审计与持续优化方向",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "高级工作台中产生的正式产物通过统一的交付 Skill 完成交付",
        "高级工作台",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "在同一私聊或同一话题用普通文本补发完整结果（合同要求）",
        "交付样式",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "合同要求状态区展示",
        "交付样式",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "不在审计中保存凭据、完整令牌或无关个人信息",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "MCP 令牌明文一律不入库（合同明令）",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "建立统一的 `user` 用户表",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "目标状态已经变化时一律不执行",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "调岗时只收回明确失效的范围",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "而合同要求两者不一致时不得视为发布完成",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "字段对应合同要求的审计内容",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "公司内部审计可以在九十天内审计完整聊天记录",
        "数据保留与删除",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "合同要求\"不执行、不保存、不回显\"",
        "开通成功后",
    ),
    GroundedAttribution(
        "docs/技术设计/代码框架.md",
        "日志、数据库不存凭据明文是产品合同明令",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        "处理次序本身是合同要求，不能重排",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        "审计与状态变更同事务\"——这是合同要求",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "src/lingxi/apps/worker/report.py",
        "产品合同明令禁止的\"伪装成功\"",
        "交付规则",
    ),
    GroundedAttribution(
        "src/lingxi/apps/scheduler/permission_refresh.py",
        "合同要求每日刷新",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "src/lingxi/apps/scheduler/permission_refresh.py",
        "建档合同要求人员 ID 必填",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/apps/gateway/log_redaction.py",
        "凭据不得进日志是产品合同明令",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "src/lingxi/core/execution/tool_policy.py",
        "用户可见文案里不出现内部标识是产品合同要求",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/execution/audit.py",
        "产品合同要求「不在审计中保存凭据、完整令牌」",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "src/lingxi/core/permission/mcp_readiness.py",
        "产品合同要求「明确确认该用户应有的公司和职能权限已经同步且可以问数后，才宣告开通成功」",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/permission/mcp_readiness.py",
        "合同要求的最后一次探针",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/conversation/pipeline.py",
        "次序本身是合同要求，不能重排",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/credentials.py",
        "日志、数据库不存凭据明文是产品合同明令",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/provisioning.py",
        "的合同要求按 `event_id` / `open_id` 幂等",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "migrations/alembic/versions/0064_permission_publish_outbox.py",
        "而产品合同要求两者不一致时**不得视为发布完成**",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "migrations/alembic/versions/0065_mcp_token_and_sync_check.py",
        "是合同要求（与 ``publish_outbox.last_outcome``",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "tests/gateway_fakes.py",
        "处理次序是合同要求",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "tests/test_permission_publish_postgres.py",
        "合同要求的\"发布读回一致后立即探一次\"",
        "开通流程",
    ),
    GroundedAttribution(
        "tests/test_mcp_readiness_machine.py",
        "合同要求的最后一次探针永远发不出去",
        "开通流程",
    ),
)

# ---------------------------------------------------------------------------
# 已知例外：核对时发现措辞源自具体 Issue 的产品负责人决定（有留痕），但
# `产品合同与外部边界.md` 正文本身没有对应文字。不静默放行、不擅自改写归属，
# 登记原因并在门禁输出里保持可见，交给编排者判断是否需要回写合同或改措辞。
# ---------------------------------------------------------------------------

REGISTERED_EXCEPTIONS: tuple[RegisteredException, ...] = (
    RegisteredException(
        "src/lingxi/apps/scheduler/__init__.py",
        "合同要求\"告警不可用时主流程行为",
        "「告警不可用时主流程行为需要明确定义」出自 Issue #153 的产品负责人决定，"
        "产品合同与外部边界正文没有关于告警/监控行为的条款；与 gateway/__init__.py "
        "同一处措辞。2026-08-19 归属核对登记，未改写，留待编排者判断是否需要回写合同。",
    ),
    RegisteredException(
        "src/lingxi/apps/gateway/__init__.py",
        "合同要求\"告警不可用时主流程行为有",
        "同上（scheduler/__init__.py 的登记）：出自 Issue #153，合同正文未提及告警行为。",
    ),
    RegisteredException(
        "src/lingxi/core/permission/publish_row.py",
        "而合同要求这里放",
        "「发布表值列表放指标名」出自 Issue #155 产品负责人对三问的答复（留痕见该 "
        "Issue 评论），是与问数 MCP 消费方的既定数据格式约定，产品合同与外部边界 "
        "正文没有规定发布表的具体字段格式。2026-08-19 归属核对登记，未改写。",
    ),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = []
    for raw_path in result.stdout.split("\0"):
        if not raw_path:
            continue
        if not raw_path.endswith(SCAN_SUFFIXES):
            continue
        path = Path(raw_path)
        if path.parts and path.parts[0] == ".tmp":
            continue
        full_path = REPOSITORY_ROOT / path
        if full_path in EXCLUDED_PATHS:
            continue
        if full_path.is_file():
            paths.append(full_path)
    return paths


def contract_sections(text: str) -> set[str]:
    sections: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if match:
            sections.add(match.group(2))
    return sections


def _display_path(path: Path) -> str:
    """相对仓库根显示；扫描根被指到仓库之外时（单元测试用临时文件会这么做），
    退化成绝对路径即可——出处只是给人看的诊断信息，不参与判定。
    """

    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path)


def find_triggered_lines(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise AttributionCheckError(f"无法读取 {_display_path(path)}：{error}") from error

    hits = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if TRIGGER_PATTERN.search(line):
            hits.append((line_number, line.strip()))
    return hits


def evaluate() -> tuple[list[str], str]:
    """返回 (失败原因列表, 汇总信息)。"""

    try:
        contract_text = CONTRACT_DOCUMENT.read_text(encoding="utf-8")
    except OSError as error:
        raise AttributionCheckError(f"无法读取产品合同文档 {CONTRACT_DOCUMENT}：{error}") from error

    sections = contract_sections(contract_text)
    if not sections:
        raise AttributionCheckError("产品合同文档里一个二/三级标题都没解析到，无法核对归属")

    failures: list[str] = []

    # (a) 登记表引用的章节必须真实存在。
    for grounded in GROUNDED_ATTRIBUTIONS:
        if grounded.section not in sections:
            failures.append(
                f"登记表里 {grounded.file} 的归属指向章节「{grounded.section}」，"
                "但产品合同文档里找不到这个标题（改名了，还是删除了？）"
            )

    # (b) 登记表的摘录必须真的能在它标注的源文件里找到。
    file_texts: dict[str, str] = {}

    def read_registered_file(relative: str) -> str | None:
        if relative in file_texts:
            return file_texts[relative]
        full_path = REPOSITORY_ROOT / relative
        try:
            text = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            failures.append(f"登记表引用的文件读不出来：{relative}（{error}）")
            file_texts[relative] = ""
            return None
        file_texts[relative] = text
        return text

    for grounded in GROUNDED_ATTRIBUTIONS:
        text = read_registered_file(grounded.file)
        if text is not None and grounded.excerpt not in text:
            failures.append(
                f"登记表摘录已经在源文件里找不到了：{grounded.file} 摘录 {grounded.excerpt!r}"
                "——原句被改动或删除时，请同步更新登记表（scripts/ci/check_contract_attribution.py）"
            )

    for exception in REGISTERED_EXCEPTIONS:
        read_registered_file(exception.file)
        text = file_texts.get(exception.file, "")
        if exception.excerpt not in text:
            failures.append(
                f"例外登记的摘录已经在源文件里找不到了：{exception.file} 摘录 {exception.excerpt!r}"
            )

    # (c) 仓库里每一处「合同要求/合同明令」都必须被登记表或例外表覆盖。
    covered_by_file: dict[str, list[str]] = {}
    for grounded in GROUNDED_ATTRIBUTIONS:
        covered_by_file.setdefault(grounded.file, []).append(grounded.excerpt)
    for exception in REGISTERED_EXCEPTIONS:
        covered_by_file.setdefault(exception.file, []).append(exception.excerpt)

    triggered_total = 0
    for path in tracked_files():
        relative = _display_path(path)
        for line_number, line in find_triggered_lines(path):
            triggered_total += 1
            excerpts = covered_by_file.get(relative, ())
            if not any(excerpt in line for excerpt in excerpts):
                failures.append(
                    f"{relative}:{line_number}：出现「合同要求」或「合同明令」但未登记"
                    f"——{line!r}。请先核对它是否真的对应产品合同正文，再登记进 "
                    "scripts/ci/check_contract_attribution.py 的 GROUNDED_ATTRIBUTIONS"
                    "（对上了）或 REGISTERED_EXCEPTIONS（对不上、且不能擅自改写归属时）。"
                )

    exception_notes = [
        f"- {exception.file}：{exception.excerpt!r} —— {exception.reason}"
        for exception in REGISTERED_EXCEPTIONS
    ]
    summary = (
        f"归属核对：扫描到 {triggered_total} 处「合同要求/合同明令」，"
        f"{len(GROUNDED_ATTRIBUTIONS)} 条登记为已核对对应合同正文，"
        f"{len(REGISTERED_EXCEPTIONS)} 条登记为已知例外（未改写归属，待裁定）"
    )
    if exception_notes:
        summary += "\n已知例外：\n" + "\n".join(exception_notes)

    return failures, summary


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        failures, summary = evaluate()
    except AttributionCheckError as error:
        print(f"归属核对检查失败：{error}", file=sys.stderr)
        return 1

    if failures:
        print("归属核对检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
