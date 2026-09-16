# Documentation

## Start here

- [Project README](../README.md): installation, startup, examples, and release assets.
- [Quickstart](QUICKSTART.md): first project, simulation, portfolio analysis, and export.
- [Lausanne example](LAUSANNE_EXAMPLE.md): data, fleet assumptions, and interpretation.
- [San Francisco example](SAN_FRANCISCO_EXAMPLE.md): OSM inputs, synthetic operating assumptions, and interpretation.
- [Notebook guide](../notebooks/README.md): executable tutorials and kernel setup.

## Scientific and technical reference

Read these documents in order when reviewing or changing scientific behavior:

1. [Mobile sensing framework](MOBILE_SENSING_FRAMEWORK.tex) — frozen scientific framework.
2. [Architecture](ARCHITECTURE.md) — components, dependency direction, persistence, and jobs.
3. [Interfaces and data contracts](INTERFACES.md) — serialized fields, artifact tables, HTTP routes, and versioning.
4. [Simulation specification](SIMULATION_SPECIFICATION.md) — event, routing, operating, random-stream, and exposure semantics.
5. [Data ingestion specification](DATA_INGESTION_SPECIFICATION.md) — imports, geographic preparation, OSM, GTFS, and service areas.
6. [Portfolio specification](PORTFOLIO_SPECIFICATION.md) — count enumeration, vehicle sampling, utility, statistics, and frontiers.
7. [User-interface specification](UI_SPECIFICATIONS.md) — supported browser workflow and display semantics.

`INTERFACES.md` owns serialized names. The scientific specifications own their mathematical meaning. The architecture owns package and process boundaries.

## Development and release

- [Contributing](../CONTRIBUTING.md): environment, generated contracts, tests, and pull requests.
- [Implementation plan](IMPLEMENTATION_PLAN.md): current public roadmap and acceptance conditions.
- [Implementation status](IMPLEMENTATION_STATUS.md): current release state and limitations.
- [Validation](VALIDATION.md): checks executed for the release candidate.
- [Mapping templates](templates/): strict example inputs and mappings used by the application and tests.

Historical milestone reports, browser captures, machine-specific logs, and acceptance workspaces are retained outside the public documentation tree. Test-owned regression baselines are stored under `tests/v2/fixtures/`.
