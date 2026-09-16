# Quickstart

This guide starts from an installed release with the matching Lausanne and San Francisco example archives. See the repository [README](../README.md) for installation.

## 1. Launch and open an example

```bash
source .venv/bin/activate
mobile-sensing launch --artifact-root ./project --port 8820
```

Open the Project page. On a new workspace, the application imports both read-only examples in background jobs. Wait until their status is ready, then open either example. If the examples do not appear, verify that each JSON manifest is beside its matching ZIP archive or that `MOBILE_SENSING_EXAMPLE_DIRECTORY` points to that directory.

The examples contain retained simulation and portfolio results, so inspection does not recompute the full experiments.

## 2. Inspect retained results

Open **Fleet results** to inspect mean spatial coverage, road sensing, daily sensing/task/activity profiles, and fleet outcomes. The coverage denominator is the eligible road-intersecting grid, not only cells with positive exposure.

Open **Portfolio results** to compare feasible fleet sensor-count portfolios. Mean and P05 are empirical statistics over sampled installations and retained operational replications. P05 is not a guaranteed minimum.

## 3. Create an editable project

Return to Project and choose **Duplicate**. The copied project has its own name and configuration history. Reference examples remain read-only.

The main workflow is:

```text
Project → Data → Environment → Fleet → Simulation → Portfolio → Results
```

Data registers immutable snapshots. Environment prepares the boundary, reporting grid, directed road network, travel times, and spatial features. Numeric source features are aggregated to the prepared grid before demand, supply, or utility weighting uses them.

## 4. Validate and run simulation

Edit the duplicated configuration, then choose **Validate**. Validation checks configuration consistency but does not generate every task or route. Choose **Run** to perform full resolution, routing, simulation, and exposure construction.

Simulation replications `R` represent operational variability. Every fleet is simulated jointly within the same replication, and the physical vehicle catalog remains fixed across replications. Start with a small `R` when testing a new configuration.

Long work runs in a separate local worker. You may navigate away, return to the job, or request cancellation. A cancelled or failed job does not publish a complete scientific artifact.

## 5. Evaluate a sensor portfolio

Portfolio configuration selects:

- a completed simulation result;
- the number of fleet-sampling rounds `J`;
- budget levels and cost units;
- one unit cost and sensor-count range per fleet;
- utility function, saturation parameter, temporal aggregation, and spatial weights.

Each sampling round draws one retained joint replication and uniformly samples concrete vehicles from each complete fleet catalog. Utility is evaluated on that sampled exposure before summary statistics are computed. Changing `J`, sensor counts, costs, or budgets does not rerun mobility.

## 6. Export and preserve the project

Use **Project → Files** to inspect the managed project folder. Use **Export complete project** for a portable ZIP containing the project configuration, transitive inputs, immutable results, and provenance. Import that ZIP into another workspace to reproduce the same retained artifacts.

Do not edit immutable input or result files directly. Register changed data or save a new configuration revision through the application.

## Working with your own data

You can create a project without the example archives. Supported inputs include service-area boundaries, directed road networks or OSM acquisition, travel-time data, numeric spatial features, vehicle catalogs, demand records or rates, assignments, and GTFS feeds. Use the strict examples in [templates](templates/) and the [data-ingestion specification](DATA_INGESTION_SPECIFICATION.md).

Large live OSM acquisitions depend on public services. For reproducible regional studies, retain acquired inputs or register local boundary and road files.
