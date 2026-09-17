import geopandas as gpd
from shapely.geometry import box
from fastapi.testclient import TestClient
from mobile_sensing.api import create_app
from mobile_sensing.jobs import JobStore
from mobile_sensing.jobs.worker import execute_job


def test_region_search_worker_result_is_bounded_and_cached_input_is_preserved(
    tmp_path, monkeypatch
):
    calls = []

    def acquire(root, **kwargs):
        calls.append(kwargs)
        return gpd.GeoDataFrame(
            {"display_name": ["Analytic region"]}, geometry=[box(6.5, 46.5, 6.6, 46.6)], crs=4326
        ), {"retrieved_at_utc": "2026-09-14T00:00:00Z"}

    monkeypatch.setattr("mobile_sensing.application.region_search.acquire_osm", acquire)
    client = TestClient(create_app(tmp_path))
    submitted = client.post("/api/v1/workbench/region-search", json={"query": "Analytic region"})
    assert submitted.status_code == 202
    store = JobStore(tmp_path)
    job, token = store.claim_next("test-owner", 60)
    value = execute_job(
        str(tmp_path),
        job.resource_id,
        job.kind,
        store.payload(job.job_id, token),
        str(tmp_path / "cancel"),
        str(tmp_path / "progress"),
    )
    store.complete(job.job_id, token, value)
    response = client.get(f"/api/v1/workbench/region-search/{job.resource_id}")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["name"] == "Analytic region" and result["boundary"]["crs"] == "EPSG:4326"
    assert result["boundary"]["features"][0]["geometry"]["type"] == "Polygon"
    assert calls[0]["operation"] == "boundary"
    assert len(client.get(f"/api/v1/jobs/{job.job_id}/events").text) > 0
