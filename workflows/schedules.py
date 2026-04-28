"""Temporal Schedule registration for the stuck-event sweeper.

The sweep workflow (`SweepStuckEventsWorkflow`) re-issues `start_workflow`
for events stuck at RECEIVED (insert succeeded, `start_workflow` did not)
*or* PROCESSING (workflow ran, then died before reaching a terminal state).
Running it on a Temporal Schedule gives us:
- Leader election for free (only one schedule fires per interval, no matter
  how many worker replicas register it).
- Observability in Temporal Web UI — pause, trigger, inspect history.
- `ScheduleOverlapPolicy.SKIP` — if a sweep run is still in flight when the
  next interval fires, skip the next firing rather than queueing.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

logger = logging.getLogger(__name__)

SWEEP_SCHEDULE_ID = "sweep-stuck-events"


def _build_schedule(
    task_queue: str,
    interval_seconds: int,
    received_grace_seconds: int,
    processing_grace_seconds: int,
    limit: int,
) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "SweepStuckEventsWorkflow",
            args=[received_grace_seconds, processing_grace_seconds, limit],
            id="sweep-stuck-events-run",
            task_queue=task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval_seconds))],
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(minutes=5),
        ),
    )


async def ensure_sweep_schedule(
    client: Client,
    task_queue: str,
    interval_seconds: int = 60,
    received_grace_seconds: int = 300,
    processing_grace_seconds: int = 900,
    limit: int = 100,
) -> None:
    """Idempotent — safe to call on every worker startup.

    First worker creates the schedule; subsequent workers (and reboots) hit
    `ALREADY_EXISTS` and update the spec instead. The update keeps the
    Schedule's history but applies any cadence/grace/limit changes from
    the deployed code.

    `processing_grace_seconds` MUST exceed `WORKFLOW_RUN_TIMEOUT` plus a
    buffer, or the sweeper races live workflows. The caller (worker
    bootstrap or `ensure_schedules` management command) enforces this
    invariant.
    """
    desired = _build_schedule(
        task_queue,
        interval_seconds,
        received_grace_seconds,
        processing_grace_seconds,
        limit,
    )
    try:
        await client.create_schedule(SWEEP_SCHEDULE_ID, desired)
        logger.info("Created Temporal sweep schedule (every %ds)", interval_seconds)
        return
    except RPCError as e:
        if e.status != RPCStatusCode.ALREADY_EXISTS:
            raise

    handle = client.get_schedule_handle(SWEEP_SCHEDULE_ID)

    async def _updater(inp: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(schedule=dataclasses.replace(
            inp.description.schedule,
            spec=desired.spec,
            policy=desired.policy,
            action=desired.action,
        ))

    await handle.update(_updater)
    logger.info("Updated existing Temporal sweep schedule (every %ds)", interval_seconds)
