# ProFact — Prompt + Artifact

Prompt-native version control for AI-assisted ("vibe") coding. ProFact records every AI turn — the prompt, the response, and the resulting code snapshot — as a version alongside Git, so you can inspect, diff, blame, test, and bisect your history at the prompt level.

## Install

ProFact requires Python 3.11+ and Git. To install it from a fresh checkout in
an isolated environment:

```sh
git clone https://github.com/Saleh7127/ProFact.git
cd ProFact
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Contributors and evaluators should install the test dependencies instead:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Quick start

```sh
cd your-repo
profact init              # set up profact in the current Git repo
profact start             # capture Cursor via hooks (default)
profact start --claude    # or launch and monitor local Claude Code sessions
profact start --codex     # or monitor local Codex sessions (keep it running)
profact start --copilot   # or install VS Code GitHub Copilot agent hooks
profact tui               # browse the captured history interactively
```

`profact start --codex` is a foreground monitor: keep its terminal open while
using Codex CLI, the Codex VS Code extension, or the ChatGPT/Codex desktop app
against the local repository. It tails new records from
`$CODEX_HOME/sessions`, captures only turns started while monitoring, and stops
with Ctrl+C. Cloud-only tasks are outside this local-filesystem capture path.

After `profact start --copilot`, VS Code automatically loads
`.github/hooks/profact.json`. Verify it with `Chat: Configure Hooks`, the
`GitHub Copilot Chat Hooks` output channel, or `Developer: Show Agent Debug
Logs`. This adapter targets local VS Code agent sessions; remote, background,
or cloud execution requires ProFact to be installed and configured in that
execution environment.

## Commands

| Command | Purpose |
| --- | --- |
| `profact init` | Initialize profact in the current repository |
| `profact start` | Configure Cursor capture hooks (default) |
| `profact start --claude` | Launch Claude Code and monitor its local session transcript |
| `profact start --codex` | Monitor local Codex CLI, IDE, and desktop sessions |
| `profact start --copilot` | Install VS Code GitHub Copilot agent hooks |
| `profact log` | Browse version history (`--graph`, `--path`, `--tests`; filters: `--tag`, `--type`, `--branch`, `--model`, `--file`, `--since`, `--test-status`) |
| `profact show <id>` | Show a version's metadata, prompt, thinking, response, and diff |
| `profact show <id> prompt` | Show only the prompt section |
| `profact show <id> thinking` | Show only the thinking/reasoning section |
| `profact show <id> response` | Show only the response section |
| `profact show <id> diff` | Show only the diff section |
| `profact diff` | Diff HEAD against the working tree |
| `profact diff <a>` | Diff a version against the working tree |
| `profact diff <a> <b>` | Diff two versions (`--stat`, `--name-only`, `--exit-code`) |
| `profact blame <file>` | Attribute lines to the prompts that produced them (`-L`, `--show`) |
| `profact blame <file> <id>` | Blame a file at a specific version |
| `profact why <file:line>` | Explain why a specific line exists |
| `profact why <file:line> <id>` | Explain a line at a specific version |
| `profact test -- <cmd>` | Run a test command and attach the result to HEAD (`--version` to target another) |
| `profact bisect --good <id> -- <cmd>` | Find the first version that broke a test (`--bad` defaults to HEAD) |
| `profact tag` | List all tags |
| `profact tag <id> <label>` | Create a tag (`--force` / `-f` to move an existing tag) |
| `profact tag --delete <label>` | Delete a tag (`-d`) |
| `profact checkout <id>` | Restore the working tree to a version (`--force` skips cross-branch confirm) |
| `profact snapshot` | Capture pending manual working-tree changes as one human version |
| `profact note <commit>` | Annotate a Git commit with HEAD (`profact note <commit> <id>` for a specific version) |
| `profact status` | Show capture and HEAD status |
| `profact health` | Summarize captured activity, tests, churn, and history attention |
| `profact tui` | Browse captured history in an interactive terminal UI |
| `profact recover` | List prompt turns interrupted before finalization |
| `profact recover <id> --finalize` | Adopt the current working tree as that turn's artifact |
| `profact recover <id> --preserve` | Close incomplete without attributing current files |
| `profact privacy scan` | Report masked secrets in stored history |
| `profact privacy scan --fix` | Redact detected secrets in stored objects |
| `profact doctor` | Check the profact store |
| `profact doctor --fix` | Apply safe repairs (prune phantoms, orphans, dangling tags) |

## Crash recovery

If Cursor, VS Code Copilot, Codex, Claude, or the machine stops during a turn,
inspect it explicitly:

```sh
profact recover
profact recover v13 --finalize  # current branch HEAD: adopt the current tree
profact recover v13 --preserve  # close incomplete; leave working files untouched
```

Recovered finalization is marked `uncertain` because post-crash edits cannot be
distinguished reliably. Preserve keeps the prompt/response record, records no
artifact, and lets current changes be captured separately with `profact
snapshot`. ProFact does not delete a crashed prompt: destructive discard would
contradict its immutable, never-lose-work history model.

## Privacy controls

High-precision secret redaction is enabled by default for newly stored prompts,
reasoning, responses, diffs, and test output. Configure repository-specific
rules in an optional, commit-safe `profact.toml`:

```toml
[privacy]
private_paths = [".env", "secrets/**", "customer-data/"]
redact_patterns = ["INTERNAL-[0-9]{8}"]
```

Private paths never enter ProFact snapshots or diffs, are left untouched by
ProFact checkout, and remain available to normal Git if deliberately tracked.
`profact.toml` itself is never restored by ProFact checkout, preventing time
travel from silently weakening the active policy.

Audit history captured before privacy controls were enabled:

```sh
profact privacy scan
profact privacy scan --fix
```

Reports contain masked excerpts only. `--fix` updates referenced ProFact
objects and removes unsafe unreferenced originals. Existing Git commits and Git
objects are not rewritten; remove secrets from Git history separately when
needed.

## Multiple capture sessions

ProFact routes concurrent Cursor events by `conversation_id` and
`generation_id`, VS Code Copilot events by `session_id` and prompt-hook
`timestamp`, Codex events by `session_id` and `turn_id`, and Claude Code events
by `sessionId` and `promptId`. Duplicate prompt/stop events are
idempotent, late responses return to their original turn when the provider
exposes them, and concurrent prompt opens advance branch HEAD atomically.
`profact status` and `profact recover` identify every open session on the
branch.

All sessions still share the repository's working tree. If their open intervals
overlap, ProFact preserves each prompt/response and snapshot but marks every
affected version `uncertain` with the reason. It does not claim which session
wrote individual files; exact concurrent filesystem attribution would require
isolated worktrees or per-write provenance.

## Completion and token telemetry

Each prompt version records when ProFact observed it closing and how it closed
(for example `cursor-stop`, `vscode-copilot-stop`, `claude-session-monitor`, or
recovery). `profact show` and the TUI Overview display elapsed wall time from
prompt open to observed completion.

Claude and Codex token usage is provider-reported and grouped by their durable
session turn identifiers. Claude usage is deduplicated across split transcript
records and summed across the tool loop, including input, output, cache-read,
and cache-creation tokens. Cursor and VS Code Copilot hooks currently do not
expose trustworthy usage, so those fields remain `not reported` rather than
being estimated. VS Code Copilot hooks expose neither assistant response,
reasoning, model, nor usage; ProFact records those fields as unavailable.

## Interactive history

`profact tui` opens a read-only history browser with a navigable DAG and tabs
for overview, prompt, reasoning, response, diff, and recorded tests. Use `/` to
search, `f` for the existing log filters, `b` to inspect another captured
branch without switching Git branches, `p` for HEAD ancestry, `h` for health,
and `?` for the complete keyboard reference.

Selecting a version does not move ProFact HEAD or change files. Checkout and
tag mutation remain CLI operations while their guarded TUI workflows are built.

## Testing and reproducing a revision

For a normal test of the current checkout, activate its virtual environment and
run:

```sh
python -m pytest -q
```

All tests should pass. The thesis evaluation environment currently collects 215
tests; a later revision may contain a different number.

To reproduce a particular published revision, use its evaluation tag or full
commit hash rather than the moving default branch:

```sh
git clone https://github.com/Saleh7127/ProFact.git
cd ProFact
git fetch --tags
git switch --detach <tag-or-full-commit-hash>
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
```

Record the source and environment together with the result:

```sh
git rev-parse HEAD
git status --short
python --version
git --version
python -c "import profact; print(profact.__version__)"
python -m pip freeze
python -m pytest -q
```

`git status --short` should be empty for a clean reproduction. The commit hash,
Python and Git versions, dependency list, operating system and hardware, and
complete test result form the minimum reproduction record.

### Creating an evaluation freeze

A freeze is only needed before collecting results that must remain reproducible;
ordinary users do not need to create one. A project maintainer should:

1. Commit the implementation, evaluation protocol, runbook, and analysis scripts.
2. Confirm the worktree is clean and the complete test suite passes.
3. Create an annotated tag at that exact commit, such as `eval-freeze-v1`.
4. Push the commit and tag to the public repository.
5. Archive the environment record and raw test output with the evaluation data.

Example maintainer commands:

```sh
git remote get-url origin
git status --short
python -m pytest -q
git tag -a eval-freeze-v1 -m "Frozen revision for thesis evaluation"
git push origin eval-freeze-v1
```

## License

See [LICENSE](LICENSE).
