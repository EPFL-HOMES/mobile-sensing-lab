import pytest
from mobile_sensing.application.run_pipeline import run_project
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.example_build import BuildCancellation, BuildProgress
from mobile_sensing.application.resource_tables import read_table
from mobile_sensing.contracts import ExecutionOptions
from tests.application.test_runs_analysis import config_fixture
from tests.application.test_headless_application import _prepared, _upload_inputs, _bundle


def test_dispatch_bounds_preserve_exact_execution_and_route_totals(tmp_path, monkeypatch):
    app, environment = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(app, environment, tmp_path)
    validated = app.validate_scenario(environment, _bundle(environment, demand, supply, locations))
    router = environment.routing
    bounds = router.travel_time_lower_bounds_from
    nodes = tuple(sorted(environment.nodes.node_id))
    for source in nodes:
        values = bounds("constant_2mps", source, nodes)
        costs = router.travel_times_from("constant_2mps", source, nodes)
        for target in nodes:
            route = router.route("constant_2mps", source, target)
            assert router.reachable(source, target) == route.reachable
            if route.reachable:
                assert values[target] <= route.total_duration_s
                assert costs[target] == route.total_duration_s
            else:
                assert costs[target] is None
    options = ExecutionOptions(memory_limit_bytes=1024**3)
    optimized = app.run_simulation(validated, options)
    monkeypatch.setattr(router, "travel_time_lower_bounds_from", None)
    exhaustive = app.run_simulation(validated, options)
    assert optimized.results == exhaustive.results


def test_dispatch_comparison_reuses_realizations_without_generators(tmp_path, monkeypatch):
    root, config = config_fixture(tmp_path)
    kwargs = dict(
        options=RunOptions(),
        cancellation=BuildCancellation(root),
        progress=BuildProgress(root),
        source_revision_id=None,
    )
    first = run_project(root, config, name="Sequential", **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Comparison resampled a realization")

    monkeypatch.setattr(
        "mobile_sensing.application.project_resolution.generate_daily_tasks", forbidden
    )
    fleet = config.fleets[0]
    compared = config.model_copy(
        update={
            "fleets": (
                fleet.model_copy(
                    update={"dispatch": fleet.dispatch.model_copy(update={"mode": "batch"})}
                ),
            )
        }
    )
    second = run_project(root, compared, name="Batch", replay_from=first, **kwargs)
    assert second.realization_source_run_id == first.run_id
    for table in ("realized_tasks", "availability", "physical_catalog", "replication_plans"):
        assert read_table(root, first.resolution, table).equals(
            read_table(root, second.resolution, table)
        )
    changed = compared.model_copy(
        update={"simulation": compared.simulation.model_copy(update={"seed": 123})}
    )
    with pytest.raises(ValueError, match="retain demand"):
        run_project(root, changed, name="Invalid comparison", replay_from=first, **kwargs)


def test_offline_bundle_worker_fresh_project_copy_and_checksum(tmp_path, monkeypatch):
    import zipfile
    from fastapi.testclient import TestClient
    from mobile_sensing.api import create_app
    from mobile_sensing.application.example_bundle import ExampleBundle, BundleFile, install_example
    from mobile_sensing.application.example_build import digest_file
    from mobile_sensing.jobs import JobStore, LocalCoordinator

    root, config = config_fixture(tmp_path / "source")
    run = run_project(
        root,
        config,
        name="Analytic retained run",
        source_revision_id=None,
        options=RunOptions(),
        cancellation=BuildCancellation(root),
        progress=BuildProgress(root),
    )
    latest = run_project(
        root,
        config,
        name="Latest analytic run",
        source_revision_id=None,
        options=RunOptions(),
        cancellation=BuildCancellation(root),
        progress=BuildProgress(root),
    )
    directory = tmp_path / "bundle"
    directory.mkdir()
    evidence = root / "example_evidence" / "evidence_hourly"
    evidence.mkdir(parents=True)
    (evidence / "example-analysis.json").write_text('{"resolution_minutes":60}')
    files = tuple(
        BundleFile(
            path=str(path.relative_to(root)),
            sha256=digest_file(path),
            size_bytes=path.stat().st_size,
        )
        for collection in (
            "inputs",
            "environments",
            "datasets",
            "simulations",
            "exposures",
            "example_evidence",
        )
        for path in sorted((root / collection).rglob("*"))
        if path.is_file()
    )
    bundle = ExampleBundle(
        bundle_id="example_fixture",
        name="Analytic example",
        config=config.model_copy(update={"linked_run_ids": (run.run_id,)}),
        files=files,
        saved_views={"Operations": "/results?view=operations"},
        description="Analytic bundle fixture, not Lausanne evidence",
    )
    (directory / "lausanne.json").write_text(bundle.model_dump_json())
    with zipfile.ZipFile(directory / "lausanne.zip", "w") as archive:
        for item in files:
            archive.write(root / item.path, item.path)
    monkeypatch.setenv("MOBILE_SENSING_EXAMPLE_DIRECTORY", str(directory))
    target = tmp_path / "fresh"
    (target / "example_evidence").mkdir(parents=True)
    historical_evidence = target / "example_evidence" / "example-analysis.json"
    historical_evidence.write_text('{"resolution_minutes":15}')
    client = TestClient(create_app(target))
    assert client.get("/api/v1/examples/lausanne").json()["available"]
    opened = client.post("/api/v1/examples/lausanne/open", json={"editable": False})
    assert opened.status_code == 200, opened.text
    with LocalCoordinator(target, max_workers=1) as coordinator:
        coordinator.run_once()
    job = JobStore(target).get_job(opened.json()["job_id"])
    assert job.status == "completed", job.error_message
    assert historical_evidence.read_text() == '{"resolution_minutes":15}'
    assert (
        target / "example_evidence" / "evidence_hourly" / "example-analysis.json"
    ).read_text() == '{"resolution_minutes":60}'
    finished = client.post(f"/api/v1/examples/lausanne/finalize/{job.job_id}")
    assert finished.status_code == 200, finished.text
    project = finished.json()
    assert {item["role"] for item in client.get("/api/v1/inputs").json()} >= {"boundary", "network"}
    assert (
        client.get("/api/v1/workbench/runs", params={"project_id": project["project_id"]}).json()[
            0
        ]["run_id"]
        == run.run_id
    )
    store = JobStore(target)
    revision = store.get_revision(project["project_id"], project["current_revision_id"])
    assert revision.payload["read_only"]
    with pytest.raises(ValueError, match="editable copy"):
        store.create_revision(
            project["project_id"], project["current_revision_id"], revision.payload
        )
    copied = client.post("/api/v1/examples/lausanne/open", json={"editable": True}).json()
    assert copied["job_id"] is None
    assert not store.list_revisions(copied["project_id"])[0].payload["read_only"]
    upgraded = bundle.model_copy(
        update={
            "bundle_id": "example_fixture_updated",
            "config": config.model_copy(update={"linked_run_ids": (latest.run_id,)}),
        }
    )
    (directory / "lausanne.json").write_text(upgraded.model_dump_json())
    opened = client.post("/api/v1/examples/lausanne/open", json={"editable": False}).json()
    assert opened["project_id"] == project["project_id"]
    with LocalCoordinator(target, max_workers=1) as coordinator:
        coordinator.run_once()
    finished = client.post(f"/api/v1/examples/lausanne/finalize/{opened['job_id']}")
    assert finished.status_code == 200, finished.text
    for identifier, expected in (
        (project["project_id"], latest.run_id),
        (copied["project_id"], run.run_id),
    ):
        visible = client.get("/api/v1/workbench/runs", params={"project_id": identifier}).json()
        assert [value["run_id"] for value in visible] == [expected]
    newest_copy = client.post("/api/v1/examples/lausanne/open", json={"editable": True}).json()
    assert store.list_revisions(newest_copy["project_id"])[0].payload["linked_run_ids"] == [
        latest.run_id
    ]
    # A changed bundle byte cannot be silently installed in another workspace.
    damaged = bundle.model_copy(
        update={"files": (files[0].model_copy(update={"sha256": "0" * 64}), *files[1:])}
    )
    (directory / "lausanne.json").write_text(damaged.model_dump_json())
    other = tmp_path / "damaged"
    with pytest.raises(ValueError, match="checksum mismatch"):
        install_example(other, cancellation=BuildCancellation(other), progress=BuildProgress(other))


def test_map_defaults_to_observation_clock_and_cached_environment_remains_verified(tmp_path):
    from mobile_sensing.api.queries import query_map
    from mobile_sensing.jobs import JobStoreLimits
    from pathlib import Path
    import json

    root, config = config_fixture(tmp_path)
    run = run_project(
        root,
        config,
        name="Observation window",
        source_revision_id=None,
        options=RunOptions(),
        cancellation=BuildCancellation(root),
        progress=BuildProgress(root),
    )
    manifest = json.loads(
        (root / "simulations" / run.simulation.artifact_id / "manifest.json").read_text()
    )
    replication = manifest["scientific_identity"]["resolved_config"]["replication_ids"][0]
    kwargs = dict(
        bbox=None,
        replication_id=replication,
        fleet_id=None,
        vehicle_id=None,
        time_start_s=None,
        time_end_s=None,
        limits=JobStoreLimits(),
        aggregation="edge_usage",
    )
    result = query_map(root, run.simulation.artifact_id, "movements", **kwargs)
    assert result["filters"]["time_start_s"] == 0
    assert result["filters"]["time_end_s"] == 86400
    assert query_map(root, run.simulation.artifact_id, "movements", **kwargs) == result
    environment = Path(root) / "environments" / config.prepared_environment.artifact.artifact_id
    grid = next(environment.rglob("grid_cells.parquet"))
    grid.write_bytes(grid.read_bytes() + b"altered")
    with pytest.raises(ValueError):
        query_map(root, run.simulation.artifact_id, "movements", **kwargs)
