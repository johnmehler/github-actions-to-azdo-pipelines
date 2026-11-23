"""Convert GitHub Actions workflow YAML into Azure Pipelines YAML.

The converter is deliberately strict: it is a copy/conversion tool only.  It
never executes workflow code, never contacts a remote, and refuses (blocks)
anything outside a small, explicitly supported subset rather than emitting a
runnable-but-wrong partial pipeline.  Any error-severity issue means the
resulting ``Conversion.pipeline`` is ``None``.
"""

from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.tokens import AliasToken, AnchorToken

_BOOL_TAG = "tag:yaml.org,2002:bool"
_MERGE_TAG = "tag:yaml.org,2002:merge"

MAX_INPUT_BYTES = 1024 * 1024  # 1 MiB per workflow file


class _WorkflowLoader(yaml.SafeLoader):
    """SafeLoader restricted to YAML 1.2 core booleans and strict mappings.

    ``yes``/``no``/``on``/``off`` therefore stay strings (so an unquoted ``on``
    remains a mapping key), while ``true``/``false`` in any letter case are
    booleans.  Mapping keys must be strings, must not repeat, and merge keys
    are rejected.
    """

    def construct_mapping(self, node, deep=False):  # noqa: ANN001, ANN202
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _value_node in node.value:
                if getattr(key_node, "tag", None) == _MERGE_TAG:
                    raise ConstructorError(
                        None, None, "merge keys ('<<') are not supported", key_node.start_mark
                    )
                if not isinstance(key_node, yaml.ScalarNode):
                    raise ConstructorError(
                        None,
                        None,
                        "mapping keys must be scalar strings",
                        key_node.start_mark,
                    )
                key = self.construct_object(key_node, deep=True)
                if not isinstance(key, str):
                    raise ConstructorError(
                        None,
                        None,
                        "mapping keys must be strings",
                        key_node.start_mark,
                    )
                if key in seen:
                    raise ConstructorError(
                        None,
                        None,
                        f"duplicate mapping key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _build_resolvers() -> dict[Any, list[Any]]:
    resolvers: dict[Any, list[Any]] = {}
    for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items():
        resolvers[first] = [(tag, regexp) for tag, regexp in entries if tag != _BOOL_TAG]
    return resolvers


_WorkflowLoader.yaml_implicit_resolvers = _build_resolvers()
_WorkflowLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


@dataclass(frozen=True)
class Issue:
    severity: str  # 'error' or 'warning'
    location: str
    message: str


@dataclass
class Conversion:
    pipeline: dict[str, Any] | None
    issues: list[Issue]


# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(r"[A-Za-z0-9_.\-/]+")
_JOB_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VERSION_RE = re.compile(r"\d+(?:\.(?:\d+|x)){0,2}")
_SECRET_EXPR_RE = re.compile(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_MACRO_RE = re.compile(r"\$\([A-Za-z_][A-Za-z0-9_.-]*\)")
_GH_RUNTIME_RE = re.compile(r"\b(?:GITHUB|RUNNER)_[A-Z0-9_]+", re.IGNORECASE)
_USES_RE = re.compile(r"([^\s@]+)@([^\s@]+)")

_SUPPORTED_EVENTS = ("push", "pull_request", "workflow_dispatch", "schedule")
_SUPPORTED_IMAGES = frozenset(
    {
        "ubuntu-latest",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "windows-latest",
        "windows-2022",
        "windows-2025",
        "macos-latest",
        "macos-14",
        "macos-15",
    }
)
_ACTIONS = {
    "actions/checkout": frozenset({4, 5, 6, 7}),
    "actions/setup-node": frozenset({4, 5, 6, 7}),
    "actions/setup-python": frozenset({5, 6, 7}),
    "actions/upload-artifact": frozenset({4, 5, 6, 7}),
}
_ACTIONS_HELP = (
    "actions/checkout@v4|v5|v6|v7, actions/setup-node@v4|v5|v6|v7, "
    "actions/setup-python@v5|v6|v7, actions/upload-artifact@v4|v5|v6|v7"
)
_ROOT_KEYS = frozenset({"name", "on", "jobs", "permissions", "concurrency"})
_JOB_KEYS = frozenset({"name", "runs-on", "needs", "steps", "timeout-minutes"})
_RUN_STEP_KEYS = frozenset({"name", "run", "shell", "working-directory", "env", "if"})
_USES_STEP_KEYS = frozenset({"name", "uses", "with", "if"})
_EVENT_FILTER_KEYS = {
    "push": frozenset({"branches"}),
    "pull_request": frozenset({"branches"}),
    "workflow_dispatch": frozenset({"inputs"}),
}
_PERMISSION_VALUES = {
    "contents": frozenset({"read", "write", "none"}),
    "actions": frozenset({"read", "none"}),
}
_DISPATCH_INPUT_KEYS = frozenset({"description", "type", "required", "default"})
_ARTIFACT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_CONCURRENCY_GROUP_RE = re.compile(r"[A-Za-z0-9_.-]+")
_INPUT_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CRON_PART_RE = re.compile(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?")
_CRON_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_GIT_LINE_RE = re.compile(r"^[ \t]*git[ \t]+(?:push|pull)\b[^\n]*", re.MULTILINE)
_GIT_PUSH_FULL_RE = re.compile(r"[ \t]*git[ \t]+push[ \t]*\r?$")
_GIT_PULL_FULL_RE = re.compile(
    r"[ \t]*git[ \t]+pull[ \t]+--rebase[ \t]+origin[ \t]+([A-Za-z0-9_./-]+)[ \t]*\r?$"
)
_HEREDOC_RE = re.compile(r"<<-?[ \t]*[\"']?[A-Za-z_][A-Za-z0-9_]*")
_XTRACE_RE = re.compile(
    r"^[ \t]*set[ \t]+(?:-[A-Za-z]*x[A-Za-z]*|-o[ \t]+xtrace)[ \t]*\r?$", re.MULTILINE
)
_STEP_SUMMARY_REDIRECT_RE = re.compile(
    r'>>[ \t]*(?:"\$GITHUB_STEP_SUMMARY"|\$GITHUB_STEP_SUMMARY)[ \t]*(?=\r?$)',
    re.MULTILINE,
)
_SHA_ASSIGN_RE = re.compile(
    r'^([ \t]*)([A-Z_][A-Z0-9_]*)="\$\{GITHUB_SHA::12\}"[ \t]*\r?$', re.MULTILINE
)
_ENV_APPEND_RE = re.compile(
    r'^([ \t]*)echo[ \t]+"([A-Z_][A-Z0-9_]*)=\$([A-Z_][A-Z0-9_]*)"'
    r'[ \t]*>>[ \t]*"?\$GITHUB_ENV"?[ \t]*\r?$',
    re.MULTILINE,
)
_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_INPUT_PATH = r"github\.event\.inputs\.([A-Za-z_][A-Za-z0-9_]*)"
_INPUT_DIRECT_RE = re.compile(_INPUT_PATH)
_INPUT_FLAG_RE = re.compile(
    _INPUT_PATH + r"\s*==\s*'true'\s*&&\s*'(--[A-Za-z0-9_-]+|)'\s*\|\|\s*'(--[A-Za-z0-9_-]+|)'"
)
_INPUT_FALLBACK_RE = re.compile(_INPUT_PATH + r"\s*\|\|\s*'([A-Za-z0-9_-]+)'")
_FALLBACK_PREFIX_RE = re.compile(r"--[A-Za-z0-9-]+=$")
_CONDITION_INPUT_RE = re.compile(
    r"github\.event\.inputs\.([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=)\s*'true'"
)
_AZURE_VAR_RESERVED = ("GHA_", "GITHUB_", "RUNNER_", "SYSTEM_", "AGENT_", "BUILD_")
_GIT_PROLOGUE = (
    "case \"$BUILD_SOURCEBRANCH\" in refs/heads/*) ;; *) "
    "echo 'Git updates require a branch run' >&2; exit 1;; esac\n"
    "if [[ -z \"$GHA_MIGRATION_GIT_TOKEN\" || \"$GHA_MIGRATION_GIT_TOKEN\" == '$('* ]]; then\n"
    "  echo 'Set GH_SECRET_GIT_WRITE_TOKEN as an Azure secret for GitHub "
    "repository writes' >&2\n"
    "  exit 1\n"
    "fi\n"
    "gha_migration_git() (\n"
    "  set +x\n"
    "  unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE\n"
    "  local auth origin\n"
    "  origin=$(git remote get-url origin) || exit 1\n"
    "  if [[ ! \"$origin\" =~ ^https://github\\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$ ]]; then\n"
    "    echo 'origin must be an HTTPS github.com repository URL without "
    "embedded credentials' >&2\n"
    "    exit 1\n"
    "  fi\n"
    "  auth=$(printf 'x-access-token:%s' \"$GHA_MIGRATION_GIT_TOKEN\" | base64 | tr -d '\\r\\n')\n"
    "  local action=\"$1\"\n"
    "  shift\n"
    "  if [[ \"$action\" == pull ]]; then\n"
    "    set -- pull --rebase \"$origin\" \"$1\"\n"
    "  else\n"
    "    set -- push \"$origin\" \"$1\"\n"
    "  fi\n"
    "  GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 "
    "GIT_CONFIG_KEY_0=http.https://github.com/.extraheader "
    "GIT_CONFIG_VALUE_0=\"AUTHORIZATION: basic $auth\" "
    "GIT_CONFIG_KEY_1=http.followRedirects GIT_CONFIG_VALUE_1=false git \"$@\"\n"
    ")\n"
)
_SUMMARY_PROLOGUE = (
    'GHA_MIGRATION_SUMMARY=$(mktemp "${AGENT_TEMPDIRECTORY}/gha-summary-XXXXXX")\n'
    "trap 'if [ -s \"$GHA_MIGRATION_SUMMARY\" ]; then printf "
    '"##vso[task.uploadsummary]%s\\n" "$GHA_MIGRATION_SUMMARY"; fi\' EXIT\n'
)

_FAILED = object()


class _Ctx:
    """Collects issues and cross-file conversion state."""

    __slots__ = (
        "issues",
        "has_secrets",
        "secret_variables",
        "default_branch",
        "dispatch_inputs",
        "required_inputs",
        "automatic_events",
        "git_write",
        "git_write_used",
        "artifact_names",
    )

    def __init__(self, default_branch: str | None = None) -> None:
        self.issues: list[Issue] = []
        self.has_secrets = False
        self.secret_variables: set[str] = set()
        self.default_branch = default_branch
        self.dispatch_inputs: dict[str, dict[str, Any]] = {}
        self.required_inputs: list[str] = []
        self.automatic_events = False
        self.git_write = False
        self.git_write_used = False
        self.artifact_names: set[str] = set()

    def error(self, location: str, message: str) -> None:
        self.issues.append(Issue("error", location, message))

    def warn(self, location: str, message: str) -> None:
        self.issues.append(Issue("warning", location, message))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _exc_location(exc: yaml.YAMLError) -> str:
    for attr in ("problem_mark", "context_mark"):
        mark = getattr(exc, attr, None)
        if mark is not None:
            return f"line {mark.line + 1}, column {mark.column + 1}"
    return "document"


def _exc_message(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None)
    if problem:
        return str(problem)
    return "invalid YAML"


def _parse_document(text: str, ctx: _Ctx) -> Any:
    """Parse ``text`` into Python objects, or ``_FAILED`` on fatal errors."""
    try:
        tokens = list(yaml.scan(text, Loader=_WorkflowLoader))
    except yaml.YAMLError as exc:
        ctx.error(_exc_location(exc), _exc_message(exc))
        return _FAILED
    except RecursionError:
        ctx.error("document", "workflow is too deeply nested")
        return _FAILED

    for token in tokens:
        if isinstance(token, (AnchorToken, AliasToken)):
            ctx.error("document", "anchors and aliases are not supported")
            return _FAILED

    loader = _WorkflowLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            ctx.error("document", "workflow document is empty")
            return _FAILED
        return loader.construct_document(node)
    except yaml.YAMLError as exc:
        ctx.error(_exc_location(exc), _exc_message(exc))
        return _FAILED
    except RecursionError:
        ctx.error("document", "workflow is too deeply nested")
        return _FAILED
    finally:
        loader.dispose()


# ---------------------------------------------------------------------------
# Source-string checks (applied to every user string that reaches the output)
# ---------------------------------------------------------------------------


def _check_expressions(value: str, location: str, ctx: _Ctx) -> None:
    if "${{" in value or "$[" in value:
        ctx.error(
            location,
            "GitHub expression syntax is not supported; rewrite this value manually",
        )
    if _MACRO_RE.search(value):
        ctx.error(
            location,
            "Azure DevOps macro syntax $(...) is not allowed here; rewrite this value manually",
        )


def _check_runtime_vars(value: str, location: str, ctx: _Ctx) -> None:
    if _GH_RUNTIME_RE.search(value):
        ctx.error(
            location,
            "GITHUB_*/RUNNER_* runtime variables have no Azure DevOps equivalent; "
            "rewrite this step manually",
        )


def _check_script(value: str, location: str, ctx: _Ctx) -> None:
    _check_expressions(value, location, ctx)
    _check_runtime_vars(value, location, ctx)


def _check_working_directory(value: str, location: str, ctx: _Ctx) -> None:
    _check_expressions(value, location, ctx)
    if not value:
        ctx.error(location, "working-directory must be a non-empty string")
    if value.startswith("/"):
        ctx.error(location, "working-directory must be relative (no leading '/')")
    if "\\" in value:
        ctx.error(location, "working-directory must not contain backslashes")
    if re.match(r"^[A-Za-z]:", value):
        ctx.error(location, "working-directory must not contain a drive letter")
    if ".." in value.split("/"):
        ctx.error(location, "working-directory must not contain '..' components")


# ---------------------------------------------------------------------------
# Triggers (`on:`)
# ---------------------------------------------------------------------------


def _parse_branches(value: Any, location: str, ctx: _Ctx) -> list[str] | None:
    if not isinstance(value, list):
        ctx.error(location, "'branches' must be a list of branch names")
        return None
    if not value:
        ctx.error(location, "'branches' must be a non-empty list")
        return None
    branches: list[str] = []
    for index, item in enumerate(value):
        item_loc = f"{location}[{index}]"
        if not isinstance(item, str) or not _BRANCH_RE.fullmatch(item):
            ctx.error(
                item_loc,
                f"invalid branch name {item!r}: only literal branch names "
                "(letters, digits, '_', '.', '-', '/') are supported",
            )
        elif item.startswith("refs/"):
            ctx.error(item_loc, f"branch {item!r}: use the short name, not a full refs/ name")
        else:
            branches.append(item)
    return branches


def _cron_field_ok(field: str, lo: int, hi: int) -> bool:
    """Validate one cron field: ``*``, numbers, ranges, lists, ``/`` steps."""
    if not field or not re.fullmatch(r"[0-9*,/\-]+", field):
        return False
    for part in field.split(","):
        match = _CRON_PART_RE.fullmatch(part)
        if match is None:
            return False
        base, end, step = match.group(1), match.group(2), match.group(3)
        if step is not None:
            if int(step) < 1:
                return False
            if base != "*" and end is None:
                return False  # 'N/step' without a range is ambiguous; reject
        if base == "*":
            if end is not None:
                return False
            continue
        low = int(base)
        if not lo <= low <= hi:
            return False
        if end is not None:
            high = int(end)
            if not lo <= high <= hi or high < low:
                return False
    return True


def _parse_schedule(value: Any, location: str, ctx: _Ctx) -> dict[str, Any]:
    if value is None:
        ctx.error(
            location,
            "'schedule' must be a non-empty list of {cron: ...} entries",
        )
        return {}
    if not isinstance(value, list) or not value:
        ctx.error(location, "'schedule' must be a non-empty list of cron entries")
        return {}
    crons: list[str] = []
    for index, item in enumerate(value):
        item_loc = f"{location}[{index}]"
        if not isinstance(item, dict):
            ctx.error(item_loc, "schedule entries must be mappings with a 'cron' key")
            continue
        for key in item:
            if key != "cron":
                ctx.error(
                    f"{item_loc}.{key}",
                    f"unsupported schedule option {key!r} (only 'cron' is supported)",
                )
        cron = item.get("cron")
        if not isinstance(cron, str) or not cron.strip():
            ctx.error(f"{item_loc}.cron", "'cron' must be a non-empty string")
            continue
        fields = cron.split()
        if len(fields) != 5:
            ctx.error(
                f"{item_loc}.cron",
                f"cron expression {cron!r} must have exactly 5 fields "
                "(minute hour day-of-month month day-of-week)",
            )
            continue
        if not all(
            _cron_field_ok(field, lo, hi)
            for field, (lo, hi) in zip(fields, _CRON_FIELD_BOUNDS)
        ):
            ctx.error(
                f"{item_loc}.cron",
                f"cron expression {cron!r} is not supported; use numeric fields, "
                "'*', ranges, comma lists and positive '/step' values only",
            )
            continue
        crons.append(cron)
    return {"crons": crons} if crons else {}


def _parse_dispatch_inputs(inputs: Any, loc: str, ctx: _Ctx) -> None:
    if not isinstance(inputs, dict):
        ctx.error(loc, "workflow_dispatch 'inputs' must be a mapping of input id to definition")
        return
    seen: set[str] = set()
    for name, spec in inputs.items():
        item_loc = f"{loc}.{name}"
        if not isinstance(name, str) or not _INPUT_ID_RE.fullmatch(name):
            ctx.error(item_loc, f"invalid workflow_dispatch input id {name!r}")
            continue
        if name.casefold() in seen:
            ctx.error(item_loc, f"duplicate workflow_dispatch input id {name!r}")
            continue
        seen.add(name.casefold())
        if not isinstance(spec, dict):
            ctx.error(item_loc, "input definition must be a mapping")
            continue
        for key in spec:
            if key not in _DISPATCH_INPUT_KEYS:
                ctx.error(
                    f"{item_loc}.{key}",
                    f"unsupported input option {key!r} (supported: "
                    + ", ".join(sorted(_DISPATCH_INPUT_KEYS))
                    + ")",
                )
        typ = spec.get("type", "string")
        if typ not in ("string", "boolean"):
            ctx.error(
                f"{item_loc}.type",
                f"unsupported input type {typ!r}; supported: 'string', 'boolean'",
            )
            continue
        required = spec.get("required", False)
        if not isinstance(required, bool):
            ctx.error(f"{item_loc}.required", "'required' must be a boolean")
            continue
        description = spec.get("description")
        if description is not None:
            if not isinstance(description, str):
                ctx.error(f"{item_loc}.description", "'description' must be a string")
            else:
                _check_expressions(description, f"{item_loc}.description", ctx)
        default = spec.get("default", False if typ == "boolean" else "")
        if typ == "boolean":
            if not isinstance(default, bool):
                ctx.error(f"{item_loc}.default", "boolean input default must be true or false")
                continue
        elif not isinstance(default, str):
            ctx.error(f"{item_loc}.default", "string input default must be a string")
            continue
        else:
            _check_expressions(default, f"{item_loc}.default", ctx)
            if "\n" in default:
                ctx.warn(
                    item_loc,
                    "multiline string defaults may need the Azure CLI/API to "
                    "supply run parameters rather than the pipeline text box",
                )
        if required and typ == "boolean" and "default" not in spec:
            ctx.error(
                item_loc,
                "required boolean inputs without a default need manual migration",
            )
            continue
        ctx.dispatch_inputs[name] = {
            "name": name,
            "type": typ,
            "default": default,
            "required": required,
            "description": description if isinstance(description, str) else name,
        }
        if typ == "string" and required and default == "":
            ctx.required_inputs.append(name)


def _parse_event_options(event: str, value: Any, location: str, ctx: _Ctx) -> dict[str, Any]:
    if event == "schedule":
        return _parse_schedule(value, location, ctx)
    if value is None:
        return {}
    if not isinstance(value, dict):
        ctx.error(location, f"options for event {event!r} must be a mapping or null")
        return {}
    if event == "workflow_dispatch":
        for key in value:
            if key != "inputs":
                ctx.error(
                    f"{location}.{key}",
                    f"unsupported workflow_dispatch option {key!r} "
                    "(supported: inputs)",
                )
        if "inputs" in value:
            _parse_dispatch_inputs(value["inputs"], f"{location}.inputs", ctx)
        return {}
    allowed = _EVENT_FILTER_KEYS[event]
    options: dict[str, Any] = {}
    for key, item in value.items():
        key_loc = f"{location}.{key}"
        if key not in allowed:
            ctx.error(
                key_loc,
                f"unsupported {event!r} option {key!r} "
                f"(supported: {', '.join(sorted(allowed))})",
            )
            continue
        branches = _parse_branches(item, key_loc, ctx)
        if branches is not None:
            options["branches"] = branches
    return options


def _parse_trigger(value: Any, ctx: _Ctx) -> dict[str, dict[str, Any]]:
    """Return ``{event: options}`` for supported events."""
    events: dict[str, dict[str, Any]] = {}
    if value is None or value == [] or value == {}:
        ctx.error("on", "'on' must define at least one event")
        return events
    if isinstance(value, str):
        items: list[Any] = [value]
        option_values: list[Any] = [None]
    elif isinstance(value, list):
        items = value
        option_values = [None] * len(value)
    elif isinstance(value, dict):
        items = list(value.keys())
        option_values = [value[key] for key in items]
    else:
        ctx.error("on", "'on' must be an event name, a list of event names, or a mapping")
        return events

    seen: set[str] = set()
    for index, (event, options) in enumerate(zip(items, option_values)):
        if isinstance(value, dict):
            loc = f"on.{event}"
        elif isinstance(value, list):
            loc = f"on[{index}]"
        else:
            loc = "on"
        if not isinstance(event, str):
            ctx.error(loc, "event names must be strings")
            continue
        if event in seen:
            ctx.error(loc, f"duplicate event {event!r}")
            continue
        seen.add(event)
        if event not in _SUPPORTED_EVENTS:
            ctx.error(
                loc,
                f"unsupported event {event!r}; supported events: "
                + ", ".join(_SUPPORTED_EVENTS),
            )
            continue
        events[event] = _parse_event_options(event, options, loc, ctx)
        if event == "workflow_dispatch":
            ctx.warn(
                loc,
                "workflow_dispatch has no Azure Pipelines trigger equivalent; "
                "users must queue the pipeline manually",
            )
        elif event == "pull_request":
            ctx.warn(
                loc,
                "pull_request maps to the Azure 'pr' trigger: YAML PR validation "
                "is supported in GitHub-connected pipelines; for Azure Repos, "
                "configure branch build-validation policies",
            )
            ctx.warn(
                loc,
                "GitHub pull_request fork and access semantics differ from "
                "Azure DevOps; review fork PR approval and access settings manually",
            )
    return events


def _build_triggers(
    events: dict[str, dict[str, Any]], ctx: _Ctx
) -> tuple[Any, Any, list[dict[str, Any]]]:
    push_branches = events.get("push", {}).get("branches")
    if "push" in events:
        if push_branches is None:
            include = ["refs/heads/*", "refs/tags/*"]
        else:
            include = [f"refs/heads/{branch}" for branch in push_branches]
        trigger: Any = {"branches": {"include": include}}
    else:
        trigger = "none"

    pr_branches = events.get("pull_request", {}).get("branches")
    if "pull_request" in events:
        # GitHub does not supersede an in-progress PR run by default.
        pr: Any = {
            "autoCancel": False,
            "branches": {"include": pr_branches if pr_branches is not None else ["*"]},
        }
    else:
        pr = "none"

    schedules: list[dict[str, Any]] = []
    crons = events.get("schedule", {}).get("crons") or []
    if crons:
        if ctx.default_branch is None:
            ctx.error(
                "on.schedule",
                "scheduled workflows need a known default branch; pass "
                "--default-branch NAME or use `transfer` (which uses the "
                "repository default branch)",
            )
        else:
            branches = _parse_branches([ctx.default_branch], "on.schedule.branches", ctx)
            if branches:
                for index, cron in enumerate(crons):
                    schedules.append(
                        {
                            "cron": cron,
                            "displayName": f"GitHub schedule {index + 1}",
                            "branches": {"include": branches},
                            "always": True,
                        }
                    )
                    ctx.warn(
                        f"on.schedule[{index}]",
                        "after cutover, retire or disable the original GitHub "
                        "schedule; Azure schedule overrides configured in the "
                        "pipeline UI take precedence over this YAML",
                    )
    return trigger, pr, schedules


def _parse_permissions(node: Any, events: dict[str, dict[str, Any]], ctx: _Ctx) -> None:
    if node is None:
        return
    loc = "permissions"
    if not isinstance(node, dict):
        ctx.error(loc, "'permissions' must be a mapping of permission to access level")
        return
    for key, value in node.items():
        key_loc = f"{loc}.{key}"
        allowed = _PERMISSION_VALUES.get(key)
        if allowed is None:
            ctx.error(
                key_loc,
                f"unsupported permission {key!r} (supported: contents, actions)",
            )
            continue
        if not isinstance(value, str) or value not in allowed:
            ctx.error(
                key_loc,
                f"permission {key!r} must be one of: "
                + ", ".join(sorted(allowed)),
            )
            continue
        if key == "contents" and value == "write":
            ctx.git_write = True
    if ctx.git_write and "pull_request" in events:
        ctx.error(
            loc,
            "contents: write cannot be combined with pull_request: fork pull "
            "requests must never receive repository write credentials",
        )
    ctx.warn(
        loc,
        "GitHub Actions GITHUB_TOKEN permission levels are not transferred; "
        "the Azure pipeline runs under its own service identity",
    )


def _parse_concurrency(node: Any, ctx: _Ctx) -> bool:
    """Return True when a supported ``concurrency`` block was parsed."""
    if node is None:
        return False
    loc = "concurrency"
    if not isinstance(node, dict):
        ctx.error(loc, "'concurrency' must be a mapping")
        return False
    for key in node:
        if key not in ("group", "cancel-in-progress"):
            ctx.error(
                f"{loc}.{key}",
                f"unsupported concurrency option {key!r} "
                "(supported: group, cancel-in-progress)",
            )
    ok = True
    group = node.get("group")
    if not isinstance(group, str) or not _CONCURRENCY_GROUP_RE.fullmatch(group):
        ctx.error(
            f"{loc}.group",
            "'group' must be a literal name (letters, digits, '_', '.', '-')",
        )
        ok = False
    if node.get("cancel-in-progress") is not False:
        ctx.error(
            f"{loc}.cancel-in-progress",
            "only 'cancel-in-progress: false' is supported; GitHub's "
            "pending-run replacement has no Azure DevOps equivalent",
        )
        ok = False
    if ok:
        ctx.warn(
            loc,
            "concurrency maps to an Azure stage-level sequential lock scoped "
            "to this pipeline: queued runs wait instead of GitHub replacing "
            "pending runs; review locks shared across pipelines before "
            "enabling",
        )
    return ok


def _dispatch_parameters(ctx: _Ctx) -> list[dict[str, Any]] | None:
    if not ctx.dispatch_inputs:
        return None
    return [
        {
            "name": spec["name"],
            "displayName": spec["description"],
            "type": spec["type"],
            "default": spec["default"],
        }
        for spec in ctx.dispatch_inputs.values()
    ]


def _convert_condition(value: Any, loc: str, ctx: _Ctx) -> str | None:
    if not isinstance(value, str) or not value.strip():
        ctx.error(loc, "'if' must be a non-empty condition string")
        return None
    text = value.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    if text == "always()":
        return "always()"
    match = _CONDITION_INPUT_RE.fullmatch(text)
    if match is not None:
        name, op = match.group(1), match.group(2)
        spec = ctx.dispatch_inputs.get(name)
        if spec is None or spec["type"] != "boolean":
            ctx.error(
                loc,
                f"'if' condition on input {name!r} requires a declared "
                "boolean workflow_dispatch input",
            )
            return None
        if ctx.automatic_events and spec["default"] is not False:
            ctx.error(
                loc,
                f"'if' on input {name!r} with automatic triggers (push/"
                "pull_request/schedule) requires the input default to be "
                "false: github.event.inputs is absent on automatic runs",
            )
            return None
        func = "eq" if op == "==" else "ne"
        return f"and(succeeded(), {func}('${{{{ parameters.{name} }}}}', 'true'))"
    ctx.error(
        loc,
        "'if' supports only 'always()' or "
        "github.event.inputs.NAME ==|!= 'true' on a declared boolean input",
    )
    return None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _parse_needs(
    job_node: Any, job_id: str, declared: set[str], ctx: _Ctx
) -> list[str]:
    if not isinstance(job_node, dict):
        return []
    needs = job_node.get("needs")
    if needs is None:
        return []
    loc = f"jobs.{job_id}.needs"
    if isinstance(needs, str):
        refs = [needs]
    elif isinstance(needs, list):
        refs = []
        for index, item in enumerate(needs):
            if not isinstance(item, str):
                ctx.error(f"{loc}[{index}]", "needs entries must be job id strings")
                continue
            refs.append(item)
    else:
        ctx.error(loc, "'needs' must be a job id or a list of job ids")
        return []

    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref in seen:
            ctx.error(loc, f"duplicate needs entry {ref!r}")
            continue
        seen.add(ref)
        if ref not in declared:
            ctx.error(loc, f"needs references unknown job {ref!r}")
            continue
        unique.append(ref)
    return unique


def _check_cycles(graph: dict[str, list[str]], ctx: _Ctx) -> None:
    state: dict[str, int] = {name: 0 for name in graph}  # 0=white 1=gray 2=black
    reported = False

    def visit(name: str, stack: list[str]) -> bool:
        nonlocal reported
        state[name] = 1
        stack.append(name)
        for dep in graph.get(name, []):
            if dep not in state:
                continue
            if state[dep] == 1:
                cycle = stack[stack.index(dep) :] + [dep]
                ctx.error("jobs", "circular job dependency: " + " -> ".join(cycle))
                reported = True
                return True
            if state[dep] == 0 and visit(dep, stack):
                return True
        stack.pop()
        state[name] = 2
        return False

    for name in graph:
        if state[name] == 0 and not reported:
            visit(name, [])


def _convert_env(env_node: Any, loc: str, ctx: _Ctx) -> dict[str, str] | None:
    if not isinstance(env_node, dict):
        ctx.error(loc, "'env' must be a mapping")
        return None
    env: dict[str, str] = {}
    for key, value in env_node.items():
        key_loc = f"{loc}.{key}"
        if not _ENV_KEY_RE.fullmatch(key):
            ctx.error(key_loc, f"invalid environment variable name {key!r}")
            continue
        if key.casefold().startswith("gha_migration_"):
            ctx.error(
                key_loc,
                f"environment variable name {key!r} uses the reserved "
                "GHA_MIGRATION_ prefix",
            )
            continue
        secret = _SECRET_EXPR_RE.fullmatch(value) if isinstance(value, str) else None
        if secret is not None:
            azure_name = "GH_SECRET_" + secret.group(1).upper()
            ctx.has_secrets = True
            ctx.secret_variables.add(azure_name)
            env[key] = f"$({azure_name})"
        elif isinstance(value, str):
            _check_script(value, key_loc, ctx)
            env[key] = value
        elif isinstance(value, bool):
            env[key] = "true" if value else "false"
        elif isinstance(value, int):
            env[key] = str(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                ctx.error(key_loc, "environment variable float value must be finite")
                continue
            env[key] = str(value)
        else:
            ctx.error(
                key_loc,
                "environment variable values must be strings, booleans, "
                "integers, or finite floats",
            )
    return env


def _parse_with(with_node: Any, loc: str, allowed: frozenset[str], ctx: _Ctx) -> dict[str, Any] | None:
    if with_node is None:
        return {}
    if not isinstance(with_node, dict):
        ctx.error(loc, "'with' must be a mapping")
        return None
    for key in with_node:
        if key not in allowed:
            ctx.error(
                f"{loc}.{key}",
                f"unsupported input {key!r} for this action (supported: "
                + ", ".join(sorted(allowed))
                + ")",
            )
    return dict(with_node)


def _checkout_inputs(with_inputs: dict[str, Any], loc: str, ctx: _Ctx) -> dict[str, Any]:
    fetch_depth = 1
    if "fetch-depth" in with_inputs:
        value = with_inputs["fetch-depth"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            ctx.error(f"{loc}.fetch-depth", "fetch-depth must be a non-negative integer")
        else:
            fetch_depth = value
    persist = with_inputs.get("persist-credentials")
    if persist is not None:
        if not isinstance(persist, bool):
            ctx.error(f"{loc}.persist-credentials", "persist-credentials must be a boolean")
        elif persist:
            ctx.error(
                f"{loc}.persist-credentials",
                "persist-credentials: true is not supported; the generated "
                "Azure checkout does not persist credentials",
            )
    else:
        ctx.warn(
            loc,
            "persist-credentials omitted: GitHub defaults it to true, but the "
            "generated Azure checkout does not persist credentials; verify the "
            "pipeline does not rely on stored git credentials",
        )
    return {
        "checkout": "self",
        "fetchDepth": fetch_depth,
        "fetchTags": fetch_depth == 0,
        "clean": True,
        "persistCredentials": False,
    }


def _version_input(with_inputs: dict[str, Any], key: str, loc: str, ctx: _Ctx) -> str | None:
    if key not in with_inputs:
        ctx.error(loc, f"missing required input {key!r}")
        return None
    value = with_inputs[key]
    if isinstance(value, bool):
        ctx.error(f"{loc}.{key}", f"{key} must be a string or integer, not a boolean")
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        ctx.error(f"{loc}.{key}", f"{key} must be a string or integer")
        return None
    if not _VERSION_RE.fullmatch(text):
        ctx.error(
            f"{loc}.{key}",
            f"{key} {text!r} is not a supported version; quote decimal versions "
            "and use forms like '20', '20.x', '20.11' or '20.11.1'",
        )
        return None
    return text


def _setup_node(with_inputs: dict[str, Any], loc: str, ctx: _Ctx) -> dict[str, Any]:
    version = _version_input(with_inputs, "node-version", loc, ctx)
    if version is not None and "." not in version:
        version += ".x"
    ctx.warn(
        loc,
        "Azure DevOps tool installation and version resolution differs from "
        "GitHub; verify the resolved Node.js version in the pipeline",
    )
    if "cache" in with_inputs:
        if with_inputs["cache"] != "npm":
            ctx.error(
                f"{loc}.cache",
                "only 'cache: npm' is supported for actions/setup-node",
            )
        else:
            ctx.warn(
                loc,
                "setup-node 'cache: npm' is intentionally not transferred: "
                "Azure DevOps has no equivalent cache step here and no "
                "cache-dir side effects were observed",
            )
    return {"task": "UseNode@1", "inputs": {"version": version if version is not None else ""}}


def _setup_python(with_inputs: dict[str, Any], loc: str, ctx: _Ctx) -> dict[str, Any]:
    version = _version_input(with_inputs, "python-version", loc, ctx)
    ctx.warn(
        loc,
        "Azure DevOps tool installation and version resolution differs from "
        "GitHub; verify the resolved Python version in the pipeline",
    )
    return {
        "task": "UsePythonVersion@0",
        "inputs": {"versionSpec": version if version is not None else ""},
    }


def _artifact_paths(path_value: Any, loc: str, ctx: _Ctx) -> list[str] | None:
    if not isinstance(path_value, str) or not path_value.strip():
        ctx.error(loc, "'path' must be a non-empty string of repo-relative file paths")
        return None
    paths: list[str] = []
    ok = True
    for raw in path_value.splitlines():
        item = raw.strip()
        if not item:
            continue
        parts = item.split("/")
        if any(
            re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part) is None
            for part in parts
        ):
            ctx.error(
                f"{loc}.path",
                f"artifact path {item!r} must be a safe repo-relative literal "
                "file path: every '/'-separated component must start with a "
                "letter, digit, '_' or '-' and contain only letters, digits, "
                "'_', '-', '.' (no globs, macros, '!'/'#' directives, hidden "
                "or '..' components, backslashes or absolute paths)",
            )
            ok = False
            continue
        paths.append(item)
    if not paths:
        ctx.error(f"{loc}.path", "'path' must list at least one file")
        return None
    return paths if ok else None


def _upload_artifact(
    node: dict[str, Any],
    with_inputs: dict[str, Any],
    loc: str,
    ctx: _Ctx,
    step_index: int,
) -> list[dict[str, Any]] | None:
    name = with_inputs.get("name")
    name_loc = f"{loc}.name"
    if not isinstance(name, str) or not _ARTIFACT_NAME_RE.fullmatch(name):
        ctx.error(
            name_loc,
            "artifact 'name' must be a literal name of letters, digits, "
            "'_', '.', '-' starting with a letter or digit",
        )
        return None
    if name.casefold() in ctx.artifact_names:
        ctx.error(
            name_loc,
            f"duplicate artifact name {name!r}; Azure artifact names must "
            "be unique across the whole pipeline",
        )
        return None
    ctx.artifact_names.add(name.casefold())

    if "retention-days" in with_inputs:
        retention = with_inputs["retention-days"]
        if type(retention) is not int or not 1 <= retention <= 90:
            ctx.error(
                f"{loc}.retention-days",
                "retention-days must be an integer between 1 and 90",
            )
        else:
            ctx.warn(
                loc,
                "upload-artifact retention-days is handled by Azure DevOps "
                "project retention settings, not per-artifact",
            )

    paths = _artifact_paths(with_inputs.get("path"), f"{loc}.with", ctx)
    if paths is None:
        return None
    dirs = [posixpath.dirname(item) or "." for item in paths]
    try:
        common = posixpath.commonpath(dirs)
    except ValueError:
        ctx.error(f"{loc}.with.path", "artifact paths cannot be combined")
        return None
    relative = [posixpath.relpath(item, common) for item in paths]

    folder = f"$(Agent.TempDirectory)/gha_migration_artifact_{step_index}"
    source_folder = "$(Build.SourcesDirectory)" + (
        f"/{common}" if common not in ("", ".") else ""
    )
    copy: dict[str, Any] = {
        "task": "CopyFiles@2",
        "inputs": {
            "SourceFolder": source_folder,
            "Contents": "\n".join(relative),
            "TargetFolder": folder,
        },
    }
    publish: dict[str, Any] = {
        "task": "PublishPipelineArtifact@1",
        "inputs": {
            "targetPath": folder,
            "artifact": name,
            "publishLocation": "pipeline",
        },
    }
    source_name = node.get("name")
    publish_name = (
        source_name
        if isinstance(source_name, str) and source_name
        else f"Publish artifact '{name}'"
    )
    copy["displayName"] = f"{publish_name} (copy files)"
    publish["displayName"] = publish_name
    ctx.warn(
        loc,
        "Azure PublishPipelineArtifact fails the step when no files match; "
        "GitHub upload-artifact only warns — verify the paths exist",
    )
    return [copy, publish]


def _convert_uses_step(
    node: dict[str, Any], loc: str, ctx: _Ctx, step_index: int
) -> list[dict[str, Any]] | None:
    uses = node.get("uses")
    if not isinstance(uses, str):
        ctx.error(f"{loc}.uses", "'uses' must be an action reference string")
        return None
    match = _USES_RE.fullmatch(uses)
    action = match.group(1) if match else uses
    ref = match.group(2) if match else ""
    versions = _ACTIONS.get(action)
    major = None
    if versions is not None and re.fullmatch(r"v[0-9]+", ref):
        try:
            major = int(ref[1:])
        except ValueError:
            major = None
    if versions is None or major not in versions:
        ctx.error(
            f"{loc}.uses",
            f"unsupported action {uses!r}; supported: {_ACTIONS_HELP}",
        )
        return None

    with_loc = f"{loc}.with"
    if action == "actions/checkout":
        with_inputs = _parse_with(node.get("with"), with_loc, frozenset({"fetch-depth", "persist-credentials"}), ctx)
        payload: dict[str, Any] = (
            _checkout_inputs(with_inputs, with_loc, ctx)
            if with_inputs is not None
            else {
                "checkout": "self",
                "fetchDepth": 1,
                "fetchTags": False,
                "clean": True,
                "persistCredentials": False,
            }
        )
        return [payload]
    if action == "actions/setup-node":
        with_inputs = _parse_with(node.get("with"), with_loc, frozenset({"node-version", "cache"}), ctx)
        return [_setup_node(with_inputs if with_inputs is not None else {}, with_loc, ctx)]
    if action == "actions/upload-artifact":
        with_inputs = _parse_with(
            node.get("with"), with_loc, frozenset({"name", "path", "retention-days"}), ctx
        )
        if with_inputs is None:
            return None
        return _upload_artifact(node, with_inputs, loc, ctx, step_index)
    # actions/setup-python
    with_inputs = _parse_with(node.get("with"), with_loc, frozenset({"python-version"}), ctx)
    return [_setup_python(with_inputs if with_inputs is not None else {}, with_loc, ctx)]


@dataclass
class _BashAdapt:
    script: str
    env: dict[str, str]
    prologue: str
    used_git: bool
    used_summary: bool


def _input_env_name(env: dict[str, str]) -> str:
    index = sum(1 for key in env if key.startswith("GHA_MIGRATION_ARG_"))
    return f"GHA_MIGRATION_ARG_{index}"


def _rewrite_input_expressions(
    script: str, loc: str, ctx: _Ctx, env: dict[str, str]
) -> str:
    """Rewrite the bounded ``github.event.inputs`` grammars for Bash.

    Recognized forms become ``$GHA_MIGRATION_ARG_n`` shell references whose
    values are Azure ``parameters.*`` expressions delivered through ``env`` —
    user input is never interpolated into the script text itself.  Anything
    unrecognized is left untouched so ``_check_script`` still blocks it.
    """
    out: list[str] = []
    pos = 0
    for match in _EXPR_RE.finditer(script):
        inner = match.group(1).strip()
        start, end = match.start(), match.end()
        replacement: str | None = None

        flag = _INPUT_FLAG_RE.fullmatch(inner)
        fallback = _INPUT_FALLBACK_RE.fullmatch(inner)
        direct = _INPUT_DIRECT_RE.fullmatch(inner)

        if flag is not None:
            name, arm_true, arm_false = flag.groups()
            spec = ctx.dispatch_inputs.get(name)
            expr_loc = f"{loc}: inputs.{name}"
            if spec is None or spec["type"] != "boolean":
                ctx.error(
                    expr_loc,
                    f"flag expression on input {name!r} requires a declared "
                    "boolean workflow_dispatch input",
                )
            elif arm_true == "" and arm_false == "":
                ctx.error(expr_loc, "flag expression has two empty arms")
            elif arm_true == "" and arm_false != "":
                if name != "dry_run":
                    ctx.error(
                        expr_loc,
                        "inverted flag form ('&& \"\" || fallback') is only "
                        "supported for an input named 'dry_run'",
                    )
                else:
                    ctx.warn(
                        expr_loc,
                        "GitHub '&& \"\" ||' selects the fallback arm even "
                        "when the input is true; the conversion keeps the "
                        "intended empty-on-true behavior — review this step",
                    )
                    if ctx.automatic_events and spec["default"] is not False:
                        ctx.error(
                            expr_loc,
                            f"'if'-style use of input {name!r} with automatic "
                            "triggers requires the input default to be false",
                        )
                    else:
                        var = _input_env_name(env)
                        env[var] = (
                            f"${{{{ iif(eq(parameters.{name}, true), "
                            f"'{arm_true}', '{arm_false}') }}}}"
                        )
                        replacement = f'${{{var}:+"${{{var}}}"}}'
            elif ctx.automatic_events and spec["default"] is not False:
                ctx.error(
                    expr_loc,
                    f"flag expression on input {name!r} with automatic "
                    "triggers requires the input default to be false",
                )
            else:
                var = _input_env_name(env)
                env[var] = (
                    f"${{{{ iif(eq(parameters.{name}, true), "
                    f"'{arm_true}', '{arm_false}') }}}}"
                )
                replacement = f'${{{var}:+"${{{var}}}"}}'
            if replacement is not None:
                # Standalone shell token required (whitespace/EOL boundaries).
                before_ok = start == 0 or script[start - 1] in " \t\n"
                after_ok = end == len(script) or script[end] in " \t\n"
                if not (before_ok and after_ok):
                    ctx.error(
                        expr_loc,
                        "flag expression must be a standalone shell token "
                        "(surrounded by whitespace or line boundaries)",
                    )
                    replacement = None
                    env.pop(var, None)
        elif fallback is not None:
            name, literal = fallback.groups()
            spec = ctx.dispatch_inputs.get(name)
            expr_loc = f"{loc}: inputs.{name}"
            if spec is None or spec["type"] != "string":
                ctx.error(
                    expr_loc,
                    f"fallback expression on input {name!r} requires a "
                    "declared string workflow_dispatch input",
                )
            elif not _FALLBACK_PREFIX_RE.search(script[:start]):
                ctx.error(
                    expr_loc,
                    "fallback input expansion is only supported as the value "
                    "of a '--option=${{ ... }}' argument",
                )
            elif end < len(script) and re.match(r"[A-Za-z0-9_]", script[end]):
                ctx.error(expr_loc, "fallback expression must end at a word boundary")
            elif ctx.automatic_events and spec["default"] not in ("", literal):
                ctx.error(
                    expr_loc,
                    f"fallback on input {name!r} with automatic triggers "
                    "requires an empty default or the literal fallback value",
                )
            else:
                var = _input_env_name(env)
                env[var] = f"${{{{ coalesce(parameters.{name}, '{literal}') }}}}"
                replacement = f'"${{{var}}}"'
        elif direct is not None:
            name = direct.group(1)
            spec = ctx.dispatch_inputs.get(name)
            expr_loc = f"{loc}: inputs.{name}"
            if spec is None or spec["type"] != "string":
                ctx.error(
                    expr_loc,
                    f"input expansion on {name!r} requires a declared "
                    "string workflow_dispatch input",
                )
            else:
                before = script[start - 1] if start > 0 else ""
                after = script[end] if end < len(script) else ""
                if before != '"' or (start >= 2 and script[start - 2] == "\\") or after != '"':
                    ctx.error(
                        expr_loc,
                        "input expansion is only supported as a whole "
                        'double-quoted string: "${{ github.event.inputs.NAME }}"',
                    )
                elif ctx.automatic_events and spec["default"] != "":
                    ctx.error(
                        expr_loc,
                        f"input expansion on {name!r} with automatic triggers "
                        "requires an empty default (inputs are absent on "
                        "automatic runs)",
                    )
                else:
                    var = _input_env_name(env)
                    env[var] = f"${{{{ parameters.{name} }}}}"
                    replacement = f"${{{var}}}"

        if replacement is not None:
            out.append(script[pos:start])
            out.append(replacement)
            pos = end
    out.append(script[pos:])
    return "".join(out)


def _rewrite_summary(script: str) -> tuple[str, bool]:
    """Map exact end-of-line ``>> $GITHUB_STEP_SUMMARY`` appends onto a temp
    file + VSO upload.  Any other use is left for ``_check_script`` to block."""
    rewritten, count = _STEP_SUMMARY_REDIRECT_RE.subn(
        '>> "$GHA_MIGRATION_SUMMARY"', script
    )
    return rewritten, count > 0


def _reserved_azure_var(name: str) -> bool:
    return name.startswith(_AZURE_VAR_RESERVED)


def _rewrite_sha_env(script: str, loc: str, ctx: _Ctx) -> str:
    """Map the bounded ``${GITHUB_SHA::12}`` + ``$GITHUB_ENV`` pair."""
    assigned: set[str] = set()
    for match in _SHA_ASSIGN_RE.finditer(script):
        name = match.group(2)
        if _reserved_azure_var(name):
            ctx.error(
                f"{loc}: env.{name}",
                f"variable name {name!r} uses a reserved Azure DevOps prefix",
            )
            continue
        assigned.add(name)

    def _env_sub(match: re.Match[str]) -> str:
        indent, name, var = match.group(1), match.group(2), match.group(3)
        if name not in assigned or var != name:
            ctx.error(
                loc,
                "GITHUB_ENV append must echo a variable previously assigned "
                "from ${GITHUB_SHA::12} using the same name",
            )
            return match.group(0)
        return (
            f"{indent}printf '##vso[task.setvariable variable={name};"
            f"isReadOnly=true]%s\\n' \"${name}\""
        )

    script = _ENV_APPEND_RE.sub(_env_sub, script)
    return script.replace("${GITHUB_SHA::12}", "${BUILD_SOURCEVERSION::12}")


def _rewrite_git(
    script: str, loc: str, ctx: _Ctx, has_checkout: bool
) -> tuple[str, bool]:
    """Rewrite recognized bare ``git push``/``git pull --rebase`` lines.

    Returns ``(script, rewritten)``.  Preconditions (``contents: write``,
    real checkout, no heredoc/xtrace) are validated by the caller-visible
    errors emitted here; unrecognized git push/pull forms always error.
    """
    lines = list(_GIT_LINE_RE.finditer(script))
    if not lines:
        return script, False

    if not ctx.git_write:
        ctx.error(
            loc,
            "git push/pull steps require 'permissions: contents: write' so "
            "the dedicated Azure secret can be wired in",
        )
        return script, False
    if not has_checkout:
        ctx.error(
            loc,
            "git write steps require an actions/checkout step in the same job",
        )
        return script, False
    if _HEREDOC_RE.search(script):
        ctx.error(
            loc,
            "cannot safely rewrite git commands in scripts containing heredocs",
        )
        return script, False
    if _XTRACE_RE.search(script):
        ctx.error(
            loc,
            "git write steps cannot run with xtrace enabled (set -x / "
            "set -o xtrace) because the token must never reach logs",
        )
        return script, False

    out: list[str] = []
    pos = 0
    rewritten = False
    for match in lines:
        line = match.group(0)
        indent = line[: len(line) - len(line.lstrip())]
        if _GIT_PUSH_FULL_RE.fullmatch(line):
            replacement = (
                f'{indent}gha_migration_git push "HEAD:$BUILD_SOURCEBRANCH"'
            )
        else:
            pull = _GIT_PULL_FULL_RE.fullmatch(line)
            if pull is None:
                ctx.error(
                    loc,
                    "unsupported git push/pull form; only a bare 'git push' "
                    "line and 'git pull --rebase origin <branch>' are supported",
                )
                continue
            branch = pull.group(1)
            if branch.startswith("-") or ".." in branch:
                ctx.error(loc, f"unsafe git pull branch {branch!r}")
                continue
            replacement = f"{indent}gha_migration_git pull {branch}"
        out.append(script[pos : match.start()])
        out.append(replacement)
        pos = match.end()
        rewritten = True
    out.append(script[pos:])
    return "".join(out), rewritten


def _adapt_bash(
    script: str, loc: str, ctx: _Ctx, has_checkout: bool
) -> _BashAdapt:
    env: dict[str, str] = {}
    prologue = ""
    used_git = False
    used_summary = False

    script = _rewrite_input_expressions(script, loc, ctx, env)

    script, used_summary = _rewrite_summary(script)
    if used_summary:
        if re.search(r"^[ \t]*trap\b", script, re.MULTILINE):
            ctx.error(
                loc,
                "step defines its own 'trap'; cannot add the summary upload trap",
            )
        else:
            prologue += _SUMMARY_PROLOGUE

    if "${GITHUB_SHA::12}" in script or "$GITHUB_ENV" in script:
        script = _rewrite_sha_env(script, loc, ctx)

    script, rewritten = _rewrite_git(script, loc, ctx, has_checkout)
    if rewritten:
        used_git = True
        env["GHA_MIGRATION_GIT_TOKEN"] = "$(GH_SECRET_GIT_WRITE_TOKEN)"
        ctx.has_secrets = True
        ctx.secret_variables.add("GH_SECRET_GIT_WRITE_TOKEN")
        ctx.git_write_used = True
        prologue += _GIT_PROLOGUE
        ctx.warn(
            loc,
            "the Azure checkout does not persist credentials and checks out a "
            "detached HEAD; pushes target HEAD:$BUILD_SOURCEBRANCH on branch "
            "runs only, and GitHub branch protections still apply",
        )
    return _BashAdapt(script, env, prologue, used_git, used_summary)


def _convert_run_step(
    node: dict[str, Any],
    loc: str,
    is_windows: bool,
    ctx: _Ctx,
    has_checkout: bool,
) -> dict[str, Any]:
    script = node.get("run")
    valid_script = isinstance(script, str) and bool(script.strip())
    if not valid_script:
        ctx.error(f"{loc}.run", "'run' must be a non-empty script string")
    script_text = script if isinstance(script, str) else ""

    shell = node.get("shell")
    explicit_bash = False
    if shell is None:
        shell_kind = "pwsh" if is_windows else "bash"
    elif shell == "bash":
        if is_windows:
            ctx.error(f"{loc}.shell", "explicit 'bash' is not supported on Windows images; use 'pwsh'")
            shell_kind = "pwsh"
        else:
            shell_kind = "bash"
            explicit_bash = True
    elif shell == "pwsh":
        shell_kind = "pwsh"
    else:
        ctx.error(
            f"{loc}.shell",
            f"unsupported shell {shell!r}; supported: 'pwsh' (all images), "
            "'bash' (non-Windows images only)",
        )
        shell_kind = "pwsh" if is_windows else "bash"

    generated_env: dict[str, str] = {}
    prologue_extra = ""
    used_git = False

    # The step-type key must be the first key in the emitted mapping.
    if shell_kind == "pwsh":
        if valid_script:
            _check_script(script_text, f"{loc}.run", ctx)
        payload: dict[str, Any] = {"pwsh": script_text}
    else:
        if valid_script:
            adapt = _adapt_bash(script_text, f"{loc}.run", ctx, has_checkout)
            script_text = adapt.script
            generated_env = adapt.env
            prologue_extra = adapt.prologue
            used_git = adapt.used_git
            _check_script(script_text, f"{loc}.run", ctx)
        prologue = ("set -eo pipefail\n" if explicit_bash else "set -e\n") + prologue_extra
        payload = {"bash": prologue + script_text}

    working_directory = node.get("working-directory")
    if working_directory is not None:
        if isinstance(working_directory, str):
            _check_working_directory(working_directory, f"{loc}.working-directory", ctx)
            if working_directory:
                payload["workingDirectory"] = working_directory
        else:
            ctx.error(f"{loc}.working-directory", "working-directory must be a string")

    env_node = node.get("env")
    source_env: dict[str, str] | None = None
    if env_node is not None:
        source_env = _convert_env(env_node, f"{loc}.env", ctx)
    merged_env = {**(source_env or {}), **generated_env}
    if merged_env:
        if used_git and any(
            key.upper().startswith("GIT_TRACE") for key in merged_env
        ):
            ctx.error(
                f"{loc}.env",
                "GIT_TRACE* variables are not allowed on git write steps "
                "because they could leak the token into logs",
            )
        payload["env"] = merged_env

    return payload


def _convert_step(
    node: Any,
    loc: str,
    is_windows: bool,
    ctx: _Ctx,
    has_checkout: bool,
    step_index: int,
) -> list[dict[str, Any]] | None:
    if not isinstance(node, dict):
        ctx.error(loc, "step must be a mapping")
        return None
    has_run = "run" in node
    has_uses = "uses" in node
    if has_run and has_uses:
        ctx.error(loc, "step cannot define both 'run' and 'uses'")
        return None
    if not has_run and not has_uses:
        ctx.error(loc, "step must define 'run' or 'uses'")
        return None

    allowed = _RUN_STEP_KEYS if has_run else _USES_STEP_KEYS
    step_kind = "run" if has_run else "uses"
    for key in node:
        if key not in allowed:
            ctx.error(
                f"{loc}.{key}",
                f"unsupported key {key!r} for a {step_kind} step "
                f"(supported: {', '.join(sorted(allowed))})",
            )

    payloads = (
        [_convert_run_step(node, loc, is_windows, ctx, has_checkout)]
        if has_run
        else _convert_uses_step(node, loc, ctx, step_index)
    )
    if payloads is None:
        return None

    name = node.get("name")
    name_str: str | None = None
    if name is not None:
        if isinstance(name, str):
            _check_expressions(name, f"{loc}.name", ctx)
            name_str = name
        else:
            ctx.error(f"{loc}.name", "step name must be a string")

    condition: str | None = None
    if_node = node.get("if")
    if if_node is not None:
        if any("checkout" in payload for payload in payloads):
            ctx.error(f"{loc}.if", "'if' is not supported on checkout steps")
        else:
            condition = _convert_condition(if_node, f"{loc}.if", ctx)

    for payload in payloads:
        if name_str is not None and "displayName" not in payload:
            payload["displayName"] = name_str
        if condition is not None:
            payload["condition"] = condition
    return payloads


def _convert_job(
    job_node: Any,
    job_id: str,
    azure_name: str,
    depends_on: list[str],
    ctx: _Ctx,
) -> dict[str, Any]:
    loc = f"jobs.{job_id}"
    out: dict[str, Any] = {"job": azure_name}
    if not isinstance(job_node, dict):
        ctx.error(loc, "job must be a mapping")
        out["dependsOn"] = depends_on
        out["steps"] = []
        return out

    for key in job_node:
        if key not in _JOB_KEYS:
            ctx.error(
                f"{loc}.{key}",
                f"unsupported job key {key!r} (supported: "
                + ", ".join(sorted(_JOB_KEYS))
                + ")",
            )

    name = job_node.get("name")
    if name is not None:
        if isinstance(name, str):
            _check_expressions(name, f"{loc}.name", ctx)
            out["displayName"] = name
        else:
            ctx.error(f"{loc}.name", "job name must be a string")

    runs_on = job_node.get("runs-on")
    is_windows = False
    if runs_on is None:
        ctx.error(f"{loc}.runs-on", "'runs-on' is required")
    elif not isinstance(runs_on, str):
        ctx.error(f"{loc}.runs-on", "runs-on must be a hosted image name string")
    elif runs_on not in _SUPPORTED_IMAGES:
        ctx.error(
            f"{loc}.runs-on",
            f"unsupported runs-on {runs_on!r}; supported hosted images: "
            + ", ".join(sorted(_SUPPORTED_IMAGES)),
        )
    else:
        out["pool"] = {"vmImage": runs_on}
        is_windows = runs_on.lower().startswith("windows")

    timeout = job_node.get("timeout-minutes")
    if timeout is not None:
        timeout_loc = f"{loc}.timeout-minutes"
        if type(timeout) is not int or not 1 <= timeout <= 360:
            ctx.error(timeout_loc, "timeout-minutes must be an integer between 1 and 360")
        else:
            out["timeoutInMinutes"] = timeout
            ctx.warn(
                timeout_loc,
                "Azure DevOps hosted plans may cap timeoutInMinutes below "
                "the GitHub value; verify the effective limit",
            )

    out["dependsOn"] = depends_on

    steps_node = job_node.get("steps")
    steps: list[dict[str, Any]] = []
    if steps_node is None:
        ctx.error(f"{loc}.steps", "'steps' is required")
    elif not isinstance(steps_node, list):
        ctx.error(f"{loc}.steps", "'steps' must be a list")
    elif not steps_node:
        ctx.error(f"{loc}.steps", "'steps' must contain at least one step")
    else:
        job_has_checkout = any(
            isinstance(step_node, dict)
            and isinstance(step_node.get("uses"), str)
            and step_node["uses"].split("@", 1)[0] == "actions/checkout"
            for step_node in steps_node
        )
        checkout_seen = False
        for index, step_node in enumerate(steps_node):
            step_loc = f"{loc}.steps[{index}]"
            converted = _convert_step(
                step_node, step_loc, is_windows, ctx, job_has_checkout, index
            )
            if converted is None:
                continue
            for step in converted:
                if "checkout" in step:
                    if index != 0 or checkout_seen:
                        ctx.error(
                            f"{step_loc}.uses",
                            "actions/checkout is only supported as the first "
                            "step of a job, and only once",
                        )
                    else:
                        checkout_seen = True
                steps.append(step)
        if not any("checkout" in step for step in steps):
            steps.insert(0, {"checkout": "none"})
        if ctx.required_inputs:
            if is_windows:
                ctx.error(
                    loc,
                    "required workflow_dispatch inputs are not supported on "
                    "Windows jobs (the guard step is Bash-only in this release)",
                )
            else:
                guard_lines = ["set -e"]
                guard_env: dict[str, str] = {}
                for input_index, input_name in enumerate(ctx.required_inputs):
                    env_key = f"GHA_MIGRATION_REQUIRED_{input_index}"
                    guard_lines.append(
                        f'if [ -z "${{{env_key}}}" ]; then '
                        f'echo "Input {input_name} is required" >&2; exit 1; fi'
                    )
                    guard_env[env_key] = f"${{{{ parameters.{input_name} }}}}"
                steps.insert(
                    1,
                    {
                        "bash": "\n".join(guard_lines) + "\n",
                        "env": guard_env,
                        "displayName": "Validate required workflow inputs",
                    },
                )
        if any(
            isinstance(step.get("env"), dict)
            and "GHA_MIGRATION_GIT_TOKEN" in step["env"]
            for step in steps
        ):
            for step in steps:
                if "fetchDepth" in step:
                    step["fetchDepth"] = 0
                    step["fetchTags"] = True
                    ctx.warn(
                        f"{loc}.steps",
                        "git write steps need full history: checkout "
                        "fetchDepth set to 0 (fetchTags enabled)",
                    )
                    break
    out["steps"] = steps
    return out


def _convert_jobs(jobs_node: Any, ctx: _Ctx) -> list[dict[str, Any]] | None:
    if jobs_node is None:
        ctx.error("jobs", "'jobs' is required")
        return None
    if not isinstance(jobs_node, dict):
        ctx.error("jobs", "'jobs' must be a mapping of job id to job definition")
        return None
    if not jobs_node:
        ctx.error("jobs", "'jobs' must define at least one job")
        return None

    # Pass 1: validate job ids and compute Azure names.
    normalized: dict[str, str] = {}
    normalized_lower: dict[str, str] = {}
    for job_id in jobs_node:
        loc = f"jobs.{job_id}"
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            ctx.error(
                loc,
                f"job id {job_id!r} must match {_JOB_ID_RE.pattern}",
            )
            continue
        azure_name = job_id.replace("-", "_")
        renamed = re.sub("deploy", "release", azure_name, flags=re.IGNORECASE)
        if renamed != azure_name:
            ctx.warn(
                loc,
                f"job id {job_id!r} contains 'deploy'; the Azure job name "
                f"was renamed to {renamed!r}",
            )
        azure_name = renamed
        lowered = azure_name.lower()
        if lowered in normalized_lower:
            ctx.error(
                loc,
                f"job id {job_id!r} collides with job id "
                f"{normalized_lower[lowered]!r}: both normalize to {azure_name!r}",
            )
            continue
        normalized[job_id] = azure_name
        normalized_lower[lowered] = job_id

    # Pass 2: needs + cycle detection.
    declared = set(jobs_node.keys())
    needs_map: dict[str, list[str]] = {}
    for job_id in normalized:
        refs = _parse_needs(jobs_node[job_id], job_id, declared, ctx)
        needs_map[job_id] = [normalized[ref] for ref in refs if ref in normalized]
    graph = {normalized[job_id]: deps for job_id, deps in needs_map.items()}
    _check_cycles(graph, ctx)

    # Pass 3: convert each job (Azure 'jobs' is a list of job mappings).
    return [
        _convert_job(jobs_node[job_id], job_id, azure_name, needs_map[job_id], ctx)
        for job_id, azure_name in normalized.items()
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _convert_root(document: Any, ctx: _Ctx) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        ctx.error("document", "workflow must be a YAML mapping")
        return None

    for key in document:
        if key not in _ROOT_KEYS:
            ctx.error(
                str(key),
                f"unsupported top-level key {key!r} (supported: "
                + ", ".join(sorted(_ROOT_KEYS))
                + ")",
            )

    name = document.get("name")
    if name is not None:
        if isinstance(name, str):
            ctx.warn(
                "name",
                "workflow `name` labels the pipeline definition rather than an "
                "Azure run number; configure run numbering separately",
            )
        else:
            ctx.error("name", "workflow `name` must be a string")

    events = _parse_trigger(document.get("on"), ctx)
    ctx.automatic_events = bool(set(events) - {"workflow_dispatch"})
    if ctx.required_inputs and ctx.automatic_events:
        ctx.error(
            "on.workflow_dispatch",
            "required workflow_dispatch inputs have no values on automatic "
            "triggers (push/pull_request/schedule); make the inputs optional "
            "or remove the automatic triggers",
        )
    _parse_permissions(document.get("permissions"), events, ctx)
    azure_jobs = _convert_jobs(document.get("jobs"), ctx)
    trigger, pr, schedules = _build_triggers(events, ctx)
    has_concurrency = _parse_concurrency(document.get("concurrency"), ctx)
    parameters = _dispatch_parameters(ctx)

    if "pull_request" in events and ctx.has_secrets:
        ctx.error(
            "jobs",
            "workflows triggered by pull_request cannot use secrets expressions: "
            "Azure cannot guarantee fork-PR secret isolation, so remove the "
            "secrets usage or the pull_request trigger",
        )
    elif ctx.secret_variables:
        variables = ", ".join(sorted(ctx.secret_variables))
        git_token_note = (
            " GH_SECRET_GIT_WRITE_TOKEN must contain a repo-scoped GitHub token."
            if "GH_SECRET_GIT_WRITE_TOKEN" in ctx.secret_variables
            else ""
        )
        ctx.warn(
            "secrets",
            "GitHub secret values are not transferred; create matching secret "
            f"variables in Azure DevOps yourself: {variables}.{git_token_note}",
        )
    if ctx.git_write and not ctx.git_write_used:
        ctx.warn(
            "permissions",
            "contents: write was declared but no supported git push/pull "
            "step was found; the write access is not transferred",
        )

    pipeline: dict[str, Any] = {}
    if parameters is not None:
        pipeline["parameters"] = parameters
    pipeline["trigger"] = trigger
    pipeline["pr"] = pr
    if schedules:
        pipeline["schedules"] = schedules
    if azure_jobs is not None and has_concurrency:
        pipeline["stages"] = [
            {"stage": "Workflow", "lockBehavior": "sequential", "jobs": azure_jobs}
        ]
    else:
        pipeline["jobs"] = azure_jobs if azure_jobs is not None else []
    return pipeline


def convert(text: str, *, default_branch: str | None = None) -> Conversion:
    """Convert GitHub Actions workflow YAML text to an Azure pipeline dict.

    ``default_branch`` supplies the branch name used by ``schedule``
    triggers; without it, scheduled workflows are blocked.
    """
    ctx = _Ctx(default_branch)
    try:
        document = _parse_document(text, ctx)
        if document is _FAILED:
            return Conversion(None, ctx.issues)
        pipeline = _convert_root(document, ctx)
    except RecursionError:
        ctx.error("document", "workflow is too deeply nested")
        pipeline = None
    except Exception:  # noqa: BLE001 - conversion must never raise
        ctx.error("document", "internal conversion error")
        pipeline = None

    if any(issue.severity == "error" for issue in ctx.issues):
        pipeline = None
    return Conversion(pipeline, ctx.issues)


def render(pipeline: dict[str, Any]) -> str:
    """Render an Azure pipeline dict as YAML text."""
    return yaml.safe_dump(pipeline, sort_keys=False)
