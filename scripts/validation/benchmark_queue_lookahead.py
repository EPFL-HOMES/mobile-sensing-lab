"""Benchmark a retained replication with an optional full-scan snapshot oracle.

Reads one replication from a saved workbench run. Writes only JSON to stdout;
no preparation cache or scientific artifact is created in the source workspace.
Run each variant in a fresh process to start with the same routing cache state.
The reference variant replaces only snapshots; other current optimizations remain.
"""

import argparse
import cProfile
import dataclasses
import hashlib
import json
import platform
import resource
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

from mobile_sensing.artifacts import verify_partitioned_artifact
from mobile_sensing.application.models import ReplicationInput
from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.application.project_resolution import assemble_runtime
from mobile_sensing.application.services import _execute_replication
from mobile_sensing.contracts import (
    ArtifactRef,
    AssignmentPlan,
    ClockConfig,
    LocationRef,
    Task,
    VehicleAvailability,
    VehicleSpec,
    VehicleStatus,
    canonical_json_bytes,
    scientific_hash,
)
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.simulation.kernel import DecisionSnapshot, _Kernel
from mobile_sensing.simulation.rng import SemanticRngStreams


class Cancellation:
    def is_cancelled(self):
        return False

    def raise_if_cancelled(self):
        pass


def reference_snapshot(self):
    """The previous snapshot implementation, used solely as a validation oracle."""
    idle = tuple(
        runtime.state
        for _, runtime in sorted(self.vehicles.items())
        if runtime.state.status == VehicleStatus.IDLE
        and runtime.availability.active
        and runtime.availability.availability_end_s is not None
        and self.now_s < runtime.availability.availability_end_s
        and self.now_s < self.end_s
    )
    waiting = tuple(self.tasks[key].task for key in sorted(self.waiting_keys))
    future = [self.end_s, *(event.time_s for _, event in self.queue if event.time_s > self.now_s)]
    if self.online_pending is not None and self.online_pending.release_s > self.now_s:
        future.append(self.online_pending.release_s)
    return DecisionSnapshot(self.now_s, idle, waiting, min(future))


def result_hash(result):
    def convert(value):
        if dataclasses.is_dataclass(value):
            return {f.name: convert(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if isinstance(value, (tuple, list)):
            return [convert(item) for item in value]
        return value

    digest = hashlib.sha256()
    for field in dataclasses.fields(result):
        digest.update(field.name.encode())
        value = getattr(result, field.name)
        for row in value if isinstance(value, tuple) else (value,):
            digest.update(canonical_json_bytes(convert(row)))
            digest.update(b"\n")
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("run_json", type=Path)
    parser.add_argument("--variant", choices=("reference", "current"), required=True)
    parser.add_argument("--hours", type=float, default=12)
    parser.add_argument("--replication-index", type=int, default=0)
    parser.add_argument("--profile", type=Path, help="Write execution-only cProfile statistics")
    args = parser.parse_args()
    root = args.workspace
    run = json.loads(args.run_json.read_text())
    reference = ArtifactRef.model_validate(run["resolution"])
    artifact = verify_partitioned_artifact(
        artifact_root=root, collection="datasets", reference=reference
    )
    folder = artifact.directory
    source = artifact.manifest.scientific_identity.resolved_config
    config = ProjectConfig.model_validate_json(json.dumps(source["project"]))
    environment = PreparedEnvironmentReader(root).read(config.prepared_environment.artifact)

    def read(name, filters=None):
        return pq.read_table(folder / "tables" / name, filters=filters).to_pylist()

    available = read("availability")
    replication_ids = sorted({row["replication_id"] for row in available})
    if not 0 <= args.replication_index < len(replication_ids):
        raise ValueError("Replication index must select a retained replication")
    replication_id = replication_ids[args.replication_index]
    filters = [("replication_id", "=", replication_id)]
    specs = tuple(
        VehicleSpec.model_validate_json(row["spec_json"]) for row in read("physical_catalog")
    )
    locations = {
        row["location_id"]: LocationRef.model_validate_json(row["location_json"])
        for row in read("resolved_locations")
    }
    tasks = tuple(
        Task.model_validate_json(row["task_json"]) for row in read("realized_tasks", filters)
    )
    replication = ReplicationInput(
        replication_id=replication_id,
        joint_scenario_id="benchmark",
        tasks=tasks,
        availability=tuple(
            VehicleAvailability.model_validate_json(row["availability_json"])
            for row in available
            if row["replication_id"] == replication_id
        ),
        input_hash="a" * 64,
        rng=SemanticRngStreams(config.simulation.seed, namespace="studio.simulation"),
        assignment_plans=tuple(
            AssignmentPlan.model_validate_json(row["plan_json"])
            for row in read("replication_plans", filters)
        ),
        online_fleet_ids=tuple(
            fleet.fleet_id for fleet in config.fleets if fleet.demand.generation_timing == "online"
        ),
    )
    static = {
        key: AssignmentPlan.model_validate_json(json.dumps(value))
        for key, value in source["assignment_plans"].items()
    }
    validated = assemble_runtime(
        config,
        environment,
        ClockConfig.model_validate_json(json.dumps(source["clock"])),
        specs,
        locations,
        source["location_area_ids"],
        static,
        (replication,),
        reference,
        set(),
        Cancellation(),
    )
    scenario = validated.bundle.scenario
    end = scenario.clock.observation_start_s + args.hours * 3600
    if not 0 < args.hours or end > scenario.clock.end_s:
        raise ValueError("Benchmark hours must fit inside the retained observation window")
    scenario = scenario.model_copy(
        update={"clock": scenario.clock.model_copy(update={"end_s": end})}
    )
    # Online streams reject releases outside the execution window. A shorter
    # benchmark retains only the observable prefix of each online stream.
    replication = dataclasses.replace(
        replication,
        tasks=tuple(
            task
            for task in replication.tasks
            if task.fleet_id not in replication.online_fleet_ids or task.release_s < end
        ),
    )
    if args.variant == "reference":
        _Kernel._snapshot = reference_snapshot
    profiler = cProfile.Profile() if args.profile is not None else None
    started = time.perf_counter()
    if profiler is not None:
        profiler.enable()
    _, result, seeds = _execute_replication(
        (
            0,
            replication,
            scenario,
            specs,
            locations,
            environment.routing,
            validated.assignment_plans,
            validated.location_area_ids,
            1000,
            Cancellation(),
        )
    )
    elapsed = time.perf_counter() - started
    if profiler is not None:
        profiler.disable()
        profiler.dump_stats(args.profile)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        f"Execution finished in {elapsed:.3f} s; hashing results for equivalence.", file=sys.stderr
    )
    print(
        json.dumps(
            {
                "variant": args.variant,
                "profiled": profiler is not None,
                "hours": args.hours,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "resolution": reference.model_dump(mode="json"),
                "replication_id": replication_id,
                "seconds": elapsed,
                "peak_rss_mib": peak / (1024**2 if platform.system() == "Darwin" else 1024),
                "vehicles": len(specs),
                "input_tasks": len(tasks),
                "events": result.processed_heap_events,
                "executions": len(result.execution_outcomes),
                "movement_rows": sum(
                    i.kind == "movement"
                    for execution in result.execution_outcomes
                    for i in execution.realized_intervals
                ),
                "routing_cache": environment.routing.cache_info()._asdict(),
                "result_hash": result_hash(result),
                "seed_hash": scientific_hash(seeds),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
