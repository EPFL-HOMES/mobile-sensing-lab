"""Readable project file projections with exact shared source locations."""

import json
import os
import shutil

from mobile_sensing.jobs.project_files import write_json, write_text, html_index
from mobile_sensing.contracts import scientific_hash


def selected_inputs(value):
    found = set()
    if isinstance(value, str) and value.startswith("input_"):
        found.add(value)
    elif isinstance(value, dict):
        for child in value.values():
            found.update(selected_inputs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(selected_inputs(child))
    return found


def write_inventory(store, directory, record, settings, runs, analyses):
    from mobile_sensing.jobs.project_layout import is_owned_store

    root = store.artifact_root
    owned = is_owned_store(root)
    files = []

    def add(path, category, name=None, source=None):
        path = path.resolve() if owned else path
        if path.is_file() and not path.is_symlink():
            files.append(
                {
                    "file_id": scientific_hash(os.path.relpath(path, directory)),
                    "name": name or path.name,
                    "category": category,
                    "path": os.path.relpath(path, directory),
                    "size_bytes": path.stat().st_size,
                    "shared": not path.is_relative_to(directory),
                    "source_id": source,
                }
            )

    add(
        directory / ("settings/current.json" if owned else "settings.json"),
        "Settings",
        "Current saved settings",
    )
    for identifier in sorted(selected_inputs(settings)):
        path = root / "inputs" / identifier / "input.json"
        if not path.exists():
            continue
        metadata = json.loads(path.read_text())
        source = root / "inputs" / identifier / metadata["file"]
        fleet = next(
            (
                f.get("name", f["fleet_id"])
                for f in settings.get("fleets", [])
                if identifier in selected_inputs(f)
            ),
            None,
        )
        category = f"Inputs / {fleet}" if fleet else "Inputs / Global"
        add(source, category, metadata["name"] + " · " + metadata["original_filename"], identifier)
        add(path, category, metadata["name"] + " · metadata", identifier)
    prepared = settings.get("prepared_environment") or {}
    for value in (prepared.get("artifact"), prepared.get("features")):
        if not value:
            continue
        collection = "environments" if value["artifact_kind"] == "environment" else "datasets"
        folder = root / collection / value["artifact_id"]
        for path in sorted(folder.rglob("*.parquet")):
            add(path, "Environment", str(path.relative_to(folder)), value["artifact_id"])
    mapping_path = directory / "result-folders.json"
    mappings = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    for kind, identifiers in (("runs", runs), ("analyses", analyses)):
        for sequence, identifier in enumerate(identifiers, 1):
            # Read only the compact immutable named-record JSON table.
            from mobile_sensing.application.run_pipeline import read_named_record

            try:
                named = read_named_record(
                    root, identifier, "studio_run" if kind == "runs" else "studio_analysis"
                ).model_dump(mode="json")
            except (FileNotFoundError, KeyError, ValueError):
                continue
            label = named["name"]
            if identifier not in mappings:
                number = 1 + sum(
                    value.startswith(("results/" if owned else "") + kind + "/")
                    for value in mappings.values()
                )
                mappings[identifier] = (
                    f"{'results/' if owned else ''}{kind}/{'run' if kind=='runs' else 'analysis'}-{number:03d}"
                )
            folder = directory / mappings[identifier]
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / "configuration.json", named["config"])
            write_json(folder / "source.json", named)
            for file in ("configuration.json", "source.json"):
                add(
                    folder / file,
                    "Runs" if kind == "runs" else "Analyses",
                    f"{label} · {file}",
                    identifier,
                )
            report = root / ".system" / "reports" / identifier
            if (report / "complete.json").is_file():
                for path in report.iterdir():
                    if path.name != "complete.json" and not (folder / path.name).exists():
                        shutil.copyfile(path, folder / path.name)
            for output in sorted(folder.glob("*")):
                if output.suffix in {".csv", ".geoparquet"} or output.name == "summary.json":
                    add(output, "Results", f"{label} · {output.name}", identifier)
            for artifact in (
                named.get("simulation"),
                named.get("exposure"),
                named.get("frontier"),
                named.get("samples"),
            ):
                if artifact:
                    collection = {
                        "simulation": "simulations",
                        "exposure": "exposures",
                        "portfolio": "portfolios",
                    }[artifact["artifact_kind"]]
                    source_folder = root / collection / artifact["artifact_id"]
                    for path in sorted(source_folder.rglob("*.parquet")):
                        add(
                            path,
                            "Complete records",
                            f"{label} · {artifact['artifact_kind']} / {path.parent.name} / {path.name}",
                            artifact["artifact_id"],
                        )
    for path in sorted((directory / "exports").glob("*")):
        add(path, "Exports")
    write_json(mapping_path, mappings)
    if len(files) > 5000:
        raise ValueError("Project file inventory exceeds the supported 5,000-file limit")
    write_json(
        directory / "files.json",
        {
            "project_id": record.project_id,
            "name": record.name,
            "directory": record.name,
            "files": files,
            "portable": owned,
        },
    )
    write_text(directory / "index.html", html_index(record.name, files))
    return files
