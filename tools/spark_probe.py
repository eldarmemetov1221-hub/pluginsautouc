#!/usr/bin/env python3
"""Spark API probe — capture REAL responses to finalize the parser.

Run this on a machine WITH network access to api.pubgredeemerbot.com and with a
valid API key. It prints the raw JSON of the key endpoints so you can paste the
results back (especially the finished stock-redeem job) to lock the mapping in
pubg_uc_spark/spark/parser.py.

Usage:
    export SPARK_API_KEY=xxxxx
    # safe / cheap checks + a lookup of a UID:
    python tools/spark_probe.py --uid 51234567890

    # capture the "account not found" job (usually no stock consumed):
    python tools/spark_probe.py --uid 999999999999 --redeem --denom 60

    # capture a SUCCESS job (WARNING: this really redeems 60 UC from stock!):
    python tools/spark_probe.py --uid <your-real-uid> --redeem --denom 60 --yes

Options:
    --base   base url (default https://api.pubgredeemerbot.com)
    --uid    PUBG player id to look up / redeem to
    --denom  UC pack for --redeem (default 60)
    --count  number of packs (default 1)
    --redeem actually POST /v1/jobs/stock-redeem (otherwise only safe GETs)
    --yes    skip the "this really redeems" confirmation prompt
    --wait   per-poll long-poll seconds (default 25, max 60)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("pip install requests first")


def show(title, resp):
    print(f"\n=== {title} -> HTTP {resp.status_code} ===")
    try:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(resp.text[:2000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SPARK_API_URL", "https://api.pubgredeemerbot.com").rstrip("/"))
    ap.add_argument("--key", default=os.environ.get("SPARK_API_KEY", ""))
    ap.add_argument("--uid", default="")
    ap.add_argument("--denom", default="60")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--redeem", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--wait", type=float, default=25.0)
    args = ap.parse_args()

    if not args.key:
        sys.exit("Set SPARK_API_KEY (env) or pass --key")

    headers = {"Accept": "application/json", "X-API-Key": args.key}
    s = requests.Session()
    s.headers.update(headers)

    # Safe, cheap sanity checks.
    show("GET /health", s.get(f"{args.base}/health", timeout=30))
    show("GET /v1/me", s.get(f"{args.base}/v1/me", timeout=30))
    show("GET /v1/quota", s.get(f"{args.base}/v1/quota", timeout=30))
    if args.uid:
        show("GET /v1/player/lookup",
             s.get(f"{args.base}/v1/player/lookup", params={"player_id": args.uid}, timeout=30))

    if not args.redeem:
        print("\n(no --redeem: skipped stock-redeem. Add --redeem to capture a job.)")
        return

    if not args.uid:
        sys.exit("--redeem needs --uid")

    payload = {"player_id": args.uid, "picks": {str(args.denom): args.count}}
    print(f"\nAbout to POST /v1/jobs/stock-redeem {payload}")
    print("WARNING: a SUCCESSFUL redeem really consumes stock and tops up the account.")
    if not args.yes:
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("Aborted.")
            return

    r = s.post(f"{args.base}/v1/jobs/stock-redeem", json=payload, timeout=30)
    show("POST /v1/jobs/stock-redeem", r)
    try:
        job = r.json()
    except ValueError:
        return
    job_id = job.get("job_id") or job.get("id") or job.get("_id")
    if not job_id:
        print("No job_id in response; nothing to poll.")
        return

    # Poll until done/failed.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        jr = s.get(f"{args.base}/v1/jobs/{job_id}", params={"wait": args.wait}, timeout=args.wait + 30)
        show(f"GET /v1/jobs/{job_id}", jr)
        try:
            status = str(jr.json().get("status") or jr.json().get("state") or "").lower()
        except ValueError:
            break
        if status in ("done", "failed"):
            print(f"\nFinal status: {status}. ^ Paste this JSON back to finalize the parser.")
            break


if __name__ == "__main__":
    main()
