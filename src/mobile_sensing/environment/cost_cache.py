"""Persistent directed travel times, one bounded source shard at a time."""

import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from mobile_sensing.contracts import scientific_hash


def bounded_source_costs(operation, sources, workers):
    """At most workers pending searches; ordered reduction, shared read-only graph."""
    from concurrent.futures import ThreadPoolExecutor

    if not sources:
        return
    # Initialize lazy native graph state before sharing it between searches.
    yield operation(sources[0])
    if workers <= 1:
        for source in sources[1:]:
            yield operation(source)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(1, len(sources), workers):
            futures = [
                pool.submit(operation, source) for source in sources[start : start + workers]
            ]
            for future in futures:
                yield future.result()


class PersistentTravelTimes:
    """Delegate actual routes unchanged; cache exact scalar planner costs only."""

    def __init__(self, routing, root, *, workers=1, memory_limit_bytes=4 * 1024**3):
        self.routing = routing
        self.root = Path(root) / "preparation_cache" / "travel_times"
        # Shared graph, at most four concurrent source searches and bounded results.
        self.cost_workers = max(1, min(workers, 4, memory_limit_bytes // (512 * 1024**2)))

    def __getattr__(self, name):
        return getattr(self.routing, name)

    def travel_times_from(self, profile_id, source_node_id, target_node_ids):
        targets = tuple(sorted(set(target_node_ids)))
        key = scientific_hash(
            {
                "version": "exact-directed-travel-times@1",
                "network": self.routing.network_hash,
                "profile": self.routing.profile_hashes[profile_id],
                "source": source_node_id,
            }
        )
        directory = self.root / key[:2]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{key}.json.gz"
        # Cross-process serialization only for the same source/profile shard.
        with (directory / f"{key}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            values = {}
            try:
                with gzip.open(path, "rb") as stream:
                    raw = stream.read(16 * 1024**2 + 1)
                if len(raw) > 16 * 1024**2:
                    raise ValueError("Oversized routing cache shard")
                saved = json.loads(raw)
                payload = saved["payload"]
                if hashlib.sha256(payload.encode()).hexdigest() != saved["sha256"]:
                    raise ValueError("Routing cost checksum mismatch")
                values = json.loads(payload)
                if not isinstance(values, dict) or any(
                    v is not None
                    and (not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0)
                    for v in values.values()
                ):
                    raise ValueError("Invalid cached routing costs")
            except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError):
                values = {}
            missing = [target for target in targets if target not in values]
            if missing:
                values.update(self.routing.travel_times_from(profile_id, source_node_id, missing))
                payload = json.dumps(values, separators=(",", ":"), allow_nan=False)
                saved = json.dumps(
                    {
                        "payload": payload,
                        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    }
                ).encode()
                if len(saved) <= 16 * 1024**2:
                    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                        temporary = Path(stream.name)
                    try:
                        with gzip.open(temporary, "wb") as stream:
                            stream.write(saved)
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
            return {target: values[target] for target in targets}
