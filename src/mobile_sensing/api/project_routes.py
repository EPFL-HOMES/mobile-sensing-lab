"""Project filesystem access and durable portable package operations."""

import json
import os
import sys
import uuid
from pathlib import Path
from fastapi import UploadFile, File, Form
from fastapi.responses import FileResponse

from mobile_sensing.jobs.project_layout import project_directory, ownership_boundary
from mobile_sensing.jobs.models import ApiModel, JobSubmission
from mobile_sensing.application.project_package import project_snapshot, read_package_header


class ProjectFileEntry(ApiModel):
    file_id: str
    name: str
    category: str
    path: str
    size_bytes: int
    shared: bool
    source_id: str | None


class ProjectFilesView(ApiModel):
    project_id: str
    name: str
    directory: str
    files: list[ProjectFileEntry]
    portable: bool


class ProjectPackageRequest(ApiModel):
    archive: bool = True


class OpenDirectoryView(ApiModel):
    directory: str


def install_project_routes(app, root, store):
    def files(project_id):
        project = store.get_project(project_id)
        directory = project_directory(root, project.name)
        value = json.loads((directory / "files.json").read_text())
        value["directory"] = str(directory)
        return value

    @app.get("/api/v1/projects/{project_id}/files", response_model=ProjectFilesView)
    def project_files(project_id: str):
        return files(project_id)

    @app.get("/api/v1/projects/{project_id}/files/{file_id}")
    def project_file(project_id: str, file_id: str):
        value = files(project_id)
        entry = next((f for f in value["files"] if f["file_id"] == file_id), None)
        if entry is None:
            raise KeyError(file_id)
        path = (Path(value["directory"]) / entry["path"]).resolve()
        if not path.is_relative_to(ownership_boundary(root)) or not path.is_file():
            raise ValueError("File is outside the managed workspace")
        return FileResponse(path, filename=path.name)

    @app.post("/api/v1/projects/{project_id}/open-directory", response_model=OpenDirectoryView)
    def open_directory(project_id: str):
        import subprocess

        directory = files(project_id)["directory"]
        if sys.platform == "darwin":
            subprocess.Popen(["open", directory])
        elif sys.platform == "win32":
            os.startfile(directory)
        else:
            subprocess.Popen(["xdg-open", directory])
        return {"directory": directory}

    @app.post("/api/v1/projects/{project_id}/package", response_model=JobSubmission)
    def package(project_id: str, request: ProjectPackageRequest):
        snapshot = project_snapshot(store, project_id)
        kind = "project_export" if request.archive else "project_files"
        job, cached = store.submit(
            kind=kind, operation=kind + "@1", payload={"snapshot": snapshot}, project_id=project_id
        )
        return JobSubmission(
            job_id=job.job_id,
            resource_id=job.resource_id,
            status_url=f"/api/v1/jobs/{job.job_id}",
            events_url=f"/api/v1/jobs/{job.job_id}/events",
            cache_hit=cached,
        )

    @app.post("/api/v1/project-imports", response_model=JobSubmission, status_code=202)
    async def import_package(
        file: UploadFile = File(), name: str = Form(min_length=1, max_length=200)
    ):
        from mobile_sensing.jobs.project_names import normalize_name

        name = normalize_name(name)
        with store._connect() as connection:
            store._check_project_name(connection, name)
        directory = root / ".project_uploads"
        directory.mkdir(exist_ok=True)
        target = directory / (uuid.uuid4().hex + ".zip")
        try:
            size = 0
            with target.open("wb") as output:
                while chunk := await file.read(1024**2):
                    size += len(chunk)
                    if size > 8 * 1024**3:
                        raise ValueError("Portable project upload exceeds 8 GiB")
                    output.write(chunk)
            header = read_package_header(target)
            project = store.create_project(name, header.get("project", {}).get("description", ""))
            job, cached = store.submit(
                kind="project_import",
                operation="project-import@1",
                payload={
                    "upload_path": str(target.relative_to(root)),
                    "project_id": project.project_id,
                    "project_name": project.name,
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
            raise
