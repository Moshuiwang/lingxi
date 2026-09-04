#!/usr/bin/env python3
"""写出 Epic Full 的最小候选证明；不联网、不持有写权限。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")


def candidate_document(
    *, repository: str, pr_number: int, head_sha: str, tested_sha: str, tree_sha: str, run_id: int
) -> dict[str, object]:
    if repository.count("/") != 1:
        raise ValueError("repository 必须是 owner/name")
    if pr_number <= 0 or run_id <= 0:
        raise ValueError("pr_number 与 run_id 必须为正整数")
    for label, value in (("head_sha", head_sha), ("tested_sha", tested_sha), ("tree_sha", tree_sha)):
        if not SHA.fullmatch(value):
            raise ValueError(f"{label} 不是 40 位小写 Git SHA")
    return {
        "schema": 1,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "tested_sha": tested_sha,
        "tree_sha": tree_sha,
        "run_id": run_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--tree-sha", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    document = candidate_document(
        repository=args.repository,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        tested_sha=args.tested_sha,
        tree_sha=args.tree_sha,
        run_id=args.run_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Epic 候选证明：PR #{args.pr_number}，tree={args.tree_sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
