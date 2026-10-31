"""Cases that start from a git checkout, suite settings, ``body:`` text and per-check rates."""

import os
import subprocess
from pathlib import Path

from evalkit import checkout, engine, plan, runner, stats
from evalkit.spec import Checkout, SpecError, load_suite
from tests.helpers import TempDirTest

SHA_A = "a" * 40
SHA_B = "b" * 40

SUITE = f"""\
samples = 1
settings = '{{"sandbox": {{"enabled": true, "filesystem": {{"denyRead": ["${{KEYS}}"]}}}}}}'

[variant.reviewer]
subject = "r.prompt.toml"

[[case]]
id       = "pr-1"
input    = "Review PR 1."
checkout = {{ from = "${{TARGET}}", head = "{SHA_A}", base = "{SHA_B}", ref = "refs/pull/1/head" }}
checks   = [{{ judge = "K1: names the defect" }}, {{ regex = "Verdict" }}]
"""

PROMPT = """\
schema = 1
kind   = "prompt"
id     = "reviewer"
tools  = ["Read", "Bash"]

[[section]]
name = "instructions"
text = "body:reviewer.md"
"""


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          check=True)
    return done.stdout.strip()


class SuiteSpecTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.write("reviewer.md", "---\nname: reviewer\ntools: Read\n---\nYou review code.\n")
        self.write("r.prompt.toml", PROMPT)

    def test_loads_checkout_settings_and_body(self) -> None:
        suite = load_suite(self.write("s.toml", SUITE))
        case = suite.cases[0]
        self.assertEqual(case.checkout, Checkout("${TARGET}", SHA_A, SHA_B, "refs/pull/1/head"))
        self.assertIn("${KEYS}", suite.settings or "")
        prompt = suite.variant("reviewer").subject
        self.assertEqual(prompt.sections[0].text.strip(), "You review code.")  # type: ignore[union-attr]

    def test_rejects_bad_checkouts_and_settings(self) -> None:
        bad = {
            "a 40-character": SUITE.replace(SHA_A, "abc123"),
            "head is required": SUITE.replace(f'head = "{SHA_A}", ', ""),
            "must start with refs/": SUITE.replace("refs/pull/1/head", "pull/1/head"),
            "unknown key": SUITE.replace("ref = ", "branch = "),
            "not JSON": SUITE.replace("settings = '{", "settings = '{{"),
            "JSON object": SUITE.replace(SUITE.splitlines()[1], "settings = '[1]'"),
        }
        for message, text in bad.items():
            with self.subTest(message), self.assertRaisesRegex(SpecError, message):
                load_suite(self.write("bad.toml", text))

    def test_key_follows_commits_and_settings_not_the_source_path(self) -> None:
        def key(text: str) -> str:
            return plan.jobs(load_suite(self.write("k.toml", text)))[0].key

        base = key(SUITE)
        self.assertEqual(base, key(SUITE.replace("${TARGET}", "/elsewhere")))
        self.assertNotEqual(base, key(SUITE.replace(f'base = "{SHA_B}"', f'base = "{"c" * 40}"')))
        self.assertNotEqual(base, key(SUITE.replace('"enabled": true', '"enabled": false')))

    def test_prompt_variants_get_the_suites_allowed_tools(self) -> None:
        suite = load_suite(self.write("s.toml", SUITE))
        argv = runner.command(suite.variant("reviewer"), Path("/tmp/sys.md"), ["Bash"])
        self.assertEqual(argv[argv.index("--allowed-tools") + 1], "Bash")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Bash")


class CheckoutTest(TempDirTest):
    def test_expand(self) -> None:
        self.assertEqual(checkout.expand("${A}/x and $B", {"A": "/a"}), "/a/x and $B")
        with self.assertRaisesRegex(checkout.CheckoutError, r"\$\{B\} is not set"):
            checkout.expand("${B}", {"B": ""})

    def test_materialise_holds_the_head_and_nothing_later(self) -> None:
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "-q")
        (source / "f.txt").write_text("one\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "one")
        base = git(source, "rev-parse", "HEAD")
        (source / "f.txt").write_text("two\n")
        git(source, "commit", "-qam", "two")
        head = git(source, "rev-parse", "HEAD")
        (source / "f.txt").write_text("three\n")
        git(source, "commit", "-qam", "three (the fix)")
        later = git(source, "rev-parse", "HEAD")

        dest = self.root / "dest"
        dest.mkdir()
        os.environ["EVALKIT_TEST_SOURCE"] = str(source)
        self.addCleanup(os.environ.pop, "EVALKIT_TEST_SOURCE")
        checkout.materialise(Checkout("${EVALKIT_TEST_SOURCE}", head, base, "refs/pull/7/head"), dest)

        self.assertEqual((dest / "f.txt").read_text(), "two\n")
        self.assertEqual(git(dest, "rev-parse", "refs/pull/7/head"), head)
        git(dest, "fetch", "-q", "origin", "refs/pull/7/head")  # the pinning step works
        self.assertEqual(git(dest, "cat-file", "-t", base), "commit")
        later_known = subprocess.run(["git", "cat-file", "-e", later], cwd=dest, check=False)
        self.assertNotEqual(later_known.returncode, 0)
        self.assertNotIn(str(source), git(dest, "remote", "-v"))

    def test_missing_source_is_an_error(self) -> None:
        dest = self.root / "dest"
        dest.mkdir()
        with self.assertRaisesRegex(checkout.CheckoutError, "not a directory"):
            checkout.materialise(Checkout(str(self.root / "nope"), SHA_A), dest)

    def test_snapshot_keeps_only_what_the_run_changed(self) -> None:
        project = self.root / "project"
        (project / "src").mkdir(parents=True)
        (project / "src" / "a.rs").write_text("fn a() {}\n")
        (project / "src" / "b.rs").write_text("fn b() {}\n")
        baseline = runner._stats(project)
        (project / "src" / "b.rs").write_text("fn b() { changed() }\n")
        (project / "probe.pyr").write_text("print(1)\n")
        files = dict(runner._snapshot(project, baseline))
        self.assertEqual(set(files), {"src/b.rs", "probe.pyr"})


class ByCheckTest(TempDirTest):
    def test_rates_per_check(self) -> None:
        def outcome(sample: int, k1: bool) -> engine.Outcome:
            return engine.Outcome("v", "pr-1", sample, k1, (), None, (("K1", k1), ("K2", True)))

        rates = stats.by_check([outcome(0, True), outcome(1, False), outcome(2, True)])
        self.assertEqual([(r.check, r.passed, r.runs) for r in rates], [("K1", 2, 3), ("K2", 3, 3)])
