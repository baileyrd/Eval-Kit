"""Ablation pieces and rewrites, the gate rule, past revisions, and the two commands."""

import contextlib
import io
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from evalkit import ablate, gitrev, stats
from evalkit.ablate import Piece
from evalkit.cli import main
from evalkit.spec import Case, Example, JudgeConfig, PromptSpec, Section, Variant, load_suite
from evalkit.trace import Trace
from tests.helpers import TempDirTest, skill_md

BODY = """\
# Widget check

Intro.

## Steps

1. Read the widget.

```bash
## not a heading
```

## Report

Say OK.

# Appendix

Kept.
"""

SUITE = """\
samples = 1

[variant.control]

[variant.skill]
subject = "widget-check"

[[case]]
id     = "one"
input  = "check my widget"
checks = [{ contains = "OK" }]
"""


def prompt() -> PromptSpec:
    return PromptSpec(Path("p.prompt.toml"), "p", None, None, (),
                      (Section("role", "You check widgets."), Section("format", "Say OK.")),
                      (Example("a", "b"),), "{{input}}", ("doc one", "doc two"))


class FakeRunner:
    """Replies OK unless the skill's Report section is missing; never needs the CLI."""

    def __init__(self, suite: object) -> None:
        pass

    def run(self, variant: Variant, case: Case) -> Trace:
        skill = variant.skill
        says_ok = skill is not None and "## Report" in (skill.root / "SKILL.md").read_text()
        return Trace("OK" if says_ok else "hmm", (), (), (), 1, 1, 0.0, 1)

    def ask(self, config: JudgeConfig, system: str, user_text: str) -> str:
        raise AssertionError("no judge checks in this suite")


def cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
            mock.patch("evalkit.cli.CliRunner", FakeRunner), \
            mock.patch("evalkit.cli.cli_version", lambda: "test"):
        code = main(list(argv))
    return code, out.getvalue()


class PromptPiecesTest(unittest.TestCase):
    def test_pieces_and_removal(self) -> None:
        spec = prompt()
        labels = [p.label for p in ablate.pieces(spec)]
        self.assertEqual(labels, ["section role", "section format", "examples",
                                  "document 1", "document 2"])
        self.assertEqual([s.name for s in ablate.without_prompt_piece(
            spec, Piece("section", "role")).sections], ["format"])
        self.assertEqual(ablate.without_prompt_piece(spec, Piece("examples", "")).examples, ())
        self.assertEqual(ablate.without_prompt_piece(spec, Piece("document", "1")).documents,
                         ("doc two",))


class SkillPiecesTest(TempDirTest):
    def test_sections_ignore_fences_and_end_at_next_heading(self) -> None:
        text = skill_md(body=BODY)
        cut = ablate.without_section(text, "Steps")
        self.assertTrue(cut.startswith("---\nname: widget-check\n"))
        self.assertNotIn("Read the widget", cut)
        self.assertNotIn("not a heading", cut)
        self.assertIn("## Report", cut)
        cut = ablate.without_section(text, "Report")
        self.assertNotIn("Say OK", cut)
        self.assertIn("# Appendix\n\nKept.", cut)
        with self.assertRaises(ValueError):
            ablate.without_section(text, "Missing")

    def test_variants_are_rewritten_copies(self) -> None:
        self.make_skill(body=BODY)
        self.write("widget-check/evals/suite.toml", "")
        suite = load_suite(self.write("suite.toml", SUITE))
        skill = replace(suite.variant("skill"), model="claude-haiku-5-5")
        made = ablate.variants(skill, self.root / "work")
        self.assertEqual([v.id for _, v in made],
                         ["skill~section-steps", "skill~section-report",
                          "skill~resource-references-guide-md"])
        by_id = {v.id: v for _, v in made}
        without_guide = by_id["skill~resource-references-guide-md"].skill
        assert without_guide is not None
        self.assertFalse((without_guide.root / "references/guide.md").exists())
        self.assertTrue((without_guide.root / "SKILL.md").exists())
        self.assertEqual(by_id["skill~section-steps"].model, "claude-haiku-5-5")
        with self.assertRaises(ValueError):
            ablate.variants(suite.variant("control"), self.root / "work")


class GateRuleTest(unittest.TestCase):
    def result(self, low: float, high: float) -> stats.Comparison:
        return stats.Comparison("a", "b", 10, (low + high) / 2, low, high, (), (), None)

    def test_fails_only_when_whole_interval_is_below_the_drop(self) -> None:
        self.assertTrue(stats.gate_passes(self.result(-0.3, -0.05), 0.1))
        self.assertFalse(stats.gate_passes(self.result(-0.4, -0.15), 0.1))
        self.assertTrue(stats.too_few_cases(self.result(-0.3, 0.1), 0.1))
        self.assertFalse(stats.too_few_cases(self.result(-0.1, 0.05), 0.1))


class CommandsTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.make_skill(body=BODY)
        self.suite = self.write("suite.toml", SUITE)

    def test_ablate_ranks_the_piece_that_matters_first(self) -> None:
        code, out = cli("ablate", str(self.suite), "skill")
        self.assertEqual(code, 0, out)
        rows = [line.strip() for line in out.splitlines() if line.startswith("  ")]
        self.assertTrue(rows[0].startswith("-100%"), out)
        self.assertIn("section Report", rows[0])
        self.assertIn("regressed: one", rows[0])
        self.assertTrue(all(r.startswith("+0%") for r in rows[1:]), out)

    def test_gate(self) -> None:
        code, out = cli("gate", str(self.suite), "control", "skill", "--max-drop", "0.1")
        self.assertEqual(code, 0, out)
        self.assertIn("gate passed", out)
        code, out = cli("gate", str(self.suite), "skill", "control", "--max-drop", "0.1")
        self.assertEqual(code, 1, out)
        self.assertIn("gate failed", out)
        code, out = cli("gate", str(self.suite), "skill", "control", "--max-drop", "2")
        self.assertEqual(code, 1, out)


class RevisionTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        (self.root / ".git").rmdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")

    def commit(self) -> None:
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "change")

    def test_worktree_shows_the_old_file_and_is_removed(self) -> None:
        path = self.write("sub/a.txt", "old")
        self.commit()
        path.write_text("new")
        with gitrev.at_revision(path, "HEAD") as past:
            self.assertEqual(past.read_text(), "old")
            tree = past.parent.parent
        self.assertFalse(tree.exists())
        with self.assertRaises(gitrev.GitError), gitrev.at_revision(path, "no-such-rev"):
            pass

    def test_gate_against_the_committed_skill(self) -> None:
        self.make_skill(body=BODY)
        suite = self.write("suite.toml", SUITE)
        self.commit()
        self.make_skill(body=BODY.replace("## Report", "## Notes"))
        code, out = cli("gate", str(suite), "skill@HEAD", "skill", "--max-drop", "0.1")
        self.assertEqual(code, 1, out)
        self.assertIn("gate failed: skill is likely worse than skill@HEAD", out)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


if __name__ == "__main__":
    unittest.main()
