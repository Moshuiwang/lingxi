"""``pyproject.toml`` 的 ruff 配置钉住用例。

两张清单只许变短，不许变长：

1. ``[tool.ruff.lint.per-file-ignores]``——收官后只剩三类：三条目录 glob（``tests/**``/
   ``migrations/**``/``scripts/**`` 整目录豁免 ``D``，决策清单 D-21）、这三个目录下其他
   规则码的存量条目、``src/lingxi/`` 下 ``PLR0913`` 存量（合同 §3 登记不做）与有意保留
   的 ``N818``。``src/lingxi/`` 下不再有任何 ``D`` 条目。
2. ``[tool.ruff.format].exclude``——收官后为空集：全仓每个 ``.py`` 都参与格式化，钉住
   的空集意味着任何新排除项即红。

判定方式是子集，不是逐字相等：允许某个文件的条目从表里整条删除，也允许某个文件
保留的规则码变少；唯一不允许的是**出现新文件**或**某个已登记文件出现新规则码**
——那意味着有人往清单里"加"而不是"减"，与本表"只收紧"的设计意图相反。

变异实测：临时在 ``pyproject.toml`` 的 per-file-ignores 里给任意一个已登记文件
追加一个此前没有的规则码，或新增一个此前未登记的文件条目，跑本文件应判红；
改完验证后删除该临时改动、清 ``__pycache__``。
"""

from __future__ import annotations

import subprocess
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_ruff_config() -> dict:
    with PYPROJECT.open("rb") as handle:
        document = tomllib.load(handle)
    return document["tool"]["ruff"]


#: `[tool.ruff]` 顶层 / `[tool.ruff.lint]` 允许出现的键的钉住快照——不是子集
#: 断言，是**逐字白名单**：出现钉住集合之外的新键（例如
#: `[tool.ruff.lint.extend-per-file-ignores]`、顶层 `extend-exclude`）即判红。
#: per-file-ignores 与 format.exclude 两张表仍按各自的"只减不增"子集断言，
#: 不在本白名单覆盖范围内（它们的钉住快照见上方 PINNED_PER_FILE_IGNORES /
#: PINNED_FORMAT_EXCLUDE）。
PINNED_TOP_LEVEL_KEYS = frozenset(["line-length", "lint", "format"])
PINNED_LINT_KEYS = frozenset(["select", "ignore", "pydocstyle", "pylint", "per-file-ignores"])
PINNED_FORMAT_KEYS = frozenset(["exclude"])
PINNED_LINE_LENGTH = 100
PINNED_SELECT = ["E", "W", "F", "I", "UP", "T10", "N", "D", "PLR0913"]
PINNED_IGNORE = ["E203", "E501", "UP031", "UP042", "UP040", "UP046", "D400", "D415"]
PINNED_PYLINT_MAX_ARGS = 10
PINNED_PYDOCSTYLE_CONVENTION = "google"


# 以下常量是收官时 pyproject.toml 里 [tool.ruff.lint.per-file-ignores] 的逐字快照，按路径排序：
# 三条目录 glob（决策清单 D-21）、tests/scripts/migrations 的非 D 存量条目、src/lingxi/ 的
# PLR0913 存量（合同 §3 登记不做）与有意保留的 N818。实际清单只许是它的子集：任何新文件、
# 任何已登记文件的新规则码即红；要放宽必须同时改这份快照并在 PR 正文说明。
PINNED_PER_FILE_IGNORES: dict[str, frozenset[str]] = {
    "migrations/**": frozenset(["D"]),
    "migrations/alembic/env.py": frozenset(["I001"]),
    "scripts/**": frozenset(["D"]),
    "scripts/ops/import_local_permission_override.py": frozenset(["F401"]),
    "scripts/probe_drive_folder_permissions.py": frozenset(["N818"]),
    "src/lingxi/adapters/claude_agent_session.py": frozenset(["PLR0913"]),
    "src/lingxi/adapters/postgres_conversation/_queue_outbox.py": frozenset(["PLR0913"]),
    "src/lingxi/adapters/postgres_local_permission.py": frozenset(["PLR0913"]),
    "src/lingxi/adapters/postgres_management_card_context.py": frozenset(["PLR0913"]),
    "src/lingxi/adapters/postgres_pending_action.py": frozenset(["PLR0913"]),
    "src/lingxi/apps/gateway/delivery.py": frozenset(["PLR0913"]),
    "src/lingxi/apps/scheduler/late_readiness_recovery.py": frozenset(["PLR0913"]),
    "src/lingxi/apps/scheduler/permission_publish.py": frozenset(["PLR0913"]),
    "src/lingxi/apps/scheduler/stalled_provisioning.py": frozenset(["PLR0913"]),
    "src/lingxi/apps/worker/report.py": frozenset(["PLR0913"]),
    "src/lingxi/core/admin/card_callback.py": frozenset(["PLR0913"]),
    "src/lingxi/core/admin/card_callback_management.py": frozenset(["PLR0913"]),
    "src/lingxi/core/admin/card_dispatch.py": frozenset(["PLR0913"]),
    "src/lingxi/core/admin/notification.py": frozenset(["N818"]),
    "src/lingxi/core/execution/card_stream.py": frozenset(["PLR0913"]),
    "src/lingxi/core/permission/targeted_recompute.py": frozenset(["PLR0913"]),
    "tests/**": frozenset(["D"]),
    "tests/test_claude_agent_session_adapter.py": frozenset(["PLR0913"]),
    "tests/test_document_delivery.py": frozenset(["PLR0913"]),
    "tests/test_gateway_transport.py": frozenset(["N818"]),
    "tests/test_management_card.py": frozenset(["N802"]),
    "tests/test_metric_map_single_source.py": frozenset(["N818"]),
    "tests/test_permission_publish_duty.py": frozenset(["PLR0913"]),
    "tests/test_permission_publish_postgres.py": frozenset(["N818"]),
    "tests/test_permission_publish_row.py": frozenset(["N802"]),
    "tests/test_permission_refresh_duty.py": frozenset(["PLR0913"]),
    "tests/test_roster_access_token_supply.py": frozenset(["N818", "PLR0913"]),
    "tests/test_targeted_permission_recompute.py": frozenset(["PLR0913"]),
    "tests/test_worker_entry.py": frozenset(["PLR0913"]),
}

# 六个贴线/冻结文件在结构性拆分完成前不参与全仓格式化，与 pyproject.toml
# [tool.ruff.format].exclude 逐字一致；拆分完成、移出该 exclude 列表后，这里
# 同步收紧（只许变短，不许变长）。
PINNED_FORMAT_EXCLUDE: frozenset[str] = frozenset()


class PerFileIgnoresOnlyShrinksTest(unittest.TestCase):
    """per-file-ignores 只许变短：新文件、新规则码都判红。"""

    def test_every_registered_file_is_within_the_pinned_set(self) -> None:
        """登记表里出现钉住快照没有的新文件即判红。"""
        actual = _load_ruff_config()["lint"]["per-file-ignores"]
        unpinned_files = sorted(set(actual) - set(PINNED_PER_FILE_IGNORES))
        self.assertEqual(
            unpinned_files,
            [],
            f"per-file-ignores 出现钉住快照里没有的新文件：{unpinned_files}——"
            "只允许删除或缩短既有条目，新增文件视为门禁被放宽，须先更新本文件的"
            "钉住快照并说明理由。",
        )

    def test_every_registered_codes_list_is_within_the_pinned_codes(self) -> None:
        """某个文件的豁免码里出现钉住快照没有的新码即判红。"""
        actual = _load_ruff_config()["lint"]["per-file-ignores"]
        overflowing: dict[str, list[str]] = {}
        for path, codes in actual.items():
            pinned_codes = PINNED_PER_FILE_IGNORES.get(path, frozenset())
            extra = sorted(set(codes) - pinned_codes)
            if extra:
                overflowing[path] = extra
        self.assertEqual(
            overflowing,
            {},
            f"以下文件的 per-file-ignores 出现钉住快照里没有的新规则码：{overflowing}"
            "——只允许收紧（删码），新增规则码视为门禁被放宽。",
        )

    def test_pinned_snapshot_itself_is_not_accidentally_empty(self) -> None:
        """自证：钉住快照本身不能是空字典。

        空字典会让两条子集断言永远空判通过，起不到钉住的作用——防止本文件被后续改动
        悄悄改成永远绿的空壳。
        """
        self.assertGreater(len(PINNED_PER_FILE_IGNORES), 0)


class FormatExcludeOnlyShrinksTest(unittest.TestCase):
    """format.exclude 只许变短；登记六个暂缓格式化文件，结构性拆分完成、移出
    exclude 列表后同步收紧这份钉住快照。"""

    def test_format_exclude_is_within_the_pinned_set(self) -> None:
        """format.exclude 出现钉住快照没有的新条目即判红。"""
        format_section = _load_ruff_config().get("format", {})
        actual = frozenset(format_section.get("exclude", []))
        extra = sorted(actual - PINNED_FORMAT_EXCLUDE)
        self.assertEqual(
            extra,
            [],
            f"format.exclude 出现钉住快照里没有的新条目：{extra}——只允许收紧。",
        )


class RuffConfigTableWhitelistTest(unittest.TestCase):
    """`[tool.ruff]` 整张表的白名单钉住：不是子集断言，是逐字相等——任何新键或
    值变化（`select`/`ignore`/`line-length`/`pylint.max-args`/
    `pydocstyle.convention`）都必须判红，`per-file-ignores`/`format.exclude`
    两张表继续走各自的"只减不增"子集断言（见上方两个 Test）。

    变异实测：加 `[tool.ruff.lint.extend-per-file-ignores]`、全局 `ignore`
    加 `"D"`、顶层加 `extend-exclude` 三种改动各自判红后还原。
    """

    def test_top_level_keys_match_the_pinned_set_exactly(self) -> None:
        actual = set(_load_ruff_config().keys())
        self.assertEqual(
            actual,
            set(PINNED_TOP_LEVEL_KEYS),
            f"[tool.ruff] 顶层键集合变了：实测 {sorted(actual)}，"
            f"钉住 {sorted(PINNED_TOP_LEVEL_KEYS)}——新增或删除顶层键视为门禁被"
            "放宽/意外收紧，须先更新本文件的钉住快照并说明理由。",
        )

    def test_lint_keys_match_the_pinned_set_exactly(self) -> None:
        actual = set(_load_ruff_config()["lint"].keys())
        self.assertEqual(
            actual,
            set(PINNED_LINT_KEYS),
            f"[tool.ruff.lint] 键集合变了：实测 {sorted(actual)}，"
            f"钉住 {sorted(PINNED_LINT_KEYS)}——例如新增 "
            "`extend-per-file-ignores` 这类旁路键也会在这里判红。",
        )

    def test_format_keys_match_the_pinned_set_exactly(self) -> None:
        actual = set(_load_ruff_config()["format"].keys())
        self.assertEqual(actual, set(PINNED_FORMAT_KEYS))

    def test_select_matches_exactly(self) -> None:
        self.assertEqual(_load_ruff_config()["lint"]["select"], PINNED_SELECT)

    def test_ignore_matches_exactly(self) -> None:
        self.assertEqual(_load_ruff_config()["lint"]["ignore"], PINNED_IGNORE)

    def test_line_length_matches_exactly(self) -> None:
        self.assertEqual(_load_ruff_config()["line-length"], PINNED_LINE_LENGTH)

    def test_pylint_max_args_matches_exactly(self) -> None:
        self.assertEqual(_load_ruff_config()["lint"]["pylint"]["max-args"], PINNED_PYLINT_MAX_ARGS)

    def test_pydocstyle_convention_matches_exactly(self) -> None:
        self.assertEqual(
            _load_ruff_config()["lint"]["pydocstyle"]["convention"],
            PINNED_PYDOCSTYLE_CONVENTION,
        )


class NoStrayRuffConfigFileTest(unittest.TestCase):
    """`--config pyproject.toml` 只是第一道防线（显式指定来源）；第二道防线是
    仓库里压根不允许存在一份会被 ruff 就近发现的旁路配置文件——独立审查实测
    坐实一份 `ruff.toml`/`.ruff.toml` 即使不被显式传入，仍会被 ruff 的默认
    配置发现顺序抢先命中。

    变异实测：在 `src/lingxi/core/` 下临时写一份 `ruff.toml`（内容
    `[lint]\\nselect = []`）——本测试判红；同时 `verify_repository.sh` 因为
    显式传了 `--config pyproject.toml`，ruff check 仍然按 pyproject.toml 的
    规则集判定，不会被这份旁路文件放行。验证后删除该临时文件。
    """

    def test_no_ruff_toml_or_dot_ruff_toml_is_tracked(self) -> None:
        # 不用 pathspec glob 过滤（git 的 fnmatch 对隐藏文件的 `*` 行为不保证
        # 跨版本一致）：直接列出全部受版本控制文件，在 Python 里按 basename
        # 精确匹配，行为不依赖 git 版本或 core.globPathspecs 配置。
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked = [line for line in result.stdout.splitlines() if line]
        offenders = [path for path in tracked if Path(path).name in ("ruff.toml", ".ruff.toml")]
        self.assertEqual(
            offenders,
            [],
            f"仓库里出现了会被 ruff 就近发现抢先生效的旁路配置文件：{offenders}——"
            "唯一合法的配置来源是 pyproject.toml，删除这些文件。",
        )


if __name__ == "__main__":
    unittest.main()
