import unittest
from collections.abc import Callable
from pathlib import Path

from evalkit import rules
from evalkit.lint import Finding, LintContext, Severity, run
from evalkit.spec import Case, load_subject
from tests.helpers import TempDirTest


class SkillRulesTest(TempDirTest):
    def lint(self, skill: Path, rule: Callable[[LintContext], object],
             cases: tuple[Case, ...] | None = ()) -> list[Finding]:
        return list(rule(LintContext(load_subject(skill), cases)))

    def test_clean_skill_has_no_findings(self) -> None:
        cases = tuple(Case(str(i), "x", (), ()) for i in range(3))
        findings = run(LintContext(load_subject(self.make_skill()), cases), rules.RULES)
        self.assertEqual(findings, [])

    def test_name_format_directory_and_reserved_words(self) -> None:
        self.write("Bad--Name/SKILL.md", "---\nname: Bad--Name\ndescription: x when y\n---\n")
        self.assertEqual(len(self.lint(self.root / "Bad--Name", rules.skill_name)), 1)
        self.write("dir/SKILL.md", "---\nname: other\ndescription: x when y\n---\n")
        (finding,) = self.lint(self.root / "dir", rules.skill_name)
        self.assertIn("does not match its directory", finding.message)
        skill = self.make_skill(name="claude-helper")
        (finding,) = self.lint(skill, rules.skill_name)
        self.assertIn("reserved word 'claude'", finding.message)

    def test_description_limits_and_style(self) -> None:
        long = self.make_skill(description="Does x when y. " + "a" * 1100)
        (finding,) = self.lint(long, rules.skill_description)
        self.assertEqual((finding.rule, finding.severity), ("skill.description.limits", Severity.ERROR))
        first_person = self.make_skill(name="first", description="I can help with widgets.")
        messages = [f.message for f in self.lint(first_person, rules.skill_description)]
        self.assertEqual(len(messages), 2)  # first person, and no "when"
        xml = self.make_skill(name="xml", description="Use when <b>asked</b>.")
        self.assertIn("XML", self.lint(xml, rules.skill_description)[0].message)

    def test_listing_cap_counts_when_to_use(self) -> None:
        skill = self.make_skill(description="Use when x. " + "a" * 1000,
                                extra=f"when_to_use: {'b' * 600}\n")
        (finding,) = self.lint(skill, rules.skill_listing)
        self.assertEqual(finding.rule, "skill.listing")

    def test_claude_code_keys_warn_unless_portable_only(self) -> None:
        skill = self.make_skill(extra="effort: high\ncolour: red\n")
        found = {(f.message.split("'")[1], f.severity) for f in self.lint(skill, rules.skill_frontmatter)}
        self.assertEqual(found, {("effort", Severity.WARN), ("colour", Severity.ERROR)})
        self.write("w.toml", 'schema = 1\nkind = "skill"\nfrom = "widget-check"\ntargets = ["api"]\n')
        ctx = LintContext(load_subject(self.root / "w.toml"), ())
        severities = {f.severity for f in rules.skill_frontmatter(ctx)}
        self.assertEqual(severities, {Severity.ERROR})

    def test_metadata_must_be_strings_and_compatibility_capped(self) -> None:
        skill = self.make_skill(extra=f"compatibility: {'c' * 501}\nmetadata:\n  version: 1.0\n")
        rules_hit = sorted(f.rule for f in self.lint(skill, rules.skill_frontmatter))
        self.assertEqual(rules_hit, ["skill.compatibility", "skill.metadata"])

    def test_body_lines_and_tokens(self) -> None:
        skill = self.make_skill(body="x\n" * 501)
        self.assertEqual(len(self.lint(skill, rules.skill_body)), 1)
        big = self.make_skill(name="big", body="y" * 20_004)
        (finding,) = self.lint(big, rules.skill_body)
        self.assertIn("about 5001 tokens", finding.message)

    def test_refs_missing_backslash_toc_and_depth(self) -> None:
        body = ("[a](references/missing.md) [b](scripts\\run.py) [c](references/long.md) "
                "[d](https://x.y) [e](#top)\n```\n[f](not/checked.md)\n```\n")
        skill = self.make_skill(body=body)
        self.write("widget-check/references/long.md",
                   "# Long\n" + "line\n" * 120 + "[g](deeper.md)\n")
        self.write("widget-check/references/deeper.md", "# Deeper\n")
        messages = sorted(f.message for f in self.lint(skill, rules.skill_refs))
        self.assertEqual(len(messages), 4, messages)
        self.assertTrue(any("does not exist" in m for m in messages))
        self.assertTrue(any("backslashes" in m for m in messages))
        self.assertTrue(any("table of" in m for m in messages))
        self.assertTrue(any("one level deep" in m for m in messages))

    def test_bold_contents_counts_as_a_table_of_contents(self) -> None:
        skill = self.make_skill()
        self.write("widget-check/references/guide.md", "# Guide\n\n**Contents**\n" + "l\n" * 120)
        self.assertEqual(self.lint(skill, rules.skill_refs), [])

    def test_cases(self) -> None:
        skill = self.make_skill()
        (none,) = self.lint(skill, rules.skill_cases, cases=None)
        self.assertEqual(none.severity, Severity.INFO)
        (few,) = self.lint(skill, rules.skill_cases, cases=(Case("a", "x", (), ()),))
        self.assertEqual(few.severity, Severity.WARN)


class PromptRulesTest(TempDirTest):
    def prompt(self, sections: str, examples: int = 3) -> LintContext:
        example = '[[example]]\ninput = "a"\noutput = "b"\n' * examples
        path = self.write("p.prompt.toml",
                          f'schema = 1\nkind = "prompt"\nid = "p"\n{sections}{example}')
        return LintContext(load_subject(path), None)

    def test_examples_outside_range(self) -> None:
        self.assertEqual(list(rules.prompt_examples(self.prompt("", examples=3))), [])
        (finding,) = rules.prompt_examples(self.prompt("", examples=1))
        self.assertEqual(finding.severity, Severity.INFO)

    def test_tone_shouting_and_negatives(self) -> None:
        text = "Do not guess.\\nNever skip tests.\\nYou MUST cite sources.\\nAvoid jargon."
        ctx = self.prompt(f'[[section]]\nname = "rules"\ntext = "{text}"\n')
        messages = [f.message for f in rules.prompt_tone(ctx)]
        self.assertEqual(len(messages), 2)
        self.assertIn("MUST", messages[0])
        self.assertIn("3 of 4 lines", messages[1])

    def test_skill_rules_ignore_prompts_and_vice_versa(self) -> None:
        ctx = self.prompt("", examples=0)
        self.assertEqual(list(rules.skill_name(ctx)), [])
        skill_ctx = LintContext(load_subject(self.make_skill()), ())
        self.assertEqual(list(rules.prompt_tone(skill_ctx)), [])


if __name__ == "__main__":
    unittest.main()
