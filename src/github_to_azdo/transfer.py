"""Interactive transfer of GitHub Actions workflows to Azure Pipelines.

``gh-to-azdo transfer`` is the only command that contacts remotes.  It drives
the locally installed ``gh`` and ``az`` CLIs: GitHub stays the source of truth
(code is never copied), converted Azure Pipelines YAML is committed on a new
``gh-to-azdo/<id>`` branch behind a **draft** pull request, and Azure pipeline
definitions use that branch so the YAML is available as soon as each pipeline
is imported.  First-run queueing is disabled.  Existing GitHub Actions
workflows are never disabled or deleted, the default branch is never written
directly, and no pipeline run is triggered by the command itself.
"""

from __future__ import annotations

import base64
import binascii
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
from typing import Any

from . import licensing
from .converter import MAX_INPUT_BYTES, convert, render

_AZ_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # Azure DevOps resource ID

_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
_BRANCH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_ORG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,49}")
_WORKFLOW_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:yml|yaml)", re.IGNORECASE)
_PIPELINE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HTTP_RE = re.compile(r"HTTP (\d{3})")
_MAX_PIPELINE_NAME = 200
_MAX_CONTENTS_ENTRIES = 1000

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
        ", ".join(f"{name} (id {pid})" for name, pid in pipelines)
        or "none confirmed"
    )
    return (
        f"partial state: branch '{branch}' "
        + ("exists" if state["ref"] else "may exist (a timed-out request may still have created it)")
        + f"; PR: {pr}; confirmed Azure pipelines: {pipeline_text}. "
        "Check both GitHub and Azure DevOps before rerunning — nothing is "
        "retried or deleted automatically"
    )


def _publish_all(
    gh: GitHub,
    azure: Azure,
    *,
    repo: str,
    default_branch: str,
    head_sha: str,
    base_tree: str,
    branch: str,
    files: list[PipelineFile],
    org: str,
    project_id: str,
    connection_id: str,
) -> str:
    """Perform every remote write; report retained state on failure."""
    state: dict[str, Any] = {"ref": False, "pr_url": None, "pipelines": []}
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
        print(f"pushed branch {branch}", file=sys.stderr)

        pr = gh.post(
            f"repos/{repo}/pulls",
            {
                "title": "Migrate selected workflows to Azure Pipelines",
                "head": branch,
                "base": default_branch,
                "draft": True,
                "body": (
                    "Review the generated Azure Pipelines YAML before "
                    "merging. Existing GitHub Actions workflows are unchanged."
                ),
            },
        )
        number = pr.get("number") if isinstance(pr, dict) else None
        if isinstance(number, int) and not isinstance(number, bool):
            pr_url = f"https://github.com/{repo}/pull/{number}"
        else:
            pr_url = "created (pull request number unknown — check the repo)"
        state["pr_url"] = pr_url
        print(f"draft pull request: {pr_url}", file=sys.stderr)

        for pipeline_file in files:
            result = azure.create_pipeline(
                org,
                project_id,
                connection_id,
                repo,
                f"refs/heads/{branch}",
                pipeline_file,
            )
            pipeline_id = result["id"]
            state["pipelines"].append((pipeline_file.name, pipeline_id))
            print(
                f"created Azure pipeline '{pipeline_file.name}' "
                f"(id {pipeline_id}): "
                f"https://dev.azure.com/{org}/{project_id}/_build?definitionId={pipeline_id}",
                file=sys.stderr,
            )
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
) -> int:
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
        files=files,
        org=org_name,
        project_id=project_id,
        connection_id=connection_id,
    )
    print(
        "\nDone. Azure pipelines point at the migration branch containing the "
        "YAML. Merge the draft PR to add it to the default branch; then change "
        f"the pipeline default branch to refs/heads/{default_branch} if needed. "
        "No runs were queued by this command."
    )
    return 0


def run_transfer(
    *,
    org: str | None = None,
    target: str | None = None,
    plan_only: bool = False,
    license_path: Path | None = None,
) -> int:
    """Run the interactive GitHub -> Azure DevOps transfer."""
    try:
        return _transfer(
            org=org,
            target=target,
            plan_only=plan_only,
            license_path=license_path,
        )
    except _Abort:
        print("transfer cancelled; nothing was published", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("transfer interrupted", file=sys.stderr)
        return 1
