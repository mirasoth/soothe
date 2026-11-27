# Changelog

All notable changes to the Soothe project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Eval steps (`kind="eval"`) skip the step-completion cognition summary: their structured `coverage_verdict` already is the cognition line, and the summarization call sat on the critical path between steps at fast-model latency (measured 3.4–5.8s per call).
- Soft coverage-eval gate (IG-SLP-015): PCE `stalled` signals lose their unconditional audit right — they force an Eval only at confidence ≥ 0.75, and a newer high-confidence `goal_likely_complete` supersedes older `stalled` noise. On the medium/unknown single-leaf branch, surviving audit signals no longer hard-force the Eval: they become evidence (`pce_evidence`) for the existing typesafe → LLM `decide_eval_required` soft chain (fail-safe still runs the Eval). COMPLEX single-leaf goals keep the hard force; the multi-leaf `eval_required()` path is unchanged.

### Fixed

- `root_eval` no longer logs "complex goal ran as single leaf; force Eval" when the Eval was actually forced by pending PCE audit signals on a non-complex goal.

### Changed

- `StepCloseReport` (`soothe.context.models`) makes `goal_portion_complete` / `early_exit` required (previously defaulted). All-default schemas give structured-output post-validation an empty `required` set, so hallucinated payloads passed validation and yielded default-filled garbage; required fields make the rejection immediate and give the model a concrete field-name repair hint. Stored close reports always carried complete field dumps, so checkpoint deserialization is unaffected.
- `assess_step_close` passes a 120 s `deadline_s` to the structured call (requires soothe-nano ≥ 2.1.1). The assessment is advisory with a fail-closed fallback; it must never hold the loop hostage behind a slow thinking model — the original incident was a 9-minute structured retry storm inside one assessment. The `soothe-nano` floor bump lands together with the nano 2.1.1 release.

## [v1.3.9] - 2026-11-27

### Added

- Add `analyze_image` and `extract_text_from_image` tools for workspace-anchored image understanding and OCR, enabled when the `image` / `ocr` model roles are configured.
- Add `/ask` slash command (CLI and TUI) for read-only, answer-only turns.
- Add TUI `/attachments` command to list or clear media attached to the composer.
- Add `stream_resume` RPC so the TUI replays missed stream frames after reconnect and reports evicted gaps.
- Add structured per-thread audit store selected by `persistence.default_backend`, replacing `conversation.jsonl`.
- Persist queued user turns across daemon restarts and show a reconnect banner when the daemon changes.

### Changed

- Replace daemon vision preflight with on-demand vision tools; remove the preflight service and its prompt chain.
- Default `agent.loop.checkpoint.auto_resume_on_start` to `true` so restarts re-enter incomplete loops.
- Land daemon image attachments in the turn workspace with age-based pruning.
- Show attachment sources and support multi-image paste, image re-encoding, and overflow toasts in the TUI composer.
- Landed image attachments from `input/attachments/` to `.soothe/cache/attachments/`: attachments are now engine-local cache (excluded from workspace sync, hidden from agent listings, TTL-pruned locally) instead of synced content-tree files.
- TUI automatic focus moves route through a single focus coordinator (`soothe_cli.tui.focus`): prioritized requests (manual tool-approval head card > clarification/plan-review inputs > lone primary input > chat prompt) with confirm-and-retry convergence replace the scattered fixed-delay autofocus attempts, so a manual-mode tool approval reliably takes focus and turn-teardown chat refocus can no longer race it back.

### Removed

- Remove daemon setting `max_concurrent_vision_preflight` (and its `daemon.yml` template key).

### Fixed

- Stop workspace sync from deleting `.soothe/**` engine state during reconcile.
- Keep stream event sequence numbers from rewinding across daemon restarts.
- Strip phantom media placeholders from history-recalled TUI submissions.
- Paste clipboard text with Cmd/Ctrl+V when the clipboard has no image.
- Keep plan-review approval controls reachable in bounded-height cards.
- Prevent plan-mode goals from reaching mutating dispatch via root evaluation.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.8...v1.3.9

## [v1.3.8] - 2026-11-26

### Added

- Turn-scoped workspace sync facade (`soothe.workspace.sync.TurnScopedWorkspaces`): hosts that bracket agent turns with open/close hooks get the full per-turn sequence (manifest load, content-tree materialization, reconcile, dirty tracking, checkpoint commit) from one entry point instead of reimplementing it over `WorkspaceManager`.
- Workspace-sync streaming for large files: content-tree uploads and restores transfer in bounded-size chunks (s3 multipart with explicit chunk size and concurrency; ranged GETs for reads), so MB–GB files never sit whole in memory; content hashes are verified end to end.
- `TurnCommit` result object (IG-CORE-011): `TurnScopedWorkspaces.close_turn` reports the turn's persistence outcome (`persisted`/`version`/`error`) instead of returning `None` — commit failures are visible to the host, not just logged.

### Changed

- **Workspace sync is single-root and single-writer (IG-CORE-011, breaking).** One per-run backend root (host base URI + `<run_id>`) holds both key spaces: the plain content tree (authoritative file store) and the engine-private manifest (`<run_id>/.workspace/manifest.json`). `WorkspaceManager`/`TurnScopedWorkspaces` take a single `backend_factory` (the `backend` + `content_backend_factory` pair and the `{workspace}` URI-template mechanism are gone), and `Workspace` takes a single `backend`. Concurrent-turn bookkeeping (shared-open refcounting, sole-active-turn reconcile gating, last-turn-out close) is deleted — hosts must serialize turns per workspace (single-writer contract, enforced by the host at its own gate). A failed commit keeps the workspace open so its dirty set survives to the next turn's commit; a clean commit closes the workspace and releases its per-run backend. Migration is a clean cutover: old `system/engine/runs/<wsId>/manifest.json` objects become inert orphans and manifests restart at version 1.
- **Workspace sync is content-authoritative (IG-CORE-010, breaking).** The plain content tree is the single authoritative file store and the manifest is the atomic commit point: `Workspace.checkpoint()` streams dirty files to the content tree (per-file atomic), mirrors deletions, then writes the manifest (version+1, `if_match` optimistic concurrency) before acknowledging the dirty set; a crash mid-close leaves per-file atomic state that the next open re-materializes. Mid-turn durability policy is "turn completion = persisted" — no debounced auto-checkpoints, no staging rows. S3 write volume roughly halves (one representation instead of CAS + content mirror).
- **soothe-daemon no longer wires workspace sync.** `loop_new` requests carrying a `workspace_sync_source` or a URI-shaped workspace are rejected fail-loud with `WORKSPACE_RESOLUTION_FAILED` — sync is a host-side capability (driven through the turn-scoped facade). The daemon-side `workspace_sync` config block, sync manager singleton, the dead `workspace_sync_source` loop-metadata persistence field, and the dev-stack `soothe-minio`/`soothe-minio-init` services are removed.

### Removed

- Workspace-sync CAS subsystem (breaking): the `workspace.state` SQLite staging store, CAS blob key space and materializer, SNAPSHOT/DELTA checkpoint payloads, the background uploader, the checkpoint debouncer, and the publish/artifact channel. Existing deployments keep their old `blobs/`, `runs/<wsId>/checkpoints/`, and `artifacts/` objects as inert orphans (bucket GC is manual).
- `WorkspaceSyncConfig` from the shared config surface.
- `is_remote_workspace_uri` from `soothe.workspace` / `soothe.workspace.resolution` — its last consumer (the daemon sync wiring) is gone; the sync factory's own scheme allowlist is the single SSRF gate (`soothe.workspace.sync.factory`).

### Fixed

- Content-tree restore could escape the workspace root through a broken symlink at the destination, and unvalidated remote listing keys reached path joining; both are now rejected/replaced.
- s3fs streaming against chunked-intolerant S3-compatible stores (e.g. Tencent COS): transfers use known-length multipart parts and ranged reads, and the integration config disables botocore's default request checksums (aws-chunked framing COS rejects).
- Trivial single-step goals finish with their verified step recap instead of waiting for a separate report generation call (`agent.loop.rules.completion.ledger_direct_passthrough_max_tool_calls`, default 4).
- Goal token totals now include completion-report synthesis tokens and report real provider usage (`soothe-nano>=2.0.14`); estimated totals stay marked as approximate.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.7...v1.3.8

## [v1.3.7] - 2026-11-25

### Added

- Approval dock (`Ctrl+R`): pending clarification/approval cards pin in a docked bar above the queued-goals bar instead of mounting inline in the transcript; multiple simultaneous approvals queue FIFO, only the head card is interactive, and the dock never steals focus from the chat input.
- Manual clarification mode (Shift+Tab cycle: Auto → Bypass → Manual → Plan → Ask): every clarification the veritas leg would answer inline is routed to the human relay instead; seeded by `agent.clarification.default_mode: manual`. In manual mode a tool approval landing in the dock takes keyboard focus automatically (clicking the chat input or Tab moves focus back; auto/bypass/plan/ask and ask_user cards never steal focus), and the status badge uses a theme-independent magenta so it never blends into bypass's amber.- Approve & remember on tool-approval cards: mints a loop-scoped generalized grant (command prefix or parent directory) so the same class of action never re-asks within the loop. Approval cards render compact: options are label-only (no verbose descriptions), and the question line no longer repeats the tool/argument preview already shown in the card header.

### Changed

- Goal-completion reports stream with the fast model role by default (`agent.loop.goal_synthesis_model_role: fast`), bounded by `goal_synthesis_idle_seconds` (default 60s, `0` disables).
- Step-card todo panes render every item with no overflow cap — live progress is always visible instead of hiding the running item behind a `+N more` row (the tools and files panes keep their preview caps).
- Step-card activity panes stack in a new reading order — todo first (the step's progress readout), then tools, files, thinking — and each pane's `──── title ────` header rule now renders in that pane's own color (label and counter bold), so sections are visually distinct at a glance. The tools pane uses the calm success green (matching its ✓ row icons) instead of the loud accent color.
- Step-card thinking pane is a plain text window (default 10 lines, up from 8): reasoning lines render verbatim with no bullet prefix and no per-line character cap, showing only the latest lines; the pane-width layout bound still clips lines wider than the terminal.
- The thinking pane no longer styles reasoning as italic: tmux terminals without `sitm` render italic as reverse video, painting a solid background block behind the whole pane. The muted foreground alone distinguishes the pane.
- Multi-tool approval gates collect one explicit decision per pending tool call, with a per-tool recap before submitting; hydrated transcripts keep the per-tool decisions.

### Fixed

- `Ctrl+R` (focus approvals) never actually focused the head card: `Widget.focus(visible=False)` raises a `TypeError` on this Textual version (`scroll_visible` is the parameter) and the error was silently suppressed — fixed to `focus(scroll_visible=False)`, and the new manual-mode dock autofocus uses the same call.
- Manual-mode dock autofocus lost the race against turn teardown: when the loop paused for a HITL answer, cursor reactivation at turn end refocused the chat input after the approval card had already taken focus. Cursor reactivation now skips the steal while a manual-mode tool approval is pending; the input regains focus once the card is answered, clicked, or Tab moves focus over.
- Step card files pane: the `+N more edits` overflow line now leads the pane like the tools and todo panes, instead of trailing below the visible rows.
- Execution token statistics no longer collapse to content-based estimates: wire events carry `token_usage_source`, goal token snapshots include the completion synthesis, and the TUI marks estimated totals.
- Every `ask_user` now terminates in an answer, an honest unanswered sentinel, or a park — the fake `"(retry)"` answer and silently-swallowed dismissals are gone; `agent.clarification.autopilot_retry_on_fail` now defaults to `false`.
- Manual clarification mode never decides on the operator's behalf when no human is attached: safety escalations and rule-unresolved gated calls park the goal for a later-attached human instead of auto-deciding.
- Manual mode escalates user deny-rule hits to the human instead of rejecting inline (the operator may override their own rule). Deny results carry a synthesized `deny_rule:<tool>:<pattern>` rule id routed through the existing `escalated_rule_id` plumbing; a human approval stamps an exact `{"rule": "deny_rule:…"}` allowlist record the pipeline honors at stage 1, so an approved override auto-clears retries of the same deny-matched action for the rest of the loop. Signature/prefix/dir grants never override deny rules, and `active_in_bypass` keeps deny absolute even in manual.
- Approving the first tool of a batched approval gate no longer silently rejects the remaining tools; every tool gets an explicit decision.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.6...v1.3.7

## [v1.3.6] - 2026-11-24

### Added

- Automatic in-execution step retry: failed steps retry inside the Execute phase on the same checkpoint thread, preserving prior tool calls and session history (config: `agent.loop.step_retry_max_attempts`, `step_retry_backoff_base_seconds`, `step_retry_backoff_max_seconds`).
- Step-retry visibility end to end: `step_retry` wire events, TUI retry/recovery badges on step cards, and daemon reconcile across restarts.
- Failed steps are re-planned and re-dispatched at the eval boundary in all interaction modes after the retry budget is exhausted.
- New `observability` extra (`soothe[observability]`) declaring the runtime Langfuse dependency; tracing warns instead of silently dropping exports when absent.

### Changed

- `agent.loop.max_step_retries` now covers eval-boundary re-dispatch in all modes; in-execution retry is bounded separately by `step_retry_max_attempts`.
- Tool-approval answers use an exact-match decision vocabulary; empty or unrecognized answers fail safe to reject (previously they defaulted to approve).

### Fixed

- Daemon RPC surface enforces authorization when identity is enabled: admin/destructive RPCs, WebSocket command dispatch, and the ACP upgrade require an authenticated session; `config_get` masks secret-bearing values.
- Feishu inbound works again (per-group/topic loop routing); AKSK creation returns the real one-time secret instead of a fabricated string.
- Runner correctness: pre-warmed worker startup, process-pool truncation notices, per-turn Ray actor cleanup, vLLM/Ollama launcher deadlock, and bounded worker-ready waits in both pools.
- Operator honesty: the stale-worker reaper requires positive evidence before killing, clarification-parked loops are never auto-cancelled by age, cron re-pends stale RUNNING jobs, and stub commands return `not_implemented` instead of fake success.
- Channel hardening: permission-gated media downloads (DingTalk), bounded webhook bodies (Teams), byte-capped media streams (Matrix), SSRF guards (WeChat), trusted authserv-ids only (email), and instance-scoped per-channel config allowlists.
- Persistence lifecycle: the loop persistence writer no longer deadlocks on same-loop init races or closed event loops and drains coalesced checkpoints at shutdown; shared PostgreSQL pool operations are marshalled onto one owner loop.
- A parallel execute wave terminates when a step exits via interrupt or cancellation instead of blocking forever.
- The loop-status heartbeat retries persistence initialization inside its tick loop, so live loops are no longer demoted by a stale `updated_at`.
- Workspace checkpoint recovery reports partial state as partial instead of claiming the newest checkpoint ID.
- `drain_goal_runtime` refuses forged or recycled-PID workspace markers via a spawn-time ownership sidecar (requires `soothe-nano>=2.0.13`).
- Corrupt CE ledger JSON raises instead of silently returning an empty ledger, suppressing partial-work markers.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.5...v1.3.6

## [v1.3.5] - 2026-11-24

### Changed

- Require `soothe-nano>=2.0.12` so the Landlock degraded tier keeps shell tools working with the `/dev/null` write carve-out and interpreter-prefix read allowance.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.4...v1.3.5

## [v1.3.4] - 2026-11-23

### Removed

- Remove the `firecracker` and `boxlite` loop runner substrates (`loop_runner.runner_mode` now accepts `thread_pool` / `process_pool` / `ray` only) and drop the `boxlite`-based `sandbox` extra.

### Fixed

- Queued goals survive tool-approval pauses: the queue is kept intact while an answer is pending and drained only after the resumed goal completes; queue goals always issue a real turn.
- Queued-goal UI unified on a single labelled, fully bordered `QueuedGoalsBar`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.3...v1.3.4

## [v1.3.3] - 2026-11-17

### Fixed

- Veritas approve/deny answers in prose form resolve inline instead of surfacing spurious human-relay popups: both the auto-mode gate and the relay resume translator classify verdict tokens by word boundary (deny wins on a tie).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.2...v1.3.3

## [v1.3.2] - 2026-11-16

### Added

- Intelligent decompose decisions: a trusted finish-vs-split verdict injects the parallel nudge, re-runs at tool-call milestones and context pressure, and refuses single-subtask proposals on action parents; new `agent.loop.decompose` keys (`root_split_nudge`, `recheck_tool_calls`, `max_rechecks_per_step`, `split_hint_threshold_pct`, `quality_ring_size`).

### Changed

- Bump `soothe-nano` floor to `2.0.9`, `soothe-client-python` floor to `1.2.1`, and `pillow` upper bound to `<13.0.0`.

### Fixed

- Finish-vs-split state is shared across LangGraph hops (previously one classifier call per hop); step-completion cognition quotes the step output instead of continuing it; an invalid `LEDGER_DIRECT` report falls back through synthesis instead of degrading to a stub.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.1...v1.3.2

## [v1.3.1] - 2026-11-11

### Added

- Proactive Coverage Evaluation (PCE): a per-loop, signal-only monitor that observes the goal DAG during execution and forces a coverage Eval for single-leaf goals, so goals no longer finalize fully unverified; new `agent.loop.pce` config block.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.3.0...v1.3.1

## [v1.3.0] - 2026-11-10

### Fixed

- Step-completion reports and final answers show real results instead of the `Step completed successfully` placeholder.
- Running steps are verified against the daemon's step DAG and runner state on loop resume, so stale steps show their real outcome instead of ticking forever.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.10...v1.3.0

## [v1.2.10] - 2026-11-09

### Added

- TypeSafe report validation gate: the final step output is validated as a goal report before `LEDGER_DIRECT` passthrough, with synthesis as the fail-safe fallback.

### Changed

- `LEDGER_DIRECT` is always the first completion candidate; eligibility uses DAG topology (short linear chains qualify) and the final step's execution output, surfacing the actual deliverable as the goal report.

### Removed

- Remove `agent.loop.final_response` and `AgenticFinalResponseMode` — completion strategy is determined automatically.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.9...v1.2.10

## [v1.2.9] - 2026-11-09

### Changed

- Bump the `soothe-nano` floor to `>=2.0.8`, pulling in workspace-scoped `rm` relaxation (in-workspace targets downgrade to an audited risk hint) and the `sudo_rm_rf` pattern-ordering fix.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.8...v1.2.9

## [v1.2.8] - 2026-11-06

### Changed

- Bump the `soothe-nano` floor to `>=2.0.7`.

### Fixed

- Goal-completion synthesis streams are guarded by the dispatch idle watchdog — a stalled provider stream re-dispatches once and then falls back to the step summary instead of hanging.
- TUI token usage renders live while streaming, with a full input/output/cache-token breakdown.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.7...v1.2.8

## [v1.2.7] - 2026-11-05

### Changed

- Route every `ask_user` through one veritas-arbitrated path, closing the coverage→`search_tools` spiral where the loop repeatedly asked for tools instead of the user.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.6...v1.2.7

## [v1.2.6] - 2026-10-30

### Added

- In-flight partial tool-arg wire updates: `soothe.stream.tool_call.update` frames with `partial: true` stream while tool args are still generating, so clients can render progress mid-call (complete-arg repeats opt-in via `agent.loop.output_streaming.emit_complete_stream_tool_updates`).
- `SOOTHE_TUI_DEBUG` is actually wired and no longer capped below INFO.

### Changed

- Step-card activity renders as flat, always-stacked panes (tools → todo → files → thinking) instead of a nested tree, at every terminal width.
- Bump the `soothe-nano` floor to `>=2.0.6`.
- Daemon stream delivery flushes buffered tool-update batches on idle ticks, so the last partial update no longer waits for the tool to finish.

### Fixed

- Step cards surface the CoreAgent reply emitted after a tool call (previously dropped by the phase gate).

## [v1.2.4] - 2026-10-30

### Changed

- Risk-tiered security: built-in `tool_approval.deny_rules` migrate into nano's severity-tiered registry — `critical` matches hard-deny, sub-critical matches surface as risk hints; `deny_rules` is now the user-defined extension point only (default empty).
- Veritas decision fast path: the calibrated risk classifier resolves approval-shaped requests inline with zero LLM calls; untrusted or unavailable verdicts fall through to the LLM leg.
- Loop-scoped grants: approving a call mints prefix/signature/rule-family grants so repeated shapes clear the gate with zero model calls (`tool_approval.dynamic_rules`, cap 256 per loop).
- Bump the `soothe-nano` floor to `>=2.0.4`.

### Fixed

- The clarification relay no longer re-issues the same question on resume, and inbox hydration is idempotent (no more duplicate cards or triple-enqueued interrupts).
- `TypeSafeRiskClassifier` circuit-breaks on permanent HTTP failures instead of retrying the endpoint on every tool call.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.3...v1.2.4

## [v1.2.3] - 2026-10-24

### Changed

- Bump `soothe-nano` floor to `>=2.0.3`, pulling in the operation-guard fix that anchors relative tool paths to the workspace root (previously denied as outside-workspace whenever the process CWD differed from the workspace root).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.2...v1.2.3

## [v1.2.2] - 2026-10-24

### Changed

- Bump `soothe-nano` floor to `>=2.0.2`.
- Spec/docs layout migration: RFC specs, implementation guides, and drafts move from `docs/` to `.platonic/` skill-default paths; no IDs, statuses, or content changed.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.1...v1.2.2

## [v1.2.1] - 2026-10-21

### Fixed

- `Workspace.materialize_content()` deletes local files absent from the content backend after restoring the tree, so backend-side deletions no longer linger in the workspace.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.2.0...v1.2.1

## [v1.2.0] - 2026-10-18

First stable release on the nano 2.0 train.

### Changed

- Align to nano 2.0: module surface (runner resolver, `soothe_nano.wire` imports) and config scaffold (`tools.execution` → `tools.shell`; `tools.data` / `tools.file_ops` / `subagents.academic_research` blocks removed); pin `soothe-nano>=2.0.0,<3.0.0`.

### Fixed

- Break the unbounded `DISPATCH → ROOT_EVAL → DISPATCH` cycle that exhausted the LangGraph recursion limit and aborted long-running loops on a single goal.
- `GraphRecursionError` is scoped to the current goal (resumable via `retry`/`resume`) instead of aborting the loop thread.
- Checkpoint serde covers every host routing enum, so branch-routing state survives restarts and thread resumes.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.1.0...v1.2.0

## [v1.1.0] - 2026-10-13

### Changed

- **Dissolve `soothe-sdk`**: every `soothe_sdk.*` import resolves to its new home (agent-axis contracts → `soothe_nano.*`, wire/display/ux → host copies); importing it is now a boundary violation.
- Floors: `soothe-nano>=1.4.2` and `soothe-client-python>=1.2.0`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.22...v1.1.0

## [v1.0.22] - 2026-10-12

### Added

- Dual-source workspace sync (content mirror): a second file-tree backend restores the content tree at open and mirrors dirty-file writes and deletions at checkpoint time, so non-engine readers can read the latest workspace state directly.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.21...v1.0.22

## [v1.0.21] - 2026-10-12

### Fixed

- MCP tools load again: `SootheConfig.secret_resolver` no longer raises, and tool discovery awaits the async `get_tools` of `langchain-mcp-adapters` 0.3.x instead of failing silently.

### Changed

- Upgrade the langchain ecosystem to the 1.x line (`langchain` 1.4.4, `langchain-core` 1.6.9, `langchain-mcp-adapters` 0.3.2, `langchain-openai` 1.7.0, `langgraph` 1.2.14) and adapt the TypeSafe decision modules to `langchain-typesafe` 0.0.1a3.
- Bump `soothe-nano` floor to 1.3.3.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.20...v1.0.21

## [v1.0.20] - 2026-10-12

### Fixed

- `Workspace.checkpoint()` no longer raises on every call — checkpoints actually persist; workspace-root files are tracked; local-filesystem sync backends create parent directories on write.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.19...v1.0.20

## [v1.0.19] - 2026-10-11

### Fixed

- The ROOT_EVAL coverage gate finalizes only on a structured `complete=true` verdict, so an incomplete Eval forces a re-audit instead of silently closing the goal.
- Consumed relay-inbox entries roll back when a malformed answer raises, so the user's answer is re-enqueued instead of lost.

### Changed

- Eval steps emit a binding structured `coverage_verdict` tool call instead of a free-prose verdict; `decompose_task` is no longer injected on Eval threads. Bump `soothe-nano` floor to 1.3.2.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.18...v1.0.19

## [v1.0.18] - 2026-10-09

### Fixed

- Render a fresh tool-approval prompt for each new step instead of reusing a lingering already-answered card, which silently parked loops in awaiting-clarification with no visible prompt.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.17...v1.0.18

## [v1.0.17] - 2026-10-07

### Changed

- Bypass mode permits all gated tool calls by default (`InlineGateConfig.active_in_bypass` defaults to `False`; set `True` to keep deny rules absolute even in bypass).
- Banned safety-rule tool approvals escalate to the human via a new `escalate` outcome instead of silently auto-rejecting (instructive reject in autopilot).
- Scale the graph `recursion_limit` with `agent.loop.max_iterations` so clarification rounds don't abort the loop mid-goal.

### Fixed

- Stale interrupts from a cancelled prior goal no longer leak into the next goal's approvals (interrupts tagged with `goal_id`).
- Bypass-mode `ask_user` defers route to the retry sentinel instead of stalling on an unanswered interrupt.
- Classifier `escalate` verdicts degrade to instructive rejects in autopilot when no human is attached.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.16...v1.0.17

## [v1.0.16] - 2026-09-27

### Changed

- Live-read `interaction_mode` in the executor so a mid-wave mode hot-swap reaches steps queued in the current wave.
- Bump `soothe-client-python` floor to 1.0.24 for `interaction_mode` `plan|bypass` support.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.15...v1.0.16

## [v1.0.15] - 2026-09-23

### Added

- Auto-compact circuit breaker (`agent.loop.max_consecutive_compact_failures`, default 3) preventing compaction death-spirals that waste API calls.
- Token-budget auto-continuation: `max_tokens` hits inject a "continue" nudge and re-dispatch (`agent.loop.token_continuation_*`, default on).
- Daemon loop stall scanner health check (`daemon.loop_status_reconciliation.stall_timeout_minutes`, default 30).

### Changed

- Dispatch stall recovery favors auto-recovery over fast failure: backoff now extends (1.25^n, cap 600s) instead of shortening; `dispatch_idle_seconds` 180s→240s and `dispatch_retry_max` 3→5 (~2400s total budget vs ~440s), configurable via `agent.loop.dispatch_idle_backoff_factor` / `dispatch_idle_backoff_cap_seconds`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.14...v1.0.15

## [v1.0.14] - 2026-09-21

### Added

- Inline tool-approval gate (`AutoModeMiddleware`): deny-rule and safety rejects resolve inline as instructive errors, auto-mode approvals execute silently, and only genuinely human decisions emit the approval interrupt (`agent.clarification.tool_approval.inline_gate`).
- `ask_user` inline fast path (`AskUserGateMiddleware`, `agent.clarification.ask_user_gate.enabled`): confident questions resolve inline with zero interrupts; the rest route to the human relay.
- TypeSafe-backed decisions with LLM fallback: intent classification (intake label + response language, own confidence gates) and goal-coverage eval decisions (skipping an audit requires high confidence).
- Swappable calibrated-probability classifier for the tool-approval gate, configured via nano's `classifier_providers` / `classifier` blocks; shipped shadow-only and disabled by default.

### Changed

- Only rule-unresolved tool calls reach the classifier; deny rules, allowlist hits, and safety checks resolve deterministically. Deny rules now also reject calls the old heuristics let execute silently, and stay absolute in bypass mode.

### Removed

- Remove `interrupt_rules.py` (absorbed into the gate's decision table), the veritas tool-approval prompt variants, `tool_approval.allow_rules`, and the dual-model `tool_approval.veritas_fallback` block.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.13...v1.0.14

## [v1.0.13] - 2026-09-19

### Added

- Config-gated static breakpoints on the loop graph (`agent.loop.debug.interrupt_before` / `interrupt_after`).
- Single-call scratchpad synthesis: the synthesis prompt self-classifies its scenario and generates in one streaming call instead of a blocking classify round trip plus a stream.

### Changed

- All pending same-thread clarification interrupts resume with one `Command(resume=...)` instead of one park→answer→resume round trip each.
- The clarification inbox is reconciled against CoreAgent checkpoint state each execute turn (stale entries dropped, missing interrupts alerted).
- Trim the DashScope provider pool in develop/production configs to the endpoints with valid credentials (blocked keys previously stalled failover ~3s each).

### Fixed

- Goal-completion synthesis streams are wrapped in `asyncio.timeout(dispatch_idle_seconds)` — a stalled provider falls back to a summary instead of blocking finalization forever.
- Client `cwd` persists as the loop workspace in ACP `session/load` / `session/resume` / `session/fork`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.12...v1.0.13

## [v1.0.12] - 2026-09-15

### Fixed

- Bump `soothe-nano` floor to `1.2.28`: thinking-only streams trigger failover instead of surfacing empty content; empty plan responses retry with a visible-output nudge.

### Added

- Workspace sync wired into the daemon: remote object-store URIs (`s3://`, `gs://`, `az://`) in `loop_new` provision an fsspec sync backend; non-allowlisted schemes are rejected (SSRF prevention).
- GPU-bound Ray actors (`RayConfig.num_gpus`) with in-actor vLLM/Ollama launchers, plus production-grade Ray runner lifecycle (per-actor resource constraints, timeout enforcement, dead-actor detection, cooperative cancellation).

### Changed

- Dispatch stall detection: `dispatch_idle_seconds` defaults to 180s with progressive backoff; the idle timer only fires during the LLM-response gap.
- ACP channel enabled by default — the daemon serves the ACP WebSocket endpoint at `/acp` out of the box.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.11...v1.0.12

## [v1.0.11] - 2026-09-13

### Fixed

- Bump `soothe-client-python` floor to 1.0.23: the published 1.0.22 lacked the `autopilot_rail_id` kwarg every TUI turn passed, raising `TypeError`.

### Changed

- All four clients (python/go/rust/typescript) support `autopilot_rail_id` end to end.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.10...v1.0.11

## [v1.0.10] - 2026-09-12

### Fixed

- Bump `soothe-nano` floor to 1.2.26: `git rm` is no longer blocked as destructive `rm`; the `run_command` security gate honors loop-scoped human approvals instead of re-denying the same command.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.9...v1.0.10

## [v1.0.9] - 2026-09-12

### Added

- Loop-native rail-governed autopilot: `job_create` with `rail_id` routes through the loop graph, and the autopilot `top` dashboard is ported into the TUI `/context` modal with a global loop→goal→step forest view.
- File-edit branch in TUI step cards (action, file path, line-change deltas).

### Removed

- Remove the `soothe-autopilot` package and all legacy standalone autopilot mode; `/autopilot` runs entirely through the loop-native rail path (config `agent.autopilot` → `agent.rail`).
- Remove the `manual` composer mode (Shift+Tab cycle: Auto → Bypass → Plan → Ask).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.8...v1.0.9

## [v1.0.8] - 2026-09-09

### Fixed

- Fix plan-mode approve resume misrouting to `END` — the follow-on exec goal is now enqueued and the plan result set instead of fataling on the next continue.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.7...v1.0.8

## [v1.0.7] - 2026-09-08

### Changed

- Hot-swap the agent mode mid-goal on Shift+Tab for all sub-modes — bypass takes effect on the running goal instead of waiting for the next turn dispatch.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.6...v1.0.7

## [v1.0.6] - 2026-09-07

### Changed

- Route banned safety-rule tool approvals to a human via a new `escalate` pipeline outcome instead of silently auto-rejecting.
- Make the step circuit breaker progress-aware: a deterministic stall triggers a guided retry before tripping fatally.

### Fixed

- Interrupt-resumed dispatches no longer count as failures in the step circuit breaker — steps needing several approvals no longer trip "re-dispatched N times without progress".
- Loops parked for clarification survive worker restarts (parked goals recognized as intentionally paused and resumable instead of demoted to idle/cancelled).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.5...v1.0.6

## [v1.0.5] - 2026-09-03

### Added

- `QueuedGoalsBar` TUI widget: all queued goals as selectable rows above the chat input (keyboard navigation, edit, cancel, `Ctrl+q`).
- `FirecrackerLoopRunner` and `BoxLiteLoopRunner` isolation substrates (`loop_runner.runner_mode: firecracker|boxlite`).
- MinIO object storage in the dev docker-compose stack plus a `workspace_sync` config block for local S3-backend testing.
- PostgreSQL-backed `WorkspaceStateStore` for workspace sync (unified persistence backend).

### Changed

- All runner configuration nests under a single `loop_runner` block (`runner_mode: thread_pool|process_pool|ray`); the flat fields are removed — env override `SOOTHE_DAEMON_LOOP_RUNNER__RUNNER_MODE`.
- Runner classes/keys renamed for clarity (`PoolLoopRunner`→`ProcessLoopRunner`, `worker_pool`→`process_pool`, `distributed`→`ray`); rails move into `soothe` as `soothe.rails`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.4...v1.0.5

## [v1.0.4] - 2026-09-01

### Added

- Bypass permissions mode: the StrangeLoop proceeds without human-in-the-loop interrupts for tool calls.

### Changed

- Bump `soothe-nano` floor to 1.2.21 and `soothe-client-python` floor to 1.0.21.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.3...v1.0.4

## [v1.0.3] - 2026-08-31

### Added

- `root_eval` retries failed steps in read-only modes (`plan`, `ask`) before finalizing, gated by `agent.loop.max_step_retries` (default 2).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.2...v1.0.3

## [v1.0.2] - 2026-08-30

### Changed

- Dispatch watchdog simplified to a single idle timer (`dispatch_idle_seconds`); new `dispatch_retry_max` (default 3) retries on the same LangGraph checkpoint.
- `global_max_llm_calls` lowered to 3 and enforced as a concurrent-stream semaphore (≤ `max_parallel_steps`).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.1...v1.0.2

## [v1.0.1] - 2026-08-30

### Added

- Conditional `interrupt_on`: only dangerous tool calls trigger human-in-the-loop.
- FIFO `ClarificationQueue` replaces first-wins capture, so clarifications process in arrival order.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v1.0.0...v1.0.1

## [v1.0.0] - 2026-08-29

### Changed

- **1.0.0 stable release** — all monorepo-owned packages mark `Development Status :: 5 - Production/Stable`; first-party upper bounds raised to `<2.0.0`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.11.0...v1.0.0

## [v0.11.0] - 2026-08-29

### Changed

- Version bump release; no user-facing changes.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.40...v0.11.0

## [v0.10.40] - 2026-08-29

### Fixed

- `ask_user` coerces invalid LLM output instead of crashing the loop (over-long headers, wrong option counts, whitespace-only questions); decomposition proposals drop out-of-range or self-referential deps instead of rejecting the whole proposal.
- Eval coverage-audit prompts constrain the thread to assessment only, stopping Eval rounds from burning a full loop on exploratory tool calls.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.39...v0.10.40

## [v0.10.39] - 2026-08-28

### Changed

- Default `agent.clarification.default_mode` to `manual` (interactive runs route clarifications through the TUI relay; autopilot still forces auto); bump `soothe-nano` floor to 1.2.14.

### Removed

- Remove dead config keys with no runtime reader across the agent/autopilot/loop/clarification/daemon blocks.

### Fixed

- Veritas defers preference-soliciting questions to the human instead of fabricating answers; tool output and raw tool-error text no longer leak into the user-visible assistant message.
- SLA template keys now match `SlaConfig` field names (previously silently ignored).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.38...v0.10.39

## [v0.10.38] - 2026-08-26

### Changed

- Auto clarification auto-approves safe tool actions via veritas (re-add `tool_approval` to `agent.clarification.force_manual_origins` to force a human on every action).

### Fixed

- Clarification focus fixes: the comments input no longer steals cursor focus, and tool-approval popups focus the Approve action.
- Clarification answers resume the interrupted agent in-thread via `Command(resume=...)`, so the approved tool executes and the agent continues with full context; the silent dead-end after submitting an answer (empty step DAG on resume) is fixed.

## [v0.10.37] - 2026-08-26

### Added

- `ask_user` LLM-callable tool emitting a structured LangGraph interrupt, wired into the relay; `deepagents` `action_requests` interrupts route through the clarification relay instead of being silently auto-approved; `AskUserPromptMiddleware` directs the model to `ask_user` at every clarification/approval gate.

### Changed

- Thread ids decouple from step ids (random ids, linear reuse); interrupt resume reuses the original step thread with `Command(resume=...)` — no re-dispatch, no synthesis workaround.

### Fixed

- `resume_thread_id` persists through the graph checkpoint so the resume path survives the pause/resume round-trip (previously a fatal error after the user answered); `GraphInterrupt` during stream routes into the relay instead of crashing the step.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.36...v0.10.37

## [v0.10.36] - 2026-08-25

### Changed

- Default `agent.clarification.default_mode` to `manual`.

### Fixed

- Pasted-text edits preserve the full paste payload around `[Pasted text #N]` tokens; plan-review char limits raised (24000/3000 → 200000/200000) so large plan artifacts are not truncated before review.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.35...v0.10.36

## [v0.10.35] - 2026-08-24

### Fixed

- Drop the stale 50ms focus-race timer in clarification cards (the race no longer exists); focus relies on `call_after_refresh` alone.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.34...v0.10.35

## [v0.10.34] - 2026-08-24

### Added

- Plan-mode review splits into three actions: Approve (enqueue the follow-on exec goal), Reject (end the goal), and Refine (iterate on the plan); untagged free-text answers default to Refine.

### Fixed

- Rejecting a plan ends the goal outright with no synthesis and no user-facing output.

### Changed

- Align the `soothe-sdk` floor to `>=1.0.12` across all packages.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.33...v0.10.34

## [v0.10.33] - 2026-08-24

### Added

- Veritas retries transient infrastructure failures with exponential backoff; resolved Q&A appends to `clarification_history` so subsequent answers see prior Q&A (new `VeritasConfig` retry fields).

### Fixed

- Replace the rigid path-existence grounding guard with an async LLM critic that judges whether proposal claims are supported by gathered evidence (the old guard spuriously rejected valid proposals).
- Break read-only evidence-gathering loops: a convergence-escalation addendum forces a decompose-or-execute decision, and a read-only-streak circuit breaker stops unbounded gather phases.

### Changed

- Restructure `soothe-cli` into one-way-dependency subpackages (slim `tui/`, new `settings/`/`display/`/`commands/`).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.31...v0.10.33

## [v0.10.32] - 2026-08-23

### Fixed

- Make the decompose evidence-gathering counter survive LangGraph `copy_context()` snapshots (previously the gate always saw 0 and rejected every `decompose_task`).

## [v0.10.31] - 2026-08-23

### Changed

- `SIMPLE` tasks use an LLM decision at ROOT_EVAL to determine whether a coverage Eval is warranted, replacing the deterministic skip; fail-safe requires Eval when the model is unavailable.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.30...v0.10.31

## [v0.10.30] - 2026-08-22

### Added

- First-class plan-mode review station in the StrangeLoop graph (`ask`/`plan` prompt addenda); approving a plan enqueues the follow-on exec goal.

### Fixed

- Plan cards never show empty placeholder sections; blank revision-comments accepted; `awaiting_clarification` re-parks idempotently; chitchat goal finalization persists to disk.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.29...v0.10.30

## [v0.10.29] - 2026-08-21

### Fixed

- Fresh chitchat goals finalize via the fast-path instead of staying stuck in `running`; clipboard over SSH/tmux reports a useful error instead of a traceback.

### Changed

- Centralize the chitchat fast-path bypass decision in `enter_loop`; routing trusts it and ENDs unconditionally.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.28...v0.10.29

## [v0.10.28] - 2026-08-21

### Changed

- Require `soothe-nano>=1.2.7`: a leaked callback manager in structured-output invokes no longer crashes intake classification.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.27...v0.10.28

## [v0.10.27] - 2026-08-21

### Fixed

- Simple tasks skip the coverage Eval gate and finalize directly, matching the trivial-task path.

### Changed

- Drop the `medium` task-complexity tier (routing collapses to minimal/simple/complex); rename the `trivial` intake label to `minimal`; require `soothe-sdk>=1.0.10`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.26...v0.10.27

## [v0.10.26] - 2026-08-21

### Changed

- The in-graph `intent_classify` node is the sole intake classification call site (pre-graph social gate removed) and projects the full ledger, so follow-up goals see prior-goal context.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.25...v0.10.26

## [v0.10.25] - 2026-08-21

### Fixed

- Trivial intake tasks skip the coverage Eval phase and finalize directly; prior goal-completion projection is bounded with head+tail truncation so conclusions survive.

### Changed

- Only `CHITCHAT`/`None` intake labels upgrade to `COMPLEX` on interrupt-resume keywords; `prior_goal_tail`/`cross_goal_completion_tail` default to unlimited.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.24...v0.10.25

## [v0.10.24] - 2026-08-20

### Added

- TUI plain-text command aliases (`exit`/`quit`/`clear`).

### Fixed

- Intake social replies kept for `continue`/`retry` when there is no checkpoint work to resume; goal-completion reports render reliably.

### Changed

- Require `soothe-sdk>=1.0.9`; split the StrangeLoop engine into `execute` and `completion` sub-packages.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.23...v0.10.24

## [v0.10.23] - 2026-08-20

### Fixed

- LangGraph node-level callback handlers no longer leak into nano structured-output invokes.

### Changed

- Require `soothe-nano>=1.2.5` (unified traced invocation); unify the StrangeLoop intake classifier into a single pass; consolidate host prompts into `soothe.prompts`; scope the `/resume` picker to the current workspace.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.22...v0.10.23

## [v0.10.22] - 2026-08-19

### Changed

- Recursive step decomposition is always-on for step THREADS; the legacy plan-generation spine (planner, `PromptBuilder`, ledger projectors) is removed.
- Autopilot gains goal GC and stale-runtime reconciliation: a watchdog cancels orphaned goals, releases stranded workspace reservations and worker slots, and re-queues stranded goals.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.21...v0.10.22

## [v0.10.21] - 2026-08-19

### Fixed

- Clarification relay hardening: hard-defers park via the Context Engine and are resumable; orphaned interrupt-resumes fail closed instead of spinning; empty questions capture as failures.

### Changed

- Migrate `soothe-sdk` from an independent PyPI repository into the monorepo as `packages/soothe-sdk` (independent 1.x version line).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.20...v0.10.21

## [v0.10.20] - 2026-08-18

### Changed

- StrangeLoop v2 graph topology overhaul: `LoopNode` lifecycle, typed `RouteDecision`, 14→12 nodes and 11→8 routers.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.19...v0.10.20

## [v0.10.19] - 2026-08-18

### Fixed

- Force-kill stuck workers on goal cancel: `force_kill` added to the runner protocol and mirrored across pool, thread, and ray runners.

### Changed

- Prod daemon state moves to a single `~/.soothe-prod` bind mount as the full `SOOTHE_HOME` workdir, isolating prod from dev.

### Removed

- Remove the AsyncAPI drift-detection pipeline and the obsolete manual-branch `docker.yml` workflow.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.18...v0.10.19

## [v0.10.18] - 2026-08-17

### Fixed

- First versioned migration adds the `checkpoint_index` column to deployed `agentloop_checkpoints` tables.

### Changed

- Bump `soothe-nano` floor to 1.2.3; ship `fd-find` and `git` alongside `ripgrep` in the runtime Docker image.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.17...v0.10.18

## [v0.10.17] - 2026-08-17

### Fixed

- Add a `deploy-autopilot` job to the release workflow so `soothe-autopilot` publishes to PyPI alongside the other packages.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.16...v0.10.17

## [v0.10.16] - 2026-08-17

### Fixed

- Stop the host `cron` check from erroring when `soothe-daemon` is absent in an isolated core venv.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.15...v0.10.16

## [v0.10.15] - 2026-08-17

### Fixed

- Stop the host `autopilot` check from erroring when `soothe-autopilot` is absent and autopilot is disabled.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.14...v0.10.15

## [v0.10.14] - 2026-08-17

### Added

- Autopilot orchestration extracted into the standalone `soothe-autopilot` package; project goal-DAG pairs seeded into the Context Engine ledger as a real multi-turn transcript.

### Changed

- Migrate to `soothe-nano` 1.2.x unified `soothe_nano.llm` (fixes dropped `tools=` on OpenAI-compatible endpoints like DashScope); daemon startup requires a live PID file and an accepting WS port.

### Removed

- Remove the dead dreaming-distillation stub and the obsolete `.platonic.yml`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.13...v0.10.14

## [v0.10.13] - 2026-08-15

### Fixed

- Shared PostgreSQL pool reopen race: `reset_pool` no longer deadlocks re-entering the non-reentrant lock.

### Changed

- Notify severity drift-awareness, TTL-based dedup, and configurable rate limits; builtin job registry seeded idempotently on daemon startup (`enable_builtin_jobs`).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.12...v0.10.13

## [v0.10.9] - 2026-08-13

### Fixed

- Retry transient SQLite checkpointer errors (WAL contention, file-lock races) with exponential backoff, mirroring the PostgreSQL path.

### Changed

- Default `final_response` to `always_synthesize` so a final CoreAgent report is always produced on goal completion; upgrade `soothe-nano` to 1.1.16 (adds a vLLM provider).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.8...v0.10.9

## [v0.10.8] - 2026-08-12

### Fixed

- Stop goal cancel from orphaning in-flight `run_command` shells — both foreground and background process groups are reaped (SIGTERM → grace → SIGKILL).

### Changed

- GitHub Pages CHANGELOG generated from the repository root via `scripts/sync_wiki_changelog.sh` (no more hand-maintained wiki copy).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.7...v0.10.8

## [v0.10.7] - 2026-08-12

### Fixed

- TUI dynamic themes no longer crash the synthesis card; the Autopilot job-maturity assessor gets full contract context instead of truncated fragments; clipboard starts with the native OS backend.

### Changed

- Centralize truncation budgets into a single Character-Cap Registry in `soothe/config/constants.py`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.6...v0.10.7

## [v0.10.6] - 2026-08-12

### Changed

- Upgrade `soothe-nano` 1.1.11 → 1.1.12; add `hide_thinking_tokens` config for thinking-token stripping.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.5...v0.10.6

## [v0.10.5] - 2026-08-11

### Fixed

- Runner shutdown unwinds dying `astream()` runs cooperatively instead of leaking "unexpected cancellation" errors; workspace-local skills resolve before the first `loop_input`; queued goals show no running timer.

### Added

- Resumable interrupted-goal cursor for cancel-then-retry: a mid-Execute cancel marks the goal `interrupted` and persists the iteration cursor, so retry resumes in place.
- Autoresearch loop rail with native exec and 15 prompt fragments.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.4...v0.10.5

## [v0.10.4] - 2026-08-10

### Fixed

- WebSocket 1009 crashes on large autopilot RPC replies (10 MiB frames both sides); the TUI no longer hangs on a stale "live" loop probe after a turn completes.

### Added

- Autopilot streaming slice DAG spawn and host worktree lifecycle; Veritas auto-clarify for rail `pause_for_user`; LLM-based rail auto-pick; report-commit judgment with bounded DAG ops.

### Changed

- Consume `soothe-sdk` from PyPI; develop-safe default rate-limit and concurrency caps.

### Removed

- Drop `max_parallel_goals` loop-concurrency keys (dead after single-goal StrangeLoop workers).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.2...v0.10.4

## [v0.10.2] - 2026-08-07

### Changed

- Release fix versions for all clients: Python 1.0.12, TypeScript 0.5.8, Go 0.4.14, Rust 0.3.7; raise the `soothe-client-python` floor to `>=1.0.12`.

## [v0.10.1] - 2026-08-07

### Added

- Rail Exec system with YAML verb bodies; multi-channel job lifecycle notify push (email, WebSocket, webhook); job descriptions persisted as `data/jobs/{id}/GOAL.md`.

### Fixed

- Freeze elapsed time for terminal jobs/goals/loops to avoid live drift.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.10.0...v0.10.1

## [v0.10.0] - 2026-08-06

### Added

- Autopilot live `top` dashboard with htop-style stats and interactive keymaps.
- Greenfield rails with LLM-determined fanout width, consensus pass, and thrash detection; Autopilot job maturity assessment and deadlocked failed-goal recovery.
- Migrate the web search/crawl backend from wizsearch to tarzi (`soothe-nano>=1.1.4`).

### Changed

- Share `max_iterations` between StrangeLoop and Autopilot instead of a duplicate budget; remove the soothe-desktop submodule.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.16...v0.10.0

## [v0.9.16] - 2026-08-05

### Added

- `soothe autopilot top` interactive keymaps; consensus evidence grounding and DAG health guardrails; tool-aware dispatch timeout and resume from successful checkpoint; TUI sticky Plan composer mode (Shift+Tab).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.15...v0.9.16

## [v0.9.15] - 2026-08-03

### Changed

- Route simple fresh-loop tasks through the trivial pseudo-plan path, bypassing the plan_assess/plan_generate cycle; unify continuation intake at `route_after_preprocess`.

### Added

- TUI highlights the leading slash-command token in the live chat input and submitted cards.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.14...v0.9.15

## [v0.9.14] - 2026-08-03

### Added

- Vision context in StrangeLoop execute-step prompts, so image-bearing turns keep their attachments through to execution.
- TUI paste: clipboard images paste as `[image N]` attachments.

### Changed

- Bump `soothe-nano` floor to `>=1.1.1`; unify plan gap analysis and assess into a single `evaluate` subgraph; adaptive plan-phase cost budgets.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.13...v0.9.14

## [v0.9.12] - 2026-07-31

### Fixed

- Plan approve / clarification resume no longer silently no-ops when the shared checkpointer advanced past a parked interrupt; card broadcasts stamp `turn_id` so the CLI no longer drops mid-turn frames.

### Added

- CLI `--plan-panel` / `--no-plan-panel` flag for the in-flow plan panel on TUI launch.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.11...v0.9.12

## [v0.9.11] - 2026-07-31

### Fixed

- Intake Pass 2 structured output resolves in one shot (`soothe-nano>=1.0.13`), eliminating flaky classification and extra latency; goal-completion reports stay scoped to the goal that actually finished.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.10...v0.9.11

## [v0.9.10] - 2026-07-30

### Fixed

- CLI reconnect retry no longer resends an empty `loop_input` for slash-skill turns after a mid-turn WebSocket drop; require `tarzi>=0.1.11` so blocking web search releases the GIL (previously froze the daemon event loop during research fan-out).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.9...v0.9.10

## [v0.9.9] - 2026-07-30

### Fixed

- SQLite loop flush coordinator no longer binds its `asyncio.Event` to the first worker loop — "bound to a different event loop" hangs are fixed; caller work is marshalled onto the bound main loop.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.8...v0.9.9

## [v0.9.8] - 2026-07-30

### Fixed

- Internal `asyncio.CancelledError` no longer interrupts a running loop (workers retry once and emit an error terminal); interrupted loops set metadata `status=idle` so resume is not stuck on `running`.

### Changed

- Require `soothe-client-python>=1.0.8` (shared stream-end cancel-reason helpers).

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.7...v0.9.8

## [v0.9.7] - 2026-07-29

### Added

- Planner plan artifact with human review gate (draft preview, action buttons); server-owned display card ledger; loop resume gate; Mermaid diagram rendering in goal-completion reports.

### Changed

- Planner reframed as a solution report instead of an investigation roadmap; StrangeLoop prefers single CoreAgent execute over trivial plan steps.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.6...v0.9.7

## [v0.9.6] - 2026-07-25

### Added

- Progressive `soothed doctor` (tool deps, gated persistence/providers/observability, `--deep` / `--live-llm`); `soothed setup` config scaffolding.

### Changed

- Unify SQLite under a process-scoped runtime; rename CodingCoreAgent → SootheNanoAgent.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.5...v0.9.6

## [v0.9.5] - 2026-07-23

### Added

- `soothe_daemon.admin_rpc` for one-shot `soothed` admin RPCs without the Python client package.

### Changed

- Simplify `soothe-daemon` first-party deps (declare `soothe` + `soothe-sdk` only); package-boundary docs and gates enforced.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.4...v0.9.5

## [v0.9.4] - 2026-07-23

### Added

- CI gates for nano duplicate symbols and first-party pin alignment.

### Changed

- Package-boundary excision: remove host/daemon-only concepts that leaked into `soothe-nano`; align `soothe-daemon` pins with `soothe`.

### Fixed

- Docker image install of `soothe` + `soothe-daemon` no longer fails on disjoint dependency ranges.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.2...v0.9.4

## [v0.9.2] - 2026-07-22

### Changed

- Bump `soothe-nano` pin to `>=1.0.0,<2.0.0` and `soothe-sdk` pin to `>=1.0.5,<2.0.0`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.9.0...v0.9.2

## [v0.9.0] - 2026-07-20

### Added

- Coding CoreAgent lives in standalone `soothe-nano` (batteries-included deepagents stack); the host composes StrangeLoop, Autopilot, and daemon around it. Config splits into `nano.yml` (nano-owned) and `soothe.yml` (host-owned).

### Changed

- Shared protocols, identity errors, and Langfuse helpers move to `soothe-sdk`; default `save_reports` to `false` for research subagents.

### Removed

- In-tree Coding CoreAgent / nano module ownership from the host package.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.8.5...v0.9.0

## [v0.8.5] - 2026-07-19

### Added

- Unified PostgreSQL backend for the display card ledger, cron, and identity when `persistence.default_backend: postgresql`.

### Changed

- Mixed durability overrides raise; SQLite WAL housekeeping skipped in Postgres mode; default recursion limit 99 → 200.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.8.4...v0.8.5

## [v0.8.4] - 2026-07-19

### Changed

- Rename the daemon intent-hint service (`direct_llm_turn` → `intent_hint_turn`) and reject legacy `intent_hint` values.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.8.3...v0.8.4

## [soothe-sdk 1.0.1] - 2026-07-19

### Removed

- Legacy loop assistant phase `direct_model` from `LOOP_ASSISTANT_OUTPUT_PHASES`.

## [v0.8.3] - 2026-07-18

### Fixed

- Daemon port isolation so integration tests never bind the production WebSocket port; agent shell kill guards that refuse daemon, self, and production-port PIDs.

### Changed

- Require `soothe-sdk>=1.0.0,<2.0.0` across core packages.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.8.2...v0.8.3

## [soothe-sdk 1.0.0] - 2026-07-17

### Removed

- `soothe_sdk.client.*` and `soothe_sdk.langchain_wire` compatibility shims; root-package re-exports; short plugin type aliases.

### Changed

- First stable major: import from subpackages only; dependent packages require `soothe-sdk>=1.0.0,<2.0.0`.

## [v0.8.2] - 2026-07-17

### Added

- Protocol-1 `autopilot_*` request RPCs for envelope-only daemons; autopilot cascade goal cancel and `cancel --all`; loop stream `turn_id` / monotonic `seq` boundaries.

### Changed

- Thin CLI daemon I/O onto `soothe_client` (`DaemonSession`); require `soothe-client-python>=0.10.0`.

[Compare with previous version]: https://github.com/mirasoth/soothe/compare/v0.8.1...v0.8.2

## [v0.7.16] - 2026-07-13

### Added

- Skillify embedding resilience with automatic retry and fallback; cancelled goal persistence for audit and resumption.

### Fixed

- Token tracking in daemon/TUI streams; streaming log capture.

## [v0.7.15] - 2026-07-12

### Added

- Configurable model roles for planner, monitor, and consensus paths; log retention and streaming stdout caps for execution tools.

### Changed

- Subagent models resolve from explicit `provider:model` config, taking precedence over `model_role`; removed deepagents execute tool in favor of host execution tools.

### Fixed

- Goal completion display showing planning text instead of deliverables.

## [v0.7.14] - 2026-07-12

### Added

- Cancel/queued-goal guards; token usage tracking in daemon stream and TUI.

### Changed

- Dropped Claude core agent + fastembed.

### Fixed

- Release-docker validation failures on push to main; CLI version resolution on editable installs.

## [v0.7.13] - 2026-07-11

### Added

- Declarative tool-call/step-count limits replacing content heuristics; opt-in MCP builtins with progressive tool loading.

### Changed

- Tool-heavy goals synthesize instead of replaying a truncated execute monologue; response language detected in Pass 1 with explicit prose directives.

## [v0.7.12] - 2026-07-11

### Added

- MCP progressive tool loading with a search-promote-bind runtime.

### Fixed

- TUI chat input focus issues.

## [v0.7.11] - 2026-07-10

### Changed

- Centralized daemon metadata merge for checkpoint writes; consolidated workspace mount resolution.

### Fixed

- Force-kill admission release; loop workspace mount metadata preserved after `/clear` and checkpoint save.

## [v0.7.10] - 2026-07-09

### Fixed

- Subagent wire progress forwarded to TUI with a unified builtin activity protocol; `LoopPersistenceWriter` cross-event-loop failures; filesystem tools resolving against the daemon temp workspace.

## [v0.7.8] - 2026-07-08

### Added

- Ctrl+T plan tree view; execute AI ledger cap (64K) for long-running operations.

## [v0.7.7] - 2026-07-07

### Added

- Plan Gap Analysis framework; assess-only projection mode; built-in `deep_research` and `academic_research` subagents.

### Changed

- Enhanced goal completion synthesis with GFM, bullets, and Mermaid diagrams; file-change previews before apply phase.

## [v0.7.6] - 2026-07-06

### Added

- Goal completion synthesis with GFM format, bullet points, and Mermaid diagrams; file-change previews in the execute workflow.

## [v0.7.5] - 2026-07-06

### Added

- Idempotent database bootstrap; high-performance persistence layer with batch writes; multi-goal ledger scaling for parallel operations.

## [v0.7.4] - 2026-07-06

### Added

- Edit coalescing for batched file modifications; log retention configuration; hot-reload config support.

### Fixed

- File change tracking and preview generation.

## [v0.7.3] - 2026-07-05

### Added

- Skill auto-invoke improvements for seamless tool discovery; `invoke_tools` method for explicit tool execution.

## [v0.7.2] - 2026-07-03

### Added

- Skill runtime discovery with dynamic loading at runtime.

## [v0.7.1] - 2026-07-01

### Added

- Performance isolation and memory optimization for concurrent goals.

## [v0.7.0] - 2026-06-30

### Added

- Post-paint initialization gating for smoother startup; enhanced agent lifecycle management.

## [v0.6.17] - 2026-06-28

### Added

- Same-file edit concurrency handling; `EditCoalescingMiddleware` for batched modifications; 24-hour subagent timeout for long-running tasks.

### Changed

- Improved concurrent edit safety.
