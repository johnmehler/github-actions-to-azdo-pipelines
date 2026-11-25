"""Command line interface for ``gh-to-azdo``.

Subcommands: ``audit``, ``dry-run``, ``migrate`` and ``batch`` work fully
offline; ``transfer``, ``apply`` and ``destroy`` contact GitHub and Azure
DevOps via the local ``gh`` and ``az`` CLIs. The tool never executes workflow
code.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import licensing
from .converter import MAX_INPUT_BYTES, Conversion, Issue, convert, render

_WORKFLOW_SUFFIXES = {".yml", ".yaml"}


class _CliError(Exception):
    """User-facing CLI failure (message already safe to print)."""


def _ensure_utf8(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - best effort for redirected streams
            pass


def _read_limited(path: Path) -> str:
    """Read at most ``MAX_INPUT_BYTES`` bytes and decode as UTF-8."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise _CliError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if len(data) > MAX_INPUT_BYTES:
        raise _CliError(f"{path}: file exceeds the 1 MiB input limit")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _CliError(f"{path}: file is not valid UTF-8") from exc


def _load_conversion(path: Path, default_branch: str | None = None) -> Conversion:
    try:
        text = _read_limited(path)
    except _CliError as exc:
        return Conversion(None, [Issue("error", "file", str(exc))])
    return convert(text, default_branch=default_branch)


def _print_issues(issues: list[Issue], path: Path) -> None:
    for issue in issues:
        print(
            f"{path}:{issue.location}: {issue.severity}: {issue.message}",
            file=sys.stderr,
        )


def _discover_workflows(directory: Path) -> list[Path]:
    """Find workflow files under ``directory`` deterministically."""
    workflows_dir = directory / ".github" / "workflows"
    if workflows_dir.is_dir():
        search_dir = workflows_dir
    else:
        search_dir = directory
    files: list[Path] = []
    try:
        entries = sorted(
            search_dir.iterdir(), key=lambda p: (p.name.casefold(), p.name)
        )
    except OSError as exc:
        raise _CliError(f"cannot list {search_dir}: {exc.strerror or exc}") from exc
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file() and entry.suffix.lower() in _WORKFLOW_SUFFIXES:
            files.append(entry)
    return files


def _require_directory(path: Path, what: str) -> None:
    if not path.is_dir():
        raise _CliError(f"{what} is not a directory: {path}")


def _cmd_audit(path_arg: str, default_branch: str | None = None) -> int:
    path = Path(path_arg)
    if path.is_dir():
        files = _discover_workflows(path)
    elif path.is_file():
        files = [path]
    else:
        raise _CliError(f"no such file or directory: {path_arg}")
    if not files:
        raise _CliError(f"no workflow files (.yml/.yaml) found under {path}")

    report: dict[str, Any] = {"workflows": []}
    for file_path in files:
        conversion = _load_conversion(file_path, default_branch)
        blocked = any(issue.severity == "error" for issue in conversion.issues)
        report["workflows"].append(
            {
                "path": str(file_path),
                "status": "blocked" if blocked else "convertible",
                "issues": [asdict(issue) for issue in conversion.issues],
            }
        )
    print(json.dumps(report, indent=2))
    return 0


def _cmd_dry_run(file_arg: str, default_branch: str | None = None) -> int:
    path = Path(file_arg)
    if not path.is_file():
        raise _CliError(f"not a file: {file_arg}")
    conversion = _load_conversion(path, default_branch)
    _print_issues(conversion.issues, path)
    if conversion.pipeline is None:
        return 1
    sys.stdout.write(render(conversion.pipeline))
    return 0


def _cmd_migrate(
    file_arg: str, output_arg: str, default_branch: str | None = None
) -> int:
    path = Path(file_arg)
    if not path.is_file():
        raise _CliError(f"not a file: {file_arg}")
    conversion = _load_conversion(path, default_branch)
    _print_issues(conversion.issues, path)
    if conversion.pipeline is None:
        return 1
    output = Path(output_arg)
    try:
        with open(output, "x", encoding="utf-8", newline="") as handle:
            handle.write(render(conversion.pipeline))
    except FileExistsError as exc:
        raise _CliError(f"output file already exists: {output}") from exc
    except OSError as exc:
        raise _CliError(f"cannot write {output}: {exc.strerror or exc}") from exc
    print(
        f"wrote {output}; review the YAML and import it into Azure DevOps manually",
        file=sys.stderr,
    )
    return 0


def _cmd_batch(
    directory_arg: str,
    output_dir_arg: str,
    license_arg: str,
    default_branch: str | None = None,
) -> int:
    # The license is verified before any workflow scanning happens.
    try:
        licensing.verify_license(Path(license_arg))
    except licensing.LicenseError as exc:
        raise _CliError(f"license verification failed: {exc}") from exc

    source = Path(directory_arg)
    _require_directory(source, "source directory")
    files = _discover_workflows(source)
    if not files:
        raise _CliError(f"no workflow files (.yml/.yaml) found under {source}")

    conversions: list[tuple[Path, Conversion]] = []
    for file_path in files:
        conversion = _load_conversion(file_path, default_branch)
        _print_issues(conversion.issues, file_path)
        conversions.append((file_path, conversion))

    blocked = [file_path for file_path, conv in conversions if conv.pipeline is None]
    if blocked:
        raise _CliError(
            f"batch aborted: {len(blocked)} of {len(conversions)} workflow(s) "
            "failed to convert; nothing was written"
        )

    stems: dict[str, Path] = {}
    for file_path, _conv in conversions:
        key = file_path.stem.casefold()
        if key in stems:
            raise _CliError(
                f"duplicate output name: {file_path} and {stems[key]} both "
                f"produce '{key}.azure-pipelines.yml'"
            )
        stems[key] = file_path

    output_dir = Path(output_dir_arg)
    try:
        output_dir.mkdir()
    except FileExistsError as exc:
        raise _CliError(f"output directory already exists: {output_dir}") from exc
    except OSError as exc:
        raise _CliError(
            f"cannot create output directory {output_dir}: {exc.strerror or exc}"
        ) from exc

    try:
        for file_path, conversion in conversions:
            destination = output_dir / f"{file_path.stem}.azure-pipelines.yml"
            with open(destination, "x", encoding="utf-8", newline="") as handle:
                handle.write(render(conversion.pipeline))  # type: ignore[arg-type]
    except OSError as exc:
        # The freshly created directory is left in place rather than risk
        # deleting something that changed underneath us.
        raise _CliError(
            f"failed writing batch output; {output_dir} may contain incomplete "
            f"files: {exc.strerror or exc}"
        ) from exc

    print(f"wrote {len(conversions)} pipeline file(s) to {output_dir}", file=sys.stderr)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh-to-azdo",
        description=(
            "Convert supported GitHub Actions workflows to Azure Pipelines YAML. "
            "audit/dry-run/migrate/batch are offline; transfer/apply/destroy use "
            "the local gh and az CLIs. The tool never executes workflow code."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser(
        "audit", help="report convertibility of workflow files as JSON"
    )
    audit.add_argument("path", nargs="?", default=".", help="file or directory (default: .)")
    audit.add_argument(
        "--default-branch",
        metavar="NAME",
        help="default branch name for schedule triggers (short name, no refs/)",
    )

    dry_run = subparsers.add_parser(
        "dry-run", help="print the converted Azure Pipelines YAML to stdout"
    )
    dry_run.add_argument("file", help="workflow file to convert")
    dry_run.add_argument(
        "--default-branch",
        metavar="NAME",
        help="default branch name for schedule triggers (short name, no refs/)",
    )

    migrate = subparsers.add_parser(
        "migrate", help="write the converted YAML to a new file (no overwrite)"
    )
    migrate.add_argument("file", help="workflow file to convert")
    migrate.add_argument("--output", required=True, help="output YAML path (must not exist)")
    migrate.add_argument(
        "--default-branch",
        metavar="NAME",
        help="default branch name for schedule triggers (short name, no refs/)",
    )

    batch = subparsers.add_parser(
        "batch", help="convert all workflows under a directory (requires a license)"
    )
    batch.add_argument("directory", help="repository or workflows directory")
    batch.add_argument("--output-dir", required=True, help="new output directory (must not exist)")
    batch.add_argument("--license", required=True, help="signed license file")
    batch.add_argument(
        "--default-branch",
        metavar="NAME",
        help="default branch name for schedule triggers (short name, no refs/)",
    )

    transfer = subparsers.add_parser(
        "transfer",
        help="select and publish GitHub workflows to Azure Pipelines",
    )
    destination = transfer.add_mutually_exclusive_group()
    destination.add_argument(
        "--target",
        metavar="URL",
        help="Azure DevOps organization/project URL (for example "
        "dev.azure.com/org/project); prompts if omitted",
    )
    destination.add_argument(
        "--org",
        metavar="NAME",
        help="Azure DevOps organization name (select the project interactively)",
    )
    transfer.add_argument(
        "--plan",
        action="store_true",
        help="preview selected workflows without publishing or requiring a license",
    )
    transfer.add_argument(
        "--output",
        metavar="FILE",
        help="save a reusable JSON plan (requires --plan; file must not exist)",
    )
    transfer.add_argument(
        "--license",
        help="signed Pro license file (required to publish more than one "
        "workflow; planning is free)",
    )
    transfer.add_argument(
        "--no-post-merge-switch",
        action="store_true",
        help="do not add the workflow that switches pipeline default branches "
        "to the repository default branch after the draft PR merges",
    )

    apply = subparsers.add_parser(
        "apply", help="apply a saved transfer plan after revalidating its inputs"
    )
    apply.add_argument("plan", help="JSON plan created by transfer --plan --output")
    apply.add_argument("--license", help="signed Pro license file for multiple workflows")
    apply.add_argument(
        "--destroy",
        action="store_true",
        help="destroy the applied transfer recorded for this plan",
    )

    destroy = subparsers.add_parser(
        "destroy", help="destroy only resources recorded for an applied plan"
    )
    destroy.add_argument("plan", help="JSON plan previously applied")
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8(sys.stdout)
    _ensure_utf8(sys.stderr)
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            return _cmd_audit(args.path, args.default_branch)
        if args.command == "dry-run":
            return _cmd_dry_run(args.file, args.default_branch)
        if args.command == "migrate":
            return _cmd_migrate(args.file, args.output, args.default_branch)
        if args.command == "batch":
            return _cmd_batch(
                args.directory, args.output_dir, args.license, args.default_branch
            )
        if args.command == "transfer":
            # Imported lazily so offline commands never pull in the online path.
            from .transfer import TransferError, run_transfer

            try:
                return run_transfer(
                    org=args.org,
                    target=args.target,
                    plan_only=args.plan,
                    license_path=Path(args.license) if args.license else None,
                    plan_output=Path(args.output) if args.output else None,
                    post_merge_switch=not args.no_post_merge_switch,
                )
            except TransferError as exc:
                raise _CliError(str(exc)) from exc
        if args.command in {"apply", "destroy"}:
            from .transfer import TransferError, run_apply, run_destroy

            try:
                if args.command == "destroy" or args.destroy:
                    return run_destroy(Path(args.plan))
                return run_apply(
                    Path(args.plan),
                    license_path=Path(args.license) if args.license else None,
                )
            except TransferError as exc:
                raise _CliError(str(exc)) from exc
        raise _CliError(f"unknown command: {args.command}")
    except _CliError as exc:
        print(f"gh-to-azdo: error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI must fail cleanly
        print(f"gh-to-azdo: unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
