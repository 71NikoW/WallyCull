"""Import from a memory card: copies, checks against the card, repairs wrong copies, never writes to the card."""

import hashlib
import os

import pytest

from wallycull import config, importer


def _tree(folder):
    return {p.relative_to(folder).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime)
            for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def card(tmp_path):
    folder = tmp_path / "card" / "100_FUJI"
    folder.mkdir(parents=True)
    for i in range(5):
        (folder / f"DSCF{i:04d}.RAF").write_bytes(os.urandom(300_000 + i))
        (folder / f"DSCF{i:04d}.JPG").write_bytes(os.urandom(50_000))
        os.utime(folder / f"DSCF{i:04d}.RAF", (1_760_000_000 + i, 1_760_000_000 + i))
    (folder / "DSCF0005.MOV").write_bytes(os.urandom(9_000_000))
    (folder / ".wallycull").mkdir()
    (folder / ".wallycull" / "wallycull.db").write_bytes(b"broken")
    return folder


def test_import_copies_and_checks_everything(card, tmp_path):
    before = _tree(card)
    target = tmp_path / "pc"
    result = importer.run(card, target, config.load())
    assert result["copied"] == 11 and result["verified"] == 11 and not result["failed"]
    assert _tree(card) == before  # the card is untouched
    copy = _tree(target)
    assert {k: v[0] for k, v in copy.items()} == {k: v[0] for k, v in before.items() if not k.startswith(".")}
    assert copy["DSCF0003.RAF"][1] == before["DSCF0003.RAF"][1]  # camera date kept
    assert not list(target.rglob("*.part"))


def test_wrong_copy_is_repaired_and_import_resumes(card, tmp_path):
    target = tmp_path / "pc"
    importer.run(card, target, config.load())
    broken = bytearray((target / "DSCF0002.RAF").read_bytes())
    broken[150_000] ^= 0xFF  # one flipped byte, as a bad card reader does it
    (target / "DSCF0002.RAF").write_bytes(bytes(broken))
    result = importer.run(card, target, config.load())
    assert result["copied"] == 0 and result["present"] == 11
    assert result["repaired"] == 1 and result["verified"] == 11
    assert (target / "DSCF0002.RAF").read_bytes() == (card / "DSCF0002.RAF").read_bytes()


def test_existing_different_file_is_never_overwritten(card, tmp_path):
    target = tmp_path / "pc"
    target.mkdir()
    (target / "DSCF0001.JPG").write_bytes(b"another photo")
    result = importer.run(card, target, config.load())
    assert result["conflicts"] == ["DSCF0001.JPG"]
    assert (target / "DSCF0001.JPG").read_bytes() == b"another photo"


def test_default_target_and_no_import_onto_the_card(card, tmp_path):
    cfg = config.load()
    cfg["files"]["import_folder"] = str(tmp_path / "pictures")
    target = importer.default_target(card, cfg)
    assert target.parent == tmp_path / "pictures" and target.name.endswith("_100_FUJI")
    with pytest.raises(ValueError):
        importer.run(card, card / "copy", cfg)


def test_checksum_reads_the_file(tmp_path):
    f = tmp_path / "x.bin"
    data = os.urandom(5 * 1024 * 1024 + 123)  # more than one chunk, not sector aligned
    f.write_bytes(data)
    assert importer.checksum(f) == hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_unbuffered_read_on_windows(tmp_path):
    f = tmp_path / "x.bin"
    data = os.urandom(9 * 1024 * 1024 + 77)
    f.write_bytes(data)
    assert importer._hash_unbuffered_windows(f) == hashlib.sha256(data).hexdigest()
