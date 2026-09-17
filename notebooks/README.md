# Notebook tutorials

Use Python 3.12 with the `web`, `notebook`, `geography`, and `optimization` extras, as installed by the README's App instructions. These notebooks use project/API adapters that currently import Unix-only file locking: **native Windows Python is not supported for these three tutorials**, even though the lower-level headless simulator has a working Windows path. Windows users can try a WSL2 Linux kernel; notebook execution on WSL2 has not been validated. See [installation and platform boundaries](../docs/INSTALLATION.md).

Download or clone the repository to obtain the `.ipynb` files. In Terminal (macOS/Linux) or Ubuntu (WSL2), activate the installed environment and register its kernel:

```bash
python -m ipykernel install --user --name mobile-sensing --display-name 'Mobile Sensing (Python 3.12)'
```

Open a notebook in your notebook editor and select **Mobile Sensing (Python 3.12)**. When using WSL2, connect the editor to WSL and select the Linux kernel, not a Windows Python interpreter. The `notebook` extra installs plotting libraries and the kernel; a notebook editor or Jupyter frontend is still required.

Run each notebook from top to bottom:

- `lausanne_simulation_tutorial.ipynb` inspects the joint five-fleet Lausanne example, including automatic Postal service areas, mean fleet/per-vehicle sensing, daily profiles, and P05 portfolio frontiers. Matching defaults reuse verified retained results; edited parameters invoke the formal backend.
- `ridehailing_simulation_tutorial.ipynb` runs a bounded `R=1` ride-hailing scenario, selects ten physical vehicles, and displays temporal/spatial sensing plus a daily trajectory animation.
- `postal_simulation_tutorial.ipynb` runs a bounded `R=1` delivery scenario with automatic service areas and the same ten-vehicle diagnostics.

The matching Lausanne example manifest and archive must be available from the installed release or through `MOBILE_SENSING_EXAMPLE_DIRECTORY`. The source notebooks contain no saved outputs. Temporary backend workspaces are removed after successful execution.

Maintainers can validate the notebooks without modifying their source files:

```bash
python -m scripts.validation.run_notebook_workflows --variant default
python -m scripts.validation.run_notebook_workflows --variant custom
```

The validation runner writes ignored records under `results/`; those records are not notebook exports.
