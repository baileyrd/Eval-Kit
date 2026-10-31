"""Judge checks: prompt, verdict parsing, caching, scoring with verdicts, calibration."""

import contextlib
import io
import json
import unittest

from evalkit import checks, engine, judge, plan
from evalkit.cli import main
from evalkit.spec import JudgeConfig, load_suite
from evalkit.store import JsonlStore, Record
from evalkit.trace import Trace
from tests.helpers import TempDirTest

SUITE = """\
samples = 1

[judge]
model = "claude-haiku-5-5"

[variant.control]

[variant.skill]
subject = "widget-check"

[[case]]
id     = "one"
input  = "describe the widget"
checks = [{ contains = "widget" }, { judge = "Mentions the colour" }]
"""


def trace(text: str, files: tuple[tuple[str, str | None], ...] = ()) -> Trace:
    return Trace(text, (), (), files, 1, 1, 0.0, 1)


class FakeJudge:
    """Passes any reply that mentions 'red'; counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def ask(self, config: JudgeConfig, system: str, user_text: str) -> str:
        self.calls += 1
        passed = "red" in user_text.split("<final_reply>")[1]
        return f'Sure. {{"reason": "checked", "pass": {json.dumps(passed)}}}'


class PureTest(unittest.TestCase):
    def test_prompt_shows_criterion_reply_and_text_files(self) -> None:
        text = judge.prompt("Is it red?", trace("a red widget", (("a.md", "x"), ("b.png", None))))
        self.assertIn("<criterion>\nIs it red?\n</criterion>", text)
        self.assertIn('<file path="a.md">', text)
        self.assertNotIn("b.png", text)

    def test_parse_verdict(self) -> None:
        self.assertEqual(judge.parse_verdict('{"reason": "ok", "pass": true}'), (True, "ok"))
        self.assertEqual(judge.parse_verdict('text\n{"pass": false}\n'), (False, ""))
        for bad in ("no json", '{"pass": "yes"}', "{not json}"):
            with self.subTest(bad=bad), self.assertRaises(judge.VerdictError):
                judge.parse_verdict(bad)

    def test_key_changes_with_rubric_model_and_trace(self) -> None:
        base = judge.verdict_key("r", JudgeConfig(), trace("a"))
        self.assertEqual(base, judge.verdict_key("r", JudgeConfig(), trace("a")))
        self.assertNotEqual(base, judge.verdict_key("r2", JudgeConfig(), trace("a")))
        self.assertNotEqual(base, judge.verdict_key("r", JudgeConfig(model="m"), trace("a")))
        self.assertNotEqual(base, judge.verdict_key("r", JudgeConfig(), trace("b")))

    def test_rubrics_found_inside_combinators(self) -> None:
        check = checks.parse({"any": [{"judge": "a"}, {"all": [{"judge": "b"}, {"regex": "x"}]}]})
        self.assertEqual(checks.rubrics(check), ("a", "b"))

    def test_agreement(self) -> None:
        result = judge.agreement([(True, True), (True, False), (False, True), (False, False)])
        self.assertEqual((result.agreed, result.judge_too_lenient, result.judge_too_strict), (2, 1, 1))
        self.assertEqual(result.rate, 0.5)
        self.assertEqual(judge.agreement([]).rate, 0.0)


class JudgingFlowTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.make_skill()
        self.suite_path = self.write("suite.toml", SUITE)
        self.suite = load_suite(self.suite_path)
        self.store = JsonlStore(self.root / ".evalkit" / "suite.jsonl")
        self.jobs = plan.jobs(self.suite)
        replies = {"control": "a widget", "skill": "a red widget"}
        for job in self.jobs:
            self.store.put(Record(job.key, job.variant.id, job.case.id, job.sample, "v1",
                                  trace(replies[job.variant.id])))

    def test_unjudged_runs_are_left_out_until_judged_then_cached(self) -> None:
        scored = engine.score(self.jobs, self.store, self.suite.judge)
        self.assertEqual((scored.outcomes, scored.unjudged), ([], 2))
        todo = judge.pending(self.jobs, self.store, self.suite.judge)
        self.assertEqual(len(todo), 2)
        fake = FakeJudge()
        failures = engine.execute_judging(todo, fake, self.suite.judge, self.store, 1,
                                          lambda *_: None)
        self.assertEqual((failures, fake.calls), (0, 2))
        self.assertEqual(judge.pending(self.jobs, self.store, self.suite.judge), [])
        reloaded = JsonlStore(self.store.path)
        scored = engine.score(self.jobs, reloaded, self.suite.judge)
        by_variant = {o.variant: o for o in scored.outcomes}
        self.assertTrue(by_variant["skill"].passed)
        self.assertFalse(by_variant["control"].passed)
        self.assertEqual(by_variant["control"].failed_checks,
                         ("judge: Mentions the colour (checked)",))

    def test_bad_judge_reply_is_reported_not_cached(self) -> None:
        class Mumbler:
            def ask(self, config: JudgeConfig, system: str, user_text: str) -> str:
                return "I think it's fine"

        todo = judge.pending(self.jobs, self.store, self.suite.judge)
        errors: list[Exception | None] = []
        failures = engine.execute_judging(todo, Mumbler(), self.suite.judge, self.store, 1,
                                          lambda _, err: errors.append(err))
        self.assertEqual(failures, 2)
        self.assertIsInstance(errors[0], judge.VerdictError)
        self.assertEqual(len(judge.pending(self.jobs, self.store, self.suite.judge)), 2)

    def test_calibrate_writes_template_then_scores_labels(self) -> None:
        engine.execute_judging(judge.pending(self.jobs, self.store, self.suite.judge), FakeJudge(),
                               self.suite.judge, self.store, 1, lambda *_: None)
        labels = self.root / "labels.jsonl"
        code, out = self.cli("calibrate", str(self.suite_path), str(labels))
        self.assertEqual(code, 0, out)
        rows = [json.loads(line) for line in labels.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["label"] is None for row in rows))
        for row in rows:  # the person disagrees with the judge on the control run
            row["label"] = True
        labels.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        code, out = self.cli("calibrate", str(self.suite_path), str(labels))
        self.assertEqual(code, 0, out)
        self.assertIn("1/2 agree (50%)", out)
        self.assertIn("too strict 1", out)
        self.assertIn("control one #0", out)

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(list(argv))
        return code, out.getvalue()


if __name__ == "__main__":
    unittest.main()
