"""Each run's scratch is private, and its sandbox cannot read the shared temp root."""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from evalkit import engine, keys, plan, runner
from evalkit.spec import SpecError, load_suite
from evalkit.store import JsonlStore, Record
from evalkit.trace import Trace
from tests.helpers import TempDirTest

SUITE = """\
samples = 1
allowed_tools = ["Bash"]

[variant.control]

[[case]]
id     = "one"
input  = "hello"
checks = [{ regex = "OK" }]
"""

STREAM = "\n".join(json.dumps(e) for e in (
    {"type": "system", "subtype": "init", "skills": []},
    {"type": "result", "subtype": "success", "result": "OK", "usage": {}},
))


class FakeClaude:
    """Stands in for ``subprocess.run``, recording what each run was given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env: dict[str, str] = kwargs["env"]
        settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
        self.calls.append({
            "argv": argv, "env": env, "cwd": Path(kwargs["cwd"]),
            "settings": None if settings is None else json.loads(Path(settings).read_text()),
            "tmp_is_dir": Path(env.get("TMPDIR", "/nonexistent")).is_dir(),
        })
        return subprocess.CompletedProcess(argv, 0, STREAM, "")


class FakeClaudeTest(TempDirTest):
    """Runs go to a fake ``claude`` under a controlled shared temp root."""

    def setUp(self) -> None:
        super().setUp()
        self.shared = (self.root / "shared").resolve()
        self.shared.mkdir()
        self.fake = FakeClaude()
        patches = [mock.patch.object(tempfile, "tempdir", str(self.shared)),
                   mock.patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "t",
                                                  "TMPDIR": str(self.shared),
                                                  "CARGO_TARGET_DIR": str(self.shared / "t")}),
                   mock.patch("evalkit.runner.subprocess.run", self.fake)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.suite = load_suite(self.write("s.toml", SUITE))

    def run_twice(self) -> list[dict[str, Any]]:
        cli = runner.CliRunner(self.suite)
        for _ in range(2):
            cli.run(self.suite.variant("control"), self.suite.cases[0])
        return self.fake.calls


class RunIsolationTest(FakeClaudeTest):
    def test_each_run_gets_its_own_temp_dir(self) -> None:
        first, second = self.run_twice()
        for call in (first, second):
            root = call["cwd"].parent
            tmp = Path(call["env"]["TMPDIR"])
            self.assertTrue(call["tmp_is_dir"])
            self.assertTrue(tmp.is_relative_to(root), f"{tmp} is outside the run's root {root}")
            self.assertNotEqual(tmp, self.shared)
            for name in ("TMP", "TEMP", "CLAUDE_CODE_TMPDIR"):
                self.assertEqual(call["env"][name], str(tmp), name)
            self.assertTrue(Path(call["env"]["CARGO_TARGET_DIR"]).is_relative_to(tmp))
        self.assertNotEqual(first["env"]["TMPDIR"], second["env"]["TMPDIR"])

    def test_sandbox_denies_the_shared_root_and_reopens_only_its_own(self) -> None:
        first, second = self.run_twice()
        for call in (first, second):
            fs = call["settings"]["sandbox"]["filesystem"]
            self.assertIn(str(self.shared), fs["denyRead"])
            self.assertEqual(fs["allowRead"], [str(call["cwd"].parent)])
        self.assertNotEqual(first["settings"], second["settings"])

    def test_judge_calls_get_a_private_temp_dir_too(self) -> None:
        cli = runner.CliRunner(self.suite)
        cli.ask(self.suite.judge, "system", "user")
        (call,) = self.fake.calls
        self.assertTrue(Path(call["env"]["TMPDIR"]).is_relative_to(call["cwd"].parent))

    def test_isolation_settings_are_pure(self) -> None:
        got = runner.isolation_settings(Path("/t/evalkit-1"), (Path("/t"), Path("/tmp")))
        self.assertEqual(got, {"sandbox": {"filesystem": {"denyRead": ["/t", "/tmp"],
                                                          "allowRead": ["/t/evalkit-1"]}}})


ISOLATED = SUITE.replace("samples = 1\n", 'samples = 1\nisolated = true\nreadable = ["~/.cargo"]\n')


class IsolatedSuiteTest(FakeClaudeTest):
    """``isolated = true`` fences the file tools too, and fails closed on Bash."""

    def test_isolated_suite_blocks_file_tools_and_forces_the_sandbox(self) -> None:
        self.suite = load_suite(self.write("s.toml", ISOLATED))
        first, _ = self.run_twice()
        settings = first["settings"]
        self.assertIs(settings["permissions"]["blockReadsOutsideWorkingDirectories"], True)
        sandbox = settings["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["failIfUnavailable"],
                          sandbox["allowUnsandboxedCommands"]), (True, True, False))
        self.assertEqual(sandbox["filesystem"]["allowRead"],
                         [str(first["cwd"].parent), "~/.cargo"])

    def test_plain_suite_sets_no_file_tool_block(self) -> None:
        first, _ = self.run_twice()
        self.assertNotIn("permissions", first["settings"])
        self.assertNotIn("enabled", first["settings"]["sandbox"])

    def test_readable_needs_isolated_and_isolated_changes_the_key(self) -> None:
        with self.assertRaisesRegex(SpecError, "readable"):
            load_suite(self.write("bad.toml", SUITE.replace(
                "samples = 1\n", 'samples = 1\nreadable = ["~/.cargo"]\n')))
        plain = plan.jobs(load_suite(self.write("p.toml", SUITE)))[0].key
        fenced = plan.jobs(load_suite(self.write("f.toml", ISOLATED)))[0].key
        self.assertNotEqual(plain, fenced)


class PolicyTest(TempDirTest):
    """A run made under an older isolation policy never satisfies a current job."""

    def test_old_policy_record_is_pending_and_labelled(self) -> None:
        suite = load_suite(self.write("s.toml", SUITE))
        jobs = plan.jobs(suite)
        store_path = self.root / "t.jsonl"
        store = JsonlStore(store_path)
        old = Trace("OK", (), (), (), 1, 1, 0.0, 1)
        store.put(Record(jobs[0].key, "control", "one", 0, "v1", old, policy=0))
        self.assertEqual(engine.pending(jobs, store, "v1"), jobs)
        line = json.loads(store_path.read_text())
        del line["policy"]  # as written before policies existed
        store_path.write_text(json.dumps(line) + "\n")
        reloaded = JsonlStore(store_path)
        self.assertEqual(reloaded.get(jobs[0].key).policy, 0)  # type: ignore[union-attr]
        manifest = keys.manifest(suite, jobs, reloaded,
                                 engine.score(jobs, reloaded, suite.judge).outcomes)
        self.assertIn("policy", manifest.splitlines()[1].split("\t"))
        self.assertEqual(manifest.splitlines()[2].split("\t")[-2], "0")
        reloaded.put(Record(jobs[0].key, "control", "one", 0, "v1", old))
        self.assertEqual(reloaded.get(jobs[0].key).policy, plan.ISOLATION_POLICY)  # type: ignore[union-attr]
        self.assertEqual(engine.pending(jobs, reloaded, "v1"), [])
