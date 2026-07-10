# Shared family Telegram drive (teldrive)

A self-hosted [teldrive](https://github.com/tgdrive/teldrive) instance that turns a
Telegram channel into a **shared cloud drive** both of you can access (web UI or an
`rclone` mount), with Telegram as the storage backend. This is the *drive* half of
the setup; for browsing/searching photos **inside the Telegram app** by `#date`
hashtags, use the vault's `BROWSE_CHANNEL_ID` (see the repo's `AGENTS.md`).

> **What teldrive is / isn't.** It's Dropbox-over-Telegram: full-fidelity files in a
> folder tree, with all metadata in **Postgres**. It is *not* a photo gallery — no
> timeline, faces, albums, Live-Photo pairing, or content de-dup, and HEIC won't
> thumbnail in a desktop browser (renders fine in iOS Safari).

## Prerequisites

- Docker + Docker Compose on the ODROID (or any always-on box).
- A Telegram account (yours). Optionally your own app id/hash from
  <https://my.telegram.org>.
- `rclone` (for seeding the iPhone folder) and optionally `jdupes` (local de-dup).

## 1. Configure

```bash
cd deploy/teldrive
cp .env.example .env
cp config.toml.example config.toml     # your filled copy is gitignored

# Postgres password (paste the SAME value into config.toml's data-source):
openssl rand -hex 24     # -> POSTGRES_PASSWORD in .env AND config.toml
openssl rand -hex 32     # -> __JWT_SECRET__ in config.toml
```

Edit **`config.toml`**: replace `__POSTGRES_PASSWORD__`, `__JWT_SECRET__`, and set
`allowed-users` to **both** partners' Telegram usernames (no `@`).

## 2. Bring it up

```bash
docker compose up -d
docker compose logs -f teldrive     # watch it come up
```

Open `http://<odroid-ip>:8080` and **log in with your Telegram account**. teldrive
stores the session in Postgres and auto-creates a storage channel.

**Both of you:** each partner listed in `allowed-users` logs in with their own
Telegram account and sees the same shared drive (backed by the primary account's
channel). *(teldrive's multi-user semantics evolve — confirm against current docs.)*

## 3. Go faster with multiple bots (beats the non-premium throttle)

Non-premium Telegram accounts are speed-throttled, and the *only* way around it is
**parallelism across accounts**. In teldrive's UI, add bot tokens (create up to ~5
via [@BotFather](https://t.me/BotFather)); teldrive adds them as channel admins and
round-robins uploads/downloads across them. Then use parallel `--transfers`.

## 4. Seed the iPhone backup folder

Configure an rclone remote of type **teldrive** (server URL + an access token you
copy from the teldrive UI — see the [rclone guide](https://teldrive-docs.pages.dev/docs/guides/rclone)):

```bash
rclone config      # new remote, type: teldrive, url: http://<odroid-ip>:8080
```

Then:

```bash
./seed-iphone-backup.sh "~/Pictures/iPhone backup" "teldrive:Family/Wife-iPhone"
```

The script de-dupes locally (jdupes), then uploads resumably with parallel
transfers matched to your bot count.

## 5. Back up the database (do not skip)

The Postgres DB is the **map** to your files — lose it (and its backups) and the
channel is a pile of unlabeled chunks. Cron the included script:

```cron
0 3 * * * /path/to/deploy/teldrive/backup-postgres.sh >> /var/log/teldrive-backup.log 2>&1
```

It writes `backups/teldrive_<ts>.sql.gz` and keeps the newest 14. Restore command is
documented at the bottom of `backup-postgres.sh`. Copy the dumps offsite too.

## iPhone gotchas

- **HEIC**: previews on your wife's iPhone (Safari), not in a desktop browser.
- **Live Photos**: land as separate `.HEIC` + `.MOV` (a drive won't pair them).
- **No content de-dup**: hence the `jdupes` pass before uploading.
- **Encryption key** (if set in `config.toml`): back it up — it's unrecoverable.
