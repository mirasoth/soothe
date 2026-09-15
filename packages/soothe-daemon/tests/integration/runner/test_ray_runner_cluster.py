"""Integration tests for Ray loop runner using a multi-node Cluster.

Uses ray.cluster_utils.Cluster to start a head node plus worker nodes,
then validates Ray scheduling and RayLoopRunner queue draining.

Requires the optional `ray` package. Marked `integration` — run with
`pytest --run-integration`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("ray")

import ray  # noqa: E402

try:
    from ray.cluster_utils import Cluster
except ImportError:  # pragma: no cover - exercised only on old Ray builds
    Cluster = None  # type: ignore[misc, assignment]

from soothe.protocols.runner import LoopRunRequest

pytestmark = pytest.mark.integration


def _make_request(**kwargs: Any) -> LoopRunRequest:
    defaults: dict[str, Any] = dict(
        loop_id="ray-integ-loop-1",
        thread_id="ray-integ-thread-1",
        user_input="hello",
    )
    defaults.update(kwargs)
    return LoopRunRequest(**defaults)


@pytest.fixture
def ray_multi_node_cluster():
    """Start a local multi-node Ray cluster and tear it down reliably."""
    if Cluster is None:
        pytest.skip("ray.cluster_utils.Cluster is not available in this Ray version")

    if ray.is_initialized():
        ray.shutdown()

    cluster = Cluster()
    head_node = cluster.add_node(num_cpus=2)
    cluster.add_node(num_cpus=1)
    cluster.add_node(num_cpus=1)

    address = head_node.address_info["address"]
    ray.init(address=address)

    try:
        yield cluster
    finally:
        if ray.is_initialized():
            ray.shutdown()
        cluster.shutdown()


def test_ray_cluster_multi_node_resources(ray_multi_node_cluster) -> None:
    """Cluster exposes multiple nodes and enough CPUs for parallel tasks."""
    nodes = ray.nodes()
    assert len(nodes) >= 2, f"expected multi-node cluster, got {len(nodes)} nodes"

    resources = ray.cluster_resources()
    cpu_total = resources.get("CPU", 0)
    assert cpu_total >= 2.0, f"expected pooled CPUs across nodes, got {resources}"

    @ray.remote
    def _worker_task() -> str:
        import socket

        return f"ok-{socket.gethostname()}"

    futures = [_worker_task.remote() for _ in range(4)]
    results = ray.get(futures)
    assert len(results) == 4
    assert all(r.startswith("ok-") for r in results)


@pytest.mark.asyncio
async def test_ray_loop_runner_streams_chunks_with_stub_actor(ray_multi_node_cluster) -> None:
    """RayLoopRunner drains ray.util.queue.Queue from a remote actor."""

    @ray.remote
    class StubLoopRunnerActor:
        def __init__(self, _config: object) -> None:
            pass

        async def run(self, request: LoopRunRequest, queue: Any) -> None:
            await queue.put_async(("chunk", (("ns",), "messages", f"echo:{request.user_input}")))
            await queue.put_async(("done", None))

        async def cancel(self) -> None:
            pass

        def ping(self) -> bool:
            return True

    with patch("soothe_daemon.runner.ray_actor.LoopRunnerActor", StubLoopRunnerActor):
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        runner = RayLoopRunner("ray-integ-loop-runner", MagicMock(), MagicMock())
        collected: list[Any] = []
        async for chunk in runner.run(_make_request(user_input="cluster")):
            collected.append(chunk)

    assert collected == [(("ns",), "messages", "echo:cluster")]


@pytest.mark.asyncio
async def test_ray_loop_runner_cancel_releases_blocked_run(ray_multi_node_cluster) -> None:
    """cancel() signals the actor so ``run`` finishes and the driver drains ``done``."""

    @ray.remote
    class HeldStubLoopRunnerActor:
        def __init__(self, _config: object) -> None:
            self._released = asyncio.Event()

        async def run(self, _request: LoopRunRequest, queue: Any) -> None:
            await queue.put_async(("chunk", (("ns",), "messages", "held")))
            await self._released.wait()
            await queue.put_async(("done", None))

        async def cancel(self) -> None:
            self._released.set()

        def ping(self) -> bool:
            return True

    with patch("soothe_daemon.runner.ray_actor.LoopRunnerActor", HeldStubLoopRunnerActor):
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        runner = RayLoopRunner("ray-integ-cancel", MagicMock(), MagicMock())
        collected: list[Any] = []
        drain_task = asyncio.create_task(_collect(runner.run(_make_request()), collected))
        await asyncio.sleep(0.5)
        await asyncio.wait_for(runner.cancel(), timeout=15.0)
        await asyncio.wait_for(drain_task, timeout=15.0)

    assert collected == [(("ns",), "messages", "held")]


@pytest.mark.asyncio
async def test_ray_loop_runner_timeout_surfaces_error(ray_multi_node_cluster) -> None:
    """Request timeout_seconds causes run() to raise RuntimeError."""

    @ray.remote
    class SlowStubLoopRunnerActor:
        def __init__(self, _config: object) -> None:
            self._released = asyncio.Event()

        async def run(self, _request: LoopRunRequest, queue: Any) -> None:
            await queue.put_async(("chunk", (("ns",), "messages", "starting")))
            await self._released.wait()
            await queue.put_async(("done", None))

        async def cancel(self) -> None:
            self._released.set()

        def ping(self) -> bool:
            return True

    with patch("soothe_daemon.runner.ray_actor.LoopRunnerActor", SlowStubLoopRunnerActor):
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        runner = RayLoopRunner("ray-integ-timeout", MagicMock(), MagicMock())
        with pytest.raises((TimeoutError, RuntimeError)):
            async for _chunk in runner.run(_make_request(timeout_seconds=0.5)):
                pass


@pytest.mark.asyncio
async def test_ray_loop_runner_dead_actor_surfaces_error(ray_multi_node_cluster) -> None:
    """When the actor dies mid-stream, run() surfaces RayActorError."""

    @ray.remote
    class DyingStubLoopRunnerActor:
        def __init__(self, _config: object) -> None:
            pass

        async def run(self, _request: LoopRunRequest, queue: Any) -> None:
            await queue.put_async(("chunk", (("ns",), "messages", "before-crash")))
            # Simulate actor process crash — never sends "done".
            await asyncio.sleep(300)

        async def cancel(self) -> None:
            pass

        def ping(self) -> bool:
            return True

    with patch("soothe_daemon.runner.ray_actor.LoopRunnerActor", DyingStubLoopRunnerActor):
        from soothe_daemon.runner.ray_runner import RayLoopRunner

        runner = RayLoopRunner("ray-integ-dead-actor", MagicMock(), MagicMock())
        collected: list[Any] = []
        drain_task = asyncio.create_task(_collect(runner.run(_make_request()), collected))
        await asyncio.sleep(1.0)

        # Kill the actor to simulate a process crash.
        assert runner._actor is not None
        ray.kill(runner._actor)

        # The drain loop should detect the dead actor and raise.
        with pytest.raises((Exception,)):  # noqa: PT011
            await asyncio.wait_for(drain_task, timeout=30.0)

    assert collected == [(("ns",), "messages", "before-crash")]


@pytest.mark.asyncio
async def test_ray_loop_runner_await_loop_dispatchable(ray_multi_node_cluster) -> None:
    """await_loop_dispatchable serializes consecutive turns on the same loop."""

    from soothe_daemon.runner.ray_runner import (
        _mark_loop_busy,
        _mark_loop_idle,
        _reset_ray_state_for_testing,
        await_loop_dispatchable,
    )

    _reset_ray_state_for_testing()

    # When not busy, returns immediately.
    await asyncio.wait_for(await_loop_dispatchable("test-loop"), timeout=1.0)

    # When busy, blocks until idle.
    _mark_loop_busy("test-loop")
    wait_task = asyncio.create_task(await_loop_dispatchable("test-loop"))
    await asyncio.sleep(0.2)
    assert not wait_task.done(), "await_loop_dispatchable should block while busy"

    _mark_loop_idle("test-loop")
    await asyncio.wait_for(wait_task, timeout=2.0)

    _reset_ray_state_for_testing()


async def _collect(gen: Any, out: list[Any]) -> None:
    async for item in gen:
        out.append(item)
