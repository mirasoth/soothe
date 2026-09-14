"""Unit tests for LocalModelServer (mocked subprocess + health check)."""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from soothe_daemon.config.models import LocalModelConfig
from soothe_daemon.runner.local_model_server import (
    LocalModelServer,
    LocalModelStartupError,
)


class TestLocalModelServerVLLM:
    """vLLM backend launch/shutdown with mocked subprocess."""

    def test_launch_vllm_returns_api_base(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="Qwen/Qwen3-32B",
            port=8765,
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # alive

        with (
            patch(
                "soothe_daemon.runner.local_model_server.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
            patch.object(server, "_allocate_port", return_value=8765),
            patch.object(server, "_wait_for_health", return_value=True),
        ):
            api_base = server.launch()

        assert api_base == "http://127.0.0.1:8765/v1"
        assert server.api_base == "http://127.0.0.1:8765/v1"
        assert server.provider_name == "local_gpu"
        # Verify vLLM command was constructed.
        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        assert "vllm.entrypoints.openai.api_server" in cmd
        assert "--model" in cmd
        assert "Qwen/Qwen3-32B" in cmd
        assert "--port" in cmd
        assert "8765" in cmd

    def test_launch_auto_allocates_port(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="m",
            port=0,
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 99
        mock_proc.poll.return_value = None

        with (
            patch(
                "soothe_daemon.runner.local_model_server.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch.object(server, "_allocate_port", return_value=8770),
            patch.object(server, "_wait_for_health", return_value=True),
        ):
            api_base = server.launch()

        assert "8770" in api_base

    def test_launch_timeout_raises(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="m",
            port=8765,
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None

        with (
            patch(
                "soothe_daemon.runner.local_model_server.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch.object(server, "_allocate_port", return_value=8765),
            patch.object(server, "_wait_for_health", return_value=False),
        ):
            with pytest.raises(LocalModelStartupError, match="failed to become healthy"):
                server.launch()

    def test_shutdown_terminates_process(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="m",
            port=8765,
        )
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 77
        # poll() returns None first (alive), then 0 (exited after SIGTERM).
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0
        server._process = mock_proc
        server._port = 8765
        server._atexit_registered = False  # skip atexit unregister

        server.shutdown()

        # Should have called send_signal (SIGTERM) then potentially kill.
        mock_proc.send_signal.assert_called_once()
        mock_proc.wait.assert_called_once()

    def test_shutdown_already_dead_no_error(self) -> None:
        cfg = LocalModelConfig(backend="vllm", model_name="m", port=8765)
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 88
        mock_proc.poll.return_value = 0  # already exited
        server._process = mock_proc
        server._port = 8765
        server._atexit_registered = False

        # Should not raise.
        server.shutdown()
        # Should not have called send_signal since process is already dead.
        mock_proc.send_signal.assert_not_called()

    def test_vllm_command_includes_extra_args(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="m",
            port=8765,
            gpu_memory_utilization=0.8,
            tensor_parallel_size=1,
            max_model_len=4096,
            quantization="awq",
            extra_args=["--trust-remote-code"],
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 1
        mock_proc.poll.return_value = None

        with (
            patch(
                "soothe_daemon.runner.local_model_server.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
            patch.object(server, "_allocate_port", return_value=8765),
            patch.object(server, "_wait_for_health", return_value=True),
        ):
            server.launch()

        cmd = mock_popen.call_args[0][0]
        assert "--gpu-memory-utilization" in cmd
        assert "0.8" in cmd
        assert "--max-model-len" in cmd
        assert "4096" in cmd
        assert "--quantization" in cmd
        assert "awq" in cmd
        assert "--trust-remote-code" in cmd


class TestLocalModelServerOllama:
    """Ollama backend launch."""

    def test_launch_ollama(self) -> None:
        cfg = LocalModelConfig(
            backend="ollama",
            model_name="llama3:70b",
            port=11434,
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)

        mock_proc = MagicMock()
        mock_proc.pid = 55
        mock_proc.poll.return_value = None

        with (
            patch(
                "soothe_daemon.runner.local_model_server.subprocess.Popen",
                return_value=mock_proc,
            ) as mock_popen,
            patch.object(server, "_allocate_port", return_value=11434),
            patch.object(server, "_wait_for_health", return_value=True),
        ):
            api_base = server.launch()

        assert "11434" in api_base
        assert "/v1" in api_base
        cmd = mock_popen.call_args[0][0]
        assert "ollama" in cmd
        assert "serve" in cmd

    def test_ollama_api_base_path(self) -> None:
        cfg = LocalModelConfig(
            backend="ollama",
            model_name="m",
            port=11434,
        )
        server = LocalModelServer(cfg)
        # Simulate post-launch state.
        server._port = 11434
        server._api_base = "http://127.0.0.1:11434/v1"
        assert server.api_base == "http://127.0.0.1:11434/v1"


class TestLocalModelServerHealthCheck:
    """Health check polling logic."""

    def test_health_check_succeeds(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="m",
            port=8765,
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)
        server._port = 8765
        server._api_base = "http://127.0.0.1:8765/v1"

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # alive
        server._process = mock_proc

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with (
            patch(
                "soothe_daemon.runner.local_model_server.urllib.request.urlopen",
                return_value=mock_resp,
            ),
            patch(
                "soothe_daemon.runner.local_model_server.time.sleep",
            ),
        ):
            result = server._wait_for_health()

        assert result is True

    def test_health_check_timeout(self) -> None:
        cfg = LocalModelConfig(
            backend="vllm",
            model_name="m",
            port=8765,
            startup_timeout_seconds=10,
        )
        server = LocalModelServer(cfg)
        server._port = 8765
        server._api_base = "http://127.0.0.1:8765/v1"

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # alive
        server._process = mock_proc

        # Simulate time passing beyond the deadline.
        # First call returns start time (0.0), subsequent calls return > timeout.
        time_values = iter([0.0, 0.1, 0.2, 11.0])

        with (
            patch(
                "soothe_daemon.runner.local_model_server.urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ),
            patch(
                "soothe_daemon.runner.local_model_server.time.sleep",
            ),
            patch(
                "soothe_daemon.runner.local_model_server.time.monotonic",
                side_effect=lambda: next(time_values),
            ),
        ):
            result = server._wait_for_health()

        assert result is False

    def test_vllm_health_url_is_root_health(self) -> None:
        # vLLM exposes its liveness probe at /health (root), separate from the
        # OpenAI API at /v1. /v1/health returns 404 and would stall launch().
        cfg = LocalModelConfig(backend="vllm", model_name="m", port=8765)
        server = LocalModelServer(cfg)
        server._port = 8765
        server._api_base = "http://127.0.0.1:8765/v1"
        assert server._health_url() == "http://127.0.0.1:8765/health"

    def test_ollama_health_url(self) -> None:
        cfg = LocalModelConfig(backend="ollama", model_name="m", port=11434)
        server = LocalModelServer(cfg)
        server._port = 11434
        assert server._health_url() == "http://127.0.0.1:11434/api/tags"

    def test_health_check_polls_vllm_root_health(self) -> None:
        # Regression: _wait_for_health must GET /health, not /v1/health (404).
        cfg = LocalModelConfig(
            backend="vllm", model_name="m", port=8765, startup_timeout_seconds=10
        )
        server = LocalModelServer(cfg)
        server._port = 8765
        server._api_base = "http://127.0.0.1:8765/v1"

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # alive
        server._process = mock_proc

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with (
            patch(
                "soothe_daemon.runner.local_model_server.urllib.request.urlopen",
                return_value=mock_resp,
            ) as mock_urlopen,
            patch("soothe_daemon.runner.local_model_server.time.sleep"),
        ):
            result = server._wait_for_health()

        assert result is True
        polled = mock_urlopen.call_args[0][0]
        assert polled.full_url == "http://127.0.0.1:8765/health"
