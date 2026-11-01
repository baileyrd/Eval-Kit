"""Each run's scratch is private, and its sandbox cannot read the shared temp root."""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from evalkit import runner
from evalkit.spec import load_suite
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


class RunIsolationTest(TempDirTest):
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
