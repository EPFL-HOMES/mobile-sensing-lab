# Installation and platform guide

Choose a workflow first: the **App** provides a browser interface and managed projects; **Python only** runs scientific services directly. Both use Python 3.12. The package name and command are `mobile-sensing`; the repository is `mobile-sensing-lab`.

Enter shell commands in the named terminal, one line at a time. Do not paste them into a Python `>>>` prompt or a notebook cell. A command may take several minutes; wait for the prompt to return before entering the next one.

## Platform boundaries

| Workflow | macOS | Linux | Native Windows | Windows with WSL2 |
|---|---|---|---|---|
| Core simulation, exposure and portfolio modules | Tested | Automated suite | Scientific imports checked; broader scientific coverage remains open | Uses Linux; not separately validated |
| Synchronous `HeadlessApplication` and headless CLI | Tested | Automated suite | Python 3.12 installation and a small environment → validation → simulation workflow passed on GitHub Windows CI | Uses Linux; not separately validated |
| Browser App, API and background coordinator | Full local validation on Apple Silicon | Build and automated suite pass; desktop coverage is narrower | Currently blocked by Unix-only file locking | Suggested full-App route; end-to-end validation remains open |
| Three bundled tutorial notebooks | Validated local workflows | End-to-end notebook validation remains open | Current tutorial adapters import the job/API layer and are blocked by the same file-lock dependency | Use a Linux Python kernel; not separately validated |

The App's coordinator and managed workspaces use `fcntl.flock`. Python documents [`fcntl` as Unix-only](https://docs.python.org/3/library/fcntl.html). Some notebook adapters reach this dependency through `mobile_sensing.jobs`, even though they run without a browser. Changing the terminal from PowerShell to Command Prompt does not remove this dependency.

The lower-level `mobile_sensing.simulation`, `mobile_sensing.exposure`, `mobile_sensing.portfolio`, and synchronous `HeadlessApplication` services do not require the App coordinator. Native Windows Python is useful for this scope. The Windows CI check uses **one worker** and a small analytic dataset; it does not certify all algorithms, geospatial formats, multiprocessing, the bundled notebooks, or the App. For initial native Windows runs, use `ExecutionOptions(workers=1)`.

## macOS

Follow the copy-and-paste commands in the [README](../README.md#macos-enter-these-commands-in-terminal). Install Python 3.12 first, then open **Applications → Utilities → Terminal**. The release wheel includes the compiled browser interface and both examples, so no frontend build is needed.

## Windows with WSL2

This runs the Linux application on your Windows computer. WSL setup follows [Microsoft's installation guide](https://learn.microsoft.com/en-us/windows/wsl/install). These instructions are provided as a route to try; this project has not completed WSL2 end-to-end validation.

### 1. Install Ubuntu from PowerShell

On Windows 11 or Windows 10 version 2004/build 19041 or newer, open **Start → PowerShell → Run as administrator** and enter:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart Windows if requested. Open **Ubuntu 24.04** from Start and finish creating the Linux username and password. If WSL is already configured, keep your existing installation and check its version in PowerShell with `wsl --list --verbose`; the Ubuntu environment should use WSL 2.

### 2. Install the App inside Ubuntu

Enter the following in the **Ubuntu terminal**, not PowerShell. Use Ubuntu 24.04 so the distribution's Python version matches the required Python 3.12:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv
mkdir -p ~/mobile-sensing-lab
cd ~/mobile-sensing-lab
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "mobile-sensing[web,optimization,geography,notebook] @ https://github.com/EPFL-HOMES/mobile-sensing-lab/releases/download/v0.1.0/mobile_sensing-0.1.0-py3-none-any.whl"
mobile-sensing launch --artifact-root ./project --port 8820 --no-browser
```

The password requested by `sudo` is your Ubuntu password; typed characters are not displayed. Keep the workspace in the Linux home directory (`~/mobile-sensing-lab`), and keep this terminal open while the App runs.

### 3. Open the App in your Windows browser

Open Edge, Chrome, or Firefox in Windows and visit **http://localhost:8820/**. WSL normally makes Linux web services accessible from the Windows browser through localhost; see [Microsoft's WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking) if that connection is unavailable.

Press **Ctrl+C in Ubuntu** to stop. To restart later, open Ubuntu and enter:

```bash
cd ~/mobile-sensing-lab
source .venv/bin/activate
mobile-sensing launch --artifact-root ./project --port 8820 --no-browser
```

Windows Python and WSL Python are separate installations. Select the WSL environment's Python interpreter when running code or notebooks through an editor connected to WSL.

## Windows native Python

Use this path for programmatic scientific workflows without the managed App. It does not enable `mobile-sensing launch`, `serve-api`, `run-coordinator`, or the three existing tutorial notebooks.

### 1. Create an environment in PowerShell

Install **64-bit Python 3.12** with the Python launcher, using a [Python installer](https://www.python.org/downloads/release/python-31210/) or your environment manager. Open **Start → PowerShell** as an ordinary user and enter:

```powershell
New-Item -ItemType Directory -Force "$HOME\mobile-sensing-lab"
Set-Location "$HOME\mobile-sensing-lab"
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install "mobile-sensing[optimization] @ https://github.com/EPFL-HOMES/mobile-sensing-lab/releases/download/v0.1.0/mobile_sensing-0.1.0-py3-none-any.whl"
.\.venv\Scripts\python.exe -c "from mobile_sensing.application import HeadlessApplication; from mobile_sensing.simulation import run_event_kernel; print('Python simulation imports OK')"
.\.venv\Scripts\mobile-sensing.exe --help
```

Using the full interpreter path avoids PowerShell activation-policy changes. All later package-installation commands should use this same Python interpreter. If `py` is unavailable, use the full path to your Python 3.12 executable for the two `py -3.12` commands.

### 2. Use the Python services

Select `.venv\Scripts\python.exe` as your IDE interpreter. Put Python code in a `.py` file, for example `my_simulation.py`, and run it from this PowerShell folder with:

```powershell
.\.venv\Scripts\python.exe .\my_simulation.py
```

The entry point is `from mobile_sensing.application import HeadlessApplication`. Construct it with a directory for generated artifacts, prepare an environment, register and normalize demand/supply, validate a `ScenarioResourceBundle`, then call `run_simulation` with `ExecutionOptions(workers=1)`. Input schemas and mappings are required; importing the class alone does not execute a simulation. See the [data specification](DATA_INGESTION_SPECIFICATION.md), [mapping templates](templates/), and the complete executable example below.

Headless CLI commands such as `validate-scenario` and `run-simulation` provide the same workflow with JSON configuration files. Use `mobile-sensing.exe run-simulation --help` to see the required paths. `--help` also lists App commands; their presence does not imply native Windows support.

### 3. Run a small complete example

For an executable example that builds a tiny environment and runs two replications, download the repository using **Code → Download ZIP**, extract it, then open the extracted `mobile-sensing-lab-main` folder in File Explorer. Right-click inside that folder and choose **Open in Terminal** (PowerShell). You should see `pyproject.toml` and `tests/` in that folder. Enter:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[optimization]" pytest
.\.venv\Scripts\python.exe -m pytest -q tests/application/test_headless_application.py::test_headless_cli_validates_and_runs_without_http_or_browser
```

Expected result: **1 passed**. The [example's source](../tests/application/test_headless_application.py) includes the full input construction, validation and simulation calls. It uses generated small inputs, temporary artifacts, and no browser or local Lausanne dataset. This is also the scoped native Windows CI check. It is a developer example, not one of the city tutorial notebooks.

## Linux

On **Ubuntu 24.04**, open Terminal and follow step 2 under [Windows with WSL2](#windows-with-wsl2) directly; WSL installation is unnecessary. Visit `http://127.0.0.1:8820/` in your Linux browser. Other distributions need their equivalent Python 3.12 and virtual-environment packages; their installation commands may differ.

## Install from source

Use a source checkout when editing code. Download **Code → Download ZIP** and extract it, or clone the repository. Open a terminal in the folder containing `pyproject.toml`. Source code and current example manifests are in Git; the matching large archives are separate [example-data release assets](https://github.com/EPFL-HOMES/mobile-sensing-lab/releases/tag/examples-2026-09-22).

For macOS/Linux/WSL, with Python 3.12 installed, enter:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[web,optimization,geography,notebook]'
```

To build the browser interface, install **Node.js 22.12–22.x**, then in that same source folder enter:

```bash
node --version
npm --prefix frontend ci
npm --prefix frontend run build
mobile-sensing launch --artifact-root ./project --port 8820
```

For native Windows Python-only work, use the PowerShell source commands above and skip the browser build. Building the frontend on Windows does not make its Python App backend compatible.

To enable the two current examples in a source checkout, run `python3 scripts/install_example_assets.py` from the repository root. The script verifies the two committed JSON manifests, downloads the corresponding release assets, reconstructs the 2.67 GB Lausanne ZIP from two parts, verifies complete SHA-256 hashes, and places only `lausanne.zip` and `san-francisco.zip` beside the manifests. Existing matching ZIPs are reused; `--replace` backs up mismatched older archives before replacing them. GitHub's automatically generated source ZIP cannot include these large data assets. The v0.1.0 wheel and its example assets are an earlier, internally matching release, not the current five-line Lausanne bundle.

The macOS-only [Start Mobile Sensing.command](../Start%20Mobile%20Sensing.command) is an alternative for source users with Python 3.12 and Node.js installed. It creates `.app-venv` and builds the interface; it is not a Windows launcher.

For AI-assisted changes, read [AGENTS.md](../AGENTS.md) first, then [CONTRIBUTING.md](../CONTRIBUTING.md) for the development checks.

## Troubleshooting

| What you see | What to do |
|---|---|
| `python3.12` or `py` not found | Install Python 3.12, reopen the terminal, then repeat the version check. |
| Python 3.13/3.14 dependency error | Create the virtual environment with Python 3.12; the current package pins that minor version. |
| `source` is not recognized in PowerShell | Follow the Windows commands, or open Ubuntu for the WSL instructions. |
| `No module named fcntl` on Windows | You reached a managed App/job/tutorial path. Use WSL2 for that workflow, or stay within the documented headless Python scope. This is not a missing pip dependency. |
| `mobile-sensing` not found | Activate the correct environment, or use `.venv/bin/mobile-sensing` (Unix) / `.\.venv\Scripts\mobile-sensing.exe` (Windows). |
| Browser interface assets missing | A source checkout needs the frontend build; use the release wheel if you only want to run the App. |
| Address already in use | Stop the earlier instance or choose another port, such as `--port 8821`, and open the matching URL. |
| Examples unavailable | For current source, run `python3 scripts/install_example_assets.py`; for the earlier v0.1.0 wheel, use its own matching example assets. |
| VS Code shows a red configuration marker | Open **View → Problems** to read the diagnostic. For a source checkout, install dependencies and run `npm --prefix frontend run typecheck`; a stale marker alone is not a compiler failure. |
