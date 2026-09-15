"""Unit tests for LocalModelConfig validation and RayConfig integration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from soothe_daemon.config.models import LocalModelConfig, RayConfig


class TestLocalModelConfig:
    """LocalModelConfig field validation."""

    def test_defaults(self) -> None:
        cfg = LocalModelConfig(model_name="Qwen/Qwen3-32B")
        assert cfg.backend == "vllm"
        assert cfg.provider_name == "local_gpu"
        assert cfg.port == 0
        assert cfg.gpu_memory_utilization == 0.9
        assert cfg.tensor_parallel_size == 1
        assert cfg.max_model_len is None
        assert cfg.quantization is None
        assert cfg.startup_timeout_seconds == 120
        assert cfg.extra_args == []
        assert cfg.override_roles == ["default"]

    def test_ollama_backend(self) -> None:
        cfg = LocalModelConfig(
            backend="ollama",
            model_name="llama3:70b",
            provider_name="local_ollama",
        )
        assert cfg.backend == "ollama"
        assert cfg.provider_name == "local_ollama"

    def test_gpu_memory_utilization_bounds(self) -> None:
        with pytest.raises(ValidationError):
            LocalModelConfig(model_name="m", gpu_memory_utilization=0.05)
        with pytest.raises(ValidationError):
            LocalModelConfig(model_name="m", gpu_memory_utilization=1.5)

    def test_tensor_parallel_size_bounds(self) -> None:
        with pytest.raises(ValidationError):
            LocalModelConfig(model_name="m", tensor_parallel_size=0)

    def test_startup_timeout_bounds(self) -> None:
        with pytest.raises(ValidationError):
            LocalModelConfig(model_name="m", startup_timeout_seconds=5)
        with pytest.raises(ValidationError):
            LocalModelConfig(model_name="m", startup_timeout_seconds=700)

    def test_override_roles_multi(self) -> None:
        cfg = LocalModelConfig(
            model_name="m",
            override_roles=["default", "fast", "think"],
        )
        assert cfg.override_roles == ["default", "fast", "think"]

    def test_extra_args(self) -> None:
        cfg = LocalModelConfig(
            model_name="m",
            extra_args=["--trust-remote-code", "--dtype=half"],
        )
        assert cfg.extra_args == ["--trust-remote-code", "--dtype=half"]


class TestRayConfigLocalModel:
    """RayConfig num_gpus + local_model integration."""

    def test_num_gpus_default_zero(self) -> None:
        cfg = RayConfig()
        assert cfg.num_gpus == 0.0
        assert cfg.local_model is None

    def test_num_gpus_set(self) -> None:
        cfg = RayConfig(num_gpus=1.0)
        assert cfg.num_gpus == 1.0

    def test_local_model_set_requires_gpus(self) -> None:
        """local_model set with num_gpus=0 should raise."""
        with pytest.raises(ValidationError):
            RayConfig(
                num_gpus=0.0,
                local_model=LocalModelConfig(model_name="m"),
            )

    def test_local_model_with_gpus_ok(self) -> None:
        cfg = RayConfig(
            num_gpus=1.0,
            local_model=LocalModelConfig(model_name="Qwen/Qwen3-32B"),
        )
        assert cfg.local_model is not None
        assert cfg.local_model.model_name == "Qwen/Qwen3-32B"

    def test_tensor_parallel_must_match_gpus(self) -> None:
        """tensor_parallel_size=2 requires num_gpus>=2."""
        with pytest.raises(ValidationError):
            RayConfig(
                num_gpus=1.0,
                local_model=LocalModelConfig(
                    model_name="m",
                    tensor_parallel_size=2,
                ),
            )

    def test_tensor_parallel_matches_gpus(self) -> None:
        cfg = RayConfig(
            num_gpus=2.0,
            local_model=LocalModelConfig(
                model_name="m",
                tensor_parallel_size=2,
            ),
        )
        assert cfg.local_model.tensor_parallel_size == 2
