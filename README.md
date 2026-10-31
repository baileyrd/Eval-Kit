# evalkit

Evaluate prompts and Agent Skills: statically against Anthropic's published
guidance, and behaviourally by running them through the Claude CLI and
comparing variants on the same cases.

Standard library only, Python 3.11+. See [docs/design.md](docs/design.md).

## Status

Phases 1 to 3 of 4: `lint`, `init`, `run`, `score`, `compare`, the model judge
and `calibrate`. Ablation and `gate` (phase 4) come later; see the design.

## Use

```
python3 -m evalkit lint path/to/skill_pack            # every SKILL.md below it
python3 -m evalkit lint my.prompt.toml my.skill.toml  # subject files
python3 -m evalkit lint skills/ --min-severity warn --format json
python3 -m evalkit init path/to/skill                 # writes evals/suite.toml

python3 -m evalkit run     suite.toml [--jobs 4]      # runs only what isn't stored yet
python3 -m evalkit score   suite.toml [--verbose]     # pass and trigger rates; no model calls
python3 -m evalkit compare suite.toml control skill   # paired lift with a 95% interval
python3 -m evalkit calibrate suite.toml labels.jsonl  # judge vs your labels
```

`lint` exits 1 when any finding is an error.

## Suites

```toml
samples       = 3             # runs per variant per case
allowed_tools = ["Bash"]      # pre-approved for every variant; edits are always allowed
timeout       = 600           # seconds per run

[variant.control]             # no subject: the same run without the skill
[variant.skill]
subject = ".."                # a skill directory, *.skill.toml or *.prompt.toml
model   = "claude-sonnet-5-5" # optional; overrides the subject's own

[[case]]
id     = "char-counter"
input  = "Build a live character counter in Datastar Pro..."
files  = { "index.html" = "file:fixtures/empty.html" }   # copied in before the run
checks = [{ regex = "data-bind" }, { contains = "280", in = "index.html" }]
```

Checks: `contains`, `regex` (both on the final text, or a file with `in`),
`file_exists`, `tool_called`, `judge`, and `all` / `any` over a list of checks.
A run passes when all of its case's checks pass.

## The judge

`{ judge = "The page shows the count in red past 280 characters" }` asks a
model whether one run meets one criterion. It sees the criterion, the final
reply and the files the run left, and answers pass or fail with a reason.

```toml
[judge]
model  = "claude-sonnet-5-5"   # the default; pick one that isn't the model under test
effort = "low"
```

`run` judges every stored run that has no verdict yet, through the same
isolated CLI and login as the runs themselves. Verdicts are cached under a hash
of the criterion, the judge model and effort, and the exact trace, so `score`
and `compare` never call the model, and a re-run trace is judged afresh. A run
still waiting for a verdict is left out of `score` and counted.

Judges are wrong sometimes, so check yours: `evalkit calibrate suite.toml
labels.jsonl` writes every verdict to `labels.jsonl` with `"label": null`. Set
each label to `true` or `false` (or delete the line), run it again, and it
reports how often the judge agrees with you, whether it errs lenient or strict,
and each disagreement with the judge's reason.

For skill variants `score` also reports the **trigger rate**, the share of runs
where Claude actually loaded the skill, and `compare` reports lift on those runs
alone. A skill that never triggers and one that triggers but doesn't help need
different fixes.

## How runs work

Each run is `claude -p` in a fresh temporary project with its own
`CLAUDE_CONFIG_DIR`, so none of your installed skills, CLAUDE.md, plugins,
settings or MCP servers leak in. A skill under test is copied into the project's
`.claude/skills/`; a prompt replaces Claude Code's system prompt. Every run
checks the loaded-skills list and fails if the control saw the skill under test,
or the skill variant didn't load it.

Runs use your Claude subscription login, never the API: `ANTHROPIC_API_KEY` is
removed from each run's environment. The login comes from
`CLAUDE_CODE_OAUTH_TOKEN` if set (create one with `claude setup-token`; needed
on macOS, where the login lives in the keychain), otherwise from a copy of
`~/.claude/.credentials.json`.

Traces go to `.evalkit/<suite>.jsonl` beside the suite (add `.evalkit/` to
`.gitignore`). Each is keyed by a hash of everything that can change the result:
the skill's files or the rendered prompt, model, effort, allowed tools, the
case's input and fixtures, and the sample number. Change a skill and only its
runs repeat; change a check and `score` re-judges without calling the model. A
new Claude CLI version re-runs everything. Failed runs aren't stored, so the
next `run` retries them. Each finding names the rule, the
file, and the Anthropic page the rule comes from.

## Subjects

A **skill** is a directory with a `SKILL.md`; that file stays canonical. A
`*.skill.toml` points at one and says where it must work:

```toml
schema  = 1
kind    = "skill"
from    = "../skill_pack/web_dev/datastar-pro"
targets = ["claude-code"]        # claude-code | claude.ai | api
```

A **prompt** is a `*.prompt.toml` holding only what the `claude` CLI can apply:
model, effort, tools, ordered sections rendered as XML tags, examples, and the
user turn. Messages API fields the CLI can't set (`max_tokens`, `temperature`,
`thinking`, ...) are rejected with the reason.

```toml
schema = 1
kind   = "prompt"
id     = "reviewer"
model  = "claude-opus-5-5"
effort = "high"

[[section]]
name = "instructions"
text = "file:reviewer/instructions.md"   # relative to this file, inside the repo

[[example]]
input  = "..."
output = "..."

[user]
template = "Review this change: {{input}}"
```

## Lint rules

| Rule | Checks |
|---|---|
| `skill.name` | 1–64 of `[a-z0-9-]`, matches its directory, no reserved words |
| `skill.description.limits` / `.style` | ≤1024 characters, no XML; third person, says when to use it |
| `skill.listing` | description + `when_to_use` ≤ 1,536 characters |
| `skill.portable` | Claude Code-only keys (error when `targets` excludes Claude Code); unknown keys |
| `skill.compatibility` / `skill.metadata` | ≤500 characters; metadata values are strings |
| `skill.body` | < 500 lines, < ~5k tokens (estimated) |
| `skill.refs` / `skill.paths` | links resolve, one level deep, table of contents past 100 lines; forward slashes |
| `skill.cases` | an eval suite with at least 3 cases |
| `prompt.examples` | 3–5 examples |
| `prompt.tone` | capitalised MUST/NEVER emphasis; rules stated only as negatives |

The limits live in `evalkit/rules.py` with the page each comes from and the
date it was last checked.

## Develop

```
python3 -m unittest discover -s tests -t .
python3 -m mypy        # strict; CI-only dependency
```
