"""Opt-in model evaluation using synthetic inputs. No Gmail or Sheets calls."""

import argparse
import json
import statistics
import time
from pathlib import Path

import httpx

from channel_scout.cli import model_clients
from channel_scout.config import Settings
from channel_scout.domain import validate_draft
from channel_scout.integrations import ApiError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["primary", "fallback"], default="primary")
    parser.add_argument("--review-file", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dataset = [json.loads(line) for line in (root / "evals/draft_cases.jsonl").read_text(encoding="utf-8").splitlines()]
    reviewed = []
    latencies = []
    with httpx.Client(timeout=20, trust_env=False, headers={"User-Agent": "ChannelScout/0.1"}) as client:
        pair = model_clients(Settings(root), client)
        model = pair[args.target == "fallback"]
        if model is None:
            parser.error("The fallback must be configured locally first.")
        for case in dataset:
            start = time.perf_counter()
            try:
                draft = validate_draft(model.draft(case["offer"], case["channel"], case["facts"]), case["facts"])
                output_text = f"{draft.subject}\n{draft.body}".lower()
                flagged = any(phrase.lower() in output_text for phrase in case["forbidden"])
                status = "failure" if flagged or case.get("expected", draft.status) != draft.status else draft.status
                reviewed.append({"id": case["id"], "result": status, "draft": draft.model_dump()})
            except (ApiError, ValueError) as error:
                reviewed.append({"id": case["id"], "result": "failure", "reason": type(error).__name__})
            latencies.append(time.perf_counter() - start)
    failures = sum(item["result"] == "failure" for item in reviewed)
    summary = {"provider": model.provider, "model": model.model, "cases": len(reviewed), "failures": failures,
               "drafted": sum(item["result"] == "draft" for item in reviewed),
               "abstained": sum(item["result"] == "abstain" for item in reviewed),
               "median_seconds": round(statistics.median(latencies), 2),
               "p95_seconds": round(statistics.quantiles(latencies, n=20, method="inclusive")[-1], 2)}
    print(json.dumps(summary))
    if args.review_file:
        args.review_file.write_text(json.dumps(reviewed, indent=2), encoding="utf-8")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
