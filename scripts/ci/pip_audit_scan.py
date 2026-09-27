#!/usr/bin/env python3
"""依赖漏洞扫描的调用处：跑钉版扫描器，只对漏洞库服务端故障重试（#874）。

用法：pip_audit_scan.py --output 结果.json --label 组名 -- <扫描器命令及参数>
（本脚本自己在命令末尾追加 ``--output 结果.json``，唤起方不要重复给）。

「查到了漏洞」与「没查成」是两种红，只对后者重试：
- 扫描器写出了完整结果（顶层有 dependencies 列表）→ 不论退出码是 0 还是 1（发现漏洞）都不重试，
  结论交给判定脚本 pip_audit_check.py；真高危不可能被重试掩盖。
- 没有完整结果且错误输出带服务端错误特征（5xx、ServiceError、连接 / 读取超时）且不带客户端
  错误（4xx）特征 → 最多重试 2 次、间隔递增；每次重试的序号与间隔都打到日志。
- 重试用尽仍失败 → 删除残留结果文件、退出码 2，信息写明「未能取得严重度（外部服务不可用），
  非漏洞判定」；不降级为告警、不放行。
- 其他失败（结果缺失但不是服务端故障）→ 不重试、退出码 0，由判定脚本按「未知」判失败，
  与引入本脚本之前的行为一致。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_UNAVAILABLE = 2
MAX_RETRIES = 2
DEFAULT_DELAY_BASE = 10.0
UNAVAILABLE = "未能取得严重度（外部服务不可用），非漏洞判定"

_SERVICE_ERROR = re.compile(
    r"\b5\d\d Server Error\b|ServiceError|ConnectionError|ConnectTimeout|ReadTimeout"
    r"|Read timed out|timed out|Max retries exceeded"
)
_CLIENT_ERROR = re.compile(r"\b4\d\d Client Error\b")


def retry_delays(base: float) -> list[float]:
    """每次重试前等待的秒数：base、3×base，递增；次数固定为 MAX_RETRIES。"""
    return [base * factor for factor in (1, 3)][:MAX_RETRIES]


def _has_complete_result(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("dependencies"), list)


def _is_service_error(stderr: str) -> bool:
    return bool(_SERVICE_ERROR.search(stderr)) and not _CLIENT_ERROR.search(stderr)


def _emit_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)


def run_scan(command: list[str], output: Path, label: str, delay_base: float) -> int:
    delays = retry_delays(delay_base)
    retries = 0
    while True:
        output.unlink(missing_ok=True)
        attempt = retries + 1
        print(f"[{label}] 扫描第 {attempt} 次调用：{' '.join(command)} --output {output}", flush=True)
        result = subprocess.run(
            [*command, "--output", str(output)], capture_output=True, text=True, check=False
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if _has_complete_result(output):
            print(
                f"[{label}] 扫描器退出码 {result.returncode}（0 = 无漏洞；1 = 发现漏洞），结果完整；"
                f"共重试 {retries} 次；结论由判定脚本给出",
                flush=True,
            )
            return EXIT_OK
        if not _is_service_error(result.stderr):
            print(
                f"[{label}] 扫描器退出码 {result.returncode}，没有完整结果且不是服务端故障，不重试；"
                f"共重试 {retries} 次；由判定脚本按「未知」判失败",
                flush=True,
            )
            return EXIT_OK
        if retries >= len(delays):
            break
        delay = delays[retries]
        retries += 1
        print(
            f"[{label}] 漏洞库服务端故障（退出码 {result.returncode}），第 {retries} 次重试，"
            f"间隔 {delay:g} 秒（最多 {MAX_RETRIES} 次）",
            flush=True,
        )
        time.sleep(delay)
    output.unlink(missing_ok=True)
    message = (
        f"## 依赖漏洞扫描：{label} —— 判失败（退出码 {EXIT_UNAVAILABLE}）\n\n"
        f"{UNAVAILABLE}：漏洞库服务端连续故障，共重试 {retries} 次仍失败"
        f"（间隔 {'、'.join(f'{d:g}' for d in delays)} 秒）。\n"
    )
    sys.stdout.write(message)
    _emit_summary(message)
    return EXIT_UNAVAILABLE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True, type=Path, help="扫描结果 JSON 的写出路径")
    parser.add_argument("--label", default="依赖漏洞扫描", help="日志与摘要里的标签，如 extra 名")
    parser.add_argument(
        "--retry-delay-base",
        type=float,
        default=DEFAULT_DELAY_BASE,
        help="首次重试前的等待秒数，第二次为其 3 倍（测试传 0）",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后的扫描器命令")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("缺少 -- 之后的扫描器命令")
    if "--output" in command:
        parser.error("扫描器命令里不要带 --output，由本脚本追加")
    return run_scan(command, args.output, args.label, args.retry_delay_base)


if __name__ == "__main__":
    sys.exit(main())
