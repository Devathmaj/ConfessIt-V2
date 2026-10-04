# app/routers/admin_plot_twist.py

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    PlotTwistBlueprintView,
    PlotTwistGenerationConfig,
    PlotTwistPackageView,
    PlotTwistSimulateRequest,
    PlotTwistSimulateResponse,
)
from ..postgres import Database
from ..services import plot_twist_generation
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin/plot-twist", tags=["Admin - Plot Twist"])

# Every route below is gated on the one capability that means "may publish a
# game". Generation runs synchronously in the request: each model call has
# its own timeout, failures mark the row generation_failed without touching
# published versions, and retries create no conflicting publications because
# publishing is an explicit, validated, append-only step.


@router.post("/blueprints", response_model=PlotTwistBlueprintView, status_code=status.HTTP_201_CREATED)
def create_blueprint(
    payload: PlotTwistGenerationConfig,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistBlueprintView:
    """Store the story-generation parameters as a draft blueprint row."""
    from ..services.plot_twist_schemas import GenerationConfig

    config = GenerationConfig.model_validate(payload.model_dump(exclude_none=True))
    created = plot_twist_generation.create_blueprint_draft(db, config, created_by=account.regno)
    return PlotTwistBlueprintView(**created)


@router.get("/blueprints", response_model=List[PlotTwistBlueprintView])
def list_blueprints(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[PlotTwistBlueprintView]:
    """Every blueprint draft, newest first."""
    return [PlotTwistBlueprintView(**entry) for entry in plot_twist_generation.list_blueprints(db)]


@router.get("/blueprints/{blueprint_id}", response_model=PlotTwistBlueprintView)
def get_blueprint(
    blueprint_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistBlueprintView:
    """One blueprint with its generation status and validation diagnostics."""
    return PlotTwistBlueprintView(**plot_twist_generation.get_blueprint(db, blueprint_id))


@router.post("/blueprints/{blueprint_id}/generate", response_model=PlotTwistBlueprintView)
def generate_blueprint(
    blueprint_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistBlueprintView:
    """Run Stage A: generate (and repair, bounded) the blueprint content."""
    return PlotTwistBlueprintView(**plot_twist_generation.generate_blueprint(db, blueprint_id))


@router.post("/blueprints/{blueprint_id}/approve", response_model=PlotTwistBlueprintView)
def approve_blueprint(
    blueprint_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistBlueprintView:
    """Approve a validated blueprint so Stage B may run against it."""
    return PlotTwistBlueprintView(**plot_twist_generation.approve_blueprint(db, blueprint_id))


@router.post("/blueprints/{blueprint_id}/packages", response_model=PlotTwistPackageView, status_code=status.HTTP_201_CREATED)
def generate_package(
    blueprint_id: str,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistPackageView:
    """Run Stage B: generate the full playable package as a new version."""
    created = plot_twist_generation.generate_package(db, blueprint_id, created_by=account.regno)
    return PlotTwistPackageView(**created)


@router.get("/packages", response_model=List[PlotTwistPackageView])
def list_packages(
    blueprint_id: Optional[str] = None,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[PlotTwistPackageView]:
    """Every story package version, newest first, optionally per blueprint."""
    return [PlotTwistPackageView(**entry) for entry in plot_twist_generation.list_packages(db, blueprint_id)]


@router.get("/packages/{package_id}", response_model=PlotTwistPackageView)
def get_package(
    package_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistPackageView:
    """One package with its scenes, endings, rewards and validation status."""
    return PlotTwistPackageView(**plot_twist_generation.get_package(db, package_id))


@router.post("/packages/{package_id}/scenes/{scene_id}/regenerate", response_model=PlotTwistPackageView)
def regenerate_scene(
    package_id: str,
    scene_id: str,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistPackageView:
    """Regenerate one scene in place; a failed repair keeps the prior draft."""
    updated = plot_twist_generation.regenerate_scene(
        db, package_id, scene_id, created_by=account.regno
    )
    return PlotTwistPackageView(**updated)


class _PackageUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package: dict[str, Any]


@router.put("/packages/{package_id}", response_model=PlotTwistPackageView)
def update_package(
    package_id: str,
    payload: _PackageUpdate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistPackageView:
    """Manually edit supported story fields; re-validates, published is immutable."""
    updated = plot_twist_generation.update_package_content(db, package_id, payload.package)
    return PlotTwistPackageView(**updated)


@router.post("/packages/{package_id}/simulate", response_model=PlotTwistSimulateResponse)
def simulate_package(
    package_id: str,
    payload: PlotTwistSimulateRequest,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistSimulateResponse:
    """Walk choice ids from the opening and inspect the outcome, without playing."""
    stored = plot_twist_generation.get_package(db, package_id)
    result = plot_twist_generation.simulate_path(stored["package"], list(payload.choice_ids or []))
    return PlotTwistSimulateResponse(**result)


@router.post("/packages/{package_id}/publish", response_model=PlotTwistPackageView)
def publish_package(
    package_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> PlotTwistPackageView:
    """Publish a validated package version; sessions pin this version."""
    published = plot_twist_generation.publish_package(db, package_id)
    return PlotTwistPackageView(**published)


@router.get("/session")
def current_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    """The current Plot Twist global period, if the reset loop opened one.

    Declared before ``/{package_id}``-style routes so ``session`` is never
    read as an id.
    """
    from fastapi import HTTPException

    from ..db_models import PlotTwistGameRecord
    from sqlalchemy import select

    with db.sessions() as session:
        current = session.scalar(
            select(PlotTwistGameRecord)
            .where(PlotTwistGameRecord.status.in_(("active", "pending")))
            .limit(1)
        )
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Plot Twist session yet",
            )
        return {
            "id": str(current.id),
            "period_start_date": current.period_start_date.isoformat() if current.period_start_date else None,
            "period_end_date": current.period_end_date.isoformat() if current.period_end_date else None,
            "story_package_id": str(current.story_package_id) if current.story_package_id else None,
            "status": current.status,
        }
