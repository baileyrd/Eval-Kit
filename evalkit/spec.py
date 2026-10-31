"""Typed subjects (prompts and skills) and the TOML files that declare them.

Loading validates at the boundary: an unknown key, a wrong type or a path that
leaves the repository raises :class:`SpecError` with the file and the reason.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from evalkit import checks, frontmatter

SCHEMA = 1
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
TARGETS = frozenset({"claude-code", "claude.ai", "api"})
SKILL_FILE = "SKILL.md"
SUITE_FILE = Path("evals") / "suite.toml"

# Messages API fields that `claude -p` has no way to set. Rejected with a reason
# rather than as merely unknown, so the author knows why.
CLI_UNSETTABLE = frozenset(
    {"max_tokens", "thinking", "budget_tokens", "temperature", "top_p", "top_k",
     "stop_sequences", "tool_choice"}
)


class SpecError(ValueError):
    """A subject or suite file is malformed."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


@dataclass(frozen=True)
class Section:
    name: str
    text: str
    placement: Literal["system", "user"] = "system"


@dataclass(frozen=True)
class Example:
    input: str
    output: str


@dataclass(frozen=True)
class PromptSpec:
    """A prompt, limited to what the Claude CLI can apply."""

    path: Path
    id: str
    model: str | None
    effort: str | None
    tools: tuple[str, ...]
    sections: tuple[Section, ...]
    examples: tuple[Example, ...]
    template: str
    documents: tuple[str, ...]


@dataclass(frozen=True)
class Resource:
    path: str  # relative to the skill root, forward slashes
    text: str | None  # Markdown files only; None for scripts and assets


@dataclass(frozen=True)
class SkillSpec:
    """An existing skill directory, read as-is. SKILL.md is canonical."""

    root: Path
    targets: frozenset[str]
    document: frontmatter.Document
    resources: tuple[Resource, ...]
    model: str | None = None  # run-only [override] from a *.skill.toml
    effort: str | None = None

    @property
    def dir_name(self) -> str:
        return self.root.name

    @property
    def name(self) -> str:
        """The name Claude Code loads the skill under: its directory name."""
        return self.root.name


@dataclass(frozen=True)
class Case:
    id: str
    input: str
    files: tuple[tuple[str, str], ...]
    checks: tuple[checks.Check, ...]


Subject = PromptSpec | SkillSpec


@dataclass(frozen=True)
class Variant:
    """A subject plus run settings; no subject is the control.

    Model and effort resolve as variant, then subject, then the CLI default (None).
    """

    id: str
    subject: Subject | None
    model: str | None
    effort: str | None

    @property
    def skill(self) -> SkillSpec | None:
        return self.subject if isinstance(self.subject, SkillSpec) else None


@dataclass(frozen=True)
class JudgeConfig:
    """The model that grades ``judge`` checks. Defaults to a model unlikely to be
    the one under test, since a judge tends to favour its own model's output."""

    model: str = "claude-sonnet-5-5"
    effort: str = "low"


@dataclass(frozen=True)
class Suite:
    path: Path
    samples: int
    allowed_tools: tuple[str, ...]  # pre-approved for every variant, so pairs stay fair
    timeout_s: int
    variants: tuple[Variant, ...]
    cases: tuple[Case, ...]
    judge: JudgeConfig = JudgeConfig()

    def variant(self, variant_id: str) -> Variant:
        for variant in self.variants:
            if variant.id == variant_id:
                return variant
        known = ", ".join(v.id for v in self.variants)
        raise SpecError(self.path, f"no variant {variant_id!r}; variants: {known}")


MAX_SAMPLES = 20
DEFAULT_TIMEOUT_S = 600


def load_subject(path: Path) -> Subject:
    """Load a skill directory, a ``*.skill.toml`` or a ``*.prompt.toml``."""
    if path.is_dir():
        return load_skill_dir(path, frozenset({"claude-code"}))
    data = _read_toml(path)
    kind = data.get("kind")
    if kind == "skill":
        return _skill_from_toml(path, data)
    if kind == "prompt":
        return _prompt_from_toml(path, data)
    raise SpecError(path, f"kind must be 'skill' or 'prompt', got {kind!r}")


def load_skill_dir(root: Path, targets: frozenset[str]) -> SkillSpec:
    skill_md = root / SKILL_FILE
    if not skill_md.is_file():
        raise SpecError(root, f"no {SKILL_FILE}")
    try:
        document = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
    except frontmatter.FrontmatterError as exc:
        raise SpecError(skill_md, str(exc)) from None
    return SkillSpec(root.resolve(), targets, document, _resources(root))


def load_suite(path: Path) -> Suite:
    """Read a whole suite: settings, variants and cases."""
    data = _read_toml(path)
    _check_keys(path, data, {"samples", "allowed_tools", "timeout", "judge", "variant", "case"})
    samples = data.get("samples", 1)
    if not isinstance(samples, int) or not 1 <= samples <= MAX_SAMPLES:
        raise SpecError(path, f"samples must be an integer from 1 to {MAX_SAMPLES}")
    timeout = data.get("timeout", DEFAULT_TIMEOUT_S)
    if not isinstance(timeout, int) or timeout <= 0:
        raise SpecError(path, "timeout must be a positive number of seconds")
    raw_variants = data.get("variant", {})
    if not isinstance(raw_variants, dict) or not raw_variants:
        raise SpecError(path, "define at least one [variant.<id>] table")
    variants = tuple(_variant(path, vid, raw) for vid, raw in raw_variants.items())
    return Suite(path, samples, tuple(_str_list(path, data, "allowed_tools", default=[])),
                 timeout, variants, _cases(path, data), _judge(path, data.get("judge", {})))


def _judge(path: Path, raw: object) -> JudgeConfig:
    if not isinstance(raw, dict):
        raise SpecError(path, "[judge] must be a table")
    _check_keys(path, raw, {"model", "effort"}, where="[judge]")
    _check_effort(path, raw.get("effort"))
    default = JudgeConfig()
    return JudgeConfig(_optional_str(path, raw, "model") or default.model,
                       _optional_str(path, raw, "effort") or default.effort)


def load_cases(path: Path) -> tuple[Case, ...]:
    """Read only the ``[[case]]`` tables of a suite file."""
    return _cases(path, _read_toml(path))


def _variant(path: Path, variant_id: str, raw: object) -> Variant:
    if not isinstance(raw, dict):
        raise SpecError(path, f"[variant.{variant_id}] must be a table")
    _check_keys(path, raw, {"subject", "model", "effort"}, where=f"[variant.{variant_id}]")
    _check_effort(path, raw.get("effort"))
    source = _optional_str(path, raw, "subject")
    # A subject usually lives in another repository, so it is not confined.
    subject = None if source is None else load_subject((path.parent / source).resolve())
    return Variant(
        id=variant_id,
        subject=subject,
        model=_optional_str(path, raw, "model") or (subject.model if subject else None),
        effort=_optional_str(path, raw, "effort") or (subject.effort if subject else None),
    )


def _cases(path: Path, data: dict[str, object]) -> tuple[Case, ...]:
    raw_cases = data.get("case", [])
    if not isinstance(raw_cases, list):
        raise SpecError(path, "'case' must be an array of tables ([[case]])")
    cases = tuple(_case(path, raw) for raw in raw_cases)
    if not cases:
        raise SpecError(path, "define at least one [[case]]")
    ids = [case.id for case in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SpecError(path, f"duplicate case ids: {', '.join(duplicates)}")
    return cases


def _skill_from_toml(path: Path, data: dict[str, object]) -> SkillSpec:
    _check_keys(path, data, {"schema", "kind", "from", "targets", "override"})
    _check_schema(path, data)
    targets = frozenset(_str_list(path, data, "targets", default=["claude-code"]))
    unknown = targets - TARGETS
    if unknown:
        raise SpecError(path, f"unknown targets {sorted(unknown)}; use {sorted(TARGETS)}")
    override = data.get("override", {})
    if not isinstance(override, dict):
        raise SpecError(path, "'override' must be a table")
    _check_keys(path, override, {"model", "effort"}, where="[override]")
    _check_effort(path, override.get("effort"))
    source = _required_str(path, data, "from")
    # A skill usually lives in another repository, so `from` is not confined.
    skill = load_skill_dir((path.parent / source).resolve(), targets)
    return replace(skill, model=_optional_str(path, override, "model"),
                   effort=_optional_str(path, override, "effort"))


def _prompt_from_toml(path: Path, data: dict[str, object]) -> PromptSpec:
    _check_keys(path, data, {"schema", "kind", "id", "model", "effort", "tools",
                             "section", "example", "user"})
    _check_schema(path, data)
    _check_effort(path, data.get("effort"))
    user = data.get("user", {})
    if not isinstance(user, dict):
        raise SpecError(path, "'user' must be a table")
    _check_keys(path, user, {"template", "documents"}, where="[user]")
    template = user.get("template", "{{input}}")
    if not isinstance(template, str) or "{{input}}" not in template:
        raise SpecError(path, "[user].template must be a string containing {{input}}")
    return PromptSpec(
        path=path,
        id=_required_str(path, data, "id"),
        model=_optional_str(path, data, "model"),
        effort=_optional_str(path, data, "effort"),
        tools=tuple(_str_list(path, data, "tools", default=[])),
        sections=tuple(_section(path, raw) for raw in _tables(path, data, "section")),
        examples=tuple(_example(path, raw) for raw in _tables(path, data, "example")),
        template=template,
        documents=tuple(_text(path, d) for d in _str_list(path, user, "documents", default=[])),
    )


def _section(path: Path, raw: dict[str, object]) -> Section:
    _check_keys(path, raw, {"name", "text", "in"}, where="[[section]]")
    placement = raw.get("in", "system")
    if placement not in ("system", "user"):
        raise SpecError(path, f"[[section]] in must be 'system' or 'user', got {placement!r}")
    return Section(_required_str(path, raw, "name"), _text(path, _required_str(path, raw, "text")),
                   placement)


def _example(path: Path, raw: dict[str, object]) -> Example:
    _check_keys(path, raw, {"input", "output"}, where="[[example]]")
    return Example(_text(path, _required_str(path, raw, "input")),
                   _text(path, _required_str(path, raw, "output")))


def _case(path: Path, raw: object) -> Case:
    if not isinstance(raw, dict):
        raise SpecError(path, "each [[case]] must be a table")
    _check_keys(path, raw, {"id", "input", "files", "checks"}, where="[[case]]")
    files = raw.get("files", {})
    if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
        raise SpecError(path, "[[case]] files must map paths to strings")
    raw_checks = raw.get("checks", [])
    if not isinstance(raw_checks, list) or not all(isinstance(c, dict) for c in raw_checks):
        raise SpecError(path, "[[case]] checks must be an array of inline tables")
    case_id = _required_str(path, raw, "id")
    try:
        parsed = tuple(checks.parse(c) for c in raw_checks)
    except ValueError as exc:
        raise SpecError(path, f"case {case_id!r}: {exc}") from None
    files = {name: _text(path, text) for name, text in files.items()}
    return Case(case_id, _required_str(path, raw, "input"), tuple(sorted(files.items())), parsed)


def _resources(root: Path) -> tuple[Resource, ...]:
    out: list[Resource] = []
    for file in sorted(root.rglob("*")):
        rel = file.relative_to(root)
        if not file.is_file() or rel.as_posix() == SKILL_FILE:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        text = file.read_text(encoding="utf-8", errors="replace") if file.suffix == ".md" else None
        out.append(Resource(rel.as_posix(), text))
    return tuple(out)


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise SpecError(path, f"cannot read: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(path, f"invalid TOML: {exc}") from None


def _check_keys(path: Path, data: dict[str, object], allowed: set[str], where: str = "") -> None:
    for key in data:
        if key in CLI_UNSETTABLE:
            raise SpecError(path, f"{where or 'top level'}: {key!r} cannot be set through the "
                                  "claude CLI, which is how evalkit runs prompts")
        if key not in allowed:
            raise SpecError(path, f"{where or 'top level'}: unknown key {key!r}; "
                                  f"allowed: {', '.join(sorted(allowed))}")


def _check_schema(path: Path, data: dict[str, object]) -> None:
    if data.get("schema") != SCHEMA:
        raise SpecError(path, f"schema must be {SCHEMA}, got {data.get('schema')!r}")


def _check_effort(path: Path, effort: object) -> None:
    if effort is not None and effort not in EFFORTS:
        raise SpecError(path, f"effort must be one of {sorted(EFFORTS)}, got {effort!r}")


def _required_str(path: Path, data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecError(path, f"{key!r} is required and must be a non-empty string")
    return value


def _optional_str(path: Path, data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise SpecError(path, f"{key!r} must be a string")
    return value


def _str_list(path: Path, data: dict[str, object], key: str, default: list[str]) -> list[str]:
    value = data.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SpecError(path, f"{key!r} must be an array of strings")
    return value


def _tables(path: Path, data: dict[str, object], key: str) -> list[dict[str, object]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise SpecError(path, f"{key!r} must be an array of tables ([[{key}]])")
    return value


def _text(path: Path, value: str) -> str:
    """Inline text, or the contents of ``file:<relative path>``."""
    if not value.startswith("file:"):
        return value
    target = _resolve(path, value.removeprefix("file:"))
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(path, f"cannot read {value}: {exc.strerror}") from None


def _resolve(path: Path, relative: str) -> Path:
    """Resolve *relative* against the TOML file's directory, inside its repository."""
    target = (path.parent / relative).resolve()
    root = _repo_root(path.parent.resolve())
    if not target.is_relative_to(root):
        raise SpecError(path, f"{relative!r} resolves outside {root}")
    return target


def _repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start
