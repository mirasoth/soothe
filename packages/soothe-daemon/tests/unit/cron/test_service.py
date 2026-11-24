"""Unit tests for CronService orchestrator (RFC-229)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from soothe_daemon.cron.extraction import AutopilotDisabledError
from soothe_daemon.cron.models import (
    DEFAULT_CRON_USER_ID,
    CronJob,
    DuplicateCronJobError,
    ExtractionResult,
    JobStatus,
    ScheduleKind,
)
from soothe_daemon.cron.service import CronService
from soothe_daemon.cron.store import CronJobStore


def _mock_config(*, max_jobs: int = 100, poll_interval: int = 60) -> MagicMock:
    cfg = MagicMock()
    cfg.agent.rail.default_rail = "feature-dev"
    cfg.cron.max_jobs = max_jobs
    cfg.cron.poll_interval = poll_interval
    cfg.cron.extraction_model = "fast"
    cfg.cron.extraction_timeout = 30
    cfg.cron.default_priority = 50
    cfg.cron.timezone = "UTC"
    cfg.cron.enable_builtin_jobs = True
    return cfg


def _extraction(
    description: str = "check deploy",
    *,
    kind: ScheduleKind = ScheduleKind.DELAY,
    value: str = "1h",
) -> ExtractionResult:
    return ExtractionResult(
        description=description,
        schedule_kind=kind,
        schedule_value=value,
        confidence=0.9,
        raw_input="test",
    )


@pytest.fixture
def temp_store(tmp_path) -> CronJobStore:
    return CronJobStore(db_path=str(tmp_path / "cron.db"), reader_pool_size=1)


@pytest.mark.asyncio
async def test_add_job_rejects_when_autopilot_disabled(temp_store: CronJobStore) -> None:
    """Pending jobs must not be created when the loop-native path is disabled."""
    svc = CronService(config=_mock_config(), store=temp_store)
    svc._config.agent.rail.default_rail = None
    svc._extraction_service.extract = AsyncMock(return_value=_extraction())

    with pytest.raises(AutopilotDisabledError, match="Cron dispatch is unavailable"):
        await svc.add_job("in 1 hour check deploy", DEFAULT_CRON_USER_ID)

    assert await svc.list_jobs(DEFAULT_CRON_USER_ID) == []
    svc._extraction_service.extract.assert_not_awaited()
    await svc.stop()


@pytest.mark.asyncio
async def test_add_job_persists(temp_store: CronJobStore) -> None:
    svc = CronService(config=_mock_config(), store=temp_store)
    svc._extraction_service.extract = AsyncMock(return_value=_extraction())

    job = await svc.add_job("in 1 hour check deploy", DEFAULT_CRON_USER_ID)

    assert job.user_id == DEFAULT_CRON_USER_ID
    assert job.status == JobStatus.PENDING
    assert job.description == "check deploy"
    listed = await svc.list_jobs(DEFAULT_CRON_USER_ID)
    assert len(listed) == 1
    assert listed[0].id == job.id
    await svc.stop()


@pytest.mark.asyncio
async def test_add_job_respects_max_jobs(temp_store: CronJobStore) -> None:
    svc = CronService(config=_mock_config(max_jobs=1), store=temp_store)
    svc._extraction_service.extract = AsyncMock(return_value=_extraction())

    await svc.add_job("in 1 hour first", DEFAULT_CRON_USER_ID)
    with pytest.raises(ValueError, match="Maximum scheduled jobs"):
        await svc.add_job("in 1 hour second", DEFAULT_CRON_USER_ID)
    await svc.stop()


@pytest.mark.asyncio
async def test_add_job_allows_reworded_variant_description(temp_store: CronJobStore) -> None:
    """Duplicate detection is an exact normalized match: a reworded variant passes.

    Per `.agents/rules/code-style.md` rule 9, substring containment and
    word-overlap heuristics are not used — "check email" must not block
    "check email and calendar".
    """
    svc = CronService(config=_mock_config(), store=temp_store)
    extraction_full = _extraction(
        "polish packages folder to cleanse dead code package by package and module by module",
        kind=ScheduleKind.CRON,
        value="0 3 * * *",
    )
    extraction_short = _extraction(
        "polish packages folder to cleanse dead code package by package",
        kind=ScheduleKind.CRON,
        value="0 3 * * *",
    )
    svc._extraction_service.extract = AsyncMock(return_value=extraction_full)

    first = await svc.add_job("every day at 3am polish packages", DEFAULT_CRON_USER_ID)
    svc._extraction_service.extract = AsyncMock(return_value=extraction_short)

    second = await svc.add_job("daily at 3am polish packages short wording", DEFAULT_CRON_USER_ID)

    assert second.id != first.id
    assert len(await svc.list_jobs(DEFAULT_CRON_USER_ID)) == 2
    await svc.stop()


@pytest.mark.asyncio
async def test_add_job_rejects_exact_duplicate_after_normalization(
    temp_store: CronJobStore,
) -> None:
    svc = CronService(config=_mock_config(), store=temp_store)
    extraction_a = _extraction(
        "Polish packages folder to cleanse dead code",
        kind=ScheduleKind.CRON,
        value="0 3 * * *",
    )
    extraction_b = _extraction(
        "polish  packages folder   to cleanse dead code",
        kind=ScheduleKind.CRON,
        value="0 3 * * *",
    )
    svc._extraction_service.extract = AsyncMock(return_value=extraction_a)

    first = await svc.add_job("every day at 3am polish packages", DEFAULT_CRON_USER_ID)
    svc._extraction_service.extract = AsyncMock(return_value=extraction_b)

    with pytest.raises(DuplicateCronJobError) as exc_info:
        await svc.add_job("daily at 3am polish packages same wording", DEFAULT_CRON_USER_ID)

    assert exc_info.value.existing_job.id == first.id
    await svc.stop()


@pytest.mark.asyncio
async def test_add_job_rejects_duplicate_active_job(temp_store: CronJobStore) -> None:
    svc = CronService(config=_mock_config(), store=temp_store)
    svc._extraction_service.extract = AsyncMock(
        return_value=_extraction(
            "polish packages folder to cleanse dead code",
            kind=ScheduleKind.CRON,
            value="0 3 * * *",
        )
    )

    first = await svc.add_job(
        "every day at 3am polish packages folder to cleanse dead code",
        DEFAULT_CRON_USER_ID,
    )
    assert first.id

    with pytest.raises(DuplicateCronJobError) as exc_info:
        await svc.add_job(
            "At 3am every day polish packages folder to cleanse dead code",
            DEFAULT_CRON_USER_ID,
        )

    assert exc_info.value.existing_job.id == first.id
    assert len(await svc.list_jobs(DEFAULT_CRON_USER_ID)) == 1
    await svc.stop()


@pytest.mark.asyncio
async def test_add_job_allows_resubmit_after_cancel(temp_store: CronJobStore) -> None:
    svc = CronService(config=_mock_config(), store=temp_store)
    svc._extraction_service.extract = AsyncMock(return_value=_extraction())

    job = await svc.add_job("in 1 hour check deploy", DEFAULT_CRON_USER_ID)
    await svc.cancel_job(job.id, DEFAULT_CRON_USER_ID)

    second = await svc.add_job("in 1 hour check deploy", DEFAULT_CRON_USER_ID)
    assert second.id != job.id
    assert len(await svc.list_jobs(DEFAULT_CRON_USER_ID, status=JobStatus.PENDING)) == 1
    await svc.stop()


@pytest.mark.asyncio
async def test_cancel_job_pending_only(temp_store: CronJobStore) -> None:
    svc = CronService(config=_mock_config(), store=temp_store)
    svc._extraction_service.extract = AsyncMock(return_value=_extraction())
    job = await svc.add_job("in 1 hour task", DEFAULT_CRON_USER_ID)

    assert await svc.cancel_job(job.id, DEFAULT_CRON_USER_ID) is True
    cancelled = await svc.show_job(job.id, DEFAULT_CRON_USER_ID)
    assert cancelled is not None
    assert cancelled.status == JobStatus.CANCELLED

    assert await svc.cancel_job(job.id, DEFAULT_CRON_USER_ID) is False
    assert await svc.cancel_job(job.id, "other-user") is False
    await svc.stop()


@pytest.mark.asyncio
async def test_tick_dispatches_due_job_to_loop(temp_store: CronJobStore) -> None:
    """Due jobs are dispatched via the loop-native path (loop_input enqueue)."""
    loop_input_dispatcher = MagicMock()
    loop_input_dispatcher.enqueue = AsyncMock()
    persistence_manager = MagicMock()
    persistence_manager.register_loop = AsyncMock()
    persistence_manager.update_loop_metadata = AsyncMock()
    persistence_manager.increment_loop_message_count = AsyncMock()

    svc = CronService(
        config=_mock_config(),
        store=temp_store,
        loop_input_dispatcher=loop_input_dispatcher,
        persistence_manager=persistence_manager,
    )
    due_job = CronJob(
        id="due001",
        user_id=DEFAULT_CRON_USER_ID,
        description="run nightly backup",
        schedule_kind=ScheduleKind.DELAY,
        schedule_value="1m",
        status=JobStatus.PENDING,
        next_run=datetime.now(tz=UTC) - timedelta(minutes=1),
    )
    await temp_store.create(due_job)

    await svc._tick()

    # The loop_input dispatcher is enqueued with a payload carrying
    # autopilot_rail_id (the loop-native rail binding).
    loop_input_dispatcher.enqueue.assert_awaited_once()
    call_args = loop_input_dispatcher.enqueue.call_args
    payload = call_args.kwargs.get("message") or call_args.args[1]
    assert payload["text"] == "run nightly backup"
    assert payload["autopilot_rail_id"] == "feature-dev"
    assert payload["cron_job_id"] == "due001"
    updated = await temp_store.get("due001")
    assert updated is not None
    assert updated.status == JobStatus.COMPLETED
    await svc.stop()


@pytest.mark.asyncio
async def test_show_job_ownership(temp_store: CronJobStore) -> None:
    svc = CronService(config=_mock_config(), store=temp_store)
    job = CronJob(
        id="own001",
        user_id=DEFAULT_CRON_USER_ID,
        description="private task",
        schedule_kind=ScheduleKind.DELAY,
        schedule_value="2h",
    )
    await temp_store.create(job)

    assert (await svc.show_job("own001", DEFAULT_CRON_USER_ID)) is not None
    assert await svc.show_job("own001", "other-user") is None
    await svc.stop()


@pytest.mark.asyncio
async def test_seed_builtin_jobs_disabled(temp_store: CronJobStore) -> None:
    """When enable_builtin_jobs is false, seeding is a no-op."""
    cfg = _mock_config()
    cfg.cron.enable_builtin_jobs = False
    svc = CronService(config=cfg, store=temp_store)

    created = await svc.seed_builtin_jobs()
    assert created == 0
    await svc.stop()


async def _backdate_running_row(store: CronJobStore, job_id: str, updated_at: datetime) -> None:
    """Set a running row's updated_at into the past (simulates a previous run)."""
    await store._runtime.run_write(
        lambda conn: conn.execute(
            "UPDATE cron_jobs SET updated_at = ? WHERE id = ?",
            (updated_at.isoformat(), job_id),
        )
    )


@pytest.mark.asyncio
async def test_start_sweeps_stale_running_recurring_job(temp_store: CronJobStore) -> None:
    """A recurring job stranded in running by a crash is re-pended on start."""
    past = datetime.now(tz=UTC) - timedelta(hours=2)
    job = CronJob(
        id="stale01",
        user_id=DEFAULT_CRON_USER_ID,
        description="nightly backup",
        schedule_kind=ScheduleKind.CRON,
        schedule_value="0 3 * * *",
        status=JobStatus.PENDING,
        next_run=datetime.now(tz=UTC) - timedelta(minutes=5),
    )
    await temp_store.create(job)
    await temp_store.update_status(job.id, JobStatus.RUNNING)
    await _backdate_running_row(temp_store, job.id, past)

    cfg = _mock_config()
    cfg.cron.enable_builtin_jobs = False
    svc = CronService(config=cfg, store=temp_store)
    await svc.start()

    updated = await temp_store.get("stale01")
    assert updated is not None
    assert updated.status == JobStatus.PENDING
    assert updated.next_run > datetime.now(tz=UTC)

    # No double-dispatch: the tick loop runs after the sweep and dispatches
    # exactly once for the recovered job.
    assert (await temp_store.get_due_jobs(datetime.now(tz=UTC) + timedelta(minutes=1))) == []
    await svc.stop()


@pytest.mark.asyncio
async def test_start_marks_stale_running_one_shot_failed(temp_store: CronJobStore) -> None:
    """A one-shot 'at' job stranded in running past its window is marked failed."""
    past = datetime.now(tz=UTC) - timedelta(hours=2)
    job = CronJob(
        id="stale02",
        user_id=DEFAULT_CRON_USER_ID,
        description="one-shot reminder",
        schedule_kind=ScheduleKind.AT,
        schedule_value=(datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
        status=JobStatus.PENDING,
        next_run=datetime.now(tz=UTC) - timedelta(hours=1),
    )
    await temp_store.create(job)
    await temp_store.update_status(job.id, JobStatus.RUNNING)
    await _backdate_running_row(temp_store, job.id, past)

    cfg = _mock_config()
    cfg.cron.enable_builtin_jobs = False
    svc = CronService(config=cfg, store=temp_store)
    await svc.start()

    updated = await temp_store.get("stale02")
    assert updated is not None
    assert updated.status == JobStatus.FAILED
    await svc.stop()


@pytest.mark.asyncio
async def test_sweep_skips_job_running_after_start(temp_store: CronJobStore) -> None:
    """A running row written at/after the daemon start instant is left alone."""
    job = CronJob(
        id="fresh01",
        user_id=DEFAULT_CRON_USER_ID,
        description="in-flight dispatch",
        schedule_kind=ScheduleKind.CRON,
        schedule_value="0 3 * * *",
        status=JobStatus.RUNNING,
        next_run=datetime.now(tz=UTC),
    )
    await temp_store.create(job)
    await temp_store.update_status(job.id, JobStatus.RUNNING)

    cfg = _mock_config()
    cfg.cron.enable_builtin_jobs = False
    svc = CronService(config=cfg, store=temp_store)

    started_at = datetime.now(tz=UTC) - timedelta(hours=1)
    swept = await svc._sweep_stale_running_jobs(started_at)

    assert swept == 0
    updated = await temp_store.get("fresh01")
    assert updated is not None
    assert updated.status == JobStatus.RUNNING
    await svc.stop()


@pytest.mark.asyncio
async def test_start_reconcile_skips_delay_jobs_on_repeat_start(
    temp_store: CronJobStore,
) -> None:
    """Restarting with an unchanged timezone must not re-anchor delay next_run."""
    svc = CronService(config=_mock_config(), store=temp_store)
    svc._extraction_service.extract = AsyncMock(return_value=_extraction())

    job = await svc.add_job("in 2 hours check deploy", DEFAULT_CRON_USER_ID)
    first_next_run = job.next_run

    await svc.start()
    restarted = await temp_store.get(job.id)
    assert restarted is not None
    assert restarted.status == JobStatus.PENDING
    assert restarted.next_run == first_next_run

    await svc.start()
    again = await temp_store.get(job.id)
    assert again is not None
    assert again.next_run == first_next_run
    await svc.stop()
