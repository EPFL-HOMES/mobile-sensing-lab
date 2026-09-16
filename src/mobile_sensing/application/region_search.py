"""Cancellable named-region lookup with bounded map preview and raw cache receipt."""

import json
from shapely import get_num_coordinates
from mobile_sensing.environment.acquisition import acquire_osm
from mobile_sensing.application.studio_models import RegionSearchResult


def search_region(root, query, cancellation, progress):
    progress.update(phase="region.search", completed=0, total=1)
    frame, receipt = acquire_osm(
        root, operation="boundary", query=query.strip(), cancellation=cancellation
    )
    cancellation.raise_if_cancelled()
    if (
        frame.empty
        or not frame.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all()
        or not frame.geometry.is_valid.all()
    ):
        raise ValueError(
            "The region search returned no valid polygon; use a more specific place name"
        )
    display = frame.to_crs(4326)
    geometry = display.geometry.union_all()
    # Simplify preview only. Environment construction uses the cached full boundary.
    tolerance = 0.00001
    while get_num_coordinates(geometry) > 50000:
        geometry = geometry.simplify(tolerance, preserve_topology=True)
        tolerance *= 2
        cancellation.raise_if_cancelled()
    display = display.iloc[:1][["geometry"]].copy()
    display.geometry = [geometry]
    value = json.loads(display.to_json(drop_id=True))
    value["crs"] = "EPSG:4326"
    progress.update(phase="region.search", completed=1, total=1)
    return RegionSearchResult(
        query=query.strip(),
        name=str(frame.iloc[0].get("display_name", query)),
        boundary=value,
        source=f"OpenStreetMap / Nominatim; retrieved {receipt['retrieved_at_utc']}; full geometry retained in the OSM cache",
    )
