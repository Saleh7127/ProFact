# profact — MVP Build Spec (Consolidated)

> Project: **profact** (Vibe Coding Version Controlling). A local CLI that gives AI-assisted development a navigable version history whose atomic unit is a **(prompt, artifact) pair**, not just a code snapshot.
>
> This spec consolidates v1 + v2 and locks the open design decisions. Work through the Phases in order; do not skip ahead. Each phase must be manually tested and working before the next begins.

---

## CONTEXT

When developing with Claude Code, a developer iterates through many prompts to build a feature. Git only preserves the final code — the reasoning trail (what was asked, in what order, with what model configuration) is lost when the session ends. `profact` captures that trail automatically and makes it navigable, the way `git log` and `git checkout` make code history navigable.

Every time you send a prompt to Claude Code, `profact` records the **prompt text, the model configuration, and the exact file changes that resulted**, storing them together as a single **version**. You can move backward and forward through this prompt-driven history like Git commits — except the unit of history is intent + its result.

**MVP scope:** single user, local machine, one active session at a time. The project being versioned is itself a Git repository. Two capture adapters are in scope:
- **Cursor (primary)** — captured via **Cursor Hooks** (`beforeSubmitPrompt`, `afterAgentResponse`, `afterFileEdit`, `sessionStart`/`sessionEnd`). This matches how the tool is actually used and gives explicit turn boundaries + exact edit attribution.
- **Claude Code (secondary, already built)** — captured via the local HTTP proxy (`ANTHROPIC_BASE_URL`). Kept as a working second adapter to prove the capture interface generalizes.

The adapter-agnostic core (store, git_ops, aggregator, CLI) is shared by both.

**Future (not built now):** Copilot / ChatGPT adapters, team collaboration, shared/committed prompt history, replay, and semantic diffing. The architecture should not block these, but none are built in the MVP.

---

## LOCKED DECISIONS

These were debated and are now final for the MVP. Do not re-litigate; implement as written.

| # | Question | Decision |
|---|----------|----------|
| A | Backward navigation + new changes | New version, `parent_id` points to the version it branched from (DAG, never overwrite) |
| B | Versioning scope: per Git branch or global | **Branch-scoped with carry-over.** Each version records its `git_branch`; each Git branch has its own profact HEAD. A new Git branch **inherits** the history up to the point it branched off (like Git itself) and diverges from there; `profact log` shows the current branch's versions plus their carried-over ancestors |
| C | Storage committed or gitignored | **Gitignored.** `.profact/` is added to `.gitignore` on `init`. Sharing/committing history is future work |
| D | Init on existing project | Start at init point: insert one `anchor` version at the current commit; do not fabricate past prompts |
| E | Multiple API calls per user prompt | One user-typed message = one version (turn-boundary rule). Tool-loop API calls are absorbed; the final artifact is the version |
| F | API ↔ file-change correlation | Deterministic Git-tree snapshot immediately before the turn and after file writes settle |
| G | Manual edits between prompts | **Recorded, not lost.** Pre-prompt edits are excluded from the AI diff via `tree_before`; standalone human edits become a `human`-type version |
| H | Unwitnessed history | Accepted limitation. Mark with an `anchor`; never guess prompt content |
| I | Mid-session pull of a foreign branch | **Out of scope** for MVP |
| J | DB growth over long sessions | **Out of scope** for MVP (thesis, not production release) |
| K | Project name | `profact` |

---

## ARCHITECTURE RULES

### 1. The version model
Every version has a `parent_id`, forming a DAG — never a flat list, never overwritten.
- `type` is one of:
  - `prompt` — a real AI turn (may have an empty diff if nothing changed)
  - `anchor` — marks the start of witnessed history; no prompt content
  - `checkpoint` — an automatic safety snapshot before a destructive checkout; no prompt content
  - `human` — file changes made without any AI turn (manual edits); no prompt content
- Going back to an earlier version and making new changes always creates a **new** version whose `parent_id` is the version it branched from — the old line is never overwritten.

### 2. HEAD — current position
`profact` maintains a current version id like Git's HEAD, but the value is **branch-local**:
- Each Git branch has its own profact HEAD, stored in SQLite (a `heads` table keyed by `git_branch`). There must not be one global HEAD shared by all Git branches.
- Every new version's `parent_id` is read from the current Git branch's profact HEAD, not from "whatever was created most recently" and not from another Git branch's HEAD.
- **Carry-over seeding:** when a Git branch has no HEAD of its own yet (freshly created), its HEAD is seeded from the repo-wide "last active" pointer (the legacy `.profact/HEAD` file, kept in sync on every HEAD move). This makes the new branch inherit the version you branched off from, so its first new version chains onto that lineage — mirroring how a Git branch shares its parent's commit history.
- `checkout <id>` is the only command that moves the current Git branch's profact HEAD manually.
- After a new version (prompt or human) is finalized, the current Git branch's profact HEAD advances to it automatically.

This is what makes "go back to v2, prompt again, get a new branch, old branch still there" actually work.

### 3. Branch-scoped history with carry-over (Decision B)
- Each version stores its `git_branch` (captured at creation time).
- `profact log` shows the versions **created on the current Git branch plus their ancestors** (the ancestors may carry a different `git_branch` because they were inherited from the branch you forked off). Concretely: take all rows with `git_branch = current`, then walk `parent_id` upward to reattach the shared trunk. A plain `git_branch =` filter is **not** sufficient — it would drop the carried-over history.
- On `profact init`, if the current Git branch has prior Git commits → insert one `anchor` version at the current commit/current working tree and point this Git branch's profact HEAD to it.
- **New Git branch (carry-over):** when you `git checkout -b X` and then produce your first version on `X`, profact does **not** start a blank line. `X`'s HEAD is seeded from the branch point (see §2 carry-over seeding), so the new version's `parent_id` is the tip you branched off. `X` and the original branch then share the common trunk and diverge — a fork in the DAG, exactly like Git commits.
- Switching Git branches switches both the visible versions and the active profact HEAD; each branch's HEAD moves independently.
- Cross-branch checkout is guarded in the MVP: if `target.git_branch` does not match the current Git branch, `profact checkout <id>` must refuse by default and tell the user to `git switch <target.git_branch>` first. Applying one Git branch's profact snapshot onto another Git branch is future work and must require an explicit command/flag.

### 4. One user prompt = one version (turn-boundary rule, Decision E)
Claude Code makes multiple API round trips per user-visible prompt (read file → edit file → run tests). The proxy must **group these into a single version**:

- Track the message array sent on each request to the proxy within the current `profact start` session.
- A request is a **new turn** (close the previous open version, open a new one) if the newly appended message has role `user` **and** contains a `text` content block (a real human-typed message).
- A request is a **continuation** of the current open version (do not create a new version) if the newly appended message has role `user` but contains **only** `tool_result` content blocks (Claude Code feeding tool output back to the model).
- When the session ends or goes idle for 60s, finalize whatever version is still open.
- **Validate against reality before locking the parser:** capture a small, anonymized fixture set from real Claude Code traffic (keep roles and content-block types; strip prompt text, file names, secrets, responses) and confirm the rule holds. If the real payload shape differs, update this rule and the fixtures first — do not guess.

### 5. File-change capture and correlation (Decision F)
- Use `git write-tree` for before/after snapshots — never `git diff HEAD` or `git rev-parse HEAD` (Claude Code writes files without committing, so HEAD-based diffing loses uncommitted work).
- **Use a temporary shadow index** (`GIT_INDEX_FILE=.profact/tmp_index`) for every snapshot. Stage the full working-directory state into this shadow index (respecting `.gitignore`), then `write-tree` from it. This captures uncommitted and untracked changes **without ever touching the user's real staging area**.
- `tree_before` is captured the moment a turn opens. `tree_after` is captured once the turn closes (§4), **after** the watcher confirms a quiet period (no file writes for ~2s, capped at 30s) — not on a fixed clock from the API response.
- The diff for a version is `git diff <tree_before> <tree_after>`.

### 6. Human-origin attribution (Decision G — included in MVP)
The goal: never silently lose a change and never attribute human work to the AI.

- **Pre-prompt manual edits are excluded automatically.** Because `tree_before` is snapshotted when the prompt turn opens, any manual edit made before the prompt is already in `tree_before` and therefore not part of the AI diff.
- **Standalone human edits become a version.** If the watcher detects file changes while **no** AI turn is open, finalize a `human`-type version (no prompt/response, `parent_id` = HEAD, diff captured). This keeps the timeline continuous.
- **Concurrent edits are flagged.** If file writes occur while an AI turn is open from a source that looks human (activity outside the expected window), mark the version `confidence='uncertain'` and surface the warning in `profact show`. Never hide it.
- A prompt that produces no file changes is valid: `tree_after == tree_before`, empty `diff_ref` (see Decision in §1 / Concern 14).

### 7. Storage defaults (Decision C)
- `.profact/` is added to `.gitignore` automatically on `init` — **not committed**. Prompts/responses may contain secrets, and the MVP is single-user/local, so there is no collaboration need that justifies the risk yet.
- SQLite database at `.profact/history.db` for structured version metadata.
- Full prompt/response text and diffs stored as flat files under `.profact/objects/<hash>`, referenced by hash from SQLite, keeping the DB small.
- No external services, no cloud, no network calls except to `api.anthropic.com` (via the proxy).

### 8. Checkout — must never lose work
```
checkout(target_id):
    1. Snapshot current working directory via shadow index → current_tree
    2. Load HEAD for the current Git branch.
    3. Refuse if target_id belongs to a different Git branch.
    4. If current_tree != tree_after(HEAD):
           create a `checkpoint` version: parent=HEAD, git_branch=current Git branch, tree_after=current_tree
           (preserves whatever the user had, untracked/uncommitted, before moving)
    5. Restore target_id's tree_after into the working directory:
           - git read-tree <target_tree> (into shadow index)
           - git checkout-index -a -f (write files into working dir)
           - delete files present now but absent in target_tree
    6. Write target_id into the current Git branch's profact HEAD
```
- **Deletion rule:** only reconcile files that are part of the shadow-index snapshot domain. Files ignored by `.gitignore` are not in these trees and must be **left alone** (e.g. `.env`, build caches, editor files, dependency dirs). A `--clean-ignored` flag may be added later, but MVP checkout preserves ignored local files.
- **Infrastructure protection:** profact's own capture machinery (`.profact/`, `.cursor/hooks.json`, `.cursor/hooks/`) is hard-protected from deletion during checkout. `.gitignore` is itself versioned, so checking out a version created before `profact start` would otherwise revert the ignore rules and let a subsequent checkout delete the hook files — silently disabling capture. These paths are skipped unconditionally, independent of gitignore state. `profact start` also adds the `.cursor/hooks*` entries to `.gitignore` (they are machine-specific).

### 9. Capture is behind an adapter interface (build one, design for more)
```python
@dataclass
class CaptureEvent:
    role: str            # "user" | "assistant"
    content_blocks: list # raw content blocks from the API message
    model: str | None
    temperature: float | None
    timestamp: str

class CaptureAdapter(Protocol):
    def start(self) -> None: ...
    def on_event(self, callback: Callable[[CaptureEvent], None]) -> None: ...
    def stop(self) -> None: ...
```
For MVP, implement **two** adapters emitting the same `CaptureEvent`s:
- `ClaudeCodeProxyAdapter` (built) — local HTTP proxy; turn boundaries via the §4 aggregator, edit attribution via the watcher.
- `CursorHooksAdapter` (primary, to build) — receives Cursor hook events. Because hooks give explicit boundaries (`beforeSubmitPrompt` opens a turn, `afterAgentResponse` closes it) and exact edits (`afterFileEdit`), this adapter needs neither the heuristic turn-boundary parser nor the time-window watcher for capture — it still snapshots `tree_before`/`tree_after` via `git_ops` so `checkout` works.

The turn-aggregation logic (§4) lives in a separate `aggregator.py`, not in the adapter, so both adapters reuse the shared core unchanged.

### 10. Git integration (light touch)
- Optionally, after a version is associated with a Git commit, write a Git Note: `git notes --ref=profact add -m '{"version_id": "<id>"}' <commit>`.
- This is the only Git history modification — never rewrite commits, never force-push, never touch the user's commit graph. It fires only if/when the user commits; do not oversell it.

---

## TASK — Build in This Order

### Phase 1 — Storage, Git primitives, and `init`
Build `store.py` (schema below), `git_ops.py` (shadow-index `write_tree()`, `diff_trees(a,b)`, `checkout_tree(hash)`, `current_branch()`, `add_note(commit, payload)` — using `subprocess` on real `git`, no Git library), and the `profact init` command: create `.profact/`, add it to `.gitignore`, create the database, and — if the current branch has prior commits — insert one `anchor` version at the current commit with `.profact/HEAD` pointing to it.

```sql
CREATE TABLE versions (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT,
    type         TEXT NOT NULL DEFAULT 'prompt',  -- 'prompt' | 'anchor' | 'checkpoint' | 'human'
    git_branch   TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    prompt_ref   TEXT,
    response_ref TEXT,
    model        TEXT,
    temperature  REAL,
    diff_ref     TEXT,
    tree_before  TEXT NOT NULL,
    tree_after   TEXT,
    commit_hash  TEXT,
    confidence   TEXT DEFAULT 'high'
);
```
**Test:** `profact init` on a repo with prior commits creates exactly one anchor version, `.profact/HEAD` points to it, and `.profact/` appears in `.gitignore`.

### Phase 2 — Turn aggregation (pure logic, no networking)
Build `aggregator.py` implementing the turn-boundary rule (§4) and version lifecycle (open → accumulate tool-result events → close → finalize). Feed it synthetic `CaptureEvent` sequences. Add anonymized real-shape fixture tests before Phase 3 is considered complete.

**Test:** a synthetic sequence `[user-text, assistant, user-tool_result, assistant, user-tool_result, assistant, user-text, ...]` produces exactly **2** versions, not 7.

### Phase 3 — The Claude Code proxy adapter
Build `adapters/claude_code_proxy.py`: an HTTP server on `localhost:7845` forwarding to `https://api.anthropic.com`, passing through streamed (SSE) responses while parsing them to extract message content, emitting `CaptureEvent`s into the aggregator.

**Test:** `ANTHROPIC_BASE_URL=http://localhost:7845 claude`, run a short multi-tool-call exchange, confirm exactly **one** version opens for that one prompt. Save a sanitized fixture proving the observed API shape matches the parser.

### Phase 4 — The watcher (with human-origin handling)
Build `watcher.py` using `watchfiles`. When the aggregator closes a turn, begin quiet-period detection (~2s, 30s cap), then snapshot `tree_after` via the shadow index and compute the diff. If file writes occur with **no** open AI turn, finalize a `human`-type version (§6). If writes occur concurrently with an open turn, mark `confidence='uncertain'`.

**Test:** (a) a single prompt editing 3 files populates `tree_after` + diff after activity settles; (b) a manual edit with no active prompt produces a `human` version, not a lost change.

### Phase 5 — Adapter-agnostic CLI commands (the project goal)
These read from `store` + `git_ops` and work for **any** adapter, so they are built first.
```bash
profact init                    # Phase 1
profact log [--graph] [--path] [--tests]  # versions on the CURRENT git branch; --graph shows the DAG + HEAD; --path = HEAD ancestry only; --tests prefixes each with its test outcome
profact show <id>               # full prompt, response, diff, confidence (warns on 'uncertain')
profact diff [<a> [<b>]] [--stat|--name-only] [--exit-code]  # compare two versions, a version vs working tree, or (no args) HEAD vs working tree; read-only, cross-branch OK
profact blame <file> [<id>] [-L N:M] [--show]  # attribute each line to the prompt/version that last wrote it; --show expands prompt+response+hunk; blames any version, follows branch ancestry
profact why <file>:<line> [<id>]  # sugar for `blame <file> -L line:line --show`: full intent behind one line
profact bisect --good <id> [--bad <id>] -- <cmd>  # binary-search the lineage for the version that broke a test (exit 0=good, 125=skip, else bad); non-destructive
profact test [--version <id>] -- <cmd>  # run a test command; record pass/fail/error on the version (HEAD by default); shown in show/status/log --tests
profact checkout <id> [-f]      # §8 — auto-checkpoint, restore tree, move HEAD; preserves ignored files; prompts before a cross-branch target (--force skips)
profact status                  # current HEAD id, branch, dirty-vs-HEAD, any open/in-progress version
profact doctor [--fix]          # report/prune history artifacts: phantom checkpoints, orphaned objects (dangling turns report-only)
```

### Phase 6 — Cursor Hooks adapter (primary capture) + `start`
```bash
profact start                   # Cursor mode: install/merge .cursor/hooks.json, run the session the hooks feed
profact start --claude          # Claude mode: set ANTHROPIC_BASE_URL, launch proxy + watcher, exec `claude`
```
Build `adapters/cursor_hooks.py` + a hidden `profact hook` entry point Cursor calls per event (event name read from the stdin payload). Map hook events into the store: `beforeSubmitPrompt` opens a turn + snapshots `tree_before` + advances HEAD; `afterAgentResponse` records the response text; `stop`/`sessionEnd` snapshot `tree_after` + diff and finalize. `afterFileEdit` is not wired (diffs come from tree snapshots; reserved for future per-edit attribution). No turn-boundary heuristic and no time-window watcher are needed on this path.

**Hook process model — LOCKED: stateless hooks + DB (no daemon).** Cursor hooks are short-lived processes, so each `profact hook <event>` invocation is a small direct `store.py` write. The only state carried between events is which version is currently open (HEAD + the row whose `tree_after` is null). SQLite (WAL) serializes concurrent `afterFileEdit` writes; crash-safety is free since the DB always reflects completed events.

**Test — the actual goal of this whole project:**
```bash
profact init && profact start
# prompt 1: create a login page         → v1
# prompt 2: change the button to blue   → v2
# exit
profact checkout v1
profact start
# prompt 3: change the button to green  → v3, parent=v1
# exit
profact log --graph
# v1 → v2 (old branch, untouched)
#   └─→ v3 (new branch, from v1)
profact checkout v2
# button is blue again — v3's green work was never lost, it's just not HEAD
```

### Phase 7 — Polish
Git Notes integration (§10, optional, fires only on commit — document, don't oversell). `profact show` clearly displays `uncertain` confidence warnings. Compact labels in `log` for `anchor` / `checkpoint` / `human` versions (they have no prompt text).

---

## OUT OF SCOPE — Not Building Now

- Copilot / ChatGPT adapters (interface is ready; adapters are not built)
- Committed / shared / mergeable `.profact/` history (Decision C); team collaboration
- Mid-session pull of a branch with foreign history (Decision I)
- DB compaction / pruning for very long sessions (Decision J)
- Semantic prompt diffing, embeddings, `profact diff <id1> <id2>`
- `profact replay` (`profact blame`/`why` are now implemented — see the CLI list)
- HTTPS/SSL handling for restricted corporate networks beyond clean forwarding to `api.anthropic.com`
- Any GUI or web dashboard

If asked to build any of these mid-phase, redirect back to the current phase.

---

## DEFINITION OF DONE

The Phase 5 test sequence runs end to end: branching backward from v1 after v2 exists produces a new, separate line of history (v3) without deleting or overwriting v2, and `checkout` between them never loses work in either direction. Additionally:
- `profact log` and profact HEAD are branch-local; a new Git branch carries over the history up to its branch point and diverges from there (shared trunk, forked tips).
- `profact checkout <id>` prompts for confirmation before restoring a version that is not part of the current branch's history (created on another branch and not a carried-over ancestor); `--force` skips the prompt. Confirming brings the version into the current branch (auto-checkpoint, restore tree, move HEAD) and it becomes visible in `profact log`.
- A manual edit made with no active prompt is preserved as a `human` version (nothing in the middle is lost).
- `profact log --graph` shows branches and HEAD; `anchor` / `checkpoint` / `human` versions are discoverable.
- `checkout` preserves ignored local files such as `.env`.
