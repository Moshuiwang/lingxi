"""`scripts/ci/check_size_ratchet.py` 的解析与判定用例（Issue #238）。

跟 ``test_acceptance_matrix_check.py`` 同一惯例：每个用例先构造一份会违规的输入，
断言它被具体地拒绝，而不是只跑一遍真实基线看它绿——一份只会通过的检查等于没有检查。
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_size_ratchet.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("size_ratchet_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class BaselineParsingTest(unittest.TestCase):
    def test_good_baseline_parses(self) -> None:
        text = "# 注释行\n\n2048\tsrc/lingxi/apps/scheduler/__init__.py\n1951\tsrc/lingxi/adapters/postgres_conversation.py\n"
        self.assertEqual(
            CHECK.parse_baseline(text),
            {
                "src/lingxi/apps/scheduler/__init__.py": 2048,
                "src/lingxi/adapters/postgres_conversation.py": 1951,
            },
        )

    def test_malformed_row_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("not-a-number\tsrc/lingxi/x.py\n")

    def test_missing_tab_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("2048 src/lingxi/x.py\n")

    def test_duplicate_path_fails_closed(self) -> None:
        with self.assertRaises(CHECK.BaselineError):
            CHECK.parse_baseline("100\tsrc/lingxi/x.py\n200\tsrc/lingxi/x.py\n")


class EvaluateTest(unittest.TestCase):
    """核心判定逻辑：不依赖磁盘，直接喂 (baseline, current) 字典。"""

    def test_growth_beyond_recorded_ceiling_is_rejected(self) -> None:
        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current = {"src/lingxi/apps/scheduler/__init__.py": 2050}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("超过棘轮基线记录的上限" in f for f in failures), failures)

    def test_shrinking_below_recorded_ceiling_is_rejected_until_refreshed(self) -> None:
        """基线必须与实测精确相等；这正是拒绝「手工把基线调大」的手段（见下一条用例）。"""

        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current = {"src/lingxi/apps/scheduler/__init__.py": 1800}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)

    def test_exact_match_passes(self) -> None:
        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_new_file_crossing_threshold_unregistered_is_rejected(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/core/new_giant_module.py": CHECK.THRESHOLD_LINES + 1}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("新超过体量棘轮阈值" in f for f in failures), failures)

    def test_file_under_threshold_and_unregistered_passes(self) -> None:
        baseline: dict[str, int] = {}
        current = {"src/lingxi/core/small_module.py": 42}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_deleted_file_leaving_baseline_is_not_a_failure(self) -> None:
        """文件已经不在扫描范围内：棘轮的目的已经达成，不强制立即刷新。"""

        baseline = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        current: dict[str, int] = {}
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_manually_inflating_the_baseline_without_touching_the_file_is_rejected(self) -> None:
        """自证：试图把基线调大 ⇒ 红。文件实际还是 2048 行，基线被手工改成 9999。"""

        baseline = {"src/lingxi/apps/scheduler/__init__.py": 9999}
        current = {"src/lingxi/apps/scheduler/__init__.py": 2048}
        failures = CHECK.evaluate(baseline, current)
        self.assertTrue(any("与实测" in f and "不一致" in f for f in failures), failures)


class RenderRoundTripTest(unittest.TestCase):
    def test_render_then_parse_round_trips(self) -> None:
        entries = {"src/lingxi/x.py": 1600, "src/lingxi/y/z.py": 2000}
        rendered = CHECK.render_baseline(entries)
        self.assertEqual(CHECK.parse_baseline(rendered), entries)


class RealBaselineIsHonestTest(unittest.TestCase):
    """反向验证：仓库里已经提交的基线文件本身必须诚实（与真实行数精确相等）。

    不钉死具体路径集合——#237/#239 两个并行 Story 分别把
    ``apps/scheduler/__init__.py`` 与 ``adapters/postgres_conversation.py``
    拆分成包，基线里哪些文件仍然超阈值会随之变化（甚至可能变成空集，见
    ``EmptyBaselineIsLegalTest``）。这条测试只断言基线本身**诚实**——记录值
    与实测精确相等、且每一条都真的落在 ``src/lingxi/`` 范围内——不断言具体
    是哪几个文件（2026-08-19 三方合并演练实测坐实：钉死具体路径的旧版本在
    #245 落地后立刻 FAIL，这不是本条测试该守的东西）。
    """

    def test_committed_baseline_matches_actual_line_counts(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        current = CHECK.measure(CHECK.iter_scope_files())
        self.assertEqual(CHECK.evaluate(baseline, current), [])

    def test_committed_baseline_entries_are_all_within_scope(self) -> None:
        baseline = CHECK.load_baseline(CHECK.BASELINE_PATH)
        for path in baseline:
            self.assertTrue(path.startswith("src/lingxi/"), path)

    def test_the_real_committed_baseline_matches_the_registered_entries(self) -> None:
        """钉住真实提交的基线文件内容——不是 ``EmptyBaselineIsLegalTest`` 里
        构造的合成夹具。任何新增登记必须有意识地同步更新这条测试并说明理由。

        登记史：2026-08-19 #237/#247 拆分后基线曾为空；2026-08-22 登记
        `assembly.py`（1535 行）——PR #292（装配期管理群警告）与 PR #293
        （停机接线与预算装配）各自都低于阈值，合并后组合首次突破；两侧均为
        必要改动，结构性拆分登记为 #284 后续（与背景线程改造同批评估），
        不在合并窗口塞设计级重构。2026-08-25 调到 1539 行——Issue #281 改道
        （Trace #304 批次 3）：`build_loop` 装配存量令牌只读源并把它传给
        `_build_onboarding_duty`（新增一个构造参数 + 一次转发 + 一处函数内
        import + 一次装配调用，净增 4 行），复用权限发布表既有的应用身份
        令牌供给、不新增凭据材料；改动净增内容已尽量收在这四行，未随手夹带
        本文件其余部分的重构。2026-08-27 调到 1538 行——Trace #328 S-P-2（存量
        权限沿用）：`permission_table_supply` 的构造从「发布消费」调用点之前
        挪到「每日权限重算」调用点之前（两个新调用点都要复用同一条供给），
        紧邻处新增同构的 `legacy_source`（`BitablePermissionTable` 实例，坐标
        缺失时为 `None`）并转发给两个 `_build_*_duty`；净变化包含新增代码与
        一段注释重排（把一段 10 行的既有说明合并成同义的更短句），两者相抵后
        文件净**减少** 1 行——`--refresh` 因此把基线调小而不是调大，纪律仍是
        「先改动本文件、再同步这条测试」。结构性拆分仍是 #284 后续。2026-08-27
        新增 `onboarding_runner.py`（1541 行，Trace #328 E-P opus 审查修复包）：
        红线-1 legacy 有界化在开通侧同判据接线——新增 `PublishHistorySource`
        Protocol、构造参数、`_resolve_legacy_source` 判定方法（通配跳过 + 发布
        足迹有界化）与红线-2 全抑制走 `_not_authorized` 出口的判断，均为本包
        「必修」红线修复要求的新代码，已尽量压缩注释（原始净增约 60 行，压到
        42 行）；`assembly.py` 同一批改动净增 1 行（`publish_history=` 转发），
        与既有 `legacy_source=` 转发合并成同一行以省出这一行，基线数值不变。
        `--refresh` 不会自动登记新文件（onboarding_runner.py 此前从未超过阈值），
        本条目按门禁提示人工加入基线。下一次这条测试失效时，同样在此登记理由。
        2026-08-28 移除 `onboarding_runner.py`——Trace #358 S-H-1（Issue #350
        Gate G-3 裁定 Option A）纯移动拆分：14 个 Protocol + `EnvironmentResult`
        搬进新文件 `onboarding_ports.py`、`_Terminal`/异常类/`_with_reference`/
        两个失败工厂/全部 `STATE_*`/`KEY_*` 常量搬进 `onboarding_terminal.py`、
        两个模块级纯函数搬进 `onboarding_support.py`；核心文件只剩模块文档字符串
        + `AutoOnboardingRunner` 主类，实测 1295 行，退回阈值以下，`--refresh`
        按规则自动移除该条目。三个新文件均远低于阈值，未新增基线登记。

        2026-08-28 移除 `assembly.py`——Trace #358 S-H-2（Issue #350 Gate G-3
        裁定 Option A）纯移动拆分：十个 `_build_*` 装配函数（花名册审计+快照
        写入→`roster_audit.py`；权限重算→`permission_refresh.py`；权限链到期
        清理→`retention.py`；就绪跟进+权限发布+告警出口→`permission_publish.py`
        与新文件 `permission_readiness_assembly.py`（就绪跟进单独成文——它含
        合法的通知重试 `sleep=stop.wait`，并入 `permission_publish.py` 会被
        `test_permission_publish_duty.py::NonBlockingTest` 的全文件级否定扫描
        连坐命中）；开通装配→`onboarding.py`；迟到就绪恢复→
        `late_readiness_recovery.py`；停摆收口→`stalled_provisioning.py`；
        组织快照同步+`_stop_aware_sleep`→`org_snapshot_sync.py`）逐组搬空，
        核心文件只剩 `build_loop` 总装配 + 全部 `_build_*` 名字的 re-export，
        实测 337 行，远退回阈值以下，`--refresh` 按规则自动移除该条目。七个
        目标文件（六个既有 + 一个新文件）均远低于阈值，未新增基线登记。

        2026-09-03 重新登记 `onboarding_runner.py`（1502 行，rc25 修复包 F2）：
        修复「ops 清单把已 active、名单授权没落报成 provisioned」要求在
        `_run` 的续行前复核返回点（`grant` 与 `recheck` 同在的唯一位置）加
        4 行（import `replace`、判定、2 行注释、标注返回）；文件此前 1497 行、
        任何实质改动都会顶穿 1500 阈值。自己的注释已从 5 行压到 2 行（完整
        语义在 `OnboardingResult` 文档与 preprovision.py 的 OUTCOME 旁注），
        修复包范围内不做编排器结构性拆分（风险远大于收益），按门禁「确有
        理由」路径人工登记 1502 为封顶——此后只许变小；下一次增长触发门禁时
        应优先拆分。下一次这条测试失效时，同样在此登记理由。
        2026-09-04 新增 `apps/worker/service.py`（1533 行，#593 热修 PR #595）：main
        恰好 1500 行；`run()` 增加「task_queued 监听连接断开 → 重建监听、建不起来
        退回轮询、失败退避一拍」（净增 33 行，含说明为什么监听断开不能带走进程的
        注释）。这是完成标准 2（断线不重启进程）的必要改动，在 main 上用
        pg_terminate_backend 实测复现进程退出。结构性拆分不塞进热修窗口，作为
        后续技术债登记在 #593。
        `onboarding_runner.py`（1502 行）复核：一次批量 lint 自动修复曾把该文件里
        五个仅供跨模块 re-export 的导入名（`EnvironmentResult`、
        `KEY_INTERNAL_ERROR`、`KEY_NOT_AUTHORIZED`、`KEY_STALLED`、
        `_KEYS_REQUIRING_REFERENCE`）连同各自的说明注释一并误删——这些名字本文件
        自身确实不用，但外部消费方按名字从这里取用，删掉即触发导入报错；跑一次
        全量 `unittest discover` 才暴露。逐字恢复原始导入与注释后，行数精确回到
        1502，与该批开工前的登记值相同：该文件在当前 lint 规则集下没有可安全
        移除的死代码，1502 继续保留为封顶，不下调，也不再登记新增理由。
        """

        self.assertEqual(
            CHECK.load_baseline(CHECK.BASELINE_PATH),
            {
                "src/lingxi/core/identity/onboarding_runner.py": 1502,
                "src/lingxi/apps/worker/service.py": 1533,
            },
        )


class EmptyBaselineIsLegalTest(unittest.TestCase):
    """空基线是合法终态，不是异常：当两个大文件都被拆分完、``src/lingxi/``
    下不再有任何文件超过阈值时，棘轮退化成"不许新文件跨过 1500 行"，那仍是
    它的主要价值——不能把"基线空"和"扫描坏了"混为一谈（补充复查要求，
    2026-08-19，与 A6"目录存在但零个 .py 文件"是两回事：那种是扫描本身失败，
    这里是扫描正常、只是没有文件超阈值）。
    """

    def test_empty_baseline_against_no_over_threshold_files_passes(self) -> None:
        self.assertEqual(CHECK.evaluate({}, {"src/lingxi/core/ids.py": 42}), [])

    def test_baseline_file_containing_only_the_header_parses_to_empty_dict(self) -> None:
        header_only = "\n".join(CHECK.BASELINE_HEADER) + "\n"
        self.assertEqual(CHECK.parse_baseline(header_only), {})


class RunRefreshClassificationTest(unittest.TestCase):
    """``run_refresh`` 必须只挡「超过上限」与「新文件未登记」两类失败，放行
    「基线记录 > 实测」（那正是它该修的陈旧记录）——这是本次修复过程中先犯
    了一次的回归（把三类失败一律挡下，导致 #245 合并后一次正常的收紧刷新也
    被拒绝），起了这组测试才发现，一并固化为回归用例。
    """

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.source_root = root / "src" / "lingxi"
        self.source_root.mkdir(parents=True)
        self.baseline_path = root / "baseline.txt"

        self._orig_repository_root = CHECK.REPOSITORY_ROOT
        self._orig_source_root = CHECK.SOURCE_ROOT
        self._orig_baseline_path = CHECK.BASELINE_PATH
        # measure() 用 REPOSITORY_ROOT 计算相对路径当基线键；一并打桩成临时
        # 目录，否则真实仓库根与临时 SOURCE_ROOT 不在同一棵树下会直接抛错。
        CHECK.REPOSITORY_ROOT = root
        CHECK.SOURCE_ROOT = self.source_root
        CHECK.BASELINE_PATH = self.baseline_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        CHECK.REPOSITORY_ROOT = self._orig_repository_root
        CHECK.SOURCE_ROOT = self._orig_source_root
        CHECK.BASELINE_PATH = self._orig_baseline_path

    def _write_module(self, relative: str, line_count: int) -> None:
        path = self.source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"# {i}" for i in range(line_count)) + "\n", encoding="utf-8")

    def test_refresh_lowers_a_shrunk_entry(self) -> None:
        self._write_module("big.py", 1600)
        self.baseline_path.write_text(CHECK.render_baseline({"src/lingxi/big.py": 2000}), encoding="utf-8")
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"src/lingxi/big.py": 1600})

    def test_refresh_removes_an_entry_that_shrank_below_threshold(self) -> None:
        self._write_module("shrunk.py", 100)
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/shrunk.py": 2000}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 0)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {})

    def test_refresh_refuses_when_an_unregistered_file_newly_crosses_threshold(self) -> None:
        """B1 的回归用例本体：新文件超阈值且未登记时，--refresh 必须拒绝写入，
        不能返回 0 让人误以为已经处理好了。"""

        self._write_module("registered.py", 100)  # 缩小，refresh 本该能处理
        self._write_module("new_giant.py", CHECK.THRESHOLD_LINES + 50)  # 全新违规
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/registered.py": 2000}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        # 拒绝时不得动基线文件——既有的合法收紧也不该被这次失败连累着丢失。
        self.assertEqual(
            CHECK.load_baseline(self.baseline_path), {"src/lingxi/registered.py": 2000}
        )

    def test_refresh_refuses_when_a_registered_file_grew_past_its_ceiling(self) -> None:
        self._write_module("grown.py", 2100)
        self.baseline_path.write_text(
            CHECK.render_baseline({"src/lingxi/grown.py": 2000}), encoding="utf-8"
        )
        exit_code = CHECK.run_refresh()
        self.assertEqual(exit_code, 1)
        self.assertEqual(CHECK.load_baseline(self.baseline_path), {"src/lingxi/grown.py": 2000})


if __name__ == "__main__":
    unittest.main()
