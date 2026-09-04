#!/usr/bin/env python3
"""把 Story PR 路由到文档快检、普通快检或完整门禁（Issue #82）。

未知路径一律升级到完整门禁。分类器的目标不是猜得尽可能细，而是让新增目录不会因为
没人更新路径表而静默绕过检查。
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

DOCUMENT_PREFIXES = ("docs/",)
DOCUMENT_FILES = {"AGENTS.md", "README.md"}
FULL_PREFIXES = (".github/workflows/", "deploy/", "migrations/", "scripts/ci/")
FULL_FILES = {".dockerignore", "Dockerfile", "alembic.ini", "pyproject.toml"}
FAST_PREFIXES = ("experiments/", "scripts/dev/", "src/", "tests/", "workers/")

# Issue #498：分层依据是「这批改动能改变什么」，而不是扩展名或提交者自称的
# 类型。L1 只允许用户可见文案与其追溯/术语事实源；L3 只允许两份权限事实配置，
# 并且必须额外生成权限影响面。清单故意使用**完整精确路径**，不接受通配符，避免
# 新增的配置文件、重命名或 quotePath 变体意外落入轻门禁。
L1_FILES = frozenset(
    {
        "src/lingxi/config/content.toml",
        "src/lingxi/config/content.lock.toml",
        "src/lingxi/config/admin_metric_alias_map.toml",
    }
)
L3_FILES = frozenset(
    {
        "src/lingxi/config/galaxy_role_function_map.toml",
        "src/lingxi/config/company_function_metric_map.toml",
    }
)
CONFIG_PREFIX = "src/lingxi/config/"

# Issue #520 F2（rc24 正式上线批）：**L1 轻量档当前停用，路由回完整门禁。**
#
# 停用理由是产品事实而不是实现细节：这三份内容资产**随镜像发布**。它们位于
# `src/**`，会命中 `publish.yml` 的路径过滤；而走轻量档时 Epic Full 不跑 image、
# 也不写候选证明，合入 main 之后 `verify_epic_candidate.py` 必然找不到候选而失败。
# 换句话说「只改文案、不构建镜像」这个前提本身站不住——文案要生效必须有新镜像。
#
# 判定这一档的代码（``L1_FILES`` / ``is_l1`` / ``l1_changed`` 输出，以及两个工作流
# 里的 l1 job）**原样保留**，留待上线后产品确认内容资产的发布路径，再把这个开关改回
# True 即可重启用。在那之前 ``l1_changed`` 仍照常输出，完整门禁里的「L1 资产版本锁
# 与术语扫描」步骤照常执行——轻量档停用不等于 L1 校验被跳过。
L1_LIGHT_ROUTE_ENABLED = False

# L2（提示词）只保留扩展位，不接线、不把任何现有路径归入这里。以后若产品批准
# 具体事实源，应显式登记路径；在此之前 unknown/full 是安全默认。
L2_FILES = frozenset()
L2_PREFIXES = ()


class Classification:
    """兼容旧 docs/fast/full，同时暴露风险与独立文档改动事实。"""

    __slots__ = ("mode", "risk_level", "docs_changed", "l1_changed", "l3_changed")

    def __init__(
        self,
        mode: str,
        risk_level: str,
        docs_changed: bool,
        l1_changed: bool,
        l3_changed: bool,
    ) -> None:
        self.mode = mode
        self.risk_level = risk_level
        self.docs_changed = docs_changed
        self.l1_changed = l1_changed
        self.l3_changed = l3_changed


def normalize_path(raw: str) -> str:
    """只剥掉 Git 可能带出的一个 ``./``，绝不 strip 文件名空白。"""

    return raw[2:] if raw.startswith("./") else raw


def normalized_paths(paths: list[str]) -> list[str]:
    return [normalize_path(path) for path in paths if path]


def is_l1(path: str) -> bool:
    return path in L1_FILES


def is_l3(path: str) -> bool:
    return path in L3_FILES


def is_l2(path: str) -> bool:
    return path in L2_FILES or path.startswith(L2_PREFIXES)


def classify_detail(paths: list[str]) -> Classification:
    """返回路由与风险级别；未知路径始终走完整门禁。"""

    normalized = normalized_paths(paths)
    if not normalized:
        return Classification("full", "full", False, False, False)

    docs_changed = any(is_document(path) for path in normalized)
    l1_changed = any(is_l1(path) for path in normalized)
    l3_changed = any(is_l3(path) for path in normalized)

    # 权限配置是最重等级，和任何其他改动混合时也不能降级。
    if l3_changed:
        return Classification("full", "l3", docs_changed, l1_changed, True)

    # L2 目前没有接线；若未来登记路径，先安全地复用完整门禁，避免形成未批准的
    # 「提示词快检」假门。产品批准后再单独实现其合同。
    if any(is_l2(path) for path in normalized):
        return Classification("full", "l2", docs_changed, l1_changed, False)

    # 配置目录里除上述白名单外的任何文件都可能改变用户体验、权限或事实源；不能
    # 因为它也位于 src/ 而沿用普通代码 fast。这个分支刻意早于 is_fast()。
    if any(
        path.startswith(CONFIG_PREFIX) and not is_l1(path) and not is_l3(path) and not is_l2(path)
        for path in normalized
    ):
        return Classification("full", "full", docs_changed, l1_changed, False)

    if any(is_full(path) for path in normalized):
        return Classification("full", "full", docs_changed, l1_changed, False)

    if all(is_document(path) or is_l1(path) for path in normalized):
        if l1_changed:
            if L1_LIGHT_ROUTE_ENABLED:
                return Classification("fast", "l1", docs_changed, True, False)
            # 停用期间路由到完整门禁（Issue #520 F2）。这样 image job 会跑、候选证明
            # 会写出来，L1 资产改动合入 main 之后 Main Publish 才有候选可回读。
            return Classification("full", "full", docs_changed, True, False)
        return Classification("docs", "l0", docs_changed, False, False)

    # L1 与普通源码/测试混合时仍必须执行 L1 专用校验，但沿用既有 fast 语义；
    # 这样文案旁边的安全代码变更不会偷偷省掉版本锁或术语扫描。
    if all(is_document(path) or is_fast(path) or is_l1(path) for path in normalized):
        return Classification("fast", "fast", docs_changed, l1_changed, False)

    return Classification("full", "full", docs_changed, l1_changed, False)


# scripts/ci/ 整目录默认提级到完整门禁，但其中已知的纯数据文件不含可执行逻辑、
# 不改变门禁脚本本身的判定行为，因此显式登记后单独按 fast 处理（Issue #298）。
# 新增候选必须显式写进这里——不在清单内的 scripts/ci/ 文件（哪怕文件名看起来也
# 像数据）默认仍然提级，防止「新增一个脚本文件、忘了登记」被静默当成数据放行。
FULL_PREFIX_DATA_FILES = frozenset(
    {
        "scripts/ci/size_ratchet_baseline.txt",
        "scripts/ci/matrix_row_size_baseline.txt",
        "scripts/ci/function_size_ratchet_baseline.txt",
        "scripts/ci/comment_ratchet_baseline.txt",
    }
)


def is_document(path: str) -> bool:
    return path in DOCUMENT_FILES or path.startswith(DOCUMENT_PREFIXES)


def is_full(path: str) -> bool:
    if path in FULL_PREFIX_DATA_FILES:
        return False
    return path in FULL_FILES or path.startswith(FULL_PREFIXES)


def is_fast(path: str) -> bool:
    return path.startswith(FAST_PREFIXES) or path in FULL_PREFIX_DATA_FILES


def classify(paths: list[str]) -> str:
    """旧调用方继续获得 docs/fast/full；新调用方应使用 :func:`classify_detail`。"""

    return classify_detail(paths).mode


def classify_risk(paths: list[str]) -> str:
    """Issue #498 风险级别（l0/l1/l2/l3/fast/full）。"""

    return classify_detail(paths).risk_level


def changed_paths(base: str, head: str, *, repository: Path | None = None) -> list[str]:
    result = subprocess.run(
        # 不过滤 D/T 等状态；并关闭 rename 折叠，让高风险旧路径和新路径都进入分类。
        # -z 让 Git 输出原始文件名并用 NUL 分隔；否则中文等非 ASCII 路径会被
        # core.quotePath 转义，docs/** 会被误判成未知高风险路径。bytes + surrogateescape
        # 还保留了不合法 UTF-8 文件名，使其无法被错误地折叠到安全路径。
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            base,
            head,
        ],
        check=True,
        capture_output=True,
        cwd=repository,
    )
    return [
        raw.decode("utf-8", errors="surrogateescape") for raw in result.stdout.split(b"\0") if raw
    ]


def write_output(destination: Path, classification: Classification, paths: list[str]) -> None:
    worker_changed = any(path.startswith("workers/") for path in paths)
    with destination.open("a", encoding="utf-8") as output:
        output.write(f"mode={classification.mode}\n")
        output.write(f"risk_level={classification.risk_level}\n")
        output.write(f"docs_changed={'true' if classification.docs_changed else 'false'}\n")
        output.write(f"l1_changed={'true' if classification.l1_changed else 'false'}\n")
        output.write(f"l3_changed={'true' if classification.l3_changed else 'false'}\n")
        output.write(f"worker_changed={'true' if worker_changed else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    paths = changed_paths(args.base, args.head)
    classification = classify_detail(paths)
    write_output(args.github_output, classification, paths)
    print(
        f"Story 路由：{classification.mode} / {classification.risk_level}"
        f"（{len(paths)} 个变更路径）"
    )
    for path in paths:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
