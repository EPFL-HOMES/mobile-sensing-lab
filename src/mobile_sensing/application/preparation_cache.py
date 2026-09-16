"""Disposable, checksummed JSON caches for deterministic preparation stages."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

from pydantic import TypeAdapter

from mobile_sensing.contracts import LocationRef, SeedManifest, scientific_hash


VERSION = "preparation-stages@1"
MAX_BYTES = 128 * 1024**2


def cached_stage(
    root,
    stage,
    identity,
    result_type,
    compute,
    *,
    support=None,
    rng=None,
    cancellation=None,
    progress=None,
):
    key = scientific_hash({"version": VERSION, "stage": stage, "inputs": identity})
    path = Path(root) / "preparation_cache" / stage / f"{key}.json.gz"
    adapter = TypeAdapter(result_type)
    if cancellation:
        cancellation.raise_if_cancelled()
    if path.is_file():
        try:
            with gzip.open(path, "rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Oversized preparation cache")
            envelope = json.loads(raw)
            payload = envelope["payload"].encode()
            if hashlib.sha256(payload).hexdigest() != envelope["sha256"]:
                raise ValueError("Preparation cache checksum mismatch")
            value = json.loads(payload)
            result = adapter.validate_json(json.dumps(value["result"]))
            locations = {
                k: LocationRef.model_validate_json(json.dumps(v))
                for k, v in value["locations"].items()
            }
            if rng is not None:
                rng.restore_manifest(SeedManifest.model_validate_json(json.dumps(value["rng"])))
            if support is not None:
                support.locations.update(locations)
            if progress:
                progress.update(phase=f"resolve.reuse.{stage}", completed=1, total=1)
            return result
        except (ValueError, KeyError, TypeError, AttributeError, OSError, EOFError):
            # A cache is never the only copy of scientific inputs or outputs.
            pass
    old_locations = set(support.locations) if support else set()
    old_streams = {x.stream_key for x in rng.manifest.streams} if rng else set()
    result = compute()
    if cancellation:
        cancellation.raise_if_cancelled()
    value = {
        "result": json.loads(adapter.dump_json(result)),
        "locations": (
            {
                k: v.model_dump(mode="json")
                for k, v in support.locations.items()
                if k not in old_locations
            }
            if support
            else {}
        ),
        "rng": (
            rng.manifest.model_copy(
                update={
                    "streams": tuple(
                        x for x in rng.manifest.streams if x.stream_key not in old_streams
                    )
                }
            ).model_dump(mode="json")
            if rng
            else None
        ),
    }
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False)
    envelope = json.dumps(
        {"payload": payload, "sha256": hashlib.sha256(payload.encode()).hexdigest()}
    ).encode()
    if len(envelope) <= MAX_BYTES:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with gzip.open(temporary, "wb") as stream:
                stream.write(envelope)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return result
