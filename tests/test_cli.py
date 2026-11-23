"""Tests for github_to_azdo.cli."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from github_to_azdo import cli, licensing
from github_to_azdo.licensing import canonical_payload

GOOD_WORKFLOW = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

BLOCKED_WORKFLOW = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - if: github.ref == 'x'
        run: echo hi
"""


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = cli.main(argv)
    return status, out.getvalue(), err.getvalue()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_keypair():
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return private, public_b64


def make_license(path: Path, private, customer="Acme", expires="2099-01-01") -> Path:
    payload = {
        "version": 1,
        "customer": customer,
        "expires": expires,
        "features": ["batch"],
    }
    envelope = {
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_payload(payload))).decode("ascii"),
    }
    return write(path, json.dumps(envelope))


class TestDryRun(unittest.TestCase):
    def test_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "ci.yml", GOOD_WORKFLOW)
            status, out, err = run_cli(["dry-run", str(wf)])
            self.assertEqual(status, 0)
            pipeline = yaml.safe_load(out)
            self.assertIn("jobs", pipeline)
            self.assertIsInstance(pipeline["jobs"], list)
            self.assertEqual(pipeline["jobs"][0]["job"], "build")
            self.assertEqual(err, "")

    def test_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "bad.yml", BLOCKED_WORKFLOW)
            status, out, err = run_cli(["dry-run", str(wf)])
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            self.assertIn(str(wf), err)
            self.assertIn("error", err)
            self.assertIn("steps[0]", err)

    def test_missing_file(self):
        status, _out, err = run_cli(["dry-run", "/nonexistent/ci.yml"])
        self.assertEqual(status, 1)
        self.assertIn("error", err)


class TestMigrate(unittest.TestCase):
    def test_writes_exclusive_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "ci.yml", GOOD_WORKFLOW)
            out_path = Path(tmp) / "azure.yml"
            status, _out, err = run_cli(["migrate", str(wf), "--output", str(out_path)])
            self.assertEqual(status, 0)
            self.assertTrue(out_path.exists())
            pipeline = yaml.safe_load(out_path.read_text(encoding="utf-8"))
            self.assertIn("jobs", pipeline)

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "ci.yml", GOOD_WORKFLOW)
            out_path = write(Path(tmp) / "azure.yml", "existing: true\n")
            status, _out, err = run_cli(["migrate", str(wf), "--output", str(out_path)])
            self.assertEqual(status, 1)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "existing: true\n")
            self.assertIn("already exists", err)

    def test_blocked_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "bad.yml", BLOCKED_WORKFLOW)
            out_path = Path(tmp) / "azure.yml"
            status, _out, err = run_cli(["migrate", str(wf), "--output", str(out_path)])
            self.assertEqual(status, 1)
            self.assertFalse(out_path.exists())

    def test_parent_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "ci.yml", GOOD_WORKFLOW)
            out_path = Path(tmp) / "nope" / "azure.yml"
            status, _out, err = run_cli(["migrate", str(wf), "--output", str(out_path)])
            self.assertEqual(status, 1)
            self.assertIn("error", err)


class TestAudit(unittest.TestCase):
    def test_single_file_convertible(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write(Path(tmp) / "ci.yml", GOOD_WORKFLOW)
            status, out, _err = run_cli(["audit", str(wf)])
            self.assertEqual(status, 0)
            report = json.loads(out)
            self.assertEqual(len(report["workflows"]), 1)
            entry = report["workflows"][0]
            self.assertEqual(entry["status"], "convertible")
            self.assertEqual(entry["path"], str(wf))
            self.assertIsInstance(entry["issues"], list)

    def test_mixed_directory_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / ".github" / "workflows"
            write(wf_dir / "b-bad.yml", BLOCKED_WORKFLOW)
            write(wf_dir / "a-good.yaml", GOOD_WORKFLOW)
            write(wf_dir / "notes.txt", "not a workflow")
            status, out, _err = run_cli(["audit", str(root)])
            self.assertEqual(status, 0)
            report = json.loads(out)
            paths = [w["path"] for w in report["workflows"]]
            # deterministic by name: a-good before b-bad
            self.assertEqual(len(paths), 2)
            self.assertTrue(paths[0].endswith("a-good.yaml"))
            self.assertTrue(paths[1].endswith("b-bad.yml"))
            statuses = {w["path"]: w["status"] for w in report["workflows"]}
            self.assertEqual(statuses[paths[0]], "convertible")
            self.assertEqual(statuses[paths[1]], "blocked")
            bad = report["workflows"][1]
            self.assertTrue(any(i["severity"] == "error" for i in bad["issues"]))
            for issue in bad["issues"]:
                self.assertEqual(set(issue.keys()), {"severity", "location", "message"})

    def test_directory_without_dot_github_scans_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "one.yml", GOOD_WORKFLOW)
            write(Path(tmp) / "two.yaml", GOOD_WORKFLOW)
            write(Path(tmp) / "skip.json", "{}")
            status, out, _err = run_cli(["audit", tmp])
            self.assertEqual(status, 0)
            report = json.loads(out)
            self.assertEqual(len(report["workflows"]), 2)

    def test_no_workflows_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, _out, err = run_cli(["audit", tmp])
            self.assertEqual(status, 1)
            self.assertIn("no workflow", err)

    def test_parse_failure_is_blocked_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "broken.yml", "jobs: [unclosed\n")
            write(Path(tmp) / "ok.yml", GOOD_WORKFLOW)
            status, out, _err = run_cli(["audit", tmp])
            self.assertEqual(status, 0)
            report = json.loads(out)
            by_path = {Path(w["path"]).name: w for w in report["workflows"]}
            self.assertEqual(by_path["broken.yml"]["status"], "blocked")
            self.assertEqual(by_path["ok.yml"]["status"], "convertible")

    def test_missing_path(self):
        status, _out, err = run_cli(["audit", "/nonexistent/path"])
        self.assertEqual(status, 1)
        self.assertIn("error", err)

    def test_symlink_entries_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "real.yml", GOOD_WORKFLOW)
            link = root / "link.yml"
            try:
                link.symlink_to(root / "real.yml")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported on this platform")
            status, out, _err = run_cli(["audit", tmp])
            self.assertEqual(status, 0)
            report = json.loads(out)
            self.assertEqual(len(report["workflows"]), 1)
            self.assertTrue(report["workflows"][0]["path"].endswith("real.yml"))


class TestBatch(unittest.TestCase):
    def setUp(self):
        self._private, self._public_b64 = make_keypair()
        patcher = mock.patch.object(licensing, "PRO_PUBLIC_KEY_B64", self._public_b64)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _license(self, tmp: str) -> Path:
        return make_license(Path(tmp) / "license.json", self._private)

    def test_success_writes_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / "repo" / ".github" / "workflows"
            write(wf_dir / "ci.yml", GOOD_WORKFLOW)
            write(wf_dir / "lint.yaml", GOOD_WORKFLOW.replace("echo hi", "echo lint"))
            lic = self._license(tmp)
            out_dir = root / "out"
            status, _out, err = run_cli(
                ["batch", str(root / "repo"), "--output-dir", str(out_dir), "--license", str(lic)]
            )
            self.assertEqual(status, 0, err)
            self.assertTrue((out_dir / "ci.azure-pipelines.yml").exists())
            self.assertTrue((out_dir / "lint.azure-pipelines.yml").exists())
            pipeline = yaml.safe_load((out_dir / "ci.azure-pipelines.yml").read_text(encoding="utf-8"))
            self.assertIn("jobs", pipeline)

    def test_blocked_leaves_no_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wf_dir = root / "repo" / ".github" / "workflows"
            write(wf_dir / "ok.yml", GOOD_WORKFLOW)
            write(wf_dir / "bad.yml", BLOCKED_WORKFLOW)
            lic = self._license(tmp)
            out_dir = root / "out"
            status, _out, err = run_cli(
                ["batch", str(root / "repo"), "--output-dir", str(out_dir), "--license", str(lic)]
            )
            self.assertEqual(status, 1)
            self.assertFalse(out_dir.exists())
            self.assertIn("bad.yml", err)

    def test_duplicate_stems_casefold_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "CI.yml", GOOD_WORKFLOW)
            write(root / "ci.yaml", GOOD_WORKFLOW)
            lic = self._license(tmp)
            out_dir = root / "out"
            status, _out, err = run_cli(
                ["batch", str(root), "--output-dir", str(out_dir), "--license", str(lic)]
            )
            self.assertEqual(status, 1)
            self.assertFalse(out_dir.exists())
            self.assertIn("duplicate", err)

    def test_bad_license_blocks_before_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "ci.yml", GOOD_WORKFLOW)
            bad_lic = write(root / "lic.json", "{not json")
            out_dir = root / "out"
            # Nonexistent source dir proves the license is checked before scanning.
            status, _out, err = run_cli(
                [
                    "batch",
                    str(root / "does-not-exist"),
                    "--output-dir",
                    str(out_dir),
                    "--license",
                    str(bad_lic),
                ]
            )
            self.assertEqual(status, 1)
            self.assertFalse(out_dir.exists())
            self.assertIn("license", err)
            self.assertNotIn("not a directory", err)

    def test_wrong_key_license_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "ci.yml", GOOD_WORKFLOW)
            other_private, _ = make_keypair()
            lic = make_license(root / "lic.json", other_private)
            out_dir = root / "out"
            status, _out, err = run_cli(
                ["batch", str(root), "--output-dir", str(out_dir), "--license", str(lic)]
            )
            self.assertEqual(status, 1)
            self.assertFalse(out_dir.exists())
            self.assertIn("signature", err)

    def test_existing_output_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "ci.yml", GOOD_WORKFLOW)
            lic = self._license(tmp)
            out_dir = root / "out"
            out_dir.mkdir()
            status, _out, err = run_cli(
                ["batch", str(root), "--output-dir", str(out_dir), "--license", str(lic)]
            )
            self.assertEqual(status, 1)
            self.assertIn("already exists", err)

    def test_no_workflows_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lic = self._license(tmp)
            status, _out, err = run_cli(
                ["batch", str(root), "--output-dir", str(root / "out"), "--license", str(lic)]
            )
            self.assertEqual(status, 1)
            self.assertIn("no workflow", err)


if __name__ == "__main__":
    unittest.main()
