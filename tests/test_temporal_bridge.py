"""Bridge-thread lifecycle: `shutdown` is idempotent and is invoked
from gunicorn's `worker_exit` hook. Also pins the backpressure cap on
`submit`."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from project import temporal_bridge
from project.temporal_bridge import BridgeOverloadedError, submit


def _build_bridge_state() -> tuple[asyncio.AbstractEventLoop, threading.Thread, MagicMock]:
    """Spin up a real asyncio loop in a daemon thread plus a stand-in
    Temporal client. We don't go through `get_temporal_client()` because
    that would require a live Temporal frontend; we install the singletons
    directly to exercise the shutdown path."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    # Wait for the loop to actually start running before returning so
    # `loop.is_running()` is True for the test.
    for _ in range(50):
        if loop.is_running():
            break
        time.sleep(0.01)
    client = MagicMock()
    client.close = MagicMock(return_value=None)
    return loop, thread, client


class TestShutdown:
    def test_idempotent_when_uninitialized(self):
        """Safe to call when no bridge was ever created — covers the path
        where a worker process never served an ingest request."""
        temporal_bridge._loop = None
        temporal_bridge._loop_thread = None
        temporal_bridge._client = None

        temporal_bridge.shutdown()  # must not raise

        assert temporal_bridge._loop is None
        assert temporal_bridge._loop_thread is None
        assert temporal_bridge._client is None

    def test_stops_loop_and_joins_thread(self):
        loop, thread, client = _build_bridge_state()
        temporal_bridge._loop = loop
        temporal_bridge._loop_thread = thread
        temporal_bridge._client = client

        temporal_bridge.shutdown(timeout=2.0)

        assert temporal_bridge._loop is None
        assert temporal_bridge._loop_thread is None
        assert temporal_bridge._client is None
        # The loop thread should have exited.
        assert not thread.is_alive()

    def test_double_call_is_safe(self):
        loop, thread, client = _build_bridge_state()
        temporal_bridge._loop = loop
        temporal_bridge._loop_thread = thread
        temporal_bridge._client = client

        temporal_bridge.shutdown(timeout=2.0)
        temporal_bridge.shutdown(timeout=2.0)  # second call: must not raise


class TestBackpressure:
    """The cap is what prevents a Temporal slowdown from freezing the
    worker pool. Tests use `override_settings` to set a tiny cap, then
    fill it with stuck submits, and confirm the next `submit` raises
    `BridgeOverloadedError` immediately rather than blocking on the
    submit timeout."""

    def setup_method(self):
        # Clean slate — counter starts at 0 for each test.
        temporal_bridge._inflight_count = 0

    def teardown_method(self):
        temporal_bridge._inflight_count = 0

    @override_settings(TEMPORAL_BRIDGE_MAX_INFLIGHT=2)
    def test_rejects_submit_over_cap(self):
        """Pre-fill the counter to the cap, then `submit` must raise
        instantly without touching the loop. The submit timeout is set
        to a value that would dominate the test runtime if the cap
        weren't checked first."""
        temporal_bridge._inflight_count = 2  # at cap

        async def _slow_coro():
            await asyncio.sleep(60)
            return "should-never-return"

        coro = _slow_coro()
        try:
            with pytest.raises(BridgeOverloadedError, match="cap=2"):
                submit(coro, timeout=10.0)
        finally:
            # Close the un-awaited coroutine so pytest doesn't warn —
            # the cap check fires before `submit` schedules it on the
            # loop, so it's our responsibility to clean up.
            coro.close()

    @override_settings(TEMPORAL_BRIDGE_MAX_INFLIGHT=4)
    def test_inflight_counter_decrements_on_success(self):
        """Counter must decrement after a successful submit so steady
        state doesn't accumulate phantom in-flight requests."""
        # Spin a real bridge loop so submit actually runs.
        loop, thread, _ = _build_bridge_state()
        temporal_bridge._loop = loop
        temporal_bridge._loop_thread = thread

        try:
            async def _fast_coro():
                return 42

            result = submit(_fast_coro(), timeout=2.0)
            assert result == 42
            assert temporal_bridge._inflight_count == 0
        finally:
            temporal_bridge.shutdown(timeout=2.0)

    @override_settings(TEMPORAL_BRIDGE_MAX_INFLIGHT=4)
    def test_inflight_counter_decrements_on_exception(self):
        """Counter must decrement even when the coroutine raises, or a
        single bad submit poisons the cap forever."""
        loop, thread, _ = _build_bridge_state()
        temporal_bridge._loop = loop
        temporal_bridge._loop_thread = thread

        try:
            async def _raising_coro():
                raise RuntimeError("synthetic")

            with pytest.raises(RuntimeError, match="synthetic"):
                submit(_raising_coro(), timeout=2.0)
            assert temporal_bridge._inflight_count == 0
        finally:
            temporal_bridge.shutdown(timeout=2.0)


class TestGunicornWiring:
    """Pin the wiring: gunicorn's `worker_exit` calls `shutdown`, and
    `post_worker_init` calls `reset_for_fork`. Without these tests, a
    refactor that breaks the lifecycle hooks would only surface in
    production via half-open gRPC channels and stranded loop threads."""

    def test_worker_exit_invokes_shutdown(self):
        from project import gunicorn_conf

        with patch.object(temporal_bridge, "shutdown") as mock_shutdown:
            gunicorn_conf.worker_exit(server=MagicMock(), worker=MagicMock())
            mock_shutdown.assert_called_once()

    def test_post_worker_init_invokes_reset_for_fork(self):
        """Pin the post-fork reset hook alongside the exit hook so the
        full lifecycle (fork → reset → exit → shutdown) is tested."""
        from project import gunicorn_conf

        with patch.object(temporal_bridge, "reset_for_fork") as mock_reset:
            gunicorn_conf.post_worker_init(worker=MagicMock())
            mock_reset.assert_called_once()
