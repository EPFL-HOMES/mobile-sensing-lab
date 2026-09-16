"""Order-independent semantic seed derivation with isolated preview streams."""

from __future__ import annotations

import hashlib

import numpy as np

from mobile_sensing.contracts import (
    SeedManifest,
    SeedStreamRecord,
    canonical_json_bytes,
)


RNG_DERIVATION_VERSION = "semantic-seed@1"


class SemanticRngStreams:
    """Derive fresh generators from semantic labels, never call order or worker identity."""

    def __init__(self, master_seed: int, *, namespace: str = "simulation") -> None:
        if isinstance(master_seed, bool) or not 0 <= master_seed <= 2**64 - 1:
            raise ValueError("master_seed must be an unsigned 64-bit integer")
        if namespace == "":
            raise ValueError("RNG namespace must be nonempty")
        self.master_seed = master_seed
        self.namespace = namespace
        self._records: dict[str, SeedStreamRecord] = {}

    def stream(self, *semantic_labels: str) -> np.random.Generator:
        if not semantic_labels or any(
            not isinstance(label, str) or label == "" for label in semantic_labels
        ):
            raise ValueError("RNG streams require nonempty string semantic labels")
        stream_key = semantic_labels[0]
        labels = semantic_labels[1:]
        record_key = "\x1f".join((self.namespace, *semantic_labels))
        preimage = canonical_json_bytes(
            {
                "derivation_version": RNG_DERIVATION_VERSION,
                "master_seed": self.master_seed,
                "namespace": self.namespace,
                "stream_key": stream_key,
                "semantic_labels": labels,
            }
        )
        seed = int.from_bytes(hashlib.sha256(preimage).digest()[:8], "big")
        manifest_key = (
            f"{self.namespace}:{stream_key}:{hashlib.sha256(record_key.encode()).hexdigest()[:16]}"
        )
        self._records[manifest_key] = SeedStreamRecord(
            stream_key=manifest_key,
            semantic_labels=tuple(labels),
            derived_seed=seed,
        )
        return np.random.Generator(np.random.PCG64(seed))

    def preview(self) -> "SemanticRngStreams":
        """Return an isolated provider which cannot consume or record simulation streams."""

        return SemanticRngStreams(self.master_seed, namespace="preview")

    def restore_manifest(self, manifest: SeedManifest) -> None:
        """Restore cached stream provenance without drawing or advancing randomness."""
        if manifest.master_seed != self.master_seed:
            raise ValueError("Cached RNG master seed mismatch")
        restored = SemanticRngStreams(self.master_seed, namespace=self.namespace)
        for record in manifest.streams:
            prefix = f"{self.namespace}:"
            if not record.stream_key.startswith(prefix):
                raise ValueError("Cached RNG namespace mismatch")
            name = record.stream_key[len(prefix) :].rsplit(":", 1)[0]
            restored.stream(name, *record.semantic_labels)
            if restored._records.get(record.stream_key) != record:
                raise ValueError("Cached RNG derivation mismatch")
        self._records.update(restored._records)

    @property
    def manifest(self) -> SeedManifest:
        return SeedManifest(
            master_seed=self.master_seed,
            streams=tuple(self._records[key] for key in sorted(self._records)),
        )
