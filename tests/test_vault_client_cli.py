from vault_client import __main__ as cli
from vault_client.config import ConfigError


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
