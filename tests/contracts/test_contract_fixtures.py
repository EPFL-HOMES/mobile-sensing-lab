from __future__ import annotations

import hashlib
from pathlib import Path

from tests.support.generate_contract_fixtures import build_fixture_files, write_fixture_files


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


def test_checked_in_contract_fixtures_regenerate_byte_for_byte(tmp_path: Path) -> None:
    write_fixture_files(tmp_path)
    expected = build_fixture_files()
    for relative, content in expected.items():
        assert (FIXTURE_ROOT / relative).read_bytes() == content
        assert (tmp_path / relative).read_bytes() == content
    assert (FIXTURE_ROOT / "SHA256SUMS").read_bytes() == (tmp_path / "SHA256SUMS").read_bytes()


def test_fixture_checksum_index_is_complete_and_correct() -> None:
    indexed: dict[str, str] = {}
    for line in (FIXTURE_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        indexed[relative] = digest

    expected = build_fixture_files()
    assert set(indexed) == set(expected)
    assert indexed == {
        relative: hashlib.sha256(content).hexdigest() for relative, content in expected.items()
    }
