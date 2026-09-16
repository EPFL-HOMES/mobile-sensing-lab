"""Immutable file snapshots shared by global and fleet input editors."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal, Any

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from pydantic import Field
from pyproj import CRS

from mobile_sensing.contracts import ContractModel, scientific_hash, stable_id

InputRole = Literal[
    "boundary",
    "network",
    "speed",
    "grid",
    "population",
    "feature",
    "weight",
    "demand",
    "supply",
    "assignment",
    "gtfs",
    "service_area",
    "area_assignment",
]


class InputRegistration(ContractModel):
    path: str
    name: str = Field(min_length=1, max_length=200)
    role: InputRole
    source_crs: str | None = None
    layer: str | None = None


class InputDescriptor(ContractModel):
    input_id: str
    name: str
    role: InputRole
    format: str
    source_crs: str | None
    layer: str | None
    columns: list[str]
    preview: list[dict[str, Any]]
    layers: list[str]
    size_bytes: int
    byte_hash: str
    content_hash: str
    file: str
    original_filename: str
    crs_required: bool
    crs_suggestion: str | None = None


class InputCrsConfirmation(ContractModel):
    source_crs: str


def snapshot_input(root: Path, request: InputRegistration, *, limit: int = 2 * 1024**3) -> dict:
    """Copy bytes before inspecting them; source paths never become live dependencies."""
    try:
        source = Path(request.path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("The selected file does not exist on the application computer") from exc
    if not source.is_file():
        raise ValueError("Select a regular file. GTFS directories must be supplied as a ZIP.")
    suffix = source.suffix.lower()
    if suffix not in {".csv", ".parquet", ".gpkg", ".geojson", ".json", ".zip"}:
        raise ValueError(
            "Supported formats: CSV, Parquet/GeoParquet, GeoPackage, GeoJSON, GTFS ZIP"
        )
    if suffix == ".zip" and request.role != "gtfs":
        raise ValueError("ZIP is supported for GTFS feeds only")
    if request.source_crs:
        CRS.from_user_input(request.source_crs)
    raw_root = root / "inputs"
    raw_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".input-", dir=raw_root) as temporary:
        stage = Path(temporary)
        target = stage / ("source" + suffix)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as src, target.open("wb") as out:
            for chunk in iter(lambda: src.read(1024**2), b""):
                size += len(chunk)
                if size > limit:
                    raise ValueError("Input exceeds the configured byte limit")
                digest.update(chunk)
                out.write(chunk)
        byte_hash = digest.hexdigest()
        crs = request.source_crs
        columns, preview, layers = [], [], []
        if suffix == ".csv":
            frame = pd.read_csv(target, nrows=8, dtype=str, keep_default_na=False)
            columns = list(frame.columns)
            preview = frame.to_dict("records")
        elif suffix == ".parquet":
            file = pq.ParquetFile(target)
            columns = file.schema_arrow.names
            metadata = file.schema_arrow.metadata or {}
            if b"geo" in metadata:
                geo = json.loads(metadata[b"geo"])
                native = geo["columns"][geo["primary_column"]].get("crs", "EPSG:4326")
                if native is not None:
                    parsed = CRS.from_user_input(native)
                    if crs and parsed != CRS.from_user_input(crs):
                        raise ValueError(
                            "Declared CRS conflicts with file metadata; transform instead of relabeling"
                        )
                    crs = parsed.to_string()
            else:
                batch = next(file.iter_batches(batch_size=8), None)
                preview = (
                    batch.to_pandas().fillna("").astype(str).to_dict("records") if batch else []
                )
        elif suffix in {".gpkg", ".geojson", ".json"}:
            if suffix == ".gpkg":
                layers = gpd.list_layers(target)["name"].tolist()
                if len(layers) > 1 and not request.layer:
                    raise ValueError(f"Select one GeoPackage layer: {', '.join(layers)}")
            frame = gpd.read_file(target, layer=request.layer, rows=8)
            columns = [str(c) for c in frame.columns if c != frame.geometry.name]
            if frame.crs:
                native = CRS.from_user_input(frame.crs)
                if crs and native != CRS.from_user_input(crs):
                    raise ValueError(
                        "Declared CRS conflicts with file metadata; transform instead of relabeling"
                    )
                crs = native.to_string()
        identity = {"bytes": byte_hash, "role": request.role, "crs": crs, "layer": request.layer}
        input_id = stable_id("input", identity)
        metadata = {
            "input_id": input_id,
            "name": request.name,
            "role": request.role,
            "format": suffix[1:],
            "source_crs": crs,
            "layer": request.layer,
            "columns": columns,
            "preview": preview,
            "layers": layers,
            "size_bytes": size,
            "byte_hash": byte_hash,
            "content_hash": scientific_hash(identity),
            "file": target.name,
            "original_filename": source.name,
            "crs_required": crs is None
            and request.role
            in {
                "boundary",
                "network",
                "grid",
                "feature",
                "population",
                "weight",
                "demand",
                "supply",
            }
            and "cell_id" not in columns,
            "crs_suggestion": (
                "EPSG:4326"
                if ({"longitude", "latitude"} <= set(columns) or {"lon", "lat"} <= set(columns))
                else None
            ),
        }
        (stage / "input.json").write_text(json.dumps(metadata, ensure_ascii=False) + "\n")
        destination = raw_root / input_id
        if not destination.exists():
            stage.rename(destination)
        return json.loads((destination / "input.json").read_text())


def load_input(root: Path, input_id: str, *, verify: bool = True) -> tuple[dict, Path]:
    if not input_id.startswith("input_") or Path(input_id).name != input_id:
        raise ValueError("Invalid managed input identifier")
    directory = root / "inputs" / input_id
    metadata = json.loads((directory / "input.json").read_text())
    path = directory / metadata["file"]
    if path.parent != directory or not path.is_file():
        raise ValueError("Invalid input inventory")
    if verify:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                digest.update(chunk)
        if digest.hexdigest() != metadata["byte_hash"]:
            raise ValueError("Input snapshot checksum mismatch")
    return metadata, path
