# ProFact — Prompt + Artifact

Prompt-native version control for AI-assisted ("vibe") coding. ProFact records every AI turn — the prompt, the response, and the resulting code snapshot — as a version alongside Git, so you can inspect, diff, blame, test, and bisect your history at the prompt level.

## Install

```sh
pip install -e .          # from the repo root
pip install -e .[dev]     # with test dependencies
```

Requires Python 3.11+ and Git.

## Quick start

```sh
cd your-repo
profact init              # set up profact in the current Git repo
profact start             # capture Cursor via hooks (default)
profact start --claude    # or capture Claude Code via a local proxy
profact start --codex     # or install project-local Codex lifecycle hooks
profact start --copilot   # or install VS Code GitHub Copilot agent hooks
profact tui               # browse the captured history interactively
```

After `profact start --codex`, open `/hooks` in Codex, review the generated
project hook, and trust it. ProFact captures official `UserPromptSubmit`,
`Stop`, and `SessionEnd` events; the Codex CLI is the validated path. IDE
clients must dispatch those same project hooks, so verify the hook appears and
fires before relying on IDE capture.

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
| `profact start` | Configure capture (Cursor, `--copilot`, `--codex`, or `--claude`) |
| `profact log` | Browse version history (`--graph`, `--path`, filters by tag/type/branch/model/file/date/test outcome) |
| `profact show <id>` | Show a version's prompt, response, and diff |
| `profact diff [a] [b]` | Diff versions or the working tree (`--stat`, `--name-only`) |
| `profact blame <file>` | Attribute lines to the prompts that produced them (`-L`, `--show`) |
| `profact why <file:line>` | Explain why a specific line exists |
| `profact test -- <cmd>` | Run a test command and attach the result to a version |
| `profact bisect --good <id> -- <cmd>` | Find the first version that broke a test |
| `profact tag` | Label, list, move, or delete version tags |
| `profact checkout <id>` | Restore the working tree to a version |
| `profact note <commit>` | Annotate a Git commit with its version |
| `profact status` | Show capture and HEAD status |
| `profact health` | Summarize captured activity, tests, churn, and history attention |
| `profact tui` | Browse captured history in an interactive terminal UI |
| `profact recover` | Inspect and recover prompt turns interrupted before finalization |
| `profact privacy scan` | Report masked secrets in stored history (`--fix` redacts them) |
| `profact doctor` | Check (and `--fix`) the profact store |

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
`timestamp`, Codex events by `session_id` and `turn_id`, and each Claude proxy
launch by its own durable session id. Duplicate prompt/stop hooks are
idempotent, late responses return to their original turn when the provider
exposes them, and concurrent prompt opens advance branch HEAD atomically.
`profact status` and `profact recover` identify every open session on the
branch. Run simultaneous Claude launches on distinct `--port` values.

All sessions still share the repository's working tree. If their open intervals
overlap, ProFact preserves each prompt/response and snapshot but marks every
affected version `uncertain` with the reason. It does not claim which session
wrote individual files; exact concurrent filesystem attribution would require
isolated worktrees or per-write provenance.

## Completion and token telemetry

Each prompt version records when ProFact observed it closing and how it closed
(for example `cursor-stop`, `vscode-copilot-stop`, `claude-session-end`, or
recovery). `profact show` and the TUI Overview display elapsed wall time from
prompt open to observed completion.

Claude token usage is provider-reported and summed across all API calls absorbed
into the user prompt, including input, output, cache-read, and cache-creation
tokens. Cursor, Codex, and VS Code Copilot lifecycle hooks currently do not
expose trustworthy token usage, so those fields remain `not reported` rather
than being estimated. Codex hooks provide the final response but not model
reasoning. VS Code Copilot hooks expose neither assistant response, reasoning,
model, nor usage. ProFact records those fields as unavailable and does not parse
the unstable transcript or guess from saved text.

## Interactive history

`profact tui` opens a read-only history browser with a navigable DAG and tabs
for overview, prompt, reasoning, response, diff, and recorded tests. Use `/` to
search, `f` for the existing log filters, `b` to inspect another captured
branch without switching Git branches, `p` for HEAD ancestry, `h` for health,
and `?` for the complete keyboard reference.

Selecting a version does not move ProFact HEAD or change files. Checkout and
tag mutation remain CLI operations while their guarded TUI workflows are built.

## Testing

```sh
pytest
```

See [TESTING.md](TESTING.md) for details, [profact_spec.md](profact_spec.md) for
the full design, [profact_tui_spec.md](profact_tui_spec.md) for the terminal UI
contract, and [profact_roadmap.md](profact_roadmap.md) for planned work.

## License

See [LICENSE](LICENSE).
