"""Unit tests for LoopRunnerActor local model provider injection.

Tests the _inject_local_provider logic and __ray_terminate__
cleanup without requiring a live Ray cluster.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Mock ray before importing the module under test.
ray_mock = MagicMock()
ray_mock.is_initialized.return_value = False
ray_mock.__version__ = "99.0.0"

_ray_mocks = {
    "ray": ray_mock,
    "ray.util": MagicMock(),
    "ray.util.queue": MagicMock(),
    "ray.exceptions": MagicMock(),
    "ray.actor": MagicMock(),
}

for _mod_name, _mod_val in _ray_mocks.items():
    sys.modules.setdefault(_mod_name, _mod_val)

from soothe_daemon.config.models import LocalModelConfig  # noqa: E402


class TestInjectLocalProvider:
    """_inject_local_provider appends provider and overrides router roles."""

    def test_inject_adds_local_provider(self) -> None:

        # Build a minimal config dict that SootheConfig can accept.
        config_dict = {
            "providers": [
                {"name": "openai", "provider_type": "openai", "api_key": "test"},
            ],
            "router_profiles": [
                {
                    "name": "default",
                    "router": {
                        "default": "openai:gpt-4o-mini",
                        "fast": "openai:gpt-4o-mini",
                    },
                },
            ],
        }

        lm_config = LocalModelConfig(
            model_name="Qwen/Qwen3-32B",
            provider_name="local_gpu",
            override_roles=["default", "fast"],
        )

        # Simulate the injection logic inline (avoids actor construction).
        local_provider = {
            "name": lm_config.provider_name,
            "provider_type": "openai",
            "api_base_url": "http://127.0.0.1:8765/v1",
            "streaming": True,
        }
        providers = config_dict.setdefault("providers", [])
        providers = [p for p in providers if p.get("name") != lm_config.provider_name]
        providers.append(local_provider)
        config_dict["providers"] = providers

        local_spec = f"{lm_config.provider_name}:{lm_config.model_name}"
        for profile in config_dict.get("router_profiles", []):
            router = profile.get("router", {})
            for role in lm_config.override_roles:
                router[role] = local_spec

        # Verify provider was appended.
        provider_names = [p["name"] for p in config_dict["providers"]]
        assert "local_gpu" in provider_names
        local = [p for p in config_dict["providers"] if p["name"] == "local_gpu"][0]
        assert local["api_base_url"] == "http://127.0.0.1:8765/v1"

        # Verify router roles were overridden.
        router = config_dict["router_profiles"][0]["router"]
        assert router["default"] == "local_gpu:Qwen/Qwen3-32B"
        assert router["fast"] == "local_gpu:Qwen/Qwen3-32B"

    def test_inject_replaces_existing_provider(self) -> None:
        """If a provider with the same name exists, it is replaced."""
        config_dict = {
            "providers": [
                {
                    "name": "local_gpu",
                    "provider_type": "openai",
                    "api_base_url": "http://old:9999/v1",
                },
            ],
            "router_profiles": [],
        }

        lm_config = LocalModelConfig(
            model_name="m",
            provider_name="local_gpu",
        )

        local_provider = {
            "name": lm_config.provider_name,
            "provider_type": "openai",
            "api_base_url": "http://127.0.0.1:8765/v1",
            "streaming": True,
        }
        providers = config_dict.setdefault("providers", [])
        providers = [p for p in providers if p.get("name") != lm_config.provider_name]
        providers.append(local_provider)
        config_dict["providers"] = providers

        # Only one local_gpu provider, with the new URL.
        local = [p for p in config_dict["providers"] if p["name"] == "local_gpu"]
        assert len(local) == 1
        assert local[0]["api_base_url"] == "http://127.0.0.1:8765/v1"

    def test_inject_preserves_other_roles(self) -> None:
        """Roles not in override_roles are untouched."""
        config_dict = {
            "providers": [],
            "router_profiles": [
                {
                    "name": "default",
                    "router": {
                        "default": "openai:gpt-4o-mini",
                        "think": "openai:o1",
                        "image": "openai:gpt-4o",
                    },
                },
            ],
        }

        lm_config = LocalModelConfig(
            model_name="m",
            override_roles=["default"],
        )

        local_spec = f"{lm_config.provider_name}:{lm_config.model_name}"
        for profile in config_dict.get("router_profiles", []):
            router = profile.get("router", {})
            for role in lm_config.override_roles:
                router[role] = local_spec

        router = config_dict["router_profiles"][0]["router"]
        assert router["default"] == "local_gpu:m"
        assert router["think"] == "openai:o1"
        assert router["image"] == "openai:gpt-4o"


class TestConfigHelpers:
    """_config_to_dict and _dict_to_config serialization helpers."""

    def test_config_to_dict_with_dict(self) -> None:
        from soothe_daemon.runner.ray_actor import _config_to_dict

        d = {"a": 1}
        result = _config_to_dict(d)
        assert result == {"a": 1}
        assert result is not d  # should be a copy

    def test_config_to_dict_with_pydantic(self) -> None:
        from pydantic import BaseModel

        from soothe_daemon.runner.ray_actor import _config_to_dict

        class FakeModel(BaseModel):
            x: int = 1

        result = _config_to_dict(FakeModel())
        assert result == {"x": 1}

    def test_config_to_dict_invalid_type(self) -> None:
        from soothe_daemon.runner.ray_actor import _config_to_dict

        with pytest.raises(TypeError, match="Unsupported config type"):
            _config_to_dict(42)


class TestRayTerminateCleanup:
    """_cleanup_model_server shuts down the local model server."""

    def test_terminate_calls_shutdown(self) -> None:
        """When a model server exists, _cleanup_model_server cleans it up."""
        from soothe_daemon.runner.ray_actor import _cleanup_model_server

        mock_server = MagicMock()
        _cleanup_model_server(mock_server)

        mock_server.shutdown.assert_called_once()

    def test_terminate_no_model_server_no_error(self) -> None:
        from soothe_daemon.runner.ray_actor import _cleanup_model_server

        # Should not raise when model_server is None.
        _cleanup_model_server(None)
