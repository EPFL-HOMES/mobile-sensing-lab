import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from mobile_sensing.api import create_app
from mobile_sensing.datasets.inputs import load_input
from mobile_sensing.jobs import JobStore


def test_snapshots_are_immutable_and_roles_preserved(tmp_path):
    source = tmp_path / "population.csv"
    source.write_text("cell_id,residents\n001,10\n")
    root = tmp_path / "workspace"
    client = TestClient(create_app(root))
    response = client.post(
        "/api/v1/inputs/register",
        json={
            "path": str(source),
            "name": "Population",
            "role": "population",
            "source_crs": "EPSG:2056",
        },
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["preview"][0]["cell_id"] == "001"
    source.write_text("changed")
    _, snapshot = load_input(root, value["input_id"])
    assert snapshot.read_text() == "cell_id,residents\n001,10\n"
    snapshot.write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        load_input(root, value["input_id"])
    assert len(client.get("/api/v1/inputs?role=population").json()) == 1
    assert client.get("/api/v1/inputs?role=network").json() == []


def test_geographic_metadata_and_uploaded_file(tmp_path):
    source = tmp_path / "boundary.geojson"
    gpd.GeoDataFrame(geometry=[box(6, 46, 6.1, 46.1)], crs=4326).to_file(source)
    client = TestClient(create_app(tmp_path / "workspace"))
    response = client.post(
        "/api/v1/inputs/upload",
        data={"name": "Region", "role": "boundary"},
        files={"file": (source.name, source.read_bytes())},
    )
    assert response.status_code == 201, response.text
    assert response.json()["source_crs"] == "EPSG:4326"
    assert (
        client.post(
            "/api/v1/inputs/register",
            json={
                "path": str(source),
                "name": "Wrong",
                "role": "boundary",
                "source_crs": "EPSG:2056",
            },
        ).status_code
        == 422
    )


def test_project_soft_delete_preserves_revisions_and_resources(tmp_path):
    client = TestClient(create_app(tmp_path))
    project = client.post("/api/v1/projects", json={"name": "Example copy"}).json()
    pid = project["project_id"]
    revision = client.post(
        f"/api/v1/projects/{pid}/revisions", json={"payload": {"source": "retained"}}
    ).json()
    store = JobStore(tmp_path)
    job, _ = store.submit(kind="test_probe", operation="active", payload={}, project_id=pid)
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 409
    store.request_cancel(job.job_id)
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 204
    assert client.get("/api/v1/projects").json() == []
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404
    assert client.get(f"/api/v1/projects/{pid}/revisions/{revision['revision_id']}").json()[
        "payload"
    ] == {"source": "retained"}
