"""Operator web UI — private console (not Skyline)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.services.static_assets import stamp_html

router = APIRouter(tags=["ui"])

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "ui.html"
_DOCS = Path(__file__).resolve().parents[1] / "templates" / "docs.html"


@router.get("/ui")
def ui_page():
    """Serve the operator interface with cache-busted asset URLs."""
    if not _TEMPLATE.is_file():
        return {"error": "UI template missing", "path": str(_TEMPLATE)}
    from fastapi.responses import HTMLResponse

    html = stamp_html(_TEMPLATE.read_text(encoding="utf-8"))
    # The shell is revalidated. Its script URLs name the build, and those
    # files stay in the browser until the next build.
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/ui/")
def ui_slash():
    return RedirectResponse(url="/ui", status_code=307)


@router.get("/docs", include_in_schema=False)
def docs_catalog():
    """Human API catalog (same content as genestack.dev/docs). Swagger is /swagger."""
    if not _DOCS.is_file():
        return {"error": "docs template missing", "path": str(_DOCS)}
    from fastapi.responses import HTMLResponse

    html = stamp_html(_DOCS.read_text(encoding="utf-8"))
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/docs/", include_in_schema=False)
def docs_slash():
    return RedirectResponse(url="/docs", status_code=307)
