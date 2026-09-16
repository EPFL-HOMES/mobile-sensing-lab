"""Human authoring API; domain execution remains in durable workers."""

from __future__ import annotations

import tempfile
import json
from pathlib import Path
from typing import Annotated

from fastapi import File, Form, UploadFile, Header
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from mobile_sensing.contracts import scientific_hash

from mobile_sensing.datasets.inputs import (
    InputDescriptor,
    InputRegistration,
    InputRole,
    InputCrsConfirmation,
    snapshot_input,
    load_input,
)
from mobile_sensing.application.studio_models import (
    EnvironmentEditor,
    FeatureSelection,
    EnvironmentEditorRequest,
    EnvironmentResult,
    WorkspaceInfo,
    RegionSearchRequest,
    RegionSearchResult,
)
from mobile_sensing.jobs.models import JobSubmission
from mobile_sensing.application.project_models import (
    ProjectConfig,
    ProjectConfigRequest,
    FleetEditor,
    ProjectResolutionResult,
    PortfolioEditor,
    PortfolioFleetEditor,
)
from mobile_sensing.application.run_models import (
    StudioRunRequest,
    StudioAnalysisRequest,
    RunView,
    AnalysisView,
    UtilityCurveRequest,
    UtilityCurve,
    MigrationView,
    RunOptions,
)
from mobile_sensing.application.example_bundle import (
    ExampleInfo,
    ExampleOpenRequest,
    ExampleOpenResponse,
    example_info as read_example_info,
    bundle_manifest as read_bundle_manifest,
    bundle_installed,
)
from mobile_sensing.jobs.models import ProjectRecord


from mobile_sensing.application.run_management import (
    RunDeletionView,
    deleted_run_ids,
    deletion_preview,
    delete_run,
    restore_run,
)
from mobile_sensing.application.temporal_preview import TemporalPreview, temporal_preview
from mobile_sensing.application.calendar_authoring import CalendarPreview, resolve_calendar
from mobile_sensing.application.project_models import SimulationEditor, NumericRange


def bundle_manifest():
    """Compatibility seam for the default bundled example and tests."""

    return read_bundle_manifest()


def install_workbench_routes(app, root, store):
    from threading import Lock

    workspace_lock = Lock()
    selection = Path(root) / "example-selection.json"
    example_key = (
        json.loads(selection.read_text()).get("example_key", "lausanne")
        if selection.is_file()
        else "lausanne"
    )

    def selected_bundle_manifest():
        return (
            bundle_manifest()
            if example_key == "lausanne"
            else read_bundle_manifest(example_key=example_key)
        )

    def example_info(root):
        return read_example_info(root, example_key=example_key)

    @app.post("/api/v1/workbench/calendar-preview", response_model=CalendarPreview)
    def preview_calendar(request: ProjectConfigRequest):
        return resolve_calendar(root, request.config)[1]

    @app.post("/api/v1/workbench/temporal-preview", response_model=TemporalPreview)
    def preview_temporal(request: ProjectConfigRequest):
        return temporal_preview(root, request.config)

    @app.post("/api/v1/workbench/portfolio-preview", response_model=PortfolioEditor)
    def preview_portfolio(editor: PortfolioEditor):
        from mobile_sensing.application.run_pipeline import read_named_record
        from mobile_sensing.application.temporal_authoring import resolve_portfolio

        run = read_named_record(root, editor.source_run_id, "studio_run")
        return resolve_portfolio(editor, run.vehicle_counts)

    @app.post("/api/v1/workbench/workspace/initialize", response_model=WorkspaceInfo)
    def initialize_workspace():
        with workspace_lock:
            from mobile_sensing.jobs.project_files import write_json

            marker = Path(root) / "workspace.json"
            if marker.exists():
                value = json.loads(marker.read_text())
                if value.get("example_job_id"):
                    previous_job = store.get_job(value["example_job_id"])
                    current_bundle_id = selected_bundle_manifest().bundle_id
                    accepted_hashes = {
                        scientific_hash(
                            {
                                "operation": "example-import@1",
                                "payload": {
                                    "bundle_id": current_bundle_id,
                                    "editable": False,
                                    "example_key": example_key,
                                },
                            }
                        ),
                        scientific_hash(
                            {
                                "operation": "example-import@1",
                                "payload": {
                                    "bundle_id": current_bundle_id,
                                    "editable": False,
                                },
                            }
                        ),
                    }
                    if (
                        previous_job.status
                        in {
                            "failed",
                            "cancelled",
                        }
                        and previous_job.request_hash not in accepted_hashes
                    ):
                        # A new distribution may recover a failed older installation.
                        value["example_job_id"] = None
                if not value.get("example_job_id"):
                    try:
                        current = store.get_project(value["example_project_id"])
                        revision = (
                            store.get_revision(current.project_id, current.current_revision_id)
                            if current.current_revision_id
                            else None
                        )
                    except KeyError:
                        revision = None
                    if (
                        revision
                        and revision.payload.get("read_only")
                        and revision.payload.get("example_bundle_id")
                        != selected_bundle_manifest().bundle_id
                    ):
                        opened = open_example(ExampleOpenRequest(editable=False))
                        value.update(
                            example_project_id=opened.project_id, example_job_id=opened.job_id
                        )
                        write_json(marker, value)
            else:
                opened = open_example(ExampleOpenRequest(editable=False))
                value = {"example_project_id": opened.project_id, "example_job_id": opened.job_id}
                write_json(marker, value)
            job_id = value.get("example_job_id")
            if job_id:
                job = store.get_job(job_id)
                if job.status == "completed":
                    finalize_example(job_id)
                    value["example_job_id"] = None
                    write_json(marker, value)
            store.list_projects()
            return WorkspaceInfo(directory=str(Path(root).resolve()), **value)

    @app.get("/api/v1/workbench/workspace", response_model=WorkspaceInfo)
    def workspace_info():
        return WorkspaceInfo(directory=str(Path(root).resolve()))

    @app.get("/api/v1/examples/lausanne", response_model=ExampleInfo)
    def example():
        return example_info(root)

    def example_config(bundle, editable):
        return bundle.config.model_copy(
            update={
                "read_only": not editable,
                "example_bundle_id": bundle.bundle_id,
                "saved_views": bundle.saved_views,
            }
        )

    def register_example_inputs(config):
        identifiers = set()

        def visit(value):
            if isinstance(value, str) and value.startswith("input_"):
                identifiers.add(value)
            elif isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(config.model_dump(mode="json"))
        for identifier in sorted(identifiers):
            metadata, _ = load_input(root, identifier)
            store.register_resource(
                resource_id=identifier,
                kind="input",
                content_hash=metadata["content_hash"],
                artifact_kind=None,
                job_id=None,
                metadata=metadata,
            )

    def save_example(project, config):
        payload = config.model_dump(mode="json")
        if project.current_revision_id:
            previous = store.get_revision(project.project_id, project.current_revision_id).payload
            if previous.get("example_bundle_id") == config.example_bundle_id:
                return
        store.create_revision(
            project.project_id,
            project.current_revision_id,
            payload,
            reference_upgrade=bool(project.current_revision_id),
        )

    @app.post("/api/v1/examples/lausanne/open", response_model=ExampleOpenResponse)
    def open_example(request: ExampleOpenRequest):
        bundle = selected_bundle_manifest()
        if bundle_installed(root, bundle):
            register_example_inputs(bundle.config)
        existing_reference = None
        if not request.editable:
            for project in store.list_projects():
                if project.current_revision_id:
                    revision = store.get_revision(project.project_id, project.current_revision_id)
                    if revision.payload.get(
                        "example_bundle_id"
                    ) == bundle.bundle_id and revision.payload.get("read_only"):
                        return ExampleOpenResponse(project_id=project.project_id)
                    if revision.payload.get("example_bundle_id") and revision.payload.get(
                        "read_only"
                    ):
                        existing_reference = project
        project = existing_reference or store.create_project(
            bundle.name,
            bundle.description,
            unique_suffix=" editable copy" if request.editable else "",
        )
        if bundle_installed(root, bundle):
            save_example(project, example_config(bundle, request.editable))
            return ExampleOpenResponse(project_id=project.project_id)
        job, _ = store.submit(
            kind="example_import",
            operation="example-import@1",
            payload={
                "bundle_id": bundle.bundle_id,
                "editable": request.editable,
                "example_key": example_key,
            },
            project_id=project.project_id,
            idempotency_key=None,
        )
        return ExampleOpenResponse(project_id=project.project_id, job_id=job.job_id)

    @app.post("/api/v1/examples/lausanne/finalize/{job_id}", response_model=ProjectRecord)
    def finalize_example(job_id: str):
        job = store.get_job(job_id)
        if job.kind != "example_import" or job.status != "completed":
            raise ValueError("Wait for the example installation to complete")
        project = store.get_project(job.project_id)
        if (
            project.current_revision_id is None
            or store.get_revision(project.project_id, project.current_revision_id).payload.get(
                "example_bundle_id"
            )
            != job.result["bundle_id"]
        ):
            config = ProjectConfig.model_validate_json(json.dumps(job.result["config"]))
            register_example_inputs(config)
            config = config.model_copy(
                update={
                    "read_only": not job.result["editable"],
                    "example_bundle_id": job.result["bundle_id"],
                    "saved_views": job.result["saved_views"],
                }
            )
            save_example(project, config)
        return store.get_project(project.project_id)

    @app.get("/api/v1/workbench/run-options", response_model=RunOptions)
    def run_options():
        return RunOptions(workers=2)

    @app.post("/api/v1/workbench/projects/{project_id}/copy", status_code=201)
    def copy_project(project_id: str, request: ProjectConfigRequest):
        original = store.get_project(project_id)
        config = request.config
        jobs = store.list_jobs(project_id=project_id)
        linked_runs = set(config.linked_run_ids)
        linked_analyses = set(config.linked_analysis_ids)
        for job in jobs:
            if job.status == "completed" and job.kind == "studio_run":
                linked_runs.add(job.result["run_id"])
            if job.status == "completed" and job.kind == "studio_analysis":
                linked_analyses.add(job.result["analysis_id"])
        copied = store.create_project(original.name, original.description, unique_suffix=" copy")
        config = config.model_copy(
            update={
                "linked_run_ids": tuple(sorted(linked_runs)),
                "linked_analysis_ids": tuple(sorted(linked_analyses)),
                "source_revision": original.current_revision_id,
                "read_only": False,
            }
        )
        store.create_revision(copied.project_id, None, config.model_dump(mode="json"))
        return store.get_project(copied.project_id)

    @app.post("/api/v1/jobs/{job_id}/retry")
    def retry(job_id: str):
        return store.retry(job_id)

    @app.get("/api/v1/workbench/runs/{run_id}/portfolio-defaults", response_model=PortfolioEditor)
    def portfolio_defaults(run_id: str):
        from mobile_sensing.application.run_pipeline import read_named_record

        run = read_named_record(root, run_id, "studio_run")
        return PortfolioEditor(
            source_run_id=run_id,
            risk_metric="p05",
            utility_temporal_resolution_minutes=1440,
            fleets=tuple(
                PortfolioFleetEditor(
                    fleet_id=fleet,
                    counts=tuple(sorted(set(range(0, count + 1, 5)) | {count})),
                    count_range=NumericRange(minimum=0, maximum=count, step=5),
                )
                for fleet, count in sorted(run.vehicle_counts.items())
            ),
        )

    @app.get("/api/v1/workbench/projects/{project_id}/migration", response_model=MigrationView)
    def migration(project_id: str):
        from mobile_sensing.application.migration import migrate_project

        project = store.get_project(project_id)
        if not project.current_revision_id:
            return MigrationView(config=ProjectConfig(), notices=())
        revision = store.get_revision(project_id, project.current_revision_id)
        return migrate_project(revision.payload, revision.revision_id)

    def save_config(project_id, config):
        if config.read_only:
            raise ValueError(
                "Create an editable copy before running new calculations on the reference example"
            )
        project = store.get_project(project_id)
        value = config.model_dump(mode="json")
        if project.current_revision_id:
            previous = store.get_revision(project_id, project.current_revision_id)
            if previous.payload == value:
                return previous
        return store.create_revision(project_id, project.current_revision_id, value)

    def submit_studio(kind, payload, project_id, idempotency_key):
        if project_id is None:
            raise ValueError("Open a project before running")
        job, cached = store.submit(
            kind=kind,
            operation=f"{kind}@3",
            payload=payload,
            project_id=project_id,
            idempotency_key=idempotency_key,
        )
        return JSONResponse(
            status_code=200 if cached else 202,
            content=JobSubmission(
                job_id=job.job_id,
                resource_id=job.resource_id,
                status_url=f"/api/v1/jobs/{job.job_id}",
                events_url=f"/api/v1/jobs/{job.job_id}/events",
                cache_hit=cached,
            ).model_dump(mode="json"),
        )

    @app.post("/api/v1/workbench/runs", response_model=JobSubmission, status_code=202)
    def run(
        request: StudioRunRequest,
        project_id: str = Header(alias="X-Project-ID"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        revision = save_config(project_id, request.config)
        return submit_studio(
            "studio_run",
            {**request.model_dump(mode="json"), "source_revision_id": revision.revision_id},
            project_id,
            idempotency_key,
        )

    @app.get("/api/v1/workbench/runs", response_model=list[RunView])
    def runs(project_id: str):
        store.get_project(project_id)
        from mobile_sensing.application.run_pipeline import read_named_record

        records = {}
        for job in store.list_jobs(project_id=project_id):
            if job.kind == "studio_run" and job.status == "completed":
                run_id = job.result["run_id"]
                records[run_id] = RunView.model_validate_json(
                    json.dumps(
                        {key: job.result[key] for key in RunView.model_fields if key in job.result}
                    )
                )
        # Example copies link immutable runs without impersonating newly executed jobs.
        project = store.get_project(project_id)
        if project.current_revision_id:
            revision = store.get_revision(project_id, project.current_revision_id)
            for run_id in revision.payload.get("linked_run_ids", []):
                records.setdefault(run_id, read_named_record(root, run_id, "studio_run"))
        deleted = deleted_run_ids(store, project_id)
        return [value for key, value in records.items() if key not in deleted]

    @app.get("/api/v1/workbench/deleted-runs", response_model=list[RunDeletionView])
    def deleted_runs(project_id: str):
        return [
            deletion_preview(store, project_id, identifier)
            for identifier in sorted(deleted_run_ids(store, project_id))
        ]

    @app.get("/api/v1/workbench/runs/{run_id}/deletion-preview", response_model=RunDeletionView)
    def preview_run_deletion(run_id: str, project_id: str):
        return deletion_preview(store, project_id, run_id)

    @app.delete("/api/v1/workbench/runs/{run_id}", status_code=204)
    def remove_run(run_id: str, project_id: str):
        delete_run(store, project_id, run_id)

    @app.post("/api/v1/workbench/runs/{run_id}/restore", status_code=204)
    def recover_run(run_id: str, project_id: str):
        restore_run(store, project_id, run_id)

    @app.get("/api/v1/workbench/runs/{run_id}", response_model=RunView)
    def run_detail(run_id: str):
        from mobile_sensing.application.run_pipeline import read_named_record

        return read_named_record(root, run_id, "studio_run")

    @app.post("/api/v1/workbench/analyses", response_model=JobSubmission, status_code=202)
    def analysis(
        request: StudioAnalysisRequest,
        project_id: str = Header(alias="X-Project-ID"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        project = store.get_project(project_id)
        if not project.current_revision_id:
            raise ValueError("Save or run a project before portfolio analysis")
        previous = store.get_revision(project_id, project.current_revision_id)
        config = ProjectConfig.model_validate_json(json.dumps(previous.payload))
        revision = save_config(project_id, config.model_copy(update={"portfolio": request.config}))
        return submit_studio(
            "studio_analysis",
            {**request.model_dump(mode="json"), "source_revision_id": revision.revision_id},
            project_id,
            idempotency_key,
        )

    @app.get("/api/v1/workbench/analyses", response_model=list[AnalysisView])
    def analyses(project_id: str):
        store.get_project(project_id)
        from mobile_sensing.application.run_pipeline import read_named_record

        records = {
            job.result["analysis_id"]: AnalysisView.model_validate_json(
                json.dumps({key: job.result[key] for key in AnalysisView.model_fields})
            )
            for job in store.list_jobs(project_id=project_id)
            if job.kind == "studio_analysis" and job.status == "completed"
        }
        project = store.get_project(project_id)
        if project.current_revision_id:
            for analysis_id in store.get_revision(
                project_id, project.current_revision_id
            ).payload.get("linked_analysis_ids", []):
                records.setdefault(
                    analysis_id, read_named_record(root, analysis_id, "studio_analysis")
                )
        return list(records.values())

    @app.post("/api/v1/workbench/utility-curve", response_model=UtilityCurve)
    def utility_curve(request: UtilityCurveRequest):
        from mobile_sensing.portfolio.utility import pointwise_utility

        values = tuple(request.saturation_minutes * i / 10 for i in range(41))
        return UtilityCurve(
            exposure_minutes=values,
            utility=tuple(
                pointwise_utility(value * 60, request.kind, request.saturation_minutes * 60)
                for value in values
            ),
            interpretation="Pointwise utility is applied inside each joint replication/vehicle sample before empirical statistics",
        )

    @app.get("/api/v1/workbench/project/defaults", response_model=ProjectConfig)
    def project_defaults():
        return ProjectConfig(simulation=SimulationEditor(time_mode="relative"))

    @app.get("/api/v1/workbench/fleet/defaults", response_model=FleetEditor)
    def fleet_defaults():
        return FleetEditor(fleet_id="fleet-1", name="Fleet 1")

    @app.post("/api/v1/workbench/resolve", response_model=JobSubmission, status_code=202)
    def resolve(
        request: ProjectConfigRequest,
        project_id: str | None = Header(default=None, alias="X-Project-ID"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        job, cached = store.submit(
            kind="studio_resolve",
            operation="configuration-check@1",
            payload=request.model_dump(mode="json"),
            project_id=project_id,
            idempotency_key=idempotency_key,
        )
        return JSONResponse(
            status_code=200 if cached else 202,
            content=JobSubmission(
                job_id=job.job_id,
                resource_id=job.resource_id,
                status_url=f"/api/v1/jobs/{job.job_id}",
                events_url=f"/api/v1/jobs/{job.job_id}/events",
                cache_hit=cached,
            ).model_dump(mode="json"),
        )

    @app.get("/api/v1/workbench/resolutions/{resource_id}", response_model=ProjectResolutionResult)
    def resolution_result(resource_id: str):
        metadata = store.get_resource(resource_id).metadata
        return ProjectResolutionResult.model_validate_json(
            json.dumps(
                {
                    key: metadata[key]
                    for key in ProjectResolutionResult.model_fields
                    if key in metadata
                }
            )
        )

    @app.get("/api/v1/workbench/inputs/{input_id}/gtfs-routes")
    def gtfs_routes(input_id: str):
        import zipfile
        import pandas as pd

        metadata, path = load_input(root, input_id)
        if metadata["role"] != "gtfs" or path.suffix != ".zip":
            raise ValueError("Select a registered GTFS ZIP")
        with zipfile.ZipFile(path) as archive:
            matches = [
                member
                for member in archive.infolist()
                if Path(member.filename).name in {"routes.txt", "routes.csv", "buses.csv"}
            ]
            if len(matches) != 1 or matches[0].file_size > 10 * 1024**2:
                raise ValueError("Feed needs one bounded routes table")
            with archive.open(matches[0]) as stream:
                frame = pd.read_csv(stream, dtype=str, keep_default_na=False)
            if "route_id" not in frame or len(frame) > 10000:
                raise ValueError("Invalid or oversized GTFS routes table")
            return [
                {
                    "route_id": row["route_id"],
                    "name": row.get("route_short_name", "")
                    or row.get("route_long_name", "")
                    or row["route_id"],
                }
                for row in frame.to_dict("records")
            ]

    @app.get("/api/v1/workbench/environment/defaults", response_model=EnvironmentEditor)
    def environment_defaults():
        return EnvironmentEditor()

    @app.post("/api/v1/workbench/region-search", response_model=JobSubmission, status_code=202)
    def region_search(request: RegionSearchRequest):
        job, cached = store.submit(
            kind="region_search",
            operation="region-search@1",
            payload=request.model_dump(mode="json"),
        )
        return JobSubmission(
            job_id=job.job_id,
            resource_id=job.resource_id,
            status_url=f"/api/v1/jobs/{job.job_id}",
            events_url=f"/api/v1/jobs/{job.job_id}/events",
            cache_hit=cached,
        )

    @app.get("/api/v1/workbench/region-search/{resource_id}", response_model=RegionSearchResult)
    def region_search_result(resource_id: str):
        record = store.get_resource(resource_id)
        return RegionSearchResult.model_validate(
            {key: record.metadata[key] for key in RegionSearchResult.model_fields}
        )

    @app.post("/api/v1/workbench/environments", response_model=JobSubmission, status_code=202)
    def prepare_environment(
        request: EnvironmentEditorRequest,
        project_id: str | None = Header(default=None, alias="X-Project-ID"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        job, cached = store.submit(
            kind="studio_environment",
            operation="studio_environment@1",
            payload=request.model_dump(mode="json"),
            project_id=project_id,
            idempotency_key=idempotency_key,
        )
        response = JobSubmission(
            job_id=job.job_id,
            resource_id=job.resource_id,
            status_url=f"/api/v1/jobs/{job.job_id}",
            events_url=f"/api/v1/jobs/{job.job_id}/events",
            cache_hit=cached,
        )
        return JSONResponse(
            status_code=200 if cached else 202, content=response.model_dump(mode="json")
        )

    @app.post("/api/v1/workbench/feature-source/map")
    def source_spatial_feature_map(selection: FeatureSelection):
        from mobile_sensing.api.workbench_maps import source_feature_preview

        return source_feature_preview(root, selection, store.limits)

    @app.get("/api/v1/workbench/features/{artifact_id}/map")
    def spatial_feature_map(artifact_id: str, feature: str):
        from mobile_sensing.api.workbench_maps import feature_preview

        return feature_preview(root, artifact_id, feature, store.limits)

    @app.get("/api/v1/workbench/environments/{artifact_id}/map")
    def environment_map(artifact_id: str):
        from mobile_sensing.api.workbench_maps import environment_preview

        return environment_preview(
            str(root), artifact_id, store.limits.max_map_features, store.limits.max_map_bytes
        )

    @app.get(
        "/api/v1/workbench/environments/{resource_id}/result", response_model=EnvironmentResult
    )
    def environment_result(resource_id: str):
        record = store.get_resource(resource_id)
        return EnvironmentResult.model_validate_json(
            json.dumps(
                {
                    key: record.metadata[key]
                    for key in EnvironmentResult.model_fields
                    if key in record.metadata
                }
            )
        )

    def register(request):
        value = snapshot_input(root, request, limit=store.limits.max_upload_bytes)
        store.register_resource(
            resource_id=value["input_id"],
            kind="input",
            content_hash=value["content_hash"],
            artifact_kind=None,
            job_id=None,
            metadata=value,
        )
        return value

    @app.delete("/api/v1/projects/{project_id}", status_code=204)
    def delete_project(project_id: str):
        store.delete_project(project_id)

    @app.get("/api/v1/inputs", response_model=list[InputDescriptor])
    def inputs(role: InputRole | None = None):
        return [
            item.metadata
            for item in store.list_resources(kind="input")
            if role is None or item.metadata["role"] == role
        ]

    @app.post("/api/v1/inputs/register", status_code=201, response_model=InputDescriptor)
    def register_path(request: InputRegistration):
        return register(request)

    @app.post(
        "/api/v1/inputs/{input_id}/confirm-crs", status_code=201, response_model=InputDescriptor
    )
    def confirm_crs(input_id: str, request: InputCrsConfirmation):
        metadata, path = load_input(root, input_id)
        return register(
            InputRegistration(
                path=str(path),
                name=metadata["name"],
                role=metadata["role"],
                layer=metadata["layer"],
                source_crs=request.source_crs,
            )
        )

    @app.post("/api/v1/inputs/upload", status_code=201, response_model=InputDescriptor)
    async def upload_input(
        file: Annotated[UploadFile, File()],
        role: Annotated[InputRole, Form()],
        name: Annotated[str, Form()],
        source_crs: Annotated[str, Form()] = "",
        layer: Annotated[str, Form()] = "",
    ):
        staging = root / "staging_uploads"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as directory:
            target = Path(directory) / Path(file.filename or "source").name
            count = 0
            with target.open("wb") as stream:
                while chunk := await file.read(1024**2):
                    count += len(chunk)
                    if count > store.limits.max_upload_bytes:
                        raise ValueError("Input exceeds configured upload limit")
                    stream.write(chunk)
            request = InputRegistration(
                path=str(target),
                name=name,
                role=role,
                source_crs=source_crs or None,
                layer=layer or None,
            )
            return await run_in_threadpool(register, request)
