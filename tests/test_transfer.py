"""Tests for github_to_azdo.transfer with mocked remote clients."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from github_to_azdo import licensing, transfer
from github_to_azdo.transfer import PipelineFile, TransferError

HEAD = "a" * 40
TREE = "b" * 40
NEW_TREE = "c" * 40
NEW_COMMIT = "d" * 40
BLOB_SHA = "e" * 40
BLOB_SHA_2 = "f" * 40
PROJECT_ID = "12345678-1234-5678-1234-567812345678"
CONN_ID = "87654321-4321-8765-4321-876543218765"

WORKFLOW = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

WORKFLOW_2 = """\
on: push
jobs:
  test:
    runs-on: windows-latest
    steps:
      - run: echo bye
        shell: pwsh
"""

BLOCKED = """\
on: push
jobs: {}
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeGitHub:
    """Scripted stand-in for transfer.GitHub recording calls."""

    def __init__(self, responses: dict, login: str = "testuser"):
        self.responses = responses
        self.login = login
        self.get_calls: list[tuple[str, bool]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.patch_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.pull_requests: dict[int, dict] = {}

    def authenticate(self) -> str:
        return self.login

    def get(self, endpoint: str, *, paginate: bool = False):
        self.get_calls.append((endpoint, paginate))
        value = self.responses.get(endpoint, _MISSING)
        if value is _MISSING and endpoint.startswith("repos/owner/repo/pulls/"):
            number = int(endpoint.rsplit("/", 1)[-1])
            value = self.pull_requests.get(number, _MISSING)
        if value is _MISSING:
            raise AssertionError(f"unexpected GET {endpoint}")
        if callable(value):
            return value(endpoint)
        return value

    def post(self, endpoint: str, body: dict):
        self.post_calls.append((endpoint, body))
        value = self.responses.get(endpoint, _MISSING)
        if value is _MISSING:
            raise AssertionError(f"unexpected POST {endpoint}")
        if callable(value):
            value = value(endpoint, body)
        if endpoint == "repos/owner/repo/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            self.responses[
                f"repos/owner/repo/git/ref/heads/{branch}"
            ] = {"object": {"sha": body["sha"]}}
        if endpoint == "repos/owner/repo/pulls":
            self.pull_requests[42] = {
                "number": 42,
                "state": "open",
                "draft": True,
                "merged": False,
                "head": {
                    "ref": body["head"],
                    "sha": self.responses[
                        f"repos/owner/repo/git/ref/heads/{body['head']}"
                    ]["object"]["sha"],
                },
                "base": {"ref": body["base"]},
            }
        return value

    def patch(self, endpoint: str, body: dict):
        self.patch_calls.append((endpoint, body))
        if endpoint.startswith("repos/owner/repo/git/refs/heads/"):
            branch = endpoint.removeprefix("repos/owner/repo/git/refs/heads/")
            self.responses[
                f"repos/owner/repo/git/ref/heads/{branch}"
            ] = {"object": {"sha": body["sha"]}}
            for pr in self.pull_requests.values():
                if pr["head"]["ref"] == branch:
                    pr["head"]["sha"] = body["sha"]
        elif endpoint.startswith("repos/owner/repo/pulls/"):
            number = int(endpoint.rsplit("/", 1)[-1])
            self.pull_requests[number]["state"] = body.get(
                "state", self.pull_requests[number]["state"]
            )
        return {}

    def delete(self, endpoint: str):
        self.delete_calls.append(endpoint)
        if endpoint.startswith("repos/owner/repo/git/refs/heads/"):
            branch = endpoint.removeprefix("repos/owner/repo/git/refs/heads/")
            self.responses.pop(
                f"repos/owner/repo/git/ref/heads/{branch}", None
            )


_MISSING = object()


class FakeAzure:
    """Scripted stand-in for transfer.Azure recording calls."""

    def __init__(
        self,
        *,
        projects=None,
        connections=None,
        conflicts=None,
        create_error_on: int | None = None,
        pipeline_overrides=None,
    ):
        self._projects = (
            projects
            if projects is not None
            else [{"id": PROJECT_ID, "name": "Main Project"}]
        )
        self._connections = (
            connections
            if connections is not None
            else [{"id": CONN_ID, "name": "GitHub", "type": "GitHub", "isReady": True}]
        )
        self._conflicts = conflicts or {}
        self._create_error_on = create_error_on
        self._pipeline_overrides = pipeline_overrides or {}
        self._definitions: dict[int, dict] = {}
        self._create_count = 0
        self.authenticated = False
        self.create_calls: list[dict] = []
        self.pipeline_calls: list[tuple[str, str, int]] = []
        self.delete_calls: list[tuple[str, str, int]] = []

    def authenticate(self) -> None:
        self.authenticated = True

    def projects(self, org):
        return list(self._projects)

    def connections(self, org, project):
        return list(self._connections)

    def pipelines(self, org, project, name):
        return list(self._conflicts.get(name.casefold(), []))

    def create_pipeline(self, org, project, connection, repo, branch, pipeline):
        self._create_count += 1
        self.create_calls.append(
            {
                "org": org,
                "project": project,
                "connection": connection,
                "repo": repo,
                "branch": branch,
                "pipeline": pipeline,
            }
        )
        if self._create_error_on == self._create_count:
            raise TransferError("simulated Azure failure")
        pipeline_id = 100 + self._create_count
        self._definitions[pipeline_id] = {
            "id": pipeline_id,
            "name": pipeline.name,
            "repository": {
                "defaultBranch": branch,
                "name": repo,
                "type": "github",
                "url": f"https://github.com/{repo}",
            },
            "process": {"yamlFilename": pipeline.path},
        }
        return {"id": pipeline_id}

    def pipeline(self, org, project, pipeline_id):
        self.pipeline_calls.append((org, project, pipeline_id))
        source = self._definitions[pipeline_id]
        detail = {
            "id": source["id"],
            "name": source["name"],
            "repository": dict(source["repository"]),
            "process": dict(source["process"]),
        }
        detail.update(self._pipeline_overrides.get(pipeline_id, {}))
        return detail

    def delete_pipeline(self, org, project, pipeline_id):
        self.delete_calls.append((org, project, pipeline_id))
        self._definitions.pop(pipeline_id, None)


def default_responses(workflow_text: str = WORKFLOW, workflows=None):
    """Build a full happy-path GitHub response map."""
    entries = workflows if workflows is not None else [
        {
            "name": "ci.yml",
            "path": ".github/workflows/ci.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": len(workflow_text.encode()),
        }
    ]
    blob_map = {BLOB_SHA: workflow_text}
    if workflows is not None:
        blob_map = {}
        for i, entry in enumerate(workflows):
            text = entry.pop("_text", WORKFLOW)
            blob_map[entry["sha"]] = text
    return {
        "/user/repos?per_page=100&sort=full_name&affiliation=owner%2Ccollaborator%2Corganization_member": [
            [
                {
                    "full_name": "owner/repo",
                    "archived": False,
                    "disabled": False,
                    "visibility": "private",
                    "permissions": {"push": True, "pull": True},
                }
            ]
        ],
        "user": {"login": "testuser"},
        "repos/owner/repo": {
            "default_branch": "main",
            "archived": False,
            "disabled": False,
            "permissions": {"push": True},
        },
        "repos/owner/repo/git/ref/heads/main": {"object": {"sha": HEAD}},
        f"repos/owner/repo/contents/.github/workflows?ref={HEAD}": entries,
        **{
            f"repos/owner/repo/git/blobs/{sha}": {
                "encoding": "base64",
                "content": _b64(text),
                "size": len(text.encode()),
            }
            for sha, text in blob_map.items()
        },
        f"repos/owner/repo/git/commits/{HEAD}": {"tree": {"sha": TREE}},
        f"repos/owner/repo/git/trees/{TREE}": {
            "tree": [{"path": "README.md", "type": "blob"}],
            "truncated": False,
        },
        "repos/owner/repo/git/trees": {"sha": NEW_TREE},
        "repos/owner/repo/git/commits": {"sha": NEW_COMMIT},
        "repos/owner/repo/git/refs": {"ref": "refs/heads/x"},
        "repos/owner/repo/pulls": {"number": 42, "html_url": "x"},
    }


def two_workflows():
    return [
        {
            "name": "ci.yml",
            "path": ".github/workflows/ci.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": 10,
            "_text": WORKFLOW,
        },
        {
            "name": "test.yml",
            "path": ".github/workflows/test.yml",
            "sha": BLOB_SHA_2,
            "type": "file",
            "size": 10,
            "_text": WORKFLOW_2,
        },
    ]


def run_transfer_captured(argv_inputs, gh, az, **kwargs):
    """Return (status, error, stdout, stderr); status is None on TransferError."""
    out, err = io.StringIO(), io.StringIO()
    status, error = None, None
    with mock.patch("builtins.input", side_effect=argv_inputs), \
        mock.patch.object(transfer, "GitHub", return_value=gh), \
        mock.patch.object(transfer, "Azure", return_value=az), \
        redirect_stdout(out), redirect_stderr(err):
        try:
            status = transfer.run_transfer(**kwargs)
        except TransferError as exc:
            error = exc
    return status, error, out.getvalue(), err.getvalue()


class TestTransferFlow(unittest.TestCase):
    def test_successful_single_transfer(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, error, out, err = run_transfer_captured(
            ["1", "1", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
        )
        self.assertIsNone(error)
        self.assertEqual(status, 0, err)
        self.assertTrue(az.authenticated)

        # SHA-pinned reads: contents and ref lookups used the head SHA.
        get_endpoints = [endpoint for endpoint, _ in gh.get_calls]
        self.assertIn(f"repos/owner/repo/contents/.github/workflows?ref={HEAD}", get_endpoints)
        self.assertIn("repos/owner/repo/git/ref/heads/main", get_endpoints)
        self.assertIn(f"repos/owner/repo/git/blobs/{BLOB_SHA}", get_endpoints)
        self.assertIn(f"repos/owner/repo/git/commits/{HEAD}", get_endpoints)
        self.assertIn(f"repos/owner/repo/git/trees/{TREE}", get_endpoints)

        # Write order: tree -> commit -> ref -> pull request; the post-merge
        # workflow is added after Azure returns the pipeline id.
        post_endpoints = [endpoint for endpoint, _ in gh.post_calls]
        self.assertEqual(
            post_endpoints,
            [
                "repos/owner/repo/git/trees",
                "repos/owner/repo/git/commits",
                "repos/owner/repo/git/refs",
                "repos/owner/repo/pulls",
                "repos/owner/repo/git/trees",
                "repos/owner/repo/git/commits",
            ],
        )
        tree_body = gh.post_calls[0][1]
        self.assertEqual(tree_body["base_tree"], TREE)
        self.assertEqual(len(tree_body["tree"]), 1)
        entry = tree_body["tree"][0]
        self.assertTrue(entry["path"].startswith("github-to-azdo-"))
        self.assertTrue(entry["path"].endswith("/ci.yml"))
        self.assertEqual(entry["mode"], "100644")
        self.assertEqual(entry["type"], "blob")
        commit_body = gh.post_calls[1][1]
        self.assertEqual(commit_body["parents"], [HEAD])
        self.assertEqual(commit_body["tree"], NEW_TREE)
        ref_body = gh.post_calls[2][1]
        self.assertTrue(ref_body["ref"].startswith("refs/heads/gh-to-azdo/"))
        self.assertEqual(ref_body["sha"], NEW_COMMIT)
        pr_body = gh.post_calls[3][1]
        self.assertEqual(pr_body["base"], "main")
        self.assertTrue(pr_body["head"].startswith("gh-to-azdo/"))
        self.assertIs(pr_body["draft"], True)
        self.assertIn("AZURE_DEVOPS_EXT_PAT", pr_body["body"])
        self.assertIn("pull/42", err)

        # Azure uses, and was read back with, the branch containing the YAML.
        self.assertEqual(len(az.create_calls), 1)
        call = az.create_calls[0]
        self.assertEqual(call["org"], "testorg")
        self.assertEqual(call["project"], PROJECT_ID)
        self.assertEqual(call["connection"], CONN_ID)
        self.assertEqual(call["repo"], "owner/repo")
        self.assertEqual(call["branch"], ref_body["ref"])
        self.assertNotEqual(call["branch"], "refs/heads/main")
        self.assertEqual(call["pipeline"].name, "GHA-owner-repo-ci")
        self.assertTrue(call["pipeline"].path.startswith("github-to-azdo-"))
        self.assertTrue(call["pipeline"].path.endswith("/ci.yml"))
        self.assertIn("trigger:", call["pipeline"].yaml)
        self.assertEqual(az.pipeline_calls, [("testorg", PROJECT_ID, 101)])

        # The finalizer updates that same pipeline after the PR reaches main.
        finalizer_tree = gh.post_calls[4][1]
        self.assertEqual(finalizer_tree["base_tree"], NEW_TREE)
        finalizer_entry = finalizer_tree["tree"][0]
        self.assertTrue(
            finalizer_entry["path"].startswith(
                ".github/workflows/gh-to-azdo-finalize-"
            )
        )
        self.assertTrue(finalizer_entry["path"].endswith(".yml"))
        finalizer = finalizer_entry["content"]
        self.assertIn("types: [closed]", finalizer)
        self.assertIn("TARGET_REF: refs/heads/main", finalizer)
        self.assertIn("GH_REPO: owner/repo", finalizer)
        self.assertIn("AZURE_ORG_URL: https://dev.azure.com/testorg", finalizer)
        self.assertIn(f"AZURE_PROJECT: {PROJECT_ID}", finalizer)
        self.assertIn('"id":101', finalizer)
        self.assertIn(call["pipeline"].path, finalizer)
        self.assertIn(call["branch"].removeprefix("refs/heads/"), finalizer)
        finalizer_commit = gh.post_calls[5][1]
        self.assertEqual(finalizer_commit["parents"], [NEW_COMMIT])
        self.assertEqual(finalizer_commit["tree"], NEW_TREE)
        self.assertEqual(len(gh.patch_calls), 1)
        self.assertEqual(
            gh.patch_calls[0],
            (
                f"repos/owner/repo/git/{ref_body['ref']}",
                {"sha": NEW_COMMIT, "force": False},
            ),
        )
        self.assertIn("added post-merge branch update workflow", err)

    def test_post_merge_switch_can_be_disabled(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
            post_merge_switch=False,
        )
        self.assertIsNone(error)
        self.assertEqual(status, 0)
        self.assertEqual(len(gh.post_calls), 4)
        self.assertEqual(gh.patch_calls, [])
        self.assertEqual(az.pipeline_calls, [("testorg", PROJECT_ID, 101)])
        self.assertNotIn("AZURE_DEVOPS_EXT_PAT", gh.post_calls[3][1]["body"])

    def test_post_merge_workflow_is_valid_and_scoped(self):
        pipeline = PipelineFile(
            source=".github/workflows/ci.yml",
            path="github-to-azdo-abc/ci.yml",
            name="GHA-owner-repo-ci",
            yaml="trigger: {}\n",
        )
        path, text = transfer._post_merge_workflow(
            repo="owner/repo",
            branch="gh-to-azdo/abc",
            default_branch="main",
            org="testorg",
            project_id=PROJECT_ID,
            transfer_id="abc",
            pipelines=[(42, pipeline)],
        )
        self.assertEqual(path, ".github/workflows/gh-to-azdo-finalize-abc.yml")
        parsed = yaml.safe_load(text)
        triggers = parsed.get("on", parsed.get(True))
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        job = parsed["jobs"]["update-pipeline-default-branch"]
        self.assertIn("github.event.pull_request.merged == true", job["if"])
        self.assertIn("github.event.pull_request.head.ref == 'gh-to-azdo/abc'", job["if"])
        env = job["env"]
        self.assertEqual(env["TARGET_REF"], "refs/heads/main")
        self.assertEqual(env["GH_REPO"], "owner/repo")
        self.assertEqual(env["AZURE_PROJECT"], PROJECT_ID)
        specs = json.loads(env["PIPELINES_JSON"])
        self.assertEqual(
            specs,
            [
                {
                    "id": 42,
                    "name": "GHA-owner-repo-ci",
                    "yaml": "github-to-azdo-abc/ci.yml",
                }
            ],
        )
        self.assertIn("contents: read", text)
        self.assertIn("secrets.AZURE_DEVOPS_EXT_PAT", text)

    def test_pipeline_branch_mismatch_fails_before_finalizer(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure(
            pipeline_overrides={
                101: {"repository": {"defaultBranch": "refs/heads/main"}}
            }
        )
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
        )
        self.assertIsNone(status)
        self.assertIn("did not retain the requested YAML location", str(error))
        self.assertIn("GHA-owner-repo-ci (id 101)", str(error))
        self.assertIn("post-merge workflow: not configured", str(error))
        self.assertEqual(len(gh.post_calls), 4)
        self.assertEqual(gh.patch_calls, [])

    def test_plan_mode_makes_no_writes(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, _error, out, _err = run_transfer_captured(
            ["1", "1", "1", "1"],
            gh,
            az,
            org="testorg",
            plan_only=True,
        )
        self.assertEqual(status, 0)
        self.assertIn("Transfer plan", out)
        self.assertIn("--plan: no changes were made", out)
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_conversion_warnings_are_numbered_by_workflow(self):
        workflow_text = """\
name: Deploy
on: workflow_dispatch
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""
        entry = {
            "name": "deploy.yml",
            "path": ".github/workflows/deploy.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": len(workflow_text.encode()),
        }
        gh = FakeGitHub(
            {
                f"repos/owner/repo/git/blobs/{BLOB_SHA}": {
                    "encoding": "base64",
                    "content": _b64(workflow_text),
                    "size": len(workflow_text.encode()),
                }
            }
        )
        err = io.StringIO()
        with redirect_stderr(err):
            files = transfer._build_pipeline_files(
                gh, "owner/repo", [entry], [0], "test-transfer"
            )

        self.assertEqual(len(files), 1)
        warning_output = err.getvalue()
        self.assertIn("Warnings for .github/workflows/deploy.yml (3):", warning_output)
        self.assertIn("  1. name: workflow", warning_output)
        self.assertIn("  2. on: workflow_dispatch", warning_output)
        self.assertIn("  3. jobs.deploy: job id", warning_output)

    def test_cancellation_at_repo_prompt(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, _error, _out, err = run_transfer_captured(["q"], gh, az, org="testorg")
        self.assertEqual(status, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(gh.post_calls, [])
        self.assertFalse(az.authenticated)

    def test_eof_aborts_cleanly(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, _error, _out, err = run_transfer_captured(
            [EOFError()], gh, az, org="testorg"
        )
        self.assertEqual(status, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(gh.post_calls, [])

    def test_wrong_confirmation_writes_nothing(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, _error, _out, err = run_transfer_captured(
            ["1", "1", "1", "1", "yes"],
            gh,
            az,
            org="testorg",
        )
        self.assertEqual(status, 1)
        self.assertIn("cancelled", err)
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_blocked_workflow_stops_before_azure(self):
        gh = FakeGitHub(default_responses(workflow_text=BLOCKED))
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
        )
        self.assertIsNone(status)
        self.assertIn("cannot be converted", str(error))
        self.assertFalse(az.authenticated)
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_duplicate_azure_name_blocks_before_writes(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure(conflicts={"gha-owner-repo-ci": [{"name": "GHA-owner-repo-ci"}]})
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
        )
        self.assertIsNone(status)
        self.assertIn("already has pipeline", str(error))
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_changed_head_blocks_before_writes(self):
        responses = default_responses()
        calls = {"n": 0}

        def moving_ref(endpoint):
            calls["n"] += 1
            sha = HEAD if calls["n"] == 1 else "9" * 40
            return {"object": {"sha": sha}}

        responses["repos/owner/repo/git/ref/heads/main"] = moving_ref
        gh = FakeGitHub(responses)
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
        )
        self.assertIsNone(status)
        self.assertIn("head changed", str(error))
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_multi_select_requires_license(self):
        gh = FakeGitHub(default_responses(workflows=two_workflows()))
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured(
            ["1", "all", "1", "1", "PUBLISH"],
            gh,
            az,
            org="testorg",
        )
        self.assertIsNone(status)
        self.assertIn("publishing more than one workflow", str(error))
        self.assertIn("Pro license", str(error))
        self.assertFalse(az.authenticated)
        self.assertEqual(gh.post_calls, [])

    def test_plan_multi_select_is_free(self):
        gh = FakeGitHub(default_responses(workflows=two_workflows()))
        az = FakeAzure()
        with mock.patch.object(licensing, "verify_license") as verify:
            status, error, out, _err = run_transfer_captured(
                ["1", "all", "1", "1"],
                gh,
                az,
                org="testorg",
                plan_only=True,
            )
        self.assertIsNone(error)
        self.assertEqual(status, 0)
        verify.assert_not_called()
        self.assertIn("Transfer plan", out)
        self.assertIn("Publishing this selection requires a signed Pro license", out)
        self.assertIn(".github/workflows/ci.yml -> github-to-azdo-", out)
        self.assertIn(".github/workflows/test.yml -> github-to-azdo-", out)
        self.assertIn("/ci.yml", out)
        self.assertIn("/test.yml", out)
        self.assertIn("GHA-owner-repo-ci", out)
        self.assertIn("GHA-owner-repo-test", out)
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_plan_collects_all_blockers(self):
        blocked_entries = [
            {
                "name": "ci.yml",
                "path": ".github/workflows/ci.yml",
                "sha": BLOB_SHA,
                "type": "file",
                "size": 10,
                "_text": BLOCKED,
            },
            {
                "name": "test.yml",
                "path": ".github/workflows/test.yml",
                "sha": BLOB_SHA_2,
                "type": "file",
                "size": 10,
                "_text": BLOCKED,
            },
        ]
        gh = FakeGitHub(default_responses(workflows=blocked_entries))
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured(
            ["1", "all"], gh, az, org="testorg", plan_only=True
        )
        self.assertIsNone(status)
        message = str(error)
        self.assertIn("2 of 2", message)
        self.assertIn(".github/workflows/ci.yml", message)
        self.assertIn(".github/workflows/test.yml", message)
        self.assertIn("nothing was published", message)
        # Both blobs were fetched and converted to gather every blocker.
        blob_gets = [e for e, _ in gh.get_calls if "git/blobs" in e]
        self.assertEqual(len(blob_gets), 2)
        # No Azure auth and no writes happened.
        self.assertFalse(az.authenticated)
        self.assertEqual(gh.post_calls, [])
        self.assertEqual(az.create_calls, [])

    def test_multi_select_with_license_proceeds(self):
        gh = FakeGitHub(default_responses(workflows=two_workflows()))
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            license_path = Path(tmp) / "license.json"
            license_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                licensing, "verify_license", return_value={"features": ["batch"]}
            ) as verify:
                status, error, _out, _err = run_transfer_captured(
                    ["1", "1,2", "1", "1", "PUBLISH"],
                    gh,
                    az,
                    org="testorg",
                    license_path=license_path,
                )
        self.assertIsNone(error)
        self.assertEqual(status, 0, _err)
        verify.assert_called_once()
        self.assertEqual(len(az.create_calls), 2)
        names = {call["pipeline"].name for call in az.create_calls}
        self.assertEqual(names, {"GHA-owner-repo-ci", "GHA-owner-repo-test"})
        # Both YAML files went into the same single commit.
        tree_body = gh.post_calls[0][1]
        self.assertEqual(len(tree_body["tree"]), 2)

    def test_partial_failure_reports_retained_ids(self):
        gh = FakeGitHub(default_responses(workflows=two_workflows()))
        az = FakeAzure(create_error_on=2)
        with tempfile.TemporaryDirectory() as tmp:
            license_path = Path(tmp) / "license.json"
            license_path.write_text("{}", encoding="utf-8")
            with mock.patch.object(licensing, "verify_license", return_value={}):
                status, error, _out, _err = run_transfer_captured(
                    ["1", "all", "1", "1", "PUBLISH"],
                    gh,
                    az,
                    org="testorg",
                    license_path=license_path,
                )
        self.assertIsNone(status)
        message = str(error)
        self.assertIn("partial state", message)
        self.assertIn("gh-to-azdo/", message)
        self.assertIn("id 101", message)  # first pipeline created before failure
        self.assertIn("pull/42", message)

    def test_target_prompt_and_project_connection_picks(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, _error, out, _err = run_transfer_captured(
            ["1", "1", "dev.azure.com/org-b/Main%20Project", "1"],
            gh,
            az,
            plan_only=True,
        )
        self.assertEqual(status, 0)
        self.assertIn("org-b", out)
        self.assertIn("Main Project", out)

    def test_target_argument_skips_target_prompt(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        status, error, out, _err = run_transfer_captured(
            ["1", "1", "1"],
            gh,
            az,
            target="https://dev.azure.com/testorg/Main%20Project",
            plan_only=True,
        )
        self.assertIsNone(error)
        self.assertEqual(status, 0)
        self.assertIn("testorg / Main Project", out)

    def test_target_prompt_reprompts_for_invalid_url(self):
        answers = iter(["https://example.com/org/project", "dev.azure.com/org/project"])
        err = io.StringIO()
        with mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)), \
            redirect_stderr(err):
            result = transfer._prompt_azure_target()
        self.assertEqual(result, ("org", "project"))
        self.assertIn("must be dev.azure.com", err.getvalue())

    def test_no_ready_github_connection_fails_with_guidance(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure(connections=[])
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1"], gh, az, org="testorg", plan_only=True
        )
        self.assertIsNone(status)
        self.assertIn("service connection", str(error))

    def test_oversize_workflow_rejected_before_blob_fetch(self):
        big_entry = {
            "name": "huge.yml",
            "path": ".github/workflows/huge.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": transfer.MAX_INPUT_BYTES + 1,
        }
        gh = FakeGitHub(default_responses(workflows=[big_entry]))
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured(
            ["1", "1", "1", "1"], gh, az, org="testorg", plan_only=True
        )
        self.assertIsNone(status)
        self.assertIn("1 MiB", str(error))
        # The blob endpoint was never requested.
        self.assertFalse(
            any("git/blobs" in endpoint for endpoint, _ in gh.get_calls)
        )

    def test_unsafe_workflow_name_is_skipped_not_sanitized(self):
        bad_entry = {
            "name": "bad name!.yml",
            "path": ".github/workflows/bad name!.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": 10,
        }
        good_entry = {
            "name": "ci.yml",
            "path": ".github/workflows/ci.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": 10,
            "_text": WORKFLOW,
        }
        gh = FakeGitHub(default_responses(workflows=[bad_entry, good_entry]))
        az = FakeAzure()
        status, _error, _out, err = run_transfer_captured(
            ["1", "1", "1", "1"], gh, az, org="testorg", plan_only=True
        )
        self.assertEqual(status, 0)
        self.assertIn("bad name!.yml", err)  # reported as skipped
        # Only the safe workflow was listed for selection.
        self.assertEqual(len(az.create_calls), 0)

    def test_generated_finalizer_workflow_is_skipped(self):
        finalizer_entry = {
            "name": f"gh-to-azdo-finalize-{'a' * 32}.yml",
            "path": f".github/workflows/gh-to-azdo-finalize-{'a' * 32}.yml",
            "sha": BLOB_SHA,
            "type": "file",
            "size": 10,
        }
        good_entry = {
            "name": "ci.yml",
            "path": ".github/workflows/ci.yml",
            "sha": BLOB_SHA_2,
            "type": "file",
            "size": 10,
            "_text": WORKFLOW,
        }
        gh = FakeGitHub(
            default_responses(workflows=[finalizer_entry, good_entry])
        )
        az = FakeAzure()
        status, _error, _out, err = run_transfer_captured(
            ["1", "1", "1", "1"], gh, az, org="testorg", plan_only=True
        )
        self.assertEqual(status, 0)
        self.assertIn("generated post-merge helper", err)
        blob_endpoints = [
            endpoint for endpoint, _ in gh.get_calls if "git/blobs" in endpoint
        ]
        self.assertEqual(blob_endpoints, [f"repos/owner/repo/git/blobs/{BLOB_SHA_2}"])

    def test_repo_without_push_access_is_filtered(self):
        responses = default_responses()
        repos_endpoint = next(
            k for k in responses if k.startswith("/user/repos")
        )
        responses[repos_endpoint] = [
            [
                {
                    "full_name": "owner/readonly",
                    "archived": False,
                    "disabled": False,
                    "permissions": {"push": False},
                }
            ]
        ]
        gh = FakeGitHub(responses)
        az = FakeAzure()
        status, error, _out, _err = run_transfer_captured([], gh, az, org="testorg")
        self.assertIsNone(status)
        self.assertIn("push access", str(error))


def run_apply_captured(inputs, plan_path, gh, az, *, license_path=None):
    out, err = io.StringIO(), io.StringIO()
    status, error = None, None
    with mock.patch("builtins.input", side_effect=inputs), \
        mock.patch.object(transfer, "GitHub", return_value=gh), \
        mock.patch.object(transfer, "Azure", return_value=az), \
        redirect_stdout(out), redirect_stderr(err):
        try:
            status = transfer.run_apply(plan_path, license_path=license_path)
        except TransferError as exc:
            error = exc
    return status, error, out.getvalue(), err.getvalue()


def run_destroy_captured(inputs, plan_path, gh, az):
    out, err = io.StringIO(), io.StringIO()
    status, error = None, None
    with mock.patch("builtins.input", side_effect=inputs), \
        mock.patch.object(transfer, "GitHub", return_value=gh), \
        mock.patch.object(transfer, "Azure", return_value=az), \
        redirect_stdout(out), redirect_stderr(err):
        try:
            status = transfer.run_destroy(plan_path)
        except TransferError as exc:
            error = exc
    return status, error, out.getvalue(), err.getvalue()


def create_saved_plan(tmp_path, gh, az):
    plan_path = Path(tmp_path) / "cloudjobs.plan.json"
    status, error, _out, err = run_transfer_captured(
        ["1", "1", "1", "1"],
        gh,
        az,
        org="testorg",
        plan_only=True,
        plan_output=plan_path,
    )
    if error is not None or status != 0:
        raise AssertionError(f"plan creation failed: {error or err}")
    return plan_path


class TestSavedTransferPlans(unittest.TestCase):
    def test_plan_outfile_is_reusable_and_read_only(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["format"], transfer._PLAN_FORMAT)
            self.assertEqual(plan["source"]["repository"], "owner/repo")
            self.assertEqual(plan["destination"]["project_id"], PROJECT_ID)
            self.assertEqual(len(plan["pipelines"]), 1)
            self.assertEqual(gh.post_calls, [])
            self.assertEqual(az.create_calls, [])
            self.assertFalse(transfer._receipt_path(plan_path).exists())

    def test_plan_outfile_refuses_to_overwrite(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "existing.json"
            plan_path.write_text("keep me", encoding="utf-8")
            status, error, _out, _err = run_transfer_captured(
                ["1", "1", "1", "1"],
                gh,
                az,
                org="testorg",
                plan_only=True,
                plan_output=plan_path,
            )
            self.assertIsNone(status)
            self.assertIn("refusing to overwrite", str(error))
            self.assertEqual(plan_path.read_text(encoding="utf-8"), "keep me")
            self.assertEqual(gh.post_calls, [])
            self.assertEqual(az.create_calls, [])

    def test_apply_receipt_can_destroy_only_created_resources(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            status, error, _out, _err = run_apply_captured(
                ["PUBLISH"], plan_path, gh, az
            )
            self.assertIsNone(error)
            self.assertEqual(status, 0)
            receipt_path = transfer._receipt_path(plan_path)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "applied")
            self.assertEqual(len(receipt["pipelines"]), 1)
            self.assertEqual(receipt["pull_request"]["number"], 42)

            status, error, out, _err = run_destroy_captured(
                [f"DESTROY {plan['transfer_id']}"], plan_path, gh, az
            )
            self.assertIsNone(error)
            self.assertEqual(status, 0)
            self.assertIn("default branch was not modified", out)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "destroyed")
            self.assertEqual(az.delete_calls, [("testorg", PROJECT_ID, 101)])
            self.assertEqual(
                gh.delete_calls,
                [
                    "repos/owner/repo/git/refs/heads/"
                    + plan["branch"]
                ],
            )
            self.assertEqual(gh.pull_requests[42]["state"], "closed")

    def test_apply_rejects_changed_source_without_writes(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            gh.responses["repos/owner/repo/git/ref/heads/main"] = {
                "object": {"sha": "9" * 40}
            }
            status, error, _out, _err = run_apply_captured(
                ["PUBLISH"], plan_path, gh, az
            )
            self.assertIsNone(status)
            self.assertIn("head changed", str(error))
            self.assertEqual(gh.post_calls, [])
            self.assertEqual(az.create_calls, [])
            self.assertFalse(transfer._receipt_path(plan_path).exists())

    def test_apply_rejects_tampered_generated_yaml(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["pipelines"][0]["yaml"] += "# changed\n"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            status, error, _out, _err = run_apply_captured(
                ["PUBLISH"], plan_path, gh, az
            )
            self.assertIsNone(status)
            self.assertIn("no longer matches", str(error))
            self.assertEqual(gh.post_calls, [])
            self.assertEqual(az.create_calls, [])

    def test_destroy_refuses_merged_pull_request_before_deleting(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            status, error, _out, _err = run_apply_captured(
                ["PUBLISH"], plan_path, gh, az
            )
            self.assertIsNone(error)
            self.assertEqual(status, 0)
            gh.pull_requests[42]["merged"] = True
            gh.pull_requests[42]["state"] = "closed"
            status, error, _out, _err = run_destroy_captured(
                [], plan_path, gh, az
            )
            self.assertIsNone(status)
            self.assertIn("is merged", str(error))
            self.assertEqual(gh.delete_calls, [])
            self.assertEqual(az.delete_calls, [])

    def test_destroy_requires_exact_confirmation(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            status, error, _out, _err = run_apply_captured(
                ["PUBLISH"], plan_path, gh, az
            )
            self.assertIsNone(error)
            self.assertEqual(status, 0)
            status, error, _out, err = run_destroy_captured(
                ["DESTROY"], plan_path, gh, az
            )
            self.assertIsNone(error)
            self.assertEqual(status, 1)
            self.assertIn("destroy cancelled", err)
            self.assertEqual(gh.delete_calls, [])
            self.assertEqual(az.delete_calls, [])
            self.assertEqual(gh.pull_requests[42]["state"], "open")

    def test_destroy_rejects_receipt_bound_to_different_plan(self):
        gh = FakeGitHub(default_responses())
        az = FakeAzure()
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = create_saved_plan(tmp, gh, az)
            status, error, _out, _err = run_apply_captured(
                ["PUBLISH"], plan_path, gh, az
            )
            self.assertIsNone(error)
            self.assertEqual(status, 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["source"]["head_sha"] = "9" * 40
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            status, error, _out, _err = run_destroy_captured(
                [], plan_path, gh, az
            )
            self.assertIsNone(status)
            self.assertIn("unchanged plan", str(error))
            self.assertEqual(gh.delete_calls, [])
            self.assertEqual(az.delete_calls, [])


class TestAdapters(unittest.TestCase):
    def test_azure_create_pipeline_argv(self):
        az = transfer.Azure()
        pipeline = PipelineFile(
            source=".github/workflows/ci.yml",
            path="github-to-azdo-abc/ci.yml",
            name="GHA-owner-repo-ci",
            yaml="trigger: {}\n",
        )
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps({"id": 7}), stderr=""
            )
            result = az.create_pipeline(
                "testorg", PROJECT_ID, CONN_ID, "owner/repo",
                "refs/heads/main", pipeline,
            )
        self.assertEqual(result["id"], 7)
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:3], ["pipelines", "create"])
        for flag, expected in (
            ("--name", "GHA-owner-repo-ci"),
            ("--repository", "owner/repo"),
            ("--repository-type", "github"),
            ("--branch", "refs/heads/main"),
            ("--yml-path", "github-to-azdo-abc/ci.yml"),
            ("--service-connection", CONN_ID),
            ("--skip-first-run", "true"),
            ("--organization", "https://dev.azure.com/testorg"),
            ("--project", PROJECT_ID),
            ("--detect", "false"),
            ("--output", "json"),
            ("--only-show-errors", None),
        ):
            self.assertIn(flag, argv)
            if expected is not None:
                self.assertEqual(argv[argv.index(flag) + 1], expected)

    def test_azure_pipeline_show_argv(self):
        az = transfer.Azure()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps({"id": 7}), stderr=""
            )
            result = az.pipeline("testorg", PROJECT_ID, 7)
        self.assertEqual(result["id"], 7)
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:3], ["pipelines", "show"])
        self.assertEqual(argv[argv.index("--id") + 1], "7")
        self.assertEqual(
            argv[argv.index("--organization") + 1],
            "https://dev.azure.com/testorg",
        )
        self.assertEqual(argv[argv.index("--project") + 1], PROJECT_ID)

    def test_azure_pipeline_delete_argv(self):
        az = transfer.Azure()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            az.delete_pipeline("testorg", PROJECT_ID, 7)
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:3], ["pipelines", "delete"])
        self.assertEqual(argv[argv.index("--id") + 1], "7")
        self.assertIn("--yes", argv)
        self.assertEqual(
            argv[argv.index("--organization") + 1],
            "https://dev.azure.com/testorg",
        )
        self.assertEqual(argv[argv.index("--project") + 1], PROJECT_ID)
        self.assertIn("--only-show-errors", argv)

    def test_azure_project_list_args_and_no_project_flag(self):
        az = transfer.Azure()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"value": []}),
                stderr="",
            )
            az.projects("testorg")
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:4], ["devops", "project", "list"])
        self.assertIn("--state-filter", argv)
        self.assertEqual(argv[argv.index("--state-filter") + 1], "wellFormed")
        self.assertNotIn("--get-all", argv)
        self.assertNotIn("--project", argv)
        self.assertIn("--organization", argv)
        self.assertIn("--only-show-errors", argv)

    def test_azure_project_list_paginates_with_continuation_token(self):
        az = transfer.Azure()
        page1 = {
            "value": [{"id": PROJECT_ID, "name": "P1"}],
            "continuation_token": "42",
        }
        page2 = {"value": [{"id": CONN_ID, "name": "P2"}]}
        with mock.patch.object(transfer, "_run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(page1), stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(page2), stderr=""
                ),
            ]
            result = az.projects("testorg")
        self.assertEqual([p["name"] for p in result], ["P1", "P2"])
        self.assertEqual(run.call_count, 2)
        second_argv = run.call_args_list[1][0][0]
        self.assertIn("--continuation-token", second_argv)
        self.assertEqual(
            second_argv[second_argv.index("--continuation-token") + 1], "42"
        )

    def test_azure_project_list_rejects_bad_continuation_token(self):
        az = transfer.Azure()
        bad = {"value": [], "continuation_token": "not-a-number"}
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(bad), stderr=""
            )
            with self.assertRaises(TransferError):
                az.projects("testorg")

    def test_azure_pipelines_list_argv_and_bare_array(self):
        az = transfer.Azure()
        payload = [
            {"name": "gha-owner-repo-ci", "id": 1},
            {"name": "unrelated", "id": 2},
        ]
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(payload), stderr=""
            )
            result = az.pipelines("testorg", PROJECT_ID, "GHA-owner-repo-ci")
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:3], ["pipelines", "list"])
        self.assertEqual(argv[argv.index("--name") + 1], "GHA-owner-repo-ci")
        self.assertIn("--project", argv)
        # Bare array parsed; case-insensitive exact name match only.
        self.assertEqual([p["name"] for p in result], ["gha-owner-repo-ci"])

    def test_azure_service_endpoints_bare_array_filtered(self):
        az = transfer.Azure()
        other_id = "99999999-9999-9999-9999-999999999999"
        payload = [
            {"id": CONN_ID, "name": "gh-ready", "type": "GitHub", "isReady": True},
            {
                "id": other_id,
                "name": "gh-not-ready",
                "type": "GitHub",
                "isReady": False,
            },
            {
                "id": other_id,
                "name": "arm",
                "type": "Azure Resource Manager",
                "isReady": True,
            },
        ]
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(payload), stderr=""
            )
            result = az.connections("testorg", PROJECT_ID)
        self.assertEqual([c["name"] for c in result], ["gh-ready"])

    def test_azure_malformed_listings_raise(self):
        az = transfer.Azure()
        with mock.patch.object(transfer, "_run") as run:
            # Wrapper response where a bare array was expected.
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"value": [{"name": "x"}]}),
                stderr="",
            )
            with self.assertRaises(TransferError):
                az.pipelines("testorg", PROJECT_ID, "x")
            # Bare array where a value wrapper was expected.
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps([]), stderr=""
            )
            with self.assertRaises(TransferError):
                az.projects("testorg")
            # Non-list value inside the wrapper.
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"value": "nope"}),
                stderr="",
            )
            with self.assertRaises(TransferError):
                az.projects("testorg")

    def test_github_get_argv_and_paginate(self):
        gh = transfer.GitHub()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="{}", stderr=""
            )
            gh.get("user")
            gh.get("/user/repos?x=1", paginate=True)
        first = run.call_args_list[0][0][0]
        self.assertEqual(first[1:4], ["api", "--hostname", "github.com"])
        self.assertEqual(first[-1], "user")
        self.assertIn("GET", first)
        second = run.call_args_list[1][0][0]
        self.assertIn("--paginate", second)
        self.assertIn("--slurp", second)

    def test_github_post_uses_input_stdin(self):
        gh = transfer.GitHub()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="{}", stderr=""
            )
            gh.post("repos/o/r/pulls", {"draft": True})
        argv = run.call_args[0][0]
        self.assertIn("POST", argv)
        self.assertIn("--input", argv)
        self.assertIn("-", argv)
        self.assertEqual(run.call_args[1]["input_text"], '{"draft": true}')

    def test_github_patch_uses_input_stdin(self):
        gh = transfer.GitHub()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="{}", stderr=""
            )
            gh.patch("repos/o/r/git/refs/heads/x", {"sha": HEAD, "force": False})
        argv = run.call_args[0][0]
        self.assertIn("PATCH", argv)
        self.assertIn("--input", argv)
        self.assertEqual(
            run.call_args[1]["input_text"],
            json.dumps({"sha": HEAD, "force": False}),
        )

    def test_github_delete_uses_delete_method(self):
        gh = transfer.GitHub()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            gh.delete("repos/o/r/git/refs/heads/gh-to-azdo/x")
        argv = run.call_args[0][0]
        self.assertIn("DELETE", argv)
        self.assertEqual(argv[-1], "repos/o/r/git/refs/heads/gh-to-azdo/x")

    def test_api_error_does_not_leak_stderr_token(self):
        gh = transfer.GitHub()
        with mock.patch.object(transfer, "_run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="auth failed token=SECRETTOKEN123 ghp_abcdef",
            )
            with self.assertRaises(TransferError) as ctx:
                gh.get("user")
        self.assertNotIn("SECRETTOKEN123", str(ctx.exception))
        self.assertNotIn("ghp_abcdef", str(ctx.exception))

    def test_clean_env_drops_debug_and_pat(self):
        with mock.patch.dict(
            os.environ,
            {
                "GH_DEBUG": "api",
                "GH_FORCE_TTY": "1",
                "AZURE_DEVOPS_EXT_PAT": "pat",
                "GH_TOKEN": "keepme",
            },
        ):
            env = transfer._clean_env()
        self.assertNotIn("GH_DEBUG", env)
        self.assertNotIn("GH_FORCE_TTY", env)
        self.assertNotIn("AZURE_DEVOPS_EXT_PAT", env)
        self.assertEqual(env["GH_TOKEN"], "keepme")
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"], "no")

    def test_clean_env_interactive_keeps_prompt_enabled(self):
        with mock.patch.dict(
            os.environ, {"GH_PROMPT_DISABLED": "1", "GH_DEBUG": "api"}
        ):
            env = transfer._clean_env(interactive=True)
            plain = transfer._clean_env()
        self.assertNotIn("GH_PROMPT_DISABLED", env)
        self.assertNotIn("GH_DEBUG", env)
        self.assertEqual(plain["GH_PROMPT_DISABLED"], "1")

    @unittest.skipUnless(os.name == "nt", "Windows Azure CLI fallback")
    def test_tool_finds_default_azure_install_when_path_is_stale(self):
        program_files = r"C:\mock-program-files"
        expected = (
            Path(program_files)
            / "Microsoft SDKs"
            / "Azure"
            / "CLI2"
            / "wbin"
            / "az.cmd"
        )
        with (
            mock.patch.object(transfer.shutil, "which", return_value=None),
            mock.patch.dict(os.environ, {"ProgramFiles": program_files}),
            mock.patch.object(Path, "is_file", return_value=True),
        ):
            self.assertEqual(transfer._tool("az"), str(expected))

    def test_org_slug_validation(self):
        for bad in ("bad org", "-lead", "a" * 60, "org/extra", ""):
            with self.subTest(org=bad):
                with self.assertRaises(TransferError):
                    transfer._validate_org(bad)

    def test_azure_target_url_parsing(self):
        self.assertEqual(
            transfer._parse_azure_target("dev.azure.com/johnmehler/cloudjobs"),
            ("johnmehler", "cloudjobs"),
        )
        self.assertEqual(
            transfer._parse_azure_target(
                "https://dev.azure.com/org-a/Cloud%20Jobs/"
            ),
            ("org-a", "Cloud Jobs"),
        )

    def test_azure_target_url_rejects_unsafe_or_incomplete_urls(self):
        for value in (
            "example.com/org/project",
            "http://dev.azure.com/org/project",
            "dev.azure.com/org",
            "dev.azure.com/org/project/extra",
            "dev.azure.com/org/%2Fproject",
            "dev.azure.com/-bad/project",
            "dev.azure.com/org/project?query=1",
        ):
            with self.subTest(target=value):
                with self.assertRaises(TransferError):
                    transfer._parse_azure_target(value)

    def test_resolve_project_matches_name_case_insensitively(self):
        project, project_id = transfer._resolve_project(
            FakeAzure(), "testorg", "main project"
        )
        self.assertEqual(project, "Main Project")
        self.assertEqual(project_id, PROJECT_ID)

    def test_resolve_project_rejects_missing_project(self):
        with self.assertRaisesRegex(TransferError, "was not found"):
            transfer._resolve_project(FakeAzure(), "testorg", "unknown")

    def test_invalid_selection_reprompts(self):
        picks = []
        answers = iter(["9", "1,1", "x", "2"])
        with mock.patch("builtins.input", side_effect=lambda _p: next(answers)):
            result = transfer._pick("test", ["a", "b"], multiple=True)
        self.assertEqual(result, [1])


class TestCliRouting(unittest.TestCase):
    def test_transfer_routes_through_cli(self):
        from github_to_azdo import cli

        with mock.patch.object(transfer, "run_transfer", return_value=0) as rt:
            status = cli.main(["transfer", "--org", "o", "--plan"])
        self.assertEqual(status, 0)
        rt.assert_called_once_with(
            org="o",
            target=None,
            plan_only=True,
            license_path=None,
            plan_output=None,
            post_merge_switch=True,
        )

    def test_transfer_target_routes_through_cli(self):
        from github_to_azdo import cli

        target = "dev.azure.com/org/project"
        with mock.patch.object(transfer, "run_transfer", return_value=0) as rt:
            status = cli.main(["transfer", "--target", target, "--plan"])
        self.assertEqual(status, 0)
        rt.assert_called_once_with(
            org=None,
            target=target,
            plan_only=True,
            license_path=None,
            plan_output=None,
            post_merge_switch=True,
        )

    def test_transfer_plan_output_routes_through_cli(self):
        from github_to_azdo import cli

        with mock.patch.object(transfer, "run_transfer", return_value=0) as rt:
            status = cli.main(
                ["transfer", "--org", "o", "--plan", "--output", "plan.json"]
            )
        self.assertEqual(status, 0)
        rt.assert_called_once_with(
            org="o",
            target=None,
            plan_only=True,
            license_path=None,
            plan_output=Path("plan.json"),
            post_merge_switch=True,
        )

    def test_apply_and_destroy_routes_through_cli(self):
        from github_to_azdo import cli

        with mock.patch.object(transfer, "run_apply", return_value=0) as apply:
            status = cli.main(["apply", "plan.json", "--license", "license.json"])
        self.assertEqual(status, 0)
        apply.assert_called_once_with(
            Path("plan.json"), license_path=Path("license.json")
        )

        with mock.patch.object(transfer, "run_destroy", return_value=0) as destroy:
            status = cli.main(["apply", "plan.json", "--destroy"])
        self.assertEqual(status, 0)
        destroy.assert_called_once_with(Path("plan.json"))

        with mock.patch.object(transfer, "run_destroy", return_value=0) as destroy:
            status = cli.main(["destroy", "plan.json"])
        self.assertEqual(status, 0)
        destroy.assert_called_once_with(Path("plan.json"))

    def test_transfer_post_merge_opt_out_routes_through_cli(self):
        from github_to_azdo import cli

        with mock.patch.object(transfer, "run_transfer", return_value=0) as rt:
            status = cli.main(
                ["transfer", "--org", "o", "--plan", "--no-post-merge-switch"]
            )
        self.assertEqual(status, 0)
        rt.assert_called_once_with(
            org="o",
            target=None,
            plan_only=True,
            license_path=None,
            plan_output=None,
            post_merge_switch=False,
        )

    def test_transfer_error_becomes_cli_error(self):
        from github_to_azdo import cli

        err = io.StringIO()
        with mock.patch.object(
            transfer, "run_transfer", side_effect=TransferError("boom")
        ), redirect_stderr(err):
            status = cli.main(["transfer"])
        self.assertEqual(status, 1)
        self.assertIn("boom", err.getvalue())


if __name__ == "__main__":
    unittest.main()
