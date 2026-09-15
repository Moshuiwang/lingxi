"""预发单用户开通记录复位工具的真库用例（`scripts/ops/stage_user_reset.py`）。

真库才能证伪的部分：整链建库后按外键顺序删得动、删得净；旁观用户与审计类表零波及；
留存内容表默认拦停；摘要不符零写；回退后逐表行数与关键字段等于备份；两道硬闸各一条；
清单之外的引用表现查即拒。飞书预发表用可注入的假传输（同守卫测试姿势），样本全部合成。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from postgres_schema import ensure_production_schema, psycopg_available, reset_production_rows

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = (REPOSITORY_ROOT / "scripts" / "ops" / "stage_user_reset.py").resolve()
print(f"loaded_stage_user_reset={SCRIPT}")
SKIP_REASON = (
    "跳过：未设置 LINGXI_POSTGRES_DSN，预发用户复位工具的真库断言未验证"
    if not os.environ.get("LINGXI_POSTGRES_DSN")
    else "跳过：LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动，预发用户复位工具的真库断言未验证"
)


def _load_script() -> Any:
    name = "stage_user_reset_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()

EMAIL = "Reset.Target@example.invalid"
NORMALIZED_EMAIL = "reset.target@example.invalid"
OPEN_ID = "ou_reset_target_fake"
USER_ID = "usr_reset_target"
OTHER_EMAIL = "bystander@example.invalid"
OTHER_OPEN_ID = "ou_bystander_fake"
OTHER_USER_ID = "usr_bystander"
CIPHER = "cipher-sentinel-never-printed"
#: 令牌库的密文列有形状 CHECK（86 位 base64 + "=="），合成一个同形状的哨兵。
DB_CIPHER = ("cipherSentinelNeverPrinted" * 4)[:86] + "=="
PROMPT = "合成问题正文-不得出现在干跑输出"
ANSWER = "合成回答正文-不得出现在干跑输出"
SCOPE = "stage-fake"
STAGE_APP_TOKEN = "stage-app-token-sentinel"
FORMAL_APP_TOKEN = "formal-app-token-sentinel"
RECORD_ID = "recTargetFake"
OTHER_RECORD_ID = "recBystanderFake"

#: 审计与留存类表：`apply` 前后行数必须逐字不变。
AUDIT_TABLES = ("operation_audit", "pending_action", "onboarding_failure", "admin_action_followup")


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        "LINGXI_POSTGRES_DSN": os.environ.get("LINGXI_POSTGRES_DSN", ""),
        "LINGXI_FEISHU_APP_ID": "cli_synthetic",
        "LINGXI_FEISHU_APP_SECRET": "app-secret-sentinel",
        "LINGXI_FEISHU_BASE_URL": "https://open.feishu.test/open-apis",
        "LINGXI_PERMISSION_BITABLE_APP_TOKEN": STAGE_APP_TOKEN,
        "LINGXI_PERMISSION_BITABLE_TABLE_ID": "tbl_stage_fake",
        "LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN": FORMAL_APP_TOKEN,
    }
    values.update(overrides)
    return values


def _bitable_fields(email: str, name: str) -> dict[str, str]:
    return {
        "record_key": email.strip().lower(),
        "email": email,
        "name": name,
        "permissions": '{"1011":["销售额"]}',
        "status": "approved",
        "updated_at": "2026-09-14T00:00:00Z",
        "token_cipher": CIPHER,
    }


class FakeTransport:
    """假飞书预发表：记录每次调用，支持换令牌、分页列举、按标识读回、删行与建行。"""

    def __init__(self, rows: dict[str, dict[str, str]]) -> None:
        self.rows = {key: dict(value) for key, value in rows.items()}
        self.calls: list[tuple[str, str]] = []
        self.created = 0

    def __call__(self, method: str, url: str, *, body: Any, headers: Any) -> Any:
        self.calls.append((method, url))
        path = urlsplit(url).path
        if method == "POST" and path.endswith("/auth/v3/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "tenant-token-sentinel"}
        assert headers.get("Authorization") == "Bearer tenant-token-sentinel", headers
        if method == "GET" and path.endswith("/records"):
            items = [
                {"record_id": record_id, "fields": dict(fields)}
                for record_id, fields in sorted(self.rows.items())
            ]
            return {"code": 0, "data": {"items": items, "has_more": False}}
        if method == "POST" and path.endswith("/records"):
            self.created += 1
            record_id = f"recCreated{self.created}"
            self.rows[record_id] = dict(body["fields"])
            return {
                "code": 0,
                "data": {"record": {"record_id": record_id, "fields": body["fields"]}},
            }
        record_id = path.rsplit("/", 1)[1]
        if method == "GET":
            if record_id not in self.rows:
                return TOOL.HttpResponse(404, {"code": 1254005, "msg": "RecordIdNotFound"})
            return {
                "code": 0,
                "data": {"record": {"record_id": record_id, "fields": self.rows[record_id]}},
            }
        if method == "DELETE":
            self.rows.pop(record_id, None)
            return {"code": 0, "data": {"deleted": True, "record_id": record_id}}
        raise AssertionError(f"未预期的调用 {method} {url}")

    def count(self, method: str) -> int:
        return sum(1 for called, url in self.calls if called == method and "/records" in url)


@unittest.skipUnless(os.environ.get("LINGXI_POSTGRES_DSN") and psycopg_available(), SKIP_REASON)
class StageUserResetPostgresTestCase(unittest.TestCase):
    """整链建库、清行，种一位目标用户与一位旁观用户的完整足迹，外加审计类行。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dsn = os.environ["LINGXI_POSTGRES_DSN"]
        ensure_production_schema(cls._dsn)

    def setUp(self) -> None:
        from lingxi.adapters.postgres import connect

        reset_production_rows(self._dsn)
        self._connection = connect(self._dsn, dedicated=True, autocommit=True)
        self.addCleanup(self._connection.close)
        self.execute(
            "INSERT INTO pending_action(id,action_type,target_open_id,initiated_by_open_id,"
            "target_state_snapshot,status,created_at,confirm_deadline_at,decided_at,"
            "decided_by_open_id,payload) VALUES ('pac_reset','local_permission_grant',"
            "'ou_target_any','ou_admin_fake','enabled','executed',now(),now()+interval '1 hour',"
            "now(),'ou_admin_fake','{\"synthetic\":true}')"
        )
        self.seed_user(USER_ID, OPEN_ID, EMAIL, "目标用户")
        self.seed_user(OTHER_USER_ID, OTHER_OPEN_ID, OTHER_EMAIL, "旁观用户")
        self.execute(
            "INSERT INTO operation_audit(id,operation_id,operation,phase,initiated_by,actor_roles,"
            "entry_point,target_user_id) VALUES ('oa_1','op_1','preprovision.apply','prepared',"
            "'ou_admin_fake','',"
            "'ops_script',%s)",
            (USER_ID,),
        )
        self.execute(
            "INSERT INTO onboarding_failure(trace_id,failure_reason,event_type) VALUES"
            " ('trc_reset','no_permission','onboarding.result')"
        )
        self.transport = FakeTransport(
            {
                RECORD_ID: _bitable_fields(EMAIL, "目标用户"),
                OTHER_RECORD_ID: _bitable_fields(OTHER_EMAIL, "旁观用户"),
            }
        )

    # ---------------------------------------------------------------- 夹具

    def execute(self, sql: str, parameters: tuple = ()) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)

    def fetch(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        with self._connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            return list(cursor.fetchall())

    def count(self, table: str, where: str = "TRUE", parameters: tuple = ()) -> int:
        return int(self.fetch(f'SELECT count(*) FROM "{table}" WHERE {where}', parameters)[0][0])

    def seed_user(self, user_id: str, open_id: str, email: str, name: str) -> None:
        """一位已开通用户的最小足迹：app_user + 十五张派生表 + 令牌库 + 内测资格。"""
        suffix = user_id.removeprefix("usr_")
        self.execute(
            "INSERT INTO app_user(id,feishu_open_id,feishu_user_id,feishu_union_id,display_name,"
            "department,tenant_key,email,provisioning_state,permission_record_id,"
            "permission_version) VALUES (%s,%s,%s,%s,%s,'数据部','tk_fake',%s,'active',%s,1)",
            (user_id, open_id, f"u_{suffix}", f"un_{suffix}", name, email, f"rec_{suffix}"),
        )
        self.execute(
            "INSERT INTO conversation(id,user_id,feishu_chat_id) VALUES (%s,%s,%s)",
            (f"cnv_{suffix}", user_id, f"oc_{suffix}"),
        )
        self.execute(
            "INSERT INTO task(id,conversation_id,user_id,inbound_event_id,prompt,status,"
            "target_worker_version) VALUES (%s,%s,%s,%s,%s,'succeeded','stable')",
            (f"tsk_{suffix}", f"cnv_{suffix}", user_id, f"evt_{suffix}", PROMPT),
        )
        self.execute(
            "INSERT INTO task_delivery_event(id,task_id,sequence,event_type,terminal_kind,"
            "worker_id,idempotency_key,expires_at) VALUES (%s,%s,1,'terminal','success',"
            "'worker-1',%s,now())",
            (f"tde_{suffix}", f"tsk_{suffix}", f"idem_{suffix}"),
        )
        self.execute(
            "INSERT INTO task_document_delivery_request(id,task_id,requester_open_id,title,"
            "paragraphs,content_expires_at) VALUES (%s,%s,%s,'合成标题','[\"合成段落\"]',now())",
            (f"tdr_{suffix}", f"tsk_{suffix}", open_id),
        )
        self.execute(
            "INSERT INTO innertest_content_capture(id,task_id,worker_id,question_content,"
            "answer_content,expires_at) VALUES (%s,%s,'worker-1',%s,%s,now())",
            (f"icc_{suffix}", f"tsk_{suffix}", PROMPT, ANSWER),
        )
        self.execute(
            "INSERT INTO qa_corpus(id,task_id,conversation_id,user_id,question_content,"
            "answer_delivered,answer_model_raw,terminal_kind,user_result,worker_id,"
            "worker_version,target_worker_version) VALUES (%s,%s,%s,%s,%s,%s,%s,'success',"
            "'obtained','worker-1','2.5.0','stable')",
            (f"qac_{suffix}", f"tsk_{suffix}", f"cnv_{suffix}", user_id, PROMPT, ANSWER, ANSWER),
        )
        self.execute(
            "INSERT INTO agent_session_cleanup(id,user_id,agent_session_id,reason) VALUES"
            " (%s,%s,%s,'new_command')",
            (f"asc_{suffix}", user_id, f"ses_{suffix}"),
        )
        self.execute(
            "INSERT INTO innertest_check(id,user_id,permission_version,publish_version,started_at,"
            "finished_at,result_code,trace_id) VALUES (%s,%s,1,1,now(),now(),'ok',%s)",
            (f"ick_{suffix}", user_id, f"trc_{suffix}"),
        )
        self.execute(
            "INSERT INTO local_permission_override(id,user_id,direction,company_id,metric_name,"
            "reason,initiated_by_open_id,pending_action_id) VALUES (%s,%s,'grant','1011','销售额',"
            "'合成','ou_admin_fake','pac_reset')",
            (f"lpo_{suffix}", user_id),
        )
        self.execute(
            "INSERT INTO mcp_access_token(user_id,token_cipher) VALUES (%s,%s)",
            (user_id, DB_CIPHER),
        )
        self.execute(
            "INSERT INTO mcp_sync_check(id,user_id,permission_version,attempt_no,result,"
            "metric_count,content_expires_at) VALUES (%s,%s,1,1,'ready',3,now())",
            (f"msc_{suffix}", user_id),
        )
        self.execute(
            "INSERT INTO onboarding_completion_notice(id,user_id,permission_version,company_name,"
            "function_name,content_expires_at,dedupe_key) VALUES (%s,%s,1,'A公司','销售',now(),%s)",
            (f"ocn_{suffix}", user_id, f"dk_{suffix}"),
        )
        self.execute(
            "INSERT INTO outreach_message(id,recipient_open_id,user_id,purpose,content_key,"
            "content_version,card_style,status,delivered_at,dedupe_key) VALUES (%s,%s,%s,'apply',"
            "'welcome','v1','card','delivered',now(),%s)",
            (f"orm_{suffix}", open_id, user_id, f"odk_{suffix}"),
        )
        self.execute(
            "INSERT INTO publish_outbox(id,user_id,permission_version,reason,payload,status,"
            "published_at,external_record_id) VALUES (%s,%s,1,'synthetic',%s,'published',now(),%s)",
            (f"pub_{suffix}", user_id, json.dumps({"token_cipher": CIPHER}), f"rec_{suffix}"),
        )
        self.execute(
            "INSERT INTO user_memory(id,user_id,memory_type,memory_key,memory_value) VALUES"
            " (%s,%s,'term_mapping','口径','合成记忆')",
            (f"mem_{suffix}", user_id),
        )
        self.execute(
            "INSERT INTO innertest_membership(scope,open_id,email) VALUES (%s,%s,%s)",
            (SCOPE, open_id, email),
        )
        self.execute(
            "INSERT INTO admin_action_followup(id,pending_action_id,subject_key,stage,"
            "target_user_id) VALUES (%s,'pac_reset',%s,'permission_recompute',%s)",
            (f"aaf_{suffix}", f"subject_{suffix}", user_id),
        )

    def run_main(
        self, *argv: str, environment: dict[str, str] | None = None, stdin: str = ""
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = TOOL.main(
                list(argv),
                environment=environment or _environment(),
                transport=self.transport,
                stdin=io.StringIO(stdin),
            )
        return code, out.getvalue(), err.getvalue()

    def identity_args(self) -> tuple[str, ...]:
        return ("--email", EMAIL, "--open-id", OPEN_ID)

    def summary_from(self, output: str) -> str:
        for line in output.splitlines():
            if line.startswith("summary_sha256="):
                return line.partition("=")[2]
        raise AssertionError(f"干跑输出没有 summary_sha256：\n{output}")

    def footprint_counts(self, user_id: str, open_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for spec in TOOL.DELETE_ORDER:
            if spec.locate == "open_id":
                counts[spec.name] = self.count(spec.name, "open_id = %s", (open_id,))
            elif spec.locate == "self":
                counts[spec.name] = self.count(spec.name, "id = %s", (user_id,))
            elif spec.locate == "task":
                counts[spec.name] = self.count(
                    spec.name,
                    "task_id IN (SELECT id FROM task WHERE user_id = %s)",
                    (user_id,),
                )
            else:
                counts[spec.name] = self.count(spec.name, "user_id = %s", (user_id,))
        return counts

    def audit_counts(self) -> dict[str, int]:
        return {table: self.count(table) for table in AUDIT_TABLES}

    def drop_retained_rows(self) -> None:
        """把目标用户的两张留存内容表清空，让默认姿势可执行。"""
        self.execute("DELETE FROM qa_corpus WHERE user_id = %s", (USER_ID,))
        self.execute(
            "DELETE FROM innertest_content_capture WHERE task_id IN"
            " (SELECT id FROM task WHERE user_id = %s)",
            (USER_ID,),
        )

    # ---------------------------------------------------------------- a. plan

    def test_plan_is_read_only_stable_and_free_of_secrets(self) -> None:
        before = self.footprint_counts(USER_ID, OPEN_ID)
        code, first, _ = self.run_main("plan", *self.identity_args())
        self.assertEqual(code, TOOL.EXIT_DONE, first)
        code, second, _ = self.run_main("plan", *self.identity_args())
        self.assertEqual(code, TOOL.EXIT_DONE)
        self.assertEqual(self.summary_from(first), self.summary_from(second))
        self.assertEqual(self.footprint_counts(USER_ID, OPEN_ID), before)
        self.assertEqual(self.transport.count("DELETE"), 0)
        for secret in (
            CIPHER,
            DB_CIPHER,
            PROMPT,
            ANSWER,
            "app-secret-sentinel",
            "tenant-token-sentinel",
        ):
            self.assertNotIn(secret, first)
        self.assertIn(f"user_id={USER_ID}", first)
        self.assertIn("表 mcp_access_token：1 行", first)
        self.assertIn(f"预发权限发布表：1 行 record={RECORD_ID}", first)
        self.assertIn("外键置空 admin_action_followup.target_user_id：1 行", first)
        # 留存内容有行：默认被拦；裁定随删后摘要随选项变化
        self.assertIn("结论：被拦", first)
        self.assertIn("qa_corpus 1 行", first)
        self.assertIn("innertest_content_capture 1 行", first)
        code, forced, _ = self.run_main("plan", *self.identity_args(), "--delete-retained-content")
        self.assertEqual(code, TOOL.EXIT_DONE)
        self.assertIn("结论：可执行", forced)
        self.assertNotEqual(self.summary_from(first), self.summary_from(forced))

    def test_plan_summary_changes_when_a_row_changes(self) -> None:
        _, first, _ = self.run_main("plan", *self.identity_args())
        self.execute(
            "UPDATE publish_outbox SET attempts = attempts + 1 WHERE id = 'pub_reset_target'"
        )
        _, second, _ = self.run_main("plan", *self.identity_args())
        self.assertNotEqual(self.summary_from(first), self.summary_from(second))

    def test_identity_mismatch_is_rejected_before_any_read_of_the_table(self) -> None:
        code, _, err = self.run_main("plan", "--email", EMAIL, "--open-id", OTHER_OPEN_ID)
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
        self.assertIn("不是同一条 app_user 记录", err)
        self.assertEqual(self.transport.calls, [])

    def test_unregistered_referencing_table_is_refused(self) -> None:
        self.execute(
            "CREATE TABLE synthetic_child(id text PRIMARY KEY,"
            " user_id text REFERENCES app_user(id) ON DELETE CASCADE)"
        )
        try:
            code, _, err = self.run_main("plan", *self.identity_args())
        finally:
            self.execute("DROP TABLE synthetic_child")
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
        self.assertIn("synthetic_child → app_user", err)

    # ---------------------------------------------------------------- b. backup

    def test_backup_json_is_complete_and_matches_plan(self) -> None:
        _, plan, _ = self.run_main("plan", *self.identity_args(), "--delete-retained-content")
        code, out, err = self.run_main("backup", *self.identity_args(), "--delete-retained-content")
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        payload = json.loads(out)
        self.assertEqual(payload["schema"], TOOL.SCHEMA_VERSION)
        self.assertEqual(payload["environment"], "stage")
        self.assertEqual(payload["email"], NORMALIZED_EMAIL)
        self.assertEqual(payload["open_id"], OPEN_ID)
        self.assertEqual(payload["user_id"], USER_ID)
        self.assertEqual(payload["summary_sha256"], self.summary_from(plan))
        expected = self.footprint_counts(USER_ID, OPEN_ID)
        self.assertEqual({name: len(rows) for name, rows in payload["tables"].items()}, expected)
        self.assertTrue(all(count == 1 for count in expected.values()), expected)
        self.assertEqual(payload["tables"]["mcp_access_token"][0]["token_cipher"], DB_CIPHER)
        self.assertEqual(payload["tables"]["task"][0]["prompt"], PROMPT)
        self.assertEqual(
            payload["relink"]["admin_action_followup"],
            [{"id": "aaf_reset_target", "target_user_id": USER_ID}],
        )
        self.assertEqual(payload["bitable_row"]["record_id"], RECORD_ID)
        self.assertEqual(payload["bitable_row"]["fields"]["token_cipher"], CIPHER)
        self.assertIn("备份完成", err)
        self.assertNotIn(CIPHER, err)

    def test_backup_without_the_flag_leaves_retained_rows_out(self) -> None:
        code, out, err = self.run_main("backup", *self.identity_args())
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        payload = json.loads(out)
        self.assertNotIn("qa_corpus", payload["tables"])
        self.assertNotIn("innertest_content_capture", payload["tables"])
        self.assertIn("备份未含留存内容表", err)

    # ---------------------------------------------------------------- c. apply

    def test_apply_refuses_a_wrong_summary_with_zero_writes(self) -> None:
        self.drop_retained_rows()
        before = self.footprint_counts(USER_ID, OPEN_ID)
        code, _, err = self.run_main("apply", *self.identity_args(), "--confirm", "0" * 64)
        self.assertEqual(code, TOOL.EXIT_SUMMARY_MISMATCH, err)
        self.assertIn("零写入", err)
        self.assertEqual(self.footprint_counts(USER_ID, OPEN_ID), before)
        self.assertEqual(self.transport.count("DELETE"), 0)
        self.assertIn(RECORD_ID, self.transport.rows)

    def test_apply_refuses_when_rows_changed_after_plan(self) -> None:
        self.drop_retained_rows()
        _, plan, _ = self.run_main("plan", *self.identity_args())
        summary = self.summary_from(plan)
        self.execute(
            "INSERT INTO user_memory(id,user_id,memory_type,memory_key,memory_value) VALUES"
            " ('mem_late',%s,'term_mapping','晚到','合成')",
            (USER_ID,),
        )
        before = self.footprint_counts(USER_ID, OPEN_ID)
        code, _, err = self.run_main("apply", *self.identity_args(), "--confirm", summary)
        self.assertEqual(code, TOOL.EXIT_SUMMARY_MISMATCH, err)
        self.assertEqual(self.footprint_counts(USER_ID, OPEN_ID), before)
        self.assertEqual(self.transport.count("DELETE"), 0)

    def test_apply_is_blocked_by_retained_content_until_the_flag_is_given(self) -> None:
        _, plan, _ = self.run_main("plan", *self.identity_args())
        code, _, err = self.run_main(
            "apply", *self.identity_args(), "--confirm", self.summary_from(plan)
        )
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE, err)
        self.assertIn("留存内容表有行", err)
        self.assertEqual(self.count("app_user", "id = %s", (USER_ID,)), 1)
        self.assertEqual(self.transport.count("DELETE"), 0)

    def test_apply_with_the_right_summary_removes_exactly_the_target(self) -> None:
        self.drop_retained_rows()
        audit_before = self.audit_counts()
        bystander_before = self.footprint_counts(OTHER_USER_ID, OTHER_OPEN_ID)
        _, plan, _ = self.run_main("plan", *self.identity_args())
        code, _, err = self.run_main(
            "apply", *self.identity_args(), "--confirm", self.summary_from(plan)
        )
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        after = self.footprint_counts(USER_ID, OPEN_ID)
        self.assertEqual(set(after.values()), {0}, after)
        self.assertEqual(self.footprint_counts(OTHER_USER_ID, OTHER_OPEN_ID), bystander_before)
        self.assertEqual(self.audit_counts(), audit_before)
        self.assertEqual(
            self.fetch(
                "SELECT target_user_id FROM admin_action_followup WHERE id='aaf_reset_target'"
            ),
            [(None,)],
        )
        self.assertEqual(
            self.fetch("SELECT target_user_id FROM admin_action_followup WHERE id='aaf_bystander'"),
            [(OTHER_USER_ID,)],
        )
        self.assertEqual(self.count("qa_corpus", "user_id = %s", (OTHER_USER_ID,)), 1)
        self.assertEqual(self.transport.count("DELETE"), 1)
        self.assertNotIn(RECORD_ID, self.transport.rows)
        self.assertIn(OTHER_RECORD_ID, self.transport.rows)
        self.assertIn("已删 mcp_access_token / 1 行", err)
        self.assertIn("已删 预发权限发布表 / 1 行", err)
        self.assertIn("回读：库侧该用户行已不存在；预发权限发布表该行已不存在", err)
        self.assertNotIn(CIPHER, err)
        self.assertNotIn(DB_CIPHER, err)

    def test_apply_with_the_flag_also_removes_retained_content(self) -> None:
        _, plan, _ = self.run_main("plan", *self.identity_args(), "--delete-retained-content")
        code, _, err = self.run_main(
            "apply",
            *self.identity_args(),
            "--delete-retained-content",
            "--confirm",
            self.summary_from(plan),
        )
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        self.assertEqual(self.count("qa_corpus", "user_id = %s", (USER_ID,)), 0)
        self.assertEqual(self.count("innertest_content_capture"), 1)
        self.assertEqual(self.count("qa_corpus"), 1)

    # ---------------------------------------------------------------- d. restore

    def test_restore_reinserts_the_backup_exactly(self) -> None:
        _, backup, _ = self.run_main("backup", *self.identity_args(), "--delete-retained-content")
        payload = json.loads(backup)
        code, _, err = self.run_main(
            "apply",
            *self.identity_args(),
            "--delete-retained-content",
            "--confirm",
            payload["summary_sha256"],
        )
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        code, _, err = self.run_main("restore", *self.identity_args(), stdin=backup)
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        self.assertIn("回退完成", err)
        for table, rows in payload["tables"].items():
            spec = next(spec for spec in TOOL.DELETE_ORDER if spec.name == table)
            for row in rows:
                predicate = " AND ".join(f'"{column}" = %s' for column in spec.key_columns)
                stored = self.fetch(
                    f'SELECT * FROM "{table}" WHERE {predicate}',
                    tuple(row[column] for column in spec.key_columns),
                )
                self.assertEqual(len(stored), 1, (table, row))
        self.assertEqual(
            self.fetch("SELECT token_cipher FROM mcp_access_token WHERE user_id = %s", (USER_ID,)),
            [(DB_CIPHER,)],
        )
        self.assertEqual(
            self.fetch("SELECT prompt, created_at FROM task WHERE id = 'tsk_reset_target'"),
            [(PROMPT, datetime.fromisoformat(payload["tables"]["task"][0]["created_at"]))],
        )
        self.assertEqual(
            self.fetch(
                "SELECT target_user_id FROM admin_action_followup WHERE id='aaf_reset_target'"
            ),
            [(USER_ID,)],
        )
        self.assertEqual(self.footprint_counts(USER_ID, OPEN_ID), {n: 1 for n in payload["tables"]})
        recreated = [
            (record_id, fields)
            for record_id, fields in self.transport.rows.items()
            if fields["record_key"] == NORMALIZED_EMAIL
        ]
        self.assertEqual(len(recreated), 1)
        self.assertNotEqual(recreated[0][0], RECORD_ID)
        self.assertEqual(recreated[0][1], _bitable_fields(EMAIL, "目标用户"))
        # 再回退一次：库里已有该用户，拒绝重复插回
        code, _, err = self.run_main("restore", *self.identity_args(), stdin=backup)
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE, err)
        self.assertIn("不重复插回", err)

    def test_restore_rejects_a_backup_for_another_identity(self) -> None:
        _, backup, _ = self.run_main("backup", *self.identity_args())
        code, _, err = self.run_main(
            "restore", "--email", OTHER_EMAIL, "--open-id", OTHER_OPEN_ID, stdin=backup
        )
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
        self.assertIn("与命令行不一致", err)

    # ---------------------------------------------------------------- e. 硬闸

    def test_gate_refuses_when_permission_table_is_the_formal_table(self) -> None:
        environment = _environment(LINGXI_PERMISSION_BITABLE_APP_TOKEN=FORMAL_APP_TOKEN)
        for command in ("plan", "backup", "restore"):
            with self.subTest(command=command):
                code, out, err = self.run_main(
                    command, *self.identity_args(), environment=environment, stdin="{}"
                )
                self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
                self.assertIn("同一个 Base", err)
                self.assertEqual(out, "")
        code, _, err = self.run_main(
            "apply", *self.identity_args(), "--confirm", "0" * 64, environment=environment
        )
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.count("app_user", "id = %s", (USER_ID,)), 1)

    def test_gate_refuses_when_the_stock_table_variable_is_missing(self) -> None:
        environment = _environment(LINGXI_STOCK_TOKEN_BITABLE_APP_TOKEN="")
        code, _, err = self.run_main("plan", *self.identity_args(), environment=environment)
        self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
        self.assertIn("无法证明预发表与正式表分离", err)
        self.assertEqual(self.transport.calls, [])

    def test_gate_refuses_when_the_environment_declares_production(self) -> None:
        for value in ("prod", " Production ", "生产"):
            with self.subTest(value=value):
                environment = _environment(LINGXI_DEPLOY_ENVIRONMENT=value)
                code, _, err = self.run_main("plan", *self.identity_args(), environment=environment)
                self.assertEqual(code, TOOL.EXIT_NOTHING_DONE)
                self.assertIn("自称生产", err)
        self.assertEqual(self.transport.calls, [])

    # ---------------------------------------------------------------- f. 审计类表

    def test_audit_tables_are_untouched_by_a_full_reset(self) -> None:
        audit_before = {
            table: self.fetch(f'SELECT * FROM "{table}" ORDER BY 1') for table in AUDIT_TABLES
        }
        _, plan, _ = self.run_main("plan", *self.identity_args(), "--delete-retained-content")
        code, _, err = self.run_main(
            "apply",
            *self.identity_args(),
            "--delete-retained-content",
            "--confirm",
            self.summary_from(plan),
        )
        self.assertEqual(code, TOOL.EXIT_DONE, err)
        for table in ("operation_audit", "pending_action", "onboarding_failure"):
            self.assertEqual(self.fetch(f'SELECT * FROM "{table}" ORDER BY 1'), audit_before[table])
        self.assertEqual(
            self.count("admin_action_followup"), len(audit_before["admin_action_followup"])
        )
        self.assertEqual(self.count("inbound_event"), 0)


if __name__ == "__main__":
    unittest.main()
