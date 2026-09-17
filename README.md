# Mobile Sensing Simulator

Mobile Sensing Simulator is a local research application for simulating vehicle fleets and comparing mobile-sensor portfolios by spatial coverage, expected utility, and lower-tail performance. It provides a browser interface, Python package, and command-line tools backed by the same deterministic scientific implementation.

The project is a working research release. Its calibrated scope is explicit: the bundled city examples use synthetic operating assumptions, inferred vehicle duties, assumed road speeds, and in-sample portfolio analysis. They demonstrate the workflow rather than estimate city-wide service performance.

## Recommended installation

The supported runtime is Python 3.12. The validated desktop platform is macOS on Apple Silicon. Linux requires POSIX file locking and has less browser coverage; native Windows launching is not supported.

For ordinary use, download the wheel from [GitHub Releases](https://github.com/EPFL-HOMES/mobile-sensing-lab/releases). A release wheel contains the built browser interface and both example projects; it does not require Node.js.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install 'mobile_sensing-0.1.0-py3-none-any.whl[web,optimization,geography,notebook]'
mobile-sensing --help
```

For development from a source checkout, Node.js 22.12–22.x and npm are also required:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web,optimization,geography,notebook]'
npm --prefix frontend ci
npm --prefix frontend run build
```

Register the environment once if you will use the tutorials:

```bash
python -m ipykernel install --user --name mobile-sensing --display-name 'Mobile Sensing (Python 3.12)'
```

Installation, live OpenStreetMap acquisition, and satellite basemaps require internet access. Bundled scientific results remain available offline.

## Start the application

From the repository or release directory:

```bash
source .venv/bin/activate
mobile-sensing launch --artifact-root ./project --port 8820
```

The application opens at `http://127.0.0.1:8820/`. Keep the terminal open and press `Ctrl+C` to stop. Reuse the same `project/` directory to retain projects.

On macOS, a source checkout can instead launch by double-clicking [Start Mobile Sensing.command](Start%20Mobile%20Sensing.command). Its first run creates a separate `.app-venv`, installs dependencies, and builds the browser interface. Python 3.12, Node.js, npm, and internet access must already be available.

## Example projects

When both matching example archives are installed, the Project page imports two read-only projects on first visit:

- **[Example] Lausanne — Five-Fleet Weekday**: Bus lines 1, 3, and 7, Postal, and Ride-hailing over the Lausanne study region. Postal uses four demand-balanced automatic service areas.
- **[Example] San Francisco — Delivery Vans and Taxis**: synthetic delivery and taxi fleets over a bounded OSM-derived study area. Delivery vans use four automatic service areas based on a normalized residential/commercial mixture.

Choose **Duplicate** before editing an example. The application creates its own `project/` workspace; the repository does not distribute a pre-created workspace.

A Git checkout or GitHub-generated source archive contains the small manifests but excludes the large ZIP files. Download these matching pairs from the [same GitHub Release](https://github.com/EPFL-HOMES/mobile-sensing-lab/releases/tag/v0.1.0) and place them together in `src/mobile_sensing/_examples/`, or set `MOBILE_SENSING_EXAMPLE_DIRECTORY` to their directory. The release wheel and packaged source distribution already include them:

```text
lausanne.json
lausanne.zip
san-francisco.json
san-francisco.zip
```

Without the archives, the application remains usable with your own data and reports that the offline examples are unavailable.

## First workflow

1. Open **Project** and wait for both example imports to finish.
2. Open an example to inspect its retained Fleet and Portfolio results.
3. Choose **Duplicate**, then edit the copied Environment or Fleet settings.
4. Use **Validate** for configuration checks. **Run** performs full task generation, routing, simulation, and exposure construction.
5. Inspect Fleet results, then configure costs, budgets, fleet sensor-count ranges, and sampling rounds in Portfolio.
6. Export a complete project ZIP to preserve its inputs, configuration, results, and provenance.

Operational replications `R` generate joint fleet operations. Portfolio sampling rounds `J` reuse those retained realizations while sampling concrete physical vehicles. Increasing `J` does not rerun mobility. Sensor counts never change the operational vehicle supply.

See [Quickstart](docs/QUICKSTART.md) for the detailed workflow, [Lausanne example](docs/LAUSANNE_EXAMPLE.md) and [San Francisco example](docs/SAN_FRANCISCO_EXAMPLE.md) for assumptions, and the [documentation index](docs/README.md) for scientific and developer references.

## Tutorials

Select the **Mobile Sensing (Python 3.12)** Jupyter kernel and run cells in order:

- [lausanne_simulation_tutorial.ipynb](notebooks/lausanne_simulation_tutorial.ipynb): the joint five-fleet example, results, and portfolio frontiers.
- [ridehailing_simulation_tutorial.ipynb](notebooks/ridehailing_simulation_tutorial.ipynb): a small full-region ride-hailing run and ten-vehicle diagnostics.
- [postal_simulation_tutorial.ipynb](notebooks/postal_simulation_tutorial.ipynb): a small delivery run with automatic service areas and ten-vehicle diagnostics.

The source notebooks contain no saved execution outputs. They use temporary workspaces and remove them after successful completion.

## Development

```bash
poetry install --all-extras
python -m pytest -q tests/v2
npm --prefix frontend test
npm --prefix frontend run build
poetry build
```

The geographic regression tests additionally require the immutable local Lausanne inputs, which are not distributed in the source repository. See [CONTRIBUTING.md](CONTRIBUTING.md) for generated contracts, test boundaries, and release checks.

<details>
<summary>Prompt for an AI coding assistant</summary>

```text
Set up and launch Mobile Sensing Simulator from <PROJECT_FOLDER>. Read
README.md and AGENTS.md first. Preserve existing project, data, and result
directories. Use Python 3.12 in a local virtual environment. For a source
checkout, install the web, optimization, geography, and notebook extras,
install the locked frontend dependencies, and build the frontend. For a
release wheel, do not require Node.js. Verify mobile-sensing --help, then
launch the existing ./project workspace. Confirm whether the matching
Lausanne and San Francisco example manifest/archive pairs are installed;
do not recompute them. Report the local URL and whether both examples appear
on the Project page. Do not upload files or alter immutable inputs.
```

</details>

## Open work and limitations

- This is a local, single-user application without authentication, cloud deployment, or a distributed job queue.
- Large-region live OSM downloads depend on public Overpass availability and may require cached or local network data.
- Synthetic demand, inferred duties, uncalibrated speeds, greedy dispatch, and illustrative costs require independent calibration before policy use.
- macOS is the fully validated desktop platform; equivalent Linux browser coverage and native Windows support remain open.
- Large visualization bundles and first-query latency remain performance-maintenance items.

## License

The source code is distributed under the [GNU General Public License v3.0](LICENSE).
Release changes are recorded in [CHANGELOG.md](CHANGELOG.md). Research users can use the repository [citation metadata](CITATION.cff).
