"""M09 FastAPI adapter over application services and persistent metadata."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

import pandas as pd
import pyarrow.parquet as pq
from fastapi import FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

from mobile_sensing.application import HeadlessApplication
from mobile_sensing.artifacts import partition_file
from mobile_sensing.datasets import load_registered_tabular_source
from mobile_sensing.environment import PreparedEnvironmentReader, environment_capability_registry
from mobile_sensing.jobs import (
    IdempotencyConflict,
    JobStore,
    JobStoreLimits,
    JobSubmission,
    ProjectCreate,
    RevisionConflict,
    RevisionCreate,
)

from mobile_sensing.api.models import (
    EnvironmentJobRequest,
    ExportJobRequest,
    ExposureJobRequest,
    GTFSJobRequest,
    ImportJobRequest,
    MatrixQueryRequest,
    MeanFleetView,
    PortfolioAnalysisJobRequest,
    PortfolioFrontierView,
    PortfolioPreviewRequest,
    ScenarioJobRequest,
    SimulationJobRequest,
)
from mobile_sensing.api.queries import (
    ArtifactCatalog,
    QueryLimitExceeded,
    query_map,
    query_matrix,
    query_operation_summary,
    query_portfolio_frontier,
    query_table,
)
from mobile_sensing.api.frontend import install_frontend_routes
from mobile_sensing.jobs.models import ProjectRecord, RevisionRecord, JobSnapshot


def _error(request: Request, status: int, code: str, message: str, issues=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "issues": issues or [],
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
    )


def _submission(snapshot, cache_hit: bool) -> JobSubmission:
    return JobSubmission(
        job_id=snapshot.job_id,
        resource_id=snapshot.resource_id,
        status_url=f"/api/v1/jobs/{snapshot.job_id}",
        events_url=f"/api/v1/jobs/{snapshot.job_id}/events",
        cache_hit=cache_hit,
    )


def create_app(
    artifact_root: str | Path,
    *,
    limits: JobStoreLimits | None = None,
    serve_frontend: bool = False,
    frontend_root: str | Path | None = None,
) -> FastAPI:
    """Create an API process. A coordinator is deliberately started separately."""

    root = Path(artifact_root).resolve()
    configured_limits = limits or JobStoreLimits()
    store = JobStore(root, configured_limits)
    application = HeadlessApplication(root)
    catalog = ArtifactCatalog(root)
    app = FastAPI(title="Mobile Sensing Local API", version="1.0.0")
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)
    app.state.artifact_root = root
    app.state.store = store

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        issues = [
            {
                "severity": "error",
                "code": "INVALID_REQUEST",
                "field_path": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
                "corrective_action": "Correct the indicated request field.",
            }
            for item in exc.errors()
        ]
        return _error(request, 422, "INVALID_REQUEST", "request validation failed", issues)

    @app.exception_handler(KeyError)
    async def not_found(request: Request, exc: KeyError):
        return _error(request, 404, "NOT_FOUND", f"unknown resource: {exc.args[0]}")

    @app.exception_handler(FileNotFoundError)
    async def file_not_found(request: Request, exc: FileNotFoundError):
        return _error(request, 404, "NOT_FOUND", str(exc))

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(request: Request, exc: IdempotencyConflict):
        return _error(request, 409, "IDEMPOTENCY_CONFLICT", str(exc))

    @app.exception_handler(RevisionConflict)
    async def revision_conflict(request: Request, exc: RevisionConflict):
        return _error(request, 409, "REFERENCE_CONFLICT", str(exc))

    @app.exception_handler(QueryLimitExceeded)
    async def query_limit(request: Request, exc: QueryLimitExceeded):
        return _error(request, 413, "QUERY_LIMIT_EXCEEDED", str(exc))

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError):
        return _error(request, 422, "INVALID_INPUT", str(exc))

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "api_version": "v1", "metadata_schema_version": 1}

    @app.get("/api/v1/capabilities")
    def capabilities():
        return environment_capability_registry()

    schema_models = {
        "environment": EnvironmentJobRequest,
        "import": ImportJobRequest,
        "gtfs-reconstruction": GTFSJobRequest,
        "scenario": ScenarioJobRequest,
        "simulation": SimulationJobRequest,
        "exposure": ExposureJobRequest,
        "portfolio": PortfolioAnalysisJobRequest,
        "matrix-query": MatrixQueryRequest,
    }

    @app.get("/api/v1/schemas/{name}")
    def schema(name: str):
        if name not in schema_models:
            raise KeyError(name)
        return schema_models[name].model_json_schema(mode="validation")

    @app.get("/api/v1/projects", response_model=list[ProjectRecord])
    def projects():
        return store.list_projects()

    @app.post("/api/v1/projects", status_code=201, response_model=ProjectRecord)
    def create_project(request: ProjectCreate):
        return store.create_project(request.name, request.description)

    @app.patch("/api/v1/projects/{project_id}", response_model=ProjectRecord)
    def rename_project(project_id: str, request: ProjectCreate):
        return store.rename_project(project_id, request.name, request.description)

    @app.get("/api/v1/projects/{project_id}", response_model=ProjectRecord)
    def project(project_id: str):
        return store.get_project(project_id)

    @app.post(
        "/api/v1/projects/{project_id}/revisions", status_code=201, response_model=RevisionRecord
    )
    def create_revision(project_id: str, request: RevisionCreate):
        return store.create_revision(project_id, request.base_revision_id, request.payload)

    @app.get("/api/v1/projects/{project_id}/revisions", response_model=list[RevisionRecord])
    def revisions(project_id: str):
        return store.list_revisions(project_id)

    @app.get("/api/v1/projects/{project_id}/revisions/{revision_id}", response_model=RevisionRecord)
    def revision(project_id: str, revision_id: str):
        return store.get_revision(project_id, revision_id)

    def submit(
        *,
        kind: str,
        operation: str,
        request_model,
        project_id: str | None,
        idempotency_key: str | None,
    ):
        snapshot, cache_hit = store.submit(
            kind=kind,
            operation=operation,
            payload=request_model.model_dump(mode="json", exclude_none=True),
            project_id=project_id,
            idempotency_key=idempotency_key,
        )
        return JSONResponse(
            status_code=200 if cache_hit else 202,
            content=_submission(snapshot, cache_hit).model_dump(mode="json"),
        )

    @app.post("/api/v1/uploads", status_code=201)
    async def upload(
        request: Request,
        file: Annotated[UploadFile, File()],
        provenance: Annotated[str, Form(min_length=1, max_length=200)] = "user_upload",
    ):
        suffix = Path(file.filename or "").suffix.casefold()
        if suffix not in {".csv", ".parquet"}:
            return _error(request, 422, "INVALID_UPLOAD", "upload must be CSV or Parquet")
        staging_root = root / ".upload-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary_directory = Path(tempfile.mkdtemp(prefix="upload-", dir=staging_root))
        safe_name = Path(file.filename or f"source{suffix}").name
        temporary = temporary_directory / safe_name
        size = 0
        try:
            with temporary.open("wb") as target:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > configured_limits.max_upload_bytes:
                        return _error(
                            request, 413, "UPLOAD_LIMIT_EXCEEDED", "upload exceeds configured limit"
                        )
                    target.write(chunk)
            source = application.register_upload(temporary, provenance=provenance)
            registration = source.registration
            store.register_resource(
                resource_id=registration.dataset_id,
                kind="upload",
                content_hash=registration.content_hash,
                artifact_kind=None,
                job_id=None,
                metadata=registration.model_dump(mode="json"),
            )
            if registration.format == "csv":
                preview = pd.read_csv(source.path, nrows=20, dtype="string", keep_default_na=False)
                columns = list(preview.columns)
                records = preview.to_dict(orient="records")
            else:
                table = pq.read_table(source.path).slice(0, 20)
                columns = table.column_names
                records = table.to_pylist()
            return {"registration": registration, "columns": columns, "preview": records}
        except (UnicodeError, ValueError) as exc:
            staging_id = f"staging_{uuid.uuid4().hex}"
            retained = root / "staging_uploads" / staging_id
            retained.parent.mkdir(parents=True, exist_ok=True)
            temporary_directory.replace(retained)
            return _error(
                request,
                422,
                "INVALID_UPLOAD",
                str(exc),
                [
                    {
                        "severity": "error",
                        "code": "INVALID_UPLOAD",
                        "field_path": "file",
                        "dataset_id": staging_id,
                        "message": str(exc),
                        "corrective_action": "Inspect the staged file and correct its format.",
                    }
                ],
            )
        finally:
            if temporary_directory.exists():
                temporary.unlink(missing_ok=True)
                temporary_directory.rmdir()

    @app.get("/api/v1/datasets")
    def datasets():
        return [
            item
            for item in store.list_resources()
            if item.kind == "upload" or item.artifact_kind == "dataset"
        ]

    @app.get("/api/v1/datasets/{dataset_id}")
    def dataset(dataset_id: str):
        try:
            record = store.get_resource(dataset_id)
            if record.kind != "upload" and record.artifact_kind != "dataset":
                raise KeyError(dataset_id)
            return record
        except KeyError:
            located = catalog.locate(dataset_id)
            if located.reference.artifact_kind != "dataset":
                raise KeyError(dataset_id)
            return located.manifest

    @app.get("/api/v1/datasets/{dataset_id}/preview")
    def dataset_preview(
        dataset_id: str,
        page_size: int = Query(default=100, ge=1),
        cursor: str | None = None,
    ):
        if page_size > configured_limits.max_page_size:
            raise QueryLimitExceeded("page_size exceeds the configured result limit")
        try:
            source = load_registered_tabular_source(dataset_id, artifact_root=root)
        except FileNotFoundError:
            located = catalog.locate(dataset_id)
            if located.reference.artifact_kind != "dataset":
                raise KeyError(dataset_id)
            table_name = next(
                (
                    name
                    for name in ("tasks", "vehicle_catalog", "rates", "locations")
                    if name in located.manifest.expected_table_names
                ),
                None,
            )
            if table_name is None:
                raise ValueError("dataset has no previewable normalized table")
            return query_table(
                catalog,
                dataset_id,
                table_name,
                filters={},
                page_size=page_size,
                cursor=cursor,
                limits=configured_limits,
            )
        if source.registration.format == "csv":
            chunks = (
                frame.to_dict(orient="records")
                for frame in pd.read_csv(
                    source.path,
                    chunksize=10_000,
                    dtype="string",
                    keep_default_na=False,
                )
            )
        else:
            chunks = (
                batch.to_pylist()
                for batch in pq.ParquetFile(source.path).iter_batches(batch_size=10_000)
            )
        if cursor is None:
            offset = 0
        else:
            try:
                cursor_value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
                offset = int(cursor_value["offset"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid dataset preview cursor") from exc
            if offset < 0 or cursor_value.get("content_hash") != source.registration.content_hash:
                raise ValueError("dataset preview cursor belongs to another source")
        rows = []
        seen = 0
        for chunk in chunks:
            for row in chunk:
                if seen >= offset and len(rows) < page_size + 1:
                    rows.append(row)
                seen += 1
            if len(rows) > page_size:
                break
        complete = len(rows) <= page_size
        rows = rows[:page_size]
        following = offset + len(rows)
        next_cursor = None
        if not complete:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "content_hash": source.registration.content_hash,
                        "offset": following,
                    },
                    sort_keys=True,
                ).encode()
            ).decode()
        return {
            "items": rows,
            "returned_count": len(rows),
            "next_cursor": next_cursor,
            "is_complete": complete,
        }

    @app.get("/api/v1/datasets/{dataset_id}/issues")
    def dataset_issues(
        dataset_id: str,
        page_size: int = Query(default=100, ge=1),
        cursor: str | None = None,
    ):
        try:
            record = store.get_resource(dataset_id)
            if record.kind == "upload":
                return {
                    "items": [],
                    "returned_count": 0,
                    "total_matching_count": 0,
                    "next_cursor": None,
                    "is_complete": True,
                }
        except KeyError:
            pass
        located = catalog.locate(dataset_id)
        if located.reference.artifact_kind != "dataset":
            raise KeyError(dataset_id)
        if "validation_issues" not in located.manifest.expected_table_names:
            return {
                "items": [],
                "returned_count": 0,
                "total_matching_count": 0,
                "next_cursor": None,
                "is_complete": True,
            }
        return query_table(
            catalog,
            dataset_id,
            "validation_issues",
            filters={},
            page_size=page_size,
            cursor=cursor,
            limits=configured_limits,
        )

    @app.delete("/api/v1/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str):
        record = store.get_resource(dataset_id)
        if record.kind != "upload" and record.artifact_kind != "dataset":
            raise RevisionConflict("resource is not a dataset")
        artifact = record.metadata.get("artifact")
        artifact_id = artifact.get("artifact_id") if isinstance(artifact, dict) else dataset_id
        dependents = sorted(
            set(store.resource_dependents(dataset_id)) | set(store.resource_dependents(artifact_id))
        )
        if dependents:
            raise RevisionConflict("resource is referenced by: " + ", ".join(dependents))
        store.delete_resource(dataset_id)
        directory = (
            root / "raw_inputs" / dataset_id
            if record.kind == "upload"
            else root / "datasets" / artifact_id
        )
        if directory.is_dir():
            shutil.rmtree(directory)
        return {"deleted": dataset_id}

    @app.post("/api/v1/imports")
    def imports(
        body: ImportJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="import",
            operation="imports",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.post("/api/v1/environments")
    def environments(
        body: EnvironmentJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="environment",
            operation="environments",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.get("/api/v1/environments/{environment_id}")
    def environment(environment_id: str):
        located = catalog.locate(environment_id)
        if located.reference.artifact_kind != "environment":
            raise KeyError(environment_id)
        return PreparedEnvironmentReader(root).read(located.reference).metadata

    @app.post("/api/v1/gtfs-reconstructions")
    def gtfs(
        body: GTFSJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="gtfs_reconstruction",
            operation="gtfs-reconstructions",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.post("/api/v1/scenarios/validate")
    def validate_scenario(
        body: ScenarioJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="scenario_validation",
            operation="scenario-validation",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.post("/api/v1/simulations")
    def simulations(
        body: SimulationJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="simulation",
            operation="simulations",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.get("/api/v1/simulations")
    def list_simulations():
        return [item for item in store.list_resources(kind="simulation")]

    @app.get("/api/v1/simulations/{simulation_id}")
    def simulation(simulation_id: str):
        located = catalog.locate(simulation_id)
        if located.reference.artifact_kind != "simulation":
            raise KeyError(simulation_id)
        return located.manifest

    @app.post("/api/v1/exposures")
    def exposures(
        body: ExposureJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="exposure",
            operation="exposures",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.get("/api/v1/exposures/{exposure_id}")
    def exposure(exposure_id: str):
        located = catalog.locate(exposure_id)
        if located.reference.artifact_kind != "exposure":
            raise KeyError(exposure_id)
        return located.manifest

    @app.post("/api/v1/portfolio-enumerations/preview")
    def preview(body: PortfolioPreviewRequest):
        return application.preview_portfolios(body.exposure, body.config, limits=body.limits)

    @app.post("/api/v1/portfolio-analyses")
    def portfolio_analyses(
        body: PortfolioAnalysisJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return submit(
            kind="portfolio",
            operation="portfolio-analyses",
            request_model=body,
            project_id=project_id,
            idempotency_key=key,
        )

    @app.get("/api/v1/portfolio-analyses/{analysis_id}")
    def portfolio_analysis(analysis_id: str):
        located = catalog.locate(analysis_id)
        if located.reference.artifact_kind != "portfolio" or (
            located.manifest.scientific_identity.algorithm_versions.get("storage")
            not in {
                "portfolio-analysis-parquet@1",
                "portfolio-analysis-parquet@2",
                "portfolio-analysis-parquet@3",
            }
        ):
            raise KeyError(analysis_id)
        return located.manifest

    @app.get("/api/v1/portfolio-samples/{samples_id}")
    def portfolio_samples(samples_id: str):
        located = catalog.locate(samples_id)
        if located.reference.artifact_kind != "portfolio" or (
            located.manifest.scientific_identity.algorithm_versions.get("storage")
            not in {"portfolio-samples-parquet@1", "portfolio-samples-reconstruct@2"}
        ):
            raise KeyError(samples_id)
        return located.manifest

    @app.get("/api/v1/portfolio-frontiers/{analysis_id}", response_model=PortfolioFrontierView)
    def portfolio_frontier(analysis_id: str, budget_id: str):
        return query_portfolio_frontier(
            root,
            analysis_id,
            budget_id=budget_id,
            limits=configured_limits,
        )

    @app.get("/api/v1/jobs/{job_id}", response_model=JobSnapshot)
    def job(job_id: str):
        return store.get_job(job_id)

    @app.get("/api/v1/jobs", response_model=list[JobSnapshot])
    def jobs(project_id: str | None = None):
        return store.list_jobs(project_id=project_id)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobSnapshot)
    def cancel(job_id: str):
        return store.request_cancel(job_id)

    @app.get("/api/v1/jobs/{job_id}/events")
    async def events(
        request: Request,
        job_id: str,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        try:
            cursor = int(last_event_id or 0)
        except ValueError:
            return _error(request, 422, "INVALID_EVENT_CURSOR", "Last-Event-ID must be an integer")

        async def stream():
            nonlocal cursor
            while True:
                persisted, reset = store.events_after(job_id, cursor)
                if reset:
                    snapshot = store.get_job(job_id).model_dump(mode="json")
                    yield f"event: reset\ndata: {json.dumps(snapshot, separators=(',', ':'))}\n\n"
                for event in persisted:
                    cursor = event.event_id
                    payload = event.model_dump(mode="json")
                    yield f"id: {event.event_id}\nevent: {event.type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                if store.get_job(job_id).status in {"completed", "failed", "cancelled"}:
                    return
                if await request.is_disconnected():
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/v1/results/{resource_id}/{table_name}")
    def results(
        resource_id: str,
        table_name: str,
        request: Request,
        page_size: int = Query(default=configured_limits.default_page_size, ge=1),
        cursor: str | None = None,
        time_start_s: float | None = None,
        time_end_s: float | None = None,
    ):
        reserved = {"page_size", "cursor", "time_start_s", "time_end_s"}
        filters = {key: value for key, value in request.query_params.items() if key not in reserved}
        return query_table(
            catalog,
            resource_id,
            table_name,
            filters=filters,
            page_size=page_size,
            cursor=cursor,
            limits=configured_limits,
            time_start_s=time_start_s,
            time_end_s=time_end_s,
        )

    @app.get("/api/v1/maps/{resource_id}/{layer}")
    def maps(
        resource_id: str,
        layer: str,
        bbox: str | None = None,
        replication_id: str | None = None,
        fleet_id: str | None = None,
        vehicle_id: str | None = None,
        time_start_s: float | None = None,
        time_end_s: float | None = None,
        aggregation: str | None = None,
    ):
        return JSONResponse(
            query_map(
                root,
                resource_id,
                layer,
                bbox=bbox,
                replication_id=replication_id,
                fleet_id=fleet_id,
                vehicle_id=vehicle_id,
                time_start_s=time_start_s,
                time_end_s=time_end_s,
                limits=configured_limits,
                aggregation=aggregation,
            )
        )

    @app.post("/api/v1/matrix-queries")
    def matrix(body: MatrixQueryRequest):
        return JSONResponse(query_matrix(root, body, configured_limits))

    @app.get("/api/v1/fleet-summaries/{exposure_id}", response_model=MeanFleetView)
    def mean_fleet_summary(
        exposure_id: str,
        fleet_id: str | None = None,
        vehicle_id: str | None = None,
        time_start_s: float | None = None,
        time_end_s: float | None = None,
    ):
        from mobile_sensing.api.fleet_queries import fleet_summary

        return fleet_summary(
            root,
            exposure_id,
            fleet_id=fleet_id,
            vehicle_id=vehicle_id,
            time_start_s=time_start_s,
            time_end_s=time_end_s,
            limits=configured_limits,
        )

    @app.get("/api/v1/operation-summaries/{simulation_id}")
    def operation_summary(
        simulation_id: str,
        replication_id: str,
        fleet_id: str | None = None,
        vehicle_id: str | None = None,
        time_start_s: float | None = None,
        time_end_s: float | None = None,
    ):
        return query_operation_summary(
            root,
            simulation_id,
            replication_id=replication_id,
            fleet_id=fleet_id,
            vehicle_id=vehicle_id,
            time_start_s=time_start_s,
            time_end_s=time_end_s,
        )

    @app.post("/api/v1/exports")
    def exports(
        body: ExportJobRequest,
        project_id: Annotated[str | None, Header(alias="X-Project-ID")] = None,
        key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        located = catalog.locate(body.resource_id)
        try:
            table = next(item for item in located.manifest.tables if item.name == body.table)
        except StopIteration as exc:
            raise KeyError(body.table) from exc
        if located.reference.artifact_kind == "environment":
            paths = [str((located.directory / table.relative_path).relative_to(root))]
        else:
            paths = [
                str(
                    partition_file(located.directory / table.relative_path, index).relative_to(root)
                )
                for index, partition in enumerate(table.partitions)
                if partition.row_count
            ]
        payload = {
            "source_paths": paths,
            "format": body.format,
            "source_resource_id": body.resource_id,
            "table": body.table,
        }
        snapshot, hit = store.submit(
            kind="export",
            operation="exports",
            payload=payload,
            project_id=project_id,
            idempotency_key=key,
        )
        return JSONResponse(
            status_code=200 if hit else 202,
            content=_submission(snapshot, hit).model_dump(mode="json"),
        )

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download(artifact_id: str):
        record = store.get_resource(artifact_id)
        path_value = record.metadata.get("download_path")
        if not isinstance(path_value, str):
            raise KeyError(artifact_id)
        path = (root / path_value).resolve()
        try:
            path.relative_to((root / "exports").resolve())
        except ValueError as exc:
            raise ValueError("invalid managed export path") from exc
        if not path.is_file():
            raise KeyError(artifact_id)
        media = (
            "application/zip"
            if path.suffix == ".zip"
            else "text/csv" if path.suffix == ".csv" else "application/vnd.apache.parquet"
        )
        return FileResponse(path, media_type=media, filename=path.name)

    from mobile_sensing.api.workbench import install_workbench_routes

    install_workbench_routes(app, root, store)
    from mobile_sensing.api.project_routes import install_project_routes

    install_project_routes(app, root, store)

    if frontend_root is not None and not serve_frontend:
        raise ValueError("frontend_root requires serve_frontend=True")
    if serve_frontend:
        install_frontend_routes(app, frontend_root)
    return app
