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
profact tui               # browse the captured history interactively
```

## Commands

| Command | Purpose |
| --- | --- |
| `profact init` | Initialize profact in the current repository |
| `profact start` | Start capturing AI turns (Cursor hooks or `--claude` proxy) |
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
| `profact doctor` | Check (and `--fix`) the profact store |

## Crash recovery

If Cursor, Claude, or the machine stops during a turn, inspect it explicitly:

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
