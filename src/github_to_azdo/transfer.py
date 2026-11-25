"""Interactive transfer of GitHub Actions workflows to Azure Pipelines.

``gh-to-azdo transfer`` plans or directly publishes a migration;
``gh-to-azdo apply`` applies a saved plan and ``gh-to-azdo destroy`` removes
only the resources recorded for that apply. All three use the locally
installed ``gh`` and ``az`` CLIs. GitHub stays the source of truth (code is
never copied); converted YAML is committed on a new ``gh-to-azdo/<id>``
branch behind a **draft** pull request, and Azure pipeline definitions use
that branch. A destroy is refused after the migration PR is merged because
the generated files are then part of the repository's default branch.
First-run queueing is disabled. Existing GitHub Actions workflows are never
disabled or deleted, the default branch is never written directly, and no
pipeline run is triggered by the command itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import licensing
from .converter import MAX_INPUT_BYTES, convert, render

_AZ_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # Azure DevOps resource ID

_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
_BRANCH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_ORG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,49}")
_WORKFLOW_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:yml|yaml)", re.IGNORECASE)
_FINALIZER_NAME_RE = re.compile(
    r"gh-to-azdo-finalize-[0-9a-f]{32}\.yml", re.IGNORECASE
)
_PIPELINE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HTTP_RE = re.compile(r"HTTP (\d{3})")
_MAX_PIPELINE_NAME = 200
_MAX_CONTENTS_ENTRIES = 1000
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_PLAN_FORMAT = "github-to-azdo-transfer-plan"
_STATE_FORMAT = "github-to-azdo-transfer-state"
_MANIFEST_VERSION = 1

_ENV_DROP = ("GH_DEBUG", "GH_FORCE_TTY", "AZURE_DEVOPS_EXT_PAT")


class TransferError(Exception):
    """User-facing transfer failure (message already safe to print)."""


class _Abort(Exception):
    """The user cancelled an interactive prompt before any write."""


@dataclass(frozen=True)
class PipelineFile:
    """One converted workflow destined for the repository and Azure."""

    source: str  # path in .github/workflows on GitHub
    path: str  # destination path in the repository for the Azure YAML
    name: str  # Azure pipeline definition name
    yaml: str  # rendered Azure Pipelines YAML


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json_file(path: Path, what: str) -> Any:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise TransferError(f"cannot read {what} file {path}: {exc.strerror or exc}") from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise TransferError(f"{what} file exceeds the 32 MiB limit")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TransferError(f"{what} file is not valid UTF-8 JSON") from exc


def _write_json_exclusive(path: Path, data: Any, what: str) -> None:
    serialized = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    if len(serialized.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise TransferError(f"{what} file would exceed the 32 MiB limit")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    except FileExistsError as exc:
        raise TransferError(f"refusing to overwrite existing {what} file: {path}") from exc
    except OSError as exc:
        raise TransferError(f"cannot write {what} file {path}: {exc.strerror or exc}") from exc


def _write_json_atomic(path: Path, data: Any, what: str) -> None:
    serialized = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    if len(serialized.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise TransferError(f"{what} file would exceed the 32 MiB limit")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
        os.replace(temporary, path)
    except OSError as exc:
        raise TransferError(
            f"cannot update {what} file {path}: {exc.strerror or exc}"
        ) from exc


def _plan_document(
    *,
    transfer_id: str,
    repo: str,
    default_branch: str,
    head_sha: str,
    org: str,
    project_id: str,
    project_name: str,
    connection_id: str,
    connection_name: str,
    files: list[PipelineFile],
    post_merge_switch: bool,
) -> dict[str, Any]:
    return {
        "format": _PLAN_FORMAT,
        "version": _MANIFEST_VERSION,
        "transfer_id": transfer_id,
        "source": {
            "repository": repo,
            "default_branch": default_branch,
            "head_sha": head_sha,
        },
        "destination": {
            "organization": org,
            "project_id": project_id,
            "project_name": project_name,
            "connection_id": connection_id,
            "connection_name": connection_name,
        },
        "branch": f"gh-to-azdo/{transfer_id}",
        "root_dir": f"github-to-azdo-{transfer_id}",
        "post_merge_switch": post_merge_switch,
        "pipelines": [
            {
                "source": pipeline.source,
                "path": pipeline.path,
                "name": pipeline.name,
                "yaml": pipeline.yaml,
            }
            for pipeline in files
        ],
    }


def _load_plan(path: Path) -> tuple[dict[str, Any], list[PipelineFile]]:
    plan = _read_json_file(path, "plan")
    expected_keys = {
        "format",
        "version",
        "transfer_id",
        "source",
        "destination",
        "branch",
        "root_dir",
        "post_merge_switch",
        "pipelines",
    }
    if not isinstance(plan, dict) or set(plan) != expected_keys:
        raise TransferError("plan file has an unsupported structure")
    if (
        plan.get("format") != _PLAN_FORMAT
        or type(plan.get("version")) is not int
        or plan.get("version") != _MANIFEST_VERSION
    ):
        raise TransferError("plan file format or version is not supported")

    transfer_id = plan.get("transfer_id")
    if not isinstance(transfer_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transfer_id):
        raise TransferError("plan file has an invalid transfer id")
    if plan.get("branch") != f"gh-to-azdo/{transfer_id}":
        raise TransferError("plan branch does not match its transfer id")
    root_dir = f"github-to-azdo-{transfer_id}"
    if plan.get("root_dir") != root_dir:
        raise TransferError("plan output directory does not match its transfer id")
    if type(plan.get("post_merge_switch")) is not bool:
        raise TransferError("plan has an invalid post-merge setting")

    source = plan.get("source")
    if not isinstance(source, dict) or set(source) != {
        "repository",
        "default_branch",
        "head_sha",
    }:
        raise TransferError("plan source information is invalid")
    repo = source.get("repository")
    if not isinstance(repo, str):
        raise TransferError("plan repository is invalid")
    _validate_repo_name(repo)
    default_branch = _validate_branch(source.get("default_branch"))
    head_sha = source.get("head_sha")
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha):
        raise TransferError("plan source commit is invalid")

    destination = plan.get("destination")
    if not isinstance(destination, dict) or set(destination) != {
        "organization",
        "project_id",
        "project_name",
        "connection_id",
        "connection_name",
    }:
        raise TransferError("plan destination information is invalid")
    org = destination.get("organization")
    if not isinstance(org, str):
        raise TransferError("plan Azure organization is invalid")
    _validate_org(org)
    _uuid(destination.get("project_id"), "project")
    _uuid(destination.get("connection_id"), "service connection")
    for key in ("project_name", "connection_name"):
        if not isinstance(destination.get(key), str) or not destination[key]:
            raise TransferError(f"plan {key.replace('_', ' ')} is invalid")

    raw_files = plan.get("pipelines")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) < _MAX_CONTENTS_ENTRIES:
        raise TransferError("plan must contain between 1 and 999 pipelines")
    owner, _, repo_name = repo.partition("/")
    files: list[PipelineFile] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {
            "source",
            "path",
            "name",
            "yaml",
        }:
            raise TransferError("plan contains an invalid pipeline entry")
        source_path = raw_file.get("source")
        if not isinstance(source_path, str) or not source_path.startswith(
            ".github/workflows/"
        ):
            raise TransferError("plan contains an invalid workflow source path")
        filename = source_path.removeprefix(".github/workflows/")
        if not _WORKFLOW_NAME_RE.fullmatch(filename):
            raise TransferError("plan contains an unsafe workflow filename")
        stem = filename.rsplit(".", 1)[0]
        path_value = raw_file.get("path")
        name_value = raw_file.get("name")
        yaml_value = raw_file.get("yaml")
        expected_name = f"GHA-{owner}-{repo_name}-{stem}"
        if path_value != f"{root_dir}/{stem}.yml" or name_value != expected_name:
            raise TransferError("plan pipeline name or YAML path is inconsistent")
        if (
            len(expected_name) > _MAX_PIPELINE_NAME
            or not _PIPELINE_NAME_RE.fullmatch(expected_name)
        ):
            raise TransferError("plan contains an unsafe Azure pipeline name")
        if (
            not isinstance(yaml_value, str)
            or not yaml_value
            or len(yaml_value.encode("utf-8")) > MAX_INPUT_BYTES * 2
        ):
            raise TransferError("plan contains invalid generated YAML")
        if path_value.casefold() in seen_paths or name_value.casefold() in seen_names:
            raise TransferError("plan contains duplicate pipeline names or paths")
        seen_paths.add(path_value.casefold())
        seen_names.add(name_value.casefold())
        files.append(
            PipelineFile(
                source=source_path,
                path=path_value,
                name=name_value,
                yaml=yaml_value,
            )
        )
    return plan, files


def _plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()


def _receipt_path(plan_path: Path) -> Path:
    return plan_path.with_name(f"{plan_path.name}.state.json")


# ---------------------------------------------------------------------------
# Subprocess / HTTP plumbing (no shell, no token or body dumps on errors)
# ---------------------------------------------------------------------------


def _clean_env(*, interactive: bool = False) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _ENV_DROP}
    if interactive:
        # Interactive logins must be able to prompt.
        env.pop("GH_PROMPT_DISABLED", None)
    else:
        env["GH_PROMPT_DISABLED"] = "1"
    env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "no"
    return env


def _tool(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if os.name == "nt" and name.casefold() == "az":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        installed_cli = (
            Path(program_files)
            / "Microsoft SDKs"
            / "Azure"
            / "CLI2"
            / "wbin"
            / "az.cmd"
        )
        if installed_cli.is_file():
            return str(installed_cli)
    return name


def _run(
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 90,
    interactive: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        if interactive:
            return subprocess.run(
                argv, timeout=timeout, check=False, env=_clean_env(interactive=True)
            )
        return subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TransferError(f"'{argv[0]}' timed out; check your connection and retry") from exc
    except OSError as exc:
        raise TransferError(
            f"cannot execute {argv[0]!r}: {exc.strerror or exc}; "
            "is the CLI installed and on PATH?"
        ) from exc


def _list_items(data: Any, what: str) -> list[dict[str, Any]]:
    """Validate a bare JSON array of objects returned by an az devops list."""
    if not isinstance(data, list) or any(
        not isinstance(item, dict) for item in data
    ):
        raise TransferError(f"Azure DevOps returned an unexpected {what} listing")
    return data


def _value_items(data: Any, what: str) -> list[dict[str, Any]]:
    """Validate a ``{"value": [...]}`` wrapper returned by an az devops list."""
    if not isinstance(data, dict):
        raise TransferError(f"Azure DevOps returned an unexpected {what} listing")
    return _list_items(data.get("value"), what)


# ---------------------------------------------------------------------------
# GitHub adapter (gh CLI)
# ---------------------------------------------------------------------------


class GitHub:
    """GitHub.com REST access through the local ``gh`` CLI."""

    def __init__(self) -> None:
        self._gh = _tool("gh")

    def _api(
        self, args: list[str], *, input_text: str | None = None, timeout: int = 90
    ) -> str:
        proc = _run(
            [self._gh, "api", "--hostname", "github.com", *args],
            input_text=input_text,
            timeout=timeout,
        )
        if proc.returncode != 0:
            match = _HTTP_RE.search(proc.stderr or "")
            detail = f" (HTTP {match.group(1)})" if match else ""
            raise TransferError(
                f"GitHub API request failed{detail}; check "
                "`gh auth status --hostname github.com` and repository permissions"
            )
        return proc.stdout or ""

    def authenticate(self) -> str:
        status = _run(
            [self._gh, "auth", "status", "--hostname", "github.com"], timeout=60
        )
        if status.returncode != 0:
            if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
                raise TransferError(
                    "the GitHub token supplied by the environment was rejected; "
                    "update GH_TOKEN/GITHUB_TOKEN or unset it and run "
                    "`gh auth login --hostname github.com`"
                )
            _run(
                [
                    self._gh,
                    "auth",
                    "login",
                    "--hostname",
                    "github.com",
                    "--web",
                    "--skip-ssh-key",
                ],
                timeout=600,
                interactive=True,
            )
            status = _run(
                [self._gh, "auth", "status", "--hostname", "github.com"], timeout=60
            )
            if status.returncode != 0:
                raise TransferError(
                    "GitHub sign-in did not complete; run "
                    "`gh auth login --hostname github.com` and retry"
                )
        user = self.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not login:
            raise TransferError(
                "could not determine the GitHub user; check "
                "`gh auth status --hostname github.com`"
            )
        return login

    def get(self, endpoint: str, *, paginate: bool = False) -> Any:
        args = ["--method", "GET"]
        if paginate:
            args += ["--paginate", "--slurp"]
        args.append(endpoint)
        try:
            return json.loads(self._api(args))
        except json.JSONDecodeError as exc:
            raise TransferError("GitHub API returned unreadable JSON") from exc

    def post(self, endpoint: str, body: dict[str, Any]) -> Any:
        try:
            return json.loads(
                self._api(["--method", "POST", endpoint, "--input", "-"], input_text=json.dumps(body))
            )
        except json.JSONDecodeError as exc:
            raise TransferError("GitHub API returned unreadable JSON") from exc

    def patch(self, endpoint: str, body: dict[str, Any]) -> Any:
        try:
            return json.loads(
                self._api(["--method", "PATCH", endpoint, "--input", "-"], input_text=json.dumps(body))
            )
        except json.JSONDecodeError as exc:
            raise TransferError("GitHub API returned unreadable JSON") from exc

    def delete(self, endpoint: str) -> None:
        self._api(["--method", "DELETE", endpoint])


# ---------------------------------------------------------------------------
# Azure DevOps adapter (az CLI)
# ---------------------------------------------------------------------------


class Azure:
    """Azure DevOps Services access via the local ``az`` CLI."""

    def __init__(self) -> None:
        self._az = _tool("az")

    def authenticate(self) -> None:
        proc = _run(
            [
                self._az,
                "extension",
                "show",
                "--name",
                "azure-devops",
                "--output",
                "none",
                "--only-show-errors",
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            raise TransferError(
                "the Azure DevOps CLI extension is not installed; run "
                "`az extension add --name azure-devops` and retry"
            )
        token = self._access_token()
        if token is None:
            _run(
                [self._az, "login", "--allow-no-subscriptions", "--output", "none"],
                timeout=600,
                interactive=True,
            )
            token = self._access_token()
        if not token:
            raise TransferError(
                "could not obtain an Azure DevOps access token; run `az login` "
                "(use --tenant TENANT for a specific directory) and retry"
            )

    def _access_token(self) -> str | None:
        proc = _run(
            [
                self._az,
                "account",
                "get-access-token",
                "--resource",
                _AZ_RESOURCE,
                "--query",
                "accessToken",
                "--output",
                "tsv",
                "--only-show-errors",
            ],
            timeout=60,
        )
        if proc.returncode != 0:
            return None
        token = (proc.stdout or "").strip()
        return token or None

    def _az_json(self, args: list[str], *, org: str, project: str | None = None) -> Any:
        argv = [
            self._az,
            *args,
            "--organization",
            f"https://dev.azure.com/{org}",
            "--detect",
            "false",
            "--output",
            "json",
            "--only-show-errors",
        ]
        if project is not None:
            argv += ["--project", project]
        proc = _run(argv, timeout=120)
        if proc.returncode != 0:
            # Only our own fixed command words are echoed — never remote data.
            raise TransferError(
                f"Azure DevOps command 'az {' '.join(args[:2])}' failed; check "
                "`az account show` and permissions on the organization"
            )
        try:
            return json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            raise TransferError("Azure DevOps returned unreadable JSON") from exc

    def projects(self, org: str) -> list[dict[str, Any]]:
        base_args = ["devops", "project", "list", "--state-filter", "wellFormed"]
        projects: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            args = list(base_args)
            if cursor is not None:
                args += ["--continuation-token", cursor]
            data = self._az_json(args, org=org)
            projects.extend(_value_items(data, "project"))
            next_cursor = data.get("continuation_token")
            if next_cursor is None:
                next_cursor = data.get("continuationToken")
            if next_cursor is None or next_cursor == "":
                break
            if isinstance(next_cursor, bool) or not isinstance(
                next_cursor, (str, int)
            ):
                raise TransferError(
                    "Azure DevOps returned an invalid project continuation token"
                )
            cursor = str(next_cursor)
            if not re.fullmatch(r"[0-9]{1,30}", cursor):
                raise TransferError(
                    "Azure DevOps returned an invalid project continuation token"
                )
            if cursor in seen:
                raise TransferError(
                    "Azure DevOps returned a repeated project continuation token"
                )
            seen.add(cursor)
        return projects

    def connections(self, org: str, project: str) -> list[dict[str, Any]]:
        data = self._az_json(
            ["devops", "service-endpoint", "list"], org=org, project=project
        )
        return [
            conn
            for conn in _list_items(data, "service connection")
            if str(conn.get("type", "")).casefold() == "github"
            and conn.get("isReady") is not False
        ]

    def pipelines(self, org: str, project: str, name: str) -> list[dict[str, Any]]:
        data = self._az_json(
            ["pipelines", "list", "--name", name], org=org, project=project
        )
        wanted = name.casefold()
        return [
            pipe
            for pipe in _list_items(data, "pipeline")
            if str(pipe.get("name", "")).casefold() == wanted
        ]

    def pipeline(self, org: str, project: str, pipeline_id: int) -> dict[str, Any]:
        data = self._az_json(
            ["pipelines", "show", "--id", str(pipeline_id)],
            org=org,
            project=project,
        )
        if not isinstance(data, dict) or data.get("id") != pipeline_id:
            raise TransferError(
                "Azure DevOps did not return the created pipeline definition"
            )
        return data

    def create_pipeline(
        self,
        org: str,
        project: str,
        connection: str,
        repo: str,
        branch: str,
        pipeline: PipelineFile,
    ) -> dict[str, Any]:
        # Every remotely derived argument is pre-validated by the caller
        # against safe patterns because ``az`` may resolve to ``az.cmd`` on
        # Windows, where the value passes through cmd.exe parsing.
        data = self._az_json(
            [
                "pipelines",
                "create",
                "--name",
                pipeline.name,
                "--repository",
                repo,
                "--repository-type",
                "github",
                "--branch",
                branch,
                "--yml-path",
                pipeline.path,
                "--service-connection",
                connection,
                "--skip-first-run",
                "true",
            ],
            org=org,
            project=project,
        )
        if not isinstance(data, dict) or type(data.get("id")) is not int:
            raise TransferError(
                "Azure DevOps did not confirm pipeline creation; check Pipelines "
                "in the project before retrying — the pipeline may already exist"
            )
        return data

    def delete_pipeline(self, org: str, project: str, pipeline_id: int) -> None:
        argv = [
            self._az,
            "pipelines",
            "delete",
            "--id",
            str(pipeline_id),
            "--yes",
            "--organization",
            f"https://dev.azure.com/{org}",
            "--project",
            project,
            "--detect",
            "false",
            "--only-show-errors",
        ]
        proc = _run(argv, timeout=120)
        if proc.returncode != 0:
            raise TransferError(
                f"Azure DevOps could not delete pipeline id {pipeline_id}; "
                "check its state in the project before retrying"
            )


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------


def _prompt(message: str) -> str:
    try:
        return input(message)
    except (EOFError, KeyboardInterrupt) as exc:
        raise _Abort() from exc


def _clean_remote(text: Any) -> str:
    """Strip control characters from remote-supplied strings before display."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:200]


def _print_numbered(title: str, rows: list[str]) -> None:
    print(title)
    for index, row in enumerate(rows, 1):
        print(f"  {index}. {row}")


def _parse_positions(raw: str, count: int, *, allow_all: bool) -> list[int]:
    text = raw.strip()
    if allow_all and text.casefold() == "all":
        return list(range(count))
    positions: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not re.fullmatch(r"[0-9]{1,10}", part):
            raise TransferError(
                "invalid selection; enter numbers separated by commas"
            )
        number = int(part)
        if number < 1 or number > count:
            raise TransferError(f"selection {number} is out of range (1-{count})")
        if number in positions:
            raise TransferError(f"duplicate selection {number}")
        positions.append(number)
    return sorted(index - 1 for index in positions)


def _pick(
    title: str,
    rows: list[str],
    *,
    multiple: bool = False,
    allow_all: bool = False,
) -> list[int]:
    if not rows:
        raise TransferError(f"nothing to select for {title}")
    _print_numbered(title, rows)
    if multiple:
        hint = "comma-separated numbers"
        if allow_all:
            hint += " or 'all'"
        hint += "; 'q' cancels"
    else:
        hint = "a number; 'q' cancels"
    while True:
        raw = _prompt(f"{title} — {hint}: ")
        if raw.strip().casefold() in ("", "q", "quit"):
            raise _Abort()
        try:
            picks = _parse_positions(raw, len(rows), allow_all=allow_all)
        except TransferError as exc:
            print(f"  {exc}", file=sys.stderr)
            continue
        if multiple or len(picks) == 1:
            return picks
        print("  select exactly one entry", file=sys.stderr)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_repo_name(repo: str) -> None:
    if not _REPO_RE.fullmatch(repo):
        raise TransferError(f"repository name {repo!r} is not a valid owner/name")
    if any(segment in (".", "..") for segment in repo.split("/")):
        raise TransferError(f"repository name {repo!r} contains an invalid path segment")


def _validate_branch(branch: Any) -> str:
    if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch):
        raise TransferError(f"default branch {branch!r} is not a safe branch name")
    if branch.startswith("refs/"):
        raise TransferError(f"default branch {branch!r} must not include a refs/ prefix")
    return branch


def _sha_of(payload: Any, what: str) -> str:
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise TransferError(f"GitHub did not return a valid {what} id")
    return sha


def _uuid(value: Any, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TransferError(f"Azure DevOps returned an invalid {what} id") from exc


# ---------------------------------------------------------------------------
# GitHub-side flow helpers
# ---------------------------------------------------------------------------


def _list_repos(gh: GitHub) -> list[dict[str, Any]]:
    pages = gh.get(
        "/user/repos?per_page=100&sort=full_name"
        "&affiliation=owner%2Ccollaborator%2Corganization_member",
        paginate=True,
    )
    if not isinstance(pages, list):
        raise TransferError("GitHub returned an unexpected repository listing")
    repos: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise TransferError("GitHub returned an unexpected repository listing")
        repos.extend(page)
    usable: list[dict[str, Any]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        if repo.get("archived") or repo.get("disabled"):
            continue
        permissions = repo.get("permissions")
        if not isinstance(permissions, dict) or not permissions.get("push"):
            continue
        full_name = repo.get("full_name")
        if isinstance(full_name, str):
            try:
                _validate_repo_name(full_name)
            except TransferError:
                continue
            usable.append(repo)
    return usable


def _resolve_head(gh: GitHub, repo: str, branch: str) -> str:
    quoted = urllib.parse.quote(branch, safe="/")
    ref = gh.get(f"repos/{repo}/git/ref/heads/{quoted}")
    obj = ref.get("object") if isinstance(ref, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise TransferError(f"could not resolve the head commit of branch {branch!r}")
    return sha


def _list_workflows(
    gh: GitHub, repo: str, ref: str
) -> tuple[list[dict[str, Any]], list[str]]:
    listing = gh.get(f"repos/{repo}/contents/.github/workflows?ref={ref}")
    if isinstance(listing, dict):
        raise TransferError(f"{repo} has no .github/workflows directory")
    if not isinstance(listing, list):
        raise TransferError("GitHub returned an unexpected workflow listing")
    if len(listing) >= _MAX_CONTENTS_ENTRIES:
        raise TransferError(
            f"{repo} has {_MAX_CONTENTS_ENTRIES}+ entries in .github/workflows; "
            "the GitHub contents API cannot list them safely — convert the "
            "files individually with `gh-to-azdo migrate` instead"
        )
    usable: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        lower = name.lower()
        if not (lower.endswith(".yml") or lower.endswith(".yaml")):
            continue
        path = entry.get("path")
        display = _clean_remote(path if isinstance(path, str) else name)
        if entry.get("type") != "file":
            skipped.append(f"{display} (not a regular file)")
            continue
        if not _WORKFLOW_NAME_RE.fullmatch(name):
            skipped.append(f"{display} (name is not a safe pipeline identifier)")
            continue
        if _FINALIZER_NAME_RE.fullmatch(name):
            skipped.append(f"{display} (generated post-merge helper)")
            continue
        sha = entry.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            skipped.append(f"{display} (missing or invalid blob id)")
            continue
        usable.append(entry)
    return usable, skipped


def _workflow_source(entry: dict[str, Any]) -> str:
    """Trusted source path built from the validated name, never remote path."""
    return f".github/workflows/{entry['name']}"


def _fetch_blob_text(gh: GitHub, repo: str, sha: str) -> str:
    blob = gh.get(f"repos/{repo}/git/blobs/{sha}")
    if not isinstance(blob, dict) or blob.get("encoding") != "base64":
        raise TransferError("GitHub returned an unsupported workflow blob")
    size = blob.get("size")
    if (
        isinstance(size, int)
        and not isinstance(size, bool)
        and size > MAX_INPUT_BYTES
    ):
        raise TransferError("a workflow file exceeds the 1 MiB input limit")
    content = blob.get("content")
    if not isinstance(content, str):
        raise TransferError("GitHub returned an empty workflow blob")
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TransferError(
            "GitHub returned a workflow blob that is not valid base64"
        ) from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise TransferError("a workflow file exceeds the 1 MiB input limit")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TransferError("a workflow file is not valid UTF-8") from exc


def _build_pipeline_files(
    gh: GitHub,
    repo: str,
    entries: list[dict[str, Any]],
    indices: list[int],
    transfer_id: str,
    *,
    collect_blockers: bool = False,
    default_branch: str | None = None,
) -> list[PipelineFile]:
    owner, _, repo_name = repo.partition("/")
    root = f"github-to-azdo-{transfer_id}"
    files: list[PipelineFile] = []
    blocked: list[str] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for index in indices:
        entry = entries[index]
        source = _workflow_source(entry)
        name = str(entry["name"])
        stem = name.rsplit(".", 1)[0]
        advertised = entry.get("size")
        if type(advertised) is int and advertised > MAX_INPUT_BYTES:
            raise TransferError(f"{source} exceeds the 1 MiB input limit")
        text = _fetch_blob_text(gh, repo, str(entry["sha"]))
        conversion = convert(text, default_branch=default_branch)
        warnings = [
            issue for issue in conversion.issues if issue.severity == "warning"
        ]
        if warnings:
            print(f"\nWarnings for {source} ({len(warnings)}):", file=sys.stderr)
            for warning_number, issue in enumerate(warnings, 1):
                prefix = f"  {warning_number}. "
                message = (
                    f"{_clean_remote(issue.location)}: "
                    f"{_clean_remote(issue.message)}"
                )
                print(
                    textwrap.fill(
                        message,
                        width=88,
                        initial_indent=prefix,
                        subsequent_indent=" " * len(prefix),
                    ),
                    file=sys.stderr,
                )
            print(file=sys.stderr)
        for issue in conversion.issues:
            if issue.severity == "error":
                print(
                    f"{source}:{_clean_remote(issue.location)}: error: "
                    f"{_clean_remote(issue.message)}",
                    file=sys.stderr,
                )
        if conversion.pipeline is None:
            if collect_blockers:
                blocked.append(source)
                continue
            raise TransferError(
                f"{source} cannot be converted to Azure Pipelines; run "
                f"`gh-to-azdo audit` on it for details — the whole transfer "
                "was stopped before any changes"
            )
        pipeline_name = f"GHA-{owner}-{repo_name}-{stem}"
        if len(pipeline_name) > _MAX_PIPELINE_NAME or not _PIPELINE_NAME_RE.fullmatch(
            pipeline_name
        ):
            raise TransferError(
                f"the generated Azure pipeline name for {source} is not safe"
            )
        dest_path = f"{root}/{stem}.yml"
        if dest_path.casefold() in seen_paths or pipeline_name.casefold() in seen_names:
            raise TransferError(
                f"{source} produces a duplicate output name; rename one of the "
                "selected workflow files and retry"
            )
        seen_paths.add(dest_path.casefold())
        seen_names.add(pipeline_name.casefold())
        files.append(
            PipelineFile(
                source=source,
                path=dest_path,
                name=pipeline_name,
                yaml=render(conversion.pipeline),
            )
        )
    if blocked:
        raise TransferError(
            f"{len(blocked)} of {len(indices)} selected workflow(s) cannot be "
            f"converted: {', '.join(blocked)}. See diagnostics; nothing was "
            "published."
        )
    return files


# ---------------------------------------------------------------------------
# Azure-side flow helpers
# ---------------------------------------------------------------------------


def _parse_azure_target(target: str) -> tuple[str, str]:
    """Parse an Azure DevOps organization/project URL."""
    raw = target.strip()
    if not raw:
        raise TransferError(
            "enter an Azure DevOps URL like dev.azure.com/organization/project"
        )
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme.casefold() != "https"
            or parsed.netloc.casefold() != "dev.azure.com"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        path = parsed.path
        if not path.startswith("/"):
            raise ValueError
        path = path[1:]
        if path.endswith("/"):
            path = path[:-1]
        segments = path.split("/")
        if len(segments) != 2 or not all(segments):
            raise ValueError
        org, project = (
            urllib.parse.unquote(segment, errors="strict") for segment in segments
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise TransferError(
            "Azure DevOps target must be dev.azure.com/organization/project"
        ) from exc

    if not _ORG_RE.fullmatch(org):
        raise TransferError("Azure DevOps organization name in the URL is invalid")
    if (
        not project
        or "/" in project
        or "\\" in project
        or any(not character.isprintable() for character in project)
    ):
        raise TransferError("Azure DevOps project name in the URL is invalid")
    return org, project


def _prompt_azure_target() -> tuple[str, str]:
    while True:
        raw = _prompt(
            "Azure DevOps organization/project URL "
            "(for example dev.azure.com/org/project; 'q' cancels): "
        )
        if raw.strip().casefold() in ("", "q", "quit"):
            raise _Abort()
        try:
            return _parse_azure_target(raw)
        except TransferError as exc:
            print(f"  {exc}", file=sys.stderr)


def _validate_org(org: str) -> str:
    if not _ORG_RE.fullmatch(org):
        raise TransferError(f"invalid Azure DevOps organization name {org!r}")
    return org


def _pick_project(azure: Azure, org: str) -> tuple[str, str]:
    projects = azure.projects(org)
    if not projects:
        raise TransferError(f"no Azure DevOps projects are visible in {org!r}")
    entries: list[tuple[str, str]] = []
    for project in projects:
        project_id = _uuid(project.get("id"), "project")
        name = project.get("name")
        display = _clean_remote(name) if isinstance(name, str) and name else project_id
        entries.append((project_id, display))
    rows = [f"{display} ({project_id})" for project_id, display in entries]
    picks = _pick(f"Azure DevOps project in {org}", rows)
    project_id, display = entries[picks[0]]
    return display, project_id


def _resolve_project(
    azure: Azure, org: str, project_ref: str
) -> tuple[str, str]:
    projects = azure.projects(org)
    if not projects:
        raise TransferError(f"no Azure DevOps projects are visible in {org!r}")

    matches: list[tuple[str, str]] = []
    for project in projects:
        project_id = _uuid(project.get("id"), "project")
        name = project.get("name")
        if not isinstance(name, str) or not name:
            continue
        if (
            name.casefold() == project_ref.casefold()
            or project_id.casefold() == project_ref.casefold()
        ):
            matches.append((_clean_remote(name), project_id))
    if not matches:
        raise TransferError(
            f"Azure DevOps project {project_ref!r} was not found or is not "
            f"visible in organization {org!r}"
        )
    if len(matches) != 1:
        raise TransferError(
            f"Azure DevOps project {project_ref!r} is ambiguous in organization "
            f"{org!r}"
        )
    return matches[0]


def _pick_connection(azure: Azure, org: str, project_id: str) -> tuple[str, str]:
    connections = azure.connections(org, project_id)
    if not connections:
        raise TransferError(
            "no ready GitHub service connection was found in the selected "
            "project; create one in Azure DevOps (Project settings → Service "
            "connections → GitHub) authorized for the repository, then retry"
        )
    entries: list[tuple[str, str]] = []
    for conn in connections:
        conn_id = _uuid(conn.get("id"), "service connection")
        name = conn.get("name")
        display = _clean_remote(name) if isinstance(name, str) and name else conn_id
        entries.append((conn_id, display))
    rows = [f"{display} ({conn_id})" for conn_id, display in entries]
    picks = _pick("GitHub service connection", rows)
    conn_id, display = entries[picks[0]]
    return display, conn_id


def _check_name_conflicts(
    azure: Azure, org: str, project_id: str, files: list[PipelineFile]
) -> None:
    conflicts: list[str] = []
    for pipeline_file in files:
        existing = azure.pipelines(org, project_id, pipeline_file.name)
        if existing:
            shown = ", ".join(
                _clean_remote(pipe.get("name", "?")) for pipe in existing[:3]
            )
            conflicts.append(f"{pipeline_file.name} (existing: {shown})")
    if conflicts:
        raise TransferError(
            "Azure DevOps already has pipeline(s) with these names: "
            + "; ".join(conflicts)
            + ". Rename the workflow file(s) or remove the existing pipelines, "
            "then retry — nothing was changed"
        )


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _base_tree(gh: GitHub, repo: str, head_sha: str) -> str:
    commit = gh.get(f"repos/{repo}/git/commits/{head_sha}")
    tree = commit.get("tree") if isinstance(commit, dict) else None
    base_tree = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(base_tree, str) or not _SHA_RE.fullmatch(base_tree):
        raise TransferError("could not resolve the base tree for the transfer")
    return base_tree


def _check_root_free(gh: GitHub, repo: str, base_tree: str, root_dir: str) -> None:
    listing = gh.get(f"repos/{repo}/git/trees/{base_tree}")
    if not isinstance(listing, dict) or listing.get("truncated"):
        raise TransferError(
            "cannot verify the repository root safely (listing truncated); "
            "refusing to write"
        )
    entries = listing.get("tree")
    if not isinstance(entries, list):
        raise TransferError("GitHub returned an unexpected tree listing")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") == root_dir:
            raise TransferError(
                f"destination directory {root_dir}/ already exists in the "
                "repository; refusing to overwrite"
            )


def _state_note(branch: str, state: dict[str, Any]) -> str:
    pr = state.get("pr_url") or "not confirmed"
    pipelines = state.get("pipelines") or []
    pipeline_text = (
        ", ".join(
            f"{item.get('name', '?')} (id {item.get('id', '?')})"
            if isinstance(item, dict)
            else f"{item[0]} (id {item[1]})"
            for item in pipelines
        )
        or "none confirmed"
    )
    finalizer = "configured" if state.get("finalizer") else "not configured"
    return (
        f"partial state: branch '{branch}' "
        + ("exists" if state["ref"] else "may exist (a timed-out request may still have created it)")
        + f"; PR: {pr}; confirmed Azure pipelines: {pipeline_text}; "
        f"post-merge workflow: {finalizer}. "
        "Check both GitHub and Azure DevOps before rerunning — nothing is "
        "retried or deleted automatically"
    )


def _verify_pipeline_config(
    data: dict[str, Any],
    *,
    branch: str,
    path: str,
    name: str,
    repo: str | None = None,
) -> None:
    repository = data.get("repository")
    process = data.get("process")
    actual_name = data.get("name")
    actual_branch = (
        repository.get("defaultBranch") if isinstance(repository, dict) else None
    )
    actual_path = (
        process.get("yamlFilename") if isinstance(process, dict) else None
    )
    if (
        (actual_name is not None and actual_name != name)
        or actual_branch != branch
        or actual_path != path
    ):
        shown_branch = _clean_remote(actual_branch or "unknown")
        shown_path = _clean_remote(actual_path or "unknown")
        raise TransferError(
            f"Azure pipeline '{name}' did not retain the requested YAML "
            f"location (expected {branch} at {path}; found {shown_branch} "
            f"at {shown_path})"
        )
    if repo is not None and isinstance(repository, dict):
        actual_type = repository.get("type")
        if actual_type is not None and str(actual_type).casefold() != "github":
            raise TransferError(
                f"Azure pipeline '{name}' no longer uses a GitHub repository"
            )
        repo_name = repository.get("name")
        if isinstance(repo_name, str) and repo_name:
            expected_names = {repo.casefold(), repo.rsplit("/", 1)[-1].casefold()}
            if repo_name.casefold() not in expected_names:
                raise TransferError(
                    f"Azure pipeline '{name}' now references a different repository"
                )
        repo_url = repository.get("url")
        if isinstance(repo_url, str) and repo.casefold() not in urllib.parse.unquote(
            repo_url
        ).casefold():
            raise TransferError(
                f"Azure pipeline '{name}' now references a different repository"
            )


def _post_merge_workflow(
    *,
    repo: str,
    branch: str,
    default_branch: str,
    org: str,
    project_id: str,
    transfer_id: str,
    pipelines: list[tuple[int, PipelineFile]],
) -> tuple[str, str]:
    """Return path and content for the post-merge Azure branch fix-up."""
    path = f".github/workflows/gh-to-azdo-finalize-{transfer_id}.yml"
    target_ref = f"refs/heads/{default_branch}"
    specs = [
        {"id": pipeline_id, "name": pipeline.name, "yaml": pipeline.path}
        for pipeline_id, pipeline in pipelines
    ]
    pipelines_json = json.dumps(specs, separators=(",", ":"))
    branch_json = json.dumps(default_branch)
    path_json = json.dumps(path)
    workflow = f"""\
name: gh-to-azdo post-merge {transfer_id}

on:
  push:
    branches: [{branch_json}]
    paths: [{path_json}]
  pull_request:
    types: [closed]
    branches: [{branch_json}]
    paths: [{path_json}]

permissions:
  contents: read

concurrency:
  group: gh-to-azdo-finalize-{transfer_id}
  cancel-in-progress: false

jobs:
  update-pipeline-default-branch:
    if: >-
      github.event_name == 'push' ||
      (github.event_name == 'pull_request' &&
       github.event.pull_request.merged == true &&
       github.event.pull_request.head.ref == '{branch}')
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      GH_REPO: {repo}
      AZURE_DEVOPS_EXT_PAT: ${{{{ secrets.AZURE_DEVOPS_EXT_PAT }}}}
      AZURE_ORG_URL: https://dev.azure.com/{org}
      AZURE_PROJECT: {project_id}
      TARGET_REF: {target_ref}
      PIPELINES_JSON: '{pipelines_json}'
    steps:
      - name: Verify and update Azure Pipelines
        shell: bash
        run: |
          set -euo pipefail
          python3 <<'PY'
          import base64
          import json
          import os
          import urllib.error
          import urllib.parse
          import urllib.request


          def fail(message):
              raise SystemExit(message)


          def response_json(request, service):
              try:
                  with urllib.request.urlopen(request, timeout=30) as response:
                      return json.load(response)
              except urllib.error.HTTPError as exc:
                  exc.read()
                  fail(f"{{service}} request failed with HTTP {{exc.code}}")
              except (urllib.error.URLError, OSError) as exc:
                  fail(f"{{service}} request failed: {{exc}}")
              except json.JSONDecodeError:
                  fail(f"{{service}} returned unreadable JSON")


          def github_file_exists(url, headers):
              request = urllib.request.Request(url, headers=headers)
              try:
                  with urllib.request.urlopen(request, timeout=30) as response:
                      payload = json.load(response)
              except urllib.error.HTTPError as exc:
                  exc.read()
                  if exc.code == 404:
                      return False
                  fail(f"GitHub request failed with HTTP {{exc.code}}")
              except (urllib.error.URLError, OSError) as exc:
                  fail(f"GitHub request failed: {{exc}}")
              except json.JSONDecodeError:
                  fail("GitHub returned unreadable JSON")
              return isinstance(payload, dict) and payload.get("type") == "file"


          def azure_request(url, headers, body=None):
              request_headers = dict(headers)
              data = None
              method = "GET"
              if body is not None:
                  request_headers["Content-Type"] = "application/json"
                  data = json.dumps(body).encode("utf-8")
                  method = "PUT"
              request = urllib.request.Request(
                  url, data=data, headers=request_headers, method=method
              )
              return response_json(request, "Azure DevOps")


          pat = os.environ.get("AZURE_DEVOPS_EXT_PAT", "").strip()
          if not pat:
              fail(
                  "AZURE_DEVOPS_EXT_PAT is not configured as a GitHub secret; "
                  "add an Azure DevOps PAT allowed to edit build definitions, "
                  "then rerun this workflow"
              )
          gh_token = os.environ.get("GH_TOKEN", "")
          if not gh_token:
              fail("GITHUB_TOKEN is unavailable; cannot verify the merged YAML")

          repo = os.environ["GH_REPO"]
          org_url = os.environ["AZURE_ORG_URL"].rstrip("/")
          project = os.environ["AZURE_PROJECT"]
          target_ref = os.environ["TARGET_REF"]
          target_branch = target_ref.removeprefix("refs/heads/")
          if target_branch == target_ref:
              fail("TARGET_REF must be a refs/heads/ branch")
          pipelines = json.loads(os.environ["PIPELINES_JSON"])

          github_headers = {{
              "Accept": "application/vnd.github+json",
              "Authorization": f"Bearer {{gh_token}}",
              "X-GitHub-Api-Version": "2022-11-28",
          }}
          encoded_branch = urllib.parse.quote(target_branch, safe="")
          missing = []
          for pipeline in pipelines:
              yaml_path = pipeline["yaml"]
              encoded_path = "/".join(
                  urllib.parse.quote(part, safe="")
                  for part in yaml_path.split("/")
              )
              url = (
                  f"https://api.github.com/repos/{{repo}}/contents/"
                  f"{{encoded_path}}?ref={{encoded_branch}}"
              )
              if not github_file_exists(url, github_headers):
                  missing.append(yaml_path)
          if missing:
              fail(
                  "generated Azure YAML is not on the target branch yet: "
                  + ", ".join(missing)
              )

          azure_token = base64.b64encode(f":{{pat}}".encode("utf-8")).decode("ascii")
          azure_headers = {{
              "Accept": "application/json",
              "Authorization": f"Basic {{azure_token}}",
          }}
          for pipeline in pipelines:
              pipeline_id = int(pipeline["id"])
              yaml_path = pipeline["yaml"]
              url = (
                  f"{{org_url}}/{{project}}/_apis/build/definitions/"
                  f"{{pipeline_id}}?api-version=7.1"
              )
              definition = azure_request(url, azure_headers)
              repository = definition.get("repository")
              process = definition.get("process")
              if not isinstance(repository, dict) or not isinstance(process, dict):
                  fail(f"Azure DevOps returned an incomplete definition for {{pipeline_id}}")
              current_branch = repository.get("defaultBranch")
              current_path = process.get("yamlFilename")
              if current_branch != target_ref or current_path != yaml_path:
                  repository["defaultBranch"] = target_ref
                  process["yamlFilename"] = yaml_path
                  azure_request(url, azure_headers, definition)
                  refreshed = azure_request(url, azure_headers)
                  refreshed_repository = refreshed.get("repository") or {{}}
                  refreshed_process = refreshed.get("process") or {{}}
                  if (
                      refreshed_repository.get("defaultBranch") != target_ref
                      or refreshed_process.get("yamlFilename") != yaml_path
                  ):
                      fail(f"Azure DevOps did not retain the update for {{pipeline_id}}")
              print(f"{{pipeline['name']}} (id {{pipeline_id}}): {{target_ref}}")
          PY
"""
    return path, workflow


def _push_post_merge_workflow(
    gh: GitHub,
    repo: str,
    *,
    branch: str,
    commit_sha: str,
    tree_sha: str,
    path: str,
    content: str,
) -> str:
    tree = gh.post(
        f"repos/{repo}/git/trees",
        {
            "base_tree": tree_sha,
            "tree": [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": content,
                }
            ],
        },
    )
    new_tree = _sha_of(tree, "post-merge tree")
    commit = gh.post(
        f"repos/{repo}/git/commits",
        {
            "message": "Add post-merge Azure pipeline branch update",
            "tree": new_tree,
            "parents": [commit_sha],
        },
    )
    new_commit = _sha_of(commit, "post-merge commit")
    quoted_branch = urllib.parse.quote(branch, safe="/")
    gh.patch(
        f"repos/{repo}/git/refs/heads/{quoted_branch}",
        {"sha": new_commit, "force": False},
    )
    return new_commit


def _publish_all(
    gh: GitHub,
    azure: Azure,
    *,
    repo: str,
    default_branch: str,
    head_sha: str,
    base_tree: str,
    branch: str,
    transfer_id: str,
    files: list[PipelineFile],
    org: str,
    project_id: str,
    connection_id: str,
    post_merge_switch: bool,
    on_state: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Perform every remote write; report retained state on failure."""
    state: dict[str, Any] = {
        "ref": False,
        "branch_sha": None,
        "pr_number": None,
        "pr_url": None,
        "pipelines": [],
        "finalizer": False,
        "finalizer_path": None,
    }

    def checkpoint() -> None:
        if on_state is not None:
            on_state(state)

    try:
        tree = gh.post(
            f"repos/{repo}/git/trees",
            {
                "base_tree": base_tree,
                "tree": [
                    {
                        "path": pipeline_file.path,
                        "mode": "100644",
                        "type": "blob",
                        "content": pipeline_file.yaml,
                    }
                    for pipeline_file in files
                ],
            },
        )
        tree_sha = _sha_of(tree, "tree")
        commit = gh.post(
            f"repos/{repo}/git/commits",
            {
                "message": "Add migrated Azure Pipelines workflows",
                "tree": tree_sha,
                "parents": [head_sha],
            },
        )
        commit_sha = _sha_of(commit, "commit")
        gh.post(
            f"repos/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
        state["ref"] = True
        state["branch_sha"] = commit_sha
        checkpoint()
        print(f"pushed branch {branch}", file=sys.stderr)

        pr_body = (
            "Review the generated Azure Pipelines YAML before merging. "
            "Existing GitHub Actions workflows are unchanged."
        )
        if post_merge_switch:
            pr_body += (
                " A generated post-merge workflow updates the imported "
                "pipeline default branch after merge; it requires the "
                "AZURE_DEVOPS_EXT_PAT repository secret."
            )
        pr = gh.post(
            f"repos/{repo}/pulls",
            {
                "title": "Migrate selected workflows to Azure Pipelines",
                "head": branch,
                "base": default_branch,
                "draft": True,
                "body": pr_body,
            },
        )
        number = pr.get("number") if isinstance(pr, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            state["pr_number"] = number
            pr_url = f"https://github.com/{repo}/pull/{number}"
        else:
            pr_url = "created (pull request number unknown — check the repo)"
        state["pr_url"] = pr_url
        checkpoint()
        if state["pr_number"] is None:
            raise TransferError(
                "GitHub did not return a pull request number; inspect the "
                "repository before retrying"
            )
        print(f"draft pull request: {pr_url}", file=sys.stderr)

        created: list[tuple[int, PipelineFile]] = []
        pipeline_ref = f"refs/heads/{branch}"
        for pipeline_file in files:
            result = azure.create_pipeline(
                org,
                project_id,
                connection_id,
                repo,
                pipeline_ref,
                pipeline_file,
            )
            pipeline_id = result["id"]
            state["pipelines"].append(
                {
                    "id": pipeline_id,
                    "name": pipeline_file.name,
                    "path": pipeline_file.path,
                }
            )
            checkpoint()
            _verify_pipeline_config(
                azure.pipeline(org, project_id, pipeline_id),
                branch=pipeline_ref,
                path=pipeline_file.path,
                name=pipeline_file.name,
                repo=repo,
            )
            created.append((pipeline_id, pipeline_file))
            print(
                f"created Azure pipeline '{pipeline_file.name}' "
                f"(id {pipeline_id}): "
                f"https://dev.azure.com/{org}/{project_id}/_build?definitionId={pipeline_id}",
                file=sys.stderr,
            )

        if post_merge_switch:
            finalizer_path, finalizer = _post_merge_workflow(
                repo=repo,
                branch=branch,
                default_branch=default_branch,
                org=org,
                project_id=project_id,
                transfer_id=transfer_id,
                pipelines=created,
            )
            finalizer_commit = _push_post_merge_workflow(
                gh,
                repo,
                branch=branch,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                path=finalizer_path,
                content=finalizer,
            )
            state["finalizer"] = True
            state["finalizer_path"] = finalizer_path
            state["branch_sha"] = finalizer_commit
            checkpoint()
            print("added post-merge branch update workflow", file=sys.stderr)
    except TransferError as exc:
        raise TransferError(f"{exc} [{_state_note(branch, state)}]") from exc
    except KeyboardInterrupt as exc:
        raise TransferError(
            f"interrupted during publish [{_state_note(branch, state)}]"
        ) from exc
    return state["pr_url"]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _transfer(
    *,
    org: str | None,
    target: str | None,
    plan_only: bool,
    license_path: Path | None,
    plan_output: Path | None,
    post_merge_switch: bool,
) -> int:
    if plan_output is not None and not plan_only:
        raise TransferError("--output is only valid with --plan")
    if org is not None and target is not None:
        raise TransferError("pass either --org or --target, not both")
    target_parts = _parse_azure_target(target) if target is not None else None
    if org is not None:
        _validate_org(org)

    gh = GitHub()
    azure = Azure()
    transfer_id = uuid.uuid4().hex
    branch = f"gh-to-azdo/{transfer_id}"
    root_dir = f"github-to-azdo-{transfer_id}"

    login = gh.authenticate()
    print(f"GitHub: signed in as {_clean_remote(login)}")

    repos = _list_repos(gh)
    if not repos:
        raise TransferError(
            "no GitHub repositories with push access were found for this "
            "account; transfer requires write access to the repository"
        )
    repo_rows = [
        f"{_clean_remote(repo.get('full_name'))} "
        f"({_clean_remote(repo.get('visibility', 'unknown')).lower()})"
        for repo in repos
    ]
    picks = _pick("GitHub repository", repo_rows)
    repo = str(repos[picks[0]]["full_name"])
    _validate_repo_name(repo)

    detail = gh.get(f"repos/{repo}")
    default_branch = _validate_branch(
        detail.get("default_branch") if isinstance(detail, dict) else None
    )
    head_sha = _resolve_head(gh, repo, default_branch)
    print(f"repository: {repo} (default branch {default_branch} @ {head_sha[:12]})")

    usable, skipped = _list_workflows(gh, repo, head_sha)
    for note in skipped:
        print(f"  skipping {note}", file=sys.stderr)
    if not usable:
        raise TransferError(
            f"no eligible workflow files (.yml/.yaml) found in {repo} under "
            ".github/workflows"
        )
    workflow_rows = [_clean_remote(_workflow_source(entry)) for entry in usable]
    workflow_picks = _pick(
        "workflows to transfer",
        workflow_rows,
        multiple=True,
        allow_all=True,
    )
    selected = sorted(workflow_picks)

    if len(selected) > 1 and not plan_only:
        if license_path is None:
            raise TransferError(
                "publishing more than one workflow requires a Pro license; "
                "pass --license FILE, select one workflow, or use --plan for "
                "a free preview"
            )
        try:
            licensing.verify_license(license_path)
        except licensing.LicenseError as exc:
            raise TransferError(f"license verification failed: {exc}") from exc
        print(f"Pro license verified ({len(selected)} workflows)")

    files = _build_pipeline_files(
        gh,
        repo,
        usable,
        selected,
        transfer_id,
        collect_blockers=plan_only,
        default_branch=default_branch,
    )
    for pipeline_file in files:
        print(
            f"  {pipeline_file.source} -> {pipeline_file.path} "
            f"(pipeline '{pipeline_file.name}')"
        )

    azure.authenticate()
    if target_parts is not None:
        org_name, project_ref = target_parts
        project_name, project_id = _resolve_project(azure, org_name, project_ref)
    elif org is not None:
        org_name = org
        project_name, project_id = _pick_project(azure, org_name)
    else:
        org_name, project_ref = _prompt_azure_target()
        project_name, project_id = _resolve_project(azure, org_name, project_ref)
    connection_name, connection_id = _pick_connection(azure, org_name, project_id)
    _check_name_conflicts(azure, org_name, project_id, files)

    print("\nTransfer plan:")
    print(f"  source:      github.com/{repo} (default branch {default_branch})")
    print(f"  workflows:   {len(files)}")
    for pipeline_file in files:
        print(f"    - {pipeline_file.source} -> {pipeline_file.path}")
    print(f"  Azure:       {org_name} / {project_name} ({project_id})")
    print(f"  connection:  {connection_name} ({connection_id})")
    print(f"  branch:      {branch}")
    print(f"  PR:          draft pull request on {repo}")
    print(
        f"  pipelines:   one per file on refs/heads/{branch} "
        "(YAML is available immediately; first run is not queued)"
    )
    if post_merge_switch:
        print(
            "  post-merge:  workflow switches pipeline default branch to "
            f"refs/heads/{default_branch} (requires AZURE_DEVOPS_EXT_PAT)"
        )
    else:
        print("  post-merge:  switch the pipeline default branch manually")
    print(
        "  unchanged:   existing GitHub Actions workflows are not disabled; "
        "configure secrets, branch policies and runner availability in "
        "Azure DevOps manually"
    )
    print(
        "  note:        the new branch and draft PR can trigger existing "
        "GitHub automation; Azure triggers may run once the YAML is available."
    )
    if plan_only:
        if len(selected) > 1:
            print(
                "Publishing this selection requires a signed Pro license "
                "(--license FILE)."
            )
        if plan_output is not None:
            document = _plan_document(
                transfer_id=transfer_id,
                repo=repo,
                default_branch=default_branch,
                head_sha=head_sha,
                org=org_name,
                project_id=project_id,
                project_name=project_name,
                connection_id=connection_id,
                connection_name=connection_name,
                files=files,
                post_merge_switch=post_merge_switch,
            )
            _write_json_exclusive(plan_output, document, "plan")
            print(f"\nSaved reusable plan to {plan_output}")
        print("\n--plan: no changes were made")
        return 0

    # Read-only publish preflight: the head must not have moved and the
    # destination root must not already exist.
    fresh_head = _resolve_head(gh, repo, default_branch)
    if fresh_head != head_sha:
        raise TransferError(
            "the default branch head changed while you were choosing; "
            "rerun transfer to pick up the new commits"
        )
    base_tree = _base_tree(gh, repo, head_sha)
    _check_root_free(gh, repo, base_tree, root_dir)

    answer = _prompt("Type PUBLISH to create the branch, draft PR and pipelines (anything else cancels): ")
    if answer != "PUBLISH":
        raise _Abort()

    # Final head check immediately before the first write.
    final_head = _resolve_head(gh, repo, default_branch)
    if final_head != head_sha:
        raise TransferError(
            "the default branch head changed during confirmation; "
            "rerun transfer to pick up the new commits"
        )

    _publish_all(
        gh,
        azure,
        repo=repo,
        default_branch=default_branch,
        head_sha=head_sha,
        base_tree=base_tree,
        branch=branch,
        transfer_id=transfer_id,
        files=files,
        org=org_name,
        project_id=project_id,
        connection_id=connection_id,
        post_merge_switch=post_merge_switch,
    )
    if post_merge_switch:
        post_merge_note = (
            "The generated post-merge workflow will switch each pipeline "
            f"default branch to refs/heads/{default_branch}; it requires "
            "the AZURE_DEVOPS_EXT_PAT repository secret."
        )
    else:
        post_merge_note = (
            "After merging, change each pipeline default branch to "
            f"refs/heads/{default_branch} if needed."
        )
    print(
        "\nDone. Azure pipelines point at the migration branch containing the "
        "YAML and can be run on that branch immediately. Merge the draft PR "
        f"to add the YAML to the default branch. {post_merge_note} "
        "No runs were queued by this command."
    )
    return 0


def _new_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": _STATE_FORMAT,
        "version": _MANIFEST_VERSION,
        "plan_sha256": _plan_digest(plan),
        "transfer_id": plan["transfer_id"],
        "status": "applying",
        "branch": {
            "created": False,
            "head_sha": None,
            "deleted": False,
        },
        "pull_request": {
            "number": None,
            "url": None,
            "closed": False,
        },
        "pipelines": [],
        "finalizer": {
            "created": False,
            "path": None,
        },
    }


def _receipt_from_publish(
    receipt: dict[str, Any], publish_state: dict[str, Any]
) -> None:
    receipt["branch"]["created"] = bool(publish_state.get("ref"))
    receipt["branch"]["head_sha"] = publish_state.get("branch_sha")
    receipt["pull_request"] = {
        "number": publish_state.get("pr_number"),
        "url": publish_state.get("pr_url"),
        "closed": False,
    }
    receipt["pipelines"] = [
        {
            "id": pipeline["id"],
            "name": pipeline["name"],
            "path": pipeline["path"],
            "deleted": False,
        }
        for pipeline in publish_state.get("pipelines", [])
    ]
    receipt["finalizer"] = {
        "created": bool(publish_state.get("finalizer")),
        "path": publish_state.get("finalizer_path"),
    }


def _load_receipt(
    path: Path, plan: dict[str, Any], files: list[PipelineFile]
) -> dict[str, Any]:
    receipt = _read_json_file(path, "state")
    expected_keys = {
        "format",
        "version",
        "plan_sha256",
        "transfer_id",
        "status",
        "branch",
        "pull_request",
        "pipelines",
        "finalizer",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise TransferError("state file has an unsupported structure")
    if (
        receipt.get("format") != _STATE_FORMAT
        or type(receipt.get("version")) is not int
        or receipt.get("version") != _MANIFEST_VERSION
        or receipt.get("transfer_id") != plan["transfer_id"]
        or receipt.get("plan_sha256") != _plan_digest(plan)
    ):
        raise TransferError("state file does not belong to this unchanged plan")
    if receipt.get("status") not in {
        "applying",
        "partial",
        "applied",
        "destroying",
        "destroyed",
    }:
        raise TransferError("state file has an invalid lifecycle status")

    branch = receipt.get("branch")
    if not isinstance(branch, dict) or set(branch) != {
        "created",
        "head_sha",
        "deleted",
    }:
        raise TransferError("state file branch record is invalid")
    if type(branch.get("created")) is not bool or type(branch.get("deleted")) is not bool:
        raise TransferError("state file branch status is invalid")
    branch_sha = branch.get("head_sha")
    if branch_sha is not None and (
        not isinstance(branch_sha, str) or not _SHA_RE.fullmatch(branch_sha)
    ):
        raise TransferError("state file branch commit is invalid")
    if branch["created"] != (branch_sha is not None):
        raise TransferError("state file branch creation record is inconsistent")
    if branch["deleted"] and not branch["created"]:
        raise TransferError("state file marks an uncreated branch as deleted")

    pr = receipt.get("pull_request")
    if not isinstance(pr, dict) or set(pr) != {"number", "url", "closed"}:
        raise TransferError("state file pull request record is invalid")
    number = pr.get("number")
    if number is not None and (type(number) is not int or number < 1):
        raise TransferError("state file pull request number is invalid")
    if pr.get("url") is not None and not isinstance(pr.get("url"), str):
        raise TransferError("state file pull request URL is invalid")
    if type(pr.get("closed")) is not bool:
        raise TransferError("state file pull request status is invalid")
    if (number is not None or pr["closed"]) and not branch["created"]:
        raise TransferError("state file pull request has no recorded branch")

    finalizer = receipt.get("finalizer")
    if not isinstance(finalizer, dict) or set(finalizer) != {"created", "path"}:
        raise TransferError("state file finalizer record is invalid")
    if type(finalizer.get("created")) is not bool:
        raise TransferError("state file finalizer status is invalid")
    expected_finalizer = (
        f".github/workflows/gh-to-azdo-finalize-{plan['transfer_id']}.yml"
        if plan["post_merge_switch"]
        else None
    )
    if finalizer.get("created") and finalizer.get("path") != expected_finalizer:
        raise TransferError("state file finalizer path does not match the plan")
    if finalizer.get("created") and not branch["created"]:
        raise TransferError("state file finalizer has no recorded branch")
    if not finalizer.get("created") and finalizer.get("path") is not None:
        raise TransferError("state file has an unexpected finalizer path")

    expected_files = {pipeline.name: pipeline.path for pipeline in files}
    pipelines = receipt.get("pipelines")
    if not isinstance(pipelines, list) or len(pipelines) > len(files):
        raise TransferError("state file pipeline record is invalid")
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for item in pipelines:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "name",
            "path",
            "deleted",
        }:
            raise TransferError("state file contains an invalid pipeline record")
        if type(item.get("id")) is not int or item["id"] < 1:
            raise TransferError("state file contains an invalid pipeline id")
        if (
            item.get("name") not in expected_files
            or expected_files[item["name"]] != item.get("path")
            or type(item.get("deleted")) is not bool
        ):
            raise TransferError("state file pipeline does not match the plan")
        if item["id"] in seen_ids or item["name"] in seen_names:
            raise TransferError("state file contains duplicate pipeline records")
        seen_ids.add(item["id"])
        seen_names.add(item["name"])
    if pipelines and (not branch["created"] or number is None):
        raise TransferError("state file pipeline records have no branch or pull request")
    if branch["deleted"] and any(not item["deleted"] for item in pipelines):
        raise TransferError("state file branch is deleted while pipelines remain")
    if receipt["status"] == "destroyed" and (
        (branch["created"] and not branch["deleted"])
        or any(not item["deleted"] for item in pipelines)
    ):
        raise TransferError("state file is marked destroyed with resources remaining")
    if receipt["status"] == "applied" and (
        not branch["created"]
        or number is None
        or len(pipelines) != len(files)
        or (plan["post_merge_switch"] and not finalizer["created"])
    ):
        raise TransferError("state file is marked applied without a complete transfer")
    return receipt


def _validate_apply_license(
    files: list[PipelineFile], license_path: Path | None
) -> None:
    if len(files) <= 1:
        return
    if license_path is None:
        raise TransferError(
            "applying more than one workflow requires a Pro license; "
            "pass --license FILE"
        )
    try:
        licensing.verify_license(license_path)
    except licensing.LicenseError as exc:
        raise TransferError(f"license verification failed: {exc}") from exc


def _verify_plan_source(
    gh: GitHub,
    plan: dict[str, Any],
    files: list[PipelineFile],
) -> tuple[str, str]:
    source = plan["source"]
    repo = source["repository"]
    detail = gh.get(f"repos/{repo}")
    if not isinstance(detail, dict) or detail.get("archived") or detail.get("disabled"):
        raise TransferError("the planned GitHub repository is unavailable")
    permissions = detail.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("push") is not True:
        raise TransferError("GitHub push permission is required to apply this plan")
    if detail.get("default_branch") != source["default_branch"]:
        raise TransferError("the GitHub default branch changed since this plan was made")
    head_sha = _resolve_head(gh, repo, source["default_branch"])
    if head_sha != source["head_sha"]:
        raise TransferError(
            "the GitHub default branch head changed since this plan was made; "
            "create a new plan"
        )

    entries, _skipped = _list_workflows(gh, repo, head_sha)
    positions = {_workflow_source(entry): index for index, entry in enumerate(entries)}
    missing = [pipeline.source for pipeline in files if pipeline.source not in positions]
    if missing:
        raise TransferError(
            "planned workflow(s) are no longer present: " + ", ".join(missing)
        )
    selected = [positions[pipeline.source] for pipeline in files]
    rebuilt = _build_pipeline_files(
        gh,
        repo,
        entries,
        selected,
        plan["transfer_id"],
        default_branch=source["default_branch"],
    )
    if rebuilt != files:
        raise TransferError(
            "the generated YAML no longer matches the saved plan; create a new plan"
        )
    return repo, head_sha


def _apply_plan(path: Path, license_path: Path | None) -> int:
    plan, files = _load_plan(path)
    _validate_apply_license(files, license_path)
    receipt_path = _receipt_path(path)
    if receipt_path.exists():
        raise TransferError(
            f"state file already exists at {receipt_path}; inspect it or destroy "
            "the recorded transfer before applying again"
        )

    gh = GitHub()
    azure = Azure()
    login = gh.authenticate()
    print(f"GitHub: signed in as {_clean_remote(login)}")
    repo, head_sha = _verify_plan_source(gh, plan, files)

    destination = plan["destination"]
    org = destination["organization"]
    azure.authenticate()
    project_name, project_id = _resolve_project(
        azure, org, destination["project_id"]
    )
    if project_id != destination["project_id"]:
        raise TransferError("the Azure DevOps project no longer matches the plan")
    connections = azure.connections(org, project_id)
    if not any(connection.get("id") == destination["connection_id"] for connection in connections):
        raise TransferError(
            "the planned GitHub service connection is no longer ready in the "
            "Azure DevOps project"
        )
    _check_name_conflicts(azure, org, project_id, files)
    base_tree = _base_tree(gh, repo, head_sha)
    _check_root_free(gh, repo, base_tree, plan["root_dir"])

    print("\nSaved transfer plan:")
    print(f"  source:      github.com/{repo} (default branch {plan['source']['default_branch']})")
    print(f"  workflows:   {len(files)}")
    for pipeline in files:
        print(f"    - {pipeline.source} -> {pipeline.path} ({pipeline.name})")
    print(f"  Azure:       {org} / {project_name} ({project_id})")
    print(f"  branch:      {plan['branch']}")
    answer = _prompt(
        f"Type PUBLISH to create this branch, draft PR and pipelines "
        f"(anything else cancels): "
    )
    if answer != "PUBLISH":
        raise _Abort()

    if _resolve_head(gh, repo, plan["source"]["default_branch"]) != head_sha:
        raise TransferError(
            "the GitHub default branch head changed during confirmation; "
            "create a new plan"
        )
    receipt = _new_receipt(plan)
    _write_json_exclusive(receipt_path, receipt, "state")

    def checkpoint(publish_state: dict[str, Any]) -> None:
        _receipt_from_publish(receipt, publish_state)
        _write_json_atomic(receipt_path, receipt, "state")

    try:
        _publish_all(
            gh,
            azure,
            repo=repo,
            default_branch=plan["source"]["default_branch"],
            head_sha=head_sha,
            base_tree=base_tree,
            branch=plan["branch"],
            transfer_id=plan["transfer_id"],
            files=files,
            org=org,
            project_id=project_id,
            connection_id=destination["connection_id"],
            post_merge_switch=plan["post_merge_switch"],
            on_state=checkpoint,
        )
    except TransferError:
        receipt["status"] = "partial"
        _write_json_atomic(receipt_path, receipt, "state")
        raise

    receipt["status"] = "applied"
    _write_json_atomic(receipt_path, receipt, "state")
    print(f"\nTransfer applied. Ownership state: {receipt_path}")
    return 0


def _verify_destroy_targets(
    gh: GitHub,
    azure: Azure,
    plan: dict[str, Any],
    files: list[PipelineFile],
    receipt: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    repo = plan["source"]["repository"]
    branch = plan["branch"]
    branch_state = receipt["branch"]
    pr_state = receipt["pull_request"]
    pr_data: dict[str, Any] | None = None
    branch_data: dict[str, Any] | None = None

    if branch_state["created"] and not branch_state["deleted"]:
        if pr_state["number"] is None:
            raise TransferError(
                "the transfer receipt has no pull request id; inspect GitHub "
                "manually before destroying any resources"
            )
        pr_data = gh.get(f"repos/{repo}/pulls/{pr_state['number']}")
        if (
            not isinstance(pr_data, dict)
            or pr_data.get("number") != pr_state["number"]
            or not isinstance(pr_data.get("head"), dict)
            or not isinstance(pr_data.get("base"), dict)
            or pr_data["head"].get("ref") != branch
            or pr_data["base"].get("ref") != plan["source"]["default_branch"]
        ):
            raise TransferError(
                "the recorded pull request no longer matches this transfer"
            )
        if pr_data.get("merged") is True:
            raise TransferError(
                "the migration pull request is merged; refusing to destroy it "
                "because the generated files are now on the default branch"
            )
        if pr_data.get("state") == "open" and pr_data.get("draft") is not True:
            raise TransferError(
                "the migration pull request is no longer a draft; refusing to close it"
            )
        if pr_data.get("state") not in {"open", "closed"}:
            raise TransferError("the recorded pull request has an unknown state")

        quoted_branch = urllib.parse.quote(branch, safe="/")
        branch_data = gh.get(f"repos/{repo}/git/ref/heads/{quoted_branch}")
        obj = branch_data.get("object") if isinstance(branch_data, dict) else None
        current_sha = obj.get("sha") if isinstance(obj, dict) else None
        if current_sha != branch_state["head_sha"]:
            raise TransferError(
                "the migration branch changed after apply; refusing to delete it"
            )
        pr_head = pr_data["head"].get("sha")
        if pr_head is not None and pr_head != current_sha:
            raise TransferError(
                "the pull request head changed after apply; refusing to destroy it"
            )
    elif any(not pipeline["deleted"] for pipeline in receipt["pipelines"]):
        if pr_state["number"] is None:
            raise TransferError(
                "the transfer receipt has no pull request id; inspect GitHub "
                "manually before destroying Azure pipelines"
            )

    destination = plan["destination"]
    project_name, project_id = _resolve_project(
        azure, destination["organization"], destination["project_id"]
    )
    if project_id != destination["project_id"]:
        raise TransferError("the Azure DevOps project no longer matches the plan")
    expected = {pipeline.name: pipeline for pipeline in files}
    for item in receipt["pipelines"]:
        if item["deleted"]:
            continue
        data = azure.pipeline(destination["organization"], project_id, item["id"])
        pipeline = expected[item["name"]]
        if data.get("name") != pipeline.name:
            raise TransferError(
                f"Azure pipeline id {item['id']} was renamed; refusing to delete it"
            )
        _verify_pipeline_config(
            data,
            branch=f"refs/heads/{branch}",
            path=pipeline.path,
            name=pipeline.name,
            repo=repo,
        )
    return pr_data, branch_data


def _destroy_plan(path: Path) -> int:
    plan, files = _load_plan(path)
    receipt_path = _receipt_path(path)
    if not receipt_path.is_file():
        raise TransferError(
            f"no apply state exists at {receipt_path}; this plan has not been applied"
        )
    receipt = _load_receipt(receipt_path, plan, files)
    if receipt["status"] == "destroyed":
        print("This transfer is already destroyed.")
        return 0
    if (
        receipt["status"] in {"applying", "partial"}
        and not receipt["branch"]["created"]
        and not receipt["pipelines"]
    ):
        raise TransferError(
            "the state file has no confirmed resources; a timed-out write may "
            "still have created remote resources, so inspect GitHub and Azure DevOps"
        )

    gh = GitHub()
    azure = Azure()
    gh.authenticate()
    azure.authenticate()
    pr_data, _branch_data = _verify_destroy_targets(
        gh, azure, plan, files, receipt
    )
    repo = plan["source"]["repository"]
    branch = plan["branch"]
    pipeline_names = [
        pipeline["name"]
        for pipeline in receipt["pipelines"]
        if not pipeline["deleted"]
    ]
    pr_number = receipt["pull_request"]["number"]
    print("\nResources recorded for destruction:")
    print(f"  GitHub PR:  {pr_number or 'none recorded'}")
    print(f"  GitHub ref: {branch if receipt['branch']['created'] and not receipt['branch']['deleted'] else 'none'}")
    for name in pipeline_names:
        print(f"  Azure:      {name}")
    answer = _prompt(
        f"Type DESTROY {plan['transfer_id']} to close/delete only these recorded "
        "resources (anything else cancels): "
    )
    if answer != f"DESTROY {plan['transfer_id']}":
        raise _Abort()

    if receipt["branch"]["created"] and not receipt["branch"]["deleted"]:
        latest_pr = gh.get(f"repos/{repo}/pulls/{pr_number}")
        latest_head = latest_pr.get("head") if isinstance(latest_pr, dict) else None
        latest_base = latest_pr.get("base") if isinstance(latest_pr, dict) else None
        if (
            not isinstance(latest_pr, dict)
            or latest_pr.get("merged") is True
            or not isinstance(latest_head, dict)
            or not isinstance(latest_base, dict)
            or latest_head.get("ref") != branch
            or latest_base.get("ref")
            != plan["source"]["default_branch"]
        ):
            raise TransferError(
                "the pull request changed during confirmation; nothing was destroyed"
            )
        if latest_pr.get("state") == "open" and latest_pr.get("draft") is not True:
            raise TransferError(
                "the pull request is no longer a draft; nothing was destroyed"
            )
        quoted_branch = urllib.parse.quote(branch, safe="/")
        latest_ref = gh.get(f"repos/{repo}/git/ref/heads/{quoted_branch}")
        latest_obj = latest_ref.get("object") if isinstance(latest_ref, dict) else None
        if (
            not isinstance(latest_obj, dict)
            or latest_obj.get("sha") != receipt["branch"]["head_sha"]
        ):
            raise TransferError(
                "the migration branch changed during confirmation; "
                "nothing was destroyed"
            )
        pr_data = latest_pr

    receipt["status"] = "destroying"
    _write_json_atomic(receipt_path, receipt, "state")
    if pr_data is not None and pr_data.get("state") == "open":
        # Re-read the PR directly before closing it to catch a merge or edit
        # made while the confirmation prompt was open.
        latest_pr = gh.get(f"repos/{repo}/pulls/{pr_number}")
        latest_head = latest_pr.get("head") if isinstance(latest_pr, dict) else None
        if (
            not isinstance(latest_pr, dict)
            or latest_pr.get("merged") is True
            or latest_pr.get("state") != "open"
            or latest_pr.get("draft") is not True
            or not isinstance(latest_head, dict)
            or latest_head.get("ref") != branch
        ):
            raise TransferError(
                "the pull request changed during confirmation; nothing further "
                "was destroyed"
            )
        gh.patch(f"repos/{repo}/pulls/{pr_number}", {"state": "closed"})
        receipt["pull_request"]["closed"] = True
        _write_json_atomic(receipt_path, receipt, "state")

    destination = plan["destination"]
    for item in receipt["pipelines"]:
        if item["deleted"]:
            continue
        data = azure.pipeline(
            destination["organization"], destination["project_id"], item["id"]
        )
        _verify_pipeline_config(
            data,
            branch=f"refs/heads/{branch}",
            path=item["path"],
            name=item["name"],
            repo=plan["source"]["repository"],
        )
        if data.get("name") != item["name"]:
            raise TransferError(
                f"Azure pipeline id {item['id']} changed during confirmation; "
                "stopping destruction"
            )
        azure.delete_pipeline(
            destination["organization"], destination["project_id"], item["id"]
        )
        item["deleted"] = True
        _write_json_atomic(receipt_path, receipt, "state")

    if receipt["branch"]["created"] and not receipt["branch"]["deleted"]:
        quoted_branch = urllib.parse.quote(branch, safe="/")
        current_ref = gh.get(f"repos/{repo}/git/ref/heads/{quoted_branch}")
        obj = current_ref.get("object") if isinstance(current_ref, dict) else None
        current_sha = obj.get("sha") if isinstance(obj, dict) else None
        if current_sha != receipt["branch"]["head_sha"]:
            raise TransferError(
                "the migration branch changed during confirmation; Azure pipelines "
                "were removed but the branch was retained"
            )
        gh.delete(f"repos/{repo}/git/refs/heads/{quoted_branch}")
        receipt["branch"]["deleted"] = True
        _write_json_atomic(receipt_path, receipt, "state")

    receipt["status"] = "destroyed"
    _write_json_atomic(receipt_path, receipt, "state")
    print(
        "\nDestroyed the recorded Azure pipelines, closed the draft PR, and "
        "deleted the migration branch. The default branch was not modified."
    )
    return 0


def run_apply(path: Path, *, license_path: Path | None = None) -> int:
    """Apply a saved transfer plan after checking its remote inputs again."""
    try:
        return _apply_plan(path, license_path)
    except _Abort:
        print("apply cancelled; nothing was published", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("apply interrupted; inspect the state file before retrying", file=sys.stderr)
        return 1


def run_destroy(path: Path) -> int:
    """Destroy only resources recorded in an applied plan's state file."""
    try:
        return _destroy_plan(path)
    except _Abort:
        print("destroy cancelled; no resources were removed", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("destroy interrupted; inspect the state file before retrying", file=sys.stderr)
        return 1


def run_transfer(
    *,
    org: str | None = None,
    target: str | None = None,
    plan_only: bool = False,
    license_path: Path | None = None,
    plan_output: Path | None = None,
    post_merge_switch: bool = True,
) -> int:
    """Run the interactive GitHub -> Azure DevOps transfer."""
    try:
        return _transfer(
            org=org,
            target=target,
            plan_only=plan_only,
            license_path=license_path,
            plan_output=plan_output,
            post_merge_switch=post_merge_switch,
        )
    except _Abort:
        print("transfer cancelled; nothing was published", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("transfer interrupted", file=sys.stderr)
        return 1
