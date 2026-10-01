"""Human API catalog at /docs; live OpenAPI at /swagger."""

from __future__ import annotations


def test_docs_catalog_html(client):
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text
    assert 'id="docs-root"' in body
    assert 'id="docs-search"' in body
    assert "/swagger" in body


def test_docs_slash_redirects(client):
    resp = client.get("/docs/", follow_redirects=False)
    assert resp.status_code in {301, 307, 308}
    assert resp.headers.get("location", "").endswith("/docs")


def test_swagger_ui(client):
    resp = client.get("/swagger")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_openapi_json(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "paths" in data
    assert "/health" in data["paths"] or any(
        p.endswith("/health") for p in data["paths"]
    )


def test_api_info_points_at_both(client):
    resp = client.get("/api")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("docs") == "/docs"
    assert data.get("swagger") == "/swagger"
    assert data.get("openapi") == "/openapi.json"
