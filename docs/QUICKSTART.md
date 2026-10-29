# Quickstart

This guide covers the browser App after installation. The release wheel already contains both city examples. Start with the [installation and platform guide](INSTALLATION.md): macOS/Linux users run the App locally; Windows users can try the WSL2 route. Native Windows Python-only users should use that guide's headless example instead of this browser workflow.

## 1. Launch and open an example

Open Terminal on macOS/Linux, or Ubuntu on Windows with WSL2. If you used the README installation folder, enter:

```bash
cd ~/mobile-sensing-lab
source .venv/bin/activate
mobile-sensing launch --artifact-root ./project --port 8820
```

For WSL2, add `--no-browser` and open `http://localhost:8820/` in your Windows browser. For a source installation, enter your source directory instead of `~/mobile-sensing-lab`.

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

For an empirical OD distribution, choose **Data → Demand / OD distribution** and register a CSV or Parquet file with `origin_cell_id`, `destination_cell_id`, and `weight`. Cell IDs must match the prepared grid; these table columns do not require a CRS. Weights are nonnegative relative frequencies with positive total mass, and each OD pair must occur once. Registration stores the file but does not activate it. In **Fleet Configuration → Demand**, choose **Generate tasks**, **Origin–destination**, then select the named file under **Spatial distribution → Demand spatial source**. The saved file name is shown and the independent origin/destination feature controls are hidden. To use those controls again, explicitly choose **Independent origin and destination distributions**. Task volume and hourly release settings remain separate; one OD file supplies the same pair distribution throughout the day.

Use **Environment → Grid → Grid filter** to add filter rows. Each row selects a prepared spatial feature or **Road network**, then chooses **Any of (OR)** or **Must include**. A retained cell satisfies every Must include row and at least one Any of row when that group is present. For example, add residential and commercial features as Any of rows, then Road network as Must include: a cell needs residential or commercial activity, and a road. Feature values must be strictly positive; roads require positive-length overlap, not point-only contact. For inhabited cells with roads, use a prepared population feature and Road network as two Must include rows. Residential geometry is a proxy and does not establish occupancy. No rows keep the full grid. Python calculates the retained count. Saving the project preserves the filter without rebuilding roads or features; historical filters keep their previous meaning.

The filter applies to generated demand origins/destinations, generated initial locations and synthetic depot weighting, and to sensing exposure and downstream portfolios. Feature mixtures are normalized over the retained grid before routing eligibility. Explicit cell-ID inputs must refer to retained, resolvable cells; imported coordinate-based operations and the complete road network retain their declared locations/routes. Exposure and coverage additionally exclude cells without a positive-length road intersection. A changed filter requires a new simulation run; Portfolio uses the filter saved with its selected source run, including its utility weights and coverage denominator. Existing runs remain unchanged.

Repeated runs with identical exposure settings reuse checksum-verified complete exposure as well as mobility. Changing the filter, sensing states or reporting bins invalidates the relevant cached output. Disposable cache indexes can be removed without deleting scientific results.

## 4. Validate and run simulation

Edit the duplicated configuration, then choose **Validate**. Validation checks configuration consistency but does not generate every task or route. Choose **Run** to perform full resolution, routing, simulation, and exposure construction.

Simulation replications `R` represent operational variability. Every fleet is simulated jointly within the same replication, and the physical vehicle catalog remains fixed across replications. Start with a small `R` when testing a new configuration.

Heatmap colours initially stop at the 95th percentile of nonzero values so isolated extreme values do not flatten the remaining range. Use **Color upper limit** above a map to select **Custom upper limit** or **Data maximum**. Custom duration limits use the displayed minutes. The legend labels the highest colour as greater than or equal to the limit when larger values share it; changing the scale changes presentation only.

The retained Lausanne case currently measures approximately 57 seconds per full-day replication on the recorded reference machine. This measures simulation computation, excluding input preparation, result publication and exposure allocation; the complete Run action includes those stages. Reproduction commands, timing samples and scientific-equivalence checks are recorded in [Implementation status](IMPLEMENTATION_STATUS.md).

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
