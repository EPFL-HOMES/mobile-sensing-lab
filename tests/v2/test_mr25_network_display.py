"""Network rendering retains branches, long chains and real disconnected components."""

import geopandas as gpd
from shapely.geometry import LineString, shape
from shapely.ops import unary_union

from mobile_sensing.api.workbench_maps import complete_road_display


def displayed_union(frame, tolerance=0):
    features, count = complete_road_display(frame, tolerance_m=tolerance)
    return unary_union([shape(item["geometry"]) for item in features]), count


def test_complete_network_over_ten_thousand_edges_is_not_subsampled():
    # A previously subsampled chain would have thousands of visible gaps.
    count = 10011
    lines = [LineString([(i * 10, 0), ((i + 1) * 10, 0)]) for i in range(count)]
    frame = gpd.GeoDataFrame(
        geometry=lines + [LineString(list(line.coords)[::-1]) for line in lines], crs=3857
    )
    displayed, display_count = displayed_union(frame)
    assert display_count == 1
    assert displayed.geom_type == "LineString"
    expected = gpd.GeoSeries([LineString([(0, 0), (count * 10, 0)])], crs=3857).to_crs(4326).iloc[0]
    assert displayed.hausdorff_distance(expected) < 1e-6
    assert len(frame) == count * 2


def test_reverse_lines_deduplicate_without_joining_crossings_or_islands():
    lines = [
        LineString([(0, 0), (100, 0)]),
        LineString([(100, 0), (0, 0)]),
        LineString([(50, -50), (50, 50)]),
        LineString([(1000, 0), (1100, 0)]),
    ]
    frame = gpd.GeoDataFrame(geometry=lines, crs=3857)
    features, count = complete_road_display(frame, tolerance_m=0)
    assert count == 3
    coordinates = features[0]["geometry"]["coordinates"]
    assert all(len(line) == 2 for line in coordinates)
    shuffled, _ = complete_road_display(frame.iloc[::-1], tolerance_m=0)
    assert shuffled == features


def test_simplification_retains_branch_junction():
    frame = gpd.GeoDataFrame(
        geometry=[
            LineString([(0, 0), (50, 1), (100, 0)]),
            LineString([(100, 0), (150, 1), (200, 0)]),
            LineString([(100, 0), (100, 100)]),
        ],
        crs=3857,
    )
    features, count = complete_road_display(frame, tolerance_m=2)
    assert count == 3
    endpoints = [
        tuple(point)
        for line in features[0]["geometry"]["coordinates"]
        for point in (line[0], line[-1])
    ]
    junction = tuple(
        round(x, 6)
        for x in gpd.GeoSeries.from_xy([100], [0], crs=3857).to_crs(4326).iloc[0].coords[0]
    )
    assert endpoints.count(junction) == 3
