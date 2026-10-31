"""Builders for throwaway skills and subject files in a temp directory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

DESCRIPTION = "Processes widget files and reports defects. Use when the user mentions widgets."


def skill_md(name: str = "widget-check", description: str = DESCRIPTION, extra: str = "",
             body: str = "# Widget check\n\nSee [the guide](references/guide.md).\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


class TempDirTest(unittest.TestCase):
    """A test case with ``self.root``, a fresh git-rooted temp directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".git").mkdir()  # marks the repository root for path checks

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_skill(self, name: str = "widget-check", **kwargs: str) -> Path:
        self.write(f"{name}/SKILL.md", skill_md(name=name, **kwargs))
        self.write(f"{name}/references/guide.md", "# Guide\n")
        return self.root / name
