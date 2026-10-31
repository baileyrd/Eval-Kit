import unittest

from evalkit.frontmatter import FrontmatterError, parse


class ParseTest(unittest.TestCase):
    def test_plain_quoted_and_map_values(self) -> None:
        doc = parse('---\nname: a-b\nlicense: "MIT"\nmetadata:\n  version: "1.0"\n---\nbody\n')
        self.assertEqual(doc.fields, {"name": "a-b", "license": "MIT",
                                      "metadata": {"version": "1.0"}})
        self.assertEqual(doc.body, "body")
        self.assertEqual(doc.body_start_line, 7)
        self.assertEqual(doc.unquoted, frozenset())

    def test_folded_block_joins_lines(self) -> None:
        doc = parse("---\ndescription: >-\n  Does one thing.\n  Use when asked.\n---\n")
        self.assertEqual(doc.fields["description"], "Does one thing. Use when asked.")

    def test_literal_block_keeps_lines(self) -> None:
        doc = parse("---\nnote: |\n  one\n  two\n---\n")
        self.assertEqual(doc.fields["note"], "one\ntwo")

    def test_list_value(self) -> None:
        doc = parse("---\npaths:\n  - src/**\n  - docs/*\n---\n")
        self.assertEqual(doc.fields["paths"], ["src/**", "docs/*"])

    def test_unquoted_number_is_flagged(self) -> None:
        doc = parse("---\nmetadata:\n  version: 1.0\n  build: 1.0.3\n---\n")
        self.assertEqual(doc.unquoted, frozenset({"metadata.version"}))

    def test_single_quotes_and_comments(self) -> None:
        doc = parse("---\n# comment\nname: 'it''s' \nlicense: MIT # trailing\n---\n")
        self.assertEqual(doc.fields, {"name": "it's", "license": "MIT"})

    def test_rejects_missing_fences(self) -> None:
        for text in ("name: x\n", "---\nname: x\n"):
            with self.subTest(text=text), self.assertRaises(FrontmatterError):
                parse(text)

    def test_rejects_unsupported_yaml(self) -> None:
        cases = {
            "duplicate": "---\nname: a\nname: b\n---\n",
            "stray indent": "---\n  name: a\n---\n",
            "nested map": "---\nmetadata:\n  a:\n    b: c\n---\n",
            "inline and block": "---\nname: a\n  more\n---\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(FrontmatterError):
                parse(text)


if __name__ == "__main__":
    unittest.main()
