"""Summarise a run and persist it."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_VERDICTS = ("ARCHIVED", "IN_FLIGHT", "AMBIGUOUS", "NOT_ARCHIVED")


def summarize(verdicts: list[dict]) -> dict:
    summary = {name: {"files": 0, "bytes": 0} for name in _VERDICTS}
    summary["TOTAL"] = {"files": 0, "bytes": 0}
    for verdict in verdicts:
        size = int(verdict.get("size") or 0)
        bucket = summary.setdefault(verdict.get("verdict", "NOT_ARCHIVED"), {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
        summary["TOTAL"]["files"] += 1
        summary["TOTAL"]["bytes"] += size
    return summary


def render_human(result) -> str:
    lines = [f"deletable: {len(result.deletable)}", f"deleted: {len(result.deleted)}",
             f"skipped: {len(result.skipped)}"]
    lines.extend(f"  {v.get('verdict', ''):8} {v.get('relpath', '')}" for v in result.deletable)
    return "\n".join(lines)


def write_report(report_dir, payload: dict, *, now: datetime | None = None) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")
    path = report_dir / f"{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
