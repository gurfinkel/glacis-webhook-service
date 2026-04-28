"""Temporal SDK ↔ sync Django bridge.

The Temporal Python SDK is async-only. Django sync views need to call
`client.start_workflow(...)`. One daemon thread runs an asyncio loop
forever, owns the `temporalio.Client`, and accepts coroutines submitted
via `asyncio.run_coroutine_threadsafe`. Sync callers block on the
resulting `Future` with a timeout; errors propagate as normal exceptions
across the future boundary.

## Fork-safety contract

Gunicorn forks worker processes from a master. POSIX `fork()` copies
only the calling thread, so the loop thread does not survive into the
child if the bridge is initialized in the master. Three mitigations:

1. Never call `get_temporal_client()` at import / `ready()` time. The
   first call must be from request-handling code in the worker.
2. Run Gunicorn with `preload_app = False` (the default).
3. `reset_for_fork()` is called from `gunicorn_conf.post_worker_init`
   so even if (1) is violated by a future contributor, the children
   rebuild their own loop and client.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable
from concurrent.futures import Future

from django.conf import settings
from temporalio.client import Client

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_client: Client | None = None
# RLock so `get_temporal_client` can hold the lock while calling
# `_ensure_loop` (which also acquires it).
_init_lock = threading.RLock()

# Cap on sync callers blocked on the bridge at once. Without it, a
# Temporal frontend slowdown cascades into every gthread thread holding
# for the submit timeout — freezing worker capacity 1:1 with upstream
# latency. When this cap is hit, new submits raise immediately so
# gthread threads return to handling other requests.
_inflight_count = 0
_inflight_lock = threading.Lock()


class BridgeOverloadedError(RuntimeError):
    """Raised by `submit` when the in-flight cap is exceeded. The view
    layer converts this to 503."""


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _init_lock:
        if _loop is not None and _loop_thread is not None and _loop_thread.is_alive():
            return _loop
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever,
            name="temporal-bridge-loop",
            daemon=True,
        )
        _loop_thread.start()
        logger.info("Temporal bridge loop thread started")
    return _loop


async def _connect() -> Client:
    return await Client.connect(
        settings.TEMPORAL_ADDRESS,
        namespace=settings.TEMPORAL_NAMESPACE,
    )


def get_temporal_client() -> Client:
    """Process-singleton `temporalio.Client`. Double-checked locking so
    two threads on the first call don't each construct and leak a
    client."""
    global _client
    if _client is not None:
        return _client
    with _init_lock:
        if _client is not None:
            return _client
        loop = _ensure_loop()
        fut: Future[Client] = asyncio.run_coroutine_threadsafe(_connect(), loop)
        _client = fut.result(timeout=10)
        logger.info(
            "Temporal client connected to %s (namespace=%s)",
            settings.TEMPORAL_ADDRESS, settings.TEMPORAL_NAMESPACE,
        )
        return _client


def submit[T](coro: Awaitable[T], timeout: float | None = None) -> T:
    """Run an async coroutine on the bridge loop from a sync caller.
    Errors raised by the coroutine surface at the call site. Raises
    `BridgeOverloadedError` immediately if more than
    `settings.TEMPORAL_BRIDGE_MAX_INFLIGHT` callers are already
    blocked."""
    if timeout is None:
        timeout = settings.TEMPORAL_START_WORKFLOW_TIMEOUT
    cap = settings.TEMPORAL_BRIDGE_MAX_INFLIGHT
    global _inflight_count
    with _inflight_lock:
        if _inflight_count >= cap:
            raise BridgeOverloadedError(
                f"Temporal bridge overloaded: {_inflight_count} in-flight, cap={cap}. "
                f"Likely upstream Temporal slowdown — request shed, retry."
            )
        _inflight_count += 1
    try:
        loop = _ensure_loop()
        fut: Future[T] = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
        return fut.result(timeout=timeout)
    finally:
        with _inflight_lock:
            _inflight_count -= 1


def reset_for_fork() -> None:
    """Drop the singletons so the post-fork worker creates its own loop
    and client. Idempotent. Call only from a fork hook."""
    global _loop, _loop_thread, _client
    _loop = None
    _loop_thread = None
    _client = None


def shutdown(timeout: float = 5.0) -> None:
    """Stop the bridge loop and join its thread on worker exit. Wired
    into `gunicorn_conf.worker_exit`. Idempotent.

    This is a soft shutdown, not a graceful drain: pending submissions
    inherit gunicorn's `graceful_timeout` window via the request handler
    that submitted them (the sync caller is still blocking on
    `Future.result(timeout=...)`), not via the bridge itself. Drops the
    `_client` reference rather than calling `.close()` —
    `temporalio.Client` doesn't expose one in the pinned SDK version;
    stopping the loop tears the gRPC channel down via grpc-aio's atexit
    handlers."""
    global _loop, _loop_thread, _client

    loop = _loop
    thread = _loop_thread

    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)

    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("Bridge: loop thread did not exit within %.1fs", timeout)

    _loop = None
    _loop_thread = None
    _client = None
    logger.info("Temporal bridge shut down")
