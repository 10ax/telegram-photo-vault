from pathlib import Path

import pytest

from app.models.database import MediaType, PhotoStatus
from app.worker import PhotoWorker


@pytest.fixture
def worker(tmp_path):
    return PhotoWorker(
        None,
        None,
        None,
        "/srv/photo-vault",
        download_root=tmp_path / "dl",
        compressed_root=tmp_path / "cp",
        mode="manual",
    )


def test_download_path_mirrors_remote_tree(worker, tmp_path):
    target = worker._build_download_path("/Camera/2024/IMG_1.jpg")
    assert target == tmp_path / "dl" / "Camera" / "2024" / "IMG_1.jpg"
    assert target.parent.is_dir()


def test_download_path_rejects_directories(worker):
    with pytest.raises(ValueError):
        worker._build_download_path("/")


def test_compressed_path_swaps_root_and_suffix(worker, tmp_path):
    local = tmp_path / "dl" / "Camera" / "IMG_1.jpg"
    assert worker._build_compressed_path(local) == tmp_path / "cp" / "Camera" / "IMG_1.webp"


def test_compressed_path_outside_download_root(worker, tmp_path):
    assert (
        worker._build_compressed_path(Path("/elsewhere/IMG_2.png"))
        == tmp_path / "cp" / "IMG_2.webp"
    )


def test_new_photo_routing():
    image = PhotoWorker._new_photo("/Camera/a.jpg")
    assert (image.media_type, image.status) == (MediaType.IMAGE, PhotoStatus.PENDING)

    video = PhotoWorker._new_photo("/Camera/a.mp4")
    assert (video.media_type, video.status) == (MediaType.VIDEO, PhotoStatus.PENDING)

    other = PhotoWorker._new_photo("/Camera/a.pdf")
    assert (other.media_type, other.status) == (MediaType.OTHER, PhotoStatus.SKIPPED)


def test_invalid_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        PhotoWorker(
            None,
            None,
            None,
            "/srv",
            download_root=tmp_path / "d",
            compressed_root=tmp_path / "c",
            mode="warp-speed",
        )
