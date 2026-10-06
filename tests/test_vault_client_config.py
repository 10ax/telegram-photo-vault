"""Config precedence: overrides > env vars > env file > defaults."""
from pathlib import Path

import pytest

from vault_client.config import ConfigError, load_config, parse_env_file

ENV_FILE = """\
# a comment
VAULT_SERVER=http://atlas.tail57bbb.ts.net:8000
VAULT_DEVICE_ID=pixel

VAULT_API_KEY=secret-from-file
"""


def test_parse_env_file_ignores_comments_and_blanks():
    assert parse_env_file(ENV_FILE) == {
        "VAULT_SERVER": "http://atlas.tail57bbb.ts.net:8000",
        "VAULT_DEVICE_ID": "pixel",
        "VAULT_API_KEY": "secret-from-file",
    }


def test_load_config_requires_server_device_and_key():
    with pytest.raises(ConfigError):
        load_config(env={}, overrides={}, env_file_text="")


def test_defaults_fill_roots_and_chunk_size():
    cfg = load_config(env={}, overrides={}, env_file_text=ENV_FILE)
    assert cfg.server == "http://atlas.tail57bbb.ts.net:8000"
    assert cfg.device_id == "pixel"
    assert cfg.api_key == "secret-from-file"
    assert cfg.roots == (Path("/sdcard/DCIM"), Path("/sdcard/Pictures"))
    assert cfg.chunk_size == 2000


def test_a_non_numeric_chunk_size_is_a_config_error():
    with pytest.raises(ConfigError):
        load_config(env={"VAULT_CHUNK_SIZE": "lots"}, overrides={}, env_file_text=ENV_FILE)


def test_env_beats_file_and_overrides_beat_both():
    cfg = load_config(
        env={"VAULT_SERVER": "http://from-env:8000", "VAULT_API_KEY": "env-key"},
        overrides={"server": "http://from-flag:8000", "roots": "/sdcard/DCIM/Camera"},
        env_file_text=ENV_FILE,
    )
    assert cfg.server == "http://from-flag:8000"
    assert cfg.api_key == "env-key"
    assert cfg.device_id == "pixel"
    assert cfg.roots == (Path("/sdcard/DCIM/Camera"),)
