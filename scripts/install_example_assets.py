"""Install the two checksummed city-example archives from the matching GitHub release.

The Lausanne archive exceeds GitHub's per-release-asset limit, so the release
contains two byte-for-byte parts. This script streams and verifies the parts,
then publishes the original ZIP atomically for the existing offline installer.
"""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).with_name("example_assets_manifest.json")
DEFAULT_DESTINATION = ROOT / "src/mobile_sensing/_examples"
BLOCK_BYTES = 4 * 1024 * 1024


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def install_one(key, entry, destination, base_url, *, replace=False):
    target = destination / f"{key}.zip"
    expected_size = entry["archive_size_bytes"]
    expected_digest = entry["archive_sha256"]
    if target.is_file() and target.stat().st_size == expected_size:
        if digest_file(target) == expected_digest:
            print(f"{key}: matching archive already installed")
            return
    if target.exists() and not replace:
        raise ValueError(f"{target} differs from the release; pass --replace to retain it as .zip.previous")

    backup = destination / f"{key}.zip.previous"
    if target.exists() and backup.exists():
        raise ValueError(f"Refusing to replace {target}: backup already exists at {backup}")
    with tempfile.TemporaryDirectory(prefix=f".{key}-download-", dir=destination) as temporary:
        staged = Path(temporary) / target.name
        complete_digest = hashlib.sha256()
        complete_size = 0
        with staged.open("wb") as output:
            for part in entry["parts"]:
                name = part["name"]
                if Path(name).name != name:
                    raise ValueError(f"Unsafe example asset name: {name}")
                part_digest = hashlib.sha256()
                part_size = 0
                print(f"{key}: downloading {name}", flush=True)
                with urlopen(f"{base_url.rstrip('/')}/{quote(name)}", timeout=120) as source:
                    while block := source.read(BLOCK_BYTES):
                        part_size += len(block)
                        if part_size > part["size_bytes"]:
                            raise ValueError(f"Example asset exceeds declared size: {name}")
                        part_digest.update(block)
                        complete_digest.update(block)
                        output.write(block)
                if part_size != part["size_bytes"] or part_digest.hexdigest() != part["sha256"]:
                    raise ValueError(f"Example asset checksum or size mismatch: {name}")
                complete_size += part_size
        if complete_size != expected_size or complete_digest.hexdigest() != expected_digest:
            raise ValueError(f"Complete {key} archive checksum or size mismatch")
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except OSError:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
    print(f"{key}: installed {complete_size} verified bytes", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example-directory", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--replace", action="store_true", help="Back up a mismatched ZIP before replacing it")
    parser.add_argument("--base-url", help="Override the release-asset URL (checksums still apply)")
    args = parser.parse_args()
    manifest = json.loads(DEFAULT_MANIFEST.read_text())
    examples = manifest["examples"]
    if set(examples) != {"lausanne", "san-francisco"}:
        raise ValueError("The source distribution must contain exactly Lausanne and San Francisco")
    destination = args.example_directory.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url or (
        f"https://github.com/{manifest['repository']}/releases/download/"
        f"{quote(manifest['release_tag'])}"
    )
    for key, entry in examples.items():
        source_manifest = destination / f"{key}.json"
        if not source_manifest.is_file() or digest_file(source_manifest) != entry["manifest_sha256"]:
            raise ValueError(f"{key}.json does not match this source revision's release")
        install_one(key, entry, destination, base_url, replace=args.replace)


if __name__ == "__main__":
    main()
