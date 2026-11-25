# github-to-azdo

CLI that converts a **supported subset** of GitHub Actions workflow YAML into
Azure Pipelines YAML. GitHub Actions is the upstream source; this is the
inverse direction of Microsoft's GitHub Actions Importer: you get a
reviewable `azure-pipelines.yml` you can diff, edit, and import yourself.

`audit`, `dry-run`, `migrate` and `batch` work fully offline. `transfer`,
`apply` and `destroy` use your local `gh` and `az` CLIs to plan, publish, or
remove recorded Azure pipeline migrations.

The tool is a **copy/conversion tool only**. It never executes workflow code.
Only `transfer`, `apply` and `destroy` contact GitHub or Azure DevOps; offline
conversions refuse to emit runnable-but-wrong partial pipelines, reporting
anything outside the supported subset as an error.

## Install

```bash
python -m pip install -e .
```

Requires Python 3.11+. Dependencies: `PyYAML` and `cryptography`.

### Transfer prerequisites

The `transfer` command also requires the GitHub CLI (`gh`) and Azure CLI
(`az`). Install them using the [GitHub CLI instructions](https://github.com/cli/cli#installation)
and the [Azure CLI instructions](https://learn.microsoft.com/cli/azure/install-azure-cli).
On Windows, you can install both with WinGet:

```powershell
winget install --id GitHub.cli --exact
winget install --id Microsoft.AzureCLI --exact
```

Open a new terminal after installation so the CLIs are available on `PATH`.
Install the Azure DevOps extension:

```bash
az extension add --name azure-devops
```

The transfer flow signs in through `gh` and `az` when needed. You can sign in
beforehand with `gh auth login --hostname github.com` and
`az login --allow-no-subscriptions`. Even `transfer --plan` requires both CLIs,
the extension, and Azure DevOps sign-in because it reads the destination
organization, project, service connection, and existing pipeline names; it
does not publish changes. If you belong to multiple directories, sign in to the
one associated with Azure DevOps using
`az login --allow-no-subscriptions --tenant TENANT`.

## Usage

```bash
# Report convertibility of one file or a whole directory (JSON on stdout)
gh-to-azdo audit path/to/repo            # or: gh-to-azdo audit .github/workflows/ci.yml

# Print the converted Azure Pipelines YAML to stdout (diagnostics on stderr)
gh-to-azdo dry-run examples/ci.yml

# Write the converted YAML to a new file (fails if the file exists)
gh-to-azdo migrate examples/ci.yml --output ci.azure-pipelines.yml

# Convert every workflow under a directory; requires a signed license
gh-to-azdo batch path/to/repo --output-dir out/pipelines --license license.json
```

`python -m github_to_azdo ...` works identically.

### What `audit` discovers

Given a directory, `audit`/`batch` use `<dir>/.github/workflows/` when it
exists, otherwise they scan the directory's direct children for `*.yml` /
`*.yaml` files (case-insensitive, sorted by name, symlinks skipped). No
workflow files found is an error.

### What `migrate`/`batch` write

`migrate` writes UTF-8 YAML with exclusive create (`x`) semantics — it never
overwrites. `batch` verifies the license **before** scanning, converts and
validates everything first, refuses duplicate output names (case-insensitive
stems), then creates the output directory exclusively and writes
`<stem>.azure-pipelines.yml` per workflow. The `--output-dir` parent directory
must already exist. If an I/O error happens mid-write the directory is left in
place and the error reports it may contain incomplete files.

These offline commands never publish anything to Azure DevOps — review the
YAML, then import or commit it through your normal Azure Repos flow.

## Transferring workflows to Azure DevOps (`transfer`)

```bash
gh-to-azdo transfer [--target URL | --org NAME] [--plan [--output FILE]] [--license license.json] [--no-post-merge-switch]
gh-to-azdo apply PLAN.json [--license license.json] [--destroy]
gh-to-azdo destroy PLAN.json
```

The remote commands drive the local `gh` and `az` CLIs: the GitHub credential
stays in `gh`'s local store and the Azure DevOps login stays in the local `az`
session — this tool never handles PATs or OAuth apps itself.

- **GitHub auth:** checks `gh auth status --hostname github.com`; if missing it
  runs `gh auth login --hostname github.com --web --skip-ssh-key` (this can
  update `gh`'s local credential store). A token supplied via
  `GH_TOKEN`/`GITHUB_TOKEN` that is rejected is reported — the tool never asks
  for a raw token.
- **Azure auth:** requires the `azure-devops` az extension — install it once
  with `az extension add --name azure-devops` — then reads an access token for
  the Azure DevOps resource; on failure it runs
  `az login --allow-no-subscriptions` (which can update `az`'s local session).
  For a specific directory tenant, run
  `az login --allow-no-subscriptions --tenant TENANT` yourself first.

Flow: pick a repository (only repos where your account has **push** access are
listed — archived/disabled repos are excluded), pick workflows from
`.github/workflows` (comma-separated numbers or `all`), then enter the Azure
DevOps organization/project URL (for example
`https://dev.azure.com/johnmehler/cloudjobs`). Use `--target URL` to supply it
up front. `--org NAME` remains available when you want to select a project from
that organization. Then select an existing **ready GitHub service connection**;
make sure it is authorized for the repository and permitted for pipeline use.
Every selected workflow is converted first; any conversion blocker stops the
whole transfer before Azure is touched.

Publishing writes, in order:

1. One commit on a new `gh-to-azdo/<id>` branch containing
   `github-to-azdo-<id>/<workflow>.yml` files (unique per run — no overwrites,
   deterministic pipeline names `GHA-<owner>-<repo>-<workflow>`; an existing
   Azure pipeline with the same name aborts beforehand).
2. A **draft** pull request — you review and merge it; nothing touches the
   default branch directly.
3. One Azure pipeline definition per file, created against the new
   `gh-to-azdo/<id>` branch via `--yml-path` with `--skip-first-run true`.
   The importer reads the definition back and verifies that Azure retained the
   migration branch and YAML path. This branch already contains the generated
   YAML, so the pipeline can be run as soon as it is imported; the command
   itself never queues a run.
4. A second commit on the same branch adds
   `.github/workflows/gh-to-azdo-finalize-<id>.yml`. After the PR merges, that
   path-filtered workflow verifies the generated YAML exists on the repository
   default branch and updates each Azure pipeline default branch back to
   `refs/heads/<default>`; later transfer scans ignore the helper. Use
   `--no-post-merge-switch` to leave that step manual.

The finalizer needs a GitHub repository secret named `AZURE_DEVOPS_EXT_PAT`
containing an Azure DevOps PAT for a user allowed to edit build definitions
(Build — Read & Execute scope). The value is never requested or printed by
`gh-to-azdo`; if the secret is absent, the post-merge workflow fails without
changing the pipeline and can be rerun after the secret is added.

`--plan` performs all reads and previews the selections and the
file→pipeline mapping without remote writes (sign-in still happens) — it is
free for any number of workflows. Add `--output migration.plan.json` to save
a reusable JSON plan. The file contains the selected workflow snapshot and
converted YAML, plus the source commit, destination IDs, pipeline names and
paths; it contains no CLI credentials. The outfile is created exclusively
and is never overwritten.

Apply the saved plan without repeating the selections:

```bash
gh-to-azdo apply migration.plan.json [--license license.json]
```

Apply rechecks the source commit and regenerated YAML, destination project,
service connection, and pipeline-name conflicts before asking you to type
`PUBLISH`. It refuses stale or edited plans. A sidecar
`migration.plan.json.state.json` records each confirmed remote resource as it
is created, so partial applies can be inspected or cleaned up.

Destroy the resources recorded for that plan after use:

```bash
gh-to-azdo destroy migration.plan.json
# Equivalent:
gh-to-azdo apply migration.plan.json --destroy
```

Destroy revalidates the recorded draft PR, branch, and pipeline definitions,
then requires typing `DESTROY <transfer-id>`. It closes the unmerged draft PR,
deletes only the pipelines recorded in the state file, and deletes the
migration branch. It never disables source GitHub Actions. It refuses to
destroy a merged migration PR because its generated YAML is then on the
default branch; it does not remove or rewrite default-branch files. Keep the
plan and its state sidecar together.

Publishing requires typing `PUBLISH`; the default-branch head is re-checked
before the first write and the run aborts if it moved. `q`, empty input, or
EOF at any prompt cancels cleanly.

In `--plan` mode every selected workflow is converted and **all** conversion
blockers are collected and reported together (per-file diagnostics on
stderr), so you see the full list of unsupported constructs before deciding —
nothing is published either way.

**Pro licensing:** previews (`--plan`) are free regardless of how many
workflows you select. **Publishing** more than one workflow in a single run
requires a signed license (`--license FILE`); publishing a single workflow is
free.

Honest limitations:

- Code stays on GitHub — nothing is copied. Pipelines are GitHub-backed and
  need the service connection plus YAML PR/build-validation setup reviewed
  manually.
- The imported pipeline initially uses the `gh-to-azdo/<id>` branch as its
  default branch so Azure can find the YAML before the draft PR is merged.
  The generated finalizer switches it to the repository default branch after
  merge when `AZURE_DEVOPS_EXT_PAT` is configured; otherwise rerun that
  workflow or switch it manually. Use `--no-post-merge-switch` to omit the
  finalizer.
- The command never queues a first run, but automatic triggers **may** apply
  as soon as the YAML is present on a watched branch — the new branch and the
  draft PR itself can trigger existing GitHub automation. Test the generated
  pipelines, then retire the original workflows manually.
- Existing GitHub Actions workflows are left untouched — nothing is disabled
  or deleted, and secrets are not copied (`$(GH_SECRET_*)` variables must be
  configured in Azure DevOps).
- Branch policies, runner availability, and fork-PR semantics need manual
  review in Azure DevOps.
- If no `--target` or `--org` is supplied, transfer prompts for the full
  organization/project URL. Project listing and connection selection still
  require permission in that Azure DevOps organization.
- If a write fails partway (e.g. the PR exists but a pipeline create fails),
  the error prints exactly which branch/PR/pipeline IDs exist and stops —
  there is no automatic rollback or retry. With a saved plan, the state
  sidecar records confirmed resources for an explicit `destroy`; check both
  dashboards before retrying an ambiguous timed-out write.
- Only the supported conversion subset transfers; anything else blocks the
  whole run (use `audit`/`migrate` for manual handling).

## Supported subset (fail-closed)

Root: `name` (warning only — it labels the pipeline definition; Azure run
numbers are configured separately), `on`, `jobs`, `permissions`,
`concurrency`. Anything else (`defaults`, `env`, top-level `if`, …) blocks.
`on` must define at least one event; `jobs` is emitted as an Azure `jobs:`
**list** (or a single-stage `stages:` wrapper when `concurrency` is present).

Triggers:

| GitHub `on` | Azure output |
|---|---|
| `push` (no filters) | `trigger.branches.include: [refs/heads/*, refs/tags/*]` |
| `push.branches: [main, dev]` | `trigger.branches.include: [refs/heads/main, refs/heads/dev]` |
| `pull_request` | `pr: {autoCancel: false, branches: {include: [*]}}` (+ warnings) |
| `pull_request.branches` | `pr.branches.include: [main, dev]` (short names, `autoCancel: false`) |
| `schedule` | `schedules:` entries `{cron, displayName, branches.include:[default], always: true}` |
| `workflow_dispatch` | `trigger: none`, `pr: none` (+ warning that Azure queues manually); declared `inputs` become pipeline `parameters` |
| missing / null / empty `on`, other events | blocked; unsupported events always block |

`schedule` accepts a non-empty list of `{cron: "…"}` maps (exactly five
numeric fields with `*`, ranges, comma lists and positive `/step` values; no
timezones or extra keys). Each cron becomes an Azure schedule pinned to the
**default branch**: pass `--default-branch NAME` to the offline commands, or
use `transfer` (which uses the repository's real default branch). Without a
known branch, scheduled workflows block. After cutover, retire the original
GitHub schedule — Azure schedule overrides set in the pipeline UI win over
the YAML. Before enabling Azure schedules or triggers, review the operations
involved: GitHub `concurrency` groups do not coordinate across pipelines, so
overlapping writer jobs (e.g. two scheduled pipelines pushing to the same
repository) are your responsibility to sequence.

`workflow_dispatch.inputs` supports `type: string` (also the implicit
default when `type` is omitted) and `type: boolean`, with optional
`description`, `required` (boolean) and type-exact `default` (missing →
`''`/`false`). Input ids must match `[A-Za-z_][A-Za-z0-9_]*` and be
case-insensitively unique; other keys block. Inputs become top-level Azure
`parameters` (`name`/`displayName`/`type`/`default`). String inputs marked
`required` with an empty default get a generated `Validate required workflow
inputs` Bash guard step after checkout in every job — required inputs are
rejected when combined with automatic triggers (`push`/`pull_request`/
`schedule`) and on Windows jobs. A `required` boolean must declare an
explicit `default` (Azure cannot distinguish "unset" from `false`); required
booleans get no guard. Multiline string defaults warn that they may need the
Azure CLI/API rather than the run text box.

Branch filters must be literal pattern-free names (`[A-Za-z0-9_.\-/]+`, no
`refs/` prefix, no globs). `paths`, `tags`, `types`, etc. block.

A workflow that has **both** a `pull_request` trigger and `${{ secrets.* }}`
usage is blocked: Azure cannot guarantee fork-PR secret isolation. PR triggers
emit `autoCancel: false` to match GitHub's default of not superseding
in-progress runs; YAML PR validation is supported in GitHub-connected
pipelines, while Azure Repos needs branch build-validation policies — review
fork PR and access settings manually.

`permissions` accepts only a mapping with `contents`/`actions` keys valued
`read`, `none` (plus `contents: write`). GitHub `GITHUB_TOKEN` permission
levels are **not** transferred — the Azure pipeline runs under its own
service identity (warning emitted). `contents: write` enables the bounded
git-push rewrite below and is rejected together with `pull_request`.

`concurrency` accepts exactly `{group: <literal [A-Za-z0-9_.-]+>,
cancel-in-progress: false}` and wraps the jobs in
`stages: [{stage: Workflow, lockBehavior: sequential, …}]` — an Azure
stage-level exclusive lock where queued runs wait instead of GitHub's
pending-run replacement (warning emitted; review cross-pipeline locks).
`cancel-in-progress: true` or extra keys block.

Jobs: `name`, `runs-on`, `needs`, `steps`, `timeout-minutes`. Job ids matching
`[A-Za-z_][A-Za-z0-9_-]*` are normalized (`-` → `_`, then `deploy` →
`release` case-insensitively with a warning — `deploy` is a reserved Azure
stage keyword); case-insensitive collisions block. `needs` supports
strings/lists of known jobs; cycles block. `dependsOn` is always emitted
explicitly. `timeout-minutes` must be an integer 1–360 → `timeoutInMinutes`
(warning: Azure hosted plans may cap it).

Images (allowlist): `ubuntu-latest`, `ubuntu-22.04`, `ubuntu-24.04`,
`windows-latest`, `windows-2022`, `windows-2025`, `macos-latest`, `macos-14`,
`macos-15` → `pool.vmImage`. No matrix, self-hosted labels, job-level `if`,
`strategy`, `outputs`, `env`, `container`, `services`, or reusable-workflow
jobs.

Steps: `run` (with `name`, `shell`, `working-directory`, `env`, `if`) or
`uses` (allowlisted actions only, `if` supported except on checkout). Default
shell is `pwsh` on Windows images, `bash` elsewhere; explicit `bash` is
non-Windows only. **Scripts are not executed or semantically validated — only
convert workflows you have reviewed**, and test the generated pipelines before
relying on them.

Step `if` supports only `always()` and
`github.event.inputs.NAME ==|!= 'true'` on a declared **boolean** dispatch
input (optional `${{ … }}` wrapper). The latter emits
`and(succeeded(), eq/ne('${{ parameters.NAME }}', 'true'))`; with automatic
triggers the input default must be `false` because `github.event.inputs` is
absent on non-manual runs. Everything else blocks.

`working-directory` must be a safe relative path (no leading `/`, no `\`, no
drive letter, no `..` components).

`${{ … }}`, `$[ … ]`, and Azure `$(…)` macro syntax are rejected in emitted
strings; `GITHUB_*`/`RUNNER_*` identifiers are rejected in scripts and step
env — **except** the bounded rewrites below. The only supported expression is
a whole-value `${{ secrets.NAME }}` in step `env`, which becomes the Azure
variable `$(GH_SECRET_NAME)` (uppercased) — configure it as a secret variable
in Azure DevOps. User env keys may not use the reserved `GHA_MIGRATION_`
prefix.

### Bounded Bash rewrites (Linux/bash steps only)

These exact patterns are rewritten; anything else still blocks:

- **Dispatch inputs in `run`:** `"${{ github.event.inputs.NAME }}"` as a whole
  double-quoted string (string inputs; empty default required on automatic
  triggers) → `"${GHA_MIGRATION_ARG_n}"` with env
  `${{ parameters.NAME }}` — user input is never interpolated into the script.
  Flag form `${{ github.event.inputs.NAME == 'true' && '--flag' || '' }}`
  as a standalone
  token → `${VAR:+"${VAR}"}` with env
  `${{ iif(eq(parameters.NAME, true), 'arm1', 'arm2') }}`. The inverted
  `&& '' || 'fallback'` form is only allowed for an input named `dry_run`
  (GitHub's `&& '' ||` picks the fallback even when true — the conversion
  keeps the intended empty-on-true behavior and warns). Fallback form
  `--opt=${{ github.event.inputs.NAME || 'lit' }}` → `--opt="${VAR}"` with env
  `${{ coalesce(parameters.NAME, 'lit') }}` (empty/literal default required on
  automatic triggers).
- **`$GITHUB_STEP_SUMMARY`:** only exact end-of-line append redirections —
  `>> $GITHUB_STEP_SUMMARY` and `>> "$GITHUB_STEP_SUMMARY"` — are rewritten
  to append to a temp file uploaded via `##vso[task.uploadsummary]` on step
  exit. Any other use (reads, mid-line redirects, trailing text) blocks.
  Steps that define their own `trap` block.
- **`${GITHUB_SHA::12}` + `$GITHUB_ENV`:** the exact pair
  `NAME="${GITHUB_SHA::12}"` followed by `echo "NAME=$NAME" >> "$GITHUB_ENV"`
  becomes `${BUILD_SOURCEVERSION::12}` plus a
  `##vso[task.setvariable variable=NAME;isReadOnly=true]` line (uppercase
  names only, Azure-reserved prefixes rejected). Other `GITHUB_*` uses block.
- **`git push` / `git pull --rebase origin <branch>`:** only with
  `permissions.contents: write`, a real `actions/checkout` in the same job,
  and no `pull_request` trigger. Rewritten lines call a generated
  `gha_migration_git()` subshell helper that reads the dedicated Azure
  secret `GH_SECRET_GIT_WRITE_TOKEN` (configure a **repo-scoped** GitHub
  token), validates `origin` is a credential-free
  `https://github.com/OWNER/REPO` URL, then runs `git push|pull` against
  that **explicit URL** (so a distinct `remote.pushurl` cannot reroute the
  credentialed request). The helper disables tracing (`set +x`, unsets
  `GIT_TRACE*`) so the encoded credential cannot leak through xtrace, and
  passes the token only via `GIT_CONFIG_*` environment — never in process
  arguments or logs. It requires a branch run (`$BUILD_SOURCEBRANCH`),
  disables terminal prompts and redirect following, and pushes
  `HEAD:$BUILD_SOURCEBRANCH` — never force. Checkout `fetchDepth` is raised
  to `0` automatically; `persistCredentials` stays `false`. Other
  `git push`/`git pull` forms, heredocs, `set -x` / `set -o xtrace` and
  `GIT_TRACE*` env on those steps all block. Write permission without a
  recognized git step warns that it is not transferred.

### Actions (major-version tags only)

- `actions/checkout@v4|v5|v6|v7` — first step only, once per job; supports
  `fetch-depth` (non-negative int, default 1; `0` also sets `fetchTags`)
  and `persist-credentials` (must be `false` if present; omitted → warning
  because GitHub defaults to `true`). Emits `checkout: self, fetchDepth,
  fetchTags, clean: true, persistCredentials: false`. Jobs without checkout
  get `checkout: none` inserted first to suppress Azure's implicit clone.
- `actions/setup-node@v4|v5|v6|v7` — inputs `node-version` (string or int;
  major-only versions get `.x` appended) → `UseNode@1`; `cache: npm` is
  accepted with a warning that the cache is intentionally not transferred.
- `actions/setup-python@v5|v6|v7` — input `python-version` →
  `UsePythonVersion@0`. Quote decimal versions (`'3.12'`); floats are
  rejected so precision is never lost.
- `actions/upload-artifact@v4|v5|v6|v7` — `with` keys `name`
  (`[A-Za-z0-9][A-Za-z0-9_.-]*`, unique across the whole pipeline),
  `path` (multiline literal repo-relative files; every `/`-separated
  component must match `[A-Za-z0-9_-][A-Za-z0-9_.-]*` — no globs,
  macros, `!`/`#` directives, hidden/`..`/empty components, backslashes or
  absolute paths) and `retention-days` (int 1–90, handled by Azure
  project retention). Emits a `CopyFiles@2` stage step into
  `$(Agent.TempDirectory)/gha_migration_artifact_<n>` followed by
  `PublishPipelineArtifact@1`; both inherit the source step's `if`
  condition. Azure publish **fails** when no files match (GitHub only
  warns) — verify paths exist. Other `with` keys block.

Composite actions, caching beyond `cache: npm`, and every other action or
`with` input still block.

## Reviewing the output in Azure DevOps

1. Run `gh-to-azdo dry-run`/`audit` and read every warning — they mark manual
   follow-ups (secret variables, credential persistence, tool versions,
   PR-trigger semantics).
2. Lint/validate the YAML (any YAML parser; Azure DevOps also validates on
   pipeline creation). The offline commands (`audit`, `dry-run`, `migrate`,
   `batch`) never talk to Azure — `az pipelines create`/`update` or the web
   UI import is a manual step. (`transfer` is the exception: it is the only
   command that talks to Azure, and only after you confirm.)
3. There is no pipeline "forecast" or live migration in this MVP: conversion
   is static YAML rewriting only.

## Licensing & sales model (Pro batch)

`batch` and multi-workflow `transfer` require a signed offline license.
Licenses are annual per-team entitlements; they are sold outside this tool via
manual invoice / payment link — **no payment processing is integrated and
this software never charges anyone**. Suggested price: **$199/team/year**
(illustrative only; nothing is activated by this repo).

License files are JSON envelopes `{payload, signature}`: an Ed25519 signature
over the canonical payload `{version: 1, customer, expires: YYYY-MM-DD,
features: ["batch"]}`. Verification is offline and fail-closed — an expired,
tampered, or unsigned license blocks `batch`.

### Vendor provisioning (before building the distributed wheel)

Keep the private key **outside this repository** (it is git-ignored as
`vendor-private.key`, but do not rely on that alone). The **public** key may be
distributed; the **private** key must never leave the vendor.

1. Generate a keypair from a private path outside the repo — stdout carries
   only the raw public key (standard base64), which you may share:

   ```bash
   python tools/issue_license.py keygen --private-key ~/secrets/vendor-private.key
   # stdout: <standard-base64 raw Ed25519 public key>
   ```

   The private key file is created exclusively (no overwrite) with mode `0600`
   on POSIX; on Windows the mode is not enforced — protect it with NTFS ACLs.

2. Bake the public key into the build by replacing **only**
   `PRO_PUBLIC_KEY_B64` in `src/github_to_azdo/licensing.py`, then build the
   wheel:

   ```bash
   python -m pip install build
   python -m build
   ```

   Do not commit real keys to any public repo.

3. Issue a license per sale:

   ```bash
   python tools/issue_license.py issue \
     --private-key ~/secrets/vendor-private.key \
     --customer "Acme Corp" \
     --expires 2027-01-31 \
     --output acme-license.json
   ```

   Deliver `acme-license.json` to the customer manually (email/portal); they
   run `gh-to-azdo batch ... --license acme-license.json`.

### Honest limitations of offline licensing

- Anyone with the source can patch out the check or recompile — offline
  signatures deter casual sharing, they cannot prevent determined bypass.
- There is **no revocation**: a leaked license works until `expires`.
- This is a licensing *policy* mechanism, not a legal license — no software
  license terms are supplied here; add your own EULA if you sell this.
- A managed/licensing-service version (online activation, revocation, seat
  management) is a possible future product, deliberately out of scope.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m github_to_azdo dry-run examples/ci.yml
```

`tools/issue_license.py` is vendor-side only and is not part of the installed
package.
