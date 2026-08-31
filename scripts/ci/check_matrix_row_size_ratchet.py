#!/usr/bin/env python3
"""验收矩阵单行体量棘轮门禁（Issue #335，方案 B，产品负责人 2026-08-28 批准 Gate G-4 产物）。

`docs/技术设计/验收矩阵.md` 真正的膨胀源不是断言条数变多，而是**单条断言的表格
cell 被写成大段裁定沿革**（2026-08-28 实测：Top 1 行单独占全文 4.9%）——这与仓库
已有的「决策记录只答 why、技术设计不复制正文」规则本身冲突，也是
``check_docs_size_budget.py`` 此前处理代码框架/验证与门禁膨胀时点名的同一种病。

复用 ``check_size_ratchet.py`` 的棘轮骨架（基线登记 / 只许缩不许涨 / --refresh 只
调小或移除 / 未登记不得新超阈值），维度从「文件行数」换成「``V-*`` 断言表格行的
UTF-8 字节数」，以断言编号为 key：

1. 已经超过 800B 的行登记进 ``matrix_row_size_baseline.txt``，只许变小、不许变大；
2. 未超过 800B 的行不得新超过 800B；
3. 超限报错并提示：把裁定沿革/形成经过移到决策记录或参考证据，cell 内只留判定
   要点 + 链接。

**同一断言编号多行出现的处理规则**：按行独立计——``measure_rows`` 对每一行分别
量出字节数，同一编号下取所有行里的最大值代表该编号，与基线的同一个上限比较。
效果等价于「每一行都独立地拿同一个 recorded 上限去比」：任意一行超标就会被抓到，
不会被同编号下的另一条短行掩盖。正常情况下不会遇到这种输入——
``check_acceptance_matrix.py`` 已经在合法文档里禁止同一断言编号重复出现（矩阵表
内 ``seen_at`` 去重 + 非法表格里出现 ``V-`` 开头行直接判错）；这里的取最大值只是
防御性兜底，不是本检查期望遇到的常态。

表格解析口径（``is_table_row``/``split_row``/``iter_matrix_rows`` 里跳过围栏、
识别分隔行、判定表头）刻意与 ``check_acceptance_matrix.py`` 的
``is_table_row``/``split_row``/``iter_tables`` 逐字节保持一致：同一份 Markdown、
同一套表格必须被两个检查同时认可为「断言行」，否则会出现「这边判定是断言行、
那边不认」的静默分歧。两份脚本刻意不互相 import（仓库里没有 CI 脚本互相 import
内部实现的先例，脚本各自独立可运行、失败原因不牵连），靠这份注释和两边测试
分别对真实文档跑通来保证口径同步。

**读取范围（Issue #479 分册后）**：矩阵从「一个文件」变成「总册
``验收矩阵.md`` + 同目录全部 ``验收矩阵-*.md`` 分册」。单行 800B 棘轮的判定标准
一个字没改，只是丈量范围扩成整个集合：以断言编号为 key，取该编号在**所有**分册
里出现过的最大字节数与基线比较，所以断言搬去哪一册都不改变判定结果。分册靠目录
扫描发现而不是写死清单——写死清单意味着新增一册忘了登记就静默脱离棘轮。

**总量触发线（非阻断）**：400KB / 1500 行双指标（先触线者生效），触线打印醒目
提示、**exit 0 不卡红**——按 Gate G-4 产物的分册预案，触线只是「下一次改动该文件
前必须先完成分册」的信号，不是立即失败；不能让一次总量提示误伤当次无关 PR。
**分册后计量单位从「整份矩阵一个文件」改成「每个文件各自独立」，两个数值不变**
（口径更新经 Issue #479 批准，留痕在总册「体量预算」小节）：一册触线就提示把那
一册再拆一层。同时打印全部分册的合计字节/行数，让「每册都不触线、加起来仍在膨胀」
这种情况有人能看见。分册规则本身写在 ``docs/技术设计/验收矩阵.md`` 文件头
「体量预算」小节，这里只负责量出数字。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MATRIX_DOCUMENT = REPOSITORY_ROOT / "docs" / "技术设计" / "验收矩阵.md"
# 分册命名空间：与总册同目录、同前缀（与 check_acceptance_matrix.py 的
# MATRIX_VOLUME_GLOB 逐字保持一致，同一份集合必须被两个检查同时看见）。
MATRIX_VOLUME_GLOB = "验收矩阵-*.md"
BASELINE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "matrix_row_size_baseline.txt"

THRESHOLD_BYTES = 800
TOTAL_BYTES_TRIGGER = 400 * 1024
TOTAL_LINES_TRIGGER = 1500

# 与 check_acceptance_matrix.py 的 MATRIX_HEADER/ASSERTION_ID/SEPARATOR_ROW/
# UNESCAPED_PIPE/ESCAPED_PIPE 逐字保持一致（见模块文档字符串）。
MATRIX_HEADER = ("#", "可验证断言", "层级", "状态")
ASSERTION_ID = re.compile(r"^V-[一-鿿]+-\d{2}$")
SEPARATOR_ROW = re.compile(r"^\|[\s:|-]+\|$")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
ESCAPED_PIPE = re.compile(r"\\\|")

BASELINE_HEADER = (
    f"# 验收矩阵单行体量棘轮基线（Issue #335）：登记当前已超过 {THRESHOLD_BYTES}B 的",
    "# V-* 断言表格行（UTF-8 字节，含整行 Markdown 语法）与其字节上限。",
    "# 由 scripts/ci/check_matrix_row_size_ratchet.py --refresh 生成，请不要手工调大",
    "# 数值——门禁会重新丈量该编号对应行的实际字节数，任何比这里记录的更大的实测值",
    "# 都直接判红；--refresh 只会把数值调小或整条移除（该编号所有行都缩到阈值以下），",
    "# 拒绝写入任何增长。",
    "# 一个从未超过阈值的编号第一次超过阈值时，不会被 --refresh 自动登记进来：先把",
    "# 裁定沿革/形成经过移到决策记录或参考证据，cell 内只留判定要点+链接；确有理由",
    "# 要接受它作为新的棘轮登记对象，人工在下面加一行「字节数<TAB>断言编号」，门禁",
    "# 会核对这一行是否等于该编号当前实测的最大字节数。",
)


class BaselineError(ValueError):
    """基线文件或矩阵文档读取/格式错误——必须失败关闭，不能当作空基线继续跑。"""


def is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1


def split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [ESCAPED_PIPE.sub("|", cell.strip()) for cell in UNESCAPED_PIPE.split(inner)]


def iter_matrix_rows(text: str):
    """产出 (行号, 断言编号, 原始行文本)，只覆盖表头为 MATRIX_HEADER 的表格行。

    自动跳过围栏代码块（文档里的模板/示例矩阵可能被围栏包住，那是文档示例不是
    登记表）；识别表头的方式与 check_acceptance_matrix.py 一致——"下一行是分隔行
    的表格行"才是表头，其余表格行按当前表头归属。
    """
    header: tuple[str, ...] | None = None
    in_fence = False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            header = None
            continue
        if in_fence:
            continue
        if not is_table_row(line):
            header = None
            continue
        if SEPARATOR_ROW.match(line.strip()):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if header is None and SEPARATOR_ROW.match(following.strip()):
            header = tuple(split_row(line))
            continue
        if header != MATRIX_HEADER:
            continue
        cells = split_row(line)
        if not cells or not ASSERTION_ID.match(cells[0]):
            continue
        yield index + 1, cells[0], line


def measure_rows(text: str) -> dict[str, int]:
    """断言编号 → 该编号所有行里的最大 UTF-8 字节数（见模块文档字符串的重复行策略）。"""

    counts: dict[str, int] = {}
    for _line_number, identifier, line in iter_matrix_rows(text):
        size = len(line.encode("utf-8"))
        counts[identifier] = max(counts.get(identifier, 0), size)
    return counts


def read_matrix_text() -> str:
    try:
        return MATRIX_DOCUMENT.read_text(encoding="utf-8")
    except OSError as error:
        raise BaselineError(f"无法读取验收矩阵文档 {MATRIX_DOCUMENT}：{error}") from error


def matrix_volumes() -> list[Path]:
    """总册同目录下的全部分册，按文件名排序。"""
    return sorted(MATRIX_DOCUMENT.parent.glob(MATRIX_VOLUME_GLOB))


def read_matrix_documents() -> dict[str, str]:
    """「显示名 → 正文」：总册在前，分册按文件名排序跟在后面。"""
    documents = {MATRIX_DOCUMENT.name: read_matrix_text()}
    for volume in matrix_volumes():
        try:
            documents[volume.name] = volume.read_text(encoding="utf-8")
        except OSError as error:
            raise BaselineError(f"无法读取验收矩阵分册 {volume}：{error}") from error
    return documents


def measure_documents(documents: dict[str, str]) -> dict[str, int]:
    """断言编号 → 该编号在整个集合里出现过的最大 UTF-8 字节数。

    合并规则与单文档内的重复行策略同一条（取最大值）：搬到哪一册都不改变判定，
    任意一册里的任意一行超标都会被抓到。
    """
    counts: dict[str, int] = {}
    for text in documents.values():
        for identifier, size in measure_rows(text).items():
            counts[identifier] = max(counts.get(identifier, 0), size)
    return counts


def parse_baseline(text: str) -> dict[str, int]:
    """解析「字节数<TAB>断言编号」登记表；任何一行格式不对都直接抛错。"""

    entries: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].isdigit():
            raise BaselineError(
                f"基线文件第 {line_number} 行格式不合法（应为「字节数<TAB>断言编号」）：{line!r}"
            )
        size_text, identifier_text = parts
        if not ASSERTION_ID.match(identifier_text):
            raise BaselineError(
                f"基线文件第 {line_number} 行的断言编号不合法：{identifier_text!r}"
            )
        if identifier_text in entries:
            raise BaselineError(f"基线文件第 {line_number} 行重复登记同一断言编号：{identifier_text}")
        entries[identifier_text] = int(size_text)
    return entries


def render_baseline(entries: dict[str, int]) -> str:
    lines = list(BASELINE_HEADER)
    lines.append("")
    for identifier in sorted(entries):
        lines.append(f"{entries[identifier]}\t{identifier}")
    return "\n".join(lines) + "\n"


def load_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise BaselineError(f"基线文件不存在：{path}（--refresh 只能刷新已有登记，不能从零生成）")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BaselineError(f"无法读取基线文件 {path}：{error}") from error
    return parse_baseline(text)


def evaluate(baseline: dict[str, int], current: dict[str, int]) -> list[str]:
    """核对棘轮的两条规则，返回失败原因列表；空列表表示通过。"""

    failures: list[str] = []

    for identifier, recorded in sorted(baseline.items()):
        actual = current.get(identifier)
        if actual is None:
            # 该编号已经不在矩阵里（删除、改名或整条断言下线）：棘轮的目的已经
            # 达成，不需要门禁介入；基线里的陈旧登记留给 --refresh 自愿清理。
            continue
        if actual > recorded:
            failures.append(
                f"{identifier}：当前 {actual}B，超过棘轮基线记录的上限 {recorded}B。"
                "规则是「已登记进基线的行只许变小、不许变大」——"
                "请把裁定沿革/形成经过移到决策记录或参考证据，cell 内只留判定要点+链接。"
            )
        elif actual < recorded:
            failures.append(
                f"{identifier}：棘轮基线记录 {recorded}B，与实测 {actual}B 不一致。"
                "基线必须与实际字节数精确相等，不允许留有余量。运行 "
                "python3 scripts/ci/check_matrix_row_size_ratchet.py --refresh 校准。"
            )

    for identifier, actual in sorted(current.items()):
        if actual > THRESHOLD_BYTES and identifier not in baseline:
            failures.append(
                f"{identifier}：{actual}B，新超过单行体量棘轮阈值（{THRESHOLD_BYTES}B）且未登记在基线里。"
                "请把裁定沿革/形成经过移到决策记录或参考证据，cell 内只留判定要点+链接；"
                "如果确有理由要接受它作为新的棘轮登记对象，在 "
                f"{BASELINE_PATH.relative_to(REPOSITORY_ROOT)} 里人工加一行"
                f"「{actual}\\t{identifier}」并在 PR 里说明理由（--refresh 不会自动添加新登记）。"
            )

    return failures


def render_total_size_notice(text: str) -> str:
    """总量触发线：只提示、不卡红。触线返回醒目多行横幅，未触线返回单行状态。"""

    total_bytes = len(text.encode("utf-8"))
    total_lines = len(text.splitlines())
    triggered = total_bytes > TOTAL_BYTES_TRIGGER or total_lines > TOTAL_LINES_TRIGGER
    if not triggered:
        return (
            f"验收矩阵总量：{total_bytes}B/{TOTAL_BYTES_TRIGGER}B、"
            f"{total_lines} 行/{TOTAL_LINES_TRIGGER} 行，未触及分册触发线（非阻断提示，不影响退出码）"
        )
    banner = "=" * 72
    return "\n".join(
        [
            banner,
            "【提示】验收矩阵某一册总量已触及分册触发线（400KB 或 1500 行，先触发者生效）：",
            f"当前 {total_bytes}B / {total_lines} 行。本提示不卡红、不影响退出码。",
            "下一次改动该分册前，必须先把它再拆一层："
            "优先在册内按既有章节继续切分（新分册照 docs/技术设计/验收矩阵-*.md 命名，"
            "并在总册分册索引里登记），或按存活状态归档到 docs/参考证据/验收矩阵-归档.md。"
            "详见总册 docs/技术设计/验收矩阵.md 头部「体量预算」小节。",
            banner,
        ]
    )


def render_total_size_report(documents: dict[str, str]) -> str:
    """逐册打印总量状态，末尾补一行全集合合计。

    合计只是给人看的信号，没有独立阈值——触发线的判定单位就是单个文件（Issue #479
    口径更新），这里不引入第二套没被批准的判定标准。
    """
    lines = [f"{name}：{render_total_size_notice(text)}" for name, text in documents.items()]
    total_bytes = sum(len(text.encode("utf-8")) for text in documents.values())
    total_lines = sum(len(text.splitlines()) for text in documents.values())
    lines.append(
        f"验收矩阵全集合合计：{len(documents)} 个文件、{total_bytes}B、{total_lines} 行"
        "（合计无独立阈值，触发线按单个文件判定；此行供人判断整体膨胀）"
    )
    return "\n".join(lines)


def run_check() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        documents = read_matrix_documents()
    except BaselineError as error:
        print(f"验收矩阵单行体量棘轮检查失败：{error}", file=sys.stderr)
        return 1

    current = measure_documents(documents)
    print(render_total_size_report(documents))

    failures = evaluate(baseline, current)
    if failures:
        print("验收矩阵单行体量棘轮检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    over_threshold = sum(1 for size in current.values() if size > THRESHOLD_BYTES)
    print(
        f"验收矩阵单行体量棘轮：通过（扫描 {len(documents)} 个文件、{len(current)} 条断言行，"
        f"阈值 {THRESHOLD_BYTES}B，{over_threshold} 条在棘轮基线内，{len(baseline)} 条基线登记）"
    )
    return 0


def run_refresh() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        documents = read_matrix_documents()
    except BaselineError as error:
        print(f"验收矩阵单行体量棘轮刷新失败：{error}", file=sys.stderr)
        return 1

    current = measure_documents(documents)

    # 与 check_size_ratchet.py 的 run_refresh 同一纪律：只处理「基线记录 > 实测」
    # 这一类（该编号已经缩小、或有人手工调大了基线，--refresh 一律以实测为准
    # 改写），拒绝代为解决另外两类——「超过棘轮基线记录的上限」是真实违规，
    # 「新超过阈值且未登记」--refresh 从不自动添加新登记（同 check_size_ratchet.py
    # 模块文档字符串④处 B1 回归教训：只挡这两类、放行第三类，否则会把 --refresh
    # 唯一的正常用途——收紧基线——也一起挡掉）。
    blocking_failures = [
        failure
        for failure in evaluate(baseline, current)
        if "超过棘轮基线记录的上限" in failure or "新超过单行体量棘轮阈值" in failure
    ]
    if blocking_failures:
        print(
            "拒绝刷新：仓库当前存在 --refresh 无法代为解决的失败——"
            "「超过棘轮基线记录的上限」是该编号违反了棘轮，先把对应行缩回基线记录的"
            "字节数以内；「新超过单行体量棘轮阈值…且未登记在基线里」--refresh 从不"
            "自动添加新登记，需要人工按提示处理：",
            file=sys.stderr,
        )
        for failure in blocking_failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    new_baseline = {
        identifier: current[identifier]
        for identifier in baseline
        if identifier in current and current[identifier] > THRESHOLD_BYTES
    }

    if new_baseline == baseline:
        print(f"验收矩阵单行体量棘轮基线：已是最新（{len(baseline)} 条登记），无需刷新")
        return 0

    lowered = sorted(
        identifier
        for identifier in new_baseline
        if identifier in baseline and new_baseline[identifier] < baseline[identifier]
    )
    removed = sorted(identifier for identifier in baseline if identifier not in new_baseline)

    BASELINE_PATH.write_text(render_baseline(new_baseline), encoding="utf-8")

    if lowered:
        print(
            "已调低："
            + "、".join(f"{identifier}（{baseline[identifier]}→{new_baseline[identifier]}）" for identifier in lowered)
        )
    if removed:
        print("已移除（已缩到阈值以下或已删除/改名）：" + "、".join(removed))
    print(f"验收矩阵单行体量棘轮基线已刷新：{len(new_baseline)} 条登记")
    return 0


def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False：与 check_size_ratchet.py 同一纪律，--refresh 本身有
    # 写入副作用，缩写匹配（如 --r/--ref）绝不能被 argparse 默认放行。
    parser = argparse.ArgumentParser(
        description="验收矩阵单行体量棘轮门禁（Issue #335）", allow_abbrev=False
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="重新丈量已登记编号的实际字节数，只调小或移除；行比登记的更大时拒绝写入",
    )
    args = parser.parse_args(argv)
    if args.refresh:
        return run_refresh()
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
