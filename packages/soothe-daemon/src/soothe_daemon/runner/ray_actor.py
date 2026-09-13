"""Ray actor hosting SootheRunner in an isolated worker process."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import ray
from ray.util.queue import Queue
from soothe.protocols.runner import LoopRunRequest

if TYPE_CHECKING:
    from soothe_daemon.config.models import LocalModelConfig

logger = logging.getLogger(__name__)


@ray.remote
class LoopRunnerActor:
    """Ray actor hosting one SootheRunner in a worker process.

    Constructed once per loop via ``LoopRunnerActor.remote(config)``.
    Streams chunks into a caller-supplied ``ray.util.queue.Queue``.

    When ``local_model_config`` is provided, the actor launches a local
    model server (vLLM/Ollama) at construction time and injects it as a
    standard provider into the agent's config — enabling GPU-bound
    on-device inference without modifying the inference path.

    Terminal message types: "chunk", "done", "error", "cancelled".
    """

    def __init__(
        self,
        config: object,
        local_model_config: LocalModelConfig | None = None,
    ) -> None:
        from soothe.runner import SootheRunner

        # Launch local model server if configured.
        self._model_server = None
        if local_model_config is not None:
            from soothe_daemon.runner.local_model_server import LocalModelServer

            self._model_server = LocalModelServer(local_model_config)
            api_base = self._model_server.launch()
            config = self._inject_local_provider(
                config,
                local_model_config,
                api_base,
            )
            logger.info(
                "LoopRunnerActor: local model server ready at %s (provider=%s, model=%s)",
                api_base,
                local_model_config.provider_name,
                local_model_config.model_name,
            )

        self._runner = SootheRunner(config)  # type: ignore[arg-type]
        self._cancelled = False

    def _inject_local_provider(
        self,
        config: object,
        lm_config: LocalModelConfig,
        api_base: str,
    ) -> object:
        """Add a local model provider and override router roles.

        Deep-copies the SootheConfig, appends a ``ModelProviderConfig``
        with the discovered ``api_base_url``, and overrides the
        specified router roles to point at ``provider:model``.
        """
        config_dict = _config_to_dict(config)

        # Append local provider.
        local_provider = {
            "name": lm_config.provider_name,
            "provider_type": "openai",
            "api_base_url": api_base,
            "streaming": True,
        }
        providers = config_dict.setdefault("providers", [])
        # Remove any existing provider with the same name.
        providers = [p for p in providers if p.get("name") != lm_config.provider_name]
        providers.append(local_provider)
        config_dict["providers"] = providers

        # Override router roles in all profiles.
        local_spec = f"{lm_config.provider_name}:{lm_config.model_name}"
        for profile in config_dict.get("router_profiles", []):
            router = profile.get("router", {})
            for role in lm_config.override_roles:
                router[role] = local_spec

        return _dict_to_config(config_dict)

    async def run(self, request: LoopRunRequest, queue: Queue) -> None:
        """Stream chunks from SootheRunner.astream() into queue."""
        from soothe.runner.worker_logging import (
            configure_loop_runner_worker_logging,
            release_loop_runner_logging,
        )

        configure_loop_runner_worker_logging(self._runner.config, request.loop_id)

        try:
            from soothe_nano.utils.runtime import stream_turn_overrides

            with stream_turn_overrides(
                model=request.model,
                model_params=request.model_params or None,
                router_profile=request.router_profile,
            ):
                async for chunk in self._runner.astream(
                    request.user_input,
                    thread_id=request.thread_id,
                    workspace=request.resolve_workspace_path(),
                    preferred_subagent=request.preferred_subagent,
                    intake_scope=request.intake_scope,
                    client_loop_id=request.loop_id,
                    autopilot_job=request.autopilot_job,
                    clarification_mode=request.clarification_mode,
                    interaction_mode=request.interaction_mode,
                    clarification_answer=request.clarification_answer,
                    clarification_answers=request.clarification_answers,
                    resume_interrupted=request.resume_interrupted,
                    approved_plan_path=request.approved_plan_path,
                    autopilot_rail_id=request.autopilot_rail_id,
                ):
                    if self._cancelled:
                        await queue.put_async(("cancelled", None))
                        return
                    await queue.put_async(("chunk", chunk))
        except Exception as exc:  # noqa: BLE001
            await queue.put_async(("error", exc))
            return
        finally:
            try:
                release_loop_runner_logging(request.loop_id)
            except Exception:
                logger.debug(
                    "Ray actor: runner logging release failed loop=%s",
                    request.loop_id,
                    exc_info=True,
                )
        await queue.put_async(("done", None))

    async def cancel(self) -> None:
        """Signal cooperative cancellation; checked between chunks in run()."""
        self._cancelled = True

    def __ray_terminate__(self) -> None:
        """Ensure local model server cleanup on actor termination.

        Ray calls this when the actor is killed or exits. Without it,
        the vLLM/Ollama subprocess may outlive the actor.
        """
        _cleanup_model_server(self._model_server)
        self._model_server = None

    def ping(self) -> bool:
        """Lightweight liveness probe for dead-actor detection."""
        return True


# ---------------------------------------------------------------------------
# Config serialization helpers (module-level for picklability).
# ---------------------------------------------------------------------------


def _config_to_dict(config: object) -> dict[str, Any]:
    """Serialize a SootheConfig (or dict) to a plain dict."""
    if isinstance(config, dict):
        return dict(config)
    if hasattr(config, "model_dump"):
        return config.model_dump()  # type: ignore[no-any-return]
    msg = f"Unsupported config type for local provider injection: {type(config)}"
    raise TypeError(msg)


def _dict_to_config(config_dict: dict[str, Any]) -> object:
    """Reconstruct a SootheConfig from a plain dict."""
    from soothe.config.settings import SootheConfig

    return SootheConfig(**config_dict)


def _cleanup_model_server(model_server: object | None) -> None:
    """Shut down a local model server if one is present.

    Extracted as a module-level function so it can be unit-tested without
    a live Ray cluster (``@ray.remote`` classes are mocks in test mode).
    """
    if model_server is not None:
        logger.info("LoopRunnerActor: cleaning up local model server on terminate")
        model_server.shutdown()  # type: ignore[attr-defined]
