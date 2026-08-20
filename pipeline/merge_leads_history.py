#!/usr/bin/env python3
"""Merge a fresh rolling-window leads build into the historical dashboard.

Fresh rows win on the same stable document identity so lifecycle/enrichment updates
are retained. Baseline-only rows are preserved so a short source window cannot
silently delete historical leads or attended Pima portal records.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def stable_key(row: dict) -> tuple:
    doc_number = str(row.get("doc_number") or "").strip()
    if doc_number:
        return (row.get("county"), row.get("source"), doc_number)
    names = tuple(str(x).strip() for x in (row.get("names") or []) if str(x).strip())
    return (
        row.get("county"), row.get("source"), row.get("doc_type"),
        row.get("recorded_date"), row.get("apn_norm") or row.get("apn"),
        names, row.get("site_address"),
    )


def load(path: Path) -> dict:
    with path.open() as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("leads"), list):
        raise SystemExit(f"invalid leads payload: {path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--fresh", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    baseline = load(args.baseline)
    fresh = load(args.fresh)
    old_rows = baseline["leads"]
    fresh_rows = fresh["leads"]

    merged = {stable_key(row): row for row in old_rows}
    old_keys = set(merged)
    for row in fresh_rows:
        merged[stable_key(row)] = row
    rows = list(merged.values())
    rows.sort(key=lambda row: (row.get("recorded_date") or "", row.get("doc_number") or ""), reverse=True)

    if len(rows) < len(old_rows):
        raise SystemExit(f"history merge reduced lead count: baseline={len(old_rows)} merged={len(rows)}")

    out = dict(baseline)
    out.update({k: v for k, v in fresh.items() if k != "leads"})
    out["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    out["total_leads"] = len(rows)
    out["resolved"] = sum(1 for row in rows if row.get("resolved"))
    out["unresolved"] = len(rows) - out["resolved"]
    dates = [row.get("recorded_date") for row in rows if row.get("recorded_date")]
    out["date_range"] = {"min": min(dates) if dates else None, "max": max(dates) if dates else None}

    counts = defaultdict(Counter)
    categories = {}
    for row in rows:
        counts[row.get("county")][row.get("doc_type")] += 1
        categories[row.get("doc_type")] = row.get("category", "Other")
    out["doc_type_counts"] = {county: dict(values.most_common()) for county, values in counts.items()}
    out["doc_type_category"] = categories
    out["leads"] = rows

    args.output.write_text(json.dumps(out, separators=(",", ":")))
    result = {
        "baseline": len(old_rows), "fresh": len(fresh_rows), "overlap": len(old_keys & {stable_key(r) for r in fresh_rows}),
        "new": len({stable_key(r) for r in fresh_rows} - old_keys), "preserved": len(old_keys - {stable_key(r) for r in fresh_rows}),
        "merged": len(rows), "date_range": out["date_range"],
        "county_counts": dict(Counter(row.get("county") for row in rows)),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
