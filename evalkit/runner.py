"""Run a job through ``claude -p`` in an isolated temporary project.

Isolation: each run gets a fresh ``CLAUDE_CONFIG_DIR``, so no user skills,
CLAUDE.md, plugins or settings leak in. The login comes from
``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``) or, failing that, a copy of
the current ``.credentials.json``. ``ANTHROPIC_API_KEY`` is removed from the
child's environment, so a run never bills the API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from evalkit import checkout, render
from evalkit.spec import Case, JudgeConfig, PromptSpec, Suite, Variant
from evalkit.trace import ToolCall, Trace

CLAUDE = "claude"
MAX_FILE_BYTES = 200_000  # larger outputs are recorded by name only
_SKIP_IN_PROJECT = (".claude", ".git")


class RunError(RuntimeError):
    """A run failed or was not isolated; nothing is stored for it."""


@dataclass(frozen=True)
class Parsed:
    """The parts of a stream-json transcript a trace needs."""

    loaded_skills: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    final_text: str
    is_error: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int


def cli_version() -> str:
    try:
        done = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=30,
                              check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunError(f"cannot run '{CLAUDE} --version': {exc}") from None
    return done.stdout.strip()


_BASE_ARGV = (CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
              "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence")


def command(variant: Variant, system_file: Path | None, allowed_tools: Iterable[str]) -> list[str]:
    """The argv for one run; the user turn goes on stdin. Pure, for testing."""
    argv = [*_BASE_ARGV, *_model_args(variant.model, variant.effort)]
    tools = list(allowed_tools)
    if isinstance(variant.subject, PromptSpec):
        assert system_file is not None
        argv += ["--system-prompt-file", str(system_file), "--tools", ",".join(variant.subject.tools)]
        return argv + (["--allowed-tools", ",".join(tools)] if tools else [])
    argv += ["--permission-mode", "acceptEdits"]
    if tools:
        argv += ["--allowed-tools", ",".join(tools)]
    return argv


def judge_command(judge: JudgeConfig, system_file: Path) -> list[str]:
    """The argv for one judge call: a plain prompt with no tools."""
    return [*_BASE_ARGV, *_model_args(judge.model, judge.effort),
            "--system-prompt-file", str(system_file), "--tools", ""]


def _model_args(model: str | None, effort: str | None) -> list[str]:
    return [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]


def parse_stream(lines: Iterable[str]) -> Parsed:
    """Read the init event, tool calls and the result event from stream-json."""
    skills: tuple[str, ...] = ()
    calls: list[ToolCall] = []
    result: Mapping[str, object] | None = None
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            skills = tuple(str(s) for s in event.get("skills", []))
        elif kind == "assistant":
            calls += [ToolCall(str(b["name"]), json.dumps(b.get("input", {}), sort_keys=True))
                      for b in event["message"]["content"] if b.get("type") == "tool_use"]
        elif kind == "result":
            result = event
    if result is None:
        raise RunError("the CLI ended without a result event")
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return Parsed(
        loaded_skills=skills,
        tool_calls=tuple(calls),
        final_text=str(result.get("result") or ""),
        is_error=bool(result.get("is_error")) or result.get("subtype") != "success",
        input_tokens=sum(int(usage.get(k) or 0) for k in
                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(str(result.get("total_cost_usd") or 0)),
        duration_ms=int(str(result.get("duration_ms") or 0)),
    )


def check_isolation(variant: Variant, loaded: Iterable[str], suite_skills: Iterable[str]) -> None:
    """Fail unless exactly the variant's own skill, of the suite's skills, was loaded."""
    loaded = set(loaded)
    own = variant.skill.name if variant.skill else None
    if own is not None and own not in loaded:
        raise RunError(f"variant {variant.id!r}: skill {own!r} was not loaded")
    leaked = sorted((set(suite_skills) & loaded) - {own})
    if leaked:
        raise RunError(f"variant {variant.id!r}: run also loaded {', '.join(leaked)}; "
                       "a skill under test is installed outside the run")


class CliRunner:
    """Runs jobs with the Claude CLI under the signed-in subscription login."""

    def __init__(self, suite: Suite) -> None:
        self.suite = suite
        self.suite_skills = tuple(v.skill.name for v in suite.variants if v.skill)

    def run(self, variant: Variant, case: Case) -> Trace:
        with _sandbox() as (root, project, env):
            baseline = self._prepare_project(case, project)
            for name, text in case.files:
                _write_inside(project, name, text)
            system_file = self._prepare_subject(variant, root, project)
            argv = command(variant, system_file, self.suite.allowed_tools)
            parsed = self._invoke(argv, render.user_turn(variant, case), project, env)
            check_isolation(variant, parsed.loaded_skills, self.suite_skills)
            if parsed.is_error:
                raise RunError(f"variant {variant.id!r}: run ended in error: {parsed.final_text[:200]}")
            return Trace(
                final_text=parsed.final_text,
                tool_calls=parsed.tool_calls,
                skills_invoked=_skills_invoked(parsed.tool_calls),
                files=_snapshot(project, baseline),
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                cost_usd=parsed.cost_usd,
                duration_ms=parsed.duration_ms,
            )

    def ask(self, judge: JudgeConfig, system: str, user_text: str) -> str:
        """One tool-less prompt to the judge model; returns its reply text."""
        with _sandbox() as (root, project, env):
            system_file = root / "judge.md"
            system_file.write_text(system, encoding="utf-8")
            parsed = self._invoke(judge_command(judge, system_file), user_text, project, env)
            if parsed.is_error:
                raise RunError(f"judge call ended in error: {parsed.final_text[:200]}")
            return parsed.final_text

    def _prepare_project(self, case: Case, project: Path) -> dict[str, tuple[int, int]]:
        """Check out the case's commit and write the suite's settings. Returns the
        checkout's files as they stood, so the trace keeps only what the run changed."""
        try:
            if case.checkout is not None:
                checkout.materialise(case.checkout, project)
            if self.suite.settings is not None:
                target = project / ".claude" / "settings.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(checkout.expand(self.suite.settings), encoding="utf-8")
        except checkout.CheckoutError as exc:
            raise RunError(f"case {case.id!r}: {exc}") from None
        return _stats(project) if case.checkout is not None else {}

    @staticmethod
    def _prepare_subject(variant: Variant, root: Path, project: Path) -> Path | None:
        subject = variant.subject
        if isinstance(subject, PromptSpec):
            system_file = root / "system.md"
            system_file.write_text(render.system_prompt(subject), encoding="utf-8")
            return system_file
        if variant.skill is not None:
            target = project / ".claude" / "skills" / variant.skill.name
            shutil.copytree(variant.skill.root, target, ignore=shutil.ignore_patterns("evals", ".*"))
        return None

    def _invoke(self, argv: list[str], user_text: str, cwd: Path, env: dict[str, str]) -> Parsed:
        try:
            done = subprocess.run(argv, input=user_text, cwd=cwd, env=env, capture_output=True,
                                  text=True, timeout=self.suite.timeout_s)
        except subprocess.TimeoutExpired:
            raise RunError(f"timed out after {self.suite.timeout_s}s") from None
        except OSError as exc:
            raise RunError(f"cannot start {CLAUDE}: {exc}") from None
        if done.returncode != 0 and not done.stdout.strip():
            raise RunError(f"{CLAUDE} exited {done.returncode}: {done.stderr.strip()[:300]}")
        return parse_stream(done.stdout.splitlines())


@contextmanager
def _sandbox() -> Iterator[tuple[Path, Path, dict[str, str]]]:
    """A temp root holding an empty project and a private config dir with the login."""
    with tempfile.TemporaryDirectory(prefix="evalkit-") as tmp:
        root = Path(tmp)
        project, config = root / "project", root / "config"
        project.mkdir()
        config.mkdir()
        yield root, project, _auth_env(config)


def _auth_env(config: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR")}
    env["CLAUDE_CONFIG_DIR"] = str(config)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return env
    source_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    credentials = source_dir / ".credentials.json"
    if not credentials.is_file():
        raise RunError("no Claude login found: run 'claude setup-token' and export "
                       "CLAUDE_CODE_OAUTH_TOKEN")
    shutil.copy2(credentials, config / ".credentials.json")
    return env


def _write_inside(project: Path, name: str, text: str) -> None:
    target = (project / name).resolve()
    if not target.is_relative_to(project.resolve()):
        raise RunError(f"fixture path {name!r} leaves the project")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _project_files(project: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if path.is_file() and rel.parts[0] not in _SKIP_IN_PROJECT:
            yield rel.as_posix(), path


def _stats(project: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for rel, path in _project_files(project):
        stat = path.stat()
        out[rel] = (stat.st_size, stat.st_mtime_ns)
    return out


def _snapshot(project: Path, baseline: dict[str, tuple[int, int]]) -> tuple[tuple[str, str | None], ...]:
    """Every file in the project except checkout files the run left as they were."""
    out: list[tuple[str, str | None]] = []
    for rel, path in _project_files(project):
        stat = path.stat()
        if baseline.get(rel) == (stat.st_size, stat.st_mtime_ns):
            continue
        out.append((rel, _text_or_none(path)))
    return tuple(out)


def _text_or_none(path: Path) -> str | None:
    if path.stat().st_size > MAX_FILE_BYTES:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _skills_invoked(calls: Iterable[ToolCall]) -> tuple[str, ...]:
    names: list[str] = []
    for call in calls:
        if call.name != "Skill":
            continue
        skill = json.loads(call.input).get("skill")
        if isinstance(skill, str) and skill not in names:
            names.append(skill)
    return tuple(names)
