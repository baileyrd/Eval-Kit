"""Lint rules, one function each, with the guidance they enforce.

Every limit below is taken from the page named in its ``source`` and was last
checked against that page on :data:`CHECKED_ON`. When the docs change, change
the constant and the date together.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from urllib.parse import unquote

from evalkit.lint import Finding, LintContext, Rule, Severity
from evalkit.spec import PromptSpec, Resource, SkillSpec

CHECKED_ON = "2026-10-31"
SPEC = "https://agentskills.io/specification.md"
CLAUDE_CODE = "https://code.claude.com/docs/en/skills"
AUTHORING = "https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices"
PROMPTING = ("https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/"
             "claude-prompting-best-practices")

NAME_MAX = 64                # SPEC
DESCRIPTION_MAX = 1024       # SPEC
COMPATIBILITY_MAX = 500      # SPEC
LISTING_MAX = 1536           # CLAUDE_CODE: description + when_to_use in the skill listing
BODY_LINES_MAX = 500         # SPEC, AUTHORING
BODY_TOKENS_MAX = 5000       # SPEC: "< 5000 tokens recommended"
CHARS_PER_TOKEN = 4          # rough estimate; the stdlib has no tokenizer
TOC_AFTER_LINES = 100        # AUTHORING: table of contents past 100 lines
MIN_CASES = 3                # AUTHORING: "at least three evaluations"
EXAMPLES_RANGE = (3, 5)      # PROMPTING: 3–5 examples
RESERVED_WORDS = ("anthropic", "claude")  # AUTHORING

PORTABLE_KEYS = frozenset({"name", "description", "license", "compatibility", "metadata",
                           "allowed-tools"})
CLAUDE_CODE_KEYS = frozenset({"when_to_use", "argument-hint", "arguments",
                              "disable-model-invocation", "user-invocable", "disallowed-tools",
                              "model", "effort", "context", "agent", "background", "hooks",
                              "paths", "shell"})

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_XML_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
_FIRST_OR_SECOND_PERSON = re.compile(r"^\s*(?:I|I'm|I can|We|You|You can|Your)\b")
_WHEN = re.compile(
    r"\b(?:when|whenever|use (?:it |this )?(?:for|to|on)|triggers? on|activates? on"
    r"|invoked? (?:on|for|after))\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# A "Contents" heading, or a bold "**Contents**" line used as one.
_CONTENTS = re.compile(r"^(?:#{1,6}\s+|\*\*|__)(?:table of )?contents\b",
                       re.IGNORECASE | re.MULTILINE)
_SHOUT = re.compile(r"\b(?:MUST|NEVER|ALWAYS|CRITICAL|IMPORTANT)\b")
_NEGATIVE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:do not|don't|never|avoid)\b", re.IGNORECASE)


def _finding(rule: str, severity: Severity, location: str, message: str, source: str) -> Finding:
    return Finding(rule, severity, location, message, source)


def _skill_md(skill: SkillSpec) -> str:
    return str(skill.root / "SKILL.md")


# --- skill rules -------------------------------------------------------------

def skill_name(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    where = _skill_md(skill)
    name = skill.document.fields.get("name")
    if not isinstance(name, str) or not name:
        yield _finding("skill.name", Severity.ERROR, where, "'name' is required", SPEC)
        return
    if len(name) > NAME_MAX or not _NAME.match(name):
        yield _finding("skill.name", Severity.ERROR, where,
                       f"'{name}' must be 1-{NAME_MAX} of [a-z0-9-], with no leading, trailing "
                       "or double hyphen", SPEC)
    if name != skill.dir_name:
        yield _finding("skill.name", Severity.ERROR, where,
                       f"'{name}' does not match its directory '{skill.dir_name}'", SPEC)
    for word in RESERVED_WORDS:
        if word in name:
            yield _finding("skill.name", Severity.ERROR, where,
                           f"'{name}' contains the reserved word '{word}'", AUTHORING)


def skill_description(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    where = _skill_md(skill)
    text = skill.document.fields.get("description")
    if not isinstance(text, str) or not text.strip():
        yield _finding("skill.description.limits", Severity.ERROR, where,
                       "'description' is required and must be a non-empty string", SPEC)
        return
    if len(text) > DESCRIPTION_MAX:
        yield _finding("skill.description.limits", Severity.ERROR, where,
                       f"description is {len(text)} characters; the limit is {DESCRIPTION_MAX}",
                       SPEC)
    if _XML_TAG.search(text):
        yield _finding("skill.description.limits", Severity.ERROR, where,
                       "description contains an XML tag", AUTHORING)
    if _FIRST_OR_SECOND_PERSON.match(text):
        yield _finding("skill.description.style", Severity.WARN, where,
                       "description should be in the third person ('Processes ...', not "
                       "'I can ...' or 'You can ...')", AUTHORING)
    if not _WHEN.search(text):
        yield _finding("skill.description.style", Severity.WARN, where,
                       "description says what the skill does but not when to use it "
                       "(add 'Use when ...')", AUTHORING)


def skill_listing(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    fields = skill.document.fields
    total = sum(len(v) for k in ("description", "when_to_use")
                if isinstance(v := fields.get(k), str))
    if total > LISTING_MAX:
        yield _finding("skill.listing", Severity.WARN, _skill_md(skill),
                       f"description + when_to_use is {total} characters; Claude Code cuts the "
                       f"listing at {LISTING_MAX}", CLAUDE_CODE)


def skill_frontmatter(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    where = _skill_md(skill)
    fields = skill.document.fields
    portable_only = "claude-code" not in skill.targets
    for key in sorted(set(fields) - PORTABLE_KEYS):
        if key in CLAUDE_CODE_KEYS:
            severity = Severity.ERROR if portable_only else Severity.WARN
            yield _finding("skill.portable", severity, where,
                           f"'{key}' works only in Claude Code; claude.ai uploads and the "
                           "Skills API reject it", CLAUDE_CODE)
        else:
            yield _finding("skill.portable", Severity.ERROR, where,
                           f"unknown frontmatter key '{key}'", SPEC)
    compatibility = fields.get("compatibility")
    if isinstance(compatibility, str) and len(compatibility) > COMPATIBILITY_MAX:
        yield _finding("skill.compatibility", Severity.ERROR, where,
                       f"compatibility is {len(compatibility)} characters; the limit is "
                       f"{COMPATIBILITY_MAX}", SPEC)
    metadata = fields.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        yield _finding("skill.metadata", Severity.ERROR, where,
                       "metadata must be a map of string keys to string values", SPEC)
    for key in sorted(k for k in skill.document.unquoted if k.startswith("metadata.")):
        yield _finding("skill.metadata", Severity.WARN, where,
                       f"'{key}' is unquoted and YAML reads it as a number, boolean or null; "
                       "metadata values must be strings, so quote it", SPEC)


def skill_body(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    where = _skill_md(skill)
    body = skill.document.body
    lines = len(body.splitlines())
    if lines > BODY_LINES_MAX:
        yield _finding("skill.body", Severity.WARN, where,
                       f"body is {lines} lines; keep it under {BODY_LINES_MAX} and move detail "
                       "into referenced files", SPEC)
    tokens = math.ceil(len(body) / CHARS_PER_TOKEN)
    if tokens > BODY_TOKENS_MAX:
        yield _finding("skill.body", Severity.WARN, where,
                       f"body is about {tokens} tokens (estimated as characters / "
                       f"{CHARS_PER_TOKEN}); keep it under {BODY_TOKENS_MAX}", SPEC)


def skill_refs(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    where = _skill_md(skill)
    by_path = {resource.path: resource for resource in skill.resources}
    direct = set()
    for target in _local_links(skill.document.body):
        if "\\" in target:
            yield _finding("skill.paths", Severity.WARN, where,
                           f"link '{target}' uses backslashes; use forward slashes", AUTHORING)
            continue
        path = _normalise(target)
        if path is None or path == "SKILL.md":
            continue
        if path not in by_path:
            yield _finding("skill.refs", Severity.ERROR, where,
                           f"link '{target}' points at a file that does not exist", AUTHORING)
            continue
        direct.add(path)
    for path in sorted(direct):
        yield from _reference_findings(skill, by_path[path], direct)


def _reference_findings(skill: SkillSpec, resource: Resource, direct: set[str]) -> Iterator[Finding]:
    if resource.text is None:
        return
    where = str(skill.root / resource.path)
    if len(resource.text.splitlines()) > TOC_AFTER_LINES and not _CONTENTS.search(resource.text):
        yield _finding("skill.refs", Severity.WARN, where,
                       f"over {TOC_AFTER_LINES} lines with no 'Contents' heading; add a table of "
                       "contents at the top", AUTHORING)
    base = PurePosixPath(resource.path).parent
    for target in _local_links(resource.text):
        path = _normalise(str(base / target))
        if path and path.endswith(".md") and path not in direct and path != "SKILL.md":
            yield _finding("skill.refs", Severity.WARN, where,
                           f"links on to '{path}', which SKILL.md does not link; keep references "
                           "one level deep", AUTHORING)


def skill_cases(ctx: LintContext) -> Iterator[Finding]:
    skill = ctx.subject
    if not isinstance(skill, SkillSpec):
        return
    where = str(skill.root)
    if ctx.cases is None:
        yield _finding("skill.cases", Severity.INFO, where,
                       "no eval suite found; run 'evalkit init' to start one", AUTHORING)
    elif len(ctx.cases) < MIN_CASES:
        yield _finding("skill.cases", Severity.WARN, where,
                       f"{len(ctx.cases)} eval case(s); write at least {MIN_CASES}", AUTHORING)
    unchecked = [case.id for case in ctx.cases or () if not case.checks]
    if unchecked:
        yield _finding("skill.cases", Severity.WARN, where,
                       f"case(s) with no checks pass vacuously: {', '.join(unchecked)}", AUTHORING)


# --- prompt rules ------------------------------------------------------------

def prompt_examples(ctx: LintContext) -> Iterator[Finding]:
    prompt = ctx.subject
    if not isinstance(prompt, PromptSpec):
        return
    low, high = EXAMPLES_RANGE
    count = len(prompt.examples)
    if not low <= count <= high:
        yield _finding("prompt.examples", Severity.INFO, str(prompt.path),
                       f"{count} example(s); {low}-{high} relevant, varied examples work best "
                       "when the output's style matters", PROMPTING)


def prompt_tone(ctx: LintContext) -> Iterator[Finding]:
    prompt = ctx.subject
    if not isinstance(prompt, PromptSpec):
        return
    for section in prompt.sections:
        where = f"{prompt.path} [section {section.name}]"
        shouts = sorted(set(_SHOUT.findall(section.text)))
        if shouts:
            yield _finding("prompt.tone", Severity.INFO, where,
                           f"emphasis in capitals ({', '.join(shouts)}); current models follow "
                           "plain instructions and over-apply shouted ones", PROMPTING)
        rules = [line for line in section.text.splitlines() if line.strip()]
        negatives = sum(1 for line in rules if _NEGATIVE.match(line))
        if len(rules) >= 3 and negatives * 2 >= len(rules):
            yield _finding("prompt.tone", Severity.INFO, where,
                           f"{negatives} of {len(rules)} lines say what not to do; say what to do "
                           "instead", PROMPTING)


# --- helpers -----------------------------------------------------------------

def _local_links(markdown: str) -> Iterator[str]:
    for match in _LINK.finditer(_strip_code(markdown)):
        target = match.group(1)
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        yield target


def _strip_code(markdown: str) -> str:
    """Drop fenced code blocks, where link syntax is example text."""
    return re.sub(r"^(```|~~~).*?^\1", "", markdown, flags=re.MULTILINE | re.DOTALL)


def _normalise(target: str) -> str | None:
    """A link target as a skill-relative POSIX path, or None if it leaves the skill."""
    parts: list[str] = []
    for part in unquote(target.split("#", 1)[0]).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts) or None


RULES: tuple[Rule, ...] = (
    skill_name, skill_description, skill_listing, skill_frontmatter, skill_body, skill_refs,
    skill_cases, prompt_examples, prompt_tone,
)
