"""Local workspace hub: existing scientific APIs run against independent project stores."""

import asyncio
import json
import re
from urllib.parse import parse_qs

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from mobile_sensing.api.app import create_app
from mobile_sensing.application.example_bundle import bundle_manifest, ExampleOpenRequest
from mobile_sensing.jobs import ProjectCreate, ProjectRecord, JobStore, RevisionConflict
from mobile_sensing.jobs.project_layout import prepare_project_directory
from mobile_sensing.jobs.workspace import ProjectWorkspace


async def invoke(app, path, *, body=None, method="POST"):
    """Invoke a local ASGI endpoint without network requests or another client dependency."""
    chunks, status, headers = [], 200, []
    consumed = False
    payload = json.dumps(body or {}).encode()

    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status, headers = message["status"], message.get("headers", [])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    }
    await app(scope, receive, send)
    value = json.loads(b"".join(chunks)) if chunks and b"".join(chunks) else None
    return JSONResponse(
        value,
        status_code=status,
        headers={k.decode(): v.decode() for k, v in headers if k.lower() in {b"x-request-id"}},
    )


def create_workspace_app(root, *, serve_frontend=False):
    workspace = ProjectWorkspace(root)
    control = workspace.root.parent / ("." + workspace.root.name + "-workspace")
    control.mkdir(exist_ok=True)
    fallback = create_app(control, serve_frontend=serve_frontend)
    apps = {}
    app = FastAPI(title="Mobile Sensing Simulator", version="1.0.0")
    app.state.workspace = workspace
    # The hub preserves the scientific API contract while choosing a private store.
    # Expose all delegated routes in the installed API documentation as well.
    app.openapi = fallback.openapi

    @app.get("/api/v1/workspace-info")
    def workspace_identity():
        return {"application": "mobile-sensing", "workspace": str(workspace.root.resolve())}

    def child(private):
        key = str(private)
        if key not in apps:
            apps[key] = create_app(private, serve_frontend=False)
        return apps[key]

    def by_project(identifier):
        return child(workspace.store(identifier).artifact_root)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse(
            {
                "code": "NOT_FOUND",
                "message": f"Project or resource not found: {error}",
                "issues": [],
                "request_id": "workspace",
            },
            status_code=404,
        )

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse(
            {
                "code": (
                    "PROJECT_CONFLICT" if isinstance(error, RevisionConflict) else "INVALID_REQUEST"
                ),
                "message": str(error),
                "issues": [],
                "request_id": "workspace",
            },
            status_code=409 if isinstance(error, RevisionConflict) else 422,
        )

    @app.get("/api/v1/projects", response_model=list[ProjectRecord])
    def projects():
        return workspace.records()

    @app.post("/api/v1/projects", response_model=ProjectRecord, status_code=201)
    def create(request: ProjectCreate):
        return workspace.create(request.name, request.description)

    @app.patch("/api/v1/projects/{project_id}", response_model=ProjectRecord)
    def rename(project_id: str, request: ProjectCreate):
        record = workspace.rename(project_id, request.name, request.description)
        apps.clear()
        return record

    @app.delete("/api/v1/projects/{project_id}", status_code=204)
    def delete(project_id: str):
        private = workspace.store(project_id).artifact_root
        workspace.delete(project_id)
        import os
        import uuid

        target = control / "trash" / (private.parent.name + "-" + uuid.uuid4().hex[:8])
        target.parent.mkdir(exist_ok=True)
        os.replace(private.parent, target)
        apps.clear()

    from mobile_sensing.application.project_models import ProjectConfigRequest

    @app.post(
        "/api/v1/workbench/projects/{project_id}/copy",
        response_model=ProjectRecord,
        status_code=201,
    )
    def copy(project_id: str, request: ProjectConfigRequest):
        return workspace.copy(project_id, request.config.model_dump(mode="json"))

    def example_private(example_key="lausanne"):
        bundle = bundle_manifest(example_key=example_key)
        city = "Lausanne" if example_key == "lausanne" else "San Francisco"
        for record in workspace.records():
            if (
                record.name.startswith("[Example]")
                or city not in record.name
                or not record.current_revision_id
            ):
                continue
            store = workspace.store(record.project_id)
            value = store.get_revision(record.project_id, record.current_revision_id).payload
            if value.get("read_only") and value.get("example_bundle_id"):
                # Rename outside the reservation lock: workspace.rename owns its transaction.
                try:
                    workspace.rename(
                        record.project_id, workspace.available_name(bundle.name), bundle.description
                    )
                except RevisionConflict:
                    # An import may still be active; retry the name upgrade on the next visit.
                    continue
                apps.clear()
        with workspace.mutation():
            private = reserve_example_private(example_key)
            (private / "example-selection.json").write_text(
                json.dumps({"example_key": example_key})
            )
            return private

    def reserve_example_private(example_key):
        bundle = bundle_manifest(example_key=example_key)
        for record in workspace.records():
            store = workspace.store(record.project_id)
            if record.current_revision_id:
                value = store.get_revision(record.project_id, record.current_revision_id).payload
                city = "Lausanne" if example_key == "lausanne" else "San Francisco"
                if (
                    value.get("example_bundle_id")
                    and value.get("read_only")
                    and city in record.name
                ):
                    return store.artifact_root
            if record.name == bundle.name:
                return store.artifact_root
        pending = workspace.root / bundle.name / ".system/ownership.json"
        if pending.is_file() and not (pending.parent.parent / "project.json").exists():
            return pending.parent
        name = workspace.available_name(bundle.name)
        return prepare_project_directory(workspace.root / name)

    @app.get("/api/v1/examples/{example_key}")
    def example_info(example_key: str):
        from mobile_sensing.application.example_bundle import example_info as info

        available = info(control, example_key=example_key)
        if not available.available:
            return available
        return info(example_private(example_key), example_key=example_key)

    @app.post("/api/v1/examples/{example_key}/open")
    async def open_example(example_key: str, request: ExampleOpenRequest):
        if not example_info(example_key).available:
            return JSONResponse(
                {
                    "code": "EXAMPLE_UNAVAILABLE",
                    "message": "The offline example bundle is unavailable",
                    "issues": [],
                    "request_id": "workspace",
                },
                status_code=404,
            )
        private = await run_in_threadpool(example_private, example_key)
        response = await invoke(
            child(private), "/api/v1/examples/lausanne/open", body={"editable": False}
        )
        if request.editable and response.status_code < 300:
            value = json.loads(response.body)
            if value.get("job_id"):
                return JSONResponse(
                    {
                        "code": "EXAMPLE_INSTALLING",
                        "message": "Wait for the example installation before creating an editable copy",
                        "issues": [],
                        "request_id": "workspace",
                    },
                    status_code=409,
                )
            record = await run_in_threadpool(workspace.copy, value["project_id"])
            return {"project_id": record.project_id, "job_id": None}
        return response

    @app.post("/api/v1/workbench/workspace/initialize")
    async def initialize():
        project_ids, job_ids = [], []
        for example_key in ("lausanne", "san-francisco"):
            if not example_info(example_key).available:
                continue
            private = await run_in_threadpool(example_private, example_key)
            response = await invoke(child(private), "/api/v1/workbench/workspace/initialize")
            if response.status_code >= 300:
                return response
            value = json.loads(response.body)
            if value.get("example_project_id"):
                project_ids.append(value["example_project_id"])
            if value.get("example_job_id"):
                job_ids.append(value["example_job_id"])
        return {
            "directory": str(workspace.root),
            "example_project_id": project_ids[0] if project_ids else None,
            "example_job_id": job_ids[0] if job_ids else None,
            "example_project_ids": project_ids,
            "example_job_ids": job_ids,
        }

    @app.get("/api/v1/workbench/workspace")
    def workspace_info():
        return {
            "directory": str(workspace.root),
            "example_project_id": None,
            "example_job_id": None,
        }

    @app.post("/api/v1/project-imports", status_code=202)
    async def import_package(
        file: UploadFile = File(), name: str = Form(min_length=1, max_length=200)
    ):
        import uuid
        from mobile_sensing.application.project_package import read_package_header
        from mobile_sensing.jobs.models import JobSubmission

        private = await run_in_threadpool(workspace.reserve_directory, name)
        name = private.parent.name
        directory = private / ".project_uploads"
        directory.mkdir(exist_ok=True)
        target = directory / (uuid.uuid4().hex + ".zip")
        size = 0
        try:
            with target.open("wb") as output:
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > 8 * 1024**3:
                        raise ValueError("Portable project upload exceeds 8 GiB")
                    output.write(chunk)
            header = await run_in_threadpool(read_package_header, target)
            store = JobStore(private)
            project = store.create_project(name, header.get("project", {}).get("description", ""))
            job, cached = store.submit(
                kind="project_import",
                operation="project-import@1",
                payload={
                    "upload_path": str(target.relative_to(private)),
                    "project_id": project.project_id,
                    "project_name": name,
                },
                project_id=project.project_id,
            )
            return JobSubmission(
                job_id=job.job_id,
                resource_id=job.resource_id,
                status_url=f"/api/v1/jobs/{job.job_id}",
                events_url=f"/api/v1/jobs/{job.job_id}/events",
                cache_hit=cached,
            )
        except BaseException:
            target.unlink(missing_ok=True)
            if not (private.parent / "project.json").exists():
                import shutil

                # The reservation is exclusively ours and has no published project.
                shutil.rmtree(private.parent)
            raise

    class Dispatch:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not scope["path"].startswith("/api/"):
                return await fallback(scope, receive, send)
            path = scope["path"]
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            params = parse_qs(scope.get("query_string", b"").decode())
            match = re.search(r"/projects/(project_[^/]+)", path)
            identifier = (
                match.group(1)
                if match
                else params.get("project_id", [None])[0] or headers.get("x-project-id")
            )
            selected = None
            try:
                job_match = re.search(r"(job_[A-Za-z0-9_]+)", path)
                if identifier and job_match:
                    try:
                        workspace.store(identifier).get_job(job_match.group(1))
                    except KeyError:
                        identifier = None
                if identifier:
                    selected = by_project(identifier)
                else:
                    workspace.refresh()
                    tokens = re.findall(
                        r"(?:job|resource|dataset|input|environment|simulation|exposure|portfolio|export)_[A-Za-z0-9_]+",
                        path,
                    )
                    for token in tokens:
                        matches = []
                        for owner, (_, private) in workspace._projects.items():
                            store = JobStore(private)
                            with store._connect() as connection:
                                known = connection.execute(
                                    "SELECT 1 FROM jobs WHERE job_id=? UNION ALL SELECT 1 FROM resources WHERE resource_id=?",
                                    (token, token),
                                ).fetchone()
                            if known or any(
                                (private / c / token).exists()
                                for c in (
                                    "datasets",
                                    "inputs",
                                    "environments",
                                    "simulations",
                                    "exposures",
                                    "portfolios",
                                )
                            ):
                                matches.append(private)
                        if len(matches) == 1:
                            selected = child(matches[0])
                            break
                    if selected is None and len(workspace._projects) == 1:
                        selected = child(next(iter(workspace._projects.values()))[1])
                if (
                    selected is None
                    and path not in {"/api/v1/health", "/api/v1/capabilities"}
                    and not path.startswith(
                        ("/api/v1/schemas/", "/api/v1/workbench/project/defaults")
                    )
                ):
                    raise ValueError("Select a project before accessing its data or results")
            except (KeyError, ValueError) as error:
                response = JSONResponse(
                    {
                        "code": "PROJECT_REQUIRED",
                        "message": str(error),
                        "issues": [],
                        "request_id": "workspace",
                    },
                    status_code=404 if isinstance(error, KeyError) else 422,
                )
                return await response(scope, receive, send)
            return await (selected or fallback)(scope, receive, send)

    app.mount("/", Dispatch())
    return app
