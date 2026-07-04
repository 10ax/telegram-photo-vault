from app.models.database import MediaType
from app.services.media import detect_media_type


def test_images():
    assert detect_media_type("/Camera/a/IMG_001.jpg") == MediaType.IMAGE
    assert detect_media_type("/Camera/a/IMG_001.HEIC") == MediaType.IMAGE
    assert detect_media_type("photo.WebP") == MediaType.IMAGE


def test_videos():
    assert detect_media_type("/Camera/VID_001.mp4") == MediaType.VIDEO
    assert detect_media_type("clip.MOV") == MediaType.VIDEO


def test_other():
    assert detect_media_type("/Camera/notes.pdf") == MediaType.OTHER
    assert detect_media_type("/Camera/noext") == MediaType.OTHER
    assert detect_media_type("archive.tar.gz") == MediaType.OTHER
