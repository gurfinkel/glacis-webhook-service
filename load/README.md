# Load testing

`ingest.k6.js` exercises `POST /webhook` end-to-end with HMAC-signed
bodies. Pass criteria are k6 thresholds (`p99<500ms` by default,
`error_rate<1%`) so the script is directly usable as a CI gate.

## Run locally

```bash
./scripts/demo.sh           # brings up the stack and seeds the demo vendor
k6 run load/ingest.k6.js    # 100 RPS / 60s / p99<500ms (CI gate)

# Production-target validation against a cluster sized for it:
LOAD_RPS=500 LOAD_DURATION=120s k6 run load/ingest.k6.js
```

Override target / vendor / secret / RPS / duration / SLO via env:

```bash
LOAD_TARGET=https://staging.example.com/webhook \
LOAD_VENDOR_ID=stress \
LOAD_HMAC_SECRET=whsec_... \
LOAD_RPS=500 \
LOAD_DURATION=300s \
LOAD_P99_MS=300 \
k6 run load/ingest.k6.js
```

## Capacity math — what RPS the default config can hit

The hot path is sequential I/O — Redis dedup → DB INSERT → `start_workflow`
(gRPC, blocking on a Future from the bridge thread) → Redis mark — with
~30-70ms p50 under no contention. Per-config concurrent-request ceiling
(`workers × threads`) bounds the sustainable RPS:

| Config (`gunicorn_conf.py` / env) | Concurrency | RPS at 50ms p50 |
|---|---|---|
| `worker_class=sync, workers=4` | 4 | ~80 |
| `worker_class=gthread, workers=4, threads=8` (default) | 32 | ~640 |
| `workers=8, threads=8` | 64 | ~1280 |

The default ships gthread/4×8 — comfortable headroom for a 500 RPS
single-pod target. CI gates default to 100 RPS (still well within
capacity, but cheap on small runners); set `LOAD_RPS=500` explicitly
to validate the production target against a sized stack.

The numbers above ignore other ceilings that surface as you scale:

- **Postgres `max_connections=100`** — gthread holds one DB connection
  per concurrent request. At `workers × threads = 64` × N replicas, you
  need pgbouncer.
- **Temporal frontend gRPC** — `start_workflow` over cross-AZ links
  adds 5-30ms per call.
- **Bridge-thread loop saturation** — one event loop per worker
  process; `client.start_workflow` is a coroutine the loop runs in
  parallel, so this rarely binds first.

## What's measured

- **`http_req_duration`** — DRF view body wall-clock. Includes auth,
  rate-limit, dedup check, DB insert, `start_workflow`, Redis mark —
  the latency vendors actually see.
- **`http_req_failed`** — non-2xx as a fraction.
- **VU count** — k6 spins up `preAllocatedVUs ≈ RPS × p99 × 2` to
  absorb transient queueing without throttling itself.

## What's NOT measured here

- Workflow processing latency (LLM call, persist) — those run on the
  worker asynchronously after the API ack. Measured via Temporal Web UI
  / OTel histograms instead.
- Burst-of-burst behavior (sudden 5x spike) — single-rate constant
  scenario. Add a `ramping-arrival-rate` scenario for that workload.

## Interpreting failures

If `p99 > LOAD_P99_MS`:
1. **Worker saturation** — check `gunicorn_workers_busy` (OTel). If
   utilization is above ~70%, raise `GUNICORN_THREADS` or
   `GUNICORN_WORKERS`, or scale replicas. Math is at the top of this
   doc.
2. **DB latency** — check `pg_stat_statements` / OTel; the `INSERT`
   on `webhook_events` is the typical bottleneck. Tune `CONN_MAX_AGE`
   or front Postgres with pgbouncer.
3. **Temporal frontend latency** — `start_workflow` over cross-AZ
   gRPC dominates if the worker pool is in another region.
4. **Bridge-thread contention** — one loop per worker process serves
   all gthread threads; if `start_workflow` itself is slow, every
   thread waits behind it. Visible as `bridge_submit_duration`
   skewing right in OTel.
