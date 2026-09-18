# Mobile Sensing Lab

Mobile Sensing Lab is a local research application for simulating vehicle fleets and comparing mobile-sensor portfolios by spatial coverage, expected utility, and lower-tail performance. It provides a browser interface, Python package, and command-line tools backed by the same deterministic scientific implementation.

The project is a working research release. Its calibrated scope is explicit: the bundled city examples use synthetic operating assumptions, inferred vehicle duties, assumed road speeds, and in-sample portfolio analysis. They demonstrate the workflow rather than estimate city-wide service performance.

## Choose how to use it

Use **Python 3.12**. You can use the browser App or call the simulator from Python without opening the App.

| Your environment | Python simulation | Browser App and bundled notebooks |
|---|---|---|
| macOS | Validated | Full workflow validated on Apple Silicon |
| Linux | Automated tests pass on Ubuntu | Build and automated tests pass; desktop interaction coverage is narrower |
| Windows, native Python | Imports and a small synchronous simulation passed on Windows CI; see the [scope and setup](docs/INSTALLATION.md#windows-native-python) | Not currently supported: these workflows load Unix-only file locking |
| Windows with WSL2 | Uses the Linux environment | Documented route to try the full App; WSL2 end-to-end testing remains open |

**Windows does not make all project code unusable.** The limitation concerns the managed App and current tutorial adapters, not the entire scientific package. Installing a wheel successfully is not a guarantee that every workflow works on Windows. See the [exact platform boundaries](docs/INSTALLATION.md#platform-boundaries).

## Install and open the App

The release wheel includes the browser interface and both city examples. **Node.js, npm, Poetry, and Git are not needed to run the installed App.** Internet access is needed for installation; the wheel is about 322 MB, plus Python dependencies.

### macOS: enter these commands in Terminal

First install Python 3.12 using a [Python installer](https://www.python.org/downloads/release/python-31210/) or your existing Python environment manager. Open **Applications → Utilities → Terminal**. Copy the following commands there, one line at a time; do not enter them at a Python `>>>` prompt:

```bash
mkdir -p ~/mobile-sensing-lab
cd ~/mobile-sensing-lab
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "mobile-sensing[web,optimization,geography,notebook] @ https://github.com/EPFL-HOMES/mobile-sensing-lab/releases/download/v0.1.0/mobile_sensing-0.1.0-py3-none-any.whl"
mobile-sensing launch --artifact-root ./project --port 8820
```

The version check should print `Python 3.12.x`. If the browser does not open, visit **http://127.0.0.1:8820/**. Keep Terminal open while using the App; press **Ctrl+C** there to stop. Your projects are saved in `~/mobile-sensing-lab/project/`.

To open it again later, open Terminal and enter only:

```bash
cd ~/mobile-sensing-lab
source .venv/bin/activate
mobile-sensing launch --artifact-root ./project --port 8820
```

### macOS alternative: double-click the launcher

If you downloaded or cloned the **source repository**, you can launch it from Finder:

1. Install Python 3.12 and Node.js 22.12–22.x (including npm), and extract the repository if you downloaded a ZIP.
2. Open the extracted project folder in Finder and double-click [Start Mobile Sensing.command](Start%20Mobile%20Sensing.command).
3. Keep its Terminal window open. The first launch creates `.app-venv`, installs Python dependencies and builds the browser interface, then starts the App. This first setup needs internet access and can take several minutes.
4. For later sessions, double-click the same file again. Stop the App with **Ctrl+C** in its Terminal window.

This launcher belongs to the source repository; the installed-wheel route above does not need it. To see both city examples from a source checkout, install the four matching [example assets](#example-projects). If an extracted ZIP has lost executable permission, open Terminal in the project folder and run `chmod +x "Start Mobile Sensing.command"` before double-clicking again.

### Windows and Linux

- **Windows, full App:** follow [Windows with WSL2](docs/INSTALLATION.md#windows-with-wsl2). It explicitly separates commands entered in PowerShell from commands entered in Ubuntu.
- **Windows, Python only:** follow [native Windows Python setup](docs/INSTALLATION.md#windows-native-python), including the supported entry points and a small executable simulation check.
- **Ubuntu/Linux:** follow [Linux setup](docs/INSTALLATION.md#linux).
- **Editing the source:** follow [source installation](docs/INSTALLATION.md#install-from-source). This is where Node.js and npm are required for the browser interface.

See [installation troubleshooting](docs/INSTALLATION.md#troubleshooting) if a command fails, and [Quickstart](docs/QUICKSTART.md) once the App opens. Live OpenStreetMap acquisition and satellite basemaps require internet access; bundled results remain available offline.

## Example projects

When both matching example archives are installed, the Project page imports two read-only projects on first visit:

- **[Example] Lausanne — Bus, Postal and Taxi Weekday**: lines 1, 3, and 7 form one Bus fleet; fixed Postal demand and synthetic Taxi demand use explicit population/OSM feature mixtures.
- **[Example] San Francisco — Taxi Weekday**: one synthetic 100-vehicle taxi fleet over a bounded OSM-derived study area, with residential, commercial, transportation, public-service and leisure demand proxies.

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

- [lausanne_simulation_tutorial.ipynb](notebooks/lausanne_simulation_tutorial.ipynb): the joint Bus/Postal/Taxi example, results, and portfolio frontiers.
- [ridehailing_simulation_tutorial.ipynb](notebooks/ridehailing_simulation_tutorial.ipynb): a small full-region ride-hailing run and ten-vehicle diagnostics.
- [postal_simulation_tutorial.ipynb](notebooks/postal_simulation_tutorial.ipynb): a small delivery run with automatic service areas and ten-vehicle diagnostics.

The source notebooks contain no saved execution outputs. They use temporary workspaces and remove them after successful completion.

## Development

For AI-assisted development, ask your assistant to read [AGENTS.md](AGENTS.md) before editing code. This file describes scientific invariants, repository boundaries, and required checks. It is a development guide, not an App installation step. Human contributors should also read [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
poetry install --all-extras
python -m pytest -q tests
npm --prefix frontend test
npm --prefix frontend run build
poetry build
```

The geographic regression tests additionally require the immutable local Lausanne inputs, which are not distributed in the source repository. See [CONTRIBUTING.md](CONTRIBUTING.md) for generated contracts, test boundaries, and release checks.

<details>
<summary>Prompt for an AI coding assistant</summary>

```text
Help me use or develop Mobile Sensing Lab in <PROJECT_FOLDER>. First read
README.md, docs/INSTALLATION.md, and AGENTS.md. Identify my operating system
and whether I need the browser App or the Python-only workflow. On native
Windows, do not assume the App or bundled notebooks work; explain the
headless Python and WSL2 alternatives. Use Python 3.12 and preserve existing
projects, data, and results. For an installed wheel, do not require Node.js.
Before changing code, read the relevant specifications and follow AGENTS.md.
Run the applicable checks and distinguish tested behavior from assumptions.
```

</details>

## Open work and limitations

- This is a local, single-user application without authentication, cloud deployment, or a distributed job queue.
- Large-region live OSM downloads depend on public Overpass availability and may require cached or local network data.
- Synthetic demand, inferred duties, uncalibrated speeds, greedy dispatch, and illustrative costs require independent calibration before policy use.
- macOS has full desktop validation. Linux desktop and WSL2 end-to-end coverage, native Windows App support, and portable tutorial adapters remain open; Python-only scope is documented separately.
- Large visualization bundles and first-query latency remain performance-maintenance items.

## License

The source code is distributed under the [GNU General Public License v3.0](LICENSE).
Release changes are recorded in [CHANGELOG.md](CHANGELOG.md). Research users can use the repository [citation metadata](CITATION.cff).
