---
name: add-config-knob
description: Use when adding or changing an environment variable for the vault — wiring it through the composition root, compose file and both reference documents so it is not half-plumbed
---

# Add a configuration knob

Every service in this app is built in exactly one place: `lifespan` in
`app/main.py`. Services read their configuration from constructor keyword
arguments and never from `os.getenv` themselves — that is what makes them
testable with fakes. Keep it that way.

There are five edits, and skipping any one of them leaves a knob that looks
supported and is not.

## 1. Parse it in the composition root

In `lifespan` (`app/main.py`), next to the other reads for that service. Use
the existing helpers rather than a bare `os.getenv`:

- `_required_env("NAME")` — raises on missing/blank, for things the app cannot
  start without.
- `_optional_env("NAME")` — `None` for missing *or* blank, so an empty
  `.env` line means "unset".
- `_parse_int_or_str(value)` — channel ids: a `-100…` numeric id becomes an
  int, a `@username` stays a string.
- `_parse_bool(value, default=False)` — accepts `1/true/yes/on`.

Sizes are converted at the boundary, not inside the service: compare
`RECOVERY_MIN_FREE_GB` (GiB in, bytes to the constructor) and
`BROWSE_MAX_VIDEO_MB` (MB in, bytes computed in `PhotoWorker.__init__`).

## 2. Pass it as a constructor keyword argument

Add a keyword-only parameter with a default to the service
(`MegaService`, `TelegramService`, `SFTPService`, `PhotoWorker`,
`RecoveryService`) and store it on `self`. The default in the signature is what
the tests get; the default in `os.getenv(...)` is what production gets — keep
the two consistent or you will debug a difference that only exists in one of
them.

## 3. Add it to `docker-compose.yml`

Under `services.telegram-photo-vault.environment`, in the same style:

```yaml
      MY_NEW_KNOB: ${MY_NEW_KNOB:-default}
```

Values that are fixed by the container layout (`DATABASE_URL`,
`DATA_VOLUME_PATH`, the `/data` roots) are hardcoded there rather than
templated. Secrets come from `.env` via `env_file` — never write a literal
token, channel id or host into the compose file.

## 4. Document it in **both** places

- `AGENTS.md` → "Required Environment Variables" or "Optional Environment
  Variables", with its default in parentheses.
- `docs/REFERENCE.md` → the `## Environment variables` table.

These are hand-written and kept current; add your line, don't restructure them.

## 5. Test the decision, not the plumbing

A knob that only gets passed through needs no test. A knob that *changes a
decision* does — write it against the constructor argument, never against the
environment:

```python
# tests/test_recovery_rules.py — the free-space floor
def test_the_free_space_floor_blocks_a_download_that_would_breach_it(tmp_path):
    service = _service(tmp_path, min_free_bytes=10**18)
    assert service._space_for(1) is False
```

Then:

```bash
source .venv/bin/activate
pytest -q
ruff check .
python -m compileall -q app scripts
```

## What not to do

- Don't read `os.getenv` inside a service, a route or the worker. `API_KEY` and
  `DATA_VOLUME_PATH` in `app/api/routes.py` are the two deliberate exceptions
  (they are request-scoped, not construction-scoped).
- Don't add a knob that changes an on-channel format — chunk names, captions
  and the manifest are a contract with files already uploaded.
- Local venv is Python 3.14, CI is 3.11: no syntax or stdlib API newer than
  3.11.
