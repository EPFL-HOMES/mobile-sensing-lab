# Upload templates and mappings

These templates are structural examples, not calibrated Lausanne inputs. Import mappings must record every source-column association, time and duration unit, CRS for coordinate layouts, quantity semantics/unit, and any zero service-duration default. Identifiers are strings; preserve leading zeros. `quantity` is one task quantity, never a row multiplier.

- `location_tasks.csv`: one service location per task.
- `od_tasks.csv`: one origin-destination task per row.
- `ordered_tasks.csv`: one row per ordered task step; indices are one-based and contiguous.
- `sparse_od_rates.csv`: a rate model, not observed task rows; intervals are half-open and rates require an explicit unit.
- `vehicles.csv`: the fixed physical catalog and baseline availability.
- `vehicle_area_assignments.csv`: separate vehicle-area foreign keys. Areas must come from an independently registered area-definition artifact; depot location never implies area assignment.

Each CSV has a same-stem `.mapping.json` example. The mappings are strict version-2 contracts accepted by the CLI, Python application service, and HTTP job adapter. Replace fleet and dataset-specific values explicitly; do not infer units, CRS, time semantics, capacity mode, or quantity interpretation from column names.

Timestamps in timestamp mappings must carry a UTC offset. The service-clock mapping requires an explicit service date, IANA timezone, and UTC origin. CSV and Parquet importers apply identical validation and publish only complete immutable artifacts.
