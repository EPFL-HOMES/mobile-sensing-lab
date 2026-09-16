"""Serve the immutable production browser bundle from the local API process."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class FrontendBundleError(RuntimeError):
    """Raised when local browser assets are absent or structurally incomplete."""


def bundled_frontend_root() -> Path:
    """Return the frontend directory installed inside the Python distribution."""

    return Path(__file__).resolve().parents[1] / "_web"


def validate_frontend_bundle(frontend_root: str | Path | None = None) -> Path:
    """Resolve and validate the minimum immutable production bundle."""

    root = Path(frontend_root).resolve() if frontend_root is not None else bundled_frontend_root()
    if not (root / "index.html").is_file() or not (root / "assets").is_dir():
        raise FrontendBundleError(
            f"frontend bundle is missing at {root}; run `npm ci && npm run build` in frontend/"
        )
    return root


def install_frontend_routes(
    app: FastAPI,
    frontend_root: str | Path | None = None,
) -> Path:
    """Install static assets and an SPA fallback after every API route."""

    root = validate_frontend_bundle(frontend_root)
    app.mount(
        "/assets",
        StaticFiles(directory=root / "assets", check_dir=True),
        name="frontend-assets",
    )

    @app.get("/", include_in_schema=False)
    def frontend_index():
        return FileResponse(root / "index.html", media_type="text/html")

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def frontend_route(frontend_path: str):
        # An unknown API route is an API error, never the browser index.
        if frontend_path == "api" or frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (root / frontend_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not Found") from exc
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(root / "index.html", media_type="text/html")

    return root
