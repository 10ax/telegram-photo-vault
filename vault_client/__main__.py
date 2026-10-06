"""CLI entry point: python -m vault_client."""
from __future__ import annotations

import argparse
import json
import sys

from vault_client.api import ApiError, VaultApi
from vault_client.config import ConfigError, load_config
from vault_client.pipeline import PreconditionError, run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vault_client", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--hash", action="store_true", help="whole-file SHA-256 for tier A")
    parser.add_argument("--roots", help="comma-separated roots")
    parser.add_argument("--device-id", dest="device_id")
    parser.add_argument("--server")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, config_loader=load_config, api_factory=VaultApi) -> int:
    args = parse_args(argv)
    overrides = {name: getattr(args, name) for name in ("server", "device_id", "roots")
                 if getattr(args, name)}
    try:
        config = config_loader(overrides=overrides)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    api = api_factory(config.server, config.api_key)
    out = (lambda *args, **kwargs: None) if args.json else print
    try:
        result = run(config, api=api, dry_run=args.dry_run, yes=args.yes,
                     hash_files=args.hash, out=out)
    except (PreconditionError, ApiError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps({"deleted": result.deleted, "summary": result.summary}, default=str))
    return 0
