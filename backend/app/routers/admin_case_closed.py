# app/routers/admin_case_closed.py

from typing import List, Optional

from fastapi import APIRouter, Depends, status

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    CaseClosedBlueprintView,
    CaseClosedGenerationConfig,
    CaseClosedPackageView,
)
from ..postgres import Database
from ..services import case_closed_generation
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin/case-closed", tags=["Admin - Case Closed"])


@router.post("/blueprints", response_model=CaseClosedBlueprintView, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    payload: CaseClosedGenerationConfig,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedBlueprintView:
    from ..services.case_closed_schemas import CaseClosedGenerationConfig as SchemaConfig

    config = SchemaConfig.model_validate(payload.model_dump(exclude_none=True))
    created = case_closed_generation.create_blueprint_draft(db, config, created_by=account.regno)
    return CaseClosedBlueprintView(**created)


@router.get("/blueprints", response_model=List[CaseClosedBlueprintView])
def list_blueprints(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[CaseClosedBlueprintView]:
    return [CaseClosedBlueprintView(**e) for e in case_closed_generation.list_blueprints(db)]


@router.get("/blueprints/{blueprint_id}", response_model=CaseClosedBlueprintView)
def get_blueprint(
    blueprint_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedBlueprintView:
    return CaseClosedBlueprintView(**case_closed_generation.get_blueprint(db, blueprint_id))


@router.post("/blueprints/{blueprint_id}/generate", response_model=CaseClosedBlueprintView)
def generate_blueprint(
    blueprint_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedBlueprintView:
    return CaseClosedBlueprintView(**case_closed_generation.generate_blueprint(db, blueprint_id))


@router.post("/blueprints/{blueprint_id}/approve", response_model=CaseClosedBlueprintView)
def approve_blueprint(
    blueprint_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedBlueprintView:
    return CaseClosedBlueprintView(**case_closed_generation.approve_blueprint(db, blueprint_id))


@router.post("/blueprints/{blueprint_id}/packages", response_model=CaseClosedPackageView, status_code=status.HTTP_201_CREATED)
def generate_package(
    blueprint_id: str,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedPackageView:
    created = case_closed_generation.generate_package(db, blueprint_id, created_by=account.regno)
    return CaseClosedPackageView(**created)


@router.get("/packages", response_model=List[CaseClosedPackageView])
def list_packages(
    blueprint_id: Optional[str] = None,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[CaseClosedPackageView]:
    return [CaseClosedPackageView(**e) for e in case_closed_generation.list_packages(db, blueprint_id)]


@router.get("/packages/{package_id}", response_model=CaseClosedPackageView)
def get_package(
    package_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedPackageView:
    return CaseClosedPackageView(**case_closed_generation.get_package(db, package_id))


@router.post("/packages/{package_id}/publish", response_model=CaseClosedPackageView)
def publish_package(
    package_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CaseClosedPackageView:
    published = case_closed_generation.publish_package(db, package_id)
    return CaseClosedPackageView(**published)


@router.get("/session")
def current_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    from fastapi import HTTPException
    from sqlalchemy import select

    from ..db_models import CaseClosedGameRecord

    with db.sessions() as session:
        current = session.scalar(
            select(CaseClosedGameRecord)
            .where(CaseClosedGameRecord.status.in_(("active", "pending")))
            .limit(1)
        )
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Case Closed session yet")
        return {
            "id": str(current.id),
            "period_start_date": current.period_start_date.isoformat() if current.period_start_date else None,
            "period_end_date": current.period_end_date.isoformat() if current.period_end_date else None,
            "case_package_id": str(current.case_package_id) if current.case_package_id else None,
            "status": current.status,
            "finalized": bool(current.finalized),
        }
