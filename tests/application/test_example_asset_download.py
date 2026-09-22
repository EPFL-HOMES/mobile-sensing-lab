"""Offline checks for the optional checksummed example-asset installer."""

import hashlib

import pytest

from scripts.install_example_assets import digest_file, install_one


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def test_split_example_archive_is_verified_and_reassembled(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    destination = tmp_path / "examples"
    destination.mkdir()
    parts = (b"first archive part", b"second archive part")
    records = []
    for index, value in enumerate(parts):
        name = f"lausanne.zip.part{index:02d}"
        (release / name).write_bytes(value)
        records.append({"name": name, "size_bytes": len(value), "sha256": _digest(value)})
    combined = b"".join(parts)
    entry = {
        "archive_size_bytes": len(combined),
        "archive_sha256": _digest(combined),
        "parts": records,
    }
    install_one("lausanne", entry, destination, release.as_uri())
    assert (destination / "lausanne.zip").read_bytes() == combined
    assert digest_file(destination / "lausanne.zip") == entry["archive_sha256"]
    install_one("lausanne", entry, destination, release.as_uri())
    assert not (destination / "lausanne.zip.previous").exists()


def test_mismatched_asset_cannot_replace_existing_archive(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    destination = tmp_path / "examples"
    destination.mkdir()
    target = destination / "lausanne.zip"
    target.write_bytes(b"previous release")
    (release / "lausanne.zip.part00").write_bytes(b"new release")
    entry = {
        "archive_size_bytes": len(b"new release"),
        "archive_sha256": _digest(b"new release"),
        "parts": [{
            "name": "lausanne.zip.part00",
            "size_bytes": len(b"new release"),
            "sha256": _digest(b"different bytes"),
        }],
    }
    with pytest.raises(ValueError, match="pass --replace"):
        install_one("lausanne", entry, destination, release.as_uri())
    with pytest.raises(ValueError, match="checksum or size mismatch"):
        install_one("lausanne", entry, destination, release.as_uri(), replace=True)
    assert target.read_bytes() == b"previous release"
    assert not (destination / "lausanne.zip.previous").exists()
