from __future__ import annotations

import pytest
from pydantic import ValidationError

from mobile_sensing.contracts import (
    GeneratorDemandConfig,
    GeneratedSupplyConfig,
    LocationRef,
    ResolutionStatus,
)
from mobile_sensing.simulation import (
    LocationWeight,
    OdWeight,
    SemanticRngStreams,
    build_generated_catalog,
    generate_piecewise_poisson,
    iter_piecewise_poisson,
    realize_availability,
)


def _location(location_id: str, x: float) -> LocationRef:
    return LocationRef(
        location_id=location_id,
        original_x=x,
        original_y=0.0,
        original_crs="EPSG:2056",
        node_id=location_id,
        snapped_x=x,
        snapped_y=0.0,
        snap_distance_m=0.0,
        resolution_status=ResolutionStatus.RESOLVED,
    )


def _demand(structure: str) -> GeneratorDemandConfig:
    return GeneratorDemandConfig.model_validate(
        {
            "source": "generator",
            "structure": structure,
            "adapter": "demand.poisson_piecewise_constant@1",
            "generation_timing": "offline",
            "parameters": {
                "intervals": (
                    {"start_s": 0.0, "end_s": 100.0, "rate_tasks_per_s": 0.5},
                    {"start_s": 100.0, "end_s": 200.0, "rate_tasks_per_s": 0.25},
                ),
                "location_weights_ref": "locations" if structure == "location" else None,
                "od_weights_ref": "od" if structure == "od" else None,
                "service_duration_s": 10.0,
                "quantity": 2.0 if structure == "od" else None,
            },
        }
    )


def test_offline_online_poisson_outputs_are_identical_and_release_ordered() -> None:
    config = _demand("od")
    weights = (
        OdWeight(origin_location_id="L0", destination_location_id="L0", weight=1.0),
        OdWeight(origin_location_id="L0", destination_location_id="L1", weight=2.0),
    )
    offline_rng = SemanticRngStreams(981)
    online_rng = SemanticRngStreams(981)
    offline = generate_piecewise_poisson(
        config,
        fleet_id="fleet",
        replication_id="rep_01",
        capacity_mode="occupancy",
        rng=offline_rng,
        od_weights=weights,
    )
    online = tuple(
        iter_piecewise_poisson(
            config.model_copy(update={"generation_timing": "online"}),
            fleet_id="fleet",
            replication_id="rep_01",
            capacity_mode="occupancy",
            rng=online_rng,
            od_weights=weights,
        )
    )
    assert offline == online
    assert [item.release_s for item in offline] == sorted(item.release_s for item in offline)
    assert all(item.steps[0].quantity_delta == -2 for item in offline)
    assert all(item.steps[1].quantity_delta == 2 for item in offline)
    assert offline_rng.manifest == online_rng.manifest


def test_zero_intensity_zero_distance_and_invalid_weights() -> None:
    config = _demand("location")
    zero = config.model_copy(
        update={
            "parameters": config.parameters.model_copy(
                update={
                    "intervals": (
                        config.parameters.intervals[0].model_copy(update={"rate_tasks_per_s": 0.0}),
                    )
                }
            )
        }
    )
    assert (
        generate_piecewise_poisson(
            zero,
            fleet_id="fleet",
            replication_id="rep",
            capacity_mode="none",
            rng=SemanticRngStreams(1),
            location_weights=(LocationWeight(location_id="L0", weight=1.0),),
        )
        == ()
    )
    with pytest.raises(ValueError, match="positive total mass"):
        generate_piecewise_poisson(
            config,
            fleet_id="fleet",
            replication_id="rep",
            capacity_mode="none",
            rng=SemanticRngStreams(1),
            location_weights=(LocationWeight(location_id="L0", weight=0.0),),
        )
    with pytest.raises(ValueError, match="unique location IDs"):
        generate_piecewise_poisson(
            config,
            fleet_id="fleet",
            replication_id="rep",
            capacity_mode="none",
            rng=SemanticRngStreams(1),
            location_weights=(
                LocationWeight(location_id="L0", weight=1.0),
                LocationWeight(location_id="L0", weight=2.0),
            ),
        )
    invalid_parameters = config.model_dump(mode="python")
    invalid_parameters["parameters"]["intervals"][0]["rate_tasks_per_s"] = -0.1
    with pytest.raises(ValidationError):
        GeneratorDemandConfig.model_validate(invalid_parameters)
    with pytest.raises(ValidationError):
        LocationWeight(location_id="L0", weight=float("nan"))


def test_preview_isolation_and_semantic_order_independence() -> None:
    provider = SemanticRngStreams(77)
    preview = provider.preview()
    preview.stream("demand.arrivals", "rep", "fleet", "interval").random(20)
    after_preview = provider.stream("demand.arrivals", "rep", "fleet", "interval").random(5)
    baseline = (
        SemanticRngStreams(77).stream("demand.arrivals", "rep", "fleet", "interval").random(5)
    )
    assert after_preview == pytest.approx(baseline)
    assert provider.manifest.streams[0].stream_key.startswith("simulation:")
    assert preview.manifest.streams[0].stream_key.startswith("preview:")

    reordered = SemanticRngStreams(77)
    second = reordered.stream("demand.locations", "rep", "fleet", "interval").random(3)
    first = reordered.stream("demand.arrivals", "rep", "fleet", "interval").random(3)
    reference = SemanticRngStreams(77)
    assert first == pytest.approx(
        reference.stream("demand.arrivals", "rep", "fleet", "interval").random(3)
    )
    assert second == pytest.approx(
        reference.stream("demand.locations", "rep", "fleet", "interval").random(3)
    )


def test_fixed_catalog_survives_replication_specific_bounded_availability() -> None:
    config = GeneratedSupplyConfig.model_validate(
        {
            "source": "generated",
            "catalog_size": 3,
            "catalog_id_namespace": "bus",
            "availability": {
                "kind": "uniform_bounded",
                "earliest_start_s": 0.0,
                "latest_start_s": 20.0,
                "duration_s": 100.0,
            },
            "initial_locations_ref": "initials",
            "capacity": {"mode": "occupancy", "unit": "passengers"},
            "capacity_value": 20.0,
            "area_definitions_ref": None,
            "area_assignments_ref": None,
            "idle_policy": {"policy": "stationary"},
            "depot_policy": None,
        }
    )
    initial_locations = tuple(_location(f"L{index}", float(index)) for index in range(3))
    catalog, specs = build_generated_catalog(
        config,
        fleet_id="fleet",
        initial_locations=initial_locations,
    )
    shuffled_catalog, shuffled_specs = build_generated_catalog(
        config, fleet_id="fleet", initial_locations=tuple(reversed(initial_locations))
    )
    assert (shuffled_catalog, shuffled_specs) == (catalog, specs)
    first = realize_availability(config, specs, replication_id="rep_01", rng=SemanticRngStreams(4))
    second = realize_availability(config, specs, replication_id="rep_02", rng=SemanticRngStreams(4))
    assert tuple(item.vehicle for item in first) == catalog.vehicle_keys
    assert tuple(item.vehicle for item in second) == catalog.vehicle_keys
    assert [item.availability_start_s for item in first] != [
        item.availability_start_s for item in second
    ]
    assert all(0 <= item.availability_start_s <= 20 for item in first)

    invalid = config.model_dump(mode="python")
    invalid["capacity_value"] = None
    with pytest.raises(ValidationError, match="requires capacity_value"):
        GeneratedSupplyConfig.model_validate(invalid)
