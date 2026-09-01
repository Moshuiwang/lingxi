#!/usr/bin/env python3
"""准备 L3 权限影响用户计数证明。

CI 不能连接 biai-stage、读取业务凭据或调用公司系统。本脚本只做两件事：权限面为空
时根据当前两个 Git ref 的事实集合差确定性生成 0；权限面非空时读取 PR 中由
biai-stage 生成的纯计数声明，并要求 trusted-base GitHub OWNER reader 生成的仓库外
provenance 绑定两份权限事实和 grant/shrink 面摘要。PR 自带的声明不是可信 stage
证据，缺少 OWNER attestation 时响亮失败，避免把自报或猜测的 0 伪装成影响面证据。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / ".github" / "permission-impact-counts.json"


def _load_impact_module():
    path = Path(__file__).with_name("check_permission_impact.py")
    spec = importlib.util.spec_from_file_location("permission_impact_for_prepare", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载权限影响面门禁：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


IMPACT = _load_impact_module()


def _head_timestamp(repository: Path, head_ref: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "show", "-s", "--format=%cI", head_ref],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("无法读取候选 head 的提交时间，不能生成计数来源元数据")
    value = result.stdout.strip()
    # 让输入通过同一带时区校验；这里不把 Git 错误全文回显到报告中。
    IMPACT._validate_timestamp(value)
    return value


def _read_json(path: Path) -> dict[str, Any]:
    return IMPACT._load_json_document(path)


def prepare(
    base_ref: str,
    head_ref: str,
    *,
    repository: Path = REPOSITORY_ROOT,
    manifest: Path = MANIFEST_PATH,
    trusted_provenance: Path | None = None,
    output: Path,
) -> dict[str, Any]:
    (
        base_roles,
        base_metrics,
        base_role_raw,
        base_metric_raw,
    ) = IMPACT._load_ref_documents_with_raw(repository, base_ref)
    (
        head_roles,
        head_metrics,
        head_role_raw,
        head_metric_raw,
    ) = IMPACT._load_ref_documents_with_raw(repository, head_ref)
    base_surface = IMPACT.build_surface(
        IMPACT._role_map(base_roles, "base 角色映射"),
        IMPACT._metric_map(base_metrics, "base 公司指标映射"),
    )
    head_surface = IMPACT.build_surface(
        IMPACT._role_map(head_roles, "head 角色映射"),
        IMPACT._metric_map(head_metrics, "head 公司指标映射"),
    )
    grant_surface = head_surface - base_surface
    shrink_surface = base_surface - head_surface
    base_facts_sha256 = IMPACT._facts_digest(base_role_raw, base_metric_raw)
    head_facts_sha256 = IMPACT._facts_digest(head_role_raw, head_metric_raw)

    if not grant_surface and not shrink_surface:
        # 空 diff 的 0 来自两个已提交 ref 的集合差，不读取任何外部数据，也不接受
        # PR 自带的数字覆盖。这样不会把一个伪造的「0」当成真实影响面证据。
        evidence = {
            "schema": IMPACT.COUNT_SCHEMA,
            "base_facts_sha256": base_facts_sha256,
            "head_facts_sha256": head_facts_sha256,
            "grant_surface_sha256": IMPACT._surface_digest(grant_surface),
            "shrink_surface_sha256": IMPACT._surface_digest(shrink_surface),
            "counts": {"grant": 0, "shrink": 0},
            "source": {
                "kind": IMPACT.EMPTY_COUNT_SOURCE,
                "environment": "repository",
                "dataset": "permission-facts",
                "query_version": "static-diff/v1",
                "captured_at": _head_timestamp(repository, head_ref),
            },
        }
    else:
        if manifest.is_symlink() or not manifest.is_file():
            raise IMPACT.CountEvidenceError(
                "权限事实发生变化但缺少 biai-stage 只读聚合清单；"
                "CI 不读取业务凭据或调用公司系统"
            )
        evidence = _read_json(manifest)
        validated = IMPACT._validate_count_manifest(
            evidence,
            expected_base_facts_sha256=base_facts_sha256,
            expected_head_facts_sha256=head_facts_sha256,
            expected_grant_surface_sha256=IMPACT._surface_digest(grant_surface),
            expected_shrink_surface_sha256=IMPACT._surface_digest(shrink_surface),
        )
        if validated["source"]["kind"] != IMPACT.STAGE_COUNT_CLAIM_SOURCE:
            raise IMPACT.CountEvidenceError(
                "非空权限影响面只能使用 biai-stage 只读聚合声明；"
                "不能用静态 0 或其他自报来源替代"
            )
        if trusted_provenance is None:
            raise IMPACT.CountEvidenceError(
                "PR 内 biai-stage 聚合只是未验证声明；缺少 GitHub OWNER attestation"
            )
        IMPACT._validate_stage_provenance(
            IMPACT._load_external_provenance(trusted_provenance, repository=repository),
            manifest=evidence,
            expected_base_facts_sha256=base_facts_sha256,
            expected_head_facts_sha256=head_facts_sha256,
            expected_grant_surface_sha256=IMPACT._surface_digest(grant_surface),
            expected_shrink_surface_sha256=IMPACT._surface_digest(shrink_surface),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--trusted-provenance",
        type=Path,
        help=(
            "trusted-base reader 生成的 GitHub OWNER provenance；PR 内清单只是 claim，"
            "没有该文件时非空权限面失败关闭"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = prepare(
            args.base_ref,
            args.head_ref,
            repository=args.repository,
            manifest=args.manifest,
            trusted_provenance=args.trusted_provenance,
            output=args.output,
        )
    except (IMPACT.ConfigShapeError, IMPACT.CountEvidenceError, RuntimeError, ValueError) as error:
        print(f"L3 权限影响面计数证明失败关闭：{error}", file=sys.stderr)
        return 1
    print(
        "L3 权限影响面计数证明：通过 "
        f"source={evidence['source']['kind']} "
        f"grant={evidence['counts']['grant']} shrink={evidence['counts']['shrink']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
