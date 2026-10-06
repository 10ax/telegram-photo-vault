"""One cleanup run: inventory -> verdicts -> verify -> review -> (Task 7) delete."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vault_client.enumerate import Entry, enumerate_entries, entry_to_manifest
from vault_client.hashing import full_sha256, head_tail_sha256


class PreconditionError(RuntimeError):
    """The run cannot start: unscanned catalog, missing root, etc."""


@dataclass
class RunResult:
    entries: list[Entry] = field(default_factory=list)
    verdicts: list[dict] = field(default_factory=list)
    deletable: list[dict] = field(default_factory=list)
    deleted: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    freshness: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)


def partition(verdicts: list[dict]) -> dict[str, list[dict]]:
    parts: dict[str, list[dict]] = {"ARCHIVED": [], "IN_FLIGHT": [], "AMBIGUOUS": [], "NOT_ARCHIVED": []}
    for verdict in verdicts:
        parts.setdefault(verdict.get("verdict", "NOT_ARCHIVED"), []).append(verdict)
    return parts


def _promote_ambiguous(api, verdicts, entries_by_relpath, window, sdcard) -> list[dict]:
    promoted = []
    for verdict in verdicts:
        entry = entries_by_relpath.get(verdict.get("relpath"))
        if entry is None or verdict.get("tg_message_id") is None or verdict.get("channel_id") is None:
            continue
        head, tail = head_tail_sha256(sdcard / entry.relpath, window)
        body = api.verify(channel_id=verdict["channel_id"], tg_message_id=verdict["tg_message_id"],
                          file_size=entry.size, head_sha256=head, tail_sha256=tail)
        if body.get("match"):
            promoted.append({**verdict, "verdict": "ARCHIVED", "tier": "FINGERPRINT"})
    return promoted


def run(config, *, api, sdcard=None, dry_run=False, yes=False, confirm=None,
        enumerate_fn=enumerate_entries, hash_files=False, refresh=None, now=None, out=print) -> RunResult:
    confirm = confirm or _input_confirm
    sdcard = Path(sdcard) if sdcard is not None else Path("/sdcard")

    freshness = api.freshness()
    if not freshness.get("archive_rows"):
        raise PreconditionError("catalog has never been scanned; run a rescan first")

    result = RunResult(freshness=freshness)
    result.entries = enumerate_fn(config.roots, sdcard=sdcard)
    entries_by_relpath = {e.relpath: e for e in result.entries}
    manifests = [
        entry_to_manifest(e, full_sha256(sdcard / e.relpath) if hash_files else None)
        for e in result.entries
    ]

    result.verdicts = api.reconcile_all(
        config.device_id, manifests, chunk_size=config.chunk_size,
        taken_at=(now or datetime.now(timezone.utc)).isoformat(),
    )
    parts = partition(result.verdicts)
    window = int(freshness.get("fingerprint_window_bytes") or 0)
    result.deletable = parts["ARCHIVED"] + _promote_ambiguous(
        api, parts["AMBIGUOUS"], entries_by_relpath, window, sdcard
    )

    if not result.deletable:
        out("Nothing is safe to delete.")
        return result

    total = sum(entries_by_relpath[v["relpath"]].size for v in result.deletable)
    out(f"{len(result.deletable)} file(s), {total} bytes reclaimable.")
    if dry_run:
        out("dry run: nothing deleted.")
        return result
    if not (yes or confirm(f"Delete {len(result.deletable)} file(s)?")):
        out("aborted.")
        return result
    # Task 7 replaces this line with the guarded delete + audit.
    return result


def _input_confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
