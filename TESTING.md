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

Expected: **59 passed**. Coverage by area:

| File | What it proves |
|------|----------------|
| `test_aggregator.py` | turn-boundary rule (1 user prompt = 1 version) |
| `test_proxy.py` | Claude proxy forwards + parses SSE, opens one version |
| `test_watcher.py` | quiet-period finalize + `human` version creation |
| `test_cli.py` | `log`/`show`/`status`/`checkout`/`diff`, full branching DoD |
| `test_cursor_hooks.py` | Cursor hook events → versions; `start` installs hooks |
| `test_phase7.py` | `note` (Git Notes) and `start --claude` wiring |
| `test_branch_heads.py` | per-branch HEADs, carry-over, cross-branch checkout prompt, `doctor` |
| `test_blame.py` | line→version attribution, reverts/re-adds, branch ancestry, `blame`/`why` |

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

Compare versions with `profact diff` (read-only; works across git branches):

```bash
profact diff v1 v2          # changes to turn v1 into v2
profact diff v2             # what changed since v2 (v2 vs current working tree)
profact diff                # HEAD vs working tree (uncommitted work), like `git diff`
profact diff v1 v2 --stat   # diffstat summary
profact diff v1 v2 --name-only
profact diff HEAD           # 'HEAD' is accepted as a version alias
```

Branch backward and confirm nothing is lost:

```bash
# (after checkout v1) create a different v3 from v1, then hop between branches
profact checkout v2         # button.py is blue again; v3 still exists in log
```

---

## 4. Git branches — per-branch HEADs with carry-over

profact history is **branch-local**: each Git branch has its own HEAD, and a new
Git branch **carries over** the history up to the point you branched off (like
Git itself), then diverges into a fork.

Continuing in the `/tmp/pf-demo` repo from section 3 (assume `main` has
`v1(anchor) → v2`), create a Git branch and add a version on it:

```bash
git checkout -b experiment
python - <<'PY'
from pathlib import Path
from profact.adapters import cursor_hooks
repo = Path("/tmp/pf-demo")
def ev(name, **k): return {"hook_event_name": name, "generation_id": "g1", **k}
cursor_hooks.handle(repo, ev("beforeSubmitPrompt", prompt="experiment work"))
(repo/"exp.py").write_text("x = 1\n")
cursor_hooks.handle(repo, ev("afterAgentResponse", text="Done."))
cursor_hooks.handle(repo, ev("stop"))
PY

profact log     # shows v1, v2 (carried over, labeled (main)) + v3 (experiment)
```

The `(branch)` column shows where each version was created — the shared trunk
stays `(main)` while your new work is `(experiment)`:

```
v3    HEAD [prompt    ] (experiment)   experiment work
v2         [prompt    ] (main)         make the button blue
v1         [anchor    ] (main)         start of history @ ...
```

`profact log` is a terse flat list. `profact log --graph` draws the actual DAG
with ASCII lanes (`*` = a version, `|` = a lane, `/` = lanes converging on a
shared parent), plus a `tips:` / `branch points:` footer — most useful once
history forks:

```
* v5    HEAD [prompt    ] (experiment)   ...
|* v4        [prompt    ] (main)         ...
*| v3        [prompt    ] (main)         ...
|/
* v2         [prompt    ] (main)         ...
* v1         [anchor    ] (main)         ...
```

Now go back to `main` and confirm the branches stay independent:

```bash
git checkout main
profact log     # shows only v1, v2 — experiment's v3 is NOT here
profact status  # HEAD is back at main's own tip (v2), not v3
```

Prompt again on `main` and it forks off the shared branch point (v2), not off
experiment — `v1 → v2 → v4` on main, `v1 → v2 → v3` on experiment.

**Cross-branch checkout asks first.** A version is "part of this branch" if it
was created here or is a carried-over ancestor. Restoring a version that lives
only on another branch would make your Git branch and files disagree, so profact
prompts before doing it instead of silently proceeding:

```bash
profact checkout v3
# note: 'v3' is not part of branch 'main' history (it was created on
# 'experiment' and isn't an ancestor of this branch).
# Check it out into 'main' anyway? [y/N]:
```

- Answer **n** (default) → aborts, nothing changes.
- Answer **y** → brings the version into the current branch (auto-checkpoints
  first, restores the tree, moves this branch's HEAD to it). It then appears in
  `profact log` for this branch so HEAD is never a dangling reference.
- Use `profact checkout <id> --force` (or `-f`) to skip the prompt in scripts.

Shared-trunk versions (e.g. `v1`, `v2`) are part of the branch, so they check out
directly with no prompt.

---

## 5. Prompt blame — trace each line back to its intent

`profact blame <file>` answers "why is this line here?" by attributing every
line to the **version (and prompt) that last wrote it** — the AI-native analogue
of `git blame`, but the "commit" is a prompt.

Continuing in `/tmp/pf-demo` (from section 3, `button.py` was written by a
prompt version), blame it:

```bash
profact blame button.py
```

```
blame button.py @ v2 [prompt]
v2    prompt 2026-07-22 make the button blue              1| color = 'blue'
```

Each row is: `version · type · date · prompt summary (origin) · line# | code`.
Origins are honest (Tier 3): a prompt shows its text, pre-existing lines show
`pre-existing (before profact)` (attributed to the `anchor`), and manual edits
show `human edit (no prompt)`.

Drill into a single line's full intent (Tier 2) — the prompt, a response
excerpt, and the exact hunk that introduced it:

```bash
profact blame button.py --show     # expand every contributing version
profact why button.py:1            # sugar for one line: blame -L 1:1 --show
profact blame button.py -L 1:1     # restrict to a line range (N or N:M)
```

Blame is read-only and time-travels: pass a version to blame a **historical**
snapshot (`profact blame button.py v2`), and it follows the branch ancestry, so
carried-over trunk lines are credited to the branch that actually wrote them.

> Blame attributes a version's **snapshot**, not unsaved edits in your working
> tree. To blame current work, checkpoint/finalize it first.

Under the hood profact replays the target's lineage as a throwaway chain of Git
commit objects over the stored trees and runs Git's own blame engine on it, so
reverts, re-additions, and moved lines all resolve correctly. Those commits are
unreachable and never enter your real history.

---

## 6. Real capture path A — Cursor (primary)

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

## 7. Real capture path B — Claude Code (secondary)

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

## 8. Optional — link a version to a Git commit

```bash
git commit -m "ship login"
profact note HEAD          # writes git notes --ref=profact mapping the commit -> HEAD version
git notes --ref=profact show HEAD
```

---

## 9. Maintenance — `profact doctor`

`doctor` scans `.profact/history.db` for safe-to-remove artifacts and reports
them. It changes nothing unless you pass `--fix`.

```bash
profact doctor            # report only
profact doctor --fix      # prune the safe items it found
```

It looks for:

- **Phantom checkpoints** — `checkpoint` versions whose diff is empty or touches
  only profact's own infrastructure (`.profact/`, `.cursor/hooks*`). These are
  artifacts of an older snapshot bug; `--fix` rewires any children onto the
  checkpoint's parent, moves any HEAD off it, and deletes it.
- **Orphaned objects** — blobs under `.profact/objects/` no longer referenced by
  any version; `--fix` deletes them to reclaim space.
- **Dangling open turns** — `prompt` versions never finalized (`tree_after`
  NULL), e.g. a session that crashed before `stop`. These are **reported only**
  and never deleted (they hold real work and finalize on the next prompt).

`doctor` never touches `prompt`/`anchor`/`human` versions or any checkpoint that
recorded real source changes.

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
- **Branch-scoped history with carry-over.** Each Git branch has its own HEAD.
  `profact log` shows the current branch's own versions **plus the carried-over
  trunk** it branched from (the `(branch)` column shows each version's origin). A
  new Git branch inherits history up to its branch point and diverges from there.
  Checking out a version that lives only on another branch **prompts for
  confirmation** (or `--force`) rather than proceeding silently. There is still no
  *merge* of divergent profact history across branches.
- **Carry-over seed can be imprecise without profact activity.** A new branch's
  starting point is seeded from the repo-wide "last active" version. In the normal
  flow (work on a branch, then `git checkout -b new`, then prompt) this is exactly
  the branch tip. But if you `git checkout` to a *stale* branch and immediately
  branch off it **without** any profact-visible work in between, the seed may be
  the last globally-active version instead of that branch's tip — profact has no
  Git post-checkout hook to observe raw branch switches.
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
- **Checkout never deletes the hooks, and snapshots never capture them.** Because
  `.gitignore` is itself versioned, checking out a version created *before*
  `profact start` restores an old `.gitignore` without the `.cursor` entries.
  profact hard-protects its own infrastructure (`.profact/`, `.cursor/hooks.json`,
  `.cursor/hooks/`) on two fronts, regardless of gitignore state: (1) `checkout`
  never deletes it, and (2) `write_tree` removes it from every snapshot. Without
  (2), a checkout would re-capture the (undeletable) hook files, drift the tree,
  and invent phantom `checkpoint` versions from work you never did.
- **Requires a Git repo.** Every command resolves the repo via `git`; running
  outside a repository errors out. Snapshots use a shadow index
  (`GIT_INDEX_FILE`), so your real staging area is never touched.
- **Long sessions grow the DB.** No compaction/pruning of `.profact/history.db`
  or the object store yet (acceptable for thesis-scale use).
- **HTTPS/corporate proxies.** The Claude proxy does clean forwarding to
  `api.anthropic.com` only; custom SSL/enterprise gateways are not handled.

### Out of scope for the MVP (by design)
Copilot/ChatGPT adapters, shared/mergeable history, mid-session pull of a
foreign branch, semantic prompt diffing, `replay`, and any GUI. (`profact diff`
and `profact blame`/`why` are now implemented — see sections 3 and 5.) The
architecture leaves room for the rest but does not implement them.
