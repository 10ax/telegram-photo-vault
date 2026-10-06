import hashlib

from vault_client.hashing import full_sha256, head_tail_sha256


def test_full_sha256_matches_hashlib(tmp_path):
    data = b"abcdef" * 1000
    f = tmp_path / "a.bin"
    f.write_bytes(data)
    assert full_sha256(f) == hashlib.sha256(data).hexdigest()


def test_head_and_tail_of_a_long_file_are_the_two_end_windows(tmp_path):
    data = bytes(range(256)) * 100  # 25,600 bytes
    f = tmp_path / "long.bin"
    f.write_bytes(data)
    head, tail = head_tail_sha256(f, window=1024)
    assert head == hashlib.sha256(data[:1024]).hexdigest()
    assert tail == hashlib.sha256(data[-1024:]).hexdigest()


def test_head_and_tail_of_a_short_file_are_both_the_whole_file(tmp_path):
    data = b"short"
    f = tmp_path / "short.bin"
    f.write_bytes(data)
    head, tail = head_tail_sha256(f, window=1024)
    assert head == hashlib.sha256(data).hexdigest()
    assert tail == hashlib.sha256(data).hexdigest()
