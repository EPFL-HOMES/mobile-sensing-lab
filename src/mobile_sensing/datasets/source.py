"""Immutable CSV/Parquet source registration and bounded chunk iteration."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from mobile_sensing.contracts import canonical_json_bytes, stable_id
from mobile_sensing.datasets.models import RegisteredTabularSource, TabularSource


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_tabular_source(
    source_path: Path, *, artifact_root: Path, provenance: str
) -> TabularSource:
    source_path = Path(source_path).resolve()
    suffix = source_path.suffix.casefold()
    formats = {".csv": "csv", ".parquet": "parquet"}
    if suffix not in formats:
        raise ValueError("tabular upload must be UTF-8 CSV or Parquet")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if formats[suffix] == "csv":
        source_path.read_text(encoding="utf-8")
    else:
        pq.read_schema(source_path)
    content_hash = _sha256(source_path)
    registration = RegisteredTabularSource(
        dataset_id=stable_id("dataset", {"content_hash": content_hash, "format": formats[suffix]}),
        content_hash=content_hash,
        original_filename=source_path.name,
        size_bytes=source_path.stat().st_size,
        format=formats[suffix],
        provenance=provenance,
    )
    final = Path(artifact_root).resolve() / "raw_inputs" / registration.dataset_id
    stored = final / f"source{suffix}"
    if final.exists():
        if not (final / "_SUCCESS").is_file() or _sha256(stored) != content_hash:
            raise ValueError("existing raw input is incomplete or has changed")
        stored_registration = RegisteredTabularSource.model_validate_json(
            (final / "registration.json").read_bytes()
        )
        if stored_registration.content_hash != content_hash:
            raise ValueError("existing raw input registration does not match its bytes")
        return TabularSource(stored_registration, stored)
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".raw-staging-", dir=final.parent) as staging_name:
        staging = Path(staging_name)
        target = staging / stored.name
        with source_path.open("rb") as reader, target.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        (staging / "registration.json").write_bytes(canonical_json_bytes(registration) + b"\n")
        (staging / "_SUCCESS").write_text("complete\n", encoding="ascii")
        os.replace(staging, final)
    return TabularSource(registration, stored)


def load_registered_tabular_source(dataset_id: str, *, artifact_root: Path) -> TabularSource:
    """Load a managed upload only after verifying identity, inventory, size, and bytes."""

    directory = Path(artifact_root).resolve() / "raw_inputs" / dataset_id
    if not (directory / "_SUCCESS").is_file():
        raise FileNotFoundError(f"raw input is absent or incomplete: {dataset_id}")
    registration = RegisteredTabularSource.model_validate_json(
        (directory / "registration.json").read_bytes()
    )
    suffix = ".csv" if registration.format == "csv" else ".parquet"
    source = directory / f"source{suffix}"
    expected = {"_SUCCESS", "registration.json", source.name}
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != expected:
        raise ValueError("raw input inventory does not match its registration")
    if registration.dataset_id != dataset_id:
        raise ValueError("raw input registration ID does not match its directory")
    if not source.is_file() or source.stat().st_size != registration.size_bytes:
        raise ValueError("raw input size does not match its registration")
    if _sha256(source) != registration.content_hash:
        raise ValueError("raw input checksum does not match its registration")
    return TabularSource(registration=registration, path=source)


def iter_source_chunks(source: TabularSource, *, chunk_size: int) -> Iterator[pd.DataFrame]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if source.registration.format == "csv":
        yield from pd.read_csv(
            source.path,
            dtype="string",
            keep_default_na=False,
            encoding="utf-8",
            chunksize=chunk_size,
        )
        return
    parquet = pq.ParquetFile(source.path)
    for batch in parquet.iter_batches(batch_size=chunk_size):
        yield batch.to_pandas()
