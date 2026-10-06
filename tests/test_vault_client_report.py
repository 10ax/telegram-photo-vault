import json
from datetime import datetime, timezone

from vault_client import report


def test_summarize_counts_and_bytes_per_verdict():
    verdicts = [
        {"verdict": "ARCHIVED", "size": 10},
        {"verdict": "ARCHIVED", "size": 5},
        {"verdict": "NOT_ARCHIVED", "size": 1},
    ]
    summary = report.summarize(verdicts)
    assert summary["ARCHIVED"] == {"files": 2, "bytes": 15}
    assert summary["NOT_ARCHIVED"] == {"files": 1, "bytes": 1}
    assert summary["TOTAL"] == {"files": 3, "bytes": 16}


def test_summarize_handles_unknown_and_missing_fields():
    summary = report.summarize([{"verdict": "WEIRD"}, {"size": None}, {}])
    assert summary["WEIRD"] == {"files": 1, "bytes": 0}
    assert summary["NOT_ARCHIVED"] == {"files": 2, "bytes": 0}
    assert summary["TOTAL"] == {"files": 3, "bytes": 0}


def test_write_report_creates_a_timestamped_json_file(tmp_path):
    path = report.write_report(tmp_path, {"deleted": 2},
                               now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
    assert path.parent == tmp_path
    assert path.name == "2026-10-06T120000Z.json"
    assert json.loads(path.read_text()) == {"deleted": 2}
