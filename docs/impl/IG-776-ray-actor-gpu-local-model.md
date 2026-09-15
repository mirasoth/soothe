# Implementation Guide: Ray Actor GPU-Bound Local Model Inference

**Guide**: IG-776
**Title**: Ray Actor GPU-Bound Local Model Implementation Guide
**Created**: 2026-09-13
**Related RFCs**: RFC-233, RFC-221

## Overview

This implementation guide covers the extension of the Ray actor loop runner to bind GPU resources, launch a local model server (vLLM/Ollama) inside the actor, and route the agent's model provider to that local endpoint. All changes are within the `soothe-daemon` package — `soothe-nano` and `soothe` are not modified.

## Prerequisites

- [x] RFC-233 proposed
- [x] RFC-221 implemented (Loop Runner Protocol and Ray)
- [x] Design draft reviewed (`docs/drafts/2026-09-13-ray-actor-gpu-local-model-design.md`)

## Implementation Plan

### Phase 1: Configuration Schema

**Goal**: Add `LocalModelConfig` and extend `RayConfig` with `num_gpus` + `local_model`.

**Tasks**:
- [ ] Add `LocalModelConfig` class to `config/models.py`
- [ ] Add `num_gpus` and `local_model` fields to `RayConfig`
- [ ] Add model validator: `local_model is not None → num_gpus >= 1.0`
- [ ] Add validator: `tensor_parallel_size > 1 → num_gpus >= tensor_parallel_size`

**Files**:
- `packages/soothe-daemon/src/soothe_daemon/config/models.py` (modified)

### Phase 2: LocalModelServer

**Goal**: Implement the model server lifecycle manager.

**Tasks**:
- [ ] Create `runner/local_model_server.py`
- [ ] Implement `LocalModelServer.__init__`, `launch()`, `shutdown()`
- [ ] Implement vLLM backend: subprocess spawn with CLI args
- [ ] Implement Ollama backend: subprocess spawn
- [ ] Implement port auto-allocation (ephemeral range 8765–8800)
- [ ] Implement health check with exponential backoff
- [ ] Implement `LocalModelStartupError` exception
- [ ] Register `atexit` cleanup hook

**Files**:
- `packages/soothe-daemon/src/soothe_daemon/runner/local_model_server.py` (new)

### Phase 3: LoopRunnerActor Extension

**Goal**: Extend the actor to launch local model and inject provider.

**Tasks**:
- [ ] Add `local_model_config` parameter to `__init__`
- [ ] Add `_inject_local_provider()` method
- [ ] Add `_override_router_roles()` helper
- [ ] Add `__ray_terminate__` for cleanup

**Files**:
- `packages/soothe-daemon/src/soothe_daemon/runner/ray_actor.py` (modified)

### Phase 4: RayLoopRunner Extension

**Goal**: Pass local model config to actor, propagate GPU resources.

**Tasks**:
- [ ] Read `local_model` from `daemon_config` in `__init__`
- [ ] Pass `local_model_config` to actor constructor in `run()`
- [ ] Add `num_gpus` to `_actor_options` in `_ensure_ray_init()`

**Files**:
- `packages/soothe-daemon/src/soothe_daemon/runner/ray_runner.py` (modified)

### Phase 5: Unit Tests

**Goal**: Full unit test coverage without GPU or live model server.

**Tasks**:
- [ ] `test_local_model_config.py` — config validation
- [ ] `test_local_model_server.py` — launch/shutdown with mocked subprocess
- [ ] `test_ray_actor_local_model.py` — provider injection logic
- [ ] `test_ray_config_gpu.py` — RayConfig validator

**Files**:
- `packages/soothe-daemon/tests/unit/runner/test_local_model_config.py` (new)
- `packages/soothe-daemon/tests/unit/runner/test_local_model_server.py` (new)
- `packages/soothe-daemon/tests/unit/runner/test_ray_actor_local_model.py` (new)
- `packages/soothe-daemon/tests/unit/config/test_ray_config_gpu.py` (new)

### Phase 6: Verification

**Goal**: Run `verify_finally.sh` and fix any issues.

**Tasks**:
- [ ] Run `./scripts/verify_finally.sh`
- [ ] Fix lint/type/test failures
- [ ] Verify no changes to `soothe-nano` or `soothe` packages

## File Structure

```
packages/soothe-daemon/
├── src/soothe_daemon/
│   ├── config/
│   │   └── models.py              # Modified: +LocalModelConfig, +RayConfig fields
│   └── runner/
│       ├── local_model_server.py  # New: LocalModelServer class
│       ├── ray_actor.py           # Modified: +local_model_config param
│       └── ray_runner.py          # Modified: +GPU options, +local_model pass-through
└── tests/unit/
    ├── runner/
    │   ├── test_local_model_config.py      # New
    │   ├── test_local_model_server.py      # New
    │   └── test_ray_actor_local_model.py   # New
    └── config/
        └── test_ray_config_gpu.py          # New
```

## Backward Compatibility

- `RayConfig` new fields have defaults (`num_gpus=0.0`, `local_model=None`) — existing configs unchanged
- `LoopRunnerActor.__init__` new parameter is optional (`None` default) — existing callers unaffected
- `RayLoopRunner` only reads `local_model` when `daemon_config` is provided
- No changes to `soothe-nano` or `soothe` packages

## Out of Scope

- Multi-actor model sharing (RFC-233 §9.1)
- Ray Serve deployment (RFC-233 §9.2)
- In-process vLLM engine (RFC-233 §9.3)
- Local embedding model (RFC-233 §10.3)
