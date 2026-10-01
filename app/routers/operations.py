"""Operation catalog endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import role_allows
from app.deps import get_current_user
from app.schemas import OperationSpec, Principal
from app.services.catalog import get_operation_catalog

router = APIRouter(prefix="/api/v1", tags=["operations"])


@router.get("/operations", response_model=list[OperationSpec])
def list_operations(
    principal: Principal = Depends(get_current_user),
) -> list[OperationSpec]:
    """Return operations the caller is allowed to see (by role)."""
    catalog = get_operation_catalog()
    return [op for op in catalog if role_allows(principal.role, op.required_role)]
