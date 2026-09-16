"""Offline geographic acquisition, preparation, and shared routing."""

from mobile_sensing.environment.capabilities import environment_capability_registry
from mobile_sensing.environment.grid import GridPreparationError, prepare_grid, prepare_population
from mobile_sensing.environment.models import (
    EnvironmentPreparationMetadata,
    LocalDatasetResource,
    LocalRegionSelection,
    PreparedEnvironment,
)
from mobile_sensing.environment.network import (
    NetworkPreparationError,
    PreparedRoutingService,
    prepare_network,
)
from mobile_sensing.environment.preparation import (
    LocalEnvironmentBuilder,
    PreparedEnvironmentReader,
)
from mobile_sensing.environment.provider import LocalDatasetCatalog, LocalFileEnvironmentProvider
from mobile_sensing.environment.repair import (
    NetworkDiagnosis,
    NetworkRepairError,
    NetworkRepairPlan,
    NetworkRepairResult,
    ScenarioImpactResult,
    apply_network_repair,
    diagnose_network,
    plan_network_repair,
    validate_scenario_impact,
)
from mobile_sensing.environment.snapping import NodeSnapper

__all__ = [
    "EnvironmentPreparationMetadata",
    "GridPreparationError",
    "LocalDatasetCatalog",
    "LocalDatasetResource",
    "LocalEnvironmentBuilder",
    "LocalFileEnvironmentProvider",
    "LocalRegionSelection",
    "NetworkPreparationError",
    "NetworkDiagnosis",
    "NetworkRepairError",
    "NetworkRepairPlan",
    "NetworkRepairResult",
    "NodeSnapper",
    "PreparedEnvironment",
    "PreparedEnvironmentReader",
    "PreparedRoutingService",
    "ScenarioImpactResult",
    "apply_network_repair",
    "diagnose_network",
    "environment_capability_registry",
    "prepare_grid",
    "prepare_network",
    "prepare_population",
    "plan_network_repair",
    "validate_scenario_impact",
]
