from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.run_pipeline import run_project
from tests.application.test_runs_analysis import config_fixture
from tests.environment.test_environment_editor import Cancellation
from tests.support.environment_fixtures import RecordedProgress
import json
import pytest


def test_complete_exposure_reuse_and_reporting_invalidation(tmp_path, monkeypatch):
    import mobile_sensing.application.services as services

    root, config = config_fixture(tmp_path)
    kwargs = dict(
        options=RunOptions(),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
        source_revision_id=None,
    )
    first = run_project(root, config, name="First", **kwargs)
    allocate = services.publish_exposure_artifact
    calls = []

    def traced(**arguments):
        calls.append(arguments["config"])
        return allocate(**arguments)

    monkeypatch.setattr(services, "publish_exposure_artifact", traced)
    second = run_project(root, config, name="Repeat", **kwargs)
    assert second.mobility_reused and second.exposure == first.exposure and not calls
    changed = config.model_copy(
        update={
            "simulation": config.simulation.model_copy(update={"temporal_resolution_minutes": 30.0})
        }
    )
    third = run_project(root, changed, name="Rebin", **kwargs)
    assert third.mobility_reused and third.exposure != first.exposure and len(calls) == 1
    # Cache indexes are disposable, while completed scientific artifacts remain reusable.
    for path in (root / "exposure_cache").glob("*.json"):
        path.unlink()
    fourth = run_project(root, config, name="Rebuilt index", **kwargs)
    assert fourth.exposure == first.exposure and len(calls) == 2

    # A cache hit must still reject changed artifact bytes.
    directory = root / "exposures" / first.exposure.artifact_id
    manifest = json.loads((directory / "manifest.json").read_text())
    table = next(item for item in manifest["tables"] if item["name"] == "time_bins")
    path = directory / table["relative_path"] / "part-000000.parquet"
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        run_project(root, config, name="Reject corrupt cached exposure", **kwargs)
    assert len(calls) == 2
