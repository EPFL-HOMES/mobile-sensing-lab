"""Bounded checks for city identity and notebook/default consistency."""

import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from mobile_sensing.api.workspace import create_workspace_app
from mobile_sensing.application.example_build import demonstration_fleets
from mobile_sensing.application.example_bundle import ExampleBundle, install_example
from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.application.example_build import BuildCancellation, BuildProgress


def test_two_city_references_use_independent_owned_stores(tmp_path, monkeypatch):
    directory = tmp_path / "bundles"
    directory.mkdir()
    for key, city in (("lausanne", "Lausanne"), ("san-francisco", "San Francisco")):
        bundle = ExampleBundle(
            bundle_id=f"example_{key}",
            example_key=key,
            name=f"[Example] {city}",
            description="Identity fixture only",
            config=ProjectConfig(),
            files=(),
            saved_views={},
        )
        (directory / f"{key}.json").write_text(bundle.model_dump_json())
        with zipfile.ZipFile(directory / f"{key}.zip", "w"):
            pass
    monkeypatch.setenv("MOBILE_SENSING_EXAMPLE_DIRECTORY", str(directory))
    app = create_workspace_app(tmp_path / "project")
    client = TestClient(app)
    for key, city in (("lausanne", "Lausanne"), ("san-francisco", "San Francisco")):
        info = client.get(f"/api/v1/examples/{key}")
        assert info.status_code == 200
        assert info.json()["name"] == f"[Example] {city}"
        private = tmp_path / "project" / f"[Example] {city}" / ".system"
        install_example(
            private,
            example_key=key,
            cancellation=BuildCancellation(private),
            progress=BuildProgress(private),
        )
    initialized = client.post("/api/v1/workbench/workspace/initialize")
    assert initialized.status_code == 200, initialized.text
    value = initialized.json()
    assert len(set(value["example_project_ids"])) == 2
    assert not value["example_job_ids"]
    again = client.post("/api/v1/workbench/workspace/initialize").json()
    assert again["example_project_ids"] == value["example_project_ids"]
    reference_id = value["example_project_ids"][0]
    app.state.workspace.rename(reference_id, "Lausanne legacy reference", "")
    renamed = client.get("/api/v1/examples/lausanne")
    assert renamed.status_code == 200
    assert (
        app.state.workspace.store(reference_id).get_project(reference_id).name
        == "[Example] Lausanne"
    )
    records = client.get("/api/v1/projects").json()
    assert {r["name"] for r in records} == {"[Example] Lausanne", "[Example] San Francisco"}
    for key in ("lausanne", "san-francisco"):
        copied = client.post(f"/api/v1/examples/{key}/open", json={"editable": True})
        assert copied.status_code == 200, copied.text
        assert copied.json()["project_id"] not in value["example_project_ids"]


def test_tutorial_defaults_match_five_fleet_example():
    from types import SimpleNamespace
    from mobile_sensing.application import project_models as models

    fleets = demonstration_fleets("input_fixture")
    scope = {
        name: getattr(models, name)
        for name in (
            "FleetEditor",
            "DemandEditor",
            "SupplyEditor",
            "DispatchEditor",
            "ShiftGroup",
            "TemporalInterval",
            "ProjectConfig",
        )
    }
    config = ProjectConfig(fleets=fleets)
    scope["source_run"] = SimpleNamespace(config=config)
    cells = json.loads(Path("notebooks/lausanne_simulation_tutorial.ipynb").read_text())["cells"]
    for index in (3, 5, 7):
        exec("".join(cells[index]["source"]), scope)
    assert scope["configuration"].fleets == fleets
    assert [fleet.fleet_id for fleet in fleets[:3]] == ["bus_1", "bus_3", "bus_7"]
    assert len({fleet.demand.route_ids for fleet in fleets[:3]}) == 3
    assert all(not c.get("outputs") for c in cells)


def test_osm_shape_contraction_preserves_directed_curved_geometry(monkeypatch, tmp_path):
    import geopandas as gpd
    from shapely.geometry import LineString, box
    from mobile_sensing.application.san_francisco_example import simplify_osm_roads

    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "mpl"))
    coordinates = [(0, 0), (10, 10), (20, 0), (30, 0)]
    edges = gpd.GeoDataFrame(
        {
            "u": [1, 2, 3],
            "v": [2, 3, 4],
            "key": [0, 0, 0],
            "one_way": [True] * 3,
            "maxspeed": ["20 mph"] * 3,
            "highway": ["residential"] * 3,
        },
        geometry=[LineString(coordinates[i : i + 2]) for i in range(3)],
        crs=32610,
    )
    grid = gpd.GeoDataFrame(geometry=[box(-1, -1, 1, 1)], crs=32610)
    contracted, report = simplify_osm_roads(edges, grid)
    assert len(contracted) == 1
    assert contracted.iloc[0].u == 1 and contracted.iloc[0].v == 4
    assert list(contracted.iloc[0].geometry.coords) == coordinates
    assert abs(report["length_residual_m"]) < 1e-9
    # An interior node selected by a reporting cell must remain a snapping endpoint.
    grid = gpd.GeoDataFrame(geometry=[box(-1, -1, 1, 1), box(9, 9, 11, 11)], crs=32610)
    preserved, _ = simplify_osm_roads(edges, grid)
    assert 2 in set(preserved.u) | set(preserved.v)


def test_packaging_preserves_requested_city_name(tmp_path):
    from mobile_sensing.application.example_build import demonstration_portfolio
    from mobile_sensing.application.example_bundle import write_bundle
    from mobile_sensing.application.run_pipeline import run_project, run_analysis
    from mobile_sensing.application.run_models import RunOptions
    from tests.v2.test_mr05_runs_analysis import config_fixture

    root, config = config_fixture(tmp_path / "source")
    config = config.model_copy(
        update={"simulation": config.simulation.model_copy(update={"replications": 10})}
    )
    kwargs = dict(
        options=RunOptions(),
        source_revision_id=None,
        cancellation=BuildCancellation(root),
        progress=BuildProgress(root),
    )
    run = run_project(root, config, name="Analytic city fixture", **kwargs)
    analysis = run_analysis(
        root, demonstration_portfolio(run), name="Analytic portfolio fixture", **kwargs
    )
    (root / "example-run.json").write_text(run.model_dump_json())
    (root / "example-analysis.json").write_text(analysis.model_dump_json())
    for name in ("input-provenance.json", "example-audit.json"):
        (root / name).write_text('{"fixture":true}')
    expected = "[Example] San Francisco identity fixture"
    bundle = write_bundle(root, tmp_path / "bundle", example_key="san-francisco", name=expected)
    assert bundle.name == expected
    assert bundle.example_key == "san-francisco"
    assert json.loads((tmp_path / "bundle/san-francisco.json").read_text())["name"] == expected
