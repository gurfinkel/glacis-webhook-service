// k6 load test — POST /webhook at LOAD_RPS (default 100) for LOAD_DURATION
// (default 60s). RPS is env-driven so the same script gates CI (modest
// rate, single-pod-default config) and validates production scale-out
// (high rate, fleet config) without forking.
//
// Pass criteria (from `thresholds`):
//   - p99 latency < LOAD_P99_MS (default 500ms) for accepted (200) requests
//   - error rate < 1% (any non-2xx)
//
// Capacity math the defaults reflect:
//   - Hot path: Redis dedup + DB INSERT + start_workflow + Redis mark ≈
//     30-70ms p50 under no contention.
//   - Default Gunicorn config (`gunicorn_conf.py`): 4 workers × 8 threads
//     = 32 concurrent requests. At 50ms p50 that headrooms ~640 RPS.
//   - 100 RPS is the CI-realistic target on a laptop / small CI runner.
//     500 RPS is the in-cluster validation target — pass `LOAD_RPS=500`
//     against a properly provisioned stack.
//
// HMAC signing implemented per the StandardWebhooks spec
// (https://www.standardwebhooks.com/) so the same signature path the auth
// class verifies is exercised end-to-end.
//
// Run:
//   ./scripts/demo.sh                                # bring up stack + seed
//   k6 run load/ingest.k6.js                         # default 100 RPS
//   LOAD_RPS=500 k6 run load/ingest.k6.js            # production target
//
// `demo.sh` already seeds the demo vendor credential the script signs
// with; no extra setup needed.

import http from "k6/http";
import { check } from "k6";
import crypto from "k6/crypto";
import encoding from "k6/encoding";

const VENDOR_ID = __ENV.LOAD_VENDOR_ID || "demo";
const SECRET = __ENV.LOAD_HMAC_SECRET || "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw";
const TARGET = __ENV.LOAD_TARGET || "http://localhost:8000/webhook";

const RPS = parseInt(__ENV.LOAD_RPS || "100", 10);
const DURATION = __ENV.LOAD_DURATION || "60s";
const P99_MS = parseInt(__ENV.LOAD_P99_MS || "500", 10);

// Pre-allocate enough VUs to drive the requested rate with k6 headroom.
// Rule of thumb: VUs ≥ RPS × p99-seconds × 2 for transient spikes.
const PRE_ALLOC = Math.max(50, Math.ceil(RPS * (P99_MS / 1000) * 2));
const MAX_VUS = Math.max(PRE_ALLOC, RPS);

// StandardWebhooks signs `${msg_id}.${unix_timestamp}.${body}` with HMAC-SHA256
// using the secret's base64-decoded bytes (the `whsec_` prefix is stripped first).
function signWebhook(msgId, timestamp, body) {
  const rawSecret = SECRET.replace(/^whsec_/, "");
  const keyBytes = encoding.b64decode(rawSecret, "std");
  const toSign = `${msgId}.${timestamp}.${body}`;
  const sig = crypto.hmac("sha256", keyBytes, toSign, "base64");
  return `v1,${sig}`;
}

export const options = {
  scenarios: {
    sustained: {
      executor: "constant-arrival-rate",
      rate: RPS,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOC,
      maxVUs: MAX_VUS,
    },
  },
  thresholds: {
    "http_req_duration{status:200}": [`p(99)<${P99_MS}`],
    "http_req_failed": ["rate<0.01"],
  },
};

export default function () {
  const msgId = `msg_${__VU}_${__ITER}_${Date.now()}`;
  const timestamp = Math.floor(Date.now() / 1000);
  const body = JSON.stringify({
    TrackingNumber: `K6-${__VU}-${__ITER}`,
    EventType: "IN_TRANSIT",
    CarrierCode: "FEDEX",
    EventTimestamp: new Date().toISOString(),
  });

  const signature = signWebhook(msgId, timestamp, body);

  const res = http.post(TARGET, body, {
    headers: {
      "Content-Type": "application/json",
      "webhook-id": msgId,
      "webhook-timestamp": String(timestamp),
      "webhook-signature": signature,
      "x-vendor-id": VENDOR_ID,
    },
  });

  check(res, {
    "status is 200": (r) => r.status === 200,
    "response is accepted or already_received": (r) => {
      try {
        const body = r.json();
        return body.status === "accepted" || body.status === "already_received";
      } catch {
        return false;
      }
    },
  });
}
