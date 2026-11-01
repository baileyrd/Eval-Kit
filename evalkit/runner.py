"""Run a job through ``claude -p`` in an isolated temporary project.

Isolation: each run gets a fresh ``CLAUDE_CONFIG_DIR``, so no user skills,
CLAUDE.md, plugins or settings leak in. The login comes from
``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``) or, failing that, a copy of
the current ``.credentials.json``. ``ANTHROPIC_API_KEY`` is removed from the
child's environment, so a run never bills the API.

Runs can share a host, so each one's scratch is private: ``TMPDIR`` (and
``CLAUDE_CODE_TMPDIR``, where the sandbox puts Bash's ``$TMPDIR``) point into the
run's own temp root, and ``--settings`` denies sandboxed commands the shared temp
root while re-opening only that run's own. Claude's file tools (Read, Grep, Glob)
are not bound by the sandbox; a suite fences them with its own ``settings``.
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
from typing import Any

from evalkit import checkout, render
from evalkit.spec import Case, JudgeConfig, PromptSpec, Suite, Variant
from evalkit.trace import ToolCall, Trace

CLAUDE = "claude"
MAX_FILE_BYTES = 200_000  # larger outputs are recorded by name only
_SKIP_IN_PROJECT = (".claude", ".git")
_TMP_VARS = ("TMPDIR", "TMP", "TEMP", "CLAUDE_CODE_TMPDIR")
_TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


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
    turns: int = 1  # more than one when a message arrived after the answer, e.g. a notice


def cli_version() -> str:
    try:
        done = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=30,
                              check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunError(f"cannot run '{CLAUDE} --version': {exc}") from None
    return done.stdout.strip()


_BASE_ARGV = (CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
              "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence")


def command(variant: Variant, system_file: Path | None, allowed_tools: Iterable[str],
            isolation: Path | None = None) -> list[str]:
    """The argv for one run; the user turn goes on stdin. Pure, for testing.

    *isolation* is a settings file passed with ``--settings``: command-line settings
    merge over the project's, and their ``allowRead`` counts even where the project's
    would not.
    """
    argv = [*_BASE_ARGV, *_model_args(variant.model, variant.effort)]
    if isolation is not None:
        argv += ["--settings", str(isolation)]
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


def isolation_settings(root: Path, shared: Iterable[Path]) -> dict[str, object]:
    """Sandbox settings that deny the *shared* temp roots and re-open only *root*.

    The narrower path wins where read rules overlap, so the run keeps its own root
    while every sibling run's project and scratch under the shared roots is unreadable.
    """
    return {"sandbox": {"filesystem": {"denyRead": [str(p) for p in shared],
                                       "allowRead": [str(root)]}}}


def _model_args(model: str | None, effort: str | None) -> list[str]:
    return [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]


def parse_stream(lines: Iterable[str]) -> Parsed:
    """Read the init event, tool calls and the result events from stream-json.

    A message can arrive after the answer, such as a background-task notice, and start
    another turn whose reply is not the answer. The answer is then the first turn's: its
    result event when the CLI wrote one per turn, otherwise the last assistant text
    before the first such message. Tokens and time add up over every turn.
    """
    skills: tuple[str, ...] = ()
    calls: list[ToolCall] = []
    results: list[Mapping[str, object]] = []
    first_turn_text: str | None = None
    injected = 0
    for event in _events(lines):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            skills = tuple(str(s) for s in event.get("skills", []))
        elif kind == "assistant":
            content = event["message"]["content"]
            calls += [ToolCall(str(b["name"]), json.dumps(b.get("input", {}), sort_keys=True))
                      for b in content if b.get("type") == "tool_use"]
            text = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
            if text.strip() and not injected:
                first_turn_text = text
        elif kind == "user" and _is_new_message(event) and first_turn_text is not None:
            injected += 1
        elif kind == "result":
            results.append(event)
    if not results:
        raise RunError("the CLI ended without a result event")
    answer = results[0]
    final = str(answer.get("result") or "")
    if len(results) == 1 and injected and first_turn_text is not None:
        final = first_turn_text
    usages = [u for u in (r.get("usage") for r in results) if isinstance(u, dict)]
    return Parsed(
        loaded_skills=skills,
        tool_calls=tuple(calls),
        final_text=final,
        is_error=bool(answer.get("is_error")) or answer.get("subtype") != "success",
        input_tokens=sum(int(u.get(k) or 0) for u in usages for k in _TOKEN_FIELDS),
        output_tokens=sum(int(u.get("output_tokens") or 0) for u in usages),
        cost_usd=max(float(str(r.get("total_cost_usd") or 0)) for r in results),
        duration_ms=sum(int(str(r.get("duration_ms") or 0)) for r in results),
        turns=max(len(results), 1 + injected),
    )


def _events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        if line.strip():
            event = json.loads(line)
            if isinstance(event, dict):
                yield event


def _is_new_message(event: Mapping[str, Any]) -> bool:
    """A user event that starts a turn, as opposed to one carrying tool results."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") != "tool_result" for b in content)


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
            isolation = root / "isolation.settings.json"
            isolation.write_text(json.dumps(isolation_settings(root, _shared_temp_roots())),
                                 encoding="utf-8")
            argv = command(variant, system_file, self.suite.allowed_tools, isolation)
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
                turns=parsed.turns,
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
    """A temp root holding an empty project, a private config dir with the login, and
    the run's own scratch dir, which every temp variable in the environment names."""
    with tempfile.TemporaryDirectory(prefix="evalkit-") as tmp:
        root = Path(tmp).resolve()
        project, config, scratch = root / "project", root / "config", root / "tmp"
        for path in (project, config, scratch):
            path.mkdir()
        env = _auth_env(config)
        env.update({name: str(scratch) for name in _TMP_VARS})
        env["CARGO_TARGET_DIR"] = str(scratch / "cargo-target")  # never a shared, stale build
        yield root, project, env


def _shared_temp_roots() -> tuple[Path, ...]:
    """Where other runs' roots and scratch can live: this host's temp dir, and /tmp."""
    roots = [Path(tempfile.gettempdir()).resolve()]
    system = Path("/tmp")
    if system.is_dir() and system.resolve() not in roots:
        roots.append(system.resolve())
    return tuple(roots)


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
