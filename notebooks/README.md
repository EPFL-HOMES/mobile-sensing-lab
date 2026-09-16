# Notebook tutorials

Use Python 3.12 with the `notebook` and `optimization` extras. Register and select the project kernel:

```bash
python -m ipykernel install --user --name mobile-sensing --display-name 'Mobile Sensing (Python 3.12)'
```

Run each notebook from top to bottom:

- `lausanne_simulation_tutorial.ipynb` inspects the joint five-fleet Lausanne example, including automatic Postal service areas, mean fleet/per-vehicle sensing, daily profiles, and P05 portfolio frontiers. Matching defaults reuse verified retained results; edited parameters invoke the formal backend.
- `ridehailing_simulation_tutorial.ipynb` runs a bounded `R=1` ride-hailing scenario, selects ten physical vehicles, and displays temporal/spatial sensing plus a daily trajectory animation.
- `postal_simulation_tutorial.ipynb` runs a bounded `R=1` delivery scenario with automatic service areas and the same ten-vehicle diagnostics.

The matching Lausanne example manifest and archive must be available from the installed release or through `MOBILE_SENSING_EXAMPLE_DIRECTORY`. The source notebooks contain no saved outputs. Temporary backend workspaces are removed after successful execution.

Maintainers can validate the notebooks without modifying their source files:

```bash
python -m tests.v2.run_notebook_workflows --variant default
python -m tests.v2.run_notebook_workflows --variant custom
```

The validation runner writes ignored records under `results/`; those records are not notebook exports.
