"""Local model server lifecycle manager for GPU-bound Ray actors.

Launches a vLLM or Ollama subprocess inside the actor process, exposes an
OpenAI-compatible HTTP endpoint, and routes the agent's model provider to it.

The server lifecycle is bound to the Ray actor:
- ``launch()`` starts the subprocess and waits for health check.
- ``shutdown()`` terminates the subprocess on actor cleanup.
- ``atexit`` registration provides a safety net for unexpected exits.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soothe_daemon.config.models import LocalModelConfig

logger = logging.getLogger(__name__)

# Ephemeral port range for auto-allocation when port=0.
_EPHEMERAL_PORT_START = 8765
_EPHEMERAL_PORT_END = 8800

# Grace period (seconds) after SIGTERM before escalating to SIGKILL.
_SHUTDOWN_GRACE_SECONDS = 5

# Health check initial backoff (seconds).
_HEALTH_CHECK_INITIAL_BACKOFF = 0.5

# Health check max backoff (seconds).
_HEALTH_CHECK_MAX_BACKOFF = 5.0


class LocalModelStartupError(RuntimeError):
    """Raised when the local model server fails to start within the timeout."""


class LocalModelServer:
    """Manages a local model server (vLLM/Ollama) inside a Ray actor.

    Launched in ``LoopRunnerActor.__init__`` when ``RayConfig.local_model``
    is configured. Exposes an OpenAI-compatible endpoint that the agent's
    ``LLMFactory`` routes to as a standard provider.

    Lifecycle:
        ``launch()`` → wait for health check → serve during actor lifetime.
        ``shutdown()`` → terminate server process on actor cleanup.
    """

    def __init__(self, config: LocalModelConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._port: int = 0
        self._api_base: str = ""
        self._atexit_registered = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def launch(self) -> str:
        """Start the model server and wait for it to become healthy.

        Returns:
            The ``api_base`` URL (e.g. ``http://127.0.0.1:8765/v1``).

        Raises:
            LocalModelStartupError: If the server fails to start or become
                healthy within ``startup_timeout_seconds``.
        """
        self._port = self._allocate_port()
        self._api_base = f"http://127.0.0.1:{self._port}/v1"

        cmd = self._build_command()
        logger.info(
            "LocalModelServer: launching %s backend on port %d: %s",
            self._config.backend,
            self._port,
            " ".join(cmd),
        )

        try:
            self._process = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._build_env(),
            )
        except FileNotFoundError as exc:
            msg = (
                f"LocalModelServer: backend '{self._config.backend}' "
                f"executable not found: {exc}. "
                f"Ensure {'vllm' if self._config.backend == 'vllm' else 'ollama'} "
                f"is installed."
            )
            raise LocalModelStartupError(msg) from exc

        # Register cleanup safety net.
        atexit.register(self.shutdown)
        self._atexit_registered = True

        # Wait for health check.
        if not self._wait_for_health():
            msg = (
                f"LocalModelServer: {self._config.backend} server failed to "
                f"become healthy within {self._config.startup_timeout_seconds}s. "
                f"Check server logs."
            )
            self._force_kill()
            raise LocalModelStartupError(msg)

        logger.info(
            "LocalModelServer: %s backend healthy at %s",
            self._config.backend,
            self._api_base,
        )
        return self._api_base

    def shutdown(self) -> None:
        """Terminate the model server subprocess.

        Sends SIGTERM, waits up to 5s, then SIGKILL if still alive.
        Safe to call multiple times.
        """
        if self._process is None:
            return

        if self._atexit_registered:
            atexit.unregister(self.shutdown)
            self._atexit_registered = False

        self._force_kill()

    @property
    def api_base(self) -> str:
        """The OpenAI-compatible API base URL (``http://127.0.0.1:<port>/v1``)."""
        return self._api_base

    @property
    def provider_name(self) -> str:
        """Provider name for injection into SootheConfig.providers."""
        return self._config.provider_name

    @property
    def port(self) -> int:
        """The actual port the server is listening on."""
        return self._port

    # ------------------------------------------------------------------
    # Internal: command building
    # ------------------------------------------------------------------

    def _build_command(self) -> list[str]:
        """Build the CLI command for the configured backend."""
        if self._config.backend == "vllm":
            return self._build_vllm_command()
        return self._build_ollama_command()

    def _build_vllm_command(self) -> list[str]:
        """Build the vLLM server CLI command."""
        cmd = [
            sys.executable,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            self._config.model_name,
            "--port",
            str(self._port),
            "--host",
            "127.0.0.1",
            "--gpu-memory-utilization",
            str(self._config.gpu_memory_utilization),
            "--tensor-parallel-size",
            str(self._config.tensor_parallel_size),
        ]
        if self._config.max_model_len is not None:
            cmd.extend(["--max-model-len", str(self._config.max_model_len)])
        if self._config.quantization is not None:
            cmd.extend(["--quantization", self._config.quantization])
        cmd.extend(self._config.extra_args)
        return cmd

    def _build_ollama_command(self) -> list[str]:
        """Build the Ollama server CLI command."""
        cmd = ["ollama", "serve", "--port", str(self._port), "--host", "127.0.0.1"]
        cmd.extend(self._config.extra_args)
        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build the environment for the subprocess."""
        env = os.environ.copy()
        if self._config.backend == "vllm":
            # Ensure CUDA visible devices is inherited.
            if "CUDA_VISIBLE_DEVICES" not in env:
                env["CUDA_VISIBLE_DEVICES"] = "0"
        return env

    # ------------------------------------------------------------------
    # Internal: port allocation
    # ------------------------------------------------------------------

    def _allocate_port(self) -> int:
        """Allocate a port — use configured port or auto-allocate from ephemeral range."""
        if self._config.port > 0:
            if self._is_port_available(self._config.port):
                return self._config.port
            # Try next 10 ports if the specified one is occupied.
            for offset in range(1, 11):
                candidate = self._config.port + offset
                if candidate <= 65535 and self._is_port_available(candidate):
                    logger.warning(
                        "LocalModelServer: port %d occupied, using %d",
                        self._config.port,
                        candidate,
                    )
                    return candidate
            msg = (
                f"LocalModelServer: ports {self._config.port}-{self._config.port + 10} all occupied"
            )
            raise LocalModelStartupError(msg)

        # Auto-allocate from ephemeral range.
        for port in range(_EPHEMERAL_PORT_START, _EPHEMERAL_PORT_END + 1):
            if self._is_port_available(port):
                return port
        msg = (
            f"LocalModelServer: no available port in range "
            f"{_EPHEMERAL_PORT_START}-{_EPHEMERAL_PORT_END}"
        )
        raise LocalModelStartupError(msg)

    @staticmethod
    def _is_port_available(port: int) -> bool:
        """Check if a TCP port is available for binding."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Internal: health check
    # ------------------------------------------------------------------

    def _wait_for_health(self) -> bool:
        """Poll the health endpoint until it responds or timeout.

        Returns:
            ``True`` if the server is healthy, ``False`` on timeout.
        """
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        backoff = _HEALTH_CHECK_INITIAL_BACKOFF
        health_url = self._health_url()

        while time.monotonic() < deadline:
            # Check if process died.
            if self._process is not None and self._process.poll() is not None:
                logger.error(
                    "LocalModelServer: %s process exited (code=%d) before becoming healthy",
                    self._config.backend,
                    self._process.returncode,
                )
                return False

            try:
                req = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass

            time.sleep(backoff)
            backoff = min(backoff * 2, _HEALTH_CHECK_MAX_BACKOFF)

        return False

    def _health_url(self) -> str:
        """Get the health check URL for the configured backend."""
        if self._config.backend == "vllm":
            return f"{self._api_base}/health"
        # Ollama health endpoint.
        return f"http://127.0.0.1:{self._port}/api/tags"

    # ------------------------------------------------------------------
    # Internal: process management
    # ------------------------------------------------------------------

    def _force_kill(self) -> None:
        """Send SIGTERM, wait grace period, then SIGKILL."""
        if self._process is None:
            return

        proc = self._process
        self._process = None

        if proc.poll() is not None:
            # Already exited.
            return

        logger.info(
            "LocalModelServer: shutting down %s (pid=%d)",
            self._config.backend,
            proc.pid,
        )

        try:
            proc.send_signal(signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        try:
            proc.wait(timeout=_SHUTDOWN_GRACE_SECONDS)
            logger.info("LocalModelServer: %s exited cleanly", self._config.backend)
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "LocalModelServer: %s did not exit within %ds, sending SIGKILL",
                self._config.backend,
                _SHUTDOWN_GRACE_SECONDS,
            )

        try:
            proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            pass
