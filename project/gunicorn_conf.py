"""Gunicorn configuration.

Env-driven (`GUNICORN_*`) so the same image deploys against different
capacity targets without rebuilding.

## Concurrency model

The hot path is sequential I/O — Redis dedup → DB INSERT →
`start_workflow` (gRPC, blocking on a Future from the bridge thread) →
Redis mark — at ~30-70ms p50 under no contention, almost all of it
socket wait.

`worker_class="gthread"` runs each worker process with N threads
multiplexing requests. Python releases the GIL during socket I/O — ~95%
of this hot path — so threads progress in parallel. `workers × threads`
is the concurrent-request ceiling: default 4 × 8 = 32, comfortably
enough to absorb 500 RPS at 50ms p50.

## Fork safety

`preload_app = False` — each worker imports the app independently so
the Temporal bridge thread (or any other connection-pooling library)
isn't created in the master and copied broken into forks. The
`post_worker_init` hook re-nulls bridge state as defense in depth; the
bridge is process-local (one per worker process, regardless of how
many gthread threads share it).
"""

from __future__ import annotations

import os

workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

preload_app = False
accesslog = "-"
errorlog = "-"


def post_worker_init(worker):  # noqa: ARG001
    from project.temporal_bridge import reset_for_fork

    reset_for_fork()


def worker_exit(server, worker):  # noqa: ARG001
    """Drain the per-worker Temporal bridge on graceful shutdown.
    Without this, the daemon thread is yanked at process exit and
    leaves gRPC streams in a half-open state."""
    from project.temporal_bridge import shutdown

    shutdown()
