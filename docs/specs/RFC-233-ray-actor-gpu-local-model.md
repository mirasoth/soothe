# RFC-233: Ray Actor GPU-Bound Local Model Inference

**RFC**: 233
**Title**: GPU-Bound Ray Actor with Local Model Server for On-Device Inference
**Status**: Proposed
**Kind**: Architecture Design
**Created**: 2026-09-13
**Authors**: Soothe Team
**Dependencies**: RFC-221, RFC-001

---

## Abstract

This RFC extends the existing Ray actor loop runner architecture (RFC-221) to enable **GPU-bound Ray actors** that launch a local model server (vLLM or Ollama) inside the actor process and route the agent's model provider to that local endpoint for inference. Each actor reserves one or more GPUs, starts the model server at actor construction time, and the standard `LLMFactory` → `ProviderRegistry` → `ChatLitellmModel` chain resolves the local model like any other OpenAI-compatible provider — no changes to `soothe-nano` or `soothe` packages.

---

## Motivation

### Current Limitation

The `LoopRunnerActor` (`@ray.remote`) hosts a `SootheRunner` inside a Ray worker process. The runner's `LLMFactory` → `ProviderRegistry` → `ChatLitellmModel` chain routes LLM calls to remote API endpoints (OpenAI, Anthropic, DashScope). There is no mechanism to:

1. Reserve GPU resources on the Ray actor.
2. Launch a local model server (vLLM, Ollama) inside the actor process.
3. Inject the local model endpoint into the agent's model provider configuration so inference stays on-device.

### Goals

- Enable GPU-bound Ray actors that run the full agent stack — including inference — locally.
- Zero remote API dependency for configured model roles (`default`, `fast`, `think`).
- Config-driven, opt-in: when `local_model` is absent, existing behavior is unchanged.
- No modifications to `soothe-nano` or `soothe` packages.

### Non-Goals

- Multi-actor model sharing (deferred — see §9.1).
- Ray Serve deployment (deferred — see §9.2).
- In-process vLLM engine bypass (deferred — see §9.3).
- Local embedding model (out of scope — see §10.3).

---

## Design Principles

### Extend, don't replace

The `LoopRunnerProtocol` / `RayLoopRunner` / `LoopRunnerActor` architecture (RFC-221) stays unchanged. We add a **model-serving layer** inside the actor, between `__init__` (model launch) and `run` (agent loop execution). The streaming queue bridge (`ray.util.queue.Queue`) and cooperative cancellation are untouched.

### One actor = one GPU = one model instance

Each `LoopRunnerActor` reserves `num_gpus=1` (or `tensor_parallel_size`) and launches exactly one local model server. The model server's lifecycle is bound to the actor: it starts in `__init__`, serves during `run`, and is cleaned up on actor termination.

### Local model is a provider, not a bypass

The local model server exposes an OpenAI-compatible HTTP endpoint. We register it as a standard `ModelProviderConfig` (`provider_type="openai"`, `api_base_url="http://127.0.0.1:<port>/v1"`). The existing `LLMFactory` → `ProviderRegistry` → `ChatLitellmModel` chain resolves it like any other provider — no special-casing in the inference path.

### Config-driven, opt-in

GPU-bound local model mode is activated by a new `local_model` config block on `RayConfig`. When absent, the existing behavior is unchanged (remote API providers).

---

## Architecture

```
SootheDaemon
    │
    │  creates per loop_id
    ▼
RayLoopRunner  (existing, unchanged interface)
    │
    │  _ensure_ray_init() — now reads num_gpus from RayConfig
    │  actor_cls.options(num_gpus=1, ...)
    ▼
LoopRunnerActor  (@ray.remote, GPU-bound)
    │
    ├── __init__:
    │     ├── LocalModelServer.launch()  ← starts vLLM/Ollama on 127.0.0.1:<port>
    │     ├── inject local provider into SootheConfig.providers
    │     └── SootheRunner(config)  ← LLMFactory sees local provider
    │
    ├── run(request, queue):
    │     ├── stream_turn_overrides(model="local_gpu:<model_name>", ...)
    │     └── self._runner.astream(...)  ← ChatLitellmModel → http://127.0.0.1:<port>/v1
    │
    └── __ray_terminate__:
          └── LocalModelServer.shutdown()
```

### Data Flow

```
User query
    → LoopRunRequest (model="local_gpu:qwen3-32b", router_profile="local-gpu")
    → RayLoopRunner.run(request)
    → LoopRunnerActor.run(request, queue)
        → stream_turn_overrides(model="local_gpu:qwen3-32b")
        → SootheRunner.astream(...)
            → LLMFactory.create_chat_model("default")
                → ProviderRegistry.resolve("local_gpu", "qwen3-32b")
                    → ResolvedProvider(litellm_model="openai/qwen3-32b",
                                       api_base="http://127.0.0.1:8765/v1")
                → ChatLitellmModel(model="openai/qwen3-32b", api_base=...)
            → litellm.acompletion(model="openai/qwen3-32b", api_base=..., ...)
                → HTTP POST → 127.0.0.1:8765/v1/chat/completions
                    → vLLM server (in-process, GPU-bound)
    → StreamChunk → ray.util.queue.Queue → RayLoopRunner → QueryEngine
```

---

## Components

### `LocalModelServer` — model lifecycle manager

New class in `packages/soothe-daemon/src/soothe_daemon/runner/local_model_server.py`.

**Responsibility:** Launch, health-check, and shut down a local model server inside a Ray actor process.

```python
class LocalModelServer:
    """Manages a local model server (vLLM/Ollama) inside a Ray actor.

    Launched in LoopRunnerActor.__init__ when RayConfig.local_model is
    configured. Exposes an OpenAI-compatible endpoint that the agent's
    LLMFactory routes to as a standard provider.

    Lifecycle:
        launch() → wait for health check → serve during actor lifetime
        shutdown() → terminate server process on actor cleanup
    """

    def __init__(self, config: LocalModelConfig) -> None: ...
    def launch(self) -> str: ...          # returns api_base URL
    def shutdown(self) -> None: ...
    @property
    def api_base(self) -> str: ...        # "http://127.0.0.1:<port>/v1"
    @property
    def provider_name(self) -> str: ...   # "local_gpu" or config-specified
```

**Backend selection:** `LocalModelConfig.backend` selects between `"vllm"` and `"ollama"`.

- **vLLM:** Spawned as a subprocess via `vllm.entrypoints.openai.api_server` with `--model`, `--port`, `--gpu-memory-utilization`, `--tensor-parallel-size` flags.
- **Ollama:** Spawned via `ollama serve` subprocess; model pre-loaded via `ollama pull <model>` (or assumed already pulled).

**Port allocation:** `LocalModelConfig.port` (default 0 = auto-allocate from ephemeral range 8765–8800).

**Health check:** Poll `GET <api_base>/health` (vLLM) or `GET <api_base>/api/tags` (Ollama) with exponential backoff, up to `startup_timeout_seconds` (default 120s). Raise `LocalModelStartupError` on timeout.

**Cleanup:** `shutdown()` sends SIGTERM to the server subprocess, waits 5s, then SIGKILL if still alive. Registered via `atexit` as a safety net.

### `LocalModelConfig` — configuration model

New Pydantic model added to `RayConfig` in `packages/soothe-daemon/src/soothe_daemon/config/models.py`.

```python
class LocalModelConfig(BaseModel):
    """Local model server configuration for GPU-bound Ray actors."""

    backend: Literal["vllm", "ollama"] = "vllm"
    model_name: str
    provider_name: str = "local_gpu"
    port: int = 0
    gpu_memory_utilization: float = Field(default=0.9, ge=0.1, le=1.0)
    tensor_parallel_size: int = Field(default=1, ge=1)
    max_model_len: int | None = None
    quantization: str | None = None
    startup_timeout_seconds: int = Field(default=120, ge=10, le=600)
    extra_args: list[str] = Field(default_factory=list)
    override_roles: list[str] = Field(default_factory=lambda: ["default"])
```

**Extension to `RayConfig`:**

```python
class RayConfig(BaseModel):
    # ... existing fields ...
    num_gpus: float = Field(default=0.0)
    local_model: LocalModelConfig | None = Field(default=None)
```

**Validation:** A model validator on `RayConfig` ensures `local_model is not None → num_gpus >= 1.0`.

### `LoopRunnerActor` — extended initialization

Modified `__init__` in `packages/soothe-daemon/src/soothe_daemon/runner/ray_actor.py`:

```python
@ray.remote
class LoopRunnerActor:
    def __init__(self, config: object,
                 local_model_config: LocalModelConfig | None = None) -> None:
        from soothe.runner import SootheRunner

        self._model_server: LocalModelServer | None = None
        if local_model_config is not None:
            self._model_server = LocalModelServer(local_model_config)
            api_base = self._model_server.launch()
            config = self._inject_local_provider(config, local_model_config, api_base)

        self._runner = SootheRunner(config)
        self._cancelled = False
```

The `_inject_local_provider` method deep-copies the `SootheConfig`, appends a `ModelProviderConfig` with the discovered `api_base_url`, and overrides the specified router roles to point at `provider_name:model_name`.

### `RayLoopRunner` — pass local model config to actor

Modified `__init__` reads `local_model` from `daemon_config.loop_runner.ray`. Modified `run()` passes `local_model_config` to the actor constructor and applies `num_gpus` to actor options.

### `_ensure_ray_init` — propagate GPU resource

The function already reads `RayConfig` and populates `_actor_options`. We add `num_gpus` to the actor options when `local_model` is configured.

---

## Configuration Example

```yaml
loop_runner:
  runner_mode: ray
  ray:
    address: null
    num_gpus: 1.0
    max_concurrent_actors: 2
    log_to_driver: false
    local_model:
      backend: vllm
      model_name: Qwen/Qwen3-32B
      provider_name: local_gpu
      port: 0
      gpu_memory_utilization: 0.9
      tensor_parallel_size: 1
      max_model_len: 32768
      quantization: awq
      startup_timeout_seconds: 180
      override_roles:
        - default
        - fast
        - think

providers:
  - name: openai
    provider_type: openai
    api_key: ${OPENAI_API_KEY}

router_profiles:
  - name: default
    router:
      default: local_gpu:Qwen/Qwen3-32B
      fast: local_gpu:Qwen/Qwen3-32B
      think: local_gpu:Qwen/Qwen3-32B
      image: openai:gpt-4o
```

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Model server startup failure | `LocalModelStartupError` in `__init__`; actor fails to construct; `RayActorError` propagated to client as stream error chunk |
| Model server crash mid-loop | `litellm` raises `ConnectionError`; runner wraps as stream error; `LocalModelServer` attempts one-time restart (up to 60s) |
| GPU OOM | vLLM crash detected; no restart (non-recoverable); error: "Reduce gpu_memory_utilization" |
| Port conflict | `port=0` auto-allocates; if specific port occupied, tries next port in `[port, port+10)` |

---

## Testing Strategy

### Unit tests (no GPU, no live model server)

| Test | File | Description |
|------|------|-------------|
| `LocalModelConfig` validation | `tests/unit/runner/test_local_model_config.py` | Backend enum, port range, num_gpus validator |
| `LocalModelServer` launch mock | `tests/unit/runner/test_local_model_server.py` | Mock subprocess, verify port discovery + health check |
| `_inject_local_provider` | `tests/unit/runner/test_ray_actor_local_model.py` | Verify provider injection and router override |
| `RayConfig` validator | `tests/unit/config/test_ray_config.py` | `local_model` set → `num_gpus >= 1` |

### Integration tests (marked `@pytest.mark.gpu`)

| Test | Description |
|------|------|
| `test_actor_launches_vllm` | End-to-end: actor launches vLLM, health check passes, simple completion works |
| `test_actor_routes_to_local_model` | Verify `LLMFactory` resolves to `127.0.0.1:<port>` |
| `test_actor_shutdown_cleans_up` | Actor kill → vLLM subprocess terminated, port released |

GPU tests are skipped in CI (`@pytest.mark.skipif(not HAS_GPU)`).

---

## Package Boundaries

Per `AGENTS.md` Rule 7b (Package Boundaries):

| Component | Package | Rationale |
|-----------|---------|-----------|
| `LocalModelServer` | `soothe-daemon` | Manages subprocess lifecycle tied to Ray actor; daemon-specific |
| `LocalModelConfig` | `soothe-daemon` (config/models.py) | Extends `RayConfig`, daemon-owned |
| `LoopRunnerActor` modifications | `soothe-daemon` (runner/ray_actor.py) | Actor class lives in daemon |
| `RayLoopRunner` modifications | `soothe-daemon` (runner/ray_runner.py) | Runner class lives in daemon |
| `SootheConfig` / `ModelProviderConfig` | `soothe-nano` (unchanged) | No changes — provider injection is runtime |
| `LLMFactory` / `ProviderRegistry` | `soothe-nano` (unchanged) | No changes — resolves local provider like any other |

**Key invariant:** `soothe-nano` and `soothe` packages are **not modified**. The local model integration is entirely within `soothe-daemon`, injected at actor construction time via config manipulation. This respects the one-way DAG: `soothe-daemon → soothe → soothe-nano → soothe-sdk`.

---

## Alternatives Considered

### Separate `ModelServerActor` (rejected)

A dedicated `@ray.remote(num_gpus=1)` actor serving multiple `LoopRunnerActor` instances.

**Why rejected:** Adds inter-actor communication complexity and service discovery. The user explicitly wants one-actor-one-GPU binding. If sharing is needed later, a `ModelServerActor` can be added without changing the `LoopRunnerActor` interface.

### Ray Serve deployment (rejected)

Deploy the model as a Ray Serve deployment with autoscaling.

**Why rejected:** Overkill for the one-GPU-per-actor use case. Can be revisited if multi-replica serving is needed.

### In-process vLLM engine (deferred)

Use `vllm.LLM` directly in-process instead of spawning an HTTP server.

**Why deferred:** Would bypass `ChatLitellmModel` and litellm entirely, requiring a new `BaseChatModel` subclass. The HTTP server approach keeps the inference path unchanged.

---

## Open Questions

1. **Multi-GPU tensor parallelism:** When `tensor_parallel_size > 1`, the actor needs `num_gpus = tensor_parallel_size`. The config validator should enforce this.
2. **Model warm-start across actors:** Each actor pays the full model load cost. A shared model cache could help but is out of scope.
3. **Embedding model:** Only chat roles are covered. Embedding still uses remote. A `local_embedding` config could be added if needed.
4. **Graceful shutdown on Ray cluster teardown:** The actor should implement `__ray_terminate__` to ensure cleanup.

---

## References

- RFC-221: Loop Runner Protocol and Subprocess Isolation
- RFC-001: Core Modules Architecture
