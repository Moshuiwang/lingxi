#!/usr/bin/env python3
"""部署四操作：先固定计划，再按批准执行；恢复必须独立批准。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

from deploy_runtime import Runtime, control_for
from deploy_state import (
    DeployError,
    StateStore,
    UnknownError,
    atomic_write,
    canonical,
    check_id,
    fingerprint,
    host_lock,
    read_json,
    validate_approval,
)

STAGES = ("prepare", "stop", "migrate", "activate", "start", "observe")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_manifest", ROOT / "scripts/ci/release_manifest.py"
)
release_manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_manifest)


def exact(value, fields, code):
    """未知字段不能成为未批准操作或秘密的入口。"""
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise DeployError(code)


def public_config(doc):
    """非秘密配置必须命中固定字段集合。"""
    exact(doc, "schema values files", "public_config_shape")
    if doc["schema"] != 1 or not isinstance(doc["values"], dict):
        raise DeployError("public_config_shape")
    exact(doc["files"], "scheduler worker", "public_file_inventory_shape")
    allowed_files = {
        "scheduler": {"company_function_metric_map.toml", "content.override.toml"},
        "worker": {"system_prompt.md", "content.override.toml"},
    }
    for role, files in doc["files"].items():
        if not isinstance(files, dict) or set(files) - allowed_files[role]:
            raise DeployError("non_public_file_rejected")
        if any(
            not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)
            for value in files.values()
        ):
            raise DeployError("public_file_digest_required")
    pattern = r"LINGXI_(?:(?:SCHEDULER|GATEWAY|WORKER|WORKER_QUEUE|MIGRATE|REAUTHORIZE)_(?:CPU_LIMIT|MEM_LIMIT|PIDS_LIMIT|TMPFS_SIZE|RUNTIME_CONFIG_DIR)|WORKER_MAX_CONCURRENCY|SCHEDULER_INTERVAL_SECONDS|INNERTEST_(?:SCOPE|BINDING_ID|SOCKET_PATH|BINDING_PATH|SOCKET_DIRECTORY|BINDING_DIRECTORY))"
    for key, value in doc["values"].items():
        if (
            not re.fullmatch(pattern, key)
            or not isinstance(value, str)
            or not re.fullmatch(r"[a-zA-Z0-9_./:@-]{1,256}", value)
        ):
            raise DeployError("non_public_configuration_rejected")
    return doc


def validate_host(host):
    """身份、目录和批准来源必须事先固定。"""
    exact(
        host,
        "schema host environment project deploy_root config_root bundle_root relay_root lock_path docker approval_sources",
        "host_shape",
    )
    if host["schema"] != 1 or host["environment"] not in ("stage", "production"):
        raise DeployError("host_environment")
    check_id(host["project"])
    for key in ("deploy_root", "config_root", "bundle_root", "relay_root", "lock_path", "docker"):
        if not Path(host[key]).is_absolute() or ".." in Path(host[key]).parts:
            raise DeployError("host_paths_must_be_absolute")
    if not host["approval_sources"] or any(
        not re.fullmatch(
            r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*(?:#issuecomment-[0-9]+)?",
            x,
        )
        for x in host["approval_sources"]
    ):
        raise DeployError("approval_sources_required")


def validate_historical_release(release):
    """历史标签单独登记原始事实，不伪造维护分支、构建或验收清单。"""
    exact(
        release,
        "schema repository version tag prerelease commit tree images migration_heads",
        "historical_release_shape",
    )
    version, candidate = release_manifest.release_version(release["tag"])
    if candidate or release["prerelease"] is not False or release["version"] != version:
        raise DeployError("historical_formal_tag_required")
    if not re.fullmatch("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", release["repository"]):
        raise DeployError("historical_repository_required")
    if any(not release_manifest.SHA.fullmatch(release[key]) for key in ("commit", "tree")):
        raise DeployError("historical_source_required")
    if (
        set(release["images"]) != set(release_manifest.SERVICES)
        or len(release["migration_heads"]) != 1
    ):
        raise DeployError("historical_artifacts_incomplete")
    for service, reference in release["images"].items():
        prefix = "ghcr.io/" + release["repository"].lower() + "-" + service + "@"
        if not reference.startswith(prefix) or not release_manifest.DIGEST.fullmatch(
            reference[len(prefix) :]
        ):
            raise DeployError("historical_image_digest_required")


def validate_history(plan, release):
    """历史恢复只属于指定部署，不补造旧 Release 验收清单。"""
    history = plan["recovery"]["historical"]
    exact(
        history,
        "schema deployment_id manifest_sha256 release_evidence_sha256 control_bundle legacy_no_handler entry_disabled_receipt_sha256 compatible compatibility_sha256",
        "historical_recovery_required",
    )
    if (
        history["schema"] != 1
        or history["deployment_id"]
        != (plan["recovery_of"]["id"] if plan["operation"] == "recover" else plan["id"])
        or history["manifest_sha256"] != fingerprint(release)
        or history["legacy_no_handler"] is not True
        or history["compatible"] is not True
    ):
        raise DeployError("historical_recovery_incompatible")
    for key in ("release_evidence_sha256", "entry_disabled_receipt_sha256", "compatibility_sha256"):
        if not re.fullmatch("[0-9a-f]{64}", history[key]):
            raise DeployError("historical_evidence_required")
    if history["control_bundle"]["schema_revision"] != 1:
        raise DeployError("historical_control_tools_required")


def validate_plan(plan, host, config):
    """执行来源必须绑定同一份部署计划。"""
    exact(
        plan,
        "schema id operation recovery_of host environment project host_sha256 old new deployer_sha256 config_sha256 acceptance_sha256 acceptance_source current_heads resources not_before expires_at drain recovery packages channel approval_source",
        "plan_shape",
    )
    check_id(plan["id"])
    if plan["operation"] == "recover":
        exact(plan["recovery_of"], "id plan_sha256", "recovery_origin_required")
        check_id(plan["recovery_of"]["id"])
    elif plan["recovery_of"] is not None:
        raise DeployError("apply_cannot_authorize_recovery")
    if plan["schema"] != 1 or plan["operation"] not in ("apply", "recover"):
        raise DeployError("plan_schema")
    if any(plan[k] != host[k] for k in ("host", "environment", "project")) or plan[
        "host_sha256"
    ] != fingerprint(host):
        raise DeployError("host_environment_changed")
    if (
        fingerprint(config) != plan["config_sha256"]
        or plan["approval_source"] not in host["approval_sources"]
    ):
        raise DeployError("configuration_or_authority_changed")
    own_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if own_digest != plan["deployer_sha256"]:
        raise DeployError("frozen_deployer_required")
    if not re.fullmatch(
        r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*(?:#issuecomment-[0-9]+)?",
        plan["acceptance_source"],
    ):
        raise DeployError("acceptance_source_required")
    if not re.fullmatch("[a-zA-Z0-9_./:-]{1,256}", plan["recovery"]["credential_source"]):
        raise DeployError("credential_source_identifier_only")
    for release in (plan["old"], plan["new"]):
        allowed = {
            "schema",
            "repository",
            "version",
            "tag",
            "prerelease",
            "branch",
            "commit",
            "tree",
            "run_id",
            "images",
            "migration_heads",
            "control_bundle",
            "candidate_tag",
            "candidate_manifest_sha256",
            "acceptance",
            "promotion",
        }
        if set(release) - allowed:
            raise DeployError("release_unknown_fields")
        if release["schema"] == "historical":
            validate_historical_release(release)
        else:
            release_manifest.validate_manifest(
                release, release["repository"], prerelease=release["prerelease"]
            )
        if release["schema"] != 2:
            validate_history(plan, release)
    if plan["operation"] == "apply" and plan["new"]["schema"] != 2:
        raise DeployError("legacy_release_cannot_be_new_target")
    if plan["environment"] == "production" and plan["new"]["prerelease"]:
        raise DeployError("production_candidate_rejected")
    if plan["old"]["repository"] != plan["new"]["repository"]:
        raise DeployError("repository_changed")
    if len(plan["new"]["migration_heads"]) != 1 or len(plan["current_heads"]) != 1:
        raise DeployError("single_migration_head_required")
    exact(plan["resources"], "required_free_bytes evidence_sha256", "resource_budget_shape")
    if (
        type(plan["resources"]["required_free_bytes"]) is not int
        or plan["resources"]["required_free_bytes"] <= 0
    ):
        raise DeployError("resource_budget_required")
    exact(plan["drain"], "gateway scheduler", "drain_shape")
    if plan["drain"] != {"gateway": 20, "scheduler": 120}:
        raise DeployError("drain_budget_changed")
    exact(
        plan["recovery"],
        "compatible evidence_sha256 target_manifest_sha256 credential_source permissions_sha256 config_sha256 historical",
        "recovery_shape",
    )
    if plan["recovery"]["compatible"] is not True:
        raise DeployError("recovery_incompatible")
    if plan["recovery"]["target_manifest_sha256"] != fingerprint(plan["old"]):
        raise DeployError("recovery_target_changed")
    for value in (
        plan["acceptance_sha256"],
        plan["resources"]["evidence_sha256"],
        plan["recovery"]["evidence_sha256"],
        plan["recovery"]["permissions_sha256"],
        plan["recovery"]["config_sha256"],
    ):
        if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
            raise DeployError("evidence_digest_required")
    exact(
        plan["channel"],
        "schema_revision protocol relay_sha256 socket_path socket_mode directory_mode binding_version uid_map_sha256 host_uid peer_uid scheduler_uid socket_gid socket_owner_uid installation_receipt_sha256",
        "channel_shape",
    )
    channel = plan["channel"]
    for key in (
        "host_uid",
        "peer_uid",
        "scheduler_uid",
        "socket_owner_uid",
        "socket_gid",
        "binding_version",
    ):
        if type(channel[key]) is not int or channel[key] < 0:
            raise DeployError("channel_integer_required")
    if channel["socket_gid"] in (0, 65534) or channel["binding_version"] < 1:
        raise DeployError("channel_restricted_group_required")
    if (
        channel["schema_revision"] != 1
        or channel["protocol"] != "2025-11-25"
        or channel["socket_mode"] != 0o660
        or channel["directory_mode"] != 0o750
        or channel["host_uid"] in (0, 65534, channel["scheduler_uid"])
        or channel["peer_uid"] in (0, 65534, channel["scheduler_uid"])
    ):
        raise DeployError("channel_identity_or_protocol")
    for key in ("relay_sha256", "uid_map_sha256", "installation_receipt_sha256"):
        if not re.fullmatch("[0-9a-f]{64}", channel[key]):
            raise DeployError("channel_evidence_required")
    if (
        not Path(channel["socket_path"]).is_absolute()
        or Path(channel["socket_path"]).name != "admin.sock"
    ):
        raise DeployError("socket_path_required")
    expected = {control_for(plan, plan[k])["sha256"] for k in ("old", "new")}
    if set(plan["packages"]) != expected or any(
        not Path(v).is_absolute() for v in plan["packages"].values()
    ):
        raise DeployError("fixed_packages_required")
    if not (
        type(plan["not_before"]) in (int, float)
        and type(plan["expires_at"]) in (int, float)
        and plan["not_before"] < plan["expires_at"]
    ):
        raise DeployError("execution_window_invalid")

    from control_bundle import verify

    tool_release = plan["new"] if plan["operation"] == "apply" else plan["old"]
    tool_bundle = control_for(plan, tool_release)
    _, contents = verify(Path(plan["packages"][tool_bundle["sha256"]]), tool_bundle)
    for name in (
        "deploy/lingxi_deploy.py",
        "deploy/deploy_state.py",
        "deploy/deploy_runtime.py",
        "deploy/control_bundle.py",
        "scripts/ci/release_manifest.py",
    ):
        if (ROOT / name).read_bytes() != contents[name]:
            raise DeployError("frozen_control_tools_required")


def plan_document(request, host, config):
    """准备计划不修改服务或环境。"""
    validate_host(host)
    public_config(config)
    # 请求本身是可读的非秘密材料；发布选择器产出的完整清单不可在此重新解析浮动 main。
    result = dict(
        request,
        schema=1,
        host=host["host"],
        environment=host["environment"],
        project=host["project"],
        host_sha256=fingerprint(host),
        config_sha256=fingerprint(config),
        deployer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    validate_plan(result, host, config)
    return result


def execute(plan, approval, store, runtime, *, now=None):
    """首次执行与中断接续共用同一状态机。"""
    approval_sha = validate_approval(plan, approval, now)
    if plan["operation"] == "recover":
        original = store.plan(plan["recovery_of"]["id"])
        if (
            fingerprint(original) != plan["recovery_of"]["plan_sha256"]
            or fingerprint(plan["new"]) != original["recovery"]["target_manifest_sha256"]
            or plan["config_sha256"] != original["recovery"]["config_sha256"]
        ):
            raise DeployError("recovery_package_changed")
    with host_lock(Path(runtime.host["lock_path"])) as lock_fd:
        runtime.lock_fd = lock_fd
        runtime.state_directory = store.root
        active_path = Path(runtime.host["lock_path"]).with_suffix(".active.json")
        if active_path.exists():
            active = read_json(active_path)
            same = active["id"] == plan["id"] and active["plan_sha256"] == fingerprint(plan)
            recovery = plan["operation"] == "recover" and plan["recovery_of"] == {
                "id": active["id"],
                "plan_sha256": active["plan_sha256"],
            }
            if active["status"] != "verified" and not same and not recovery:
                raise DeployError("unfinished_host_deployment")
        state = store.state(plan)
        if state["approval_sha256"] not in (None, approval_sha):
            raise DeployError("approval_changed")
        state["approval_sha256"] = approval_sha
        state["status"] = "running"
        store.save(plan, state)
        try:
            runtime.preflight(plan)
            atomic_write(
                active_path,
                {"id": plan["id"], "plan_sha256": fingerprint(plan), "status": "running"},
            )
            for stage in STAGES:
                runtime.check_revocation(plan)
                if time.time() > plan["expires_at"]:
                    raise UnknownError("execution_window_expired")
                if plan["operation"] == "recover" and stage == "migrate":
                    snapshot = runtime.snapshot(plan)
                    if snapshot["migration_heads"] != plan["current_heads"]:
                        raise UnknownError("recovery_never_downgrades_database")
                    continue
                previous = state["stages"].get(stage, {})
                snapshot = runtime.snapshot(plan) if stage != "prepare" else None
                # 完成账必须与当前事实一致；后续阶段已启动服务时不重新执行停止阶段。
                later = any(name in state["stages"] for name in STAGES[STAGES.index(stage) + 1 :])
                if previous.get("status") == "verified":
                    if stage == "stop" and later:
                        continue
                    if stage == "observe":
                        if state.get("verified") and runtime.complete(stage, plan, snapshot):
                            continue
                    elif runtime.complete(stage, plan, snapshot):
                        continue
                    elif stage in ("migrate", "activate", "start"):
                        raise UnknownError("verified_stage_drift")
                if stage == "migrate":
                    if runtime.complete(stage, plan, snapshot):
                        state["stages"][stage] = {
                            "status": "verified",
                            "time": time.time(),
                            "actual": snapshot,
                        }
                        store.save(plan, state)
                        continue
                    if (
                        snapshot["job"]
                        or previous
                        or snapshot["migration_heads"] != plan["current_heads"]
                    ):
                        raise UnknownError("migration_unknown_no_retry")
                state["stages"][stage] = {
                    "status": "running",
                    "started_at": time.time(),
                    "time": time.time(),
                }
                store.save(plan, state)
                if stage == "observe":

                    def save_sample(actual):
                        samples = state["stages"][stage].setdefault("samples", [])
                        if len(samples) >= 256:
                            raise UnknownError("observation_sample_limit")
                        samples.append({"time": time.time(), "actual": actual})
                        store.save(plan, state)

                    snapshot = runtime.observe(plan, save_sample)
                else:
                    runtime.perform(stage, plan)
                    snapshot = runtime.snapshot(plan)
                if not runtime.complete(stage, plan, snapshot):
                    raise UnknownError("stage_result_unverified")
                state["stages"][stage].update(status="verified", time=time.time(), actual=snapshot)
                store.save(plan, state)
            state["cleanup"] = runtime.cleanup(plan)
            state.update(status="verified", verified=time.time(), error=None)
            store.save(plan, state)
            atomic_write(
                active_path,
                {"id": plan["id"], "plan_sha256": fingerprint(plan), "status": "verified"},
            )
        except Exception as error:
            state["status"] = "unknown" if isinstance(error, (UnknownError, OSError)) else "failed"
            state["error"] = str(error) if isinstance(error, DeployError) else "operation_failed"
            store.save(plan, state)
            raise
        finally:
            runtime.lock_fd = None
        return state


def preview(plan, host, config):
    """差异、窗口和恢复材料在批准前集中展示。"""
    changes = [
        {
            "service": service,
            "old": plan["old"]["images"][service],
            "new": plan["new"]["images"][service],
        }
        for service in release_manifest.SERVICES
        if plan["old"]["images"][service] != plan["new"]["images"][service]
    ]
    return {
        "result": "等待批准",
        "target": {
            key: host[key]
            for key in (
                "host",
                "project",
                "deploy_root",
                "config_root",
                "bundle_root",
                "relay_root",
            )
        },
        "public_configuration": config,
        "deployment_id": plan["id"],
        "environment": plan["environment"],
        "image_changes": changes,
        "control_bundle": {
            "old": control_for(plan, plan["old"])["sha256"],
            "new": control_for(plan, plan["new"])["sha256"],
        },
        "migration": {"current": plan["current_heads"], "target": plan["new"]["migration_heads"]},
        "window": {"start": plan["not_before"], "end": plan["expires_at"]},
        "resources": plan["resources"],
        "drain_seconds": plan["drain"],
        "observation_seconds": 900,
        "recovery": plan["recovery"],
    }


def main():
    """只接受四个明确的操作。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-contract", required=True, type=Path)
    parser.add_argument("--public-config", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--request", required=True, type=Path)
    p.add_argument("--dry-run", action="store_true")
    for name in ("apply", "status", "recover"):
        p = sub.add_parser(name)
        p.add_argument("plan_id")
        if name != "status":
            p.add_argument("--approval", required=True, type=Path)
    args = parser.parse_args()
    host, config = read_json(args.host_contract), public_config(read_json(args.public_config))
    validate_host(host)
    store = StateStore(args.state_directory)
    if args.operation == "plan":
        plan = plan_document(read_json(args.request), host, config)
        if not args.dry_run:
            store.save_plan(plan)
        print(
            canonical(
                {
                    "plan": plan,
                    "plan_sha256": fingerprint(plan),
                    "summary": preview(plan, host, config),
                }
            ).decode(),
            end="",
        )
        return
    plan = store.plan(args.plan_id)
    validate_plan(plan, host, config)
    runtime = Runtime(host, config)
    if args.operation == "status":
        state = store.state(plan)
        try:
            actual = runtime.snapshot(plan, readonly=True)
            result = dict(state, actual=actual)
            if state["status"] == "verified" and not runtime.complete("start", plan, actual):
                result["status"] = "unknown"
        except Exception:
            result = dict(state, status="unknown", error="actual_state_unavailable")
    else:
        if plan["operation"] != args.operation:
            raise DeployError("independent_recovery_approval_required")
        result = execute(plan, read_json(args.approval), store, runtime)
    labels = {
        "planned": "等待批准",
        "running": "等待部署完成",
        "verified": "成功",
        "failed": "失败",
        "unknown": "结果待核查",
    }
    result["result"] = labels.get(result["status"], "结果待核查")
    result["required_action"] = (
        "无需重复执行"
        if result["status"] == "verified"
        else "按原计划和有效批准回读接续；恢复须独立批准"
    )
    print(canonical(result).decode(), end="")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(error)
                    if isinstance(error, DeployError)
                    else "invalid_input_or_environment",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
