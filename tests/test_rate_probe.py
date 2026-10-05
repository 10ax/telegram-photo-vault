"""The rate-probe summariser: pure arithmetic, no network.

The probe itself talks to Telegram; only its reducer is worth a test, and it is
worth one because the whole result of a run is this one dict.
"""
from scripts.probe_rate_limit import parse_channel_id, summarize


def test_a_numeric_channel_id_becomes_an_int_but_a_username_stays_a_string():
    """A numeric id handed to pyrogram as a str is treated as a phone number
    (contacts.ResolvePhone) and the probe dies before its first read."""
    assert parse_channel_id("-1002637897512") == -1002637897512
    assert parse_channel_id("@somechannel") == "@somechannel"


def test_summarize_counts_floods_and_reports_success_latency_percentiles():
    records = [
        {"latency_s": 0.10, "flood_seconds": None},
        {"latency_s": 0.20, "flood_seconds": 11},
        {"latency_s": 0.30, "flood_seconds": None},
        {"latency_s": 0.40, "flood_seconds": 12},
    ]

    result = summarize(records, elapsed_seconds=60.0)

    assert result["requests"] == 4
    assert result["floods"] == 2
    assert result["flood_waits"] == [11, 12]
    assert result["max_flood_seconds"] == 12
    assert result["per_minute"] == 4.0
    # Percentiles are over successful reads only; a flood is not a latency.
    assert result["latency_p50_s"] == 0.1
    assert result["latency_p95_s"] == 0.3


def test_summarize_of_nothing_is_zeroes_not_an_error():
    result = summarize([], elapsed_seconds=0.0)

    assert result["requests"] == 0
    assert result["floods"] == 0
    assert result["flood_waits"] == []
    assert result["max_flood_seconds"] == 0
    assert result["latency_p50_s"] is None
    assert result["per_minute"] == 0.0
