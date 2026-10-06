import json

from vault_client import __main__ as cli
from vault_client.api import ApiError
from vault_client.config import Config, ConfigError


def test_parse_args_defaults_and_overrides():
    args = cli.parse_args(["--dry-run", "--device-id", "pixel2", "--hash"])
    assert args.dry_run is True
    assert args.device_id == "pixel2"
    assert args.hash is True
    assert args.yes is False


def test_a_config_error_exits_2(capsys):
    def broken_loader(**kwargs):
        raise ConfigError("missing required setting(s): server")

    code = cli.main([], config_loader=broken_loader)

    assert code == 2
    assert "missing required setting" in capsys.readouterr().err


def _config(tmp_path) -> Config:
    return Config(server="http://x", device_id="pixel", api_key="k",
                  roots=(tmp_path / "DCIM",), report_dir=tmp_path)


def test_server_unreachable_exits_3_with_a_clear_message(tmp_path, capsys):
    class UnreachableApi:
        def __init__(self, server, api_key):
            pass

        def freshness(self):
            raise ApiError(0, "server unreachable: connection refused")

    code = cli.main([], config_loader=lambda **kwargs: _config(tmp_path),
                    api_factory=UnreachableApi)

    assert code == 3
    assert "server unreachable" in capsys.readouterr().err


def test_json_suppresses_the_human_report(tmp_path, capsys):
    class EmptyApi:
        def __init__(self, server, api_key):
            pass

        def freshness(self):
            return {"archive_rows": 1, "frontier": None, "fingerprint_window_bytes": 0}

        def reconcile_all(self, device_id, entries, **kwargs):
            return []

    code = cli.main(["--json", "--dry-run"], config_loader=lambda **kwargs: _config(tmp_path),
                    api_factory=EmptyApi)

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["summary"]["TOTAL"]["files"] == 0
    assert "Nothing is safe to delete" not in captured.out
