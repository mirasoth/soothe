# Ray Actor GPU-Bound Local Model Agent — Design Draft

**Status:** Draft
**Date:** 2026-09-13
**Author:** Soothe Team
**Scope:** Extend the existing Ray actor loop runner (`LoopRunnerActor` / `RayLoopRunner`) to bind GPU resources, launch a local model server inside the actor, and route the agent's model provider to that local endpoint for inference.

---

## 1. Problem

The current `LoopRunnerActor` (`@ray.remote`) hosts a `SootheRunner` inside a Ray worker process. The runner's `LLMFactory` → `ProviderRegistry` → `ChatLitellmModel` chain routes LLM calls to remote API endpoints (OpenAI, Anthropic, DashScope, etc.). There is no mechanism to:

1. Reserve GPU resources on the Ray actor.
2. Launch a local model server (vLLM, Ollama, etc.) inside the actor process.
3. Inject the local model endpoint into the agent's model provider configuration so inference stays on-device.

The goal is to enable **GPU-bound Ray actors** that run the full agent stack — including inference — locally, with zero remote API dependency for the configured model roles.

---

## 2. Design Principles

### 2.1 Extend, don't replace

The existing `LoopRunnerProtocol` / `RayLoopRunner` / `LoopRunnerActor` architecture (RFC-221) stays unchanged. We add a **model-serving layer** inside the actor, between `__init__` (model launch) and `run` (agent loop execution). The streaming queue bridge (`ray.util.queue.Queue`) and cooperative cancellation are untouched.

### 2.2 One actor = one GPU = one model instance

Each `LoopRunnerActor` reserves `num_gpus=1` and launches exactly one local model server. The model server's lifecycle is bound to the actor: it starts in `__init__`, serves during `run`, and is cleaned up on actor termination. This matches the user's requirement of one-GPU-per-actor binding.

### 2.3 Local model is a provider, not a bypass

The local model server exposes an OpenAI-compatible HTTP endpoint. We register it as a standard `ModelProviderConfig` (provider_type=`"openai"`, api_base_url=`"http://127.0.0.1:<port>/v1"`). The existing `LLMFactory` → `ProviderRegistry` → `ChatLitellmModel` chain resolves it like any other provider — no special-casing in the inference path.

### 2.4 Config-driven, opt-in

GPU-bound local model mode is activated by a new `local_model` config block on `RayConfig`. When absent, the existing behavior is unchanged (remote API providers).

---

## 3. Architecture

```
SootheDaemon
    │
    │  creates per loop_id
    ▼
RayLoopRunner  (existing, unchanged interface)
    │
    │  _ensure_ray_init() — now reads num_gpus from RayConfig.local_model
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
    │     ├── stream_turn_overrides(model="local_vllm:<model_name>", ...)
    │     └── self._runner.astream(...)  ← ChatLitellmModel → http://127.0.0.1:<port>/v1
    │
    └── __del__ / cleanup:
          └── LocalModelServer.shutdown()
```

### 3.1 Data flow

```
User query
    → LoopRunRequest (model="local_vllm:qwen3-32b", router_profile="local-gpu")
    → RayLoopRunner.run(request)
    → LoopRunnerActor.run(request, queue)
        → stream_turn_overrides(model="local_vllm:qwen3-32b")
        → SootheRunner.astream(...)
            → LLMFactory.create_chat_model("default")
                → ProviderRegistry.resolve("local_vllm", "qwen3-32b")
                    → ResolvedProvider(litellm_model="openai/qwen3-32b",
                                       api_base="http://127.0.0.1:8765/v1")
                → ChatLitellmModel(model="openai/qwen3-32b", api_base=...)
            → litellm.acompletion(model="openai/qwen3-32b", api_base=..., ...)
                → HTTP POST → 127.0.0.1:8765/v1/chat/completions
                    → vLLM server (in-process, GPU-bound)
    → StreamChunk → ray.util.queue.Queue → RayLoopRunner → QueryEngine
```

---

## 4. Components

### 4.1 `LocalModelServer` — model lifecycle manager

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
    def provider_name(self) -> str: ...   # "local_vllm" or config-specified
```

**Backend selection:** `LocalModelConfig.backend` selects between `"vllm"` and `"ollama"`.

- **vLLM:** Spawned as a subprocess via `vllm.entrypoints.openai.api_server` with `--model`, `--port`, `--gpu-memory-utilization`, `--tensor-parallel-size` flags. The server runs in the same process group as the actor.
- **Ollama:** Spawned via `ollama serve` subprocess; model pre-loaded via `ollama pull <model>` (or assumed already pulled).

**Port allocation:** `LocalModelConfig.port` (default 0 = auto-allocate from ephemeral range 8765–8800). The server writes its actual port to a temp file; `LocalModelServer` reads it back.

**Health check:** Poll `GET <api_base>/health` (vLLM) or `GET <api_base>/api/tags` (Ollama) with exponential backoff, up to `startup_timeout_seconds` (default 120s). Raise `LocalModelStartupError` on timeout.

**Cleanup:** `shutdown()` sends SIGTERM to the server subprocess, waits 5s, then SIGKILL if still alive. Registered via `atexit` as a safety net.

### 4.2 `LocalModelConfig` — configuration model

New Pydantic model added to `RayConfig` in `packages/soothe-daemon/src/soothe_daemon/config/models.py`.

```python
class LocalModelConfig(BaseModel):
    """Local model server configuration for GPU-bound Ray actors.

    When set on RayConfig.local_model, each LoopRunnerActor launches
    a local model server and routes inference to it.
    """

    backend: Literal["vllm", "ollama"] = "vllm"
    """Inference backend: vLLM (high-throughput, GPU) or Ollama (lightweight)."""

    model_name: str
    """HuggingFace model ID (vLLM) or Ollama model tag."""

    provider_name: str = "local_gpu"
    """Provider name injected into SootheConfig.providers for routing."""

    port: int = 0
    """Port for the local server (0 = auto-allocate from 8765–8800)."""

    gpu_memory_utilization: float = Field(default=0.9, ge=0.1, le=1.0)
    """vLLM GPU memory fraction (vLLM backend only)."""

    tensor_parallel_size: int = Field(default=1, ge=1)
    """vLLM tensor parallelism (must match num_gpus for multi-GPU)."""

    max_model_len: int | None = None
    """Maximum context length override (vLLM --max-model-len)."""

    quantization: str | None = None
    """Quantization method (e.g. 'awq', 'gptq', None for fp16)."""

    startup_timeout_seconds: int = Field(default=120, ge=10, le=600)
    """Max seconds to wait for the model server to become healthy."""

    extra_args: list[str] = Field(default_factory=list)
    """Additional CLI args passed to the backend server."""

    # Router profile override — which ModelRole(s) route to local model
    override_roles: list[ModelRole] = Field(default_factory=lambda: ["default"])
    """Model roles to override with the local model (default: just 'default').
    Set to ['default','fast','think'] to route all chat roles locally."""
```

**Extension to `RayConfig`:**

```python
class RayConfig(BaseModel):
    # ... existing fields ...

    num_gpus: float = Field(
        default=0.0,
        description="GPUs to reserve per Ray actor (0 = no GPU). "
        "Set to 1.0 when local_model is configured.",
    )
    local_model: LocalModelConfig | None = Field(
        default=None,
        description="When set, each Ray actor launches a local model server "
        "and routes inference to it. Requires num_gpus >= 1.",
    )
```

**Validation:** A model validator on `RayConfig` ensures `local_model is not None → num_gpus >= 1.0`.

### 4.3 `LoopRunnerActor` — extended initialization

Modified `__init__` in `packages/soothe-daemon/src/soothe_daemon/runner/ray_actor.py`:

```python
@ray.remote
class LoopRunnerActor:
    def __init__(self, config: object, local_model_config: LocalModelConfig | None = None) -> None:
        from soothe.runner import SootheRunner

        # Launch local model server if configured.
        self._model_server: LocalModelServer | None = None
        if local_model_config is not None:
            self._model_server = LocalModelServer(local_model_config)
            api_base = self._model_server.launch()
            # Inject local provider into config.
            config = self._inject_local_provider(config, local_model_config, api_base)

        self._runner = SootheRunner(config)
        self._cancelled = False

    def _inject_local_provider(
        self,
        config: SootheConfig,
        lm_config: LocalModelConfig,
        api_base: str,
    ) -> SootheConfig:
        """Add a local model provider and override router roles."""
        local_provider = ModelProviderConfig(
            name=lm_config.provider_name,
            provider_type="openai",
            api_base_url=api_base,
            streaming=True,
        )
        # Deep-copy config, append provider, override router roles.
        config_dict = config.model_dump()
        config_dict.setdefault("providers", []).append(local_provider.model_dump())
        # Override router roles to point at local provider.
        self._override_router_roles(config_dict, lm_config)
        return SootheConfig(**config_dict)

    def _override_router_roles(self, config_dict: dict, lm_config: LocalModelConfig) -> None:
        """Point specified ModelRoles at the local provider:model spec."""
        local_spec = f"{lm_config.provider_name}:{lm_config.model_name}"
        for profile in config_dict.get("router_profiles", []):
            for role in lm_config.override_roles:
                profile["router"][role] = local_spec
```

### 4.4 `RayLoopRunner` — pass local model config to actor

Modified `run()` in `packages/soothe-daemon/src/soothe_daemon/runner/ray_runner.py`:

```python
class RayLoopRunner:
    def __init__(self, loop_id, config, daemon_config=None):
        # ... existing ...
        self._local_model_config = None
        if daemon_config and daemon_config.loop_runner.ray.local_model:
            self._local_model_config = daemon_config.loop_runner.ray.local_model

    async def run(self, request):
        _ensure_ray_init(self._daemon_config)

        # Build actor with GPU options when local_model is configured.
        actor_cls = LoopRunnerActor
        opts = dict(_actor_options)
        if self._local_model_config is not None:
            ray_config = self._daemon_config.loop_runner.ray
            opts["num_gpus"] = ray_config.num_gpus
        if opts:
            actor_cls = actor_cls.options(**opts)

        self._actor = actor_cls.remote(
            self._config,
            local_model_config=self._local_model_config,
        )
        # ... rest unchanged ...
```

### 4.5 `_ensure_ray_init` — propagate GPU resource

The function already reads `RayConfig` and populates `_actor_options`. We add `num_gpus` to the actor options when `local_model` is configured:

```python
def _ensure_ray_init(daemon_config):
    # ... existing logic ...
    if ray_config is not None:
        # ... existing num_cpus, object_store_memory ...
        if ray_config.num_gpus > 0:
            actor_opts["num_gpus"] = ray_config.num_gpus
```

---

## 5. Configuration Example

```yaml
# config/local-gpu-agent.yaml
loop_runner:
  runner_mode: ray
  ray:
    address: null  # auto-detect local cluster
    num_gpus: 1.0
    max_concurrent_actors: 2  # one GPU per actor → 2 concurrent loops
    log_to_driver: false
    local_model:
      backend: vllm
      model_name: Qwen/Qwen3-32B
      provider_name: local_gpu
      port: 0  # auto-allocate
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
  - name: openai  # fallback for image/ocr/embedding roles
    provider_type: openai
    api_key: ${OPENAI_API_KEY}

router_profiles:
  - name: default
    router:
      default: local_gpu:Qwen/Qwen3-32B  # overridden at runtime
      fast: local_gpu:Qwen/Qwen3-32B
      think: local_gpu:Qwen/Qwen3-32B
      image: openai:gpt-4o  # stays remote
```

At runtime, the actor's `_inject_local_provider` appends the `local_gpu` provider with the discovered `api_base_url` and overrides the router to point `default`/`fast`/`think` roles at `local_gpu:Qwen/Qwen3-32B`.

---

## 6. Error Handling

### 6.1 Model server startup failure

If `LocalModelServer.launch()` times out or the health check fails:
- Raise `LocalModelStartupError` inside `LoopRunnerActor.__init__`.
- The actor fails to construct; `RayLoopRunner.run()` catches the `RayActorError` and propagates it as a stream error chunk to the client.
- The client sees: `"Local model server failed to start: <details>"`.

### 6.2 Model server crash mid-loop

If the vLLM/Ollama subprocess dies during `run()`:
- `litellm.acompletion()` raises a `ConnectionError` or `httpx.ConnectError`.
- The runner's existing error handling wraps it as a `StreamChunk` error event.
- `LocalModelServer` monitors the subprocess PID; on unexpected exit, it logs the exit code and attempts a one-time restart (up to `restart_timeout_seconds`, default 60s).

### 6.3 GPU OOM

vLLM handles OOM internally via PagedAttention. If the server crashes with CUDA OOM:
- `LocalModelServer` detects subprocess exit, logs the error, and does NOT restart (OOM is non-recoverable without reducing `gpu_memory_utilization`).
- The error surfaces as: `"Local model server exited (OOM). Reduce gpu_memory_utilization."`.

### 6.4 Port conflict

`port=0` auto-allocates from ephemeral range. If a specific port is set and occupied, `launch()` tries the next port in range `[port, port+10)` before failing.

---

## 7. Testing

### 7.1 Unit tests

| Test | File | Description |
|------|------|-------------|
| `LocalModelConfig` validation | `tests/unit/runner/test_local_model_config.py` | Backend enum, port range, num_gpus validator |
| `LocalModelServer` launch mock | `tests/unit/runner/test_local_model_server.py` | Mock subprocess, verify port discovery + health check |
| `_inject_local_provider` | `tests/unit/runner/test_ray_actor_local_model.py` | Verify provider injection and router override |
| `RayConfig` validator | `tests/unit/config/test_ray_config.py` | `local_model` set → `num_gpus >= 1` |

### 7.2 Integration tests (marked `@pytest.mark.gpu`)

| Test | Description |
|------|-------------|
| `test_actor_launches_vllm` | End-to-end: actor launches vLLM, health check passes, simple completion works |
| `test_actor_routes_to_local_model` | Verify `LLMFactory.create_chat_model("default")` resolves to `127.0.0.1:<port>` |
| `test_actor_shutdown_cleans_up` | Actor kill → vLLM subprocess terminated, port released |

GPU tests are skipped in CI (`@pytest.mark.skipif(not HAS_GPU)`).

---

## 8. Package Boundaries

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

## 9. Alternatives Considered

### 9.1 Separate `ModelServerActor` (rejected)

A dedicated `@ray.remote(num_gpus=1)` actor serving multiple `LoopRunnerActor` instances via Ray Serve or direct RPC.

**Why rejected:** Adds inter-actor communication complexity, service discovery, and connection management. The user explicitly wants one-actor-one-GPU binding. If sharing is needed later, a `ModelServerActor` can be added without changing the `LoopRunnerActor` interface — the local provider's `api_base` would point at the shared actor's endpoint instead of `127.0.0.1`.

### 9.2 Ray Serve deployment (rejected)

Deploy the model as a Ray Serve deployment with autoscaling.

**Why rejected:** Overkill for the one-GPU-per-actor use case. Ray Serve adds deployment management, replica tracking, and routing layers that don't serve a single-actor-single-model scenario. Can be revisited if multi-replica serving is needed.

### 9.3 In-process vLLM engine (deferred)

Use `vllm.LLM` directly in-process instead of spawning an HTTP server.

**Why deferred:** Would bypass `ChatLitellmModel` and litellm entirely, requiring a new `BaseChatModel` subclass. The HTTP server approach keeps the inference path unchanged (litellm → HTTP → vLLM server). An in-process engine can be added later as a performance optimization if HTTP overhead proves significant.

---

## 10. Open Questions

1. **Multi-GPU tensor parallelism:** When `tensor_parallel_size > 1`, the actor needs `num_gpus = tensor_parallel_size`. The config validator should enforce this. Deferred to implementation.

2. **Model warm-start across actors:** If multiple actors load the same model, each pays the full load cost. A shared model cache (Ray object store) could help but is out of scope for this design.

3. **Embedding model:** The local model config only covers chat roles (`default`/`fast`/`think`). Embedding still uses the configured `embedding_profile` (remote). A `local_embedding` config could be added if needed.

4. **Graceful shutdown on Ray cluster teardown:** `LocalModelServer.shutdown()` is registered via `atexit`, but Ray's actor killing may not trigger `__del__` reliably. The actor should implement `__ray_terminate__` to ensure cleanup.
