"""Configuration checks, independent caches and deterministic bounded preparation."""

import shutil

import pytest

from mobile_sensing.application.configuration_check import check_configuration
from mobile_sensing.application.project_models import (
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    DispatchEditor,
)
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.spatial_support import prepare_support
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.environment.cost_cache import (
    PersistentTravelTimes,
    bounded_source_costs,
)
from tests.v2.test_mr05_runs_analysis import config_fixture
from tests.v2.test_mr02_environment_editor import Cancellation
from tests.v2.environment_fixtures import RecordedProgress


def resolve(root, config, **kwargs):
    return resolve_project(
        root, config, cancellation=Cancellation(), progress=RecordedProgress(), **kwargs
    )


def test_configuration_check_does_not_prepare_or_draw(tmp_path, monkeypatch):
    root, config = config_fixture(tmp_path)
    import mobile_sensing.application.project_resolution as module

    def forbidden(*args, **kwargs):
        raise AssertionError("Lightweight checks must not generate tasks, routes or plans")

    monkeypatch.setattr(module, "resolve_project", forbidden)
    monkeypatch.setattr(PreparedEnvironmentReader, "read", forbidden)
    result = check_configuration(
        root, config, cancellation=Cancellation(), progress=RecordedProgress()
    )
    assert result.validation_level == "configuration" and result.artifact is None
    assert result.task_counts == {} and result.vehicle_counts == {"f": 2}
    assert not (root / "preparation_cache").exists()
    fleet = config.fleets[0]
    invalid = config.model_copy(
        update={
            "fleets": (
                fleet.model_copy(
                    update={"supply": fleet.supply.model_copy(update={"work_hours": 20.0})}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="fit entirely"):
        check_configuration(root, invalid, cancellation=Cancellation(), progress=RecordedProgress())


def test_fleet_caches_preserve_rng_plans_and_invalidate_independently(tmp_path, monkeypatch):
    root, config = config_fixture(tmp_path)
    prepared = PreparedEnvironmentReader(root).read(config.prepared_environment.artifact)
    support = prepare_support(root, prepared, config.prepared_environment.features)
    cell = next(iter(support.cell_locations))
    postal = FleetEditor(
        fleet_id="postal",
        name="Postal",
        demand=DemandEditor(
            task_volume=6, location_condition="depot_roundtrip", release_mode="at_start"
        ),
        supply=SupplyEditor(
            fleet_size=2,
            initial_location="depot",
            depot_cell_id=cell,
            capacity_mode="consumable",
            capacity=3,
        ),
        dispatch=DispatchEditor(mode="one_shot"),
    )
    config = config.model_copy(update={"fleets": (postal, config.fleets[0])})
    cold = resolve(root, config)
    warm = resolve(root, config, workers=2)
    assert cold.validated.reference == warm.validated.reference
    assert [r.rng.manifest for r in cold.validated.replications] == [
        r.rng.manifest for r in warm.validated.replications
    ]
    shutil.rmtree(root / "preparation_cache")
    parallel = resolve(root, config, workers=2)
    assert cold.validated.reference == parallel.validated.reference

    import mobile_sensing.application.project_resolution as module

    original = module.generate_daily_tasks
    calls = []

    def generate(fleet, *args, **kwargs):
        calls.append(fleet.fleet_id)
        return original(fleet, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unchanged Postal plan must be reused")

    monkeypatch.setattr(module, "generate_daily_tasks", generate)
    monkeypatch.setattr(module, "plan_one_shot", forbidden)
    other = config.fleets[1]
    changed = config.model_copy(
        update={
            "fleets": (
                postal,
                other.model_copy(
                    update={"demand": other.demand.model_copy(update={"task_volume": 7.0})}
                ),
            )
        }
    )
    updated = resolve(root, changed)
    assert calls == ["f", "f"]
    for before, after in zip(cold.validated.replications, updated.validated.replications):
        assert [t for t in before.tasks if t.fleet_id == "postal"] == [
            t for t in after.tasks if t.fleet_id == "postal"
        ]
        assert before.assignment_plans == after.assignment_plans


def test_directed_cost_cache_reuses_only_missing_pairs_and_profile_identity(tmp_path):
    class Routing:
        network_hash = "network"
        profile_hashes = {"default": "v1"}
        calls = []

        def travel_times_from(self, profile, source, targets):
            self.calls.append((source, tuple(targets)))
            return {
                target: 0.0 if source == target else None if source == "b" else 1.25
                for target in targets
            }

    routing = Routing()
    cache = PersistentTravelTimes(routing, tmp_path)
    assert cache.travel_times_from("default", "a", ["a", "b"]) == {"a": 0.0, "b": 1.25}
    assert cache.travel_times_from("default", "a", ["a", "b", "c"])["c"] == 1.25
    assert routing.calls == [("a", ("a", "b")), ("a", ("c",))]
    assert cache.travel_times_from("default", "b", ["a"])["a"] is None
    again = PersistentTravelTimes(routing, tmp_path)
    again.travel_times_from("default", "a", ["b"])
    assert len(routing.calls) == 3
    routing.profile_hashes = {"default": "v2"}
    again.travel_times_from("default", "a", ["b"])
    assert len(routing.calls) == 4
    for path in (tmp_path / "preparation_cache").rglob("*.gz"):
        path.write_bytes(b"broken")
    assert again.travel_times_from("default", "a", ["b"]) == {"b": 1.25}
    assert len(routing.calls) == 5


def test_bounded_search_order_and_concurrency():
    import threading
    import time

    lock = threading.Lock()
    active = peak = 0

    def compute(value):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.005)
        with lock:
            active -= 1
        return value

    assert list(bounded_source_costs(compute, tuple(range(20)), 3)) == list(range(20))
    assert 1 < peak <= 3


def test_stage_cache_corruption_and_cancellation(tmp_path):
    import gzip
    from mobile_sensing.application.preparation_cache import cached_stage

    calls = []

    def compute():
        calls.append(1)
        return {"value": 7}

    def run(**kwargs):
        return cached_stage(tmp_path, "test", {"input": 1}, dict[str, int], compute, **kwargs)

    assert run() == run() == {"value": 7}
    assert len(calls) == 1
    path = next((tmp_path / "preparation_cache").rglob("*.gz"))
    with gzip.open(path, "wb") as stream:
        stream.write(b'{"payload": 4, "sha256": "bad"}')
    assert run() == {"value": 7} and len(calls) == 2

    class Cancelled:
        def raise_if_cancelled(self):
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        run(cancellation=Cancelled())
    assert len(calls) == 2
