"""Characterisation tests for how MegaService shells out to MEGAcmd.

MEGAcmd itself is replaced with throwaway shell scripts in a temp directory —
the service takes each command name as a constructor argument, so no mocking is
needed and the real asyncio subprocess path is exercised. The parsing of
`mega-ls -R` output is covered separately in tests/test_mega_parse.py.
"""
from pathlib import Path

import pytest

from app.services.mega import MegaCmdError, MegaService

LS_OUTPUT = """/Camera:
IMG_001.jpg
sub/

/Camera/sub:
IMG_002.jpg
"""


def _fake_command(tmp_path: Path, name: str, *, stdout="", stderr="", exit_code=0) -> Path:
    """Write an executable stand-in that logs its argv and replays fixed output."""
    payload = tmp_path / f"{name}.stdout"
    payload.write_text(stdout)
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> "{tmp_path / f"{name}.argv"}"\n'
        f'cat "{payload}"\n'
        + (f'printf "%s" "{stderr}" >&2\n' if stderr else "")
        + f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script


def _argv(tmp_path: Path, name: str) -> list[str]:
    log = tmp_path / f"{name}.argv"
    return log.read_text().splitlines() if log.exists() else []


async def test_listing_passes_the_recursive_flag_and_the_target_folder(tmp_path):
    command = _fake_command(tmp_path, "ls", stdout=LS_OUTPUT)
    service = MegaService(target_folder="/Camera", mega_ls_command=str(command))

    files = await service.list_new_files()

    assert files == ["/Camera/IMG_001.jpg", "/Camera/sub/IMG_002.jpg"]
    assert _argv(tmp_path, "ls") == ["-R", "/Camera"]


async def test_a_non_zero_exit_becomes_a_megacmderror_carrying_the_stderr(tmp_path):
    command = _fake_command(tmp_path, "ls", stderr="Not logged in.", exit_code=9)
    service = MegaService(target_folder="/Camera", mega_ls_command=str(command))

    with pytest.raises(MegaCmdError) as excinfo:
        await service.list_new_files()

    message = str(excinfo.value)
    assert "Command failed (9)" in message
    assert "Not logged in." in message


async def test_a_silent_failure_still_names_the_command(tmp_path):
    # MEGAcmd with no server running exits non-zero and says nothing useful.
    command = _fake_command(tmp_path, "ls", exit_code=1)
    service = MegaService(target_folder="/Camera", mega_ls_command=str(command))

    with pytest.raises(MegaCmdError, match=r"Command failed \(1\)"):
        await service.list_new_files()


async def test_download_creates_the_parent_directory_and_normalises_the_remote_path(
    tmp_path,
):
    command = _fake_command(tmp_path, "get")
    service = MegaService(target_folder="/Camera", mega_get_command=str(command))
    target = tmp_path / "dl" / "Camera" / "2024" / "IMG_1.jpg"

    returned = await service.download_file("Camera//2024/IMG_1.jpg", target)

    assert returned == target
    assert target.parent.is_dir()
    assert _argv(tmp_path, "get") == ["/Camera/2024/IMG_1.jpg", str(target)]


async def test_delete_normalises_the_remote_path(tmp_path):
    command = _fake_command(tmp_path, "rm")
    service = MegaService(target_folder="/Camera", mega_rm_command=str(command))

    await service.delete_file("/Camera/sub/../sub/IMG_1.jpg/")

    assert _argv(tmp_path, "rm") == ["/Camera/sub/../sub/IMG_1.jpg"]


async def test_a_failed_delete_raises_rather_than_silently_leaving_the_source(tmp_path):
    command = _fake_command(tmp_path, "rm", stderr="Not found", exit_code=1)
    service = MegaService(target_folder="/Camera", mega_rm_command=str(command))

    with pytest.raises(MegaCmdError, match="Not found"):
        await service.delete_file("/Camera/IMG_1.jpg")


def test_an_empty_target_folder_is_rejected_at_construction():
    with pytest.raises(ValueError, match="Remote path cannot be empty"):
        MegaService(target_folder="   ")


@pytest.mark.parametrize(
    "target, path, inside",
    [
        ("/Camera", "/Camera", True),
        ("/Camera", "/Camera/IMG_1.jpg", True),
        ("/Camera", "/Camera/sub/IMG_1.jpg", True),
        ("/Camera", "/CameraRoll/IMG_1.jpg", False),
        ("/Camera", "/Other/IMG_1.jpg", False),
        ("/", "/anything/at/all.jpg", True),
    ],
)
def test_target_folder_containment(target, path, inside):
    assert MegaService(target_folder=target)._is_within_target(path) is inside
