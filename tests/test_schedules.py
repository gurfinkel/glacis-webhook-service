"""Schedule registration is idempotent. Sweep workflow scans both
stuck-state lists and fans out reissues."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.service import RPCError, RPCStatusCode

from workflows.schedules import SWEEP_SCHEDULE_ID, ensure_sweep_schedule


@pytest.mark.asyncio
class TestEnsureSweepSchedule:
    async def test_creates_when_not_exists(self):
        client = MagicMock()
        client.create_schedule = AsyncMock()
        await ensure_sweep_schedule(client, task_queue="q", interval_seconds=60)
        client.create_schedule.assert_awaited_once()
        called_id = client.create_schedule.await_args.args[0]
        assert called_id == SWEEP_SCHEDULE_ID

    async def test_updates_when_already_exists(self):
        client = MagicMock()
        client.create_schedule = AsyncMock(
            side_effect=RPCError("exists", RPCStatusCode.ALREADY_EXISTS, b"")
        )
        handle = MagicMock()
        handle.update = AsyncMock()
        client.get_schedule_handle = MagicMock(return_value=handle)

        await ensure_sweep_schedule(client, task_queue="q", interval_seconds=60)

        client.create_schedule.assert_awaited_once()
        client.get_schedule_handle.assert_called_once_with(SWEEP_SCHEDULE_ID)
        handle.update.assert_awaited_once()

    async def test_propagates_other_rpc_errors(self):
        client = MagicMock()
        client.create_schedule = AsyncMock(
            side_effect=RPCError("frontend down", RPCStatusCode.UNAVAILABLE, b"")
        )
        with pytest.raises(RPCError):
            await ensure_sweep_schedule(client, task_queue="q")


@pytest.mark.asyncio
class TestSweepStuckEventsWorkflow:
    """The workflow itself: scan both states + fan-out activities. We test the
    activity contract via the mocked-activity workflow harness pattern from
    tests/test_workflows.py."""

    async def test_scans_both_states_and_fans_out_to_reissue(self):
        import uuid

        from temporalio import activity
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker

        from workflows.definitions import SweepStuckEventsWorkflow

        received_calls: list = []
        processing_calls: list = []
        reissue_calls: list = []

        @activity.defn(name="list_stuck_received")
        async def fake_list_received(older_than_seconds: int, limit: int) -> list[dict]:
            received_calls.append((older_than_seconds, limit))
            return [
                {"id": "evt-r1", "idempotency_key": "kr1", "raw_payload": {"a": 1}},
            ]

        @activity.defn(name="list_stuck_processing")
        async def fake_list_processing(older_than_seconds: int, limit: int) -> list[dict]:
            processing_calls.append((older_than_seconds, limit))
            return [
                {"id": "evt-p1", "idempotency_key": "kp1", "raw_payload": {"b": 2}},
                {"id": "evt-p2", "idempotency_key": "kp2", "raw_payload": {"c": 3}},
            ]

        @activity.defn(name="reissue_classify_workflow")
        async def fake_reissue(idempotency_key: str, event_id: str, payload: dict) -> None:
            reissue_calls.append((idempotency_key, event_id, payload))

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-sweep",
                workflows=[SweepStuckEventsWorkflow],
                activities=[fake_list_received, fake_list_processing, fake_reissue],
            ):
                count = await env.client.execute_workflow(
                    SweepStuckEventsWorkflow.run,
                    args=[300, 900, 100],
                    id=f"test-sweep-{uuid.uuid4()}",
                    task_queue="test-sweep",
                )

        assert count == 3
        assert received_calls == [(300, 100)]
        assert processing_calls == [(900, 100)]
        # All three rows reach reissue regardless of which state they came from.
        keys = sorted(call[0] for call in reissue_calls)
        assert keys == ["kp1", "kp2", "kr1"]


class TestProcessingGraceInvariant:
    """The PROCESSING grace must exceed `WORKFLOW_RUN_TIMEOUT + buffer`, or
    the sweeper would re-issue payloads to live workflows."""

    def test_rejects_grace_smaller_than_run_timeout(self):
        from workflows.worker import _validate_processing_grace

        with pytest.raises(ValueError, match="race live workflows"):
            _validate_processing_grace(60)  # 1 minute, way below 10-min run timeout

    def test_rejects_grace_at_run_timeout_without_buffer(self):
        from workflows.definitions import WORKFLOW_RUN_TIMEOUT
        from workflows.worker import _validate_processing_grace

        with pytest.raises(ValueError):
            _validate_processing_grace(int(WORKFLOW_RUN_TIMEOUT.total_seconds()))

    def test_accepts_default_900_seconds(self):
        from workflows.worker import _validate_processing_grace

        _validate_processing_grace(900)  # default — must not raise
