# profact — Ideas & Roadmap

> Status: **living roadmap.** Completed items are marked below; unmarked items
> remain proposals. This document collects directions to grow the current MVP
> into a full product and a strong thesis. The MVP already delivers: capture
> (Cursor hooks + Claude proxy),
> including model **reasoning capture** (`afterAgentThought`) alongside each
> prompt/response/diff, content-addressed storage, a prompt DAG with per-branch
> carry-over HEADs, safe `checkout`, `log/show/status/diff`, `blame`/`why`,
> `bisect`, `test` (outcome recording), repository-wide `tag`, `note`, and
> `doctor`.
>
> Ideas were drafted here and cross-checked against a GPT-5.5 brainstorm
> ([feature brainstorm](ee5efa3b-3992-419d-b8c7-78b216e6fe66)).

---

## 1. Product vision

**"Git for AI intent."** Not a replacement for Git — the missing history layer
between *prompts*, *model behavior*, and *code artifacts*. Git records *what*
changed; profact records *why* (the prompt) and *how it was produced* (model,
response, settings), and lets you navigate, branch, and reason over that.

Three bets, in order:
1. Make the captured history **useful** (diff, blame, search, regression-hunt).
2. Make it **visible** (TUI / editor panel / local web dashboard).
3. Make it **shareable & research-grade** (redacted export, datasets, metrics).

---

## 2. Guiding principles

- **Local-first & private by default.** No data leaves the machine unless the
  user explicitly exports.
- **Never lose work.** Every operation is non-destructive (DAG, checkpoints).
- **Git-native, not Git-hostile.** Reuse Git plumbing; never touch the user's
  real index or force workflow changes.
- **Adapter-agnostic.** The core never assumes a specific AI tool.
- **Earn trust before sync.** Secret-safety and redaction come *before* any
  sharing feature.

---

## 3. Feature ideas by category

Effort/Impact are rough (Low / Med / High). ★ = strong thesis contribution.

### 3.1 Core version-control UX
- **`profact diff <a> <b>`** — ✅ **implemented.** Compare any two versions, a
  version against the working tree (`diff <a>`), or HEAD against the working tree
  (`diff`); `--stat` / `--name-only`; read-only and cross-branch. *Impact: High · Effort: Med.*
- **`profact tag <id> <label>`** — ✅ **implemented.** Repository-wide labels
  (`working`, `buggy`, `demo`, `regression-source`) resolve anywhere a version
  id is accepted; accidental moves are guarded by `--force`. *Impact: Med · Effort: Low.*
- **Named prompt-branches** — name divergent prompt lines independently of Git
  branches, so experiments don't each need a Git branch. *Impact: High · Effort: Med.*
- **Undo / redo stack** — human-friendly movement over the DAG for users who
  don't think in Git terms. *Impact: High · Effort: Med.*
- **Prompt-aware squash** — collapse a noisy chain of micro-prompts into one
  curated version with a synthesized summary. ★ *Impact: High · Effort: High.*
- **Replay a prompt on a new base** — re-run an old prompt (same model/settings)
  against the current tree; a genuinely new VC operation. ★ *Impact: High · Effort: High.*
- **Patch/bundle export** — export a version or chain (prompts + responses +
  metadata + diffs) for review or reproduction. *Impact: High · Effort: Med.*
- **Prune / archive** — compact low-value versions while keeping recoverability
  (extends today's `doctor`). *Impact: Med · Effort: Med.*

### 3.2 Navigation & discovery
- **`profact blame <file>`** — ✅ **implemented.** Attributes each line to the
  version/prompt that last produced it (Tier 1), labels honest origins —
  AI prompt / human edit / pre-existing (Tier 3), and `--show` / `profact why
  <file>:<line>` expands the full prompt + response excerpt + hunk (Tier 2).
  Blames any historical version and follows branch ancestry; reuses Git's blame
  engine over a throwaway synthetic-commit replay of the lineage. ★ *Impact: Very High · Effort: High.*
- **`profact bisect -- <test cmd>`** — ✅ **implemented.** Binary-searches the
  prompt lineage between a known-good and known-bad version, running a test
  command at each candidate (exit 0 = good, 125 = skip, else bad) to pinpoint the
  version that introduced a regression. Non-destructive (never creates versions
  or moves HEAD; restores the working tree); validates endpoints; handles skips.
  *Impact: Very High · Effort: Med.*
- **Line-origin search** — given a line/regex, find the version that added,
  changed, or removed it. *Impact: High · Effort: Med.*
- **Semantic prompt search** — search by meaning ("auth bug", "made UI
  responsive"), not just substring. ★ *Impact: High · Effort: High.*
- **Rich `log` filters** — ✅ **implemented.** AND-composable filtering by tag,
  type, Git branch, model substring, changed file/directory, age/ISO date, and
  test status. Existing ancestry `--path` and display-only `--tests` remain
  compatible via new `--file` and `--test-status` filter names.
  *Impact: High · Effort: Med.*
- **Rich `log` presentation** — ✅ **implemented.** Plain and graph modes expose
  timestamp, parent, diffstat, model, thinking/confidence, tags, tips and branch
  points; `--path` reports omitted side paths; `--tests` distinguishes untested
  versions and summarizes outcomes. *Impact: High · Effort: Med.*
- **"Why this changed"** — for a hunk, surface prompt + response excerpt + model
  metadata + neighboring versions in one view. *Impact: High · Effort: Med.*
- **Dead-end detection** — flag abandoned/reverted prompt branches. *Impact: Med · Effort: Med.*

### 3.3 Visualization & UI
- **Interactive TUI** — terminal DAG browser: previews, diffs, checkout, tags,
  filters. Best "first serious UX" with no server. *Impact: High · Effort: Med.*
- **Editor panel (VS Code / Cursor extension)** — prompt history beside the
  code, wrapping the local CLI. *Impact: Very High · Effort: High.*
- **Local web dashboard** — DAG navigation, diff panes, prompt/response reading,
  branch compare. Great for demos. *Impact: Very High · Effort: High.*
- **Session replay view** — step through a session prompt-by-prompt and watch
  files evolve; excellent for teaching, demos, and studies. ★ *Impact: High · Effort: High.*
- **DAG graph rendering** — ✅ **implemented.** ASCII lanes expose forks,
  convergence, tips, branch points, version types, origin branch, and HEAD.
  *Impact: High · Effort: Med.*
- **Diff heatmap** — which files churn most across turns. *Impact: Med · Effort: Med.*

### 3.4 Collaboration & sharing
- **Redacted history export** — share selected history after removing secrets,
  ignored paths, and private text. *Impact: High · Effort: High.*
- **`.profactbundle` format** — portable bundle (versions, objects, parent links,
  privacy metadata) so collaborators inspect lineage without committing
  `.profact/`. *Impact: High · Effort: High.*
- **Prompt-lineage code review** — reviewers see which prompts produced which
  hunks; AI changes become auditable. ★ *Impact: Very High · Effort: High.*
- **Optional team sync server** — private sync of prompt histories across a team.
  *Impact: High · Effort: High.*
- **Share a single version** — sanitized, permalink-style export for bug reports
  and paper artifacts. *Impact: Med · Effort: Med.*

### 3.5 Integrations (adapters & ecosystem)
- **More capture adapters** — Aider, Continue, Windsurf, Copilot, Cursor CLI, a
  generic OpenAI-compatible proxy, Anthropic SDK proxy. Delivers the
  IDE/CLI-agnostic promise. *Impact: Very High · Effort: High.*
- **Test-runner hooks** — ✅ **implemented.** `profact test -- <cmd>` runs a test
  command and records pass/fail/error on a version (content-addressed output),
  making history behavior-aware, not just diff-aware. Surfaced in `show`,
  `status`, and `log --tests`. Foundation for effectiveness scoring and
  bug-causing-prompt analysis. ★ *Impact: High · Effort: Med.*
- **Richer model metadata** — provider, model version, temperature, tools,
  context size, token counts (where available). *Impact: High · Effort: Med.*
- **Smarter Git-commit linking** — suggest attaching a whole prompt chain to a
  commit, not just current HEAD. *Impact: High · Effort: Med.*
- **CI provenance check** — verify generated code has prompt lineage and no
  unredacted secrets on PRs. *Impact: Med · Effort: High.*
- **Issue/ticket linking** — link versions to GitHub/Linear/Jira from prompts or
  branch names. *Impact: Med · Effort: Med.*

### 3.6 Analytics & insights
- **Session health summary** — end-of-session recap: prompts, branches, tests,
  churn, reverted work, likely-useful versions. Immediate payoff. *Impact: High · Effort: Med.*
- **AI churn metrics** — lines repeatedly rewritten across nearby versions;
  flags unstable code / poor prompt decomposition. *Impact: High · Effort: Med.*
- **Cost / token tracking** — tokens, latency, estimated cost per
  version/session. *Impact: High · Effort: Med.*
- **Prompt-effectiveness score** — did a prompt help? (tests passed, sane diff
  size, not later reverted, low churn). ★ *Impact: High · Effort: High.*
- **Model comparison** — replay equivalent prompts across models on the same
  base and compare. ★ *Impact: High · Effort: High.*
- **Bug-causing-prompt analysis** — correlate failed tests / reverted hunks /
  later fixes with earlier prompts. ★ *Impact: Very High · Effort: High.*

### 3.7 Research / thesis angles
- **Prompt-artifact dataset builder** — anonymized (prompt, response, diff,
  tests, outcome) datasets from real workflows. ★ *Impact: Very High · Effort: High.*
- **Reproducibility protocol** — define the metadata required to reproduce an AI
  coding turn; positions profact as research infrastructure. ★ *Impact: High · Effort: Med.*
- **Intent-to-diff evaluation** — do prompts accurately predict the resulting
  code change? ★ *Impact: High · Effort: High.*
- **Prompt-branching study** — how developers explore alternatives with AI vs
  Git-only workflows (HCI contribution). ★ *Impact: High · Effort: High.*
- **Human-vs-AI edit attribution** — how often humans correct/overwrite AI
  edits. ★ *Impact: High · Effort: Med.*
- **Prompt-failure taxonomy** — wrong file, over-edit, under-edit, regression,
  hallucinated API, style drift; a strong conceptual framework. *Impact: High · Effort: Med.*

### 3.8 Robustness & productionization
- **Multi-session support** — track open turns by session/generation instead of
  assuming one active session (lifts a core MVP limit). *Impact: Very High · Effort: High.*
- **Crash recovery** — on startup, detect dangling open turns and offer to
  finalize/discard/checkpoint. *Impact: High · Effort: Med.*
- **Git branch-switch hooks** — observe branch changes to make carry-over
  seeding precise (fixes the known stale-branch sharp edge). *Impact: High · Effort: Med.*
- **Large-repo performance** — changed-path tracking, sparse/incremental
  snapshots, object reuse. *Impact: High · Effort: High.*
- **`profact.toml` config** — adapters, ignored paths, privacy rules, retention,
  test command, UI prefs. *Impact: High · Effort: Med.*
- **Retention policies** — prune by age / branch / size / tag. *Impact: Med · Effort: Med.*

### 3.9 Safety, privacy & security
- **Secret scanner** — scan prompts/responses/diffs before storing or exporting;
  don't let `.profact/` become a secret sink. *Impact: Very High · Effort: Med.*
- **Redaction rules** — user patterns for paths, prompt text, env values, diff
  content. *Impact: High · Effort: Med.*
- **Private-path opt-out** — never capture configured globs even if Git would.
  *Impact: High · Effort: Med.*
- **Local encryption** — encrypt objects / sensitive fields with an OS
  keychain-backed key. *Impact: High · Effort: High.*
- **Safe-sharing preview** — before export, show exactly what will leave the
  machine. *Impact: High · Effort: Med.*
- **Policy mode** — org rules: diffs-only, no raw prompts, local-only,
  encrypted-only. *Impact: Med · Effort: High.*

### 3.10 Adoption & monetization
- **Killer demo: "time-travel a bug."** Bug introduced by one prompt → found by
  `bisect` → explained by `blame` → fixed by `replay`. One story that sells the
  whole idea. *Impact: Very High · Effort: Med.*
- **Open-core.** Keep CLI/TUI/storage open source; monetize team sync, review UI,
  governance, hosted dashboards. *Impact: High · Effort: Med.*
- **Research edition.** Anonymization + dataset export + reproducibility tooling
  for labs; become a standard instrument. *Impact: High · Effort: Med.*
- **IDE marketplace extension.** Meet developers where they already work.
  *Impact: Very High · Effort: High.*
- **Scripted demo repo.** Branching prompts, failed attempts, test results,
  dashboard views — turns "abstract concept" into "I get it." *Impact: High · Effort: Low.*

---

## 4. Suggested phased roadmap

**Phase A — Make history useful (CLI power tools)**
`profact diff` ✅, `blame`/`why` ✅, `bisect` ✅, `tag` ✅, and rich `log`
filters ✅ are done. Phase A is complete.

**Phase B — Make history visible**
Interactive TUI first (no server), then a local web dashboard, then an
editor panel. The TUI is the cheapest path to a "wow".

**Phase C — Make history trustworthy**
Secret scanner + redaction + private-path opt-out + safe-sharing preview.
Prerequisite for anything shared.

**Phase D — Make history shareable & research-grade**
`.profactbundle` export, prompt-lineage review, dataset builder,
reproducibility protocol, test-result lineage, metrics.

**Phase E — Productionize**
Multi-session, crash recovery, branch-switch hooks, large-repo performance,
`profact.toml`, retention.

---

## 5. Top 10 highest-impact shortlist

| # | Idea | Why it's top-tier | Effort | Impact | Thesis |
|--:|------|-------------------|:------:|:------:|:------:|
| 1 | Prompt **blame** | Clearest "Git for AI intent"; maps lines → prompts | High | Very High | ★★★ |
| 2 | **Regression hunt / bisect** ✅ | Turns history into a debugging tool | Med | Very High | ★★ |
| 3 | Editor **panel** (VS Code/Cursor) | Puts history where devs already work | High | Very High | ★ |
| 4 | **Multi-session** support | Removes a core MVP limitation | High | Very High | ★ |
| 5 | **Prompt-lineage code review** | Makes AI code auditable in teams | High | Very High | ★★★ |
| 6 | **`profact diff` + TUI** | Cheapest path to real, daily-use UX | Med | High | ★ |
| 7 | **Replay prompt on new base** | A novel VC operation for AI coding | High | High | ★★★ |
| 8 | **Secret scanner + redaction** | Trust prerequisite for sharing | Med | Very High | ★ |
| 9 | **Test-runner hooks** ✅ | Ties versions to behavioral outcomes | Med | High | ★★★ |
| 10 | **Prompt-artifact dataset builder** | Makes profact research infrastructure | High | Very High | ★★★ |

---

## 6. Strongest thesis contributions

The ideas that exploit profact's *unique data model* (paired prompt + response +
diff + outcome), rather than UI polish:

- **Prompt blame** and **replay** — new operations only possible with this data.
- **Prompt-artifact datasets** + **reproducibility protocol** — research
  infrastructure and publishable artifacts.
- **Intent-to-diff evaluation** — does stated intent predict the actual change?
- **Bug-causing-prompt analysis** + **test-result lineage** — outcome-aware
  history.
- **Human-vs-AI edit attribution** + **prompt-failure taxonomy** — empirical
  framing of AI-assisted development.

Recommended thesis core: **capture model (done) → blame/replay operations →
dataset + intent-to-diff evaluation**, with the TUI/dashboard as the artifact
that makes it demonstrable.

---

## 7. Explicit non-goals (for now)

Keep scope honest: no full merge of divergent prompt DAGs across machines, no
hosted multi-tenant service, no attempt to replace Git, and no automatic
prompt-quality "grading" that users can't inspect. These can come later, but
chasing them early would dilute the core.

---

## 8. Current status and remaining work

This section is the execution checklist. Earlier sections explain the product
ideas; this section records what is done, what should be built next, and what is
deliberately deferred.

### Done

- [x] Cursor Hooks and Claude Code proxy capture adapters.
- [x] Prompt, reasoning, response, model metadata, tree snapshot, and diff
  capture.
- [x] Content-addressed object storage and SQLite metadata.
- [x] Prompt DAG with branch-local HEADs and Git-branch carry-over.
- [x] Safe checkout with automatic checkpoints and protected ignored files.
- [x] `init`, `start`, `log`, `show`, `status`, `diff`, `checkout`, `note`,
  `recover`, and `doctor`.
- [x] Prompt attribution through `blame` and `why`.
- [x] Regression search through `bisect`.
- [x] Test-result recording and display through `test` and `log --tests`.
- [x] Repository-wide tags usable as version references.
- [x] Rich log filtering by tag, type, branch, model, changed path, time, and
  test status.
- [x] CLI responsibility cleanup: checkout, doctor, Cursor setup, and log query
  preparation live outside `cli.py`.
- [x] `profact health` repository-history summary with activity, test, churn,
  topology, and attention findings.

### Completed milestone — make existing history immediately useful

- [x] Implement `profact health`, initially as a CLI report.
  - Summarize prompt, human-edit, checkpoint, and branch counts.
  - Summarize passed, failed, errored, and untested versions.
  - Identify high-churn files and repeatedly rewritten areas.
  - Surface uncertain attribution, dangling turns, and abandoned side paths.
  - Highlight versions worth inspecting with `show`, `blame`, or `bisect`.
  - Keep calculations inspectable; do not emit an unexplained quality score.
- [x] Add focused tests for health metrics and empty/partial-history cases.
- [ ] Document a reproducible demo session that produces meaningful health
  output alongside the TUI demo.

### Current milestone — make history visible

- [ ] Build an interactive TUI over the existing read-only APIs.
  - [x] Harden SQLite and Git shadow-index concurrency for a long-lived reader.
  - [x] Add reusable version-detail, graph-layout, status, and presenter models.
  - [x] Add the read-only Textual browser, responsive detail tabs, search,
    filters, branch viewing, ancestry mode, health, help, and manual refresh.
  - [x] Stabilize dense wide/narrow rendering, keyboard scrolling, empty and
    missing-content states, and startup/quit worker behavior through Pilot use.
  - [ ] Add guarded checkout and repository-wide tag actions after read-only
    interaction stability is established.
  - DAG and branch navigation.
  - Prompt, reasoning, response, and diff panes.
  - Tag, test, confidence, and health indicators.
  - Filters and version search.
  - Safe checkout only after explicit confirmation.
- [ ] Use the TUI as the eventual home for replay comparison and other visual
  analyses.
- [ ] Consider a local web dashboard and editor panel only after the TUI proves
  the information architecture.

The implementation contract, visual semantics, safety boundaries, and testing
gates for this milestone live in `profact_tui_spec.md`.

### Trust and robustness work

- [x] Add explicit crash recovery for dangling open turns: inspect, finalize
  the current branch HEAD with uncertain attribution, or preserve any turn as
  incomplete without touching files. Destructive discard is deliberately
  excluded because captured prompts are immutable work, not repair artifacts.
- [ ] Add a secret scanner for prompts, responses, reasoning, and diffs.
- [ ] Add configurable redaction and private-path opt-out.
- [ ] Add `profact.toml` for capture, privacy, test, retention, and UI settings.
- [ ] Make Git branch carry-over precise with branch-switch observation.
- [ ] Add multi-session event routing before claiming concurrent-session
  support; solve or clearly mark overlapping-edit attribution.
- [ ] Profile and optimize snapshotting for large repositories.

### Deferred features

- [ ] **Replay on a new base.** Revisit with the TUI. A complete replay feature
  must preserve the source version, create a new version on the selected base,
  record `replayed_from`, and compare the two transformations:
  source parent → source versus replay base → replay result. Cursor replay is
  assisted unless Cursor exposes a supported prompt-submission API; automated
  replay can initially target Claude Code in an isolated worktree.
- [ ] Named prompt branches independent of Git branches.
- [ ] Human-friendly undo/redo navigation over safe checkout.
- [ ] Patch/bundle export and safe-sharing preview, only after privacy controls.
- [ ] Prompt-lineage code review and `.profactbundle` exchange.
- [ ] Dataset export, reproducibility protocol, and intent-to-diff evaluation.
- [ ] Retention, pruning, and archival policies.
- [ ] Additional capture adapters and optional team synchronization.

### Recommended order

Completed: `profact health`, the stabilized read-only TUI, and explicit crash
recovery.

1. Secret scanning, redaction, and private-path rules.
2. Reliable multi-session capture and branch-switch observation.
3. Replay with transformation-to-transformation comparison.
4. Export, research tooling, and productionization.
