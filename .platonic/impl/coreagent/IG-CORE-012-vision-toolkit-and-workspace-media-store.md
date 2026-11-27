# Implementation Guide: Vision Toolkit and Workspace Media Store

**Guide**: IG-CORE-012
**Title**: Native vision toolkit + workspace media store (turning image understanding into an agent tool)
**Created**: 2026-11-27
**Related**: RFC-CORE-010 (Workspace Sync), IG-CORE-009/010/011 (workspace sync series), **IG-CORE-013 (workspace directory normalization — `.soothe/` is the single private namespace; manifest key is `.soothe/manifest.json`)**, daemon vision preflight (`soothe_daemon.services.image_understanding`)

## 1. Current State and Problems

1. **The daemon vision preflight is a one-shot distillation**: before the turn starts,
   `enrich_user_text_with_vision()` makes a standalone call to the `image`-role model and
   produces a `--- Vision summary ---` text block spliced into the goal; the attachment
   bytes are never addressable again. Consequences: the agent cannot ask targeted follow-up
   questions about image details ("what does the gauge in the bottom-left read"), cannot
   re-query, and on history recall + resend the attachments are already gone via
   `MediaTracker.clear()` (structural flaw — the TUI-side placeholder stripping is only a
   stopgap).
2. **The agent loop has zero vision tools**: nano `_tool_groups` has no vision group; the
   media entries in `soothe_client.tools.metadata` (`extract_text_from_image`/
   `analyze_video`/`transcribe_audio`/`audio_qa`) are display-only zombie entries with no
   implementations.
3. **flowjet has no daemon**: the input pipeline's three flatten points (`normalize_input` /
   `messages_to_input` / `_extract_prompt_text`) drop every non-text block; no multimodal
   path exists. However, flowjet already has a per-run workspace (single-root content tree +
   `.soothe/manifest.json` atomic commit, IG-CORE-011 series as amended by IG-CORE-013 D1 —
   `.workspace` is retired) — that is the natural media store.

## 2. Design Decisions

### D1 — Vision capability = a native nano toolkit (core of this IG)

New file `soothe_nano/toolkits/vision.py`, following the existing toolkit three-layer
pattern (`BaseTool` subclass + `Toolkit` aggregate + `@plugin(name="vision",
trust_level="built-in")`):

| Tool | Model role | Semantics |
|---|---|---|
| `analyze_image(image_path, question?, detail?)` | `image` | Targeted understanding with a question; `detail` reserved (`auto`/`low`/`high` passed through to the OpenAI-compat `detail` field; wiring later) |
| `extract_text_from_image(image_path, language?)` | `ocr` | OCR; also turns the client metadata zombie entry into a real implementation |

- **Multimodal message construction** is consolidated into a nano public foundation: new
  file `soothe_nano/llm/multimodal.py` (`build_image_message(text, images:
  list[ImageBytes|Path])` → langchain `HumanMessage` content blocks, base64 data-URL
  form). The daemon preflight and `intent_hint_turn` are refactored to reuse it in a later
  slice (removing the two parallel wrappers) — not in this slice.
- **Call chokepoint**: `ainvoke_traced(model, ..., purpose="vision_tool",
  component="toolkit.vision")` — rate limiting, timeout-retry, and Langfuse tracing come
  for free.
- **Runtime dependency injection**: isomorphic with `shell.py` — the tool signature carries
  `runtime: Annotated[ToolRuntime | None, InjectedToolArg()]`; workspace anchoring goes
  through the same `WorkspaceResolver.resolve_tool_execution` path resolution; the model is
  obtained via `config.create_chat_model(role)` role routing (when the `image`/`ocr` roles
  are absent from config, the corresponding tool is not registered — same gating pattern as
  `http_requests`).
- **Config**: `config/models.py` gains `VisionToolsConfig(enabled=True,
  image_role="image", ocr_role="ocr", max_image_bytes=20MiB,
  allowed_formats=(png, jpeg, webp, gif, bmp))`, attached to `ToolsConfig.vision`;
  `utils/resolver.py`'s `_tool_groups` gains `"vision"`. The tool group defaults to enabled
  (aligning with the existing `ToolConfig.enabled=True` convention) and degrades gracefully
  to an empty group when no roles are configured. **Scope note**: `ModelRole` already
  includes `"image"` and `"ocr"` (`config/models.py:195`) and `ModelsConfig` already has
  `image`/`ocr` fields (`config/models.py:224-225`) — S1 touches neither the role enum nor
  the models schema; gating checks the existing `ModelsConfig` role fields directly.
- **Security**:
  - Path anchoring to the workspace root + boundary validation (reusing the
    `_check_workspace_boundary` semantics from `security/path_security`; relative paths
    anchor to the workspace; escapes rejected);
  - Content sniffing uses magic bytes to determine MIME — extension names are not trusted;
    rejects anything over `max_image_bytes`. The 20MiB limit and five-format list are
    same-value copies of the daemon preflight limits (`image_understanding.py`
    `_MAX_IMAGE_BYTES`/`_ALLOWED_MIME`) — nano cannot import the daemon constant
    (package boundaries), so nano defines its own; a host-side twin test asserts the two
    sides stay equal (same pinning family as the `.soothe`/`input/attachments` literals);
  - The tool is read-only (does not write the workspace) and usable in Ask mode — it is
    NOT added to `ASK_MUTATING_TOOL_GROUPS` (`agent/interaction_mode.py`).
- **Sandbox mode**: the tool executes in the host process (consistent with the filesystem
  tools) and reads the real workspace path directly; the sandbox guest's shell remapping
  does not affect this tool (nothing to handle).

### D2 — Media store = the workspace content tree's `input/attachments/` (no new storage subsystem)

**Core principle: images are workspace files.** No CAS, no session store, no new key
namespace — each host lands attachments into the run workspace using its existing
persistence machinery:

- Layout: `input/attachments/<sha256[:16]>.<ext>` (content-addressed dedup; the `input/`
  prefix is inside the workspace sync's tracked scope). Dedup semantics are **per-run
  workspace** (IG-CORE-011 C single root — each run has its own content tree); cross-run
  dedup is intentionally not pursued.
- **Hard placement constraint (pinned by IG-CORE-013 D2)**: media files and any derived
  state MUST NOT land under `.soothe/` — that prefix is the framework-private namespace
  excluded from sync and hidden from LLM listings (nano `ESSENTIAL_GLOB_EXCLUDES`), so
  anything placed there becomes invisible and unsynced. Media stays in `input/` where it
  is tracked, synced, and addressable; this also bars the temptation of stashing tool
  artifacts under `.soothe/` (the vision tool is read-only anyway).
- The literal `input/attachments` prefix is a cross-host contract: each host repo
  (soothe daemon / flowjet) defines a module-level constant and pins it with a contract
  test (twin-test pattern, same as IG-CORE-013's `.soothe` literal pinning), preventing
  drift into divergent prefixes like `input/images/`.
- **Persistence rationale**: attachments are ordinary content-tree files, so they ride
  the manifest commit — under IG-CORE-011 C, a failed commit keeps the workspace open
  with the dirty set alive, and a completed commit persists them across restarts and
  history recall. No extra lifecycle code.
- **flowjet (no daemon — this IG's direct answer)**: the three entry flatten points are
  changed to collect image blocks (data-URL / base64) → before building the turn payload,
  write them into the run workspace via the per-run workspace backend's `put_content_file`
  → paths are merged into the input text as
  `[image 1: input/attachments/ab12cd34.png]` → atomically committed with the manifest at
  `close_turn`. Addressable across restarts and history recall; synced to S3 with zero
  extra code.
- **daemon (later slice)**: `QueryEngine` lands the wire attachments into the agent
  workspace at the same path before preflight; the enrichment text appends the paths and
  instructs the agent to use the tool for deep reads; the TUI `MediaTracker` placeholders
  map to the landed paths — on history recall + resend the placeholders → paths remain
  valid, structurally fixing "resend loses images" (at which point
  `_strip_stale_media_placeholders` can be removed as the stopgap).
- Zero wire protocol change: the `attachments` field already exists; what's new is only
  host landing behavior and the paths in text.

### D3 — Preflight and tool division of labor (hybrid, not either/or)

- The preflight stays as the cheap "awareness injection" (existence + gist + tool
  guidance); deep reads go to targeted `analyze_image` calls.
- flowjet initially has no preflight: the agent learns image paths directly from the input
  text and calls the tools itself; the same fast path can be added later if needed.

**Amended (2026-11-27, user decision)**: the daemon preflight is **removed entirely**,
not kept as a hybrid. The daemon lands attachments and merges the landed-paths note
(shared `landed_paths_note` helper) into the input text; the agent performs all vision
reads through the toolkit. The `enrich_user_text_with_vision` service, its instruction
templates, the `max_concurrent_vision_preflight` setting, and the preflight semaphore
are gone; wire validation (`validate_and_normalize_image_attachments`) stays.

## 3. Implementation Slices

| Slice | Repo | Contents |
|---|---|---|
| S1 | soothe-nano | vision toolkit + multimodal builder + config + resolver group + tests; release train (Unreleased → next patch, coordinated with IG-CORE-013 S2 — see sequencing note below) |
| S2 | soothe (daemon) | engine lands attachments → workspace; enrichment appends paths + tool guidance; reuse the multimodal builder; MediaTracker / history-recall closure |
| S3 | flowjet | three entry points accept image blocks → write workspace `input/attachments/` → merge paths into text; remove the flatten-drops-images behavior. **Two file planes**: `normalize_input`/`messages_to_input` in `server/openai_compat/schemas.py`, `_extract_prompt_text` in `server/acp/agent.py` — S3 touches both (OpenAI-compat + ACP); `messages_to_input` prefixes roles, so image collection must preserve role context |
| S4 | soothe (client axis) | align `soothe_client.tools.metadata` media entries (`extract_text_from_image` becomes real, unimplemented entries removed) + sync `docs/wiki/capabilities/tools.md` |

No blocking between slices (S1 first; S2/S3 each consume the released S1, or validate
locally via PYTHONPATH).

**Implementation status (2026-11-27, all uncommitted)**:

- **S1 (nano) done.** `toolkits/vision.py` (AnalyzeImageTool / ExtractTextFromImageTool /
  VisionToolkit / VisionPlugin), `llm/multimodal.py` (`build_image_message` / `ImageBytes` /
  `sniff_image_mime`), `VisionToolsConfig` + defaults in `config/models.py`, `"vision"`
  group in resolver + builtin plugins table. Tests 39 new; nano suite 2241 passed.
  **Amended (2026-11-27)**: no toolkit-level workspace boundary — the tools are
  classified as filesystem-read policy tools (`path_arg_keys` + `outcome_type="file_read"`
  in the metadata twins), so `image_path` goes through the shared operation-security
  middleware chain (sensitive/denied/allowed paths, boundary +
  `security.allow_paths_outside_workspace` + `require_approval_for_outside_paths`),
  identical to other read tools. `build_image_message` emits dict-form text parts
  (bare strings crash litellm's OpenAI transform — shipped as 2.0.16 and fixed).
- **S2 (daemon) done — amended: preflight removed.** `QueryEngine._land_query_attachments`
  resolves the loop workspace (early loop-metadata fetch, failure degrades to a text-only
  turn) and lands attachments before the turn; the landed-paths note (shared
  `landed_paths_note`) is merged into the input text. Per the D3 amendment the legacy
  preflight (`enrich_user_text_with_vision`, instruction templates,
  `max_concurrent_vision_preflight`, the throttle semaphore, and its test file) is
  deleted; `validate_and_normalize_image_attachments` stays as the wire gate. The
  StrangeLoop vision-context chain that consumed the removed `--- Vision summary ---`
  blocks is deleted with it (`sloop/utils/vision_context.py`, the executor `vision_context`
  plumbing, the `VISION CONTEXT` prompt section, the step-brief hint, and the two
  vision constants); the intent-hint image turns reuse `build_image_message`. The TUI
  placeholder→path closure (stopgap removal) is **not** done — it needs a
  daemon→client landed-path feedback design; `_strip_stale_media_placeholders` stays.
  Daemon suite green (1451 after the removals); `test_daemon_image_input.py` rewritten to
  assert real landed bytes (LLM-gated, skipped locally).
- **S3 (flowjet) done.** `RunRequest.attachments`; extraction in
  `openai_compat/schemas.py` (data-URL only, MIME allowlist/aliases, 20 MiB / 8-image
  limits, chat + responses shapes) and `_extract_prompt_attachments` for ACP image
  blocks; the isolating backend lands after `open_session` (materialized tree + dirty
  tracking) and appends the landed-paths note; landing failure degrades to text-only.
  flowjet suite 698+8 passed (the one failure, `test_workspace_sync.py:387`'s legacy
  `.workspace/manifest.json` pin, is the documented IG-CORE-013 interaction and passes
  against the installed soothe).
- **S4 (client axis) done.** `analyze_image` added, zombies
  (`analyze_video`/`transcribe_audio`/`audio_qa`) removed, `extract_text_from_image`
  retained; metadata twins byte-synced with hash bumps on both sides; wiki
  (`capabilities/tools.md` incl. ecosystem-source table, `Basic-Concepts.md`) synced.
  `get_video_info` kept (not in the IG's zombie list). Client suite 334 passed.
- **Deviation from D2 (stronger)**: the landing helper lives once in
  `soothe.workspace.attachments` (`land_attachments` / `attachment_rel_path` /
  `landed_paths_note` / `ATTACHMENTS_DIR`) and both hosts import it — a single
  definition replaces the planned per-host constants + twin tests (the literal pin test
  lives in `packages/soothe/tests/unit/workspace/test_attachments.py`). flowjet writes
  the local materialized tree (picked up by the turn's dirty-tracker commit) rather than
  calling the backend's `put_content_file` directly — same persistence channel, and it
  stays resolver-agnostic (local-only / sync / sandbox all work).
- **方案 A adopted (2026-11-27, user decision)**: media eviction happens at the
  persistence layer, not by relocating media into `.soothe`. `prune_attachments`
  (age-based TTL; daemon `attachment_ttl_s` default 7 days / `0` disables, flowjet uses
  the shared 7-day default) runs before landing; deletions of previously committed
  files mirror back through the turn's sync commit. Media stays in `input/attachments/`
  — tracked, synced, addressable.

**Sequencing vs IG-CORE-013 (workspace directory normalization)**: IG-CORE-013 lands
first. It owns the nano release train touching the same surfaces (glob excludes, security
sensitive-component list) and the flowjet workspace-sync files this IG's S3 touches
(`arbox.py` push/pull skip alignment vs the flatten points + `put_content_file` write
path). Landing 013 first avoids a second nano version bump in the same window and merge
conflicts in flowjet; S2/S3 then consume the post-013 nano.

## 4. Tests

- nano `tests/unit/toolkits/test_vision_toolkit.py`: gating (missing roles → empty group,
  disabled → empty, boundary-escape rejection, magic-bytes type detection, oversize
  rejection) + mocked-model assertions on message shape and `ainvoke_traced` arguments
  (precedents: `test_http_requests_toolkit.py` gating + `test_deepxiv.py` mock pattern).
- `tests/unit/llm/test_multimodal.py`: builder output block shapes.
- resolver: `"vision"` group registration and Ask-mode retention assertions.
- per-host contract test pinning the `input/attachments` literal (twin-test pattern;
  daemon + flowjet) — see D2 placement constraint.
- host-side twin test asserting nano's default `max_image_bytes`/`allowed_formats` equal
  the daemon preflight limits (`_MAX_IMAGE_BYTES`/`_ALLOWED_MIME`) — see D1 Security.
- S2/S3: within each slice, per the host test layout.

## 5. Boundaries and Risks

- **nano independence**: no host-concept leakage — tools route models via config roles,
  the workspace goes through nano's existing `soothe_nano.workspace` machinery, and nothing
  imports any host package.
- **Cost**: a tool call is a targeted VLM call with a question; `ainvoke_traced` has the
  call-policy rate limiting built in; `max_image_bytes` reuses the daemon preflight limit.
- **Multi-host consistency**: daemon and flowjet use the same landing layout
  (`input/attachments/`) and consistent agent-side prompting; only the persistence channel
  differs (sync backend vs daemon direct write).
- **IG-CORE-013 amendment**: the manifest key is `.soothe/manifest.json` (`.workspace` is
  retired). S2/S3 tests must assert on the media paths and commit behavior only — never
  pin the legacy `.workspace/manifest.json` key. Concrete counterexample to avoid copying:
  `flowjet/tests/unit/server/test_workspace_sync.py:387` currently pins
  `(remote / ws_id / ".workspace" / "manifest.json")` — it breaks when IG-CORE-013 S3
  lands and is the template S3 tests would most naturally copy from.
- **Release sequencing**: S1 ships nano first; soothe/flowjet enable the tool surface only
  after consuming the new nano (floor bump verified together with the host slices).

## Appendix A — Rejected Alternative

A session-level media store outside the workspace was considered. It would force flowjet
to build new lifecycle management and would not persist with the manifest; the workspace
content tree covers all requirements with zero new code, so it was rejected.
