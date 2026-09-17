# Input templates

These are editable Python/CLI input examples, not complete App project exports or calibrated datasets. Copy a template before editing it. Files in `tests/fixtures/` instead belong to automated verification.

| Template | Where to use it | Values to review or replace |
|---|---|---|
| [location_tasks.csv](uploads/location_tasks.csv) and [mapping](uploads/location_tasks.mapping.json) | Location-demand normalization | Task/fleet IDs, coordinates, CRS, times and service durations |
| [od_tasks.csv](uploads/od_tasks.csv) and [mapping](uploads/od_tasks.mapping.json) | Origin–destination demand normalization | Origins/destinations, fleet IDs, units and time interpretation |
| [ordered_tasks.csv](uploads/ordered_tasks.csv) and [mapping](uploads/ordered_tasks.mapping.json) | Ordered multi-step demand normalization | Task IDs, contiguous step indices, locations and schedules |
| [sparse_od_rates.csv](uploads/sparse_od_rates.csv) and [mapping](uploads/sparse_od_rates.mapping.json) | Rate-driven demand generation | OD IDs, half-open intervals, rates and units |
| [vehicles.csv](uploads/vehicles.csv) and [mapping](uploads/vehicles.mapping.json) | Physical-vehicle supply normalization | Vehicle/fleet IDs, availability, initial locations and capacity |
| [vehicle_area_assignments.csv](uploads/vehicle_area_assignments.csv) and [mapping](uploads/vehicle_area_assignments.mapping.json) | Vehicle-to-service-area assignment | Vehicle and area IDs matching independently registered inputs |
| [environment/lausanne_environment_repaired.json](environment/lausanne_environment_repaired.json) | `prepare-environment --config` / environment services | Catalog references, municipalities, CRS, grid, travel times and repair policy |
| [gtfs/lausanne_route_13.json](gtfs/lausanne_route_13.json) | `reconstruct-gtfs --config` | Route IDs, service date, routing profile and limits |
| [simulation/lausanne_smoke.json](simulation/lausanne_smoke.json) | `lausanne-smoke --config`; small local-data workflow | Local Lausanne inputs, route/date, duration, replications and resource limits |
| [simulation/lausanne_research.json](simulation/lausanne_research.json) | Same workflow with research-scale settings | Review workload and assumptions before running |
| [portfolio/lausanne_counts.json](portfolio/lausanne_counts.json) | Portfolio preview/evaluation configuration | Actual completed exposure ID, fleet IDs, counts, costs, budgets and sampling |
| [portfolio/lausanne_uniform_weights.json](portfolio/lausanne_uniform_weights.json) | Portfolio evaluation `--weights` | Weight ID matching the portfolio configuration and weighting assumptions |

The Lausanne configurations need matching local inputs; they do not download the original dataset. Ready-made city projects are separate [Release examples](../../README.md#example-projects).

## Use an upload mapping

1. Copy the CSV and its same-stem `.mapping.json` into your working directory.
2. Replace sample rows and update every column association and semantic setting. Preserve string identifiers and leading zeros.
3. Register the input, then pass its mapping to the corresponding normalization service or CLI command. See [upload semantics](uploads/README.md) and the [data specification](../DATA_INGESTION_SPECIFICATION.md).

Filenames describe function; schema versions and algorithm IDs define contracts. Renaming files does not change these identifiers or authorize inferring missing units, CRS, assignments or time semantics.
