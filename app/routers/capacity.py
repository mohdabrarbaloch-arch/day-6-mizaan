"""Capacity: per-region quota, utilisation and fleet cost."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_role
from ..engine import capacity
from ..models import Deployment, ModelVersion, Region, User
from ..schemas import CapacityOut, RegionOut

router = APIRouter(prefix="/api/capacity", tags=["capacity"])


@router.get("/regions", response_model=list[RegionOut])
def list_regions(
    _user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> list[RegionOut]:
    regions = db.scalars(select(Region).order_by(Region.name.asc())).all()
    return [RegionOut.model_validate(r) for r in regions]


@router.get("", response_model=CapacityOut)
def get_capacity(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> CapacityOut:
    regions = db.scalars(select(Region).order_by(Region.name.asc())).all()
    summary = capacity.summarise_regions(
        [
            {
                "name": r.name,
                "gpu_quota": r.gpu_quota,
                "gpu_used": r.gpu_used,
                "usd_per_gpu_hour": r.usd_per_gpu_hour,
            }
            for r in regions
        ]
    )

    rows = db.execute(
        select(Deployment, ModelVersion)
        .join(ModelVersion, Deployment.model_version_id == ModelVersion.id)
        .where(ModelVersion.owner_id == user.id, Deployment.status == "active")
    ).all()

    fleet = capacity.fleet_cost(
        [
            {
                "id": dep.id,
                "region": dep.region,
                "replicas": dep.replicas,
                "gpus_per_replica": dep.gpus_per_replica,
            }
            for dep, _model in rows
        ],
        {r.name: r.usd_per_gpu_hour for r in regions},
    )

    return CapacityOut(summary=summary, fleet=fleet)
