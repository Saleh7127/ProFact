# profact — Development Plan

Companion to `profact_spec.md`. The spec defines *what* and *why*; this plan defines *how* and *in what order*. Phases are sequential and gated: do not start a phase until the previous one's exit test passes.

---

## Stack & dependencies

| Concern | Choice | Why |
|---------|--------|-----|
| Language | Python 3.11+ | `subprocess`, `sqlite3`, typing in stdlib |
| CLI | `typer` | Declarative commands, matches spec |
| File watching | `watchfiles` | Rust-backed, debounced events |
| Proxy server | `starlette` + `uvicorn` | Async, native SSE streaming pass-through |
| HTTP forwarding | `httpx` (streaming) | `stream=True` preserves SSE chunks |
| DB | `sqlite3` (stdlib) | No dependency needed |
| Git | `subprocess` → real `git` | No Git library (spec §1 of v1, kept) |
| Tests | `pytest` | Fixtures for aggregator + proxy shapes |

`pyproject.toml` with these deps + a `profact` console-script entry point (`profact = "profact.cli:app"`).

---

## Target layout

```
profact/
  __init__.py
  store.py            # SQLite schema + version CRUD
  git_ops.py          # shadow-index write_tree, diff_trees, checkout_tree, current_branch, add_note
  aggregator.py       # turn-boundary rule, version lifecycle
  watcher.py          # watchfiles loop, quiet-period, human-origin detection
  cli.py              # thin Typer commands + orchestration
  log_view.py         # log ancestry/filtering/provenance/DAG rendering
  adapters/
    __init__.py
    base.py           # CaptureEvent dataclass, CaptureAdapter Protocol
    claude_code_proxy.py   # secondary adapter (built)
    cursor_hooks.py        # primary adapter (to build): consumes Cursor hook events
tests/
  test_aggregator.py
  fixtures/           # anonymized real Claude Code request shapes
pyproject.toml
```

---

## Phase 0 — Scaffolding (no logic)
- Create package layout above, `pyproject.toml`, install deps in a venv.
- `profact --help` runs and lists stub commands.
- **Exit:** `pip install -e .` succeeds; `profact --help` works.

## Phase 1 — Storage + Git primitives + `init` (spec §1, §3, §7, Phase 1)
- `store.py`: create DB from the spec schema; `create_version(...)`, `get_version(id)`, `list_versions(branch)`, HEAD read/write helpers.
- `git_ops.py`: `write_tree()` (shadow index via `GIT_INDEX_FILE`), `diff_trees(a,b)`, `checkout_tree(hash)`, `current_branch()`, `add_note(commit, payload)`. Each backed by `subprocess` against this repo.
- `cli.py` `init`: create `.profact/`, append `.profact/` to `.gitignore`, create DB; if current branch has commits → one `anchor` version + HEAD → it.
- **Exit test:** `profact init` on a repo with prior commits creates exactly one anchor, `.profact/HEAD` points to it, `.profact/` is in `.gitignore`. Each `git_ops` function verified against this repo.

## Phase 2 — Turn aggregation (pure logic, no network) (spec §4, Phase 2)
- `adapters/base.py`: `CaptureEvent`, `CaptureAdapter` Protocol.
- `aggregator.py`: turn-boundary rule (new turn on `user`+`text`; continuation on `user`+only `tool_result`); lifecycle open→accumulate→close→finalize; 60s idle finalize.
- Capture anonymized fixtures of real Claude Code request shapes into `tests/fixtures/`.
- **Exit test:** synthetic `[user-text, assistant, user-tool_result, assistant, user-tool_result, assistant, user-text]` → exactly **2** versions; fixture tests confirm the rule matches real payloads.

## Phase 3 — Claude Code proxy adapter (spec §9, Phase 3)
- `adapters/claude_code_proxy.py`: starlette app on `localhost:7845`, forward to `https://api.anthropic.com` with `httpx` streaming, pass SSE through unchanged while parsing message content; emit `CaptureEvent`s into the aggregator.
- Capture `tree_before` (via `git_ops.write_tree`) when a turn opens.
- **Exit test:** `ANTHROPIC_BASE_URL=http://localhost:7845 claude` → short multi-tool exchange → exactly **one** version opens; requests/responses behave normally; sanitized fixture saved.

## Phase 4 — Watcher + human-origin handling (spec §5, §6, Phase 4)
- `watcher.py`: `watchfiles` loop; on turn close, quiet-period (~2s, 30s cap) then snapshot `tree_after` + compute diff + finalize.
- File writes with **no** open turn → finalize a `human`-type version (parent=HEAD).
- Concurrent writes during an open turn → `confidence='uncertain'`.
- **Exit test:** (a) one prompt editing 3 files → `tree_after` + diff populate after settle; (b) a manual edit with no active prompt → a `human` version exists (nothing lost).

## Phase 5 — Adapter-agnostic CLI (spec Phase 5 — the project goal)
Build the read/navigate commands first; they touch only `store` + `git_ops` and work for **any** adapter (Cursor or Claude). This is the highest-value, lowest-risk work and is unblocked by the adapter pivot.
- `log [--graph] [--path]` — branch-scoped, mark HEAD, compact labels for `anchor`/`checkpoint`/`human`.
- `show <id>` — prompt/response/diff/confidence (clear `uncertain` warnings).
- `checkout <id>` — spec §8: auto-checkpoint → restore tree → move HEAD; preserve ignored files.
- `status` — current HEAD, branch, working-dir state.
- **Exit test:** seed versions via the store, then run the full DoD sequence — v1→v2, checkout v1, (synthetic) prompt→v3 (parent=v1), `log --graph` shows both branches, `checkout v2` restores, v3 work intact.

## Phase 6 — Cursor Hooks adapter (primary capture)
- `adapters/cursor_hooks.py` + a `profact hook <event>` entry point that Cursor invokes per event (reads hook JSON from stdin).
- `profact start` (Cursor mode): install/merge `.cursor/hooks.json` for `beforeSubmitPrompt` / `afterAgentResponse` / `afterFileEdit` / `sessionStart` / `sessionEnd`; run the long-lived profact session the hooks feed into. (See open design question: hook→session process model.)
- Map hook events directly to store writes (stateless, LOCKED (b)): `beforeSubmitPrompt` opens a turn + snapshots `tree_before` + advances HEAD; `afterAgentResponse` records the response text; `stop`/`sessionEnd` snapshot `tree_after` + diff and finalize. The open turn = the HEAD `prompt` version whose `tree_after` is NULL. A new `beforeSubmitPrompt` also finalizes any dangling open turn (covers a missing `stop`). No turn-boundary heuristic, no time-window watcher needed on this path.
- Diffs come from Git-tree snapshots (same as the proxy path), so `afterFileEdit` is **not** wired — it would be redundant. Reserved for future per-edit attribution.
- Hooks must fail open: `profact hook` always exits 0 with valid JSON, even on bad input or an uninitialized repo.
- **Exit test:** a real Cursor prompt that edits files produces exactly one `prompt` version with correct `tree_before`/`tree_after` and diff; `log`/`show`/`checkout` (Phase 5) operate on it unchanged. **DONE** — validated via unit tests + an end-to-end run piping hook JSON through the generated `.cursor/hooks/profact-hook.sh`.

## Phase 7 — `start` for Claude + polish (spec Phase 6) — DONE
- `profact start --claude`: launch `claude` with a local transcript monitor,
  grouping typed prompts and complete tool loops by Claude's durable
  `sessionId`/`promptId`. The earlier API proxy remains covered as compatibility
  infrastructure but is not used for turn boundaries.
- `profact note <commit> [version]`: optional Git Notes link (`git notes --ref=profact`), defaults to HEAD. Light touch, fires only when the user asks.
- **Exit:** DoD passes via the Cursor path (Phase 6) and the Claude path (proxy tests + `run_claude` wiring test). 29 tests green.

---

## Cursor hook → session process model — LOCKED: (b) stateless hooks + DB
Cursor hooks are **short-lived processes invoked once per event**, so they cannot hold session state in memory across a turn. **Decision: option (b).** Because hooks supply explicit turn boundaries, there is almost no aggregation state — the only thing carried between events is *which version is currently open* (HEAD + the row whose `tree_after` is still null). Each `profact hook <event>` invocation is a small **direct `store.py` write**:
- `beforeSubmitPrompt` → create the `prompt` version, snapshot `tree_before`, advance HEAD.
- `afterAgentResponse` → write `response_ref`, snapshot `tree_after`, compute diff, finalize.

No daemon, no socket, no long-lived process to manage (avoids the proxy's orphaned-process risk). SQLite (WAL) handles concurrent `afterFileEdit` writes, and crash-safety is free — the DB always reflects exactly the events that completed.
- Rejected (a) daemon: reintroduces lifecycle/IPC/cleanup risk; its main benefit (reusing the in-memory aggregator) is moot once hooks provide explicit boundaries.

## Critical-path risks (watch these, don't pre-solve)
1. **Hook process model (Phase 6)** — see open design question above; decides the whole Cursor adapter shape.
2. **SSE pass-through (Phase 3, done)** — must stream chunks, not buffer, or `claude` hangs.
3. **Real request shape (Phase 2/3)** — validate fixtures before trusting the proxy parser.
4. **Process lifecycle (Phase 6/7)** — `start` must clean up daemon/proxy/watcher on exit.

## Build order summary
`0 scaffold → 1 storage/git/init → 2 aggregator → 3 proxy → 4 watcher → 5 adapter-agnostic CLI → 6 Cursor hooks adapter (primary) → 7 Claude start + polish`. **All phases (1–7) complete.** Each gate is a manual run, not just unit tests.
