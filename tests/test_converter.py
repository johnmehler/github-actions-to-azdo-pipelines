"""Tests for github_to_azdo.converter."""

from __future__ import annotations

import unittest

import yaml

from github_to_azdo.converter import Issue, convert, render


def errors(conversion) -> list[Issue]:
    return [i for i in conversion.issues if i.severity == "error"]


def warnings(conversion) -> list[Issue]:
    return [i for i in conversion.issues if i.severity == "warning"]


def find_job(pipeline, name: str) -> dict:
    return next(job for job in pipeline["jobs"] if job["job"] == name)


GOLDEN = """\
on:
  push:
    branches:
      - main
      - release
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2
          persist-credentials: false
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - name: Test
        run: npm test
  win:
    needs: build
    runs-on: windows-2022
    steps:
      - run: Write-Output hi
        shell: pwsh
      - run: echo done
"""

EXPECTED = {
    "trigger": {"branches": {"include": ["refs/heads/main", "refs/heads/release"]}},
    "pr": "none",
    "jobs": [
        {
            "job": "build",
            "pool": {"vmImage": "ubuntu-latest"},
            "dependsOn": [],
            "steps": [
                {
                    "checkout": "self",
                    "fetchDepth": 2,
                    "fetchTags": False,
                    "clean": True,
                    "persistCredentials": False,
                },
                {"task": "UseNode@1", "inputs": {"version": "20.x"}},
                {"bash": "set -e\nnpm test", "displayName": "Test"},
            ],
        },
        {
            "job": "win",
            "pool": {"vmImage": "windows-2022"},
            "dependsOn": ["build"],
            "steps": [
                {"checkout": "none"},
                {"pwsh": "Write-Output hi"},
                {"pwsh": "echo done"},
            ],
        },
    ],
}


class TestGoldenConversion(unittest.TestCase):
    def test_exact_supported_shape(self):
        conversion = convert(GOLDEN)
        self.assertEqual(errors(conversion), [])
        self.assertIsNotNone(conversion.pipeline)
        self.assertEqual(conversion.pipeline, EXPECTED)
        # Only expected warning: setup-node version resolution.
        self.assertEqual(len(warnings(conversion)), 1)
        self.assertIn("Node.js", warnings(conversion)[0].message)

    def test_render_roundtrip(self):
        conversion = convert(GOLDEN)
        text = render(conversion.pipeline)
        self.assertIsInstance(text, str)
        loaded = yaml.safe_load(text)
        self.assertEqual(loaded, conversion.pipeline)
        # Azure schema: 'jobs' is a list; each job and step leads with its
        # discriminator key.
        self.assertIsInstance(loaded["jobs"], list)
        for job in loaded["jobs"]:
            self.assertEqual(next(iter(job)), "job")
        build = find_job(loaded, "build")
        self.assertEqual(
            [next(iter(step)) for step in build["steps"]],
            ["checkout", "task", "bash"],
        )
        self.assertIn("trigger:", text)
        self.assertIn("jobs:", text)

    def test_unquoted_on_is_a_key(self):
        # Regression: YAML 1.1 would parse `on` as boolean True.
        conversion = convert("on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")
        self.assertEqual(errors(conversion), [])
        self.assertEqual(conversion.pipeline["trigger"], {"branches": {"include": ["refs/heads/*", "refs/tags/*"]}})
        self.assertEqual(conversion.pipeline["pr"], "none")

    def test_yaml_1_2_booleans(self):
        # yes/no/on/off stay strings; true/false are booleans.
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          A: yes\n          B: 'off'\n          C: true\n"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][1]
        self.assertEqual(step["env"], {"A": "yes", "B": "off", "C": "true"})


class TestTriggers(unittest.TestCase):
    def _conv(self, on_yaml: str) -> object:
        return convert(f"on:\n{on_yaml}jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")

    def test_pull_request_warnings_and_pr_trigger(self):
        conversion = self._conv("  pull_request:\n")
        self.assertEqual(errors(conversion), [])
        self.assertEqual(
            conversion.pipeline["pr"],
            {"autoCancel": False, "branches": {"include": ["*"]}},
        )
        self.assertEqual(conversion.pipeline["trigger"], "none")
        self.assertGreaterEqual(len(warnings(conversion)), 2)

    def test_pull_request_branches(self):
        conversion = self._conv("  pull_request:\n    branches: [main, dev]\n")
        self.assertEqual(errors(conversion), [])
        self.assertEqual(
            conversion.pipeline["pr"],
            {"autoCancel": False, "branches": {"include": ["main", "dev"]}},
        )

    def test_event_list_form(self):
        conversion = convert(
            "on: [push, workflow_dispatch]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertEqual(conversion.pipeline["trigger"]["branches"]["include"], ["refs/heads/*", "refs/tags/*"])
        self.assertTrue(any("workflow_dispatch" in w.message for w in warnings(conversion)))

    def test_duplicate_event_blocked(self):
        conversion = self._conv("  push:\n  push:\n")
        # Duplicate keys are rejected at YAML load time.
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_unsupported_event_blocked(self):
        for event in ("schedule", "workflow_call", "pull_request_target", "release"):
            with self.subTest(event=event):
                conversion = self._conv(f"  {event}:\n")
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(any(event in e.message for e in errors(conversion)))

    def test_push_paths_option_blocked(self):
        conversion = self._conv("  push:\n    paths: ['src/**']\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("paths" in e.message for e in errors(conversion)))

    def test_push_tags_option_blocked(self):
        conversion = self._conv("  push:\n    tags: ['v*']\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_glob_branch_blocked(self):
        conversion = self._conv("  push:\n    branches: ['release/*']\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_refs_branch_blocked(self):
        conversion = self._conv("  push:\n    branches: ['refs/heads/main']\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("refs/" in e.message for e in errors(conversion)))

    def test_empty_branches_blocked(self):
        conversion = self._conv("  push:\n    branches: []\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_workflow_dispatch_inputs_unsupported_type_blocked(self):
        conversion = self._conv("  workflow_dispatch:\n    inputs:\n      env:\n        type: choice\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("type" in e.message for e in errors(conversion)))

    def test_workflow_dispatch_only_no_ci_triggers(self):
        conversion = convert(
            "on: workflow_dispatch\n"
            "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertIsNotNone(conversion.pipeline)
        self.assertEqual(conversion.pipeline["trigger"], "none")
        self.assertEqual(conversion.pipeline["pr"], "none")
        self.assertTrue(any("workflow_dispatch" in w.message for w in warnings(conversion)))

    def test_missing_or_empty_on_is_blocked(self):
        jobs = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        for prefix in ("", "on:\n", "on: []\n", "on: {}\n", "on: ~\n"):
            with self.subTest(prefix=repr(prefix)):
                conversion = convert(prefix + jobs)
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(
                    any("'on'" in e.message for e in errors(conversion)),
                    f"expected 'on' error for {prefix!r}: {conversion.issues}",
                )


class TestFailClosed(unittest.TestCase):
    BASE_JOBS = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"

    def test_unsupported_root_key(self):
        conversion = convert(f"on: push\npermissions: write-all\n{self.BASE_JOBS}")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("permissions" in e.message for e in errors(conversion)))

    def test_defaults_concurrency_blocked(self):
        for key in ("defaults", "concurrency", "env"):
            with self.subTest(key=key):
                conversion = convert(f"on: push\n{key}: {{}}\n{self.BASE_JOBS}")
                self.assertIsNone(conversion.pipeline)

    def test_unsupported_uses_blocked(self):
        for uses in (
            "actions/cache@v4",
            "actions/checkout@v3",
            "actions/checkout@main",
            "actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8",
            "docker://alpine:3.19",
            "./local/action",
            "third-party/action@v1",
        ):
            with self.subTest(uses=uses):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                    f"    steps:\n      - uses: {uses}\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(any("unsupported action" in e.message for e in errors(conversion)))

    def test_step_if_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - if: github.ref == 'refs/heads/main'\n        run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("'if'" in e.message for e in errors(conversion)))

    def test_job_if_matrix_outputs_blocked(self):
        for key in ("if", "strategy", "outputs", "env", "container"):
            with self.subTest(key=key):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    {key}: {{}}\n"
                    "    steps:\n      - run: echo hi\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_duplicate_yaml_key_blocked(self):
        conversion = convert("on: push\non: pull_request\n" + self.BASE_JOBS)
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("duplicate" in e.message for e in errors(conversion)))

    def test_anchor_alias_blocked(self):
        conversion = convert(
            "on: push\nx: &anchor 1\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo *anchor\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("anchor" in e.message.lower() or "alias" in e.message.lower() for e in errors(conversion)))

    def test_merge_key_blocked(self):
        # With anchor/alias (rejected at token scan) ...
        conversion = convert(
            "on: push\nbase: &b {runs-on: ubuntu-latest}\n"
            "jobs:\n  a:\n    <<: *b\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))
        # ... and without any anchor (rejected by the mapping constructor).
        conversion = convert(
            "on: push\njobs:\n  a:\n    <<: {runs-on: ubuntu-latest}\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("merge" in e.message for e in errors(conversion)))

    def test_dependency_cycle_blocked(self):
        conversion = convert(
            "on: push\njobs:\n"
            "  a:\n    needs: c\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n"
            "  b:\n    needs: a\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo b\n"
            "  c:\n    needs: b\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo c\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("circular" in e.message for e in errors(conversion)))

    def test_self_cycle_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    needs: a\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("circular" in e.message for e in errors(conversion)))

    def test_needs_unknown_job_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    needs: ghost\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("ghost" in e.message for e in errors(conversion)))

    def test_normalization_collision_blocked(self):
        conversion = convert(
            "on: push\njobs:\n"
            "  build-app:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n"
            "  build_app:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo b\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("collides" in e.message or "colliding" in e.message for e in errors(conversion)))

    def test_case_insensitive_collision_blocked(self):
        conversion = convert(
            "on: push\njobs:\n"
            "  Build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n"
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo b\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_deploy_name_renamed(self):
        conversion = convert(
            "on: push\njobs:\n  deploy-prod:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertIsNotNone(conversion.pipeline)
        find_job(conversion.pipeline, "release_prod")
        self.assertTrue(any("deploy" in w.message and "renamed" in w.message for w in warnings(conversion)))

    def test_bad_job_id_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  '123 bad':\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_missing_runs_on_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("runs-on" in e.message for e in errors(conversion)))

    def test_unsupported_image_blocked(self):
        for image in ("ubuntu-latest-8-cores", "self-hosted", "macos-13", "ubuntu-20.04"):
            with self.subTest(image=image):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: {image}\n    steps:\n      - run: echo hi\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_github_expression_in_run_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo ${{ github.sha }}\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("expression" in e.message for e in errors(conversion)))

    def test_dollar_bracket_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo $[ github.sha ]\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_azure_macro_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo $(Build.BuildNumber)\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("macro" in e.message.lower() for e in errors(conversion)))

    def test_github_runner_vars_blocked(self):
        for snippet in ("echo $GITHUB_SHA", "echo $github_token", "RUNNER_OS=x"):
            with self.subTest(snippet=snippet):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                    f"    steps:\n      - run: {snippet}\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(any("GITHUB_" in e.message or "RUNNER_" in e.message for e in errors(conversion)))

    def test_bash_on_windows_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: windows-latest\n"
            "    steps:\n      - run: echo hi\n        shell: bash\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("bash" in e.message for e in errors(conversion)))

    def test_pwsh_everywhere_ok(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: Write-Output hi\n        shell: pwsh\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertEqual(find_job(conversion.pipeline, "a")["steps"][1]["pwsh"], "Write-Output hi")

    def test_explicit_bash_pipefail(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: make test\n        shell: bash\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertEqual(find_job(conversion.pipeline, "a")["steps"][1]["bash"], "set -eo pipefail\nmake test")

    def test_unsupported_shell_blocked(self):
        for shell in ("python", "sh", "cmd", "powershell", "bash -e {0}"):
            with self.subTest(shell=shell):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                    f"    steps:\n      - run: echo hi\n        shell: {shell}\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_empty_run_blocked(self):
        for run_step in ("- run: ''", "- run: '   '", "- run:"):
            with self.subTest(run_step=run_step):
                conversion = convert(
                    "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                    f"    steps:\n      {run_step}\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(
                    any("non-empty" in e.message for e in errors(conversion)),
                    f"expected non-empty error: {conversion.issues}",
                )

    def test_working_directory_validation(self):
        bad = ("/abs/path", "C:\\temp", "C:/temp", "a/../b", "..", "a\\b")
        for wd in bad:
            with self.subTest(wd=wd):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                    f"    steps:\n      - run: echo hi\n        working-directory: '{wd}'\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))
        good = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        working-directory: 'sub/dir'\n"
        )
        self.assertEqual(errors(good), [])
        self.assertEqual(find_job(good.pipeline, "a")["steps"][1]["workingDirectory"], "sub/dir")

    def test_checkout_position_and_repeat_blocked(self):
        # checkout not first
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n      - uses: actions/checkout@v4\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("first step" in e.message for e in errors(conversion)))
        # checkout twice
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n      - run: echo hi\n"
            "      - uses: actions/checkout@v4\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("first step" in e.message for e in errors(conversion)))

    def test_checkout_inputs_validation(self):
        # persist-credentials true blocked
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n        with:\n          persist-credentials: true\n"
            "      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("persist-credentials" in e.message for e in errors(conversion)))
        # fetch-depth non-int blocked
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: '2'\n"
            "      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("fetch-depth" in e.message for e in errors(conversion)))
        # unknown with key blocked
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n        with:\n          submodules: true\n"
            "      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))
        # omitted persist-credentials warns but converts
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertTrue(any("persist-credentials" in w.message for w in warnings(conversion)))
        step = find_job(conversion.pipeline, "a")["steps"][0]
        self.assertEqual(step["fetchDepth"], 1)
        self.assertIs(step["fetchTags"], False)
        self.assertFalse(step["persistCredentials"])

    def test_checkout_fetch_depth_zero_fetches_tags(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n        with:\n"
            "          fetch-depth: 0\n          persist-credentials: false\n"
            "      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][0]
        self.assertEqual(step["fetchDepth"], 0)
        self.assertIs(step["fetchTags"], True)

    def test_setup_task_inputs(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/setup-python@v6\n        with:\n          python-version: '3.12.1'\n"
            "      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        # steps[0] is the implicit checkout:none; the setup task follows.
        step = find_job(conversion.pipeline, "a")["steps"][1]
        self.assertEqual(step, {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12.1"}})
        # float versions must be quoted
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/setup-python@v5\n        with:\n          python-version: 3.12\n"
            "      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("python-version" in e.message for e in errors(conversion)))
        # cache: npm is accepted with a warning
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/setup-node@v4\n        with:\n          node-version: '20'\n          cache: npm\n"
            "      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertTrue(any("cache" in w.message for w in warnings(conversion)))
        # other cache values still block
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/setup-node@v4\n        with:\n          node-version: '20'\n          cache: yarn\n"
            "      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("cache" in e.message for e in errors(conversion)))

    def test_secret_mapping(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n"
            "          TOKEN: ${{ secrets.my_token }}\n"
            "          ALIAS: ${{ secrets.my_token }}\n"
            "          API_KEY: ${{ secrets.api_key }}\n"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][1]
        self.assertEqual(step["env"]["TOKEN"], "$(GH_SECRET_MY_TOKEN)")
        self.assertEqual(step["env"]["ALIAS"], "$(GH_SECRET_MY_TOKEN)")
        self.assertEqual(step["env"]["API_KEY"], "$(GH_SECRET_API_KEY)")
        secret_warnings = [w for w in warnings(conversion) if w.location == "secrets"]
        self.assertEqual(len(secret_warnings), 1)
        self.assertIn(
            "GH_SECRET_API_KEY, GH_SECRET_MY_TOKEN", secret_warnings[0].message
        )
        self.assertIn("not transferred", secret_warnings[0].message)

    def test_secret_with_pull_request_blocked(self):
        conversion = convert(
            "on:\n  push:\n  pull_request:\n"
            "jobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          TOKEN: ${{ secrets.my_token }}\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("pull_request" in e.message and "secret" in e.message.lower() for e in errors(conversion)))

    def test_non_secret_expression_env_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          X: ${{ github.sha }}\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_env_scalar_types(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          S: str\n          B: true\n"
            "          I: 42\n          F: 1.5\n"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][1]
        self.assertEqual(step["env"], {"S": "str", "B": "true", "I": "42", "F": "1.5"})

    def test_env_bad_types_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          L: [a, b]\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          N: ~\n"
        )
        self.assertIsNone(conversion.pipeline)

    def test_env_key_validation(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo hi\n        env:\n          '1BAD': x\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_multiple_errors_reported(self):
        conversion = convert(
            "on: push\nunknown: 1\njobs:\n"
            "  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ${{ x }}\n"
            "  b:\n    runs-on: ubuntu-latest\n    bad-key: 1\n    steps:\n      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        messages = " ".join(e.message for e in errors(conversion))
        self.assertIn("unknown", messages)
        self.assertIn("expression", messages)
        self.assertIn("bad-key", messages)

    def test_yaml_syntax_error(self):
        conversion = convert("on: push\njobs: [unclosed\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))
        # Location should mention a line, not file contents.
        self.assertTrue(any("line" in e.location for e in errors(conversion)))

    def test_multiple_documents_blocked(self):
        conversion = convert("on: push\n---\non: pull_request\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_non_mapping_root_blocked(self):
        for doc in ("- a\n- b\n", "just a string\n", "42\n"):
            with self.subTest(doc=doc):
                conversion = convert(doc)
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_empty_document_blocked(self):
        conversion = convert("")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))


JOBS = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"


class TestSchedule(unittest.TestCase):
    def _conv(self, on_yaml: str, branch: str | None = "main"):
        return convert(f"on:\n{on_yaml}{JOBS}", default_branch=branch)

    def test_schedule_cron_emits_azure_schedule(self):
        conversion = self._conv(
            "  schedule:\n    - cron: '0 7 * * *'\n    - cron: '30 7 * * 1'\n"
        )
        self.assertEqual(errors(conversion), [])
        schedules = conversion.pipeline["schedules"]
        self.assertEqual(len(schedules), 2)
        self.assertEqual(schedules[0]["cron"], "0 7 * * *")
        self.assertEqual(schedules[0]["displayName"], "GitHub schedule 1")
        self.assertEqual(schedules[0]["branches"], {"include": ["main"]})
        self.assertIs(schedules[0]["always"], True)
        self.assertEqual(schedules[1]["cron"], "30 7 * * 1")
        self.assertTrue(any("retire" in w.message for w in warnings(conversion)))

    def test_schedule_needs_default_branch(self):
        conversion = self._conv("  schedule:\n    - cron: '0 7 * * *'\n", branch=None)
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("default branch" in e.message or "default_branch" in e.message or "--default-branch" in e.message for e in errors(conversion)))

    def test_schedule_bad_cron_blocked(self):
        for cron in ("0 7 * *", "61 7 * * *", "0 7 * * 8", "0 7 * * */0", "0 7 * * 1-3/0", "0 7 * * * *"):
            with self.subTest(cron=cron):
                conversion = self._conv(f"  schedule:\n    - cron: '{cron}'\n")
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_schedule_scalar_blocked(self):
        conversion = self._conv("  schedule:\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("schedule" in e.message for e in errors(conversion)))

    def test_schedule_extra_option_blocked(self):
        conversion = self._conv("  schedule:\n    - cron: '0 7 * * *'\n      timezone: UTC\n")
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(errors(conversion))

    def test_cron_lists_ranges_steps_ok(self):
        conversion = self._conv("  schedule:\n    - cron: '*/15 9-17 1,15 * 1-5'\n")
        self.assertEqual(errors(conversion), [])
        self.assertEqual(conversion.pipeline["schedules"][0]["cron"], "*/15 9-17 1,15 * 1-5")


class TestJobKeys(unittest.TestCase):
    def test_timeout_minutes(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: 120\n"
            "    steps:\n      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        self.assertEqual(find_job(conversion.pipeline, "a")["timeoutInMinutes"], 120)
        self.assertTrue(any("timeout" in w.message.lower() for w in warnings(conversion)))
        for bad in (0, 361, "'60'", 1.5, True):
            with self.subTest(bad=bad):
                conversion = convert(
                    f"on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    timeout-minutes: {bad}\n"
                    "    steps:\n      - run: echo hi\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_deploy_rename_with_needs(self):
        conversion = convert(
            "on: push\njobs:\n"
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo b\n"
            "  deploy:\n    needs: build\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo d\n"
        )
        self.assertEqual(errors(conversion), [])
        pipeline = conversion.pipeline
        self.assertEqual(find_job(pipeline, "release")["dependsOn"], ["build"])
        self.assertTrue(any("deploy" in w.message for w in warnings(conversion)))


class TestPermissionsConcurrency(unittest.TestCase):
    def test_permissions_contents_write(self):
        conversion = convert(f"on: push\npermissions:\n  contents: write\n  actions: read\n{JOBS}")
        self.assertEqual(errors(conversion), [])
        self.assertTrue(any("not transferred" in w.message for w in warnings(conversion)))
        # write declared but no git step -> warn not transferred
        self.assertTrue(any("not transferred" in w.message and "git" in w.message.lower() for w in warnings(conversion)))

    def test_permissions_write_with_pull_request_blocked(self):
        conversion = convert(
            "on:\n  push:\n  pull_request:\npermissions:\n  contents: write\n" + JOBS
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("pull_request" in e.message and "contents" in e.message for e in errors(conversion)))

    def test_permissions_invalid(self):
        for block in ("permissions: write-all\n", "permissions:\n  contents: admin\n", "permissions:\n  issues: write\n"):
            with self.subTest(block=block):
                conversion = convert(f"on: push\n{block}{JOBS}")
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_concurrency_stage_lock(self):
        conversion = convert(
            "on: push\nconcurrency:\n  group: deploy\n  cancel-in-progress: false\n" + JOBS
        )
        self.assertEqual(errors(conversion), [])
        stages = conversion.pipeline["stages"]
        self.assertEqual(stages[0]["stage"], "Workflow")
        self.assertEqual(stages[0]["lockBehavior"], "sequential")
        self.assertNotIn("jobs", conversion.pipeline)
        self.assertTrue(any("sequential" in w.message for w in warnings(conversion)))

    def test_concurrency_invalid(self):
        for block in (
            "concurrency:\n  group: deploy\n  cancel-in-progress: true\n",
            "concurrency:\n  group: deploy\n",
            "concurrency:\n  group: 'a b'\n  cancel-in-progress: false\n",
            "concurrency:\n  group: deploy\n  cancel-in-progress: false\n  extra: 1\n",
        ):
            with self.subTest(block=block):
                conversion = convert(f"on: push\n{block}{JOBS}")
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))


DISPATCH = """on:
  workflow_dispatch:
    inputs:
      dry_run:
        description: Dry run
        type: boolean
        default: false
      companies:
        type: string
        required: true
      days:
        type: string
        default: '8'
"""


class TestDispatchInputs(unittest.TestCase):
    def test_parameters_and_required_guard(self):
        conversion = convert(DISPATCH + JOBS)
        self.assertEqual(errors(conversion), [])
        params = conversion.pipeline["parameters"]
        self.assertEqual(
            [p["name"] for p in params], ["dry_run", "companies", "days"]
        )
        self.assertEqual(params[0]["type"], "boolean")
        self.assertEqual(params[1]["default"], "")
        self.assertEqual(params[2]["default"], "8")
        job = find_job(conversion.pipeline, "a")
        guard = job["steps"][1]
        self.assertEqual(guard["displayName"], "Validate required workflow inputs")
        self.assertIn("GHA_MIGRATION_REQUIRED_0", guard["bash"])
        self.assertIn("companies", guard["bash"])
        self.assertEqual(guard["env"]["GHA_MIGRATION_REQUIRED_0"], "${{ parameters.companies }}")

    def test_implicit_string_and_bool_defaults(self):
        conversion = convert(
            "on:\n  workflow_dispatch:\n    inputs:\n      flag:\n        type: boolean\n"
            "      text:\n        description: stuff\n" + JOBS
        )
        self.assertEqual(errors(conversion), [])
        params = {p["name"]: p for p in conversion.pipeline["parameters"]}
        self.assertEqual(params["flag"]["default"], False)
        self.assertEqual(params["text"]["default"], "")
        self.assertEqual(params["text"]["displayName"], "stuff")

    def test_required_with_automatic_events_blocked(self):
        conversion = convert(
            "on:\n  push:\n  workflow_dispatch:\n    inputs:\n      companies:\n"
            "        type: string\n        required: true\n" + JOBS
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("required" in e.message for e in errors(conversion)))

    def test_required_on_windows_blocked(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: windows-latest\n    steps:\n      - run: echo hi\n        shell: pwsh\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("Windows" in e.message for e in errors(conversion)))

    def test_required_boolean_rules(self):
        # required boolean with explicit false default: valid, no guard step
        conversion = convert(
            "on:\n  workflow_dispatch:\n    inputs:\n      flag:\n"
            "        type: boolean\n        required: true\n        default: false\n"
            + JOBS
        )
        self.assertEqual(errors(conversion), [])
        job = find_job(conversion.pipeline, "a")
        self.assertFalse(
            any(s.get("displayName") == "Validate required workflow inputs"
                for s in job["steps"])
        )
        # required boolean without any default: blocked
        conversion = convert(
            "on:\n  workflow_dispatch:\n    inputs:\n      flag:\n"
            "        type: boolean\n        required: true\n" + JOBS
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(
            any("required boolean" in e.message for e in errors(conversion))
        )

    def test_input_validation(self):
        for inputs_yaml, needle in (
            ("      bad-id:\n        type: string\n", "input id"),
            ("      x:\n        type: number\n", "type"),
            ("      x:\n        required: 'yes'\n", "required"),
            ("      x:\n        type: boolean\n        default: 'true'\n", "default"),
            ("      x:\n        type: string\n        default: 5\n", "default"),
            ("      x:\n        type: string\n        bogus: 1\n", "bogus"),
        ):
            with self.subTest(inputs_yaml=inputs_yaml):
                conversion = convert(
                    "on:\n  workflow_dispatch:\n    inputs:\n" + inputs_yaml + JOBS
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(any(needle in e.message for e in errors(conversion)))


class TestStepIf(unittest.TestCase):
    def test_bool_input_condition(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - if: ${{ github.event.inputs.dry_run == 'true' }}\n        run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][2]
        self.assertEqual(
            step["condition"],
            "and(succeeded(), eq('${{ parameters.dry_run }}', 'true'))",
        )

    def test_ne_condition_and_always(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - if: github.event.inputs.dry_run != 'true'\n        run: echo a\n"
            "      - if: always()\n        run: echo b\n"
        )
        self.assertEqual(errors(conversion), [])
        steps = find_job(conversion.pipeline, "a")["steps"]
        self.assertEqual(
            steps[2]["condition"],
            "and(succeeded(), ne('${{ parameters.dry_run }}', 'true'))",
        )
        self.assertEqual(steps[3]["condition"], "always()")

    def test_condition_on_uses_step(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/setup-node@v4\n        if: github.event.inputs.dry_run == 'true'\n"
            "        with:\n          node-version: '20'\n"
            "      - run: echo hi\n"
        )
        self.assertEqual(errors(conversion), [])
        step = next(
            s for s in find_job(conversion.pipeline, "a")["steps"]
            if s.get("task") == "UseNode@1"
        )
        self.assertIn("condition", step)

    def test_unknown_condition_blocked(self):
        for cond in (
            "github.ref == 'refs/heads/main'",
            "github.event.inputs.dry_run == 'false'",
            "success()",
            "github.event.inputs.undeclared == 'true'",
        ):
            with self.subTest(cond=cond):
                conversion = convert(
                    DISPATCH
                    + f"jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                    f"      - if: {cond}\n        run: echo hi\n"
                )
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(any("'if'" in e.message for e in errors(conversion)))

    def test_condition_on_checkout_blocked(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v4\n        if: always()\n"
            "      - run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("checkout" in e.message for e in errors(conversion)))

    def test_bool_condition_with_automatic_events_needs_false_default(self):
        conversion = convert(
            "on:\n  push:\n  workflow_dispatch:\n    inputs:\n      dry_run:\n"
            "        type: boolean\n        default: true\n"
            "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - if: github.event.inputs.dry_run == 'true'\n        run: echo hi\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("default" in e.message for e in errors(conversion)))


class TestBashInputExpressions(unittest.TestCase):
    BASE = DISPATCH + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"

    def _run_step(self, script: str):
        return convert(self.BASE + f"      - run: |\n" + "".join(
            f"          {line}\n" for line in script.splitlines()
        ))

    def test_direct_string_in_env_not_script(self):
        conversion = self._run_step(
            'printf \'%s\\n\' "${{ github.event.inputs.companies }}" > /tmp/list.txt'
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][2]
        self.assertIn('"${GHA_MIGRATION_ARG_0}"', step["bash"])
        self.assertNotIn("companies", step["bash"].split("env")[0] if "env" in step else step["bash"])
        self.assertEqual(step["env"]["GHA_MIGRATION_ARG_0"], "${{ parameters.companies }}")

    def test_direct_requires_quotes_and_string_type(self):
        # unquoted -> blocked
        conversion = self._run_step("echo ${{ github.event.inputs.companies }}")
        self.assertIsNone(conversion.pipeline)
        # bool input -> blocked
        conversion = self._run_step('echo "${{ github.event.inputs.dry_run }}"')
        self.assertIsNone(conversion.pipeline)
        # undeclared -> blocked
        conversion = self._run_step('echo "${{ github.event.inputs.nope }}"')
        self.assertIsNone(conversion.pipeline)

    def test_flag_expressions(self):
        conversion = self._run_step(
            "npm run x ${{ github.event.inputs.dry_run == 'true' && '--dry-run' || '' }} "
            "${{ github.event.inputs.dry_run == 'true' && '' || '--apply' }}"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][2]
        self.assertIn('${GHA_MIGRATION_ARG_0:+"${GHA_MIGRATION_ARG_0}"}', step["bash"])
        self.assertIn('${GHA_MIGRATION_ARG_1:+"${GHA_MIGRATION_ARG_1}"}', step["bash"])
        env = step["env"]
        self.assertEqual(
            env["GHA_MIGRATION_ARG_0"],
            "${{ iif(eq(parameters.dry_run, true), '--dry-run', '') }}",
        )
        self.assertEqual(
            env["GHA_MIGRATION_ARG_1"],
            "${{ iif(eq(parameters.dry_run, true), '', '--apply') }}",
        )
        self.assertTrue(any("fallback arm" in w.message for w in warnings(conversion)))

    def test_inverted_flag_only_for_dry_run(self):
        conversion = convert(
            "on:\n  workflow_dispatch:\n    inputs:\n      other:\n        type: boolean\n"
            "        default: false\n"
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: |\n"
            "          npm x ${{ github.event.inputs.other == 'true' && '' || '--go' }}\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("dry_run" in e.message for e in errors(conversion)))

    def test_fallback_expression(self):
        conversion = self._run_step(
            "npm run collect --days=${{ github.event.inputs.days || '8' }}"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][2]
        self.assertIn('--days="${GHA_MIGRATION_ARG_0}"', step["bash"])
        self.assertEqual(
            step["env"]["GHA_MIGRATION_ARG_0"],
            "${{ coalesce(parameters.days, '8') }}",
        )
        # fallback outside --opt= context -> blocked
        conversion = self._run_step(
            "echo value is ${{ github.event.inputs.days || '8' }}"
        )
        self.assertIsNone(conversion.pipeline)

    def test_unknown_expression_still_blocked(self):
        conversion = self._run_step("echo ${{ github.sha }}")
        self.assertIsNone(conversion.pipeline)
        conversion = self._run_step("echo ${{ secrets.TOKEN }}")
        self.assertIsNone(conversion.pipeline)

    def test_gha_migration_env_reserved(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: echo hi\n        env:\n          GHA_MIGRATION_ARG_0: x\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("GHA_MIGRATION_" in e.message for e in errors(conversion)))


GIT_WF = """on:
  workflow_dispatch:
permissions:
  contents: write
jobs:
  ship:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: |
          git config user.email ci@example.com
          git add -A
          git commit -m 'update [skip ci]'
          git push
      - run: git pull --rebase origin main
"""


class TestGitWrites(unittest.TestCase):
    def test_git_push_rewrite(self):
        conversion = convert(GIT_WF)
        self.assertEqual(errors(conversion), [])
        job = find_job(conversion.pipeline, "ship")
        checkout = job["steps"][0]
        self.assertEqual(checkout["persistCredentials"], False)
        self.assertEqual(checkout["fetchDepth"], 0)
        self.assertIs(checkout["fetchTags"], True)
        push_step = job["steps"][1]
        self.assertIn('gha_migration_git push "HEAD:$BUILD_SOURCEBRANCH"', push_step["bash"])
        self.assertNotIn("push origin", push_step["bash"])
        self.assertIn("GHA_MIGRATION_GIT_TOKEN", push_step["env"])
        self.assertEqual(push_step["env"]["GHA_MIGRATION_GIT_TOKEN"], "$(GH_SECRET_GIT_WRITE_TOKEN)")
        bash = push_step["bash"]
        self.assertIn("gha_migration_git() (", bash)
        self.assertIn("set +x", bash)
        self.assertIn("unset GIT_TRACE GIT_TRACE_CURL GIT_CURL_VERBOSE", bash)
        self.assertIn("git remote get-url origin", bash)
        # push/pull route through the validated URL, not the origin remote name
        self.assertIn('set -- push "$origin" "$1"', bash)
        self.assertIn('set -- pull --rebase "$origin" "$1"', bash)
        pull_step = job["steps"][2]
        self.assertIn("gha_migration_git pull main", pull_step["bash"])
        self.assertNotIn("pull --rebase origin", pull_step["bash"])
        secret_warnings = [w for w in warnings(conversion) if w.location == "secrets"]
        self.assertEqual(len(secret_warnings), 1)
        self.assertIn("GH_SECRET_GIT_WRITE_TOKEN", secret_warnings[0].message)
        self.assertIn("repo-scoped GitHub token", secret_warnings[0].message)

    def test_git_without_permission_blocked(self):
        conversion = convert(GIT_WF.replace("permissions:\n  contents: write\n", ""))
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("contents: write" in e.message or "contents" in e.message for e in errors(conversion)))

    def test_git_unsupported_form_blocked(self):
        conversion = convert(
            GIT_WF.replace("git push", "git push origin main --force")
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("unsupported git" in e.message for e in errors(conversion)))

    def test_git_without_checkout_blocked(self):
        conversion = convert(
            GIT_WF.replace("      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n", "")
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("checkout" in e.message for e in errors(conversion)))

    def test_git_xtrace_blocked(self):
        conversion = convert(
            GIT_WF.replace(
                "          git config user.email",
                "          set -x\n          git config user.email",
            )
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("xtrace" in e.message for e in errors(conversion)))


class TestSummaryAndSha(unittest.TestCase):
    def test_step_summary_redirect(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: |\n"
            "          echo '## Summary' >> $GITHUB_STEP_SUMMARY\n"
            "          cat report.md >> \"$GITHUB_STEP_SUMMARY\"\n"
        )
        self.assertEqual(errors(conversion), [])
        step = find_job(conversion.pipeline, "a")["steps"][1]
        self.assertIn('>> "$GHA_MIGRATION_SUMMARY"', step["bash"])
        self.assertIn("task.uploadsummary", step["bash"])
        self.assertIn("mktemp", step["bash"])

    def test_step_summary_unhandled_blocked(self):
        # non-append uses are no longer rewritten; _check_script must block them
        for script in (
            "cat $GITHUB_STEP_SUMMARY",
            "cat report.md >> $GITHUB_STEP_SUMMARY extra",
            "echo '$GITHUB_STEP_SUMMARY'",
        ):
            with self.subTest(script=script):
                conversion = convert(
                    "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
                    f"      - run: {script}\n"
                )
                self.assertIsNone(conversion.pipeline)
        # trailing content after the redirect target also fails closed
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: |\n"
            "          cat report.md >> $GITHUB_STEP_SUMMARY # note\n"
        )
        self.assertIsNone(conversion.pipeline)

    def test_sha_and_github_env_pair(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: |\n"
            "          RELEASE=\"${GITHUB_SHA::12}\"\n"
            "          echo \"RELEASE=$RELEASE\" >> \"$GITHUB_ENV\"\n"
            "      - run: echo \"deployed $RELEASE\"\n"
        )
        self.assertEqual(errors(conversion), [])
        first = find_job(conversion.pipeline, "a")["steps"][1]
        self.assertIn('RELEASE="${BUILD_SOURCEVERSION::12}"', first["bash"])
        self.assertIn("task.setvariable variable=RELEASE", first["bash"])
        self.assertNotIn("GITHUB_ENV", first["bash"])
        self.assertNotIn("GITHUB_SHA", first["bash"])

    def test_github_env_without_sha_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: echo \"X=$X\" >> \"$GITHUB_ENV\"\n"
        )
        self.assertIsNone(conversion.pipeline)

    def test_other_github_sha_blocked(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: echo ${GITHUB_SHA::7}\n"
        )
        self.assertIsNone(conversion.pipeline)


class TestUploadArtifact(unittest.TestCase):
    def test_artifact_pair(self):
        conversion = convert(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/upload-artifact@v4\n"
            "        with:\n"
            "          name: ats-jobs-data\n"
            "          path: |\n"
            "            ats/companies.json\n"
            "            ats/ats_jobs_data.json\n"
            "          retention-days: 5\n"
            "      - run: echo done\n"
        )
        self.assertEqual(errors(conversion), [])
        steps = find_job(conversion.pipeline, "a")["steps"]
        copy_step, publish_step = steps[1], steps[2]
        self.assertEqual(copy_step["task"], "CopyFiles@2")
        self.assertEqual(copy_step["inputs"]["SourceFolder"], "$(Build.SourcesDirectory)/ats")
        self.assertEqual(
            copy_step["inputs"]["Contents"], "companies.json\nats_jobs_data.json"
        )
        self.assertEqual(publish_step["task"], "PublishPipelineArtifact@1")
        self.assertEqual(publish_step["inputs"]["artifact"], "ats-jobs-data")
        self.assertEqual(publish_step["inputs"]["publishLocation"], "pipeline")
        self.assertTrue(any("retention" in w.message for w in warnings(conversion)))
        self.assertTrue(any("no files match" in w.message for w in warnings(conversion)))

    def test_artifact_validation(self):
        base = (
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/upload-artifact@v4\n        with:\n"
        )
        for with_yaml in (
            "          name: 'bad name'\n          path: a.txt\n",
            "          name: ok\n          path: '../secret.txt'\n",
            "          name: ok\n          path: 'a/*.txt'\n",
            "          name: ok\n          path: '.hidden.txt'\n",
            "          name: ok\n          path: 'a//b.txt'\n",
            "          name: ok\n          path: '/abs.txt'\n",
            "          name: ok\n          path: 'trail/\n",
            "          name: ok\n          path: 'a/$(whoami).txt'\n",
            "          name: ok\n          path: 'a/${{ inputs.x }}.txt'\n",
            "          name: ok\n          path: '!include.txt'\n",
            "          name: ok\n          path: '#directive.txt'\n",
            "          name: ok\n          path: 'a b.txt'\n",
            "          name: ok\n          path: a.txt\n          bogus: 1\n",
            "          name: ok\n          path: a.txt\n          retention-days: 200\n",
            "          path: a.txt\n",
        ):
            with self.subTest(with_yaml=with_yaml):
                conversion = convert(base + with_yaml)
                self.assertIsNone(conversion.pipeline)
                self.assertTrue(errors(conversion))

    def test_artifact_name_unique_across_jobs(self):
        conversion = convert(
            "on: push\njobs:\n"
            "  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/upload-artifact@v4\n        with:\n"
            "          name: same\n          path: a.txt\n"
            "  b:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/upload-artifact@v4\n        with:\n"
            "          name: SAME\n          path: b.txt\n"
        )
        self.assertIsNone(conversion.pipeline)
        self.assertTrue(any("duplicate artifact" in e.message for e in errors(conversion)))

    def test_artifact_condition_applies_to_both(self):
        conversion = convert(
            DISPATCH
            + "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/upload-artifact@v4\n"
            "        if: github.event.inputs.dry_run == 'true'\n"
            "        with:\n          name: art\n          path: a.txt\n"
        )
        self.assertEqual(errors(conversion), [])
        artifact_steps = [
            s
            for s in find_job(conversion.pipeline, "a")["steps"]
            if s.get("task") in ("CopyFiles@2", "PublishPipelineArtifact@1")
        ]
        self.assertEqual(len(artifact_steps), 2)
        for step in artifact_steps:
            self.assertIn("condition", step)


class TestCombinedWorkflow(unittest.TestCase):
    """Anonymized mix of the constructs found in real migrated workflows."""

    WF = """name: Nightly ingest
on:
  schedule:
    - cron: '0 7 * * *'
    - cron: '0 8 * * *'
    - cron: '30 7 * * 1'
  workflow_dispatch:
    inputs:
      dry_run:
        type: boolean
        default: false
      discover:
        type: boolean
        default: false
      companies:
        type: string
        required: true
      days:
        type: string
        default: '8'
permissions:
  contents: write
  actions: read
jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-node@v7
        with:
          node-version: 22
          cache: npm
      - uses: actions/setup-python@v7
        with:
          python-version: '3.12'
      - name: Collect
        if: ${{ github.event.inputs.dry_run == 'true' }}
        run: |
          printf '%s\\n' "${{ github.event.inputs.companies }}" > /tmp/onboard-companies.txt
          npm run collect --days=${{ github.event.inputs.days || '8' }} ${{ github.event.inputs.dry_run == 'true' && '--dry-run' || '' }} ${{ github.event.inputs.discover == 'true' && '--discover' || '' }}
          echo done >> $GITHUB_STEP_SUMMARY
  deploy:
    needs: ingest
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - name: Stage data
        run: |
          git config user.email ci@example.com
          git pull --rebase origin main
          git add ats/
          git commit -m 'update data [skip ci]'
          git push
"""

    def test_mixed_workflow_converts(self):
        # required inputs + schedule -> blocked by design; use push instead
        wf = self.WF.replace(
            "on:\n  schedule:\n    - cron: '0 7 * * *'\n    - cron: '0 8 * * *'\n    - cron: '30 7 * * 1'\n  workflow_dispatch:",
            "on:\n  workflow_dispatch:",
        )
        conversion = convert(wf, default_branch="main")
        self.assertEqual(errors(conversion), [])
        pipeline = conversion.pipeline
        # deploy job renamed; needs maps to normalized names
        deploy = find_job(pipeline, "release")
        self.assertEqual(deploy["dependsOn"], ["ingest"])
        self.assertEqual(deploy["timeoutInMinutes"], 60)
        # git pull + push rewritten in the deploy job
        git_step = deploy["steps"][2]
        self.assertIn('gha_migration_git push "HEAD:$BUILD_SOURCEBRANCH"', git_step["bash"])
        self.assertIn("gha_migration_git pull main", git_step["bash"])
        # parameters present
        self.assertEqual(
            [p["name"] for p in pipeline["parameters"]],
            ["dry_run", "discover", "companies", "days"],
        )

    def test_scheduled_variant_needs_optional_inputs(self):
        # same workflow but companies optional -> schedule works
        wf = self.WF.replace("        required: true\n", "")
        conversion = convert(wf, default_branch="main")
        self.assertEqual(errors(conversion), [])
        self.assertEqual(len(conversion.pipeline["schedules"]), 3)
        self.assertEqual(
            conversion.pipeline["schedules"][2]["cron"], "30 7 * * 1"
        )
        # bool conditions on automatic events require false defaults (they are false)
        self.assertIsNotNone(conversion.pipeline)


if __name__ == "__main__":
    unittest.main()
