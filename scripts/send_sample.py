#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "standardwebhooks"]
# ///
"""Sign and post a sample webhook to the local service.

Usage:
    ./scripts/send_sample.py                    # default: shipment_fedex
    ./scripts/send_sample.py invoice_vendor_a   # any file from samples/
    ./scripts/send_sample.py --all              # post every sample, in order

Reads from samples/<name>.json. Signs with the demo HMAC secret seeded by
scripts/demo.sh. Prints the response status, body, and idempotency key for
each post; exit code 0 if every post got 200, non-zero otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from standardwebhooks.webhooks import Webhook

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = REPO_ROOT / "samples"

ENDPOINT = "http://localhost:8000/webhook"
VENDOR_ID = "demo"
SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


def post_sample(name: str) -> bool:
    sample_path = SAMPLES_DIR / f"{name}.json"
    if not sample_path.exists():
        print(f"  [error] sample not found: {sample_path}", file=sys.stderr)
        return False

    body = sample_path.read_text()
    msg_id = f"demo-{name}-{int(time.time() * 1000)}"
    ts = datetime.now(tz=UTC)
    sig = Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body)

    try:
        r = requests.post(
            ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/json",
                "webhook-id": msg_id,
                "webhook-timestamp": str(int(ts.timestamp())),
                "webhook-signature": sig,
                "x-vendor-id": VENDOR_ID,
            },
            timeout=10,
        )
    except requests.exceptions.ConnectionError as e:
        print(f"  [error] could not reach {ENDPOINT}: {e}", file=sys.stderr)
        print("  is docker compose up?", file=sys.stderr)
        return False

    ok = r.status_code == 200
    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {"raw": r.text[:200]}

    marker = "ok" if ok else "FAIL"
    key = data.get("idempotency_key", "—")
    print(f"  [{marker}] {name:24} {r.status_code}  key={key}")
    if not ok:
        print(f"         body={data}", file=sys.stderr)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign and post a sample webhook to the local service.",
    )
    parser.add_argument(
        "name", nargs="?", default="shipment_fedex",
        help="sample basename without .json (default: shipment_fedex)",
    )
    parser.add_argument(
        "--all", action="store_true", help="post every sample in samples/",
    )
    args = parser.parse_args()

    if args.all:
        names = sorted(p.stem for p in SAMPLES_DIR.glob("*.json"))
    else:
        names = [args.name]

    all_ok = True
    for name in names:
        if not post_sample(name):
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
