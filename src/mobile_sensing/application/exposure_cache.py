"""Disposable indexes of complete, checksum-verified exposure artifacts."""

import os
import tempfile
from pathlib import Path

from mobile_sensing.contracts import (
    ArtifactRef,
    canonical_json_text,
    scientific_hash,
    scientific_projection,
)
from mobile_sensing.exposure.allocation import EXPOSURE_ALLOCATION_VERSION
from mobile_sensing.exposure.geometry import EDGE_GRID_ALGORITHM_VERSION, ROAD_GRID_DOMAIN_VERSION
from mobile_sensing.exposure.storage import EXPOSURE_STORAGE_VERSION, ExposureArtifactReader
from mobile_sensing.exposure.operating import OPERATING_ALLOCATION_VERSION
from mobile_sensing.simulation.storage import SIMULATION_STORAGE_VERSION


def cached_exposure(root, environment, simulation, config, compute):
    exposure_config = scientific_projection(config)
    if config.included_cell_ids is None:
        exposure_config.pop("included_cell_ids")
    identity = {
        "environment": environment,
        "simulation": simulation,
        "exposure": exposure_config,
        "algorithms": [
            EXPOSURE_STORAGE_VERSION,
            EXPOSURE_ALLOCATION_VERSION,
            EDGE_GRID_ALGORITHM_VERSION,
            ROAD_GRID_DOMAIN_VERSION,
            OPERATING_ALLOCATION_VERSION,
            SIMULATION_STORAGE_VERSION,
        ],
    }
    path = Path(root) / "exposure_cache" / f"{scientific_hash(identity)}.json"
    if path.is_file():
        reference = ArtifactRef.model_validate_json(path.read_bytes())
        target = Path(root) / "exposures" / reference.artifact_id / "manifest.json"
        if target.is_file():
            artifact = ExposureArtifactReader(root).axes(reference)["artifact"]
            if artifact.manifest.scientific_identity.resolved_config["exposure"] != exposure_config:
                raise ValueError("Exposure cache configuration mismatch")
            dependencies = {
                item.role: (item.artifact_id, item.content_hash)
                for item in artifact.manifest.dependencies
            }
            for role, value in (("environment", environment), ("simulation", simulation)):
                if dependencies.get(role) != (value.artifact_id, value.content_hash):
                    raise ValueError("Exposure cache dependency mismatch")
            return reference
    reference = compute()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish the disposable pointer only after the complete artifact exists.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(canonical_json_text(reference))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return reference
