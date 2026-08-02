"""Read-only coverage and health report for the crawler source catalog."""
import argparse
import glob
import json
import math
import os
import time
from collections import Counter, defaultdict

import yaml

import hybrid_hunter as hh
import probe


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _latest_health(state_paths):
    health = {}
    failures = {}
    pending = 0
    for path in state_paths:
        state = _load_json(path)
        pending += len(state.get("_pending") or [])
        for name, entry in (state.get("_failures") or {}).items():
            current = failures.get(name)
            if not current or entry.get("count", 0) > current.get("count", 0):
                failures[name] = entry
        for name, entry in (state.get("_health") or {}).items():
            current = health.get(name)
            if not current or entry.get("last_run", "") > current.get("last_run", ""):
                health[name] = entry
    return health, failures, pending


def build_report(config_path="config.yaml", candidates_path="candidates.yaml",
                 state_paths=None, stale_days=7, zero_streak=24):
    with open(config_path, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    problems = hh.validate_config(config)
    if problems:
        raise ValueError("invalid config: " + "; ".join(problems))

    ats_entries = config.get("ats_companies") or []
    custom_entries = config.get("custom_pages") or []
    entries = ats_entries + custom_entries
    if state_paths is None:
        state_paths = sorted(set(
            ([hh.STATE_FILE] if os.path.exists(hh.STATE_FILE) else [])
            + glob.glob("state.shard-*-of-*.json")))
    health, failures, pending = _latest_health(state_paths)

    candidates = probe.load_candidates(candidates_path) if os.path.exists(candidates_path) else []
    candidate_statuses = Counter(row.get("status", "candidate") for row in candidates)
    active_names = {entry["name"].casefold() for entry in entries}
    candidate_active = sum(
        row["name"].casefold() in active_names or row.get("status") == "active"
        for row in candidates)

    now = time.time()
    stale_cutoff = now - stale_days * 86400
    no_health = []
    stale = []
    suspicious_zero = []
    durations = []
    requests = 0
    family_health = defaultdict(lambda: {"sources": 0, "healthy": 0, "failing": 0})
    ats_by_name = {entry["name"]: entry.get("ats", "custom_page") for entry in entries}
    for entry in entries:
        name = entry["name"]
        family = ats_by_name[name]
        family_health[family]["sources"] += 1
        item = health.get(name)
        if not item:
            no_health.append(name)
        else:
            last_success = hh._parse_ts(item.get("last_success"))
            if not last_success or last_success < stale_cutoff:
                stale.append(name)
            if item.get("successful_zero_streak", 0) >= zero_streak:
                suspicious_zero.append(name)
            durations.append(item.get("duration_ms", 0))
            requests += item.get("request_count", 0)
        if name in failures:
            family_health[family]["failing"] += 1
        else:
            family_health[family]["healthy"] += 1

    durations.sort()
    p95 = durations[max(0, math.ceil(len(durations) * 0.95) - 1)] if durations else None
    structured_ratio = (len(ats_entries) / len(entries) * 100) if entries else 100.0
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "coverage": {
            "active_sources": len(entries),
            "ats_sources": len(ats_entries),
            "custom_pages": len(custom_entries),
            "structured_ratio_percent": round(structured_ratio, 1),
            "ats_families": dict(Counter(
                entry["ats"] for entry in ats_entries).most_common()),
        },
        "candidates": {
            "total": len(candidates),
            "statuses": dict(sorted(candidate_statuses.items())),
            "active_conversion_count": candidate_active,
            "active_conversion_percent": (
                round(candidate_active / len(candidates) * 100, 1) if candidates else 0.0),
        },
        "health": {
            "state_files": state_paths,
            "sources_with_health": len(health),
            "failing_sources": sorted(failures),
            "pending_alerts": pending,
            "without_runtime_evidence": sorted(no_health),
            "stale_sources": sorted(stale),
            "suspicious_zero_sources": sorted(suspicious_zero),
            "latest_run_request_count": requests,
            "p95_source_duration_ms": p95,
            "by_source_type": dict(sorted(family_health.items())),
        },
    }


def print_markdown(report):
    coverage = report["coverage"]
    candidates = report["candidates"]
    health = report["health"]
    print("# Source catalog report")
    print()
    print(f"- Active: {coverage['active_sources']} "
          f"({coverage['ats_sources']} ATS, {coverage['custom_pages']} custom)")
    print(f"- Structured coverage: {coverage['structured_ratio_percent']}%")
    print(f"- Candidates: {candidates['total']} "
          f"({candidates['active_conversion_percent']}% active conversion)")
    print(f"- Current failures: {len(health['failing_sources'])}")
    print(f"- Pending alerts: {health['pending_alerts']}")
    print(f"- Sources without runtime evidence: {len(health['without_runtime_evidence'])}")
    print(f"- Stale sources: {len(health['stale_sources'])}")
    print(f"- Suspicious successful-zero streaks: {len(health['suspicious_zero_sources'])}")
    print(f"- Latest-run requests represented: {health['latest_run_request_count']}")
    print(f"- p95 source duration: {health['p95_source_duration_ms']} ms")
    print()
    print("## ATS families")
    print()
    for family, count in coverage["ats_families"].items():
        status = health["by_source_type"][family]
        print(f"- {family}: {count} source(s), {status['failing']} failing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--candidates", default="candidates.yaml")
    parser.add_argument("--state", action="append", dest="states",
                        help="State file to include; repeat for multiple shards")
    parser.add_argument("--stale-days", type=int, default=7)
    parser.add_argument("--zero-streak", type=int, default=24)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    args = parser.parse_args()
    if args.stale_days < 1 or args.zero_streak < 1:
        parser.error("--stale-days and --zero-streak must be positive")
    try:
        report = build_report(
            args.config, args.candidates, args.states,
            stale_days=args.stale_days, zero_streak=args.zero_streak)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_markdown(report)


if __name__ == "__main__":
    main()
