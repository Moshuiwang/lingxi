"""Trusted-stage OWNER attestation payload renderer tests."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "owner_attestation_payload_under_test",
    ROOT / "scripts" / "ops" / "render_permission_impact_owner_attestation.py",
)
assert SPEC is not None and SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RENDERER
SPEC.loader.exec_module(RENDERER)


class OwnerAttestationPayloadTest(unittest.TestCase):
    BASE = "a" * 40
    HEAD = "b" * 40
    NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
    CAPTURED = "2026-09-01T11:50:00+00:00"
    ISSUED = "2026-09-01T11:55:00+00:00"

    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = Path(self.tmp)
        self.manifest_path = self.repository / ".github" / "permission-impact-counts.json"
        self.manifest_path.parent.mkdir(parents=True)
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
        self.manifest_path.write_text(
            json.dumps(self.manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def test_renderer_emits_exact_raw_body_for_comment_file(self) -> None:
        body = RENDERER.render(
            manifest_path=self.manifest_path,
            pr_number=518,
            base_sha=self.BASE,
            head_sha=self.HEAD,
            exporter={"commit": "1" * 40, "blob": "2" * 40},
            issued_at=self.ISSUED,
            expires_at="2026-09-01T12:05:00+00:00",
            nonce="nonce-0123456789ab",
            now=self.NOW,
        )
        output = self.repository / "comment-body.json"
        RENDERER._write(output, body)
        raw_bytes = output.read_bytes()
        expected_bytes = (
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.assertEqual(raw_bytes, expected_bytes)
        self.assertEqual(raw_bytes.count(b"\n"), 1)
        raw = raw_bytes.decode("utf-8")
        self.assertEqual(RENDERER.READER._json_document(raw, "body"), body)
        self.assertNotIn("```", raw)
        with self.assertRaises(RENDERER.READER.AttestationError):
            RENDERER.READER._json_document(
                (b"```json\n" + raw_bytes + b"```").decode("utf-8"),
                "wrapped body",
            )
        self.assertEqual(body["repository"]["id"], RENDERER.READER.REPOSITORY_ID)
        self.assertEqual(body["pull_request"]["number"], 518)

    def test_renderer_generates_secure_nonce_and_supports_image_digest(self) -> None:
        body = RENDERER.render(
            manifest_path=self.manifest_path,
            pr_number=518,
            base_sha=self.BASE,
            head_sha=self.HEAD,
            exporter={"image_digest": "sha256:" + "3" * 64},
            issued_at=self.ISSUED,
            expires_at="2026-09-01T12:05:00+00:00",
            now=self.NOW,
        )
        self.assertRegex(body["nonce"], RENDERER.READER.NONCE_RE)
        self.assertEqual(body["exporter"], {"image_digest": "sha256:" + "3" * 64})

    def test_renderer_rejects_invalid_exporter_and_time_window(self) -> None:
        common = {
            "manifest_path": self.manifest_path,
            "pr_number": 518,
            "base_sha": self.BASE,
            "head_sha": self.HEAD,
            "issued_at": self.ISSUED,
            "expires_at": "2026-09-01T12:05:00+00:00",
            "nonce": "nonce-0123456789ab",
            "now": self.NOW,
        }
        with self.assertRaises(RENDERER.READER.AttestationError):
            RENDERER.render(exporter={"commit": "1" * 40}, **common)
        with self.assertRaises(RENDERER.READER.AttestationError):
            RENDERER.render(
                exporter={"commit": "1" * 40, "blob": "2" * 40},
                issued_at="2026-09-01T12:01:00+00:00",
                **{key: value for key, value in common.items() if key != "issued_at"},
            )
        with self.assertRaises(RENDERER.READER.AttestationError):
            RENDERER.render(
                exporter={"commit": "1" * 40, "blob": "2" * 40},
                expires_at="2026-09-01T12:16:00+00:00",
                **{key: value for key, value in common.items() if key != "expires_at"},
            )


if __name__ == "__main__":
    unittest.main()
