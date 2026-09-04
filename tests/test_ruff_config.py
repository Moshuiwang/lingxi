"""``pyproject.toml`` 的 ruff 配置钉住用例。

两张清单只许变短，不许变长：

1. ``[tool.ruff.lint.per-file-ignores]``——存量 ``D``（docstring 结构）/
   ``PLR0913``（参数个数）以及门禁接线批留下的 ``I001``/``UP035``/``F401``/
   ``N818``/``N802`` 残留登记表。``D`` 的强制范围收窄到 ``src/lingxi/``：
   ``tests/**``/``migrations/**``/``scripts/**`` 三条 glob 整目录豁免 ``D``
   （决策清单 D-21），这三条 glob 本身也是钉住条目，按同一条"只减不增"规则
   判定——删除或收紧这三条同样需要走本文件的钉住快照。逐文件的 ``D``
   条目仍然只登记 ``src/lingxi/`` 下的文件；三个豁免目录下若还有其他规则码
   （``PLR0913``/``N818``/``N802``/``F401``/``I001``）的存量违规，各自登记
   为独立条目，不搭 ``D`` 的豁免。后续清理批会逐个 ``src/lingxi/`` 文件把
   ``D`` 条目删空或缩短，收官目标是 ``src/lingxi/`` 下的 ``D`` 条目清空
   （不含三条豁免 glob 本身）。
2. ``[tool.ruff.format].exclude``——全仓格式化落地时同时排除六个贴线/冻结
   文件（结构性拆分之前，格式化的引号/换行重排会改动行数，可能顶穿文件体量
   棘轮阈值或让基线数字对不上）；钉住集与 ``pyproject.toml`` 逐字一致。这六
   个文件拆分完成、移出 exclude 列表后，这里同步收紧。

判定方式是子集，不是逐字相等：允许某个文件的条目从表里整条删除，也允许某个文件
保留的规则码变少；唯一不允许的是**出现新文件**或**某个已登记文件出现新规则码**
——那意味着有人往清单里"加"而不是"减"，与本表"只收紧"的设计意图相反。

变异实测：临时在 ``pyproject.toml`` 的 per-file-ignores 里给任意一个已登记文件
追加一个此前没有的规则码，或新增一个此前未登记的文件条目，跑本文件应判红；
改完验证后删除该临时改动、清 ``__pycache__``。
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_ruff_config() -> dict:
    with PYPROJECT.open("rb") as handle:
        document = tomllib.load(handle)
    return document["tool"]["ruff"]


# 以下常量是接线落地时 pyproject.toml 里 [tool.ruff.lint.per-file-ignores] 的
# 逐字快照，按文件路径排序。后续清理批缩短实际清单后，无需同步缩短这份钉住
# 快照——测试断言的是"实际 ⊆ 钉住"，钉住快照只是历史上限，不用跟着每次收紧
# 同步下调（除非要收紧钉住本身，那是一次有意的门禁升级，不属于批次清理的日常
# 工作）。
PINNED_PER_FILE_IGNORES: dict[str, frozenset[str]] = {
    "migrations/**": frozenset(["D"]),
    "migrations/alembic/env.py": frozenset(["I001"]),
    "scripts/**": frozenset(["D"]),
    "scripts/ops/import_local_permission_override.py": frozenset(["F401"]),
    "scripts/probe_drive_folder_permissions.py": frozenset(["N818"]),
    "src/lingxi/adapters/admin_metric_alias_map_file.py": frozenset(["D"]),
    "src/lingxi/adapters/admin_post_callback.py": frozenset(["D"]),
    "src/lingxi/adapters/admin_registry.py": frozenset(["D"]),
    "src/lingxi/adapters/claude_agent_hooks.py": frozenset(["D"]),
    "src/lingxi/adapters/claude_agent_session.py": frozenset(["D", "PLR0913", "N818"]),
    "src/lingxi/adapters/company_function_metric_map_file.py": frozenset(["D"]),
    "src/lingxi/adapters/delegated_credentials.py": frozenset(["D"]),
    "src/lingxi/adapters/delegated_subject_lookup.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_admin_card.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_bitable_association.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_delivery.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_directory.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_docx_delivery.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_events.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_group_message.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_longconn.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_org_snapshot_reader.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_outbound.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_permission_bitable.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_reauthorization.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_roster_bitable.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_sheets_delivery.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_tenant_token.py": frozenset(["D"]),
    "src/lingxi/adapters/feishu_user_message.py": frozenset(["D"]),
    "src/lingxi/adapters/galaxy_csv_export.py": frozenset(["D"]),
    "src/lingxi/adapters/galaxy_import.py": frozenset(["D"]),
    "src/lingxi/adapters/mcp_token_cipher.py": frozenset(["D"]),
    "src/lingxi/adapters/oauth_bridge_client.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_content_capture.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_content_capture_retention.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_dataclasses.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_gateway_store.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_listener.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_queue_base.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_queue_gateway_delivery.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_queue_lifecycle.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_queue_outbox.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/adapters/postgres_conversation/_queue_session_cleanup.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_task_queue.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_conversation/_transaction.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_daily_report.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_daily_report_watermark.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_document_delivery.py": frozenset(["D", "N818"]),
    "src/lingxi/adapters/postgres_email_binding.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_galaxy_snapshot.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_identity.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_late_readiness_recovery.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_local_permission.py": frozenset(["D", "PLR0913", "N818"]),
    "src/lingxi/adapters/postgres_management_card_context.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/adapters/postgres_mcp_token.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_onboarding_failure.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_pending_action.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/adapters/postgres_permission_publish.py": frozenset(["D", "N818"]),
    "src/lingxi/adapters/postgres_permission_recompute_trigger.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_roster_audit.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_roster_snapshot.py": frozenset(["D", "N818"]),
    "src/lingxi/adapters/postgres_stalled_provisioning.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_targeted_recompute_lookup.py": frozenset(["D"]),
    "src/lingxi/adapters/postgres_user_memory.py": frozenset(["D"]),
    "src/lingxi/adapters/query_mcp_probe.py": frozenset(["D"]),
    "src/lingxi/adapters/retention.py": frozenset(["D"]),
    "src/lingxi/adapters/role_function_map_file.py": frozenset(["D"]),
    "src/lingxi/adapters/stock_token_bitable.py": frozenset(["D"]),
    "src/lingxi/adapters/user_environment.py": frozenset(["D"]),
    "src/lingxi/adapters/user_mcp_config.py": frozenset(["D"]),
    "src/lingxi/apps/admin_bootstrap/__init__.py": frozenset(["D"]),
    "src/lingxi/apps/gateway/__init__.py": frozenset(["D", "I001", "UP035"]),
    "src/lingxi/apps/gateway/config.py": frozenset(["D"]),
    "src/lingxi/apps/gateway/delivery.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/apps/gateway/document_delivery.py": frozenset(["D"]),
    "src/lingxi/apps/gateway/group_mention_hint.py": frozenset(["D"]),
    "src/lingxi/apps/gateway/log_redaction.py": frozenset(["D"]),
    "src/lingxi/apps/gateway/management_status.py": frozenset(["D"]),
    "src/lingxi/apps/gateway/onboarding.py": frozenset(["D"]),
    "src/lingxi/apps/healthcheck/__init__.py": frozenset(["D"]),
    "src/lingxi/apps/liveness.py": frozenset(["D"]),
    "src/lingxi/apps/reauthorize/__init__.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/alerting_assembly.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/assembly.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/audit.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/config.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/credential_rotation.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/daily_report.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/document_delivery_dead_letter.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/late_readiness_recovery.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/apps/scheduler/loop.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/onboarding.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/org_snapshot_sync.py": frozenset(["D", "N818"]),
    "src/lingxi/apps/scheduler/permission_publish.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/apps/scheduler/permission_readiness_assembly.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/permission_refresh.py": frozenset(["D", "PLR0913", "N818"]),
    "src/lingxi/apps/scheduler/retention.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/roster_audit.py": frozenset(["D"]),
    "src/lingxi/apps/scheduler/stalled_provisioning.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/apps/trace/__init__.py": frozenset(["D"]),
    "src/lingxi/apps/worker/__init__.py": frozenset(["D"]),
    "src/lingxi/apps/worker/cli.py": frozenset(["D"]),
    "src/lingxi/apps/worker/config.py": frozenset(["D"]),
    "src/lingxi/apps/worker/report.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/apps/worker/report_extraction.py": frozenset(["D"]),
    "src/lingxi/apps/worker/service.py": frozenset(["D", "PLR0913", "I001"]),
    "src/lingxi/apps/worker/session_cleanup.py": frozenset(["D"]),
    "src/lingxi/apps/worker/turn.py": frozenset(["D"]),
    "src/lingxi/config/content.py": frozenset(["D"]),
    "src/lingxi/core/admin/card_callback.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/core/admin/card_dispatch.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/core/admin/card_layout.py": frozenset(["D"]),
    "src/lingxi/core/admin/commands.py": frozenset(["D"]),
    "src/lingxi/core/admin/display_names.py": frozenset(["D"]),
    "src/lingxi/core/admin/management_card.py": frozenset(["D"]),
    "src/lingxi/core/admin/notification.py": frozenset(["D", "N818"]),
    "src/lingxi/core/admin/pending_action.py": frozenset(["D", "N818"]),
    "src/lingxi/core/admin/registry.py": frozenset(["D", "N818"]),
    "src/lingxi/core/admin/router.py": frozenset(["D", "PLR0913", "UP035"]),
    "src/lingxi/core/admin/views.py": frozenset(["D"]),
    "src/lingxi/core/alerting.py": frozenset(["D"]),
    "src/lingxi/core/conversation/commands.py": frozenset(["D"]),
    "src/lingxi/core/conversation/onboarding_recovery.py": frozenset(["D"]),
    "src/lingxi/core/conversation/pipeline.py": frozenset(
        ["D", "PLR0913", "N818", "I001", "UP035"]
    ),
    "src/lingxi/core/conversation/ports.py": frozenset(["D"]),
    "src/lingxi/core/conversation/session_window.py": frozenset(["D"]),
    "src/lingxi/core/daily_report.py": frozenset(["D"]),
    "src/lingxi/core/delivery/ports.py": frozenset(["D"]),
    "src/lingxi/core/execution/audit.py": frozenset(["D"]),
    "src/lingxi/core/execution/card_stream.py": frozenset(["D", "PLR0913", "N818"]),
    "src/lingxi/core/execution/document_delivery.py": frozenset(["D"]),
    "src/lingxi/core/execution/hooks.py": frozenset(["D"]),
    "src/lingxi/core/execution/input_safety.py": frozenset(["D"]),
    "src/lingxi/core/execution/message_stream.py": frozenset(["D"]),
    "src/lingxi/core/execution/tool_policy.py": frozenset(["D"]),
    "src/lingxi/core/identity/access_token_supply.py": frozenset(["D", "N818"]),
    "src/lingxi/core/identity/credentials.py": frozenset(["D", "N818"]),
    "src/lingxi/core/identity/first_contact.py": frozenset(["D"]),
    "src/lingxi/core/identity/identifiers.py": frozenset(["D"]),
    "src/lingxi/core/identity/innertest_roster_gate.py": frozenset(["D"]),
    "src/lingxi/core/identity/legacy_permission_import.py": frozenset(["D"]),
    "src/lingxi/core/identity/onboarding.py": frozenset(["D"]),
    "src/lingxi/core/identity/onboarding_guards.py": frozenset(["D"]),
    "src/lingxi/core/identity/onboarding_ports.py": frozenset(["D"]),
    "src/lingxi/core/identity/onboarding_runner.py": frozenset(
        ["D", "PLR0913", "F401", "I001", "UP035"]
    ),
    "src/lingxi/core/identity/onboarding_support.py": frozenset(["D"]),
    "src/lingxi/core/identity/onboarding_terminal.py": frozenset(["D", "N818"]),
    "src/lingxi/core/identity/org_snapshot.py": frozenset(["D"]),
    "src/lingxi/core/identity/preprovision.py": frozenset(["D"]),
    "src/lingxi/core/identity/provisioning.py": frozenset(["D"]),
    "src/lingxi/core/identity/roster_audit.py": frozenset(["D"]),
    "src/lingxi/core/identity/roster_report.py": frozenset(["D"]),
    "src/lingxi/core/identity/roster_snapshot.py": frozenset(["D"]),
    "src/lingxi/core/identity/stock_token_source.py": frozenset(["D"]),
    "src/lingxi/core/ids.py": frozenset(["D"]),
    "src/lingxi/core/innertest_content_capture.py": frozenset(["D"]),
    "src/lingxi/core/permission/account_match.py": frozenset(["D"]),
    "src/lingxi/core/permission/galaxy_export.py": frozenset(["D"]),
    "src/lingxi/core/permission/galaxy_scope.py": frozenset(["D"]),
    "src/lingxi/core/permission/legacy_diff.py": frozenset(["D"]),
    "src/lingxi/core/permission/local_override.py": frozenset(["D"]),
    "src/lingxi/core/permission/mcp_readiness.py": frozenset(["D"]),
    "src/lingxi/core/permission/merge_sources.py": frozenset(["D"]),
    "src/lingxi/core/permission/metric_translation.py": frozenset(["D", "N818"]),
    "src/lingxi/core/permission/notification.py": frozenset(["D"]),
    "src/lingxi/core/permission/position_override.py": frozenset(["D"]),
    "src/lingxi/core/permission/publish.py": frozenset(["D", "N818"]),
    "src/lingxi/core/permission/publish_row.py": frozenset(["D"]),
    "src/lingxi/core/permission/role_function.py": frozenset(["D"]),
    "src/lingxi/core/permission/table_access_token_supply.py": frozenset(["D", "N818"]),
    "src/lingxi/core/permission/targeted_recompute.py": frozenset(["D", "PLR0913"]),
    "src/lingxi/core/permission/tenant_token_supply.py": frozenset(["D"]),
    "src/lingxi/core/user_memory.py": frozenset(["D"]),
    "src/lingxi/core/year_grounding_guard.py": frozenset(["D"]),
    "tests/**": frozenset(["D"]),
    "tests/test_claude_agent_session_adapter.py": frozenset(["PLR0913"]),
    "tests/test_document_delivery.py": frozenset(["PLR0913"]),
    "tests/test_gateway_transport.py": frozenset(["N818"]),
    "tests/test_management_card.py": frozenset(["N802"]),
    "tests/test_metric_map_single_source.py": frozenset(["N818"]),
    "tests/test_permission_publish_duty.py": frozenset(["PLR0913"]),
    "tests/test_permission_publish_postgres.py": frozenset(["N818"]),
    "tests/test_permission_publish_row.py": frozenset(["N802"]),
    "tests/test_permission_refresh_duty.py": frozenset(["PLR0913"]),
    "tests/test_roster_access_token_supply.py": frozenset(["PLR0913", "N818"]),
    "tests/test_targeted_permission_recompute.py": frozenset(["PLR0913"]),
    "tests/test_worker_entry.py": frozenset(["PLR0913"]),
}

# 六个贴线/冻结文件在结构性拆分完成前不参与全仓格式化，与 pyproject.toml
# [tool.ruff.format].exclude 逐字一致；拆分完成、移出该 exclude 列表后，这里
# 同步收紧（只许变短，不许变长）。
PINNED_FORMAT_EXCLUDE: frozenset[str] = frozenset(
    [
        "src/lingxi/apps/gateway/__init__.py",
        "src/lingxi/apps/scheduler/permission_refresh.py",
        "src/lingxi/apps/worker/service.py",
        "src/lingxi/core/admin/router.py",
        "src/lingxi/core/conversation/pipeline.py",
        "src/lingxi/core/identity/onboarding_runner.py",
    ]
)


class PerFileIgnoresOnlyShrinksTest(unittest.TestCase):
    """per-file-ignores 只许变短：新文件、新规则码都判红。"""

    def test_every_registered_file_is_within_the_pinned_set(self) -> None:
        """登记表里出现钉住快照没有的新文件即判红。"""
        actual = _load_ruff_config()["lint"]["per-file-ignores"]
        unpinned_files = sorted(set(actual) - set(PINNED_PER_FILE_IGNORES))
        self.assertEqual(
            unpinned_files,
            [],
            f"per-file-ignores 出现钉住快照里没有的新文件：{unpinned_files}——"
            "只允许删除或缩短既有条目，新增文件视为门禁被放宽，须先更新本文件的"
            "钉住快照并说明理由。",
        )

    def test_every_registered_codes_list_is_within_the_pinned_codes(self) -> None:
        """某个文件的豁免码里出现钉住快照没有的新码即判红。"""
        actual = _load_ruff_config()["lint"]["per-file-ignores"]
        overflowing: dict[str, list[str]] = {}
        for path, codes in actual.items():
            pinned_codes = PINNED_PER_FILE_IGNORES.get(path, frozenset())
            extra = sorted(set(codes) - pinned_codes)
            if extra:
                overflowing[path] = extra
        self.assertEqual(
            overflowing,
            {},
            f"以下文件的 per-file-ignores 出现钉住快照里没有的新规则码：{overflowing}"
            "——只允许收紧（删码），新增规则码视为门禁被放宽。",
        )

    def test_pinned_snapshot_itself_is_not_accidentally_empty(self) -> None:
        """自证：钉住快照本身不能是空字典。

        空字典会让两条子集断言永远空判通过，起不到钉住的作用——防止本文件被后续改动
        悄悄改成永远绿的空壳。
        """
        self.assertGreater(len(PINNED_PER_FILE_IGNORES), 0)


class FormatExcludeOnlyShrinksTest(unittest.TestCase):
    """format.exclude 只许变短；本批是空集，全仓格式化批接线后同步更新钉住快照。"""

    def test_format_exclude_is_within_the_pinned_set(self) -> None:
        """format.exclude 出现钉住快照没有的新条目即判红。"""
        format_section = _load_ruff_config().get("format", {})
        actual = frozenset(format_section.get("exclude", []))
        extra = sorted(actual - PINNED_FORMAT_EXCLUDE)
        self.assertEqual(
            extra,
            [],
            f"format.exclude 出现钉住快照里没有的新条目：{extra}——只允许收紧。",
        )


if __name__ == "__main__":
    unittest.main()
