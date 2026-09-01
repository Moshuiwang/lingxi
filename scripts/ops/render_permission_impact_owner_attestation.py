#!/usr/bin/env python3
"""Render the exact JSON body for a GitHub OWNER permission-impact comment.

Run this from the trusted biai-stage/exporter workspace after the count manifest
has been produced.  The output is a raw JSON document suitable for the GitHub
comment composer (no Markdown fence or prose).  The CI reader still revalidates
every field against the PR and official API response; this helper does not sign,
post, or persist any GitHub state.

Example::

    python3 scripts/ops/render_permission_impact_owner_attestation.py \
      --manifest .github/permission-impact-counts.json \
      --pr-number 518 --base-sha <base-sha> --head-sha <head-sha> \
      --exporter-commit <exporter-commit> --exporter-blob <exporter-blob> \
      --output /tmp/permission-impact-owner-attestation.json

Then paste the file as one ordinary, unedited GitHub PR comment.  A later
``pull_request_target`` run reads it back; the comment must be created after
``issued_at`` and before ``expires_at``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import secrets
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
READER_PATH = REPOSITORY_ROOT / "scripts" / "ci" / "read_github_owner_attestation.py"


def _load_reader():
    spec = importlib.util.spec_from_file_location("owner_attestation_reader_for_renderer", READER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 OWNER attestation reader：{READER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


READER = _load_reader()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat()


def render(
    *,
    manifest_path: Path,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    exporter: dict[str, str],
    issued_at: str | None = None,
    expires_at: str | None = None,
    nonce: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    manifest, manifest_sha256 = READER._load_manifest(manifest_path)
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise READER.AttestationError("PR 编号必须是正整数")
    READER._sha1(base_sha, "base SHA")
    READER._sha1(head_sha, "head SHA")
    normalized_exporter = READER._validate_exporter(exporter)
    current = (now or _now()).astimezone(dt.timezone.utc)
    source = READER._mapping(manifest["source"], "manifest source")
    captured_at = source["captured_at"]
    issued = issued_at or _iso(current)
    if expires_at is None:
        _, issued_moment = READER._timestamp(issued, "issued_at")
        expires = _iso(issued_moment + dt.timedelta(seconds=READER.MAX_ATTESTATION_TTL_SECONDS))
    else:
        expires = expires_at
    body: dict[str, Any] = {
        "schema": READER.ATTESTATION_SCHEMA,
        "repository": {
            "id": READER.REPOSITORY_ID,
            "full_name": READER.REPOSITORY_FULL_NAME,
        },
        "pull_request": {
            "number": pr_number,
            "base_sha": base_sha,
            "head_sha": head_sha,
        },
        "manifest_sha256": manifest_sha256,
        "base_facts_sha256": manifest["base_facts_sha256"],
        "head_facts_sha256": manifest["head_facts_sha256"],
        "grant_surface_sha256": manifest["grant_surface_sha256"],
        "shrink_surface_sha256": manifest["shrink_surface_sha256"],
        "counts": dict(manifest["counts"]),
        "exporter": normalized_exporter,
        "query_version": source["query_version"],
        "captured_at": captured_at,
        "issued_at": issued,
        "expires_at": expires,
        "nonce": nonce or secrets.token_urlsafe(24),
    }
    # Validate as if the comment were created at issued_at.  The reader later
    # replaces that synthetic timestamp with GitHub's immutable created_at.
    READER.validate_attestation(
        body,
        repository_id=READER.REPOSITORY_ID,
        repository_full_name=READER.REPOSITORY_FULL_NAME,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        comment_created_at=issued,
        now=current,
    )
    return body


def _write(path: Path, body: dict[str, Any]) -> None:
    if path.is_symlink():
        raise READER.AttestationError("输出路径不得是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    exporter = parser.add_mutually_exclusive_group(required=True)
    exporter.add_argument("--exporter-commit")
    exporter.add_argument("--image-digest")
    parser.add_argument("--exporter-blob")
    parser.add_argument("--issued-at")
    parser.add_argument("--expires-at")
    parser.add_argument("--nonce")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.exporter_commit is not None:
            if args.exporter_blob is None:
                raise READER.AttestationError("--exporter-commit 必须与 --exporter-blob 成对提供")
            exporter_value = {"commit": args.exporter_commit, "blob": args.exporter_blob}
        else:
            if args.exporter_blob is not None:
                raise READER.AttestationError("--exporter-blob 只能与 --exporter-commit 一起提供")
            exporter_value = {"image_digest": args.image_digest}
        body = render(
            manifest_path=args.manifest,
            pr_number=args.pr_number,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            exporter=exporter_value,
            issued_at=args.issued_at,
            expires_at=args.expires_at,
            nonce=args.nonce,
        )
        _write(args.output, body)
    except (READER.AttestationError, RuntimeError, OSError, ValueError) as error:
        print(f"OWNER attestation payload 生成失败关闭：{error}", file=sys.stderr)
        return 1
    print(f"OWNER attestation payload：已生成 {args.output}（未提交 GitHub）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
