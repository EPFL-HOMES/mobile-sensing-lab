"""Structural extension boundaries for later implementation milestones."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Protocol, runtime_checkable

from mobile_sensing.contracts.artifacts import (
    ArtifactRef,
    CatalogIdentity,
    EnvironmentArtifactRef,
    ExposureReadMetadata,
    JointReplicationIdentity,
    PortfolioCountRecord,
    RawGeographicBundle,
    ReplicationSetIdentity,
    SamplingOrderingRecord,
    SamplingRoundRecord,
    SensingMatrixQuery,
    SensingMatrixSlice,
    SparseExposureChunk,
    SparseExposureRow,
)
from mobile_sensing.contracts.configuration import (
    DemandConfig,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    FleetConfig,
    PortfolioConfig,
    SupplyConfig,
)
from mobile_sensing.contracts.records import (
    AssignmentRow,
    ExecutionPlan,
    LocationRef,
    RouteResult,
    Task,
    VehicleKey,
    VehicleAvailability,
    VehicleSpec,
    VehicleState,
)


@runtime_checkable
class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...


@runtime_checkable
class ProgressSink(Protocol):
    def update(self, *, phase: str, completed: int, total: int | None) -> None: ...


@runtime_checkable
class NamedRngStreamProvider(Protocol):
    def stream(self, *semantic_labels: str) -> object: ...


class ReplicationContext(Protocol):
    replication: JointReplicationIdentity
    environment: EnvironmentArtifactRef
    catalog: CatalogIdentity
    rng: NamedRngStreamProvider
    cancellation: CancellationToken
    progress: ProgressSink


class EnvironmentProvider(Protocol):
    capability_key: str

    def acquire(
        self,
        request: EnvironmentProviderRequest,
        *,
        cancellation: CancellationToken,
        progress: ProgressSink,
    ) -> RawGeographicBundle: ...


class EnvironmentBuilder(Protocol):
    def prepare(
        self,
        raw_bundle: RawGeographicBundle,
        config: EnvironmentBuildConfig,
        *,
        cancellation: CancellationToken,
        progress: ProgressSink,
    ) -> EnvironmentArtifactRef: ...


class DemandAdapter(Protocol):
    capability_key: str

    def normalize(
        self,
        source: ArtifactRef,
        config: DemandConfig,
        *,
        cancellation: CancellationToken,
        progress: ProgressSink,
    ) -> ArtifactRef: ...


class DemandSource(Protocol):
    def tasks(self, config: DemandConfig, context: ReplicationContext) -> Iterator[Task]: ...


class SupplySource(Protocol):
    def build_catalog(self, config: SupplyConfig) -> tuple[VehicleSpec, ...]: ...

    def realize_availability(
        self,
        config: SupplyConfig,
        catalog: Sequence[VehicleSpec],
        context: ReplicationContext,
    ) -> tuple[VehicleAvailability, ...]: ...


class EligibilityFilter(Protocol):
    def feasible_pairs(
        self,
        vehicles: Sequence[VehicleState],
        tasks: Sequence[Task],
        fleet: FleetConfig,
    ) -> Iterable[tuple[VehicleKey, str]]: ...


class DispatchPolicy(Protocol):
    capability_key: str

    def assign(
        self,
        vehicles: Sequence[VehicleState],
        tasks: Sequence[Task],
        feasible_pairs: Iterable[tuple[VehicleKey, str]],
    ) -> tuple[AssignmentRow, ...]: ...


class RoutingService(Protocol):
    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult: ...


class OperationalRoutingService(RoutingService, Protocol):
    """Routing extension required only by generated neighboring-node cruising."""

    def outgoing_neighbor_node_ids(
        self, profile_id: str, source_node_id: str
    ) -> tuple[str, ...]: ...

    def location_for_node(self, node_id: str) -> LocationRef: ...


class TaskExecutor(Protocol):
    def plan(
        self,
        vehicle: VehicleState,
        task: Task,
        routing: RoutingService,
        *,
        assigned_at_s: float,
        execution_token: int,
    ) -> ExecutionPlan: ...


class OperationalPolicy(Protocol):
    capability_key: str

    def next_task(
        self,
        vehicle: VehicleState,
        waiting_tasks: Sequence[Task],
        context: ReplicationContext,
    ) -> Task | None: ...


class ExposureAllocator(Protocol):
    def allocate(
        self,
        simulation: ArtifactRef,
        *,
        cancellation: CancellationToken,
        progress: ProgressSink,
    ) -> ArtifactRef: ...


class ExposureReader(Protocol):
    def read_sparse_chunks(
        self,
        exposure: ArtifactRef,
        *,
        replication_ids: Sequence[str],
        vehicle_keys: Sequence[VehicleKey],
    ) -> tuple[ExposureReadMetadata, Iterator[SparseExposureChunk]]: ...


class SensingQueryService(Protocol):
    def query(self, exposure: ArtifactRef, request: SensingMatrixQuery) -> SensingMatrixSlice: ...


class UtilityFunction(Protocol):
    capability_key: str

    def __call__(self, sparse_matrix: Iterable[SparseExposureRow]) -> float: ...


class CountEnumerator(Protocol):
    def enumerate(
        self, config: PortfolioConfig, catalog: CatalogIdentity
    ) -> Iterator[PortfolioCountRecord]: ...


class AllocationSampler(Protocol):
    def draw(
        self,
        *,
        round_id: int,
        catalog: CatalogIdentity,
        complete_replications: ReplicationSetIdentity,
        sampling_seed: int,
    ) -> tuple[SamplingRoundRecord, tuple[SamplingOrderingRecord, ...]]: ...


class PortfolioEvaluator(Protocol):
    def evaluate(
        self,
        config: PortfolioConfig,
        exposure: ArtifactRef,
        *,
        cancellation: CancellationToken,
        progress: ProgressSink,
    ) -> ArtifactRef: ...


class FrontierBuilder(Protocol):
    def build(self, portfolio: ArtifactRef, budget_minor: int) -> ArtifactRef: ...
