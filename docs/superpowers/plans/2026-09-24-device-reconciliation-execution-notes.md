# Device reconciliation — execution notes

What was decided while executing `2026-09-24-device-reconciliation.md` (and Tasks 1-3 of the
catalog plan it depends on) that is not derivable from the diff. Kept because the reasoning
behind a safety rule outlives the commit that introduced it.

Branch `device-reconciliation`, 19 commits from `6b0ad65`. Suite 221 → 331.

## Defects found in the spec and plan during implementation

Six, all mine, all caught by an implementer or a reviewer rather than by me.

1. **`MANIFEST_KIND` was invented.** The plan specified `"chunked-file-manifest"`; this repo writes
   `"telegram-photo-vault/chunked-file"` (`app/services/chunking.py`). Followed verbatim,
   `resolve_manifests` would have rejected every real manifest, recorded `enrich_error` and never
   retried — leaving every file above 2 GB permanently invisible to the lookup, with the task's own
   tests green because the plan's fixture repeated the same wrong literal. `catalog.py` now imports
   the constant rather than restating it, so a copy cannot drift again.

2. **A hash was treated only as a promotion, never as a veto.** A candidate whose known `sha256`
   *contradicted* the local file stayed eligible for `ARCHIVED` on a name-and-size match, so the
   server could authorise deleting a photograph while holding proof that the archived bytes differ.
   Reachable: `chunked_sha256` is always set on a resolved manifest, and `CatalogItem.sha256` comes
   from `photos` and from the backup script's state DB. **Disproof now outranks metadata
   inference** — a contradicted candidate is disqualified from every tier below `HASH`, and the
   entry is reported `AMBIGUOUS` / `hash_mismatch` with the message id, because same name, same
   size, different content is exactly what a person must look at.

3. **`/api/vault/verify` omitted the size equality the spec requires.** The spec's tier A- reads
   "equal fingerprints plus equal size"; the endpoint returned `match: true` on head-and-tail
   agreement alone. Since the documented way out of `AMBIGUOUS` is verify-then-delete, and the
   commonest reason for `AMBIGUOUS` is `size_mismatch`, a client author reading only the contract
   would have deleted on weaker evidence than the design requires. The server now reads the
   archived file's own size, refuses a match without it, and publishes it.

4. **`vault_lookup` could never return `ARCHIVED`** — it built its entry with `mtime: None`, and a
   missing `mtime` fails closed, while the plan's own test asserted the opposite.

5. **`taken_at` was accepted and never persisted.**

6. **A test was told to import three classes it never referenced**, which `ruff` would have failed
   as F401.

## Rulings

Recorded because each one is a decision a person might reasonably have made differently.

- **A contradicting hash disqualifies rather than merely failing to promote**, and reports
  `hash_mismatch`. Cost if wrong: a file whose hash the client computed incorrectly is kept rather
  than deleted — the harmless direction.
- **The freshness rule demotes `NAME_SIZE` matches only**, never `HASH` or `FINGERPRINT`. A
  name-and-size match is an inference a stale catalog undermines; a content hash is proof that
  dates cannot undermine. Being conservative about proof would cost deletions the owner is
  entitled to and buy nothing.
- **A lookup verdict is informational and never authorises a deletion.** `GET /api/vault/lookup`
  has the freshness gate off by design; the exemption is an explicit `freshness_gate` parameter
  rather than a substituted timestamp, the response carries `freshness_gate: false`, and
  `docs/REFERENCE.md` says only `POST /api/devices/{id}/reconcile` may authorise a deletion.
- **A pipeline veto is never shadowed by corroboration.** `mega-ls -R` is recursive, so two folders
  can hold one leaf name; `_pipeline_statuses` orders by `mega_path` and lets a non-`COMPLETED`
  status displace a stored `COMPLETED` one.
- **The freshness frontier is the oldest archive channel, not the newest.** A freshly scanned
  channel must not vouch for a stale one. A configured archive channel with no rows makes the
  whole value `None`, so everything fails closed.
- **`decide` compares `PhotoStatus` with `==`, never `is`.** It is a `str`-Enum, so `in` matched raw
  strings while `is` did not — an asymmetry that failed open on exactly the two vetoes.
- **Provenance matching is scoped per channel.** Telegram message ids are per-chat and every
  channel's start at 1, so an unscoped match mis-attributes rows systematically.
- **`DeletionRecord.tier` is a `MatchTier`, so a bad value is a 422.** As a `str` it raised inside
  the loop, producing a 500 with the commit never reached — losing the audit for the whole batch at
  the moment the client had already deleted those files locally.
- **`POST /api/catalog/scan` and `/catalog/resolve-manifests` live in this plan**, though the scan
  belongs to the catalog plan's Task 5. Without them nothing could populate the catalog, `evaluate`
  would raise `CatalogNeverScanned` forever, and the feature would ship inert. When the catalog's
  remaining tasks land, keep their version and drop this copy.
- **`catalog_items` gets a `_COLUMN_MIGRATIONS` entry** despite the catalog plan saying that dict is
  never touched: that rule was written for a table `create_all` makes whole, and these columns are
  added to one that may already exist on disk.

## Known limitation, parked deliberately

**A dormant archive channel pins the freshness frontier forever.** The frontier is
`min(max(message_date))` per archive channel. `IPHONE_CHANNEL_ID` is a one-off migration channel
whose newest message date never advances, so once it is configured every local file newer than the
migration date is demoted to `IN_FLIGHT` permanently and tier `NAME_SIZE` becomes unreachable.

Inert today — only one archive channel and one mirror are configured, and with a single archive
channel `min` equals `max`. The direction is fail-closed, so it costs deletions rather than files.

**The correct fix is to track when each channel was last *scanned* and use that as the frontier.**
A dormant channel scanned yesterday is fresh even though its newest message is old. Worth doing
before `IPHONE_CHANNEL_ID` is set, because the symptom is that the feature quietly stops
authorising any deletion at all.

## Smaller things left undone

Each was raised by a review and deferred on purpose: `parse_manifest` accepts `bool` for
`total_size` and its docstring overstates which exception it raises; `resolve_manifests` opens one
session per row and is still synchronous in its handler (~100 s at `limit=50`); `match_backup_db`
does a blocking `sqlite3` read inside an `async def` and its `sha256` fallback is channel-agnostic;
`MANIFEST_SUFFIX` is a third copy of an on-channel constant; a naive `mtime` is read as UTC rather
than rejected; `_pipeline_statuses` scans the whole `photos` table per call; a negative size is
reported as `zero_byte_file`; row order among duplicate candidates is unspecified; the snapshot
counters are an unlocked read-modify-write, documented as non-idempotent rather than fixed;
`latest_snapshot` omits three byte fields; `app/api/routes.py` has grown to hosting two subsystems
and wants splitting when the catalog's remaining tasks land.

Four safety-relevant wirings now live only in `lifespan`, which this repo documents as untested:
`worker_channel_id`, `archive_channel_ids`, `backup_state_db` and `catalog.shutdown()`. Each is
covered at the service level, none through the composition root.
