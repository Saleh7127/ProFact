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
| `profact doctor` | Check (and `--fix`) the profact store |

## Testing

```sh
pytest
```

See [TESTING.md](TESTING.md) for details, [profact_spec.md](profact_spec.md) for the full design, and [profact_roadmap.md](profact_roadmap.md) for planned work.

## License

See [LICENSE](LICENSE).
