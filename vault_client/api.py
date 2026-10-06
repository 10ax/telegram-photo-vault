"""Thin HTTP client for the vault API. Talks to the server only, never Telegram."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _default_transport(method, url, headers, body, timeout=30.0):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ApiError(0, f"server unreachable: {exc}") from exc


class VaultApi:
    def __init__(self, server: str, api_key: str, *, transport: Transport | None = None, timeout: float = 30.0) -> None:
        self.server = server.rstrip("/")
        self.api_key = api_key
        self._transport = transport or (lambda m, u, h, b: _default_transport(m, u, h, b, timeout))

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"X-Api-Key": self.api_key}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, f"{self.server}{path}", headers, body)
        if not 200 <= status < 300:
            try:
                detail = json.loads(raw).get("detail", raw.decode(errors="replace"))
            except Exception:
                detail = raw.decode(errors="replace")
            raise ApiError(status, str(detail))
        return json.loads(raw) if raw else {}

    def freshness(self) -> dict:
        return self.request("GET", "/api/catalog/freshness")

    def reconcile(self, device_id, entries, *, snapshot_id=None, taken_at=None, final=True) -> dict:
        return self.request("POST", f"/api/devices/{device_id}/reconcile", {
            "entries": list(entries), "snapshot_id": snapshot_id, "taken_at": taken_at, "final": final,
        })

    def verify(self, *, channel_id, tg_message_id, file_size, head_sha256, tail_sha256) -> dict:
        return self.request("POST", "/api/vault/verify", {
            "channel_id": channel_id, "tg_message_id": tg_message_id, "file_size": file_size,
            "head_sha256": head_sha256, "tail_sha256": tail_sha256,
        })

    def deletions(self, device_id, records) -> dict:
        return self.request("POST", f"/api/devices/{device_id}/deletions", {"deleted": list(records)})

    def reconcile_all(self, device_id, entries, *, chunk_size: int = 2000, taken_at=None) -> list[dict]:
        """Reconcile every entry, paginating with snapshot_id.

        A 413 refuses the batch before anything is recorded, so it is safe to
        split and retry against the same snapshot. Any other failure propagates:
        a retried reconcile would double-count the snapshot.
        """
        entries = list(entries)
        verdicts: list[dict] = []
        snapshot_id = None
        total_files = None
        index = 0
        size = max(1, chunk_size)
        while index < len(entries):
            chunk = entries[index:index + size]
            final = index + size >= len(entries)
            try:
                body = self.reconcile(device_id, chunk, snapshot_id=snapshot_id, taken_at=taken_at, final=final)
            except ApiError as exc:
                if exc.status == 413 and size > 1:
                    size = max(1, size // 2)
                    continue
                raise
            snapshot_id = body.get("snapshot_id", snapshot_id)
            summary = body.get("summary") or {}
            if summary.get("total_files") is not None:
                total_files = summary["total_files"]
            verdicts.extend(body.get("entries", []))
            index += len(chunk)
        if total_files is not None and total_files != len(entries):
            raise ApiError(500, f"reconcile summary mismatch: server reported {total_files} of {len(entries)}")
        return verdicts
