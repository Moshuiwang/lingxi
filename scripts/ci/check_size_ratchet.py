#!/usr/bin/env python3
"""文件体量棘轮门禁（Issue #238）：已超过阈值的文件只许变小、不许变大。

代码框架长期缺一道挡住「往同一个巨型文件里继续加东西」的门禁：#227 与 S-D-02 各自
往 2048 行的 ``apps/scheduler/__init__.py`` 里加代码，两次都没有任何检查会红。

**不设绝对行数上限**：本仓库文档字符串极重（``core/permission/publish_row.py`` 980
行里很大一块是连贯的中文正文说明），一个偏低的绝对上限只会逼人把连贯逻辑劈成互相
牵连、更难读的两半。改用**棘轮**：

- 已经超过阈值（1500 行）的文件，登记在
  ``scripts/ci/size_ratchet_baseline.txt`` 里，只许变小、不许变大；
- 未超阈值的文件不得新超过阈值——不存在"先登记后随便涨"的口子；
- 基线**必须是生成的**：``--refresh`` 重新丈量已登记文件的当前行数，只会调小或整条
  移除（文件缩到阈值以下），**从不新增登记、也拒绝把任何一条调大**——如果某个已登记
  文件的行数比基线记录的还多，``--refresh`` 直接拒绝写入并报错，不会把这次增长当成
  新的基线；这正是"棘轮"这个名字的来源。

阈值取 1500 行：本仓库 ``src/lingxi/`` 下一次真实丈量显示，绝大多数模块（含文档字符串
很重的 ``publish_row.py`` 980 行、``mcp_readiness.py`` 1140 行）都在 1500 行以内，
只有 ``adapters/postgres_conversation.py``（1951 行）与 ``apps/scheduler/__init__.py``
（2048 行）这两个已知需要拆分的文件超过它——这正是 Issue #238 点名允许存量存在、
但要求今后只减不增的两个文件。取更低的阈值会把 publish_row.py / mcp_readiness.py
这类偏长但内聚、连贯的文件也提前拖进棘轮，逼着今后自然的补充硬拆成不连贯的两半，
不符合 Issue 的明确要求。

范围只覆盖 ``src/lingxi/`` 下的正式代码：测试文件按用例数量自然变长是正常现象，
拆分测试的成本和收益与拆分业务逻辑完全不同，不在本门禁的目标问题（生产代码里的
职责堆积）之内。

扫描失败必须失败关闭：源码根目录缺失、基线文件缺失或格式不合法、任何一个受扫描
文件读不出来，都直接判红，不能把"没扫到"悄悄当成"通过"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "lingxi"
BASELINE_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "size_ratchet_baseline.txt"
THRESHOLD_LINES = 1500

BASELINE_HEADER = (
    f"# 文件体量棘轮基线（Issue #238）：登记当前已超过 {THRESHOLD_LINES} 行的文件与其行数上限。",
    "# 由 scripts/ci/check_size_ratchet.py --refresh 生成，请不要手工调大数值——",
    "# 门禁会重新丈量文件的实际行数，任何比这里记录的更大的实测值都直接判红；",
    "# --refresh 只会把数值调小或整条移除（文件缩到阈值以下），拒绝写入任何增长。",
    "# 一个从未超过阈值的文件第一次超过阈值时，不会被 --refresh 自动登记进来：",
    "# 先拆分或精简该文件；确有理由要接受它作为新的棘轮登记对象，人工在下面加一行",
    "# 「行数<TAB>路径」，门禁会核对这一行是否等于该文件的当前实际行数。",
)


class BaselineError(ValueError):
    """基线文件读取或格式错误——必须失败关闭，不能当作空基线继续跑。"""


def iter_scope_files() -> list[Path]:
    if not SOURCE_ROOT.is_dir():
        raise BaselineError(f"源码根目录不存在：{SOURCE_ROOT}")
    return sorted(SOURCE_ROOT.rglob("*.py"))


def measure(paths: list[Path]) -> dict[str, int]:
    """路径 → 实际行数；任何一个文件读不出来都直接抛错（失败关闭）。"""

    counts: dict[str, int] = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            relative = path.relative_to(REPOSITORY_ROOT)
            raise BaselineError(f"无法读取 {relative}：{error}") from error
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        counts[relative] = len(text.splitlines())
    return counts


def parse_baseline(text: str) -> dict[str, int]:
    """解析「行数<TAB>路径」登记表；任何一行格式不对都直接抛错。"""

    entries: dict[str, int] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0].isdigit():
            raise BaselineError(
                f"基线文件第 {line_number} 行格式不合法（应为「行数<TAB>路径」）：{line!r}"
            )
        count_text, path_text = parts
        if path_text in entries:
            raise BaselineError(f"基线文件第 {line_number} 行重复登记同一路径：{path_text}")
        entries[path_text] = int(count_text)
    return entries


def render_baseline(entries: dict[str, int]) -> str:
    lines = list(BASELINE_HEADER)
    lines.append("")
    for path in sorted(entries):
        lines.append(f"{entries[path]}\t{path}")
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

    for path, recorded in sorted(baseline.items()):
        actual = current.get(path)
        if actual is None:
            # 文件已经不在扫描范围内（删除、改名或移出 src/lingxi）：棘轮的目的
            # 已经达成，不需要门禁介入；基线里的陈旧登记留给 --refresh 自愿清理。
            continue
        if actual > recorded:
            failures.append(
                f"{path}：当前 {actual} 行，超过棘轮基线记录的上限 {recorded} 行。"
                "规则是「已超过阈值的文件只许变小、不许变大」——"
                "请把这次改动净增的内容移出这个文件，或拆分其中一部分职责。"
            )
        elif actual < recorded:
            # 记录比实测更大：可能是文件已经缩小但没运行 --refresh，也可能是有人
            # 手工把基线数值调大了却没有对应地改动文件。两种情况在单次快照里长得
            # 一样，本门禁不猜测意图，一律要求基线与实测**精确相等**——这正是拒绝
            # 「手工调大基线」的手段：调大之后基线不再等于实测，直接判红；
            # 唯一能让检查再次变绿的路径是 --refresh，而 --refresh 只会把数值写成
            # 真实测得的行数，不采信基线文件里原有的数字。
            failures.append(
                f"{path}：棘轮基线记录 {recorded} 行，与实测 {actual} 行不一致。"
                "基线必须与实际行数精确相等，不允许留有余量（这也是本门禁拒绝"
                "「手工把基线调大」的手段）。运行 "
                "python3 scripts/ci/check_size_ratchet.py --refresh 校准。"
            )

    for path, actual in sorted(current.items()):
        if actual > THRESHOLD_LINES and path not in baseline:
            failures.append(
                f"{path}：{actual} 行，新超过体量棘轮阈值（{THRESHOLD_LINES} 行）且未登记在基线里。"
                "规则是「未超过阈值的文件不得新超过阈值」——"
                "请拆分或精简这个文件；如果确有理由要接受它作为新的棘轮登记对象，"
                f"在 {BASELINE_PATH.relative_to(REPOSITORY_ROOT)} 里人工加一行"
                f"「{actual}\\t{path}」并在 PR 里说明理由（--refresh 不会自动添加新文件）。"
            )

    return failures


def run_check() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"文件体量棘轮检查失败：{error}", file=sys.stderr)
        return 1

    failures = evaluate(baseline, current)
    if failures:
        print("文件体量棘轮检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    over_threshold = sum(1 for count in current.values() if count > THRESHOLD_LINES)
    print(
        f"文件体量棘轮：通过（扫描 {len(current)} 个源文件，阈值 {THRESHOLD_LINES} 行，"
        f"{over_threshold} 个文件在棘轮基线内，{len(baseline)} 条基线登记）"
    )
    return 0


def run_refresh() -> int:
    try:
        baseline = load_baseline(BASELINE_PATH)
        current = measure(iter_scope_files())
    except BaselineError as error:
        print(f"文件体量棘轮刷新失败：{error}", file=sys.stderr)
        return 1

    growth_failures = [
        failure
        for failure in evaluate(baseline, current)
        if "超过棘轮基线记录的上限" in failure
    ]
    if growth_failures:
        print(
            "拒绝刷新：以下文件的行数比基线记录的还大，这不是「基线该更新」，"
            "是「文件违反了棘轮」——先把文件缩回基线记录的行数以内，"
            "--refresh 不会把一次增长当成新的基线：",
            file=sys.stderr,
        )
        for failure in growth_failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    new_baseline = {
        path: current[path]
        for path in baseline
        if path in current and current[path] > THRESHOLD_LINES
    }

    if new_baseline == baseline:
        print(f"文件体量棘轮基线：已是最新（{len(baseline)} 条登记），无需刷新")
        return 0

    lowered = sorted(
        path
        for path in new_baseline
        if path in baseline and new_baseline[path] < baseline[path]
    )
    removed = sorted(path for path in baseline if path not in new_baseline)

    BASELINE_PATH.write_text(render_baseline(new_baseline), encoding="utf-8")

    if lowered:
        print("已调低：" + "、".join(f"{path}（{baseline[path]}→{new_baseline[path]}）" for path in lowered))
    if removed:
        print("已移除（已缩到阈值以下或已删除）：" + "、".join(removed))
    print(f"文件体量棘轮基线已刷新：{len(new_baseline)} 条登记")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="文件体量棘轮门禁（Issue #238）")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="重新丈量已登记文件的实际行数，只调小或移除；文件比登记的更大时拒绝写入",
    )
    args = parser.parse_args(argv)
    if args.refresh:
        return run_refresh()
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
