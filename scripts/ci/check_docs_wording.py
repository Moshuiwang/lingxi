#!/usr/bin/env python3
"""用词门禁：正式文件禁用英文行话的逐字直译（Issue #597，替换表见 `docs/技术设计/代码规范.md`）。

被禁的那个字是英文 leg 的中文直译，2026-08-06 由一条 CI 注释带进来，此后沿
「合同 → 实施报告 → Issue 评论 → 接手卡」扩散到四十余行，而产品负责人看不懂它。

扫描面是 `git ls-files` 列出的全部受版本控制文件，按**原始字节**匹配：不整文件
decode，二进制文件因此也不构成盲区；`git ls-files` 失败或列不出文件一律判红——
扫不动不等于没问题。

豁免只按文件路径精确列举、每条带理由，绝不用目录通配（通配等于给未来所有 Trace
开一条后门）。清单里的文件若已不在版本控制里、或已经不再命中，同样判红：前者防
陈旧登记，后者逼清单自然变短。`tests/test_docs_wording_gate.py` 钉住这份清单只减
不增——没人能靠往清单里加文件绕过本门禁。
"""

from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 本文件不直接写出被禁的那个字：写了会被本门禁自己扫到，逼得脚本要么判自己红，
# 要么把自己加进豁免清单——那就是门禁上的第一个后门。改用码位转义构造。
BANNED_CHARACTER = "\u817f"
BANNED_BYTES = BANNED_CHARACTER.encode("utf-8")

REPLACEMENT_HINT = (
    f"替换口径（`docs/技术设计/代码规范.md` 四）：「{BANNED_CHARACTER}」→ "
    "旅程或调用链路的一步用「环节」；CI 矩阵、构建的一条用「路径」或「分支」；"
    f"「代码{BANNED_CHARACTER}证据」这类用「代码侧证据」。逐句读上下文选词，不要机械全局替换。"
)

# 豁免清单：路径 → 理由。**精确到文件，不接受目录通配。**外层包一层只读映射：
# 否则 `__main__` 里一句 `EXEMPT_FILES[...] = ...` 就能在运行期加一条豁免，而钉住
# 测试是 import 期读的清单，看不见这次扩容（外审 2026-09-06 实测的绕过路径）。
_EXEMPT_FILES: Mapping[str, str] = {
    "docs/traces/469-rc22打磨与体验批/合同.md": (
        "产品负责人 2026-09-06 裁定「已经收口的 trace 合同文件不用改了」——历史批准记录不改写"
    ),
    "docs/traces/521-rc24正式上线/合同.md": (
        "同上：已收口 Trace 的历史批准记录，按产品负责人 2026-09-06 裁定不动"
    ),
    "docs/traces/630-清仓批一/合同.md": "本门禁所属批次的合同正文，正文本身就在讨论这个被禁词",
    "docs/traces/630-清仓批一/任务表.md": "同上：本批任务表正文即在讨论这个被禁词",
    "docs/traces/630-清仓批一/验收.md": "同上：本批验收标准正文即在讨论这个被禁词",
    "docs/技术设计/代码规范.md": "替换表所在处——不写出被禁的词，这张表就没有意义",
}

EXEMPT_FILES: Mapping[str, str] = types.MappingProxyType(_EXEMPT_FILES)


# 已知边界（外审 2026-09-06 实测，明确接受，不构成绕过许可）：按 UTF-8 字节匹配，
# 因此非 UTF-8 编码（UTF-16/32、GB18030）里的该字扫不到，反过来那些编码的合法
# 文本也可能偶然含这串字节而被误伤；`read_bytes` 跟随符号链接，读的是目标内容；
# 本机运行时列的是索引、读的是工作区，暂存后又改回干净内容能骗过本机（CI 是干净
# 检出，拦得住）。三者都要刻意操纵才能触发，本仓库全部文本是 UTF-8。


def tracked_files(root: Path) -> list[str] | None:
    """列出受版本控制的文件；列不出来返回 None（调用方据此失败关闭）。"""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [chunk.decode("utf-8", "surrogateescape") for chunk in completed.stdout.split(b"\0")]
    return [path for path in paths if path] or None


def _hits(root: Path, relative: str) -> list[tuple[int, str]] | None:
    """按原始字节找命中行；文件读不出来返回 None（同样失败关闭）。"""
    try:
        data = (root / relative).read_bytes()
    except OSError:
        return None
    if BANNED_BYTES not in data:
        return []
    return [
        (number, line.decode("utf-8", "replace").rstrip("\r"))
        for number, line in enumerate(data.split(b"\n"), start=1)
        if BANNED_BYTES in line
    ]


def _report(title: str, lines: Sequence[str]) -> None:
    print(title, file=sys.stderr)
    for line in lines:
        print(f"  - {line}", file=sys.stderr)


def run(root: Path, tracked: Sequence[str], exempt: Mapping[str, str]) -> int:
    """核对一份扫描面，返回退出码。四类问题一次全报，不让第一类掩盖其余三类。"""
    for path, reason in exempt.items():
        if not reason.strip():
            _report("用词门禁：豁免清单里有条目没写理由（每条豁免必须写明裁定依据）：", [path])
            return 1

    tracked_set = set(tracked)
    stale = [path for path in exempt if path not in tracked_set]
    unreadable: list[str] = []
    offenders: list[str] = []
    removable: list[str] = []

    for path in tracked:
        hits = _hits(root, path)
        if hits is None:
            unreadable.append(path)
        elif path in exempt:
            if not hits:
                removable.append(path)
        else:
            offenders.extend(f"{path}:{number}: {text.strip()}" for number, text in hits)

    if stale:
        _report(
            "用词门禁：豁免清单登记的文件已不在版本控制里（改名或删除时必须同步删登记）：", stale
        )
    if unreadable:
        _report("用词门禁：以下受版本控制的文件读不出来，扫描面不完整（失败关闭）：", unreadable)
    if removable:
        _report(
            "用词门禁：以下豁免已经没有命中，请从 EXEMPT_FILES 里删掉（清单只该变短）：", removable
        )
    if offenders:
        _report("用词门禁：以下位置出现了被禁的英文直译词：", offenders)
        print(REPLACEMENT_HINT, file=sys.stderr)

    if stale or unreadable or removable or offenders:
        return 1

    print(
        f"用词门禁：{len(tracked)} 个受版本控制文件全部通过（豁免 {len(exempt)} 个，逐条登记有理由）"
    )
    return 0


def main() -> int:
    tracked = tracked_files(REPO_ROOT)
    if tracked is None:
        print(
            "用词门禁：`git ls-files` 失败或没有列出任何文件，扫描面为空——判红。"
            "扫不动不等于没问题，这里不允许静默通过。",
            file=sys.stderr,
        )
        return 1
    return run(REPO_ROOT, tracked, EXEMPT_FILES)


if __name__ == "__main__":
    raise SystemExit(main())
