"""Operation catalog tests."""

from __future__ import annotations


def _extract_ops(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("operations", "items", "catalog", "data"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
    return []


def _op_names(ops) -> set[str]:
    names: set[str] = set()
    for op in ops:
        if isinstance(op, str):
            names.add(op)
        elif isinstance(op, dict):
            name = (
                op.get("id") or op.get("name") or op.get("operation") or op.get("key")
            )
            if name:
                names.add(str(name))
    return names


def test_list_operations_non_empty(client, admin_headers):
    """GET /api/v1/operations returns a non-empty catalog."""
    resp = client.get("/api/v1/operations", headers=admin_headers)
    assert resp.status_code == 200
    ops = _extract_ops(resp.json())
    assert len(ops) > 0, "operation catalog should not be empty"


def test_catalog_includes_expected_operations(client, admin_headers):
    """Catalog includes genestack.service.enable and no leftover installer ids."""
    resp = client.get("/api/v1/operations", headers=admin_headers)
    assert resp.status_code == 200
    names = _op_names(_extract_ops(resp.json()))
    assert (
        "genestack.service.enable" in names
    ), f"missing genestack.service.enable in {sorted(names)}"
    assert not any(name.startswith("maas.") for name in names)
