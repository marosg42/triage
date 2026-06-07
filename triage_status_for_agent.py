#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "python-dotenv",
# ]
# ///
"""
Output UUIDs of finished, failed pipeline runs with no triager assigned.
One UUID per line on stdout — intended as input to another script.
"""

import argparse
import os
import requests
import sys
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.environ["WEEBL_API_BASE"]
TOKEN = os.environ["WEEBL_TOKEN"]


def fetch_all(session, url, params, page_size=100):
    results = []
    params = dict(params, limit=page_size, offset=0)
    while True:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        total = data.get("count", "?")
        print(f"\r  fetching: {len(results)}/{total}", end="", file=sys.stderr, flush=True)
        if not data.get("next"):
            break
        params["offset"] += params["limit"]
    print(file=sys.stderr)
    return results


def triager_name(pipeline):
    tb = pipeline.get("triaged_by")
    if isinstance(tb, dict):
        return tb.get("username")
    return str(tb) if tb else None


def main():
    parser = argparse.ArgumentParser(
        description="Output UUIDs of finished failed pipelines with no triager."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Limit to pipelines from the last N days (default: no limit)",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers["Authorization"] = f"Token {TOKEN}"

    since = None
    if args.days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        print(f"Fetching pipelines (last {args.days} days)...", file=sys.stderr)
    else:
        print("Fetching pipelines...", file=sys.stderr)

    params = {
        "triaged": "false",
        "completed": "true",
        "failed": "true",
        "ordering": "-completed_at",
    }
    if since:
        params["completed_at_from"] = since

    pipelines = fetch_all(session, f"{API_BASE}/pipelines/", params)

    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        pipelines = [
            p for p in pipelines
            if p.get("completed_at") and
            datetime.fromisoformat(p["completed_at"].replace("Z", "+00:00")) >= since_dt
        ]

    untriaged = [p for p in pipelines if not triager_name(p)]

    print(f"Found {len(untriaged)} untriaged runs.", file=sys.stderr)

    for p in untriaged:
        print(p["uuid"])


if __name__ == "__main__":
    main()
