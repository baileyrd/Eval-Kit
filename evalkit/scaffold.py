"""``evalkit init``: write a starter eval suite next to a skill.

The cases are placeholders. Their inputs are marked TODO and carry a hint from
the skill's description, because a good case is a realistic request only the
author can write.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from evalkit.spec import SUITE_FILE, SkillSpec

CASE_COUNT = 3
_WHEN = re.compile(r"\b(Use (?:it |this )?when\b[^.]*\.?)", re.IGNORECASE)


def suite_path(skill: SkillSpec) -> Path:
    return skill.root / SUITE_FILE


def render_suite(skill: SkillSpec) -> str:
    """The text of a starter ``evals/suite.toml`` for *skill*."""
    hint = _hint(skill)
    lines = [
        "# evalkit suite. Each case is a realistic request; checks decide if it passed.",
        "# Replace every TODO, then: evalkit lint <skill dir>",
        "samples = 3",
        "",
        "[variant.control]            # the same run with no skill installed",
        "",
        "[variant.skill]",
        'subject = ".."               # this skill\'s directory',
    ]
    for number in range(1, CASE_COUNT + 1):
        lines += [
            "",
            "[[case]]",
            f'id     = "case-{number}"',
            f"input  = {_toml_string(f'TODO: a realistic request. Hint: {hint}')}",
            "checks = []                  # TODO, e.g. { regex = \"...\" }, { file_exists = \"out.md\" }",
        ]
    return "\n".join(lines) + "\n"


def write_suite(skill: SkillSpec, force: bool) -> Path:
    """Write the starter suite; refuse to overwrite one unless *force*."""
    path = suite_path(skill)
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_suite(skill), encoding="utf-8")
    return path


def _hint(skill: SkillSpec) -> str:
    description = skill.document.fields.get("description")
    if not isinstance(description, str) or not description:
        return "the skill has no description"
    match = _WHEN.search(description)
    if match:
        return match.group(1)
    return description.split(". ", 1)[0]


def _toml_string(text: str) -> str:
    # A JSON string is a valid TOML basic string for any text json.dumps emits.
    return json.dumps(text, ensure_ascii=False)
