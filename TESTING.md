# Testing profact locally

`profact` gives AI-assisted coding a navigable history whose unit is a
**(prompt, artifact) pair**. This guide shows how to run it end to end on your
machine and lists the current limitations.

All commands assume you are at the repo root: `/Users/saleh/Downloads/V2C2`.

---

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This puts a `profact` command on your PATH (`pyproject.toml [project.scripts]`)
and installs the dev dependency (`pytest`).

> If you prefer not to install, you can run the CLI as a module from the repo
> root: `PYTHONPATH=. python -m profact.cli <command>`.

---

## 2. Run the automated tests (fastest confidence check)

```bash
pytest -q
```

Expected: **29 passed**. Coverage by area:

| File | What it proves |
|------|----------------|
| `test_aggregator.py` | turn-boundary rule (1 user prompt = 1 version) |
| `test_proxy.py` | Claude proxy forwards + parses SSE, opens one version |
| `test_watcher.py` | quiet-period finalize + `human` version creation |
| `test_cli.py` | `log`/`show`/`status`/`checkout`, full branching DoD |
| `test_cursor_hooks.py` | Cursor hook events → versions; `start` installs hooks |
| `test_phase7.py` | `note` (Git Notes) and `start --claude` wiring |

---

## 3. Try the core history commands by hand (no AI needed)

You can exercise the entire version model in a throwaway repo without any AI
tool, by treating a finalized capture the way the adapters do.

```bash
mkdir /tmp/pf-demo && cd /tmp/pf-demo
git init -q -b main && git config user.email you@example.com && git config user.name you
echo "hello" > a.txt && git add a.txt && git commit -qm init

profact init          # creates .profact/, gitignores it, makes anchor v1
profact status
profact log
```

To create real prompt-style versions without wiring an AI, drive the store the
same way the Cursor adapter does (this is exactly what the tests do):

```bash
python - <<'PY'
from pathlib import Path
from profact.adapters import cursor_hooks
repo = Path("/tmp/pf-demo")
def ev(name, **k): return {"hook_event_name": name, "generation_id": "g1", **k}
cursor_hooks.handle(repo, ev("beforeSubmitPrompt", prompt="make the button blue"))
(repo/"button.py").write_text("color = 'blue'\n")
cursor_hooks.handle(repo, ev("afterAgentResponse", text="Done."))
cursor_hooks.handle(repo, ev("stop"))
PY

profact log                 # v2 (prompt) on top of v1 (anchor)
profact show v2             # prompt, response, diff
profact checkout v1         # button.py disappears (restored to anchor)
profact log --graph         # see the DAG + branch tips
```

Branch backward and confirm nothing is lost:

```bash
# (after checkout v1) create a different v3 from v1, then hop between branches
profact checkout v2         # button.py is blue again; v3 still exists in log
```

---

## 4. Real capture path A — Cursor (primary)

This is the intended day-to-day mode.

```bash
cd /path/to/your/project   # any git repo
profact init
profact start              # installs .cursor/hooks.json + .cursor/hooks/profact-hook.sh
```

Then **reload/restart Cursor** so it loads the hooks (Cursor watches
`hooks.json` but a reload is the reliable trigger). Now just use Cursor's chat
normally. Each prompt you send becomes a version:

```bash
profact log
profact show <id>
profact checkout <id>
```

Verify the hooks are live in **Cursor → Settings → Hooks** (or the Hooks output
channel). To confirm the wiring without Cursor, simulate an event:

```bash
echo '{"hook_event_name":"beforeSubmitPrompt","generation_id":"g1","prompt":"hi","workspace_roots":["'"$PWD"'"]}' \
  | bash .cursor/hooks/profact-hook.sh   # prints {}
profact log                               # a new prompt version appears
```

---

## 5. Real capture path B — Claude Code (secondary)

Requires the `claude` CLI installed and on PATH.

```bash
cd /path/to/your/project
profact init
profact start --claude     # starts the proxy on :7845, then launches claude
```

`profact` sets `ANTHROPIC_BASE_URL` to the local proxy, captures each turn, and
tears the proxy/watcher down when `claude` exits. Inspect with `profact log` /
`profact show` as usual. Use `--port` if 7845 is taken.

---

## 6. Optional — link a version to a Git commit

```bash
git commit -m "ship login"
profact note HEAD          # writes git notes --ref=profact mapping the commit -> HEAD version
git notes --ref=profact show HEAD
```

---

## Current limitations & conflicts

### Capture limitations
- **Cursor CLI vs IDE.** Per Cursor's docs, `afterAgentResponse` fires in the
  **IDE** but not the Cursor CLI; `stop`/`sessionEnd` fire in both. In the IDE
  you get prompt + response + diff. In the CLI you still get prompt + diff, but
  the assistant response text may be missing. profact targets the IDE.
- **Cloud agents not supported.** Cursor cloud agents fire only tool-level
  hooks (no `beforeSubmitPrompt`/`stop`/`sessionEnd`), so capture there is out
  of scope.
- **One active session at a time.** The open turn is identified as "the HEAD
  prompt version whose `tree_after` is NULL." Two concurrent agent sessions in
  the same repo would interleave and mis-attribute. Single local user assumed.
- **Diffs are tree snapshots, not per-edit.** `afterFileEdit` is intentionally
  not wired; the diff is `git diff <tree_before> <tree_after>`. Exact per-edit
  attribution is reserved for later. (Claude path uses a watcher quiet-period to
  decide when to snapshot; very fast successive turns could blur a boundary.)
- **Mid-turn crash.** If Cursor/the machine dies after `beforeSubmitPrompt` but
  before `stop`, the version stays open (`tree_after` NULL) until the *next*
  prompt closes it — its diff then spans until that moment. No active recovery
  on startup yet.

### Design decisions that are limitations by choice
- **Branch-scoped history.** `profact log` shows only the current Git branch's
  versions. Switching Git branches shows a different (fresh) history; there is
  no cross-branch merge of profact history.
- **`.profact/` is gitignored, not shared.** History is local and single-user.
  Prompts/responses may contain secrets, so nothing is committed. No team
  sync/merge.
- **`init` starts at "now".** On an existing repo, init records a single
  `anchor` at the current commit; it does not reconstruct past prompts
  (unwitnessed history is never fabricated).
- **Checkout preserves ignored files.** `checkout` only reconciles files inside
  the tracked-tree domain; `.gitignore`-d files (e.g. `.env`, build caches) are
  left untouched. There is no `--clean-ignored` yet.

### Conflicts / sharp edges to be aware of
- **Hook runner is environment-pinned.** `profact start` writes
  `.cursor/hooks/profact-hook.sh` referencing the **current Python interpreter**
  and this repo's path (via `PYTHONPATH`). If you move the checkout or change
  envs, re-run `profact start`. The generated `.cursor/` hook files are
  machine-specific and not meant to be committed for others — `start` adds
  `.cursor/hooks.json` and `.cursor/hooks/` to `.gitignore` automatically.
- **Checkout never deletes the hooks.** Because `.gitignore` is itself versioned,
  checking out a version created *before* `profact start` would otherwise restore
  an old `.gitignore` and let a later checkout delete the hook files (silently
  disabling capture). profact hard-protects its own infrastructure
  (`.profact/`, `.cursor/hooks.json`, `.cursor/hooks/`) so checkout can never
  remove it, regardless of gitignore state.
- **Requires a Git repo.** Every command resolves the repo via `git`; running
  outside a repository errors out. Snapshots use a shadow index
  (`GIT_INDEX_FILE`), so your real staging area is never touched.
- **Long sessions grow the DB.** No compaction/pruning of `.profact/history.db`
  or the object store yet (acceptable for thesis-scale use).
- **HTTPS/corporate proxies.** The Claude proxy does clean forwarding to
  `api.anthropic.com` only; custom SSL/enterprise gateways are not handled.

### Out of scope for the MVP (by design)
Copilot/ChatGPT adapters, shared/mergeable history, mid-session pull of a
foreign branch, semantic prompt diffing / `profact diff`, `replay`/`blame`, and
any GUI. The architecture leaves room for these but does not implement them.
