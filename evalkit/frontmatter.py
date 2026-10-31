"""Read SKILL.md frontmatter without a YAML dependency.

Skill frontmatter uses a small, regular subset of YAML: top-level ``key: value``
pairs, folded (``>``, ``>-``) or literal (``|``, ``|-``) block scalars, and
one-level maps (``metadata:``) or lists (``paths:``). This module parses
exactly that subset and raises :class:`FrontmatterError` on anything else
rather than guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

Value = str | dict[str, str] | list[str]

_KEY = re.compile(r"^([A-Za-z_][\w-]*):(?:\s+(.*?))?\s*$")
_BLOCK = re.compile(r"^[>|][-+]?$")
# Plain scalars YAML would load as something other than a string.
_NON_STRING = re.compile(
    r"^(?:[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|true|false|yes|no|on|off|null|~)$",
    re.IGNORECASE,
)


class FrontmatterError(ValueError):
    """The file has no frontmatter, or uses YAML outside the supported subset."""


@dataclass(frozen=True)
class Document:
    """A SKILL.md split into its parts."""

    fields: dict[str, Value]
    unquoted: frozenset[str]  # dotted keys whose plain value YAML reads as non-string
    body: str
    body_start_line: int  # 1-based line number where the body begins


def parse(text: str) -> Document:
    """Split *text* into frontmatter fields and body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("file must start with a '---' frontmatter fence")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise FrontmatterError("frontmatter has no closing '---' fence") from None
    fields, unquoted = _parse_block(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    return Document(fields, frozenset(unquoted), body, end + 2)


def is_non_string(raw: str) -> bool:
    """True when a plain (unquoted) scalar would load as a number, bool or null."""
    return bool(_NON_STRING.match(raw))


def _parse_block(lines: list[str]) -> tuple[dict[str, Value], set[str]]:
    fields: dict[str, Value] = {}
    unquoted: set[str] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0].isspace():
            raise FrontmatterError(f"unexpected indentation: {line.strip()!r}")
        match = _KEY.match(line)
        if not match:
            raise FrontmatterError(f"expected 'key: value', got {line.strip()!r}")
        key, raw = match.group(1), match.group(2) or ""
        if key in fields:
            raise FrontmatterError(f"duplicate key {key!r}")
        child, i = _indented(lines, i + 1)
        fields[key] = _value(key, raw, child, unquoted)
    return fields, unquoted


def _indented(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect the indented (or blank) lines that belong to the previous key."""
    end = start
    while end < len(lines) and (not lines[end].strip() or lines[end][0].isspace()):
        end += 1
    return lines[start:end], end


def _value(key: str, raw: str, child: list[str], unquoted: set[str]) -> Value:
    if _BLOCK.match(raw):
        return _block_scalar(raw[0], child)
    if raw:
        if any(line.strip() for line in child):
            raise FrontmatterError(f"{key!r} has both an inline value and indented lines")
        if is_non_string(raw):
            unquoted.add(key)
        return _scalar(raw)
    items = [line.strip() for line in child if line.strip()]
    if not items:
        return ""
    if all(item.startswith("- ") for item in items):
        return [_scalar(item[2:].strip()) for item in items]
    return _map(key, items, unquoted)


def _map(parent: str, items: list[str], unquoted: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        match = _KEY.match(item)
        if not match or not match.group(2):
            raise FrontmatterError(f"unsupported value under {parent!r}: {item!r}")
        sub, raw = match.group(1), match.group(2)
        if is_non_string(raw):
            unquoted.add(f"{parent}.{sub}")
        out[sub] = _scalar(raw)
    return out


def _block_scalar(style: str, child: list[str]) -> str:
    text = [line.strip() for line in child]
    while text and not text[-1]:
        text.pop()
    if style == "|":
        return "\n".join(text)
    return re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(text)).replace("\n\n", "\n")


def _scalar(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value: str = json.loads(raw)  # YAML double-quoted escapes are a JSON superset
            return value
        except json.JSONDecodeError:
            raise FrontmatterError(f"unsupported escape in {raw!r}") from None
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw.split(" #", 1)[0].rstrip()
