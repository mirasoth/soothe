"""CronService — orchestrator for cron jobs.

Coordinates NL extraction, persistence, and execution through the loop-native
submission path (loop_input with ``autopilot_rail_id``). Cron dispatch creates
a fresh loop and enqueues a ``loop_input`` carrying the configured default
rail id so ``StrangeLoop.run_with_progress`` binds a ``LoopRailInterpreter``
— bypassing the removed ``AutopilotService.submit_task`` path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from soothe_daemon.cron.builtin import BUILTIN_JOBS
from soothe_daemon.cron.extraction import AutopilotDisabledError, CronExtractionService
from soothe_daemon.cron.messages import AUTOPILOT_REQUIRED_FOR_CRON
from soothe_daemon.cron.models import (
    DEFAULT_CRON_USER_ID,
    CronJob,
    DuplicateCronJobError,
    JobStatus,
)
from soothe_daemon.cron.schedule import ScheduleSpec, schedule_timezone_label
from soothe_daemon.cron.store_factory import create_cron_job_store

if TYPE_CHECKING:
    from soothe.config.settings import SootheConfig

logger = logging.getLogger(__name__)


class CronService:
    """Orchestrating service for cron jobs (extraction, scheduling, persistence, dispatch)."""

    def __init__(
        self,
        config: SootheConfig,
        *,
        loop_input_dispatcher: Any | None = None,
        persistence_manager: Any | None = None,
        store: Any | None = None,
    ) -> None:
        """Initialize CronService.

        Args:
        config: Host configuration (provides cron + autopilot.default_rail).
        loop_input_dispatcher: Daemon LoopInputDispatcher used to enqueue
            loop_input messages carrying ``autopilot_rail_id`` for the
            loop-native submission path.
        persistence_manager: Daemon persistence manager used to register
            loops created for cron dispatch.
        store: Optional cron job store override.
        """
        self._config = config
        self._loop_input_dispatcher = loop_input_dispatcher
        self._persistence_manager = persistence_manager
        self._cron_config = config.cron

        # Create components
        self._store = store or create_cron_job_store(config)
        self._extraction_service = CronExtractionService(
            config,
            model_role=self._cron_config.extraction_model,
            timeout=self._cron_config.extraction_timeout,
        )

        # Running state
        self._running = False
        self._tick_task: asyncio.Task | None = None

        logger.info(
            "CronService initialized: max_jobs=%d poll_interval=%d timezone=%s",
            self._cron_config.max_jobs,
            self._cron_config.poll_interval,
            schedule_timezone_label(self._cron_config.timezone),
        )

    def _schedule_spec(self, kind: str, value: str) -> ScheduleSpec:
        """Build a schedule spec using the configured cron timezone."""
        return ScheduleSpec(
            kind=kind,
            value=value,
            timezone=self._cron_config.timezone,
        )

    async def _reconcile_pending_schedules(self) -> None:
        """Recompute next_run for pending jobs after timezone config changes."""
        now = datetime.now(tz=UTC)
        pending = await self._store.list_pending()
        for job in pending:
            spec = self._schedule_spec(job.schedule_kind.value, job.schedule_value)
            next_run = spec.next_after(now)
            if next_run is None:
                continue
            if next_run != job.next_run:
                await self._store.update_next_run(job.id, next_run, job.run_count)
                logger.info(
                    "Cron job next_run reconciled: id=%s next_run=%s timezone=%s",
                    job.id,
                    next_run.isoformat(),
                    schedule_timezone_label(self._cron_config.timezone),
                )

    async def seed_builtin_jobs(self) -> int:
        """Seed built-in recurring maintenance jobs into the store.

        Iterates the built-in job registry (see `cron.builtin`) and creates
        each job if it does not already exist. Seeding is idempotent: a job with
        the same stable `job_id` is never duplicated.

        Returns:
        Number of newly created built-in jobs (0 if all already existed).
        """
        if not self._cron_config.enable_builtin_jobs:
            logger.debug("Built-in cron jobs disabled; skipping seed pass")
            return 0

        created = 0
        for spec in BUILTIN_JOBS:
            existing = await self._store.get(spec.job_id)
            if existing is not None:
                logger.debug("Built-in cron job already exists: id=%s", spec.job_id)
                continue

            schedule = self._schedule_spec(spec.schedule_kind.value, spec.schedule_value)
            next_run = schedule.next_after(datetime.now(tz=UTC))
            if next_run is None:
                logger.warning(
                    "Built-in cron job has no valid next run: id=%s schedule=%s",
                    spec.job_id,
                    spec.schedule_value,
                )
                continue

            job = CronJob(
                id=spec.job_id,
                user_id=DEFAULT_CRON_USER_ID,
                description=spec.description,
                schedule_kind=spec.schedule_kind,
                schedule_value=spec.schedule_value,
                priority=spec.priority,
                status=JobStatus.PENDING,
                next_run=next_run,
            )
            await self._store.create(job)
            created += 1
            logger.info(
                "Built-in cron job seeded: id=%s next_run=%s",
                spec.job_id,
                next_run.isoformat(),
            )
        return created

    async def start(self) -> None:
        """Start the cron service monitoring loop."""
        if self._running:
            logger.warning("CronService already running")
            return

        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop())

        await self._reconcile_pending_schedules()
        await self.seed_builtin_jobs()

        logger.info(
            "CronService started with poll_interval=%ds timezone=%s",
            self._cron_config.poll_interval,
            schedule_timezone_label(self._cron_config.timezone),
        )

    async def stop(self) -> None:
        """Stop the cron service monitoring loop."""
        if not self._running:
            return

        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None

        await self._store.close()
        logger.info("CronService stopped")

    async def add_job(
        self,
        natural_language: str,
        user_id: str,
        priority: int | None = None,
    ) -> CronJob:
        """Submit job via natural language.

        Args:
        natural_language: User's natural language request.
        user_id: Owner user identifier.
        priority: Optional priority override.

        Returns:
        Created CronJob with id and next_run set.

        Raises:
        AutopilotDisabledError: If autopilot scheduling is disabled or no
            dispatch rail_id is configured.
        ExtractionError: If NL extraction fails.
        DuplicateCronJobError: If an equivalent active job already exists.
        ValueError: If max_jobs limit exceeded.
        """
        if not self._config.agent.autopilot.enabled or self._resolve_dispatch_rail_id() is None:
            logger.warning(
                "Cron job submission rejected: dispatch path not configured "
                "(autopilot.enabled=%s, user=%s)",
                self._config.agent.autopilot.enabled,
                user_id,
            )
            raise AutopilotDisabledError(AUTOPILOT_REQUIRED_FOR_CRON)

        # Check job limit
        current_count = await self._store.count_by_user(user_id)
        if current_count >= self._cron_config.max_jobs:
            raise ValueError(
                f"Maximum scheduled jobs ({self._cron_config.max_jobs}) reached for user {user_id}"
            )

        # Extract schedule from natural language
        extraction = await self._extraction_service.extract(natural_language)

        existing = await self._store.find_active_duplicate(
            user_id,
            description=extraction.description,
            schedule_kind=extraction.schedule_kind,
            schedule_value=extraction.schedule_value,
        )
        if existing is not None:
            logger.info(
                "Cron job duplicate rejected: existing=%s user=%s description=%s",
                existing.id,
                user_id,
                extraction.description[:50],
            )
            raise DuplicateCronJobError(existing)

        # Generate job ID
        job_id = uuid.uuid4().hex[:12]

        # Compute next_run via ScheduleSpec
        spec = self._schedule_spec(extraction.schedule_kind.value, extraction.schedule_value)
        next_run = spec.next_after(datetime.now(tz=UTC))
        if next_run is None:
            raise ValueError(
                f"Schedule {extraction.schedule_kind}={extraction.schedule_value} has no valid next run"
            )

        # Create CronJob
        job = CronJob(
            id=job_id,
            user_id=user_id,
            description=extraction.description,
            schedule_kind=extraction.schedule_kind,
            schedule_value=extraction.schedule_value,
            end_condition=extraction.end_condition,
            priority=priority or self._cron_config.default_priority,
            status=JobStatus.PENDING,
            next_run=next_run,
        )

        # Persist to store
        await self._store.create(job)

        logger.info(
            "Cron job created: id=%s user=%s next_run=%s description=%s",
            job_id,
            user_id,
            next_run.isoformat(),
            extraction.description[:50],
        )

        return job

    async def list_jobs(
        self,
        user_id: str,
        status: JobStatus | str | None = None,
    ) -> list[CronJob]:
        """List jobs for user, optionally filtered by status.

        Args:
        user_id: User identifier.
        status: Optional status filter.

        Returns:
        List of CronJob objects owned by this user.
        """
        return await self._store.list_by_user(user_id, status)

    async def cancel_job(self, job_id: str, user_id: str) -> bool:
        """Cancel a pending job.

        Args:
        job_id: Job identifier.
        user_id: User identifier (for ownership validation).

        Returns:
        True if cancelled, False if not found or not owned.
        """
        job = await self._store.get(job_id)
        if job is None or job.user_id != user_id:
            return False

        if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            return False

        await self._store.update_status(job_id, JobStatus.CANCELLED)
        logger.info("Cron job cancelled: id=%s user=%s", job_id, user_id)
        return True

    async def show_job(self, job_id: str, user_id: str) -> CronJob | None:
        """Get job details.

        Args:
        job_id: Job identifier.
        user_id: User identifier (for ownership validation).

        Returns:
        CronJob if found and owned by user, None otherwise.
        """
        job = await self._store.get(job_id)
        if job is None or job.user_id != user_id:
            return None
        return job

    def _resolve_dispatch_rail_id(self) -> str | None:
        """Return the rail id to bind for cron-dispatched goals.

        Uses ``agent.autopilot.default_rail`` from the host config. Returns
        ``None`` when no rail is configured (dispatch is rejected upstream).
        """
        rail_id = getattr(self._config.agent.autopilot, "default_rail", None)
        if isinstance(rail_id, str) and rail_id.strip():
            return rail_id.strip()
        return None

    def _is_dispatch_ready(self) -> bool:
        """Return True when the loop-native dispatch path is configured."""
        return (
            self._loop_input_dispatcher is not None
            and self._persistence_manager is not None
            and self._resolve_dispatch_rail_id() is not None
        )

    async def _tick_loop(self) -> None:
        """Periodic monitoring loop for due jobs."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Cron tick error")

            await asyncio.sleep(self._cron_config.poll_interval)

    async def _tick(self) -> None:
        """Single tick: check due jobs and dispatch."""
        now = datetime.now(tz=UTC)
        due_jobs = await self._store.get_due_jobs(now)

        if not due_jobs:
            return

        logger.debug("Cron tick: %d due jobs", len(due_jobs))

        rail_id = self._resolve_dispatch_rail_id()
        for job in due_jobs:
            # Check end condition
            if self._is_job_expired(job, now):
                await self._store.update_status(job.id, JobStatus.COMPLETED, last_run=now)
                logger.info("Cron job expired: id=%s end_condition=%s", job.id, job.end_condition)
                continue

            # Mark as running
            await self._store.update_status(job.id, JobStatus.RUNNING)

            # Dispatch via the loop-native submission path: create a fresh
            # loop and enqueue a loop_input carrying autopilot_rail_id so
            # run_with_progress binds a LoopRailInterpreter.
            if rail_id and self._loop_input_dispatcher and self._persistence_manager:
                try:
                    loop_id = await self._dispatch_via_loop(job, rail_id)
                    logger.info(
                        "Cron job dispatched: id=%s loop_id=%s rail_id=%s",
                        job.id,
                        loop_id,
                        rail_id,
                    )

                    # For one-shot jobs, mark completed after dispatch
                    # (goal execution is async, we just track dispatch success)
                    if job.is_one_shot():
                        await self._store.update_status(
                            job.id,
                            JobStatus.COMPLETED,
                            last_run=now,
                        )
                        logger.info("One-shot cron job completed: id=%s", job.id)

                except Exception:
                    logger.exception("Cron job dispatch failed: id=%s", job.id)
                    await self._store.update_status(job.id, JobStatus.FAILED)
            else:
                logger.warning(
                    "Cron dispatch path not configured (no rail_id / dispatcher): id=%s",
                    job.id,
                )
                await self._store.update_status(job.id, JobStatus.FAILED)

    async def _dispatch_via_loop(self, job: CronJob, rail_id: str) -> str:
        """Create a fresh loop and enqueue the cron goal via loop_input.

        Args:
        job: Due CronJob to dispatch.
        rail_id: Builtin rail id to bind for this goal.

        Returns:
        The newly created loop_id.
        """
        from uuid_utils import uuid7

        loop_id = str(uuid7())

        # Resolve workspace (daemon workspace — cron goals are daemon-owned).
        from soothe.workspace import resolve_loop_workspace

        try:
            effective_workspace = resolve_loop_workspace(
                loop_id=loop_id,
                client_workspace=None,
            )
        except ValueError:
            from soothe.workspace import resolve_daemon_workspace

            effective_workspace = resolve_daemon_workspace()

        from soothe.sloop.checkpoints.directory_manager import (
            PersistenceDirectoryManager,
        )

        loop_dir = PersistenceDirectoryManager.get_loop_directory(loop_id)
        loop_dir.mkdir(parents=True, exist_ok=True)

        await self._persistence_manager.register_loop(
            loop_id=loop_id,
            current_thread_id="",
            status="created",
        )
        await self._persistence_manager.update_loop_metadata(
            loop_id,
            current_workspace=str(effective_workspace),
            cron_job_id=job.id,
        )

        # Enqueue the goal as a loop_input with autopilot_rail_id. The
        # LoopInputDispatcher worker calls _process_loop_input_message →
        # run_query(autopilot_rail_id=…) → LoopRunRequest → run_with_progress.
        queue_payload: dict[str, Any] = {
            "type": "input",
            "text": job.description,
            "client_id": None,
            "autopilot_rail_id": rail_id,
            "cron_job_id": job.id,
        }
        await self._loop_input_dispatcher.enqueue(loop_id, queue_payload)

        try:
            await self._persistence_manager.increment_loop_message_count(loop_id, human=1)
        except Exception:
            logger.warning(
                "Failed to increment human_message_count for cron loop %s",
                loop_id,
                exc_info=True,
            )

        return loop_id

    def _is_job_expired(self, job: CronJob, now: datetime) -> bool:
        """Check if recurring job has reached end condition.

        Args:
        job: CronJob to check.
        now: Current time.

        Returns:
        True if job should be marked completed due to end condition.
        """
        if not job.end_condition:
            return False

        # Parse end condition
        # Format: "until YYYY-MM-DD" or "for N days/weeks"
        cond = job.end_condition.lower()

        if cond.startswith("until "):
            try:
                end_date = datetime.fromisoformat(cond[6:].strip())
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=UTC)
                return now >= end_date
            except ValueError:
                logger.warning("Invalid end_condition: %s", job.end_condition)
                return False

        if cond.startswith("for "):
            # Parse duration: "for 2 weeks", "for 10 days"
            import re

            match = re.match(r"for (\d+) (day|days|week|weeks)", cond)
            if match:
                count = int(match.group(1))
                unit = match.group(2)
                if unit in ("week", "weeks"):
                    delta_days = count * 7
                else:
                    delta_days = count

                expiry = job.created_at + __import__("datetime").timedelta(days=delta_days)
                return now >= expiry

        return False

    async def handle_goal_completion(
        self,
        job_id: str,
        success: bool,
    ) -> None:
        """Handle goal completion callback for recurring jobs.

        Called when a goal dispatched from a cron job completes.

        Args:
        job_id: Cron job identifier.
        success: Whether goal execution succeeded.
        """
        job = await self._store.get(job_id)
        if job is None:
            logger.warning("Goal completion for unknown cron job: %s", job_id)
            return

        if job.is_recurring() and job.status == JobStatus.RUNNING:
            # Compute next run
            spec = self._schedule_spec(job.schedule_kind.value, job.schedule_value)
            next_run = spec.next_after(datetime.now(tz=UTC))

            if next_run and not self._is_job_expired(job, next_run):
                # Reschedule
                await self._store.update_next_run(
                    job_id,
                    next_run,
                    job.run_count + 1,
                )
                logger.info(
                    "Cron job rescheduled: id=%s next_run=%s run_count=%d",
                    job_id,
                    next_run.isoformat(),
                    job.run_count + 1,
                )
            else:
                # Mark completed
                await self._store.update_status(
                    job_id,
                    JobStatus.COMPLETED,
                    last_run=datetime.now(tz=UTC),
                )
                logger.info("Recurring cron job completed: id=%s", job_id)
        elif success:
            await self._store.update_status(
                job_id,
                JobStatus.COMPLETED,
                last_run=datetime.now(tz=UTC),
            )
        else:
            await self._store.update_status(job_id, JobStatus.FAILED)
