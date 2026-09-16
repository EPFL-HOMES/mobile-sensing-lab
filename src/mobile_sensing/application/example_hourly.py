"""Recompute hourly example exposure/analysis from retained exact mobility."""

import argparse
import json
from pathlib import Path

from mobile_sensing.application.example_bundle import bundle_manifest, write_bundle
from mobile_sensing.application.example_audit import audit_example
from mobile_sensing.application.run_pipeline import read_named_record, run_project, run_analysis
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.project_reports import build_report
from mobile_sensing.application.services import HeadlessApplication


class Cancellation:
    def raise_if_cancelled(self):
        return None

    def is_cancelled(self):
        return False


class Progress:
    def update(self, **value):
        print(json.dumps(value), flush=True)


def build_hourly(root, destination):
    root = Path(root).resolve()
    bundle = bundle_manifest()
    runs = [
        read_named_record(root, identifier, "studio_run")
        for identifier in bundle.config.linked_run_ids
    ]
    original = next(run for run in runs if run.realization_source_run_id is None)
    old_batch = next(run for run in runs if run.realization_source_run_id is not None)
    old_analysis = read_named_record(root, bundle.config.linked_analysis_ids[0], "studio_analysis")
    cancellation, progress = Cancellation(), Progress()
    kwargs = dict(
        options=RunOptions(memory_limit_bytes=8 * 1024**3),
        cancellation=cancellation,
        progress=progress,
        source_revision_id=None,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Hourly example must reuse its exact retained mobility")

    execute = HeadlessApplication.run_simulation
    HeadlessApplication.run_simulation = forbidden
    try:
        config = original.config.model_copy(
            update={
                "simulation": original.config.simulation.model_copy(
                    update={"temporal_resolution_minutes": 60.0}
                )
            }
        )
        run = run_project(root, config, name="Lausanne · full day · 1-hour reporting", **kwargs)
        batch_config = old_batch.config.model_copy(
            update={
                "simulation": old_batch.config.simulation.model_copy(
                    update={"temporal_resolution_minutes": 60.0}
                )
            }
        )
        batch = run_project(
            root,
            batch_config,
            name="Lausanne · same requests · 2-minute Batch · 1-hour reporting",
            replay_from=original,
            **kwargs,
        )
    finally:
        HeadlessApplication.run_simulation = execute
    assert run.mobility_reused and run.simulation == original.simulation
    assert batch.mobility_reused and batch.simulation == old_batch.simulation
    editor = old_analysis.config.model_copy(update={"source_run_id": run.run_id})
    analysis = run_analysis(
        root, editor, name="Lausanne · hourly exposure · population utility", **kwargs
    )
    for name, value in (
        ("example-run.json", run),
        ("example-batch-run.json", batch),
        ("example-analysis.json", analysis),
    ):
        (root / name).write_text(value.model_dump_json(indent=2))
    for name in ("input-provenance.json",):
        if not (root / name).exists():
            candidates = [
                item.path
                for item in bundle.files
                if Path(item.path).name == name and Path(item.path).parts[0] == "example_evidence"
            ]
            if len(candidates) != 1:
                raise ValueError(f"Example bundle must contain exactly one {name}")
            (root / name).write_bytes((root / candidates[0]).read_bytes())
    audit = audit_example(root)
    (root / "example-audit.json").write_text(json.dumps(audit, indent=2))
    for value, kind in ((run, "studio_run"), (batch, "studio_run"), (analysis, "studio_analysis")):
        build_report(root, value.artifact.artifact_id, kind, cancellation)
    output = write_bundle(root, Path(destination))
    print(
        json.dumps(
            {
                "bundle_id": output.bundle_id,
                "run_id": run.run_id,
                "analysis_id": analysis.analysis_id,
                "files": len(output.files),
                "hourly": True,
                "mobility_reused": True,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    arguments = parser.parse_args()
    build_hourly(arguments.root, arguments.destination)
