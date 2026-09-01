#!/usr/bin/env python3
"""在 biai-stage 生成 L3 权限影响用户计数证明。

这是受控验收环境的只读导出工具，不属于 CI：它只执行 ``COUNT(DISTINCT user_id)``
聚合，不把用户标识、邮箱、角色分组明细或数据库凭据写入输出。生成的 JSON 只是
可随权限配置 PR 提交的 stage 声明（claim）；CI 只有在 trusted-base GitHub OWNER
reader 验证 PM 的一次性评论后才采信，CI 本身不会连接数据库或公司系统。

调用示例（在 biai-stage 的受控工作目录执行，DSN 仅从环境变量读取）：

    PYTHONPATH=src python3 scripts/ops/export_permission_impact_counts.py \
      --base-ref <base-sha> --head-ref <head-sha> \
      --output .github/permission-impact-counts.json

随后在 trusted stage 使用 ``scripts/ops/render_permission_impact_owner_attestation.py``
生成 OWNER 要发布的 raw JSON 正文；该正文不能由 PR runner 自行生成或代发。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DSN_ENV = "LINGXI_POSTGRES_DSN"


def _load_impact_module():
    path = REPOSITORY_ROOT / "scripts" / "ci" / "check_permission_impact.py"
    spec = importlib.util.spec_from_file_location("permission_impact_for_export", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载权限影响面门禁")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


IMPACT = _load_impact_module()


def _facts(repository: Path, ref: str) -> tuple[dict[str, str], dict[str, dict[str, tuple[str, ...]]], bytes, bytes]:
    roles, metrics, role_raw, metric_raw = IMPACT._load_ref_documents_with_raw(repository, ref)
    return (
        IMPACT._role_map(roles, f"{ref} 角色映射"),
        IMPACT._metric_map(metrics, f"{ref} 公司指标映射"),
        role_raw,
        metric_raw,
    )


def _role_names(entries: set[tuple[str, str, str, str]]) -> list[str]:
    return sorted({entry[0] for entry in entries})


def _read_stage_counts(
    dsn: str,
    *,
    grant_roles: list[str],
    shrink_roles: list[str],
) -> tuple[int, int, str]:
    try:
        from lingxi.adapters.postgres import connect
    except ImportError as error:
        raise RuntimeError("biai-stage 缺少 Lingxi PostgreSQL 连接工厂，未生成计数证明") from error

    try:
        with connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '3s'")
                cursor.execute(
                    """
                    SELECT completed_at
                      FROM galaxy_import_batch
                     WHERE status = 'complete' AND expires_at > now()
                     ORDER BY completed_at DESC NULLS LAST, id DESC
                     LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row or not isinstance(row[0], datetime):
                    raise RuntimeError("biai-stage 没有可用的 complete 权限快照")
                captured_at = row[0].isoformat()

                query = """
                    WITH latest_batch AS (
                        SELECT id
                          FROM galaxy_import_batch
                         WHERE status = 'complete' AND expires_at > now()
                         ORDER BY completed_at DESC NULLS LAST, id DESC
                         LIMIT 1
                    )
                    SELECT count(DISTINCT role.user_id)
                      FROM galaxy_user_role AS role
                     JOIN latest_batch AS batch ON batch.id = role.batch_id
                     WHERE role.role_name = ANY(%s::text[])
                """
                cursor.execute(query, (grant_roles,))
                grant_row = cursor.fetchone()
                cursor.execute(query, (shrink_roles,))
                shrink_row = cursor.fetchone()
                if not grant_row or not shrink_row:
                    raise RuntimeError("biai-stage 聚合未返回计数")
                grant_count, shrink_count = grant_row[0], shrink_row[0]
                if not isinstance(grant_count, int) or not isinstance(shrink_count, int):
                    raise RuntimeError("biai-stage 聚合返回了非整数结果")
                return grant_count, shrink_count, captured_at
    except RuntimeError:
        raise
    except Exception as error:
        # 不把驱动异常全文打印出去：某些连接错误会回显 DSN 或主机细节。
        raise RuntimeError("biai-stage 只读聚合失败，未生成计数证明") from error


def export(
    base_ref: str,
    head_ref: str,
    *,
    repository: Path = REPOSITORY_ROOT,
    output: Path,
    provenance_output: Path | None = None,
) -> dict[str, Any]:
    if provenance_output is not None:
        raise IMPACT.CountEvidenceError(
            "stage exporter 不生成 GitHub OWNER provenance；请把本清单交给 PM，"
            "由 trusted-base GitHub OWNER reader 验证一次性评论"
        )
    base_roles, base_metrics, base_role_raw, base_metric_raw = _facts(repository, base_ref)
    head_roles, head_metrics, head_role_raw, head_metric_raw = _facts(repository, head_ref)
    base_surface = IMPACT.build_surface(base_roles, base_metrics)
    head_surface = IMPACT.build_surface(head_roles, head_metrics)
    grant_surface = head_surface - base_surface
    shrink_surface = base_surface - head_surface
    if not grant_surface and not shrink_surface:
        raise IMPACT.CountEvidenceError(
            "权限影响面为空；请由 CI 的静态 diff 推导 0，不必读取 biai-stage"
        )

    connection_string = os.environ.get(DSN_ENV)
    if not connection_string:
        raise IMPACT.CountEvidenceError(
            f"未设置 {DSN_ENV}；只读聚合必须在 biai-stage 的受控环境执行"
        )
    grant_count, shrink_count, captured_at = _read_stage_counts(
        connection_string,
        grant_roles=_role_names(grant_surface),
        shrink_roles=_role_names(shrink_surface),
    )
    manifest = {
        "schema": IMPACT.COUNT_SCHEMA,
        "base_facts_sha256": IMPACT._facts_digest(base_role_raw, base_metric_raw),
        "head_facts_sha256": IMPACT._facts_digest(head_role_raw, head_metric_raw),
        "grant_surface_sha256": IMPACT._surface_digest(grant_surface),
        "shrink_surface_sha256": IMPACT._surface_digest(shrink_surface),
        "counts": {"grant": grant_count, "shrink": shrink_count},
        "source": {
            "kind": IMPACT.STAGE_COUNT_CLAIM_SOURCE,
            "environment": "biai-stage",
            "dataset": "galaxy_user_role",
            "query_version": "permission-impact-users/v1",
            "captured_at": captured_at,
        },
    }
    IMPACT._validate_count_manifest(
        manifest,
        expected_base_facts_sha256=manifest["base_facts_sha256"],
        expected_head_facts_sha256=manifest["head_facts_sha256"],
        expected_grant_surface_sha256=manifest["grant_surface_sha256"],
        expected_shrink_surface_sha256=manifest["shrink_surface_sha256"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--provenance-output",
        type=Path,
        help="已废弃；GitHub OWNER provenance 只能由 CI 的 trusted-base reader 生成",
    )
    args = parser.parse_args()
    try:
        manifest = export(
            args.base_ref,
            args.head_ref,
            repository=args.repository,
            output=args.output,
            provenance_output=args.provenance_output,
        )
    except (IMPACT.ConfigShapeError, IMPACT.CountEvidenceError, RuntimeError) as error:
        print(f"L3 权限影响面 stage 计数导出失败关闭：{error}", file=sys.stderr)
        return 1
    print(
        "L3 权限影响面 stage 计数导出：通过 "
        f"grant={manifest['counts']['grant']} shrink={manifest['counts']['shrink']} "
        f"captured_at={manifest['source']['captured_at']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
