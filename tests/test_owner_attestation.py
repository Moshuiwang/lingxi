"""Issue #498 GitHub OWNER attestation 的正向与 fail-closed 矩阵。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "github_owner_attestation_under_test",
    ROOT / "scripts" / "ci" / "read_github_owner_attestation.py",
)
assert SPEC is not None and SPEC.loader is not None
READER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = READER
SPEC.loader.exec_module(READER)


def _file_entry(
    filename: str,
    *,
    sha: str = "a" * 40,
    status: str = "modified",
    previous_filename: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "filename": filename,
        "sha": sha,
        "status": status,
        "additions": 1,
        "deletions": 0,
        "changes": 1,
        "blob_url": f"https://github.com/Moshuiwang/lingxi/blob/{sha}/{filename}",
        "raw_url": f"https://github.com/Moshuiwang/lingxi/raw/{sha}/{filename}",
        "contents_url": f"https://api.github.com/repos/Moshuiwang/lingxi/contents/{filename}?ref={sha}",
    }
    if previous_filename is not None:
        entry["previous_filename"] = previous_filename
    return entry


class FakeApi:
    """只模拟官方 REST 返回值；生产 reader 不依赖这份夹具。"""

    def __init__(
        self,
        pr: dict[str, Any],
        comments: list[list[dict[str, Any]]],
        files: list[list[dict[str, Any]]] | None = None,
        *,
        error_at: str | None = None,
        malformed_link: str | None = None,
        repository_links: bool = False,
    ) -> None:
        self.pr = pr
        self.comments = comments
        self.files = files or [[_file_entry("src/lingxi/config/company_function_metric_map.toml")]]
        self.error_at = error_at
        self.malformed_link = malformed_link
        self.repository_links = repository_links
        self.calls: list[str] = []

    def get(self, path: str) -> Any:
        self.calls.append(path)
        if self.error_at and self.error_at in path:
            raise READER.ApiError("synthetic API failure")
        if path.endswith("/pulls/7"):
            value = self.pr
        elif "/pulls/7/files?" in path:
            page = int(path.rsplit("page=", 1)[1])
            value = self.files[page - 1] if page <= len(self.files) else []
        elif "/issues/7/comments?" in path:
            page = int(path.rsplit("page=", 1)[1])
            value = self.comments[page - 1] if page <= len(self.comments) else []
        else:
            raise AssertionError(f"unexpected API path: {path}")
        headers: dict[str, str] = {}
        if "/issues/7/comments?" in path:
            page = int(path.rsplit("page=", 1)[1])
            if self.malformed_link is not None and page == 1:
                headers["Link"] = self.malformed_link
            elif page < len(self.comments):
                link_path = (
                    "/repositories/1309889651/issues/7/comments"
                    if self.repository_links
                    else "/repos/Moshuiwang/lingxi/issues/7/comments"
                )
                headers["Link"] = (
                    f'<https://api.github.com{link_path}'
                    f'?per_page=100&page={page + 1}>; rel="next", '
                    f'<https://api.github.com{link_path}'
                    f'?per_page=100&page={len(self.comments)}>; rel="last"'
                )
        elif "/pulls/7/files?" in path:
            page = int(path.rsplit("page=", 1)[1])
            if page < len(self.files):
                link_path = (
                    "/repositories/1309889651/pulls/7/files"
                    if self.repository_links
                    else "/repos/Moshuiwang/lingxi/pulls/7/files"
                )
                headers["Link"] = (
                    f'<https://api.github.com{link_path}'
                    f'?per_page=100&page={page + 1}>; rel="next", '
                    f'<https://api.github.com{link_path}'
                    f'?per_page=100&page={len(self.files)}>; rel="last"'
                )
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return READER.ApiResponse(value=value, raw=raw, headers=headers, url=f"https://api.github.com{path}")


class OwnerAttestationTest(unittest.TestCase):
    BASE = "a" * 40
    HEAD = "b" * 40
    RUN_SHA = "c" * 40
    NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
    CAPTURED = "2026-09-01T11:50:00+00:00"
    ISSUED = "2026-09-01T11:55:00+00:00"
    EXPIRES = "2026-09-01T12:05:00+00:00"
    COMMENT_TIME = ISSUED

    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = Path(self.tmp)
        manifest_path = self.repository / ".github" / "permission-impact-counts.json"
        manifest_path.parent.mkdir(parents=True)
        self.manifest = {
            "schema": "lingxi.permission-impact-counts/v2",
            "base_facts_sha256": "d" * 64,
            "head_facts_sha256": "e" * 64,
            "grant_surface_sha256": "f" * 64,
            "shrink_surface_sha256": "1" * 64,
            "counts": {"grant": 3, "shrink": 1},
            "source": {
                "kind": "biai-stage-read-only-aggregate-claim",
                "environment": "biai-stage",
                "dataset": "galaxy_user_role",
                "query_version": "permission-impact-users/v1",
                "captured_at": self.CAPTURED,
            },
        }
        manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.manifest_path = manifest_path

    def _pr(self, *, author: dict[str, Any] | None = None, base: str | None = None, head: str | None = None) -> dict[str, Any]:
        return {
            "number": 7,
            "changed_files": 1,
            "base": {
                "sha": base or self.BASE,
                "repo": {"id": READER.REPOSITORY_ID, "full_name": READER.REPOSITORY_FULL_NAME},
            },
            "head": {"sha": head or self.HEAD},
            "user": author or {"login": "contributor", "id": 123, "node_id": "U_other", "type": "User"},
        }

    def _comment(self, *, body: str | None = None, **overrides: Any) -> dict[str, Any]:
        comment = {
            "id": 9001,
            "html_url": "https://github.com/Moshuiwang/lingxi/pull/7#issuecomment-9001",
            "url": "https://api.github.com/repos/Moshuiwang/lingxi/issues/comments/9001",
            "body": body if body is not None else self._body(),
            "created_at": self.COMMENT_TIME,
            "updated_at": self.COMMENT_TIME,
            "author_association": READER.OWNER_ASSOCIATION,
            "minimized": None,
            "performed_via_github_app": None,
            "user": {
                "login": READER.OWNER_LOGIN,
                "id": READER.OWNER_ID,
                "node_id": READER.OWNER_NODE_ID,
                "type": READER.OWNER_TYPE,
            },
        }
        comment.update(overrides)
        return comment

    def _body(self, **overrides: Any) -> str:
        document: dict[str, Any] = {
            "schema": READER.ATTESTATION_SCHEMA,
            "repository": {"id": READER.REPOSITORY_ID, "full_name": READER.REPOSITORY_FULL_NAME},
            "pull_request": {"number": 7, "base_sha": self.BASE, "head_sha": self.HEAD},
            "manifest_sha256": READER._canonical_digest(self.manifest),
            "base_facts_sha256": self.manifest["base_facts_sha256"],
            "head_facts_sha256": self.manifest["head_facts_sha256"],
            "grant_surface_sha256": self.manifest["grant_surface_sha256"],
            "shrink_surface_sha256": self.manifest["shrink_surface_sha256"],
            "counts": {"grant": 3, "shrink": 1},
            "exporter": {"commit": "1" * 40, "blob": "2" * 40},
            "query_version": "permission-impact-users/v1",
            "captured_at": self.CAPTURED,
            "issued_at": self.ISSUED,
            "expires_at": self.EXPIRES,
            "nonce": "nonce-0123456789ab",
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(document.get(key), dict):
                document[key] = {**document[key], **value}
            else:
                document[key] = value
        return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _read(
        self,
        *,
        api: FakeApi | None = None,
        comment: dict[str, Any] | None = None,
        pr: dict[str, Any] | None = None,
        files: list[list[dict[str, Any]]] | None = None,
        run_id: int = 12345,
        run_sha: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        fake = api or FakeApi(pr or self._pr(), [[comment or self._comment()]], files)
        return READER.read_attestation(
            fake,
            repository=self.repository,
            pr_number=7,
            base_sha=self.BASE,
            head_sha=self.HEAD,
            manifest_path=self.manifest_path,
            run_id=run_id,
            run_sha=run_sha or self.RUN_SHA,
            now=self.NOW,
        )

    def test_owner_comment_is_converted_to_existing_provenance_and_public_evidence(self) -> None:
        comment = self._comment()
        attestation, provenance, evidence = self._read(comment=comment)
        self.assertEqual(attestation["schema"], READER.ATTESTATION_SCHEMA)
        self.assertEqual(provenance["schema"], READER.PROVENANCE_SCHEMA)
        self.assertEqual(provenance["source"]["kind"], READER.PROVENANCE_SOURCE)
        self.assertEqual(set(provenance), READER.PROVENANCE_KEYS)
        self.assertEqual(set(provenance["attestation"]), READER.PROVENANCE_ATTESTATION_KEYS)
        self.assertEqual(set(evidence), READER.EVIDENCE_KEYS)
        self.assertEqual(set(evidence["comment"]), READER.EVIDENCE_COMMENT_KEYS)
        self.assertEqual(provenance["attestation"]["comment_id"], 9001)
        self.assertEqual(provenance["attestation"]["user_id"], READER.OWNER_ID)
        self.assertEqual(evidence["pr_mode"], "regular-l3")
        self.assertEqual(evidence["run_id"], 12345)
        self.assertEqual(evidence["run_sha"], self.RUN_SHA)
        self.assertEqual(evidence["comment"]["user_id"], READER.OWNER_ID)
        self.assertEqual(evidence["comment"]["body"], comment["body"])
        self.assertEqual(
            set(json.loads(evidence["comment"]["body"])),
            READER.ATTESTATION_KEYS,
        )
        self.assertNotIn("user_id", evidence["comment"]["body"])
        self.assertNotIn("token", evidence["comment"]["body"].lower())
        self.assertEqual(len(evidence["comment"]["body_sha256"]), 64)
        self.assertEqual(len(evidence["api_response_sha256"]), 64)
        self.assertEqual(len(evidence["challenge_sha256"]), 64)
        self.assertEqual(
            evidence["challenge_sha256"],
            READER._run_challenge_digest(
                repository=READER.REPOSITORY_FULL_NAME,
                pr_number=7,
                base_sha=self.BASE,
                head_sha=self.HEAD,
                run_id=12345,
                run_sha=self.RUN_SHA,
                pr_mode="regular-l3",
                comment_id=9001,
                body_sha256=evidence["comment"]["body_sha256"],
                api_response_sha256=evidence["api_response_sha256"],
            ),
        )
        self.assertNotIn("GITHUB_TOKEN", json.dumps(evidence))

    def test_run_challenge_changes_with_trusted_run_identity(self) -> None:
        _, provenance_a, evidence_a = self._read(run_id=12345)
        _, provenance_b, evidence_b = self._read(run_id=12346)
        self.assertNotEqual(evidence_a["challenge_sha256"], evidence_b["challenge_sha256"])
        self.assertNotEqual(
            provenance_a["attestation"]["challenge_sha256"],
            provenance_b["attestation"]["challenge_sha256"],
        )

    def test_pre_generated_payload_may_be_commented_one_or_several_seconds_later(self) -> None:
        for delay in (1, 3):
            with self.subTest(delay=delay):
                created_at = f"2026-09-01T11:55:{delay:02d}+00:00"
                self._read(comment=self._comment(created_at=created_at, updated_at=created_at))

    def test_comment_must_follow_issued_at_and_be_at_or_before_now(self) -> None:
        too_early = self._comment(
            body=self._body(issued_at="2026-09-01T11:55:01+00:00")
        )
        with self.assertRaises(READER.AttestationError):
            self._read(comment=too_early)
        future = self._comment(
            created_at="2026-09-01T12:00:01+00:00",
            updated_at="2026-09-01T12:00:01+00:00",
        )
        with self.assertRaises(READER.AttestationError):
            self._read(comment=future)
        too_late = self._comment(
            body=self._body(
                issued_at="2026-09-01T11:30:00+00:00",
                expires_at="2026-09-01T11:40:00+00:00",
            ),
            created_at="2026-09-01T11:35:00+00:00",
            updated_at="2026-09-01T11:35:00+00:00",
        )
        with self.assertRaises(READER.AttestationError):
            self._read(comment=too_late)

    def test_wrong_author_id_node_type_and_association_fail_closed(self) -> None:
        cases = [
            {"login": "other", "id": READER.OWNER_ID, "node_id": READER.OWNER_NODE_ID, "type": "User"},
            {"login": READER.OWNER_LOGIN, "id": 999, "node_id": READER.OWNER_NODE_ID, "type": "User"},
            {"login": READER.OWNER_LOGIN, "id": READER.OWNER_ID, "node_id": "U_other", "type": "User"},
            {"login": READER.OWNER_LOGIN, "id": READER.OWNER_ID, "node_id": READER.OWNER_NODE_ID, "type": "Bot"},
        ]
        for user in cases:
            with self.subTest(user=user):
                with self.assertRaises(READER.AttestationError):
                    self._read(comment=self._comment(user=user))
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(author_association="CONTRIBUTOR"))

    def test_pr_author_must_differ_from_owner(self) -> None:
        owner = {"login": READER.OWNER_LOGIN, "id": READER.OWNER_ID, "node_id": READER.OWNER_NODE_ID, "type": "User"}
        with self.assertRaises(READER.AttestationError):
            self._read(pr=self._pr(author=owner))
        for collision in (
            {**owner, "login": "renamed"},
            {**owner, "id": 999},
            {**owner, "node_id": "U_other"},
        ):
            with self.subTest(collision=collision), self.assertRaises(READER.AttestationError):
                self._read(pr=self._pr(author=collision))
        with self.assertRaises(READER.AttestationError):
            self._read(api=FakeApi(self._pr(author={"login": "contributor"}), [[self._comment()]]))

    def test_repository_trust_root_cannot_be_overridden(self) -> None:
        with self.assertRaises(READER.AttestationError):
            READER.read_attestation(
                FakeApi(self._pr(), [[self._comment()]]),
                repository=self.repository,
                repository_id=99,
                pr_number=7,
                base_sha=self.BASE,
                head_sha=self.HEAD,
                manifest_path=self.manifest_path,
                run_id=12345,
                run_sha=self.RUN_SHA,
                now=self.NOW,
            )

    def test_app_and_minimized_or_edited_comment_fail(self) -> None:
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(performed_via_github_app={"id": 1}))
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(minimized=True))
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(updated_at="2026-09-01T11:56:00+00:00"))

    def test_old_base_head_and_repository_bindings_fail(self) -> None:
        with self.assertRaises(READER.AttestationError):
            self._read(pr=self._pr(base="1" * 40))
        with self.assertRaises(READER.AttestationError):
            self._read(pr=self._pr(head="1" * 40))
        wrong_repo = self._comment(body=self._body(repository={"id": 99}))
        with self.assertRaises(READER.AttestationError):
            self._read(comment=wrong_repo)
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(body=self._body(pull_request={"number": 8})))

    def test_digest_and_count_bindings_fail(self) -> None:
        mutations = [
            {"manifest_sha256": "0" * 64},
            {"base_facts_sha256": "0" * 64},
            {"head_facts_sha256": "0" * 64},
            {"grant_surface_sha256": "0" * 64},
            {"shrink_surface_sha256": "0" * 64},
            {"counts": {"grant": 9}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(READER.AttestationError):
                    self._read(comment=self._comment(body=self._body(**mutation)))

    def test_unknown_fields_and_markdown_wrapping_fail(self) -> None:
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(body=self._body(extra="nope")))
        wrapped = "```json\n" + self._body() + "\n```"
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(body=wrapped))
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(body=chr(160) + self._body()))
        duplicate = self._body().replace(
            '"schema":"lingxi.permission-impact-attestation/v1",',
            '"schema":"lingxi.permission-impact-attestation/v1","schema":"lingxi.permission-impact-attestation/v1",',
        )
        with self.assertRaises(READER.AttestationError):
            self._read(comment=self._comment(body=duplicate))

    def test_expired_future_and_long_ttl_fail(self) -> None:
        for mutation in (
            {"expires_at": "2026-09-01T11:59:00+00:00"},
            {"issued_at": "2026-09-01T12:01:00+00:00"},
            {"captured_at": "2026-09-01T12:01:00+00:00"},
            {"expires_at": "2026-09-01T12:16:00+00:00"},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(READER.AttestationError):
                    self._read(comment=self._comment(body=self._body(**mutation)))

    def test_nonce_must_be_present_and_unguessable_shape(self) -> None:
        for nonce in ("", "short", "含中文", "x" * 129):
            with self.subTest(nonce=nonce):
                with self.assertRaises(READER.AttestationError):
                    self._read(comment=self._comment(body=self._body(nonce=nonce)))

    def test_duplicate_valid_comments_and_pagination_duplicate_fail(self) -> None:
        second = self._comment(body=self._body(nonce="nonce-9876543210ab"), id=9002)
        with self.assertRaises(READER.AttestationError):
            self._read(api=FakeApi(self._pr(), [[self._comment()], [second]]))
        duplicate = self._comment()
        with self.assertRaises(READER.ApiError):
            self._read(api=FakeApi(self._pr(), [[self._comment()], [duplicate]]))

    def test_official_repository_id_pagination_links_are_accepted(self) -> None:
        second = self._comment(
            body="ordinary",
            id=9002,
            author_association="CONTRIBUTOR",
            user={"login": "contributor", "id": 123, "node_id": "U_other", "type": "User"},
            url="https://api.github.com/repos/Moshuiwang/lingxi/issues/comments/9002",
            html_url="https://github.com/Moshuiwang/lingxi/pull/7#issuecomment-9002",
        )
        api = FakeApi(
            {**self._pr(), "changed_files": 2},
            [[self._comment()], [second]],
            files=[
                [_file_entry("src/lingxi/config/company_function_metric_map.toml")],
                [_file_entry("README.md")],
            ],
            repository_links=True,
        )
        self._read(api=api)
        self.assertIn("page=2", api.calls[-1])

    def test_pull_files_use_official_shape_and_endpoint_key_without_id(self) -> None:
        renamed = _file_entry(
            "src/lingxi/config/new.toml",
            sha="b" * 40,
            status="renamed",
            previous_filename="src/lingxi/config/old.toml",
        )
        paths, _, count = READER._changed_files(
            FakeApi(self._pr(), [[self._comment()]], files=[[renamed]]),
            pr_number=7,
            repository_full_name=READER.REPOSITORY_FULL_NAME,
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            paths,
            {"src/lingxi/config/new.toml", "src/lingxi/config/old.toml"},
        )

    def test_pull_files_status_allowlist_matches_real_rest_values(self) -> None:
        for offset, status in enumerate(sorted(READER.FILE_STATUSES)):
            with self.subTest(status=status):
                paths, _, count = READER._changed_files(
                    FakeApi(
                        self._pr(),
                        [[self._comment()]],
                        files=[[_file_entry(f"{status}.txt", status=status, sha=f"{offset + 1:040x}")]],
                    ),
                    pr_number=7,
                    repository_full_name=READER.REPOSITORY_FULL_NAME,
                )
                self.assertEqual(paths, {f"{status}.txt"})
                self.assertEqual(count, 1)

        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(
                    self._pr(),
                    [[self._comment()]],
                    files=[[_file_entry("deleted.txt", status="deleted")]],
                ),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )

    def test_pull_files_bad_shape_duplicate_pagination_and_api_failure_fail_closed(self) -> None:
        bad_shape = _file_entry("README.md")
        del bad_shape["sha"]
        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(self._pr(), [[self._comment()]], files=[[bad_shape]]),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )
        duplicate = [
            _file_entry("README.md", sha="a" * 40),
            _file_entry("README.md", sha="b" * 40),
        ]
        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(self._pr(), [[self._comment()]], files=[duplicate]),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )
        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(
                    self._pr(),
                    [[self._comment()]],
                    files=[[_file_entry("README.md")], [_file_entry("README.md")]],
                ),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )
        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(
                    self._pr(),
                    [[self._comment()]],
                    files=[
                        [_file_entry("README.md", sha="a" * 40)],
                        [_file_entry("README.md", sha="b" * 40)],
                    ],
                ),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )
        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(
                    self._pr(),
                    [[self._comment()]],
                    files=[[_file_entry("README.md")], [_file_entry("README.md")]],
                    error_at="files",
                ),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )

    def test_changed_files_count_uses_pr_response_and_rejects_truncation(self) -> None:
        valid = READER._validate_pr_response(
            self._pr(),
            repository_id=READER.REPOSITORY_ID,
            repository_full_name=READER.REPOSITORY_FULL_NAME,
            pr_number=7,
            base_sha=self.BASE,
            head_sha=self.HEAD,
        )
        self.assertEqual(valid["changed_files"], 1)
        with self.assertRaises(READER.AttestationError):
            READER._validate_pr_response(
                {**self._pr(), "changed_files": 3001},
                repository_id=READER.REPOSITORY_ID,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
                pr_number=7,
                base_sha=self.BASE,
                head_sha=self.HEAD,
            )
        for value in (True, 1.5, "1", None):
            with self.subTest(value=value), self.assertRaises(READER.AttestationError):
                READER._validate_pr_response(
                    {**self._pr(), "changed_files": value},
                    repository_id=READER.REPOSITORY_ID,
                    repository_full_name=READER.REPOSITORY_FULL_NAME,
                    pr_number=7,
                    base_sha=self.BASE,
                    head_sha=self.HEAD,
                )

        three_thousand = [
            [_file_entry(f"file-{offset + index}.txt", sha=f"{offset + index + 1:040x}") for index in range(100)]
            for offset in range(0, 3000, 100)
        ]
        _, _, count = READER._changed_files(
            FakeApi(self._pr(), [[self._comment()]], files=three_thousand),
            pr_number=7,
            repository_full_name=READER.REPOSITORY_FULL_NAME,
        )
        self.assertEqual(count, 3000)
        three_thousand_one = three_thousand + [[_file_entry("file-3000.txt", sha="b" * 40)]]
        with self.assertRaises(READER.ApiError):
            READER._changed_files(
                FakeApi(self._pr(), [[self._comment()]], files=three_thousand_one),
                pr_number=7,
                repository_full_name=READER.REPOSITORY_FULL_NAME,
            )
        with self.assertRaises(READER.AttestationError):
            self._read(
                api=FakeApi(
                    {**self._pr(), "changed_files": 2},
                    [[self._comment()]],
                )
            )

    def test_api_failure_or_malformed_pagination_fails_closed(self) -> None:
        with self.assertRaises(READER.ApiError):
            self._read(api=FakeApi(self._pr(), [[self._comment()]], error_at="comments"))
        malformed = FakeApi(
            self._pr(),
            [[self._comment()]],
            malformed_link='<https://evil.example.test/comments?per_page=100&page=2>; rel="next"',
        )
        with self.assertRaises(READER.ApiError):
            self._read(api=malformed)

    def test_missing_comment_metadata_fails_closed(self) -> None:
        for field in ("performed_via_github_app", "minimized", "url", "html_url"):
            comment = self._comment()
            del comment[field]
            with self.subTest(field=field), self.assertRaises(READER.ApiError):
                self._read(comment=comment)

    def test_same_pr_modifying_verifier_or_workflow_is_rejected(self) -> None:
        for path in (
            ".github/workflows/ci.yml",
            "scripts/ci/read_github_owner_attestation.py",
            "scripts/ci/prepare_permission_impact_counts.py",
            "scripts/ci/check_permission_impact.py",
            "scripts/ops/export_permission_impact_counts.py",
        ):
            with self.subTest(path=path):
                with self.assertRaises(READER.AttestationError):
                    self._read(files=[[_file_entry(path)]])

    def test_permission_pr_allowlist_rejects_runtime_deploy_and_other_configuration(self) -> None:
        for path in (
            "src/lingxi/core/permission/publish.py",
            "deploy/compose.prod.yaml",
            ".github/other-workflow.yml",
            "src/lingxi/config/content.toml",
        ):
            with self.subTest(path=path), self.assertRaises(READER.AttestationError):
                self._read(files=[[_file_entry(path)]])
        self._read(files=[[_file_entry("tests/test_owner_attestation.py")]])
        self._read(files=[[_file_entry("docs/traces/502-rc23清仓批/验收.md")]])

    def test_evidence_only_sentinel_is_exact_and_hard_nonmerge_mode(self) -> None:
        sentinel = self.repository / READER.EVIDENCE_ONLY_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(READER.EVIDENCE_ONLY_SENTINEL_CONTENT, encoding="utf-8")
        result = self._read(
            files=[[_file_entry(READER.EVIDENCE_ONLY_SENTINEL)]],
        )
        self.assertEqual(result[2]["pr_mode"], "evidence-only")
        sentinel.write_text("not-the-contract\n", encoding="utf-8")
        with self.assertRaises(READER.AttestationError):
            self._read(files=[[_file_entry(READER.EVIDENCE_ONLY_SENTINEL)]])

    def test_evidence_only_sentinel_cannot_hide_runtime_or_deploy_files(self) -> None:
        sentinel = self.repository / READER.EVIDENCE_ONLY_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(READER.EVIDENCE_ONLY_SENTINEL_CONTENT, encoding="utf-8")
        with self.assertRaises(READER.AttestationError):
            self._read(
                files=[
                    [
                        _file_entry(READER.EVIDENCE_ONLY_SENTINEL),
                        _file_entry("deploy/compose.prod.yaml", sha="b" * 40),
                    ]
                ]
            )

    def test_empty_missing_or_non_attestation_comments_fail(self) -> None:
        with self.assertRaises(READER.AttestationError):
            self._read(api=FakeApi(self._pr(), [[]]))
        ordinary = self._comment(body="hello", id=9002)
        with self.assertRaises(READER.AttestationError):
            self._read(api=FakeApi(self._pr(), [[ordinary]]))

    def test_ordinary_edited_comment_is_not_an_attestation_candidate(self) -> None:
        ordinary = self._comment(
            body="hello",
            id=9002,
            url="https://api.github.com/repos/Moshuiwang/lingxi/issues/comments/9002",
            html_url="https://github.com/Moshuiwang/lingxi/pull/7#issuecomment-9002",
            updated_at="2026-09-01T11:59:00+00:00",
        )
        _, _, evidence = self._read(api=FakeApi(self._pr(), [[ordinary, self._comment()]]))
        self.assertEqual(evidence["comment"]["id"], 9001)


if __name__ == "__main__":
    unittest.main()
