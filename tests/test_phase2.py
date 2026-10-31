"""Suites, checks, rendering, keys, the store, the runner's pure parts and stats."""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from evalkit import checks, engine, plan, render, runner, stats
from evalkit.spec import PromptSpec, SpecError, Variant, load_suite
from evalkit.store import JsonlStore, Record
from evalkit.trace import ToolCall, Trace
from tests.helpers import TempDirTest


def trace(text: str = "", files: tuple[tuple[str, str | None], ...] = (),
          calls: tuple[ToolCall, ...] = (), skills: tuple[str, ...] = ()) -> Trace:
    return Trace(text, calls, skills, files, 10, 5, 0.01, 100)


SUITE = """\
samples = 2
allowed_tools = ["Read"]

[variant.control]

[variant.skill]
subject = "widget-check"
effort  = "low"

[[case]]
id     = "one"
input  = "check my widget"
files  = { "w.txt" = "widget" }
checks = [{ regex = "OK" }]

[[case]]
id     = "two"
input  = "and this one"
checks = [{ any = [{ contains = "fine" }, { file_exists = "report.md" }] }]
"""


class ChecksTest(unittest.TestCase):
    def test_each_kind(self) -> None:
        t = trace("all OK", files=(("out.md", "done"),), calls=(ToolCall("Read", "{}"),))
        passing = [{"contains": "OK"}, {"regex": r"^all"}, {"contains": "done", "in": "out.md"},
                   {"file_exists": "out.md"}, {"tool_called": "Read"},
                   {"all": [{"contains": "all"}, {"contains": "OK"}]},
                   {"any": [{"contains": "nope"}, {"contains": "OK"}]}]
        for raw in passing:
            with self.subTest(raw=raw):
                self.assertTrue(checks.parse(raw).passes(t, {}))
        failing = [{"contains": "nope"}, {"regex": "x+y"}, {"contains": "x", "in": "missing.md"},
                   {"file_exists": "missing.md"}, {"tool_called": "Bash"},
                   {"all": [{"contains": "all"}, {"contains": "nope"}]}]
        for raw in failing:
            with self.subTest(raw=raw):
                self.assertFalse(checks.parse(raw).passes(t, {}))

    def test_rejects_malformed_checks(self) -> None:
        bad = [{}, {"contains": "a", "regex": "b"}, {"contains": ""}, {"regex": "("},
               {"file_exists": "a", "in": "b"}, {"all": []}, {"judge": ""}, {"vibe": "x"},
               {"contains": 3}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                checks.parse(raw)


class SuiteTest(TempDirTest):
    def test_loads_variants_with_precedence_and_typed_checks(self) -> None:
        self.make_skill()
        self.write("w.skill.toml", 'schema = 1\nkind = "skill"\nfrom = "widget-check"\n'
                                   '[override]\nmodel = "claude-haiku-5-5"\neffort = "high"\n')
        suite = load_suite(self.write("suite.toml", SUITE + '[variant.tuned]\nsubject = "w.skill.toml"\n'))
        self.assertEqual([v.id for v in suite.variants], ["control", "skill", "tuned"])
        self.assertIsNone(suite.variant("control").subject)
        self.assertEqual(suite.variant("skill").effort, "low")
        tuned = suite.variant("tuned")
        self.assertEqual((tuned.model, tuned.effort), ("claude-haiku-5-5", "high"))
        self.assertIsInstance(suite.cases[1].checks[0], checks.AnyOf)
        self.assertEqual(suite.cases[0].files, (("w.txt", "widget"),))

    def test_rejects_bad_suites(self) -> None:
        self.make_skill()
        bad = {
            "no variants": '[[case]]\nid = "a"\ninput = "x"\n',
            "no cases": "[variant.control]\n",
            "samples": 'samples = 0\n[variant.control]\n[[case]]\nid = "a"\ninput = "x"\n',
            "check": '[variant.control]\n[[case]]\nid = "a"\ninput = "x"\nchecks = [{ vibe = "x" }]\n',
            "judge": '[judge]\neffort = "huge"\n[variant.control]\n[[case]]\nid = "a"\ninput = "x"\n',
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(SpecError):
                load_suite(self.write("bad.toml", text))
        suite = load_suite(self.write("ok.toml", '[variant.control]\n[[case]]\nid = "a"\ninput = "x"\n'))
        with self.assertRaisesRegex(SpecError, "no variant 'nope'"):
            suite.variant("nope")


class RenderAndPlanTest(TempDirTest):
    def prompt_suite(self, template: str = "Q: {{input}}") -> Path:
        self.write("p.prompt.toml", 'schema = 1\nkind = "prompt"\nid = "p"\n'
                   '[[section]]\nname = "role"\ntext = "Be brief."\n'
                   '[[section]]\nname = "note"\ntext = "Mind the date."\nin = "user"\n'
                   '[[example]]\ninput = "a"\noutput = "b"\n'
                   f'[user]\ndocuments = ["doc text"]\ntemplate = "{template}"\n')
        return self.write("s.toml", '[variant.p]\nsubject = "p.prompt.toml"\n'
                                    '[[case]]\nid = "a"\ninput = "hello"\n')

    def test_prompt_renders_documents_above_query(self) -> None:
        suite = load_suite(self.prompt_suite())
        variant, case = suite.variants[0], suite.cases[0]
        assert isinstance(variant.subject, PromptSpec)
        system = render.system_prompt(variant.subject)
        self.assertIn("<role>\nBe brief.\n</role>", system)
        self.assertIn("<examples>", system)
        self.assertNotIn("Mind the date", system)
        user = render.user_turn(variant, case)
        self.assertLess(user.index("<documents>"), user.index("<note>"))
        self.assertTrue(user.endswith("Q: hello"))

    def test_keys_are_stable_and_change_with_inputs(self) -> None:
        self.make_skill()
        path = self.write("suite.toml", SUITE)
        first = [j.key for j in plan.jobs(load_suite(path))]
        self.assertEqual(len(first), 2 * 2 * 2)
        self.assertEqual(len(set(first)), len(first))
        self.assertEqual(first, [j.key for j in plan.jobs(load_suite(path))])
        self.write("widget-check/evals/suite.toml", "# the skill's own suite is not the skill")
        self.assertEqual(first, [j.key for j in plan.jobs(load_suite(path))])
        self.write("widget-check/references/guide.md", "# Guide, edited\n")
        changed = [j.key for j in plan.jobs(load_suite(path))]
        self.assertEqual(first[:4], changed[:4])  # control untouched
        self.assertNotEqual(first[4:], changed[4:])


class StoreTest(TempDirTest):
    def test_round_trip_and_last_record_wins(self) -> None:
        path = self.root / "s" / "t.jsonl"
        store = JsonlStore(path)
        t = trace("hi", files=(("a.bin", None),), calls=(ToolCall("Skill", '{"skill": "x"}'),))
        store.put(Record("k", "v", "c", 0, "1.0", t))
        store.put(Record("k", "v", "c", 0, "2.0", replace(t, final_text="again")))
        reloaded = JsonlStore(path).get("k")
        assert reloaded is not None
        self.assertEqual((reloaded.cli_version, reloaded.trace.final_text), ("2.0", "again"))
        self.assertEqual(reloaded.trace.files, (("a.bin", None),))

    def test_corrupt_line_names_the_file_and_line(self) -> None:
        path = self.write("bad.jsonl", '{"key": "k"}\n')
        with self.assertRaisesRegex(ValueError, "bad.jsonl:1"):
            JsonlStore(path)


class RunnerPureTest(TempDirTest):
    def test_command_for_each_kind(self) -> None:
        self.make_skill()
        self.write("p.prompt.toml", 'schema = 1\nkind = "prompt"\nid = "p"\ntools = ["Read"]\n')
        suite = load_suite(self.write("s.toml", SUITE + '[variant.p]\nsubject = "p.prompt.toml"\n'))
        control = runner.command(suite.variant("control"), None, ["Read"])
        self.assertIn("acceptEdits", control)
        self.assertEqual(control[control.index("--allowed-tools") + 1], "Read")
        self.assertNotIn("--model", control)
        skill = runner.command(suite.variant("skill"), None, [])
        self.assertEqual(skill[skill.index("--effort") + 1], "low")
        self.assertNotIn("--allowed-tools", skill)
        prompt = runner.command(suite.variant("p"), Path("/tmp/sys.md"), [])
        self.assertEqual(prompt[prompt.index("--tools") + 1], "Read")
        self.assertIn("--system-prompt-file", prompt)
        for argv in (control, skill, prompt):
            self.assertIn("--strict-mcp-config", argv)

    def test_parse_stream(self) -> None:
        lines = [
            json.dumps({"type": "system", "subtype": "init", "skills": ["pong", "debug"]}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hm"},
                {"type": "tool_use", "name": "Skill", "input": {"skill": "pong"}}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "PONG",
                        "usage": {"input_tokens": 2, "cache_read_input_tokens": 8,
                                  "output_tokens": 3}, "total_cost_usd": 0.5,
                        "duration_ms": 900}),
        ]
        parsed = runner.parse_stream(lines)
        self.assertEqual(parsed.loaded_skills, ("pong", "debug"))
        self.assertEqual(parsed.final_text, "PONG")
        self.assertEqual((parsed.input_tokens, parsed.output_tokens), (10, 3))
        self.assertFalse(parsed.is_error)
        self.assertEqual(runner._skills_invoked(parsed.tool_calls), ("pong",))
        with self.assertRaisesRegex(runner.RunError, "without a result"):
            runner.parse_stream(lines[:2])

    def test_isolation_check(self) -> None:
        self.make_skill()
        suite = load_suite(self.write("s.toml", SUITE))
        control, skill = suite.variant("control"), suite.variant("skill")
        runner.check_isolation(control, ["debug"], ["widget-check"])
        runner.check_isolation(skill, ["widget-check", "debug"], ["widget-check"])
        with self.assertRaisesRegex(runner.RunError, "also loaded widget-check"):
            runner.check_isolation(control, ["widget-check"], ["widget-check"])
        with self.assertRaisesRegex(runner.RunError, "was not loaded"):
            runner.check_isolation(skill, ["debug"], ["widget-check"])


class FakeRunner:
    """Passes case 'one' only for the skill variant; fails the second call it gets."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, variant: Variant, case: object) -> Trace:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("boom")
        ok = variant.id == "skill"
        return trace("OK" if ok else "no", skills=("widget-check",) if ok else ())


class EngineAndStatsTest(TempDirTest):
    def test_run_store_score_and_compare(self) -> None:
        self.make_skill()
        suite = load_suite(self.write("suite.toml", SUITE))
        store = JsonlStore(self.root / "t.jsonl")
        jobs = plan.jobs(suite)
        events: list[tuple[str, bool]] = []
        failures = engine.execute(engine.pending(jobs, store, "v1"), FakeRunner(), store, "v1", 1,
                                  lambda label, err: events.append((label, err is None)))
        self.assertEqual((failures, len(events)), (1, 8))
        self.assertEqual(len(engine.pending(jobs, store, "v1")), 1)  # the failure retries
        self.assertEqual(len(engine.pending(jobs, store, "v2")), 8)  # new CLI re-runs all
        engine.execute(engine.pending(jobs, store, "v1"), FakeRunner(), store, "v1", 1,
                       lambda *_: None)
        outcomes = engine.score(jobs, store, suite.judge).outcomes
        self.assertEqual(len(outcomes), 8)
        summary = {s.variant: s for s in stats.summarise(outcomes)}
        self.assertIsNone(summary["control"].trigger_rate)
        self.assertEqual(summary["skill"].trigger_rate, 1.0)
        result = stats.compare(outcomes, "control", "skill")
        self.assertEqual(result.cases, 2)
        self.assertAlmostEqual(result.lift, 0.5)  # case one 0 -> 1, case two 0 -> 0
        self.assertEqual((result.improved, result.regressed), (("one",), ()))
        self.assertEqual(result.lift_when_triggered, 0.5)

    def test_bootstrap(self) -> None:
        self.assertEqual(stats.bootstrap([0.2] * 5), (0.2, 0.2))
        low, high = stats.bootstrap([-1.0, 0.0, 1.0, 1.0])
        self.assertLessEqual(low, 0.25)
        self.assertGreaterEqual(high, 0.25)
        self.assertEqual(stats.bootstrap([0.0, 1.0]), stats.bootstrap([0.0, 1.0]))  # seeded

    def test_compare_needs_shared_cases(self) -> None:
        with self.assertRaisesRegex(ValueError, "no case in common"):
            stats.compare([engine.Outcome("a", "x", 0, True, (), None)], "a", "b")


if __name__ == "__main__":
    unittest.main()
