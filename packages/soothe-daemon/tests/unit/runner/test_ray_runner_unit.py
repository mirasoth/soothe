"""Unit tests for Ray runner module-level helpers (no live Ray cluster).

Mocks `ray` at the module level so tests run on any CI without Ray.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

# Mock ray before importing the module under test.
# ray_runner.py does `import ray` at top level, so we must inject a mock
# into sys.modules before the first import.
ray_mock = MagicMock()
ray_mock.is_initialized.return_value = False
ray_mock.__version__ = "99.0.0"

ray_util_queue_mock = MagicMock()
ray_exceptions_mock = MagicMock()
ray_exceptions_mock.RayActorError = type("RayActorError", (Exception,), {})

_ray_mocks = {
    "ray": ray_mock,
    "ray.util": MagicMock(),
    "ray.util.queue": ray_util_queue_mock,
    "ray.exceptions": ray_exceptions_mock,
    "ray.actor": MagicMock(),
}

for _mod_name, _mod_val in _ray_mocks.items():
    sys.modules.setdefault(_mod_name, _mod_val)

from soothe_daemon.config import SootheDaemonConfig  # noqa: E402
from soothe_daemon.config.models import LoopRunnerConfig, RayConfig  # noqa: E402


class TestEnsureRayInit:
    """`_ensure_ray_init` reads RayConfig and calls ray.init() once."""

    def test_ensure_ray_init_calls_ray_init_once(self) -> None:
        from soothe_daemon.runner.ray_runner import (
            _ensure_ray_init,
            _reset_ray_state_for_testing,
        )

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(
                runner_mode="ray",
                ray=RayConfig(address="ray://example:10001", num_cpus=2, max_concurrent_actors=5),
            ),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            _ensure_ray_init(daemon_cfg)

            mock_r.init.assert_called_once()
            call_kwargs = mock_r.init.call_args
            assert call_kwargs.kwargs.get("address") == "ray://example:10001"

        _reset_ray_state_for_testing()

    def test_ensure_ray_init_is_idempotent(self) -> None:
        from soothe_daemon.runner.ray_runner import (
            _ensure_ray_init,
            _reset_ray_state_for_testing,
        )

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(runner_mode="ray"),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            mock_r.init.reset_mock()
            _ensure_ray_init(daemon_cfg)
            _ensure_ray_init(daemon_cfg)  # second call should be a no-op
            assert mock_r.init.call_count == 1

        _reset_ray_state_for_testing()

    def test_ensure_ray_init_skips_when_already_connected(self) -> None:
        from soothe_daemon.runner.ray_runner import (
            _ensure_ray_init,
            _reset_ray_state_for_testing,
        )

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(runner_mode="ray"),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = True
            mock_r.init.reset_mock()
            _ensure_ray_init(daemon_cfg)
            assert mock_r.init.call_count == 0

        _reset_ray_state_for_testing()

    def test_actor_options_populated_from_ray_config(self) -> None:
        import soothe_daemon.runner.ray_runner as rrm

        _reset_ray_state_for_testing = rrm._reset_ray_state_for_testing
        _ensure_ray_init = rrm._ensure_ray_init

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(
                runner_mode="ray",
                ray=RayConfig(
                    num_cpus=4, object_store_memory=1_000_000_000, max_concurrent_actors=3
                ),
            ),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            _ensure_ray_init(daemon_cfg)

            assert rrm._actor_options.get("num_cpus") == 4
            assert rrm._actor_options.get("object_store_memory") == 1_000_000_000
            assert rrm._actor_semaphore is not None

        _reset_ray_state_for_testing()

    def test_actor_options_include_num_gpus_when_set(self) -> None:
        """When RayConfig.num_gpus > 0, _actor_options includes num_gpus."""
        import soothe_daemon.runner.ray_runner as rrm

        _reset_ray_state_for_testing = rrm._reset_ray_state_for_testing
        _ensure_ray_init = rrm._ensure_ray_init

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(
                runner_mode="ray",
                ray=RayConfig(
                    num_cpus=2,
                    num_gpus=1.0,
                    max_concurrent_actors=2,
                ),
            ),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            _ensure_ray_init(daemon_cfg)

            assert rrm._actor_options.get("num_gpus") == 1.0
            assert rrm._actor_options.get("num_cpus") == 2

        _reset_ray_state_for_testing()

    def test_actor_options_no_num_gpus_when_zero(self) -> None:
        """When RayConfig.num_gpus == 0, num_gpus is not in actor_options."""
        import soothe_daemon.runner.ray_runner as rrm

        _reset_ray_state_for_testing = rrm._reset_ray_state_for_testing
        _ensure_ray_init = rrm._ensure_ray_init

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(
                runner_mode="ray",
                ray=RayConfig(num_cpus=2, num_gpus=0.0),
            ),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            _ensure_ray_init(daemon_cfg)

            assert "num_gpus" not in rrm._actor_options

        _reset_ray_state_for_testing()

    def test_actor_options_include_runtime_env_when_set(self) -> None:
        """RayConfig.runtime_env is folded into _actor_options."""
        import soothe_daemon.runner.ray_runner as rrm

        _reset_ray_state_for_testing = rrm._reset_ray_state_for_testing
        _ensure_ray_init = rrm._ensure_ray_init

        _reset_ray_state_for_testing()

        runtime_env = {
            "env_vars": {
                "LD_LIBRARY_PATH": "/opt/cuda/lib64:/opt/nvidia/cu13/lib",
                "VLLM_NO_USAGE_STATS": "1",
            }
        }
        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(
                runner_mode="ray",
                ray=RayConfig(num_gpus=1.0, runtime_env=runtime_env),
            ),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            _ensure_ray_init(daemon_cfg)

            assert rrm._actor_options.get("runtime_env") == runtime_env
            assert rrm._actor_options.get("num_gpus") == 1.0

        _reset_ray_state_for_testing()

    def test_actor_options_no_runtime_env_when_none(self) -> None:
        """When RayConfig.runtime_env is None, runtime_env is not in actor_options."""
        import soothe_daemon.runner.ray_runner as rrm

        _reset_ray_state_for_testing = rrm._reset_ray_state_for_testing
        _ensure_ray_init = rrm._ensure_ray_init

        _reset_ray_state_for_testing()

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(
                runner_mode="ray",
                ray=RayConfig(num_gpus=1.0),
            ),
        )

        with patch("soothe_daemon.runner.ray_runner.ray", ray_mock) as mock_r:
            mock_r.is_initialized.return_value = False
            _ensure_ray_init(daemon_cfg)

            assert "runtime_env" not in rrm._actor_options

        _reset_ray_state_for_testing()


class TestAwaitLoopDispatchable:
    """`await_loop_dispatchable` serializes consecutive turns on the same loop."""

    def test_returns_immediately_when_not_busy(self) -> None:
        from soothe_daemon.runner.ray_runner import (
            _reset_ray_state_for_testing,
            await_loop_dispatchable,
        )

        _reset_ray_state_for_testing()

        asyncio.run(asyncio.wait_for(await_loop_dispatchable("test-loop"), timeout=1.0))

        _reset_ray_state_for_testing()

    def test_blocks_when_busy_then_unblocks(self) -> None:
        from soothe_daemon.runner.ray_runner import (
            _mark_loop_busy,
            _mark_loop_idle,
            _reset_ray_state_for_testing,
            await_loop_dispatchable,
        )

        _reset_ray_state_for_testing()

        _mark_loop_busy("test-loop")

        async def _test() -> None:
            wait_task = asyncio.create_task(await_loop_dispatchable("test-loop"))
            await asyncio.sleep(0.1)
            assert not wait_task.done(), "should block while busy"

            _mark_loop_idle("test-loop")
            await asyncio.wait_for(wait_task, timeout=2.0)

        asyncio.run(_test())
        _reset_ray_state_for_testing()


class TestRayLoopRunnerConstructor:
    """`RayLoopRunner` accepts `daemon_config` (was 2 args, now 3)."""

    def test_constructor_accepts_daemon_config(self) -> None:
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        daemon_cfg = SootheDaemonConfig(
            loop_runner=LoopRunnerConfig(runner_mode="ray"),
        )
        agent_cfg = MagicMock()

        runner = RayLoopRunner("loop-1", agent_cfg, daemon_cfg)
        assert runner._loop_id == "loop-1"
        assert runner._daemon_config is daemon_cfg

    def test_constructor_daemon_config_defaults_to_none(self) -> None:
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        agent_cfg = MagicMock()
        runner = RayLoopRunner("loop-2", agent_cfg)
        assert runner._daemon_config is None


class TestRayLoopRunnerLiveness:
    """`_is_actor_alive` distinguishes a dead actor from a busy one.

    A local-model actor blocks in ``__init__`` while loading vLLM (a minute or
    more) and cannot answer a ping during that window. A ping *timeout* must
    NOT be reported as death — only ``RayActorError`` (process gone) is.

    The module global ``RayActorError`` is patched to a local class so the
    tests don't depend on the shared ``ray.exceptions`` mock resolving to a
    real class (sibling tests can leave a bare MagicMock in sys.modules).
    """

    _DEAD = type("_DeadActorError", (Exception,), {})

    def _make_runner(self):
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        runner = RayLoopRunner("loop-x", MagicMock(), MagicMock())
        runner._actor = MagicMock()
        return runner

    def test_no_actor_returns_false(self) -> None:
        runner = self._make_runner()
        runner._actor = None
        assert runner._is_actor_alive() is False

    def test_ping_ok_returns_true(self) -> None:
        runner = self._make_runner()
        with patch("soothe_daemon.runner.ray_runner.ray") as mock_ray:
            mock_ray.get.return_value = True
            assert runner._is_actor_alive() is True
            mock_ray.get.assert_called_once()

    def test_ray_actor_error_means_dead(self) -> None:
        runner = self._make_runner()
        with (
            patch("soothe_daemon.runner.ray_runner.RayActorError", self._DEAD),
            patch("soothe_daemon.runner.ray_runner.ray") as mock_ray,
        ):
            mock_ray.get.side_effect = self._DEAD("actor gone")
            assert runner._is_actor_alive() is False

    def test_timeout_means_busy_not_dead(self) -> None:
        runner = self._make_runner()
        with (
            patch("soothe_daemon.runner.ray_runner.RayActorError", self._DEAD),
            patch("soothe_daemon.runner.ray_runner.ray") as mock_ray,
        ):
            mock_ray.get.side_effect = TimeoutError()
            assert runner._is_actor_alive() is True

    def test_other_exception_means_busy_not_dead(self) -> None:
        runner = self._make_runner()
        with (
            patch("soothe_daemon.runner.ray_runner.RayActorError", self._DEAD),
            patch("soothe_daemon.runner.ray_runner.ray") as mock_ray,
        ):
            mock_ray.get.side_effect = RuntimeError("transient")
            assert runner._is_actor_alive() is True
