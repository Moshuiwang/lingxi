#!/usr/bin/env python3
"""把构建好的镜像推到 GHCR；权限不足时**显式降级**为上传 artifact（Issue #62 / S11）。

2026-08-06 产品负责人决策（Issue #62 决策登记）：镜像仓库为 GHCR
（`ghcr.io/moshuiwang/…`），CI 用 Actions 原生 `GITHUB_TOKEN` 推送，不新增外部账号凭据。
机器人 GitHub App 的 packages 写权限**在实施时核实**：不足则先降级为「只构建不推送、
产物以 CI artifact 留证」，并回报产品负责人开通权限，**不得引入个人凭据**。

这个脚本就是那条决策的执行体。它的全部难点在于：降级必须是**显式分支**，不是吞错。

    为什么不用 `continue-on-error` 或 `|| true`：那样任何失败都变成绿色，
    于是「推上去了」和「没推上去」在 CI 上长得一模一样。等到有人真的去 `biai-stage`
    拉那个 tag 时才发现镜像不存在——而那时他手里拿的是一份写着"通过"的 CI 记录。
    降级是一个需要被看见的状态，不是一个可以被忽略的失败。

因此本脚本把失败**分类**：

  - 权限/认证类失败  → 降级：保存 tar、写明"未推送 GHCR：权限不足"、发 ::warning::、
                       退出 0（这是已被产品负责人预先授权的降级路径）。
  - 其他一切失败      → 不降级：原样报错并退出非 0（网络抖动、磁盘满、manifest 非法
                       都不是"权限不足"，把它们一起降级会掩盖真正的问题）。

成功路径的 digest 是**独立回读**的：推送完另起一次 `docker manifest inspect` 向仓库
要 digest，而不是从 push 的输出里抠。复述构建输出证明不了"仓库里真的有这个镜像"。

用法：

    python3 scripts/ci/push_image.py ghcr.io/moshuiwang/lingxi-scheduler:20260806-abc123def456 \\
        --artifact-dir dist --summary-file "$GITHUB_STEP_SUMMARY" --output-file "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

# 权限/认证类失败的判据。取自 registry v2 规范的错误码与 GHCR 的实际回话措辞。
#
# 刻意写得**窄**：宁可把一个真正的权限问题误判成"其他失败"（结果是 job 变红，有人来看），
# 也不要把一个网络问题误判成"权限不足"（结果是 job 变绿，没人来看）。
# 误判方向的选择本身就是这条断言的内容。
PERMISSION_PATTERNS = (
    r"\bdenied\b",
    r"\bunauthorized\b",
    r"insufficient_scope",
    r"authentication required",
    r"\b403\b",
    r"\b401\b",
    r"permission_denied",
    r"installation not allowed",
)

PERMISSION = "permission"
OTHER = "other"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_command(argv: list[str]) -> CommandResult:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def classify_push_failure(output: str) -> str:
    """把一次推送失败分成「权限不足」与「其他」。

    只看措辞，不看退出码：`docker push` 对所有失败都返回 1。
    """

    lowered = output.lower()
    for pattern in PERMISSION_PATTERNS:
        if re.search(pattern, lowered):
            return PERMISSION
    return OTHER


def read_back_digest(reference: str, runner=run_command) -> str | None:
    """向**镜像仓库**要 digest，而不是复述 push 的输出。

    `docker manifest inspect` 会真的去拉一次 manifest：它回答的是"仓库里现在有没有
    这个引用"，而 push 的 stdout 只回答"客户端认为自己发完了"。断言 M2-62-34 要的是前者。
    """

    result = runner(["docker", "manifest", "inspect", "--verbose", reference])
    if result.returncode != 0:
        return None
    match = re.search(r'"digest"\s*:\s*"(sha256:[0-9a-f]{64})"', result.stdout)
    return match.group(1) if match else None


def write_line(path: pathlib.Path | None, text: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", help="完整镜像引用，例如 ghcr.io/moshuiwang/lingxi-scheduler:20260806-abc123def456")
    parser.add_argument("--artifact-dir", type=pathlib.Path, help="降级时把镜像 tar 存到这里")
    parser.add_argument("--summary-file", type=pathlib.Path, help="GitHub Step Summary 文件")
    parser.add_argument("--output-file", type=pathlib.Path, help="GitHub Actions 的 GITHUB_OUTPUT 文件")
    args = parser.parse_args()

    reference = args.reference
    print(f"推送 {reference}")
    result = run_command(["docker", "push", reference])
    combined = f"{result.stdout}\n{result.stderr}"

    # ---- 成功路径 ---------------------------------------------------------
    if result.returncode == 0:
        digest = read_back_digest(reference)
        if digest is None:
            # 推送自称成功，但仓库里读不回来。这**不是**降级情形——它意味着我们对
            # "镜像已发布"的判断没有依据，必须红。
            print(
                f"推送自称成功，但从仓库回读 {reference} 的 digest 失败。"
                "无法证明镜像真的在仓库里，不接受这种收口。",
                file=sys.stderr,
            )
            return 1
        print(f"已推送并回读 digest：{digest}")
        write_line(args.output_file, "pushed=true")
        write_line(args.output_file, f"digest={digest}")
        write_line(
            args.summary_file,
            f"### 镜像已推送 GHCR\n\n"
            f"- 引用：`{reference}`\n"
            f"- digest（独立回读自仓库，非复述构建输出）：`{digest}`\n"
            f"- 部署时按 digest 固定即可完全不可变。\n",
        )
        return 0

    # ---- 失败：分类 -------------------------------------------------------
    kind = classify_push_failure(combined)

    if kind is not PERMISSION:
        print(f"推送 {reference} 失败，且不是权限问题——不降级。", file=sys.stderr)
        print(combined.strip(), file=sys.stderr)
        write_line(args.output_file, "pushed=false")
        write_line(
            args.summary_file,
            f"### 镜像推送失败（非权限问题，未降级）\n\n"
            f"- 引用：`{reference}`\n"
            f"- 这类失败不在产品负责人预先授权的降级范围内，因此 job 判红。\n",
        )
        return 1

    # ---- 降级路径：权限不足 -----------------------------------------------
    print(f"::warning::未推送 GHCR：权限不足。{reference} 只作为 CI artifact 留证。")
    print("这是 Issue #62 决策登记里预先授权的降级路径，不是失败被吞掉。", file=sys.stderr)
    print(combined.strip(), file=sys.stderr)

    saved_to = None
    if args.artifact_dir is not None:
        args.artifact_dir.mkdir(parents=True, exist_ok=True)
        safe_name = reference.replace("/", "_").replace(":", "_") + ".tar"
        saved_to = args.artifact_dir / safe_name
        save = run_command(["docker", "save", "-o", str(saved_to), reference])
        if save.returncode != 0:
            print(f"降级路径本身失败：docker save 未成功\n{save.stderr}", file=sys.stderr)
            return 1
        print(f"已保存镜像 tar：{saved_to}")

    write_line(args.output_file, "pushed=false")
    write_line(args.output_file, "degraded=permission")
    write_line(
        args.summary_file,
        "### 未推送 GHCR：权限不足（已降级为 CI artifact）\n\n"
        f"- 引用：`{reference}`\n"
        f"- 产物：`{saved_to if saved_to else '（未指定 --artifact-dir，未保存）'}`\n"
        "- **需要产品负责人操作**：为机器人 GitHub App 开通 packages 写权限，"
        "然后重跑本 job。在此之前镜像只存在于 CI artifact，`biai-stage` 无法按 tag 拉取。\n"
        "- 依据：Issue #62 决策登记（2026-08-06）——权限不足时降级留证并回报，"
        "**不得引入个人凭据**。\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
