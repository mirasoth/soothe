# Implementation Guide: Workspace Directory Normalization

**Guide**: IG-CORE-013
**Title**: Workspace reserved-directory cleanup and normalization (`.soothe` / `.workspace` / `input`…)
**Created**: 2026-11-27
**Related**: RFC-CORE-010 (Workspace Sync), IG-CORE-011 (single root / single writer), nano `workspace/` package, flowjet sandbox push/pull

## 1. Current State (audited)

Reserved names inside the local agent workspace today, with owners:

| Name | Owner | Contents | Synced? | Hidden from LLM? |
|---|---|---|---|---|
| `.soothe/` | nano runtime (`workspace_runtime.py:38` virtual home; `workspace_paths.py:11` backups; `skills/workspace_sync.py:36` mirror; `toolkits/shell.py:122,280` background/foreground; `utils/runtime.py:153` subagent outputs; deepagents middleware `large_tool_results`/`conversation_history`) + soothe core (`sloop/plans/artifact.py`, `rails/selector.py`, `rails/worktree_ops.py`, `runner/shell_drain.py`) | framework/engine runtime state | no (never watched) | **no** — glob-enumerable, LLM-writable |
| `.workspace/` (local) | sync engine (`manager.py:121`) | **empty placeholder only** — manifest lives in the backend key space | no (excluded in `dirty_tracker.py:26` + `workspace.py:130`) | no |
| `input/ working/ output/` | sync engine (`manager.py:31`, `dirty_tracker.py:48` — **duplicated**) | tracked product prefixes | yes | no |
| `.agents/skills` | Claude-style skill root (read convention, `skills/builtins.py`) | user skills | no | no |
| `~/.soothe` | SOOTHE_HOME (host data) | daemon/session/SQLite state | n/a (outside workspace) | n/a |

### Defects (severity-ordered)

1. **CRITICAL — reconcile wipes `.soothe/`**: `_reconcile_content` scans the whole local
   tree (`_scan_local_files` = full `rglob("*")`, `workspace.py:277-279`); skip prefixes are
   only `.workspace/`, `__pycache__/`, `.git/` (`workspace.py:130,244`). Every
   materialize+reconcile pass deletes any `.soothe/**` file absent from the content tree —
   plans, backups, skills mirror, background logs, rails state. `.soothe` is never synced
   (dirty tracker watches only `input/working/output` + root-level files), so it is
   unrecoverable. Verified in source.
2. **Skip/exclusion lists defined in ≥4 places, mutually drifted**: sync engine
   `workspace.py:130` (materialize skip), `dirty_tracker.py:26` (`_EXCLUDED_DIRS`), flowjet
   `arbox.py:26/28` (push skips `.workspace`, **pull does not** — asymmetric: guest-written
   `.workspace/**` gets pulled back to the host), nano `filesystem/workspace.py:67-78`
   (`ESSENTIAL_GLOB_EXCLUDES`, no `.soothe`). No shared constant anywhere.
3. **`_DEFAULT_WATCHED_SUBDIRS` + normalizer duplicated** (`manager.py:31,34-54` vs
   `dirty_tracker.py:48,51-86`) with conflicting semantics: manager appends extras to the
   defaults; the tracker's normalizer **replaces** defaults when given a non-empty list.
   The engine path happens to compose correctly, but direct `DirtyTracker` construction
   silently drops `input/working/output`.
4. **`.soothe` is LLM-visible and LLM-writable**: framework-private state (background
   logs, `.meta` ownership sidecars, conversation history) sits in the same namespace the
   model can read, write, and glob; it is not in any exclusion or dangerous-component list.
5. **Subagent output split**: browser/computer use `WorkspaceRuntime.virtual_home()/agents/<name>`
   (`utils/runtime.py:140`), deep_research forces `<workspace>/.soothe/agents/<name>`
   (`utils/runtime.py:153`) — two trees depending on virtual mode.
6. **Custom `backup_dir` escapes the private namespace** (`workspace_paths.py:139-176`):
   a configured `backup_dir` maps to a backend-cwd-relative path without the `.soothe`
   prefix in virtual mode.
7. **Sandbox half-routes `/.soothe`** (`agent/builder.py:637-644`): the prefix routes to
   the host backend, but guest shell writes `<workspace>/.soothe/background` directly —
   the same prefix lives half on the host, half in the guest (intentional per comment, but
   undocumented as a contract).
8. **`process_workspace_root()` implicitly creates `/tmp/soothe-workspace`**
   (`workspace_runtime.py:100-107`): a shared, undeclared "reserved workspace" with
   cross-session/cross-user ambiguity.
9. **DANGEROUS_COMPONENTS duplicated in nano** (`path_security.py:82-113` vs
   `operation_guard.py:358-377`), sets differ despite the "kept in sync" comment.
10. **Local `.workspace/` dir is dead weight**: created by `manager.py:121`, never written
    locally (manifest is backend-key-space only), no code reads it; a test assumes
    `.workspace/state.db` protection semantics nothing implements.

## 2. Design Decisions

### D1 — One private namespace: `.soothe/`; `.workspace` is retired entirely (user decision 2026-11-27)

- `.soothe/` stays the single agent-plane private namespace for ALL framework/engine
  runtime state (nano runtime + host rails/plans/worktrees/shell-drain). Rationale:
  largest installed surface (nano runtime, `~/.soothe` consistency, SKILL.md documents),
  and nothing needs renaming — only protection and visibility change.
- The **manifest moves into the namespace**: backend key space
  `.workspace/manifest.json` → `.soothe/manifest.json` (amends IG-CORE-011's key choice;
  its single-root/single-writer contracts are unchanged). Locally `.workspace/` is no
  longer created. Migration = clean cutover (IG-CORE-011 precedent): the content tree is
  authoritative, so a lost manifest only resets the version counter to 1 and drops the
  first turn's mid-commit-crash protected set. **No backward compatibility**: every
  `.workspace` skip-list entry, write-protection branch, and test fixture is deleted;
  stale backend `.workspace` keys from a pre-cutover deployment are not migrated or
  excluded — delete them from the bucket once if present.
- Target local layout:

```
<workspace>/
├── input/            # tracked — host-provided inputs (materials)
├── working/          # tracked — agent working artifacts
├── output/           # tracked — final deliverables
├── .soothe/          # SINGLE private namespace (never synced, hidden from LLM listings)
│   ├── backups/  background/  foreground/  agents/<name>/  skills/
│   ├── plans/  rails/  large_tool_results/  conversation_history/
│   ├── cache/attachments/  # engine-local wire-attachment cache (IG-CORE-012 D2 amendment, 2026-11-27)
│   └── AGENTS.md
└── <user content>
```

### D2 — One exclusion list per repo, contract-pinned across repos

- **sync engine (soothe)**: a single module-level constant
  `_ENGINE_SKIP_PREFIXES = (".soothe/", "__pycache__/", ".git/")` defined
  once in `workspace/sync/` and consumed by materialize skip, `_reconcile_content`, and
  `DirtyTracker._EXCLUDED_DIRS`. Reconcile additionally never prunes top-level `.soothe`;
  `.soothe/` also keeps the backend manifest key out of content listings.
- **nano**: `ESSENTIAL_GLOB_EXCLUDES` gains `.soothe` so
  ls/glob/grep do not enumerate framework state; the security layer adds a bypass-immune
  `filesystem.private_namespace` rule — **reads allowed (medium → audited hint), direct
  tool writes denied (critical → hard-deny under the default posture)**. Scope note
  (implementation deviation, 2026-11-27): the write deny is scoped to the framework-state
  subdirs (`backups`, `background`, `foreground`, `conversation_history`,
  `large_tool_results`, `skills`), NOT the whole `.soothe`
  tree — virtual mode maps `~` to `<workspace>/.soothe`, so a blanket deny would break
  benign home scratch writes; subagent outputs (`.soothe/agents/`) and skill-authored
  rails (`.soothe/rails/`) must stay writable. Framework code writes via internal APIs
  that bypass the guard.
- **flowjet**: `_PUSH_SKIP_DIRS` and `_PULL_SKIP_DIRS` both gain `.soothe` (fixing the
  pull-side asymmetry).
- Cross-repo pinning: the literal `.soothe` cannot be a shared import across repos, so
  each repo gets a contract test asserting its constant equals `.soothe`
  (twin-test pattern, same as the client metadata twins).

### D3 — Watched subdirs: one definition, one normalizer

`_DEFAULT_WATCHED_SUBDIRS` and its normalizer live in one place (`dirty_tracker.py`);
`manager.py` imports them and keeps its append-to-defaults composition explicitly named
(e.g. `merge_watched_subdirs(configured)`). Direct `DirtyTracker` construction with extra
subdirs no longer silently drops the defaults.

### D4 — Runtime paths unified under the namespace

- Subagent outputs: one resolver — workspace-bound → `<workspace>/.soothe/agents/<name>`;
  no workspace → `SOOTHE_HOME/agents/<name>`. browser/computer and deep_research converge.
- `backup_dir`: a custom name always resolves inside the namespace (`<virtual>/.soothe/<name>`);
  the escape-to-workspace-root behavior is removed.
- `process_workspace_root()`: the implicit `/tmp/soothe-workspace` is replaced by a
  SOOTHE_HOME-scoped path (`~/.soothe/data/tmp/workspaces/<purpose>`), private per user.
- Sandbox `/.soothe` routing: documented as a contract — framework state routes to the
  host backend; guest shell process dirs (`<workspace>/.soothe/{background,foreground}`)
  intentionally stay in the guest, now covered by D2's exclusion lists on both sides.

### D5 — Nano security dedupe

`DANGEROUS_COMPONENTS` collapses to one definition in `security/` (single module,
imported by both `path_security.py` and `operation_guard.py`); `.soothe` added as a
sensitive component (read=medium / write=critical). `.agents/` stays a read-only discovery
convention, documented as such.

## 3. Implementation Slices

| Slice | Repo | Contents | Status |
|---|---|---|---|
| S1 | soothe (sync engine) | `_ENGINE_SKIP_PREFIXES` single source (materialize + reconcile + dirty tracker); watched-subdir dedupe; manifest → `.soothe/manifest.json`; stop creating local `.workspace/`; regression tests (reconcile preserves `.soothe`; backend keyspace never materializes) | done 2026-11-27 (147 tests green) |
| S2 | soothe-nano | glob excludes + `filesystem.private_namespace` rule (scoped write deny, see D2 note); DANGEROUS_COMPONENTS dedupe; subagent-output resolver unification; `backup_dir` namespace nesting; `/tmp` root replacement; twin contract test; release train | done 2026-11-27 (2226 tests green; release pending) |
| S3 | flowjet | push/pull skip alignment (`.soothe` both sides, symmetric); twin contract test | done 2026-11-27 (59 tests green) |
| S4 | docs | CHANGELOG entries (soothe / nano / flowjet); this IG | done 2026-11-27 |

Migration: no renames — existing workspaces keep their `.soothe` layout; `.workspace`
is fully retired with no compat surface (no local dir, no skip entry, no orphan
handling). Stale backend `.workspace` keys from a pre-cutover deployment are not
excluded from content listings — delete them from the bucket once if present. The
reconcile fix is protective-only; nothing re-uploads `.soothe`.

## 4. Tests

- Engine: reconcile preserves `.soothe/**` across open_turn; `.workspace` keys never
  materialize; watched-subdir merge semantics (defaults + extras, direct construction);
  local `.workspace` no longer created.
- nano: glob/ls exclude `.soothe`; security write-denied / read-allowed; subagent output
  resolver matrix (workspace bound / virtual / none); backup_dir nesting; contract test
  `.soothe` literal.
- flowjet: pull no longer re-imports guest-written `.soothe/**`; push skip parity.

## 5. Risks

- Hiding `.soothe` from listings could break prompts that told agents to inspect framework
  state — none exist in-repo; the security read path stays open, so direct reads still work.
- flowjet deployments that relied on reconcile pruning unknown local files now keep
  `.soothe/**` — intended (it is engine state, not user content).
- `/tmp` root replacement changes process-isolation staging location; audit
  `process_workspace_root()` callers before S2 lands.
