import contextlib
import io
import json
import tomllib
import unittest

from evalkit.cli import main
from evalkit.spec import load_cases
from tests.helpers import TempDirTest


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class LintCommandTest(TempDirTest):
    def test_clean_tree_exits_zero_and_finds_skills_recursively(self) -> None:
        self.make_skill()
        code, out, _ = run_cli("lint", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("skill.cases", out)  # no suite yet: a note, not an error
        self.assertIn("0 error(s)", out)

    def test_error_exits_one_and_json_is_parseable(self) -> None:
        self.write("bad/SKILL.md", "---\nname: wrong\ndescription: Use when x.\n---\n")
        code, out, _ = run_cli("lint", str(self.root / "bad"), "--format", "json",
                               "--min-severity", "error")
        self.assertEqual(code, 1)
        (finding,) = json.loads(out)
        self.assertEqual((finding["rule"], finding["severity"]), ("skill.name", "error"))

    def test_malformed_spec_is_a_finding_not_a_crash(self) -> None:
        path = self.write("p.prompt.toml", 'schema = 1\nkind = "prompt"\nid = "x"\ntemperature = 0\n')
        code, out, _ = run_cli("lint", str(path))
        self.assertEqual(code, 1)
        self.assertIn("spec", out)

    def test_empty_directory_reports_on_stderr(self) -> None:
        (self.root / "nothing").mkdir()
        code, _, err = run_cli("lint", str(self.root / "nothing"))
        self.assertEqual(code, 0)
        self.assertIn("no SKILL.md", err)


class InitCommandTest(TempDirTest):
    def test_writes_a_loadable_suite_with_three_cases(self) -> None:
        skill = self.make_skill()
        code, out, _ = run_cli("init", str(skill))
        self.assertEqual(code, 0, out)
        suite = skill / "evals" / "suite.toml"
        data = tomllib.loads(suite.read_text())
        self.assertEqual(set(data["variant"]), {"control", "skill"})
        cases = load_cases(suite)
        self.assertEqual(len(cases), 3)
        self.assertIn("Use when the user mentions widgets", cases[0].input)
        code, out, _ = run_cli("lint", str(skill))
        self.assertIn("pass vacuously", out)  # the TODO cases have no checks yet

    def test_refuses_to_overwrite_without_force(self) -> None:
        skill = self.make_skill()
        run_cli("init", str(skill))
        code, _, err = run_cli("init", str(skill))
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(run_cli("init", str(skill), "--force")[0], 0)


if __name__ == "__main__":
    unittest.main()
