from app.services.mega import MegaService

LS_OUTPUT = """\
/Camera:
IMG_001.jpg
sub/

/Camera/sub:
IMG_002.jpg
deeper/

/Camera/sub/deeper:
IMG_003.heic
"""


def test_recursive_listing():
    service = MegaService(target_folder="/Camera")
    assert service._parse_mega_ls_output(LS_OUTPUT) == [
        "/Camera/IMG_001.jpg",
        "/Camera/sub/IMG_002.jpg",
        "/Camera/sub/deeper/IMG_003.heic",
    ]


def test_absolute_lines_outside_target_are_filtered():
    service = MegaService(target_folder="/Camera")
    output = "/Other/file.jpg\n/Camera/ok.jpg\n"
    assert service._parse_mega_ls_output(output) == ["/Camera/ok.jpg"]


def test_target_normalization():
    assert MegaService(target_folder="Camera/").target_folder == "/Camera"
    assert MegaService(target_folder="/").target_folder == "/"
