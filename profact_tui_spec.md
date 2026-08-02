# ProFact TUI Specification

> Status: implementation specification for the first interactive ProFact UI.
> The TUI is a presentation layer over the existing local store and Git-backed
> services. It must not invent a second history model or weaken checkout safety.

## 1. Decision

The TUI is the next product milestone after `profact health`.

The CLI already makes captured history useful through log, show, diff, blame,
bisect, tests, tags, and health. The remaining usability problem is navigation:
users must repeatedly invoke commands and remember version ids to inspect a DAG.
A terminal UI solves that problem without introducing a server, account, or new
storage format. It also establishes the visual model needed by future replay
comparison.

Crash recovery and privacy controls remain important but do not block a local,
read-only browser. Replay and undo/redo are deferred until the navigation model
and checkout interactions have been proven in the TUI.

## 2. Engineering principles

- **One history model.** The TUI consumes `store`, `log_view`, `health`, and
  other shared services. It never copies version-control rules into widgets.
- **Read-only first.** Browsing must be stable before checkout or tag mutation
  is exposed.
- **Branch semantics stay honest.** Viewing another branch does not switch Git
  branches or move its ProFact HEAD.
- **No inferred quality.** Tests, confidence, churn, and side paths are facts or
  heuristics with visible evidence, never a hidden score.
- **No UI-thread blocking.** SQLite and Git subprocess work runs in Textual
  thread workers.
- **Keyboard complete.** Every workflow works without a mouse or color.
- **Safe actions delegate.** The later checkout action must call
  `checkout.checkout`; it may not reproduce checkout or checkpoint logic.

## 3. Framework and command

Use Textual 8.x. It supports Python 3.11, Windows Terminal, headless Pilot tests,
responsive layouts, and thread workers for the project's blocking APIs.

```text
profact tui
```

The command is available after the normal ProFact installation and appears in
`profact --help`. It validates that the current directory is an initialized Git
repository before launching the application.

## 4. Initial information architecture

### 4.1 Wide terminal (100 columns or more)

```text
+ ProFact | repo | Git: main | viewing: main | HEAD v21 | clean --------+
| History / DAG                 | Version v21 [prompt]                    |
|                               | [Overview][Prompt][Thinking][Response] |
| > * v21 HEAD [pass] prompt    | [Diff][Tests]                          |
|   | v20      [pass] human     |                                        |
|   * v19      untested prompt  | selected tab content                   |
|   | v18      [FAIL] prompt    |                                        |
+-------------------------------+----------------------------------------+
| / search  f filters  g graph  p ancestry  h health  r refresh  ? help |
+------------------------------------------------------------------------+
```

The left side is the selectable history. The right side is detail for exactly
one version. Page sections are unframed working areas rather than nested cards.

### 4.2 Medium terminal (80-99 columns)

The same split remains, but history rows retain only graph, id, HEAD, test, type,
and a truncated summary. Full metadata remains in Overview.

### 4.3 Narrow terminal (below 80 columns)

History occupies the full screen. Enter opens full-screen detail; Escape returns
to history. Diffs do not wrap and can scroll horizontally.

## 5. History rows

Each row can display:

- ASCII DAG gutter;
- version id;
- `HEAD` marker when applicable;
- type: `prompt`, `human`, `anchor`, or `checkpoint`;
- origin Git branch;
- prompt summary or the established non-prompt summary;
- repository-wide tags;
- test state: pass, fail, error, or untested;
- thinking state for prompts: captured, none, or unknown;
- confidence warning when attribution is not high.

The selected row changes only the viewed detail. It never changes ProFact HEAD.
HEAD remains visibly marked even when it is older than versions above it.

## 6. Detail tabs

### Overview

Version id, type, origin branch, parent, timestamp, model and temperature, tags,
commit link, test summary, thinking source, confidence, tree finalization state,
and whether this is the viewed branch's HEAD.

### Prompt

Raw stored prompt. Non-prompt versions show `(no prompt recorded)`.

### Thinking

Captured reasoning, the established explicit no-reasoning placeholder, or
`(no reasoning recorded)` when the source is unknown.

### Response

Raw stored response, or `(no response recorded)`.

### Diff

Stored unified patch grouped by file. Additions, deletions, headers, and context
may use distinct styles but remain understandable without color. The initial TUI
does not perform semantic mapping, side-by-side replay comparison, or claim two
patches implement equivalent behavior.

### Tests

Status, command, timestamp, exit code, and captured output. A pass is described
as a recorded run, not as proof that the current working tree passes. Untested is
neutral and never styled like failure.

## 7. Navigation and views

| Key | Action |
| --- | --- |
| `Up`/`Down`, `j`/`k` | Select previous/next version |
| `Enter` | Focus detail, or open detail on a narrow terminal |
| `1`-`6` | Open Overview, Prompt, Thinking, Response, Diff, Tests |
| `g` | Toggle compact list and DAG presentation |
| `p` | Toggle full visible history and HEAD ancestry |
| `/` | Quick substring search |
| `f` | Open advanced filters |
| `h` | Open health view |
| `r` | Refresh history and status |
| `?` | Open keyboard/help overlay |
| `Esc` | Close overlay/detail or return to history |
| `q` | Quit when no modal is open |

Quick search matches version id, prompt summary, model, and tags. Advanced
filters match `profact log`: tag, type, branch, model, changed path, age/date,
and test status. Validation and error wording come from the shared query layer.

Ancestry mode always reports how many side-path versions are omitted. Filtered
views always state how many versions are filtered out.

## 8. Branch viewing

The initial branch is the current Git branch. A branch selector may inspect any
captured branch using its branch-local ProFact HEAD and carried-over ancestors.
Changing the viewed branch does not run `git switch`, modify files, or move any
HEAD. The header separately labels the actual Git branch and viewed branch.

Display reads use `seed=False`; an untouched Git branch must not silently borrow
the repository-wide carry-over pointer.

## 9. Health view

The health screen presents the existing `HealthReport`:

- captured activity and topology;
- finalized test outcomes;
- repeatedly changed files;
- uncertain attribution and dangling turns;
- unreferenced side-path tips;
- evidence-backed inspection suggestions.

Selecting a version-backed finding returns to history with that version
selected. Selecting a churn file applies the changed-path filter. The UI retains
`unreferenced side-path tip`; it never upgrades that heuristic to “abandoned.”

## 10. DAG visualization contract

- The graph is an ASCII lane gutter adjacent to rows, not a separate decorative
  canvas.
- Lane columns are routing artifacts. They are not persistent branch identities
  and must not receive colors suggesting otherwise.
- A parent outside the visible slice gets a continuation indication rather than
  appearing to be a root.
- Filter chips and omitted counts remain visible when the displayed graph is
  incomplete.
- If timestamp ordering places a child before its parent, the UI warns rather
  than drawing a misleading lineage.
- The visible branch's shared ancestors retain their recorded origin branch.

## 11. Refresh and concurrency

Manual refresh is required in the first release. Debounced live refresh may
watch `.profact/history.db`, its WAL file, and `.profact/HEAD` after concurrency
hardening is validated.

- SQLite uses WAL and a bounded busy timeout so hook writers and TUI readers can
  coexist.
- Each Git snapshot/checkout uses a unique shadow index.
- History/object loading runs in an exclusive thread worker so stale selection
  requests cannot overwrite newer ones.
- Dirty-tree computation runs on startup, explicit refresh, and after mutations,
  not on a rapid timer.
- Selection is preserved by version id after refresh when possible.

## 12. Later safe actions

Checkout and tags are a second TUI milestone.

Checkout is disabled for unfinalized versions. Every checkout requires a modal
showing target id/type/summary, actual and viewed branch, and whether dirty work
will be checkpointed. No is the default. Cross-branch confirmation text comes
from the existing checkout service. The operation runs serially in a worker and
refreshes immediately afterward.

Tag create/delete/move uses shared validation and store APIs. Moving an existing
tag requires an explicit force confirmation.

TUI Back/Forward navigates screens or selections only. It must not be presented
as ProFact undo/redo.

## 13. Empty, loading, and failure states

Required states:

- initialized repository with no versions;
- no versions on the viewed branch;
- no filter matches;
- missing content-addressed object;
- dangling/open prompt turn;
- missing/stale branch HEAD;
- Git status still loading;
- SQLite/Git worker failure.

Errors are dismissible and use user-facing messages, not tracebacks. No-version
and no-match wording stays consistent with the CLI.

## 14. Accessibility

- State is encoded with text plus optional color, never color alone.
- ASCII graph symbols remain usable in no-color terminals.
- Focus is visibly indicated and key hints update with context.
- Prompt/response text wraps; unified diffs do not.
- No font-size or terminal-width assumptions are embedded in content.

## 15. Testing gates

### Service and presenter tests

Use the existing `git_repo` fixture for linear and forked histories, branch
carry-over, filters, open turns, missing objects, tags, tests, confidence,
health links, graph lanes, and refresh selection preservation.

### Textual Pilot tests

Use `App.run_test()` to verify startup, keyboard selection, all tabs, graph and
ancestry toggles, search/filter validation, health navigation, help, worker
failures, and wide/narrow layouts.

### Safety tests for later actions

Prove that declined checkout leaves HEAD and files unchanged, dirty checkout
creates a visible checkpoint, cross-branch checkout requires confirmation,
open turns cannot be restored, and tag moves require force.

### Manual release checks

Verify Windows Terminal at wide, 80-column, and narrow sizes; `NO_COLOR`;
concurrent Cursor hook writes; a dirty real Git index; ignored files; and both
same-branch and cross-branch checkout. Read-only browsing must not change the
working tree, staging area, branch-local HEADs, or captured history.

## 16. First-release acceptance criteria

- `profact tui` launches from an initialized repository and appears in help.
- Linear and forked histories are keyboard navigable.
- Every version exposes Overview, Prompt, Thinking, Response, Diff, and Tests.
- HEAD, version type, branch origin, test, tags, confidence, and thinking state
  are represented honestly.
- Search, existing log filters, ancestry mode, health, refresh, empty states,
  and responsive layouts work in Pilot tests.
- The UI remains responsive while Git/SQLite work executes.
- The existing CLI behavior and tests remain unchanged.

## 17. Deferred

Replay, undo/redo, bisect execution, test execution, doctor repair, blame file
browsing, capture lifecycle management, semantic diffing, web serving, editor
integration, version deletion, sharing, and collaboration are outside the first
TUI release.
