"""One cleanup run: inventory -> verdicts -> verify -> review -> delete."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vault_client import report
from vault_client.api import ApiError
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


def _finish(config, result, out) -> RunResult:
    result.summary = report.summarize(result.verdicts)
    payload = {"deletable": result.deletable, "deleted": result.deleted,
               "skipped": result.skipped, "summary": result.summary}
    try:
        report.write_report(config.report_dir, payload)
    except OSError as exc:
        out(f"warning: could not write report: {exc}")
    out(report.render_human(result))
    return result


def _promote_ambiguous(api, verdicts, entries_by_relpath, window, sdcard, skipped) -> list[dict]:
    promoted = []
    for verdict in verdicts:
        entry = entries_by_relpath.get(verdict.get("relpath"))
        if entry is None or verdict.get("tg_message_id") is None or verdict.get("channel_id") is None:
            continue
        head, tail = head_tail_sha256(sdcard / entry.relpath, window)
        try:
            body = api.verify(channel_id=verdict["channel_id"], tg_message_id=verdict["tg_message_id"],
                              file_size=entry.size, head_sha256=head, tail_sha256=tail)
        except ApiError as exc:
            skipped.append({"relpath": entry.relpath, "reason": f"verify_failed: {exc.detail}"})
            continue
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
    _warn_if_frontier_stale(freshness, now, out)
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
    for verdict in result.verdicts:
        entry = entries_by_relpath.get(verdict.get("relpath"))
        if entry is not None:
            verdict["size"] = entry.size
    parts = partition(result.verdicts)
    window = int(freshness.get("fingerprint_window_bytes") or 0)
    result.deletable = parts["ARCHIVED"] + _promote_ambiguous(
        api, parts["AMBIGUOUS"], entries_by_relpath, window, sdcard, result.skipped
    )

    if not result.deletable:
        out("Nothing is safe to delete.")
        return _finish(config, result, out)

    total = sum(entries_by_relpath[v["relpath"]].size for v in result.deletable)
    out(f"{len(result.deletable)} file(s), {total} bytes reclaimable.")
    if dry_run:
        out("dry run: nothing deleted.")
        return _finish(config, result, out)
    if not (yes or confirm(f"Delete {len(result.deletable)} file(s)?")):
        out("aborted.")
        return _finish(config, result, out)

    try:
        _delete_confirmed(result, entries_by_relpath, sdcard, refresh or _refresh_media_store, out)
    except BaseException:
        # An interrupt or error mid-delete must not lose the audit for what
        # already went; write it, then let the exception propagate.
        _audit_and_report(config, result, api, out)
        raise
    out(f"deleted {len(result.deleted)} file(s); skipped {len(result.skipped)}.")
    return _audit_and_report(config, result, api, out)


def _warn_if_frontier_stale(freshness, now, out) -> None:
    frontier = freshness.get("frontier")
    if not frontier:
        return
    try:
        frontier_dt = datetime.fromisoformat(frontier)
    except (TypeError, ValueError):
        return
    reference = now or datetime.now(timezone.utc)
    if frontier_dt < reference:
        out(f"warning: catalog frontier {frontier} is in the past; recent files will be held IN_FLIGHT")


def _audit_and_report(config, result, api, out) -> RunResult:
    if result.deleted:
        try:
            api.deletions(config.device_id, result.deleted)
        except Exception as exc:  # the bytes are already gone; the audit is best-effort
            out(f"warning: could not record deletion audit: {exc}")
    return _finish(config, result, out)


def _unchanged(path, entry: Entry) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return (stat.st_size == entry.size
            and datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) == entry.mtime)


def _refresh_media_store(paths, *, runner=subprocess.run) -> None:
    """Best-effort: tell Android the deleted paths are gone, once per directory."""
    for directory in sorted({str(Path(p).parent) for p in paths}):
        try:
            runner(["termux-media-scan", "-r", directory], check=False, capture_output=True)
        except Exception:
            pass


def _delete_confirmed(result, entries_by_relpath, sdcard, refresh, out) -> None:
    for verdict in result.deletable:
        entry = entries_by_relpath.get(verdict.get("relpath"))
        if entry is None:
            result.skipped.append({"relpath": verdict.get("relpath"), "reason": "not_enumerated"})
            continue
        path = sdcard / entry.relpath
        try:
            if not _unchanged(path, entry):
                result.skipped.append({"relpath": entry.relpath, "reason": "changed_or_missing"})
                out(f"skipped (changed or already gone): {entry.relpath}")
                continue
            path.unlink()
        except FileNotFoundError:
            result.skipped.append({"relpath": entry.relpath, "reason": "already_gone"})
            continue
        except OSError as exc:
            result.skipped.append({"relpath": entry.relpath, "reason": f"io_error: {exc}"})
            out(f"skipped (io error): {entry.relpath}: {exc}")
            continue
        result.deleted.append({
            "relpath": entry.relpath, "name": entry.name, "size": entry.size,
            "tier": verdict.get("tier"), "channel_id": verdict.get("channel_id"),
            "tg_message_id": verdict.get("tg_message_id"),
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        })
    refresh([str(sdcard / d["relpath"]) for d in result.deleted])


def _input_confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
