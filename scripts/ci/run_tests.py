#!/usr/bin/env python3
"""按分片跑 `tests/` 下的单元测试（Issue #712）。

`scripts/ci/verify_repository.sh` 此前直接调用
``PYTHONPATH=src python3 -m unittest discover -s tests -v``。把这一行换成调用
本脚本，是为了让"按文件分片只跑一部分测试"与"不分片、跑全部测试"共用同一套
判定逻辑与同一种输出格式——下游比对新旧两条路径时不用适配两套输出。

分片依据 ``LINGXI_TEST_SHARD_INDEX`` / ``LINGXI_TEST_SHARD_COUNT`` 两个环境
变量，按**测试文件**的绝对路径稳定哈希分桶，不按单条用例分桶：一是不拆散同一个
``TestCase`` 类共享的 ``setUpClass`` 状态，二是与 ``tests/postgres_schema.py``
按 DSN 缓存表结构的策略天然兼容——同一分片全程只连一个 DSN，缓存正好命中一次。
用 ``hashlib`` 而不是内置 ``hash()``：后者受 ``PYTHONHASHSEED`` 影响，同一个
文件路径在不同进程里可能算出不同桶，会让分片结果不确定。

两个环境变量任一缺失都视为"不分片"：发现全部测试、原样跑完，不额外打印任何
分片标记行——这正是改造前 ``unittest discover -s tests -v`` 的行为，是本脚本
必须留的回退路径，不是遗漏。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"

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
    """一条用例所在的源文件绝对路径，作为分片哈希的输入。

    模块导入失败时 unittest 会造一个占位 `_FailedTest`，它的 `__module__`
    指向 `unittest.loader` 本身而不是真正失败的那个文件——这种情况下退回
    用 `case.id()`（其中包含失败模块名）分桶：不如按文件分桶精确，但仍然
    稳定、仍然会被跑到、仍然会在失败时把对应分片染红，不会被静默漏掉。
    """

    module = sys.modules.get(type(case).__module__)
    module_file = getattr(module, "__file__", None) if module else None
    if not module_file:
        return case.id()
    return str(Path(module_file).resolve())


def _shard_bucket(key: str, shard_count: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % shard_count


def _select_shard(
    suite: unittest.TestSuite, shard_index: int, shard_count: int
) -> unittest.TestSuite:
    selected = unittest.TestSuite()
    for case in _iter_test_cases(suite):
        if _shard_bucket(_case_source_file(case), shard_count) == shard_index:
            selected.addTest(case)
    return selected


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

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    report_path = os.environ.get("LINGXI_TEST_REPORT_PATH")
    if report_path:
        _write_report(report_path, shard, all_ids, result)

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
    for path in log_paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        summaries = _parse_shard_summaries(text)
        if not summaries:
            problems.append(f"{path}：没有找到 {_SHARD_SUMMARY_PREFIX} 汇总行，该分片可能没跑完。")
            continue
        for summary in summaries:
            seen[summary["index"]] = seen.get(summary["index"], 0) + 1

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
