import unittest

from evalkit.spec import PromptSpec, SkillSpec, SpecError, load_cases, load_subject
from tests.helpers import TempDirTest

PROMPT = """\
schema = 1
kind   = "prompt"
id     = "reviewer"
model  = "claude-opus-5-5"
effort = "high"
tools  = []

[[section]]
name = "role"
text = "You are a reviewer."

[[section]]
name = "instructions"
text = "file:instructions.md"
in   = "user"

[[example]]
input  = "a"
output = "b"

[user]
documents = ["file:doc.txt"]
template  = "Review: {{input}}"
"""


class PromptSpecTest(TempDirTest):
    def test_loads_sections_examples_and_files(self) -> None:
        self.write("instructions.md", "Find bugs.")
        self.write("doc.txt", "diff")
        spec = load_subject(self.write("r.prompt.toml", PROMPT))
        assert isinstance(spec, PromptSpec)
        self.assertEqual([s.name for s in spec.sections], ["role", "instructions"])
        self.assertEqual(spec.sections[1].text, "Find bugs.")
        self.assertEqual(spec.sections[1].placement, "user")
        self.assertEqual(spec.documents, ("diff",))
        self.assertEqual(spec.tools, ())

    def test_rejects_api_fields_the_cli_cannot_set(self) -> None:
        path = self.write("p.toml", 'schema = 1\nkind = "prompt"\nid = "x"\nmax_tokens = 10\n')
        with self.assertRaisesRegex(SpecError, "cannot be set through the claude CLI"):
            load_subject(path)

    def test_rejects_unknown_keys_bad_effort_and_bad_template(self) -> None:
        cases = {
            "unknown": 'colour = "red"',
            "effort": 'effort = "extreme"',
            "template": '[user]\ntemplate = "no placeholder"',
            "placement": '[[section]]\nname = "a"\ntext = "b"\nin = "both"',
        }
        for label, extra in cases.items():
            with self.subTest(label):
                path = self.write("p.toml", f'schema = 1\nkind = "prompt"\nid = "x"\n{extra}\n')
                with self.assertRaises(SpecError):
                    load_subject(path)

    def test_file_reference_cannot_leave_the_repository(self) -> None:
        path = self.write("p.toml", 'schema = 1\nkind = "prompt"\nid = "x"\n'
                                    '[[section]]\nname = "a"\ntext = "file:../../etc/passwd"\n')
        with self.assertRaisesRegex(SpecError, "outside"):
            load_subject(path)


class SkillSpecTest(TempDirTest):
    def test_loads_directory_and_resources(self) -> None:
        spec = load_subject(self.make_skill())
        assert isinstance(spec, SkillSpec)
        self.assertEqual(spec.dir_name, "widget-check")
        self.assertEqual(spec.targets, frozenset({"claude-code"}))
        self.assertEqual([r.path for r in spec.resources], ["references/guide.md"])

    def test_skill_toml_imports_and_sets_targets(self) -> None:
        self.make_skill()
        path = self.write("subjects/w.skill.toml",
                          'schema = 1\nkind = "skill"\nfrom = "../widget-check"\n'
                          'targets = ["claude.ai"]\n[override]\neffort = "low"\n')
        spec = load_subject(path)
        assert isinstance(spec, SkillSpec)
        self.assertEqual(spec.targets, frozenset({"claude.ai"}))

    def test_skill_toml_rejects_unknown_target(self) -> None:
        self.make_skill()
        path = self.write("w.toml", 'schema = 1\nkind = "skill"\nfrom = "widget-check"\n'
                                    'targets = ["web"]\n')
        with self.assertRaisesRegex(SpecError, "unknown targets"):
            load_subject(path)

    def test_missing_skill_md_and_bad_frontmatter(self) -> None:
        (self.root / "empty").mkdir()
        with self.assertRaisesRegex(SpecError, "no SKILL.md"):
            load_subject(self.root / "empty")
        self.write("broken/SKILL.md", "no frontmatter")
        with self.assertRaisesRegex(SpecError, "frontmatter"):
            load_subject(self.root / "broken")

    def test_wrong_schema_or_kind(self) -> None:
        for text in ('schema = 2\nkind = "skill"\nfrom = "."\n', 'schema = 1\nkind = "agent"\n'):
            with self.subTest(text=text), self.assertRaises(SpecError):
                load_subject(self.write("x.toml", text))


class CasesTest(TempDirTest):
    def test_loads_cases(self) -> None:
        path = self.write("suite.toml", '[[case]]\nid = "a"\ninput = "hi"\n'
                                        'files = { "x.txt" = "y" }\nchecks = [{ regex = "h" }]\n')
        (case,) = load_cases(path)
        self.assertEqual((case.id, case.input, case.files), ("a", "hi", (("x.txt", "y"),)))
        self.assertEqual(case.checks, ({"regex": "h"},))

    def test_rejects_duplicate_ids_and_missing_input(self) -> None:
        dup = self.write("d.toml", '[[case]]\nid = "a"\ninput = "x"\n[[case]]\nid = "a"\ninput = "y"\n')
        with self.assertRaisesRegex(SpecError, "duplicate case ids: a"):
            load_cases(dup)
        empty = self.write("e.toml", '[[case]]\nid = "a"\n')
        with self.assertRaisesRegex(SpecError, "'input' is required"):
            load_cases(empty)


if __name__ == "__main__":
    unittest.main()
