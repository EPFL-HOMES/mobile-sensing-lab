from datetime import date
import zipfile
import numpy as np
import pandas as pd
import pytest

from mobile_sensing.application.project_models import (
    DemandEditor,
    TemporalInterval,
    SimulationEditor,
    ProjectConfig,
    SupplyEditor,
    ShiftGroup,
    FleetEditor,
    PortfolioEditor,
    PortfolioFleetEditor,
    NumericRange,
)
from mobile_sensing.application.civil_time import civil_clock
from mobile_sensing.application.temporal_authoring import (
    draw_releases,
    profile_intervals,
    expand_range,
    resolve_portfolio,
)
from mobile_sensing.application.temporal_preview import temporal_preview, expected_overlap
from mobile_sensing.application.calendar_authoring import resolve_calendar
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.run_pipeline import mobility_projection
from mobile_sensing.application.migration import migrate_project
from tests.application.test_authoring import environment
from tests.environment.test_environment_editor import Cancellation, register
from tests.support.environment_fixtures import RecordedProgress
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.contracts import ExecutionOptions


def test_fixed_profile_poisson_rate_and_dst_elapsed_units():
    clock = civil_clock(SimulationEditor(service_date=date(2026, 3, 29)), "Europe/Zurich")
    profile = (
        TemporalInterval(start_time="00:00", end_time="04:00", value=1),
        TemporalInterval(start_time="12:00", end_time="13:00", value=0),
    )
    fixed = DemandEditor(temporal_mode="shares", time_profile=profile, task_volume=37)
    releases = draw_releases(fixed, clock, np.random.default_rng(10))
    assert len(releases) == 37 and np.all((releases >= 0) & (releases < 3 * 3600))
    rates = fixed.model_copy(
        update={
            "temporal_mode": "rates",
            "volume_mode": "expected",
            "time_profile": (profile[0].model_copy(update={"value": 100.0}),),
        }
    )
    counts = [len(draw_releases(rates, clock, np.random.default_rng(seed))) for seed in range(500)]
    assert abs(np.mean(counts) - 300) < 4 and 240 < np.var(counts) < 360
    assert len(set(counts)) > 1
    bad = fixed.model_copy(update={"time_profile": (profile[0], profile[0])})
    with pytest.raises(ValueError, match="nonoverlapping"):
        draw_releases(bad, clock, np.random.default_rng(1))


def test_uploaded_profile_preserves_source_row_error(tmp_path):
    path = tmp_path / "profile.csv"
    pd.DataFrame([{"start_time": "00:00", "end_time": "12:00", "value": "invalid"}]).to_csv(
        path, index=False
    )
    demand = DemandEditor(
        temporal_mode="shares", time_profile_input=register(tmp_path / "root", path, "demand")
    )
    with pytest.raises(ValueError, match="source row 2"):
        profile_intervals(tmp_path / "root", demand)


def test_profiles_shift_groups_warmup_and_online_share_common_execution(tmp_path):
    root, editor, prepared = environment(tmp_path)
    demand = DemandEditor(
        task_volume=12,
        temporal_mode="shares",
        time_profile=(TemporalInterval(start_time="00:00", end_time="24:00", value=1),),
    )
    supply = SupplyEditor(
        fleet_size=2,
        operating_start="00:00",
        operating_end="24:00",
        shift_groups=(
            ShiftGroup(
                name="Carry-in",
                count=1,
                start_time="20:00",
                latest_start="20:00",
                work_hours=8,
                day_offset=-1,
            ),
            ShiftGroup(
                name="Day", count=1, start_time="04:00", latest_start="06:00", work_hours=18
            ),
        ),
    )
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        fleets=(FleetEditor(fleet_id="f", name="Fleet", demand=demand, supply=supply),),
        simulation=SimulationEditor(time_mode="relative", warmup_hours=8, replications=2),
    )
    before = temporal_preview(root, config)
    assert before.fleets[0].expected_task_total == pytest.approx(12)
    assert before.fleets[0].bins[0].expected_active_vehicles == 1
    assert before.fleets[0].bins[4].expected_active_vehicles == pytest.approx(0.25)
    offline = resolve_project(
        root, config, cancellation=Cancellation(), progress=RecordedProgress()
    )
    online_config = config.model_copy(
        update={
            "fleets": (
                config.fleets[0].model_copy(
                    update={"demand": demand.model_copy(update={"generation_timing": "online"})}
                ),
            )
        }
    )
    online = resolve_project(
        root, online_config, cancellation=Cancellation(), progress=RecordedProgress()
    )
    assert offline.validated.catalog == online.validated.catalog
    for left, right in zip(offline.validated.replications, online.validated.replications):
        assert left.tasks == right.tasks and left.availability == right.availability
        assert len([task for task in left.tasks if task.release_s >= 0]) == 12
        assert any(task.release_s < 0 for task in left.tasks)
        assert len({task.task_id for task in left.tasks}) == len(left.tasks)
    app = HeadlessApplication(root)
    options = ExecutionOptions(memory_limit_bytes=512 * 1024**2)
    left, right = app.run_simulation(offline.validated, options), app.run_simulation(
        online.validated, options
    )
    assert [r.task_outcomes for r in left.results] == [r.task_outcomes for r in right.results]
    assert [r.lifecycle_events for r in left.results] == [r.lifecycle_events for r in right.results]
    assert temporal_preview(root, config) == before


def test_representative_weekday_resolves_feed_coverage_and_exact_day(tmp_path):
    from tests.datasets.test_gtfs import _feed

    feed = _feed(tmp_path / "feed")
    archive = tmp_path / "feed.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for path in feed.iterdir():
            output.write(path, path.name)
    root = tmp_path / "root"
    fleet = FleetEditor(
        fleet_id="bus",
        name="Bus",
        demand=DemandEditor(
            source="import",
            task_type="ordered",
            template="gtfs",
            input_id=register(root, archive, "gtfs"),
            route_ids=("001",),
        ),
    )
    config = ProjectConfig(
        fleets=(fleet,), simulation=SimulationEditor(time_mode="weekday", weekday=2)
    )
    resolved, view = resolve_calendar(root, config)
    assert view.resolved_date == "2026-01-07" and view.candidate_days == 52
    assert resolved.simulation.timezone == "Europe/Zurich"
    exact = config.model_copy(
        update={"simulation": SimulationEditor(service_date=date(2030, 1, 1))}
    )
    with pytest.raises(ValueError, match="No common active service day"):
        resolve_calendar(root, exact)
    generic = config.model_copy(update={"simulation": SimulationEditor(time_mode="relative")})
    with pytest.raises(ValueError, match="GTFS"):
        resolve_calendar(root, generic)


def test_decimal_ranges_limits_catalog_and_migration_identity():
    assert expand_range(NumericRange(minimum=0.1, maximum=0.35, step=0.1)) == (0.1, 0.2, 0.3, 0.35)
    editor = PortfolioEditor(
        budget_range=NumericRange(minimum=0, maximum=5, step=3),
        fleets=(
            PortfolioFleetEditor(
                fleet_id="f", count_range=NumericRange(minimum=0, maximum=5, step=3)
            ),
        ),
    )
    resolved = resolve_portfolio(editor, {"f": 5})
    assert resolved.budgets == (0, 3, 5) and resolved.fleets[0].counts == (0, 3, 5)
    with pytest.raises(ValueError, match="physical catalog"):
        resolve_portfolio(editor, {"f": 4})
    with pytest.raises(ValueError, match="integer"):
        expand_range(NumericRange(minimum=0, maximum=2, step=0.5), integers=True)
    with pytest.raises(ValueError, match="exceeds"):
        expand_range(NumericRange(minimum=0, maximum=100000, step=0.1))
    old = ProjectConfig(schema_version="3.0", fleets=(FleetEditor(fleet_id="f", name="F"),))
    migrated = migrate_project(old.model_dump(mode="json"), "revision-old")
    assert migrated.config.schema_version == "3.4" and migrated.notices
    assert mobility_projection(old) == mobility_projection(migrated.config)
    projection = mobility_projection(old)
    assert (
        "time_mode" not in projection["simulation"]
        and "shift_groups" not in projection["fleets"][0]["supply"]
    )
    assert expected_overlap(0, 3600, -14400, -14400, 28800) == 3600
