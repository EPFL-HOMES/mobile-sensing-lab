from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mobile_sensing.contracts import (
    EnvironmentBuildConfig,
    PortfolioConfig,
    ProjectRevision,
    ScenarioConfig,
    canonical_json_bytes,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "examples"


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("environment_build_config.json", EnvironmentBuildConfig),
        ("portfolio_config.json", PortfolioConfig),
        ("project_revision.json", ProjectRevision),
        ("scenario_config.json", ScenarioConfig),
    ],
)
def test_schema_round_trip_is_canonical(filename: str, model: type) -> None:
    content = (FIXTURES / filename).read_bytes().rstrip(b"\n")
    parsed = model.model_validate_json(content)
    assert canonical_json_bytes(parsed) == content
    assert model.model_validate_json(parsed.model_dump_json()) == parsed


def test_unknown_nonfinite_numeric_and_legacy_values_are_rejected() -> None:
    scenario = json.loads((FIXTURES / "scenario_config.json").read_text())

    unknown = copy.deepcopy(scenario)
    unknown["sensor_counts"] = {"fleet_001": 1}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ScenarioConfig.model_validate_json(json.dumps(unknown))

    legacy = copy.deepcopy(scenario)
    legacy["schema_version"] = "1.0"
    with pytest.raises(ValidationError, match="literal_error"):
        ScenarioConfig.model_validate_json(json.dumps(legacy))

    nonfinite = copy.deepcopy(scenario)
    nonfinite["clock"]["end_s"] = float("nan")
    with pytest.raises(ValidationError, match="finite_number"):
        ScenarioConfig.model_validate_json(json.dumps(nonfinite))

    numeric_id = copy.deepcopy(scenario)
    numeric_id["environment_id"] = 7
    with pytest.raises(ValidationError, match="string_type"):
        ScenarioConfig.model_validate_json(json.dumps(numeric_id))

    for filename, model in (
        ("environment_build_config.json", EnvironmentBuildConfig),
        ("portfolio_config.json", PortfolioConfig),
        ("project_revision.json", ProjectRevision),
    ):
        versioned = json.loads((FIXTURES / filename).read_text())
        versioned["schema_version"] = "1.0"
        with pytest.raises(ValidationError, match="literal_error"):
            model.model_validate_json(json.dumps(versioned))


def test_configuration_discriminators_exclude_unsupported_algorithms() -> None:
    scenario = json.loads((FIXTURES / "scenario_config.json").read_text())

    batch = copy.deepcopy(scenario)
    batch["fleets"][0]["dispatch"] = {"policy": "batch", "batch_interval_s": 30}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ScenarioConfig.model_validate_json(json.dumps(batch))

    tsp = copy.deepcopy(scenario)
    tsp["fleets"][0]["routing"]["tsp"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ScenarioConfig.model_validate_json(json.dumps(tsp))

    uploaded = copy.deepcopy(scenario)
    uploaded["fleets"][0]["demand"] = {
        "source": "upload",
        "structure": "od",
        "adapter": "demand.upload_od@1",
        "parameters": {
            "dataset_id": "tasks_001",
            "mapping_id": "mapping_001",
            "error_policy": "strict",
        },
    }
    assert (
        ScenarioConfig.model_validate_json(json.dumps(uploaded)).fleets[0].demand.source == "upload"
    )

    invalid_supply = copy.deepcopy(scenario)
    invalid_supply["fleets"][0]["supply"]["source"] = "route_as_vehicle"
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ScenarioConfig.model_validate_json(json.dumps(invalid_supply))

    invalid_depot = copy.deepcopy(scenario)
    invalid_depot["fleets"][0]["supply"]["depot_policy"] = {
        "policy": "return_when_blocked",
        "implementation": "operational.depot_return@1",
    }
    with pytest.raises(ValidationError, match="consumable capacity"):
        ScenarioConfig.model_validate_json(json.dumps(invalid_depot))


def test_operational_replications_and_sampling_rounds_are_distinct_contracts() -> None:
    scenario = json.loads((FIXTURES / "scenario_config.json").read_text())
    portfolio = json.loads((FIXTURES / "portfolio_config.json").read_text())

    contaminated_scenario = copy.deepcopy(scenario)
    contaminated_scenario["sampling_rounds"] = 100
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ScenarioConfig.model_validate_json(json.dumps(contaminated_scenario))

    contaminated_portfolio = copy.deepcopy(portfolio)
    contaminated_portfolio["replications"] = 2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PortfolioConfig.model_validate_json(json.dumps(contaminated_portfolio))

    mixed_levels = copy.deepcopy(portfolio)
    mixed_levels["count_enumeration"]["fleets"]["fleet_001"] = {
        "count_levels": [0, 2, 4],
        "min_count": 0,
        "max_count": 4,
        "step": 2,
    }
    with pytest.raises(ValidationError):
        PortfolioConfig.model_validate_json(json.dumps(mixed_levels))

    wrong_design = copy.deepcopy(portfolio)
    wrong_design["sampling_design"] = "independent_vehicle_replications"
    with pytest.raises(ValidationError, match="literal_error"):
        PortfolioConfig.model_validate_json(json.dumps(wrong_design))

    mismatched_costs = copy.deepcopy(portfolio)
    mismatched_costs["costs"]["by_fleet_minor"] = {"other_fleet": 10}
    with pytest.raises(ValidationError, match="identical fleet IDs"):
        PortfolioConfig.model_validate_json(json.dumps(mismatched_costs))
