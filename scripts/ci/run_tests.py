#!/usr/bin/env python3
"""按分片跑 `tests/` 下的单元测试（Issue #712）。

`scripts/ci/verify_repository.sh` 此前直接调用
``PYTHONPATH=src python3 -m unittest discover -s tests -v``。把这一行换成调用
本脚本，是为了让"按文件分片只跑一部分测试"与"不分片、跑全部测试"共用同一套
判定逻辑与同一种输出格式——下游比对新旧两条路径时不用适配两套输出。

分片依据 ``LINGXI_TEST_SHARD_INDEX`` / ``LINGXI_TEST_SHARD_COUNT`` 两个环境
变量，按**测试文件**分桶，不按单条用例分桶：一是不拆散同一个 ``TestCase`` 类
共享的 ``setUpClass`` 状态，二是与 ``tests/postgres_schema.py`` 按 DSN 缓存表
结构的策略天然兼容——同一分片全程只连一个 DSN，缓存正好命中一次。

分桶算法是贪心装箱（按估计耗时从大到小排序，每次把当前文件放进眼下总耗时最
小的分片），依据是 ``scripts/ci/shard_durations.json`` 这份可提交的耗时清单，
不再按文件路径哈希取模——哈希不看每个文件实际跑多久，容易把几个耗时大户分进
同一片，让总时长被最慢那片锁死（#712 调粒度批的实测教训）。清单按"仓库相对
路径"记录每个测试文件上一次量出来的耗时；清单里查不到的文件（新增文件、清单
过期）**不会被跳过**，按清单里已有文件的平均耗时兜底分权重，照常分进某个分片。

清单怎么重新量：给 ``LINGXI_TEST_DURATIONS_DIR`` 指一个目录跑一遍（分片或不
分片都行），每个分片会各自写一份 ``durations-shard-<N>.json``（不分片时写
``durations-unsharded.json``），把这些文件合并、四舍五入后覆盖提交到
``scripts/ci/shard_durations.json`` 即可——这一步不追求自动化，改一次分桶策
略之间的间隔够长，手工合并的成本不值得为它单独造一条流水线。

**两个环境变量都不设**才视为"不分片"：发现全部测试、原样跑完，不额外打印任何
分片标记行——这正是改造前 ``unittest discover -s tests -v`` 的行为，是本脚本
必须留的回退路径，不是遗漏。只设其中一个是**响亮失败**（见 ``ShardEnvironmentError``）：
那多半是拼错或残留，猜一个默认值顶上会让人以为跑了全部、实际只跑了一片。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"

# 耗时清单的落库位置：与 run_tests.py 同目录，跟着 scripts/ci 一起走版本控制。
DURATION_MANIFEST_PATH = REPOSITORY_ROOT / "scripts/ci/shard_durations.json"

# 清单整体为空（例如第一次跑、还没量过任何数据）时的兜底权重——此时所有文件都
# 拿到同一个值，贪心装箱退化成"按文件数尽量均分"，不是报错，也不是不分片。
_DEFAULT_WEIGHT_SECONDS_FALLBACK = 1.0

# 分片跑批各自把这一行打到自己的日志里，供 verify_repository.sh 汇合后核对
# "分片数与预期是否相符、每片是否都真的跑完"——不复用 unittest 自带的
# `Ran N tests in Xs`，那一行既不点名是第几片，格式也不是为机器解析设计的。
_SHARD_SUMMARY_PREFIX = "RUN_TESTS_SHARD_SUMMARY"


def _iter_test_cases(suite: unittest.TestSuite) -> Any:
    """把 discover() 返回的嵌套 TestSuite 展平成单条 TestCase 的序列。"""

    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_test_cases(item)
        else:
            yield item


def _case_source_file(case: unittest.TestCase) -> str:
    """一条用例所在的源文件绝对路径，作为分片分桶的依据。

    模块导入失败时 unittest 会造一个占位 `_FailedTest`，它的 `__module__`
    指向 `unittest.loader` 本身——**实测下来这类用例是按 `unittest/loader.py`
    的路径分桶的**，不是按真正失败的那个文件，也不是按 `case.id()`。行为仍然
    安全：它稳定、仍然会被分到某一片、仍然会在失败时把该片染红，不会被静默
    漏掉；只是同一批导入失败会挤在同一片里，不影响正确性。
    """

    module = sys.modules.get(type(case).__module__)
    module_file = getattr(module, "__file__", None) if module else None
    if not module_file:
        return case.id()
    return str(Path(module_file).resolve())


def _manifest_key(source_file: str) -> str:
    """把源文件标识换算成耗时清单里用的键：仓库相对路径。

    清单要能跨机器、跨 worktree 提交复用，不能按绝对路径记——不同 checkout 的
    路径前缀不一样。`_case_source_file` 的正常输出是绝对路径，这里换算成相对
    路径；换算不出来（不是绝对路径，例如模块导入失败时退回的 `该文件在仓库树下的相对路径`，或
    者绝对路径根本不在仓库树下）一律原样返回——`_assign_files_to_shards` 的
    默认权重兜底会接住它，不需要在这里特殊处理或报错。

    注：模块导入失败造出的 `_FailedTest` 解析出的是标准库 `unittest/loader.py`
    的绝对路径，不在仓库树下，因此走「原样返回」那一支——**不是**退回 `case.id()`。
    把关审查实测坐实；此前这段与 `_case_source_file` 的说明互相矛盾。
    """

    path = Path(source_file)
    if not path.is_absolute():
        return source_file
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return source_file


def _load_duration_manifest(path: Path | None = None) -> dict[str, float]:
    """读耗时清单；文件不存在、内容损坏或格式不对都返回空字典，不报错中断。

    清单是本机/CI 跑批之间手工同步的辅助数据，不是权威真相——读不到就退回"当
    它是空的"，让 `_default_weight_seconds` 的兜底逻辑接管，而不是让整个分片
    流程因为一份数据文件的问题而跑不起来。
    """

    manifest_path = path if path is not None else DURATION_MANIFEST_PATH
    if not manifest_path.exists():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): float(value)
        for key, value in raw.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def _default_weight_seconds(manifest: dict[str, float]) -> float:
    """清单里查不到某个文件时，给它的估计耗时兜底。

    清单非空时取清单里已知文件的平均耗时——一个新文件大概率不是全仓最慢也不是
    最快的那一档，平均值比"当它是 0 秒"或"当它是全仓最长"都更接近事实。清单
    整体为空（例如还没有量过任何数据）时退回一个固定常数，让所有文件权重相同，
    贪心装箱此时退化成按文件数尽量均分。
    """

    if not manifest:
        return _DEFAULT_WEIGHT_SECONDS_FALLBACK
    return sum(manifest.values()) / len(manifest)


def _assign_files_to_shards(
    file_keys: list[str], shard_count: int, manifest: dict[str, float]
) -> dict[str, int]:
    """贪心装箱：按估计耗时从大到小排序，每次把当前文件放进总耗时最小的分片。

    这是经典的"最长优先"装箱启发式——先放大件才不会到最后被小件填不平的桶接住
    一个大件。`file_keys` 先经过一次普通排序再按权重降序排：Python 的排序是稳
    定的，权重相同（尤其是清单缺条目、大量文件共享同一个兜底权重）时就按文件路
    径的字母序决定先后，装箱结果与调用方传入 `file_keys` 的原始顺序无关，同一
    份清单在任何进程里都能独立算出同一个分配结果。
    """

    default_weight = _default_weight_seconds(manifest)
    ordered = sorted(
        sorted(file_keys),
        key=lambda key: manifest.get(key, default_weight),
        reverse=True,
    )
    bucket_totals = [0.0] * shard_count
    assignment: dict[str, int] = {}
    for key in ordered:
        weight = manifest.get(key, default_weight)
        target = min(range(shard_count), key=lambda index: (bucket_totals[index], index))
        assignment[key] = target
        bucket_totals[target] += weight
    return assignment


def _select_shard(
    suite: unittest.TestSuite,
    shard_index: int,
    shard_count: int,
    manifest: dict[str, float] | None = None,
) -> unittest.TestSuite:
    if manifest is None:
        manifest = _load_duration_manifest()
    cases = list(_iter_test_cases(suite))
    file_keys = sorted({_manifest_key(_case_source_file(case)) for case in cases})
    assignment = _assign_files_to_shards(file_keys, shard_count, manifest)
    selected = unittest.TestSuite()
    for case in cases:
        if assignment[_manifest_key(_case_source_file(case))] == shard_index:
            selected.addTest(case)
    return selected


def _record_case_durations(cases: list[unittest.TestCase]) -> dict[str, float]:
    """给每条用例的 `run` 包一层计时，按文件累加耗时，供重新生成耗时清单用。

    包的是实例属性 `run`（原生 snake_case），不是子类化 `TestResult` 去覆写
    `startTest`/`stopTest`——后者是驼峰命名，会撞上本仓库的 N802 规则（同类冲
    突与规避方式见 `_outcome_records` 的说明）。`TestCase.__call__` 内部调用的
    是 `self.run(...)`，属于普通属性查找，会用到这里实例级别的覆盖——用一个
    最小可复现的例子验证过这一点，不是凭印象假设 unittest 的内部实现。
    只在 `LINGXI_TEST_DURATIONS_DIR` 设置时才会被调用，不影响日常跑批路径。
    """

    durations: dict[str, float] = {}

    for case in cases:
        key = _manifest_key(_case_source_file(case))
        original_run = case.run

        def _timed_run(result: Any = None, _original: Any = original_run, _key: str = key) -> Any:
            start = time.perf_counter()
            try:
                return _original(result)
            finally:
                durations[_key] = durations.get(_key, 0.0) + (time.perf_counter() - start)

        case.run = _timed_run  # type: ignore[method-assign]

    return durations


def _duration_output_path(shard: tuple[int, int] | None) -> Path | None:
    directory = os.environ.get("LINGXI_TEST_DURATIONS_DIR")
    if not directory:
        return None
    suffix = f"shard-{shard[0]}" if shard is not None else "unsharded"
    return Path(directory) / f"durations-{suffix}.json"


class ShardEnvironmentError(RuntimeError):
    """分片环境变量本身不合法——必须响亮失败，不能猜一个默认值顶上。"""


def _read_shard_env() -> tuple[int, int] | None:
    index_raw = os.environ.get("LINGXI_TEST_SHARD_INDEX")
    count_raw = os.environ.get("LINGXI_TEST_SHARD_COUNT")
    if index_raw is None and count_raw is None:
        return None
    if index_raw is None or count_raw is None:
        raise ShardEnvironmentError(
            "LINGXI_TEST_SHARD_INDEX 与 LINGXI_TEST_SHARD_COUNT 必须同时设置或同时不设置，"
            f"当前 INDEX={index_raw!r} COUNT={count_raw!r}。"
        )
    try:
        shard_index, shard_count = int(index_raw), int(count_raw)
    except ValueError as error:
        raise ShardEnvironmentError(f"分片环境变量必须是整数：{error}") from error
    if shard_count < 1 or not (0 <= shard_index < shard_count):
        raise ShardEnvironmentError(
            f"分片参数不合法：INDEX={shard_index} COUNT={shard_count}，"
            "要求 COUNT>=1 且 0<=INDEX<COUNT。"
        )
    return shard_index, shard_count


def _last_meaningful_line(traceback_text: str) -> str:
    """从 unittest 已经格式化好的 traceback 字符串里取最后一个非空行。

    `TestResult.addFailure`/`addError` 存的不是原始 `exc_info`，而是已经过
    `_exc_info_to_string` 格式化的完整 traceback 文本；最后一行通常形如
    `ExceptionType: message`，是这里能拿到的、跨机器最稳定的失败摘要——不含
    行号、对象地址这些一换机器就变的内容。
    """

    lines = [line for line in traceback_text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _outcome_records(
    all_ids: list[str], result: unittest.TestResult
) -> list[dict[str, str | None]]:
    """按"发现顺序"逐条给出结论——不是只统计通过/失败/跳过各多少条。

    直接读 `TestResult` 收集的四张表而不是自定义 `TestResult` 子类：后者需要
    覆写 `addSuccess`/`addFailure` 等驼峰命名的基类方法，与本仓库 `N802`
    规则冲突（历史同类冲突见 `pyproject.toml` 里另外两个测试文件的豁免）；
    读现成的表既不用碰 `pyproject.toml`，产出的记录也完全一样。
    """

    failed = {test.id(): _last_meaningful_line(text) for test, text in result.failures}
    errored = {test.id(): _last_meaningful_line(text) for test, text in result.errors}
    expected_failed = {
        test.id(): _last_meaningful_line(text) for test, text in result.expectedFailures
    }
    unexpectedly_ok = {test.id() for test in result.unexpectedSuccesses}
    skipped = {test.id(): reason for test, reason in result.skipped}

    records: list[dict[str, str | None]] = []
    for test_id in all_ids:
        if test_id in failed:
            records.append({"id": test_id, "outcome": "fail", "signature": failed[test_id]})
        elif test_id in errored:
            records.append({"id": test_id, "outcome": "error", "signature": errored[test_id]})
        elif test_id in expected_failed:
            records.append(
                {
                    "id": test_id,
                    "outcome": "expected_failure",
                    "signature": expected_failed[test_id],
                }
            )
        elif test_id in unexpectedly_ok:
            records.append({"id": test_id, "outcome": "unexpected_success", "signature": None})
        elif test_id in skipped:
            records.append({"id": test_id, "outcome": "skip", "signature": skipped[test_id]})
        else:
            records.append({"id": test_id, "outcome": "pass", "signature": None})
    return records


def _write_report(
    path: str, shard: tuple[int, int] | None, all_ids: list[str], result: unittest.TestResult
) -> None:
    payload = {
        "shard_index": shard[0] if shard else None,
        "shard_count": shard[1] if shard else None,
        "tests_run": result.testsRun,
        "records": _outcome_records(all_ids, result),
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run() -> int:
    shard = _read_shard_env()
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS_DIRECTORY))
    if shard is not None:
        suite = _select_shard(suite, shard[0], shard[1])
    all_ids = [case.id() for case in _iter_test_cases(suite)]

    durations_output = _duration_output_path(shard)
    timing: dict[str, float] = {}
    if durations_output is not None:
        timing = _record_case_durations(list(_iter_test_cases(suite)))

    # `python -m unittest` 的默认是 warnings='default'；TextTestRunner 不传这个
    # 参数时默认是 None，于是 DeprecationWarning / ResourceWarning 不再打印。
    # 二审实测：同一夹具旧路径打印 4 行警告、新路径 0 行。退出码虽不受影响，但
    # 「回退路径与改造前逐字相同」是本次改造能合并的前提，少打印的警告同样是
    # 可观察差异，必须补齐。
    runner = unittest.TextTestRunner(verbosity=2, warnings="default")
    result = runner.run(suite)

    report_path = os.environ.get("LINGXI_TEST_REPORT_PATH")
    if report_path:
        _write_report(report_path, shard, all_ids, result)

    if durations_output is not None:
        durations_output.parent.mkdir(parents=True, exist_ok=True)
        durations_output.write_text(
            json.dumps(
                {key: round(value, 3) for key, value in sorted(timing.items())},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if shard is not None:
        shard_index, shard_count = shard
        sys.stderr.write(
            f"{_SHARD_SUMMARY_PREFIX} index={shard_index} count={shard_count} "
            f"ran={result.testsRun} ok={'true' if result.wasSuccessful() else 'false'}\n"
        )

    return 0 if result.wasSuccessful() else 1


def _parse_shard_summaries(text: str) -> list[dict[str, int | bool]]:
    summaries: list[dict[str, int | bool]] = []
    for line in text.splitlines():
        if not line.startswith(_SHARD_SUMMARY_PREFIX):
            continue
        fields = dict(item.split("=", 1) for item in line.split()[1:])
        summaries.append(
            {
                "index": int(fields["index"]),
                "count": int(fields["count"]),
                "ran": int(fields["ran"]),
                "ok": fields["ok"] == "true",
            }
        )
    return summaries


def verify_shard_logs(log_paths: list[Path], expected_count: int) -> list[str]:
    """核对一批分片日志：每个分片号 0..expected_count-1 都恰好产出一次汇总行。

    这本身就是一道门禁（必做项第 3 条）：分片后 `Ran N` 会碎成 N 行，如果汇总
    环节能悄悄漏掉一片，"N 不减少"这条验收标准自己就没法核——历史上真的发生过
    `Ran` 行被管道 grep 滤掉（`docs/traces/630-清仓批一/任务表.md:37`）。
    """

    problems: list[str] = []
    if len(log_paths) != expected_count:
        problems.append(f"分片日志数量是 {len(log_paths)}，与期望的分片数 {expected_count} 不符。")

    seen: dict[int, int] = {}
    ran_by_index: dict[int, int] = {}
    for path in log_paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        summaries = _parse_shard_summaries(text)
        if not summaries:
            problems.append(f"{path}：没有找到 {_SHARD_SUMMARY_PREFIX} 汇总行，该分片可能没跑完。")
            continue
        for summary in summaries:
            seen[summary["index"]] = seen.get(summary["index"], 0) + 1
            ran_by_index[summary["index"]] = summary["ran"]

    # 独立审查 P1-2：此前这里解析了 `ran=` 却从不使用，而上面的说明宣称自己守的
    # 是「N 不减少」——某一片 `ran=0` 照样判绿，正是它要挡的那种漏跑。汇总行齐全
    # 只证明「每片都留下了脚印」，不证明「每片真的跑到了用例」。
    for index, ran in sorted(ran_by_index.items()):
        if ran <= 0:
            problems.append(
                f"分片 {index} 的汇总行写着 ran={ran}：这一片一条用例都没跑到。"
                "分片划分出了空桶，或者发现阶段被改坏了——两者都会让总数悄悄变少。"
            )
    total_ran = sum(ran_by_index.values())
    if ran_by_index and total_ran <= 0:
        problems.append(f"全部分片合计只跑了 {total_ran} 条用例，这不可能是一次真实跑批。")

    for index in range(expected_count):
        occurrences = seen.get(index, 0)
        if occurrences == 0:
            problems.append(f"分片 {index} 没有任何日志产出汇总行。")
        elif occurrences > 1:
            problems.append(f"分片 {index} 的汇总行出现了 {occurrences} 次，期望恰好 1 次。")
    unexpected = sorted(set(seen) - set(range(expected_count)))
    if unexpected:
        problems.append(f"出现了超出期望分片数范围的分片号：{unexpected}。")
    return problems


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-shard-logs",
        type=int,
        metavar="COUNT",
        help="核对模式：不跑测试，核对 COUNT 份分片日志是否每片都产出了汇总行",
    )
    parser.add_argument("log_paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    if args.check_shard_logs is None:
        if args.log_paths:
            parser.error("不带 --check-shard-logs 时不接受位置参数")
        return _run()

    problems = verify_shard_logs(args.log_paths, args.check_shard_logs)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"分片汇总核对：{args.check_shard_logs} 个分片全部产出且不重复", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main(sys.argv[1:]))
    except ShardEnvironmentError as error:
        print(f"run_tests：{error}", file=sys.stderr)
        raise SystemExit(1) from error
