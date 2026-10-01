"""Operator web UI — private console (not Skyline)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["ui"])

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "ui.html"
_DOCS = Path(__file__).resolve().parents[1] / "templates" / "docs.html"


@router.get("/ui")
def ui_page():
    """Serve the operator interface with cache-busted asset URLs."""
    if not _TEMPLATE.is_file():
        return {"error": "UI template missing", "path": str(_TEMPLATE)}
    from fastapi.responses import HTMLResponse

    from app.version import BUILD, VERSION

    html = _TEMPLATE.read_text(encoding="utf-8")
    stamp = f"?v={VERSION}-{BUILD}"
    html = html.replace("/static/css/portal.css", f"/static/css/portal.css{stamp}")
    html = html.replace("/static/css/theme.css", f"/static/css/theme.css{stamp}")
    html = html.replace("/static/js/app.js", f"/static/js/app.js{stamp}")
    # Browsers must always revalidate the shell so new builds ship instantly.
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

    from app.version import BUILD, VERSION

    html = _DOCS.read_text(encoding="utf-8")
    stamp = f"?v={VERSION}-{BUILD}"
    html = html.replace("/static/css/portal.css", f"/static/css/portal.css{stamp}")
    html = html.replace("/static/css/theme.css", f"/static/css/theme.css{stamp}")
    html = html.replace("/static/css/docs.css", f"/static/css/docs.css{stamp}")
    html = html.replace(
        "/static/js/docs-catalog.js", f"/static/js/docs-catalog.js{stamp}"
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@router.get("/docs/", include_in_schema=False)
def docs_slash():
    return RedirectResponse(url="/docs", status_code=307)
