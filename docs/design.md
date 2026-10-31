# evalkit: a modular eval kit for prompts and skills

Status: accepted 2026-10-31; phases 1 to 3 built (see §13 for where the code departs). This version
folds in the Elephant Goldfish review (`egm-review.md`) and the owner's decision that
prompt specs carry only the fields the Claude CLI can apply. Each fix is tagged with
the review's finding id: B = blocker, M = major, m = minor, n = nit.

## 0. Problem, users, constraints

**Problem.** There is no repeatable way to tell whether a skill or a prompt makes
Claude better at the job it was written for, or whether an edit made it worse.
Structural checks exist, but nothing measures behaviour.

**First users.** The 24 skills in `skill_pack`. Most have no test cases (2 of 24
have any), so writing the first cases has to be cheap (M2).

**Constraints.**
- **No API billing.** Every model call goes through the `claude` CLI on a Claude
  subscription login. Prompt specs therefore hold only what the CLI can set (B1, n5).
- Standard library only, Python 3.11+ (`tomllib`).
- Ports and adapters. No abstraction before it has two real call sites.

**Non-goals (n4).** A web UI, a database, plugin loading, telemetry, red-teaming,
multi-provider support, the Messages API, and skills declared in TOML.

## 1. Ideas the design rests on

1. **A prompt and a skill are both a _subject_.** A **variant** is a subject plus run
   settings, or no subject at all (the control).
2. **Evaluation is a paired comparison** of two variants on the same cases (§7).
3. **A pass is a rate.** Each case is sampled N times.
4. **Running and scoring are separate steps.** Traces and judge verdicts are both stored,
   so scoring can be re-run with no new model calls (M4).
5. **Code checks first, judges last.**
6. **Results are addressed by content.** Every input that can change a result is part of the key (B2).
7. **Static before behavioural.** Lint costs nothing and runs on all 24 skills on day one.
8. **Loaded is not the same as helpful.** For skills, the trigger rate is reported apart from the lift (M3).

## 2. Domain types (n2)

| Type | Fields |
|---|---|
| `PromptSpec` | id, model, effort, tools, ordered sections, examples, user template, documents |
| `SkillSpec` | id, `from` path, targets, frontmatter (read from SKILL.md), body sections, resources, tree hash |
| `Variant` | id, subject (`PromptSpec`, `SkillSpec` or `None`), model, effort |
| `Case` | id, input, fixture files, checks |
| `Trace` | final text, files written, tool calls, `skills_invoked`, tokens, latency, error |
| `Verdict` | check id, value in [0, 1], passed, reason |
| `Result` | key, variant id, case id, sample index, trace, verdicts |
| `Finding` | rule id, severity, location, message, source URL |

**Precedence (M6).** Model and effort resolve as variant, then subject, then the runner
default. A skill's `model = "inherit"` resolves to the variant's model. The resolved
values go into the key.

## 3. Subject specs in TOML

Paths are relative to the TOML file and must stay inside its repository (m2). The
schema is versioned. An unknown key is an error, and the message names the reason
when the key is a known API field the CLI can't set (B1).

### 3.1 `*.prompt.toml`: only what the CLI can apply

```toml
schema = 1
kind   = "prompt"
id     = "code-reviewer"
model  = "claude-opus-5-5"        # --model
effort = "high"                   # --effort: low | medium | high | xhigh | max
tools  = []                       # --tools; [] = no tools, a plain prompt

[[section]]                       # rendered as <name>…</name>, in order
name = "role"
text = "You are a senior Rust reviewer."
[[section]]
name = "instructions"
text = "file:reviewer/instructions.md"
in   = "system"                   # system (default) | user   (m1)

[[example]]                       # rendered inside <examples><example>
input  = "..."
output = "..."

[user]
documents = ["file:fixtures/diff.patch"]   # rendered in <documents>, above the query
template  = "Review this change: {{input}}"
```

The system sections go to `--system-prompt-file`, which replaces Claude Code's own
system prompt. `max_tokens`, `thinking`, `temperature`, `stop_sequences` and
`tool_choice` are rejected, because the CLI has no way to set them.

### 3.2 `*.skill.toml`: import only (M12)

`SKILL.md` stays canonical. The TOML points at a skill and adds only what a run needs.

```toml
schema  = 1
kind    = "skill"
from    = "../skill_pack/web_dev/datastar-pro"
targets = ["claude-code"]         # claude-code | claude.ai | api   (M5, m11)

[override]                        # optional, run-only; exempt from lint   (m6)
effort = "high"
```

Declaring a skill in TOML and rendering `SKILL.md` from it is deferred until a
skill without a `SKILL.md` needs it.

## 4. Static evaluation: lint

Each rule is `(LintContext) -> list[Finding]`, where
`LintContext = (spec, cases: list[Case] | None, targets)` (M5). Every doc-derived
constant lives in one table with `source_url` and `checked_on` (m5). Constants are
confirmed against the docs before any rule that uses them ships at `error`.

| Rule | Severity | Checks |
|---|---|---|
| `skill.name` | error | 1–64 characters, `[a-z0-9-]`, no leading, trailing or double hyphen, matches its directory, no `anthropic` or `claude` |
| `skill.description.limits` | error | 1–1024 characters, no XML tags |
| `skill.description.style` | warn | third person; says what the skill does *and* when to use it (heuristic) |
| `skill.listing` | warn | description + `when_to_use` ≤ 1,536 characters |
| `skill.portable` | warn; error when `targets` excludes claude-code | Claude Code-only frontmatter keys |
| `skill.body` | warn | < 500 lines; < 5k tokens, estimated as characters / 4 and labelled as an estimate |
| `skill.refs` | warn | references one level deep; a table of contents in references over 100 lines |
| `skill.paths` | warn | forward slashes; MCP tools named `Server:tool` |
| `skill.cases` | warn | at least 3 cases (needs `cases`) |
| `prompt.examples` | info | 3 to 5 examples |
| `prompt.tone` | info | ALL-CAPS MUST, CRITICAL or NEVER; rules phrased only as negatives |

`prompt.order` was dropped, because the schema already prevents that ordering mistake (m10).

## 5. Ports and adapters

```python
class Runner(Protocol):
    def run(self, variant: Variant, case: Case) -> Trace: ...
class Store(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, record: dict) -> None: ...
Check = Callable[[Trace, Lookup], Verdict]     # Lookup: cached judge verdicts
```

| Port | Adapter | Phase |
|---|---|---|
| Runner | `cli`: `claude -p` in an isolated temp project | P2 |
| Runner | `fake`: canned traces | P1 (tests) |
| Store | `jsonl`: one append-only file per suite, written by one process only (M11) | P2 |
| Check | code checks (§6) | P2 |
| Check | `judge`: a rubric scored through the `cli` runner, verdict cached under `hash(rubric, judge model, trace key)` | P3 |

### 5.1 The `cli` runner (B2, M10)

**Isolation.** `--bare` is out: it accepts only an API key and never reads the
subscription login. Each run gets its own environment instead:

- `CLAUDE_CONFIG_DIR` set to a fresh temp dir, so no user skills, `CLAUDE.md`, plugins or settings leak in.
- `CLAUDE_CODE_OAUTH_TOKEN` taken from `claude setup-token`, run once by the owner, so the subscription login carries over.
- `--setting-sources project --strict-mcp-config --no-session-persistence`.

**Invocation.**
```
claude -p <rendered user turn> --output-format stream-json --verbose
  --model M --effort E
  [--system-prompt-file F --tools <list>]                      # prompt subjects
  [--permission-mode acceptEdits --allowed-tools <skill's>]    # skill subjects
```
Skill subjects are copied into `<temp>/.claude/skills/<name>/`. Fixture files are copied into `<temp>` (m3).

**Preflight.** The stream's init event lists the loaded skills. The run aborts if the
control lists any skill, or if a skill variant doesn't list the skill under test.

**Trace capture.** The final text, tool calls and token counts are parsed from the
stream. `skills_invoked` comes from the Skill tool calls. `files_written` comes from a
diff of the temp directory before and after the run.

**Trust boundary.** A run is exactly as trusted as the skill's scripts. Run third-party
skills inside a container.

**Failures and throughput (M11).** Runs are serial by default, with `--jobs N` to go
wider. Only successful traces are stored under their key. Errors are logged and
retried on the next `run`. `plan` prints the job count, and over 50 jobs it requires `--yes`.

**Key.** The hash covers the CLI version, the rendered subject (for skills, a hash of
the whole tree), the case input and fixture bytes, the resolved model and effort, the
tool and permission flags, and the sample index.

To prove in the first P2 spike: that `CLAUDE_CONFIG_DIR` plus the OAuth token isolates
runs as described, and that the init event lists skills.

## 6. Checks (M7)

| Key | Argument | Target | Pass condition |
|---|---|---|---|
| `contains` / `regex` | string | `final_text`, or a file via `in = "path"` | it matches |
| `file_exists` | path | temp project | the file exists |
| `tool_called` | tool name | trace tool calls | the tool was called at least once |
| `skill_triggered` | (none) | `skills_invoked` | the skill under test was invoked |
| `all` / `any` | list of checks | — | all of them pass / any of them passes |
| `judge` | rubric string | final text and files | the judge says it passes (P3) |

A check passes when its value is 1.0. `weight`, `not` and `json_schema` are deferred.

## 7. Comparison statistics (M1, m13)

- **Per-case rate:** the mean pass rate across samples.
- **Lift:** the mean of the per-case differences (head minus base) over cases both variants share.
- **Interval:** a 95% percentile bootstrap that resamples cases, 2,000 draws, fixed seed.
- **Flipped case:** a case whose majority verdict differs between the two variants.
- **Trigger rate** (skills): the share of runs where the skill was invoked. Lift is
  reported twice, over all runs and over triggered runs only (M3).
- **Gate:** fails only when the upper bound of the lift interval is below `-max_drop`.
- A warning is printed when the interval is wider than `2 × max_drop`, because the
  gate can't detect a drop that size with so few cases.

**Control (M8).** For a skill, the control is the same run with no skill installed.
For a prompt, the control must be a named baseline prompt. `compare` refuses a pair
whose case inputs render differently.

## 8. Suites and cases

```toml
samples = 3
[variant.control]
[variant.skill]
subject = "subjects/datastar.skill.toml"

[[case]]
id     = "char-counter"
input  = "Build a live character counter in Datastar Pro..."
files  = { "index.html" = "file:fixtures/empty.html" }
checks = [{ skill_triggered = {} }, { regex = "data-bind" }]
```

`evalkit init <skill dir>` writes a stub suite with 3 cases. Their prompts are built from
the skill's description, and they're marked TODO for the author to finish (M2).

## 9. CLI

```
evalkit lint    <subject.toml | skill dir>...     # P1
evalkit init    <skill dir>                       # P1
evalkit run     suite.toml [--jobs N] [--yes]     # P2
evalkit score   suite.toml [--no-judge]           # P2 (judge P3)
evalkit compare suite.toml BASE HEAD              # P2
evalkit gate    suite.toml BASE HEAD --max-drop X # P4; HEAD@<git rev> materialised via `git worktree` (m4)
```

## 10. Phases and size (M2, m9)

| Phase | Ships | Model calls | Approx. lines |
|---|---|---|---|
| P1 | spec loading (import mode and prompts), lint, `init`, fake runner, tests | none | 450 |
| P2 | `cli` runner, isolation, `jsonl` store, code checks, `run`/`score`/`compare`, stats; 3 skills with real cases | yes | 500 |
| P3 | `judge`, verdict cache, calibration against `labels.jsonl` (`trace_key, check_id, passed`) (m8) | yes | 150 |
| P4 | leave-one-out ablation (split on `##` headings, plus one piece per resource), `gate` (M9) | yes | 150 |

Each phase works on its own. P1 is useful on day one with no cases and no cost.

## 11. Layout

```
evalkit/
  spec.py  lint.py  rules.py       # P1
  plan.py  stats.py  checks.py     # P2 (pure)
  runner_cli.py  store_jsonl.py    # P2 (I/O)
  judge.py  ablate.py              # P3, P4
  cli.py
  tests/
```

## 12. Open questions

1. Can the judge use a different model from the subject's, to avoid the judge
   favouring its own model's output? The proposed default is Sonnet as judge, and the
   judge model must always differ from the subject's.
2. P2 spike: does the isolation in §5.1 hold, and which init event fields list skills?

## Sources

- Agent Skills specification: https://agentskills.io/specification.md
- Claude Code skills: https://code.claude.com/docs/en/skills
- Skill authoring best practices: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
- Prompting best practices: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
- Claude Code CLI 2.1.314 `--help`: `--bare`, `--system-prompt-file`, `--tools`, `--setting-sources`, `--strict-mcp-config`, `setup-token`

## 13. Implementation notes (phases 1 to 3)

Where the code departs from the sections above, and why:

- **`skill_triggered` is not a check.** It would always fail for the control and
  inflate lift. Trigger rate is reported for every skill variant instead, with
  lift recomputed on triggered runs only (§7).
- **The CLI version is stored with each trace, not hashed into the key.** `run`
  re-runs any trace whose version differs, and `score`/`compare` can still find
  traces without asking the CLI its version.
- **Permissions are suite-level.** `allowed_tools` applies to every variant so a
  pair differs only in its subject; edits are always allowed (`acceptEdits`).
- **Login**: `CLAUDE_CODE_OAUTH_TOKEN` if set, else a copy of
  `.credentials.json` into the run's config dir. The P2 spike confirmed this
  isolates user skills and still authenticates; bundled Claude Code skills still
  load in every variant, which is fair because they load in all of them.
- **No fake runner module.** Tests pass a small fake that satisfies the `Runner`
  protocol directly.
- **Judge checks take cached verdicts.** `Check.passes(trace, verdicts)`; `run`
  fills the cache, `score` only reads it. A verdict's key covers the rubric,
  judge model and effort, and the full trace. The judge model is a suite-level
  `[judge]` setting defaulting to Sonnet; it is not forced to differ from the
  subject's model, since a variant using the CLI default model can't be checked.
- **Calibration labels** are addressed by variant, case, sample and rubric
  rather than by trace key, so a person can read and edit them. `calibrate`
  writes the template itself when the labels file doesn't exist.

