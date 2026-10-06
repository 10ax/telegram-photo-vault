"""Resolve client settings from flags, environment variables and an env file."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_ENV_FILE = Path.home() / ".config" / "vault-client" / "env"
DEFAULT_ROOTS = "/sdcard/DCIM,/sdcard/Pictures"
DEFAULT_REPORT_DIR = str(Path.home() / "storage" / "shared" / "vault-client-reports")
DEFAULT_CHUNK_SIZE = 2000

_KEY_MAP = {
    "server": "VAULT_SERVER",
    "device_id": "VAULT_DEVICE_ID",
    "api_key": "VAULT_API_KEY",
    "roots": "VAULT_ROOTS",
    "report_dir": "VAULT_REPORT_DIR",
    "chunk_size": "VAULT_CHUNK_SIZE",
}


class ConfigError(RuntimeError):
    """A required setting is missing, or a value cannot be used."""


@dataclass(frozen=True)
class Config:
    server: str
    device_id: str
    api_key: str
    roots: tuple[Path, ...]
    report_dir: Path
    chunk_size: int = DEFAULT_CHUNK_SIZE


def parse_env_file(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def load_config(
    *,
    env_file: Path | None = None,
    env_file_text: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> Config:
    if env_file_text is None:
        path = env_file if env_file is not None else DEFAULT_ENV_FILE
        env_file_text = path.read_text() if path.exists() else ""
    file_values = parse_env_file(env_file_text)
    env_values = {k: v for k, v in (env or {}).items() if v}
    override_values = {k: v for k, v in (overrides or {}).items() if v}
    sources = (override_values, env_values, file_values)

    def value(flag: str) -> str | None:
        canonical = _KEY_MAP[flag]
        for source in sources:
            found = source.get(flag) or source.get(canonical)
            if found:
                return found
        return None

    server, device_id, api_key = value("server"), value("device_id"), value("api_key")
    missing = [n for n, v in (("server", server), ("device_id", device_id), ("api_key", api_key)) if not v]
    if missing:
        raise ConfigError(f"missing required setting(s): {', '.join(missing)}")

    roots = tuple(Path(p.strip()) for p in (value("roots") or DEFAULT_ROOTS).split(",") if p.strip())
    return Config(
        server=server, device_id=device_id, api_key=api_key,
        roots=roots, report_dir=Path(value("report_dir") or DEFAULT_REPORT_DIR),
        chunk_size=int(value("chunk_size") or DEFAULT_CHUNK_SIZE),
    )
