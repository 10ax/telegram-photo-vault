"""Characterisation tests for SFTPService's constructor guard and path handling.

No SSH is attempted here: every assertion is about a decision made before
asyncssh.connect() is ever reached.
"""
import pytest

from app.services.sftp import SFTPService

KNOWN_HOSTS = "/data/ssh/known_hosts"


def _service(**kwargs):
    kwargs.setdefault("known_hosts", KNOWN_HOSTS)
    return SFTPService("odroid.example.invalid", "vault", **kwargs)


def test_host_key_verification_cannot_be_skipped_by_accident():
    with pytest.raises(ValueError, match="known_hosts is required"):
        SFTPService("odroid.example.invalid", "vault")

    with pytest.raises(ValueError, match="known_hosts is required"):
        SFTPService("odroid.example.invalid", "vault", known_hosts="")


def test_insecure_mode_must_be_asked_for_explicitly(caplog):
    with caplog.at_level("WARNING"):
        service = SFTPService(
            "odroid.example.invalid", "vault", allow_insecure_host_key=True
        )

    assert service.allow_insecure_host_key is True
    assert "host-key verification is DISABLED" in caplog.text


def test_defaults_are_the_documented_ones():
    service = _service()
    assert service.port == 22
    assert service.password is None
    assert service.client_keys is None
    assert service.allow_insecure_host_key is False


@pytest.mark.parametrize(
    "given, expected",
    [
        ("/srv/photo-vault", "/srv/photo-vault"),
        ("srv/photo-vault", "/srv/photo-vault"),
        ("/srv/photo-vault/", "/srv/photo-vault"),
        ("  /srv/photo-vault  ", "/srv/photo-vault"),
        ("/srv//photo-vault", "/srv/photo-vault"),
        ("/srv/./photo-vault", "/srv/photo-vault"),
        ("/", "/"),
    ],
)
def test_remote_directories_are_normalised_to_a_clean_absolute_path(given, expected):
    assert SFTPService._normalize_remote_dir(given) == expected


def test_an_empty_remote_directory_is_rejected():
    with pytest.raises(ValueError, match="Remote directory cannot be empty"):
        SFTPService._normalize_remote_dir("   ")


async def test_a_missing_local_file_fails_before_any_connection(tmp_path):
    service = _service()

    with pytest.raises(FileNotFoundError, match="Local file not found"):
        await service.upload_file(tmp_path / "does-not-exist.webp", "/srv/photo-vault")


async def test_a_directory_is_not_an_uploadable_file(tmp_path):
    service = _service()

    with pytest.raises(FileNotFoundError):
        await service.upload_file(tmp_path, "/srv/photo-vault")
