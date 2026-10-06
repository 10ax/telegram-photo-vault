# vault_client

Phone-side cleanup client for the Telegram photo vault. It enumerates local
photos/videos, asks the server which are already archived, and deletes only the
ones the server calls `ARCHIVED` — after you confirm.

## Phone setup (Termux)

1. Install Termux from the **GitHub/F-Droid build** (not Play) and
   `pkg install python git`.
2. Grant **All files access**: Android Settings → Apps → Special app access →
   All files access → Termux. `termux-setup-storage` alone is read-only on
   modern Android.
3. Reach the server over **Tailscale** (Android 17 blocks LAN by default).
4. `git clone` this repo and `cd telegram-photo-vault`.

## Configure

`~/.config/vault-client/env` (chmod 600):

    VAULT_SERVER=http://atlas.tail57bbb.ts.net:8000
    VAULT_DEVICE_ID=pixel
    VAULT_API_KEY=…

## Use

    python -m vault_client            # review, then confirm
    python -m vault_client --dry-run  # never deletes
    python -m vault_client --yes      # unattended after review
    python -m vault_client --hash     # whole-file hashes for tier A evidence

Reports land in `~/storage/shared/vault-client-reports/`. After deleting, it
runs `termux-media-scan` (install Termux:API for a clean gallery). Without
All files access, deletion fails with `EACCES` — re-grant it in Settings.
