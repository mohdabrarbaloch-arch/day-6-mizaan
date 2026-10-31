"""Deployments: place a model in a region, with overrides, quota and cost."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import limit_write, require_role
from ..engine import capacity
from ..engine import digest as digest_engine
from ..models import Deployment, ModelVersion, Region, User
from ..schemas import DeploymentCreate, DeploymentOut, DeploymentScale

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


def _region_map(db: Session) -> dict[str, Region]:
    return {r.name: r for r in db.scalars(select(Region)).all()}


def _to_out(dep: Deployment, model: ModelVersion, regions: dict[str, Region]) -> DeploymentOut:
    region = regions.get(dep.region)
    gpus_total = capacity.gpus_requested(dep.replicas, dep.gpus_per_replica)
    cost = (
        capacity.monthly_cost(dep.replicas, dep.gpus_per_replica, region.usd_per_gpu_hour)
        if region
        else None
    )
    return DeploymentOut(
        id=dep.id,
        model_version_id=dep.model_version_id,
        model_name=model.name,
        model_version=model.version,
        region=dep.region,
        environment=dep.environment,  # type: ignore[arg-type]
        replicas=dep.replicas,
        gpus_per_replica=dep.gpus_per_replica,
        gpus_total=gpus_total,
        monthly_usd=cost,
        overrides=dep.overrides or {},
        digest=dep.digest,
        digest_short=digest_engine.digest_short(dep.digest),
        status=dep.status,
        created_at=dep.created_at.isoformat() if dep.created_at else "",
    )


@router.post(
    "",
    response_model=DeploymentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_write)],
)
def create_deployment(
    payload: DeploymentCreate,
    user: User = Depends(require_role("engineer")),
    db: Session = Depends(get_db),
) -> DeploymentOut:
    model = db.get(ModelVersion, payload.model_version_id)
    if model is None or model.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")

    regions = _region_map(db)
    region = regions.get(payload.region)
    if region is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown region {payload.region!r}",
        )

    requested = capacity.gpus_requested(payload.replicas, payload.gpus_per_replica)
    quota = capacity.fits(region.gpu_quota, region.gpu_used, requested)
    if not quota["ok"]:
        # 409 with the arithmetic, so the operator knows what to free up.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Region {region.name} has {quota['available']} GPU(s) available "
                f"(quota {quota['quota']}, used {quota['used']}); "
                f"this deployment needs {quota['requested']} "
                f"(short by {quota['shortfall']})"
            ),
        )

    try:
        effective = digest_engine.merge_overrides(model.manifest or {}, payload.overrides)
        dep_digest = digest_engine.manifest_digest(effective)
    except digest_engine.ManifestError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    dep = Deployment(
        model_version_id=model.id,
        region=region.name,
        environment=payload.environment,
        replicas=payload.replicas,
        gpus_per_replica=payload.gpus_per_replica,
        overrides=payload.overrides,
        effective_manifest=effective,
        digest=dep_digest,
        status="active",
    )
    db.add(dep)

    # Hold the quota in the same transaction as the deployment row.
    region.gpu_used = region.gpu_used + requested

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A {payload.environment} deployment of this model already exists "
                f"in {region.name}"
            ),
        ) from None
    db.refresh(dep)
    db.refresh(region)

    return _to_out(dep, model, regions)


@router.get("", response_model=list[DeploymentOut])
def list_deployments(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> list[DeploymentOut]:
    rows = db.execute(
        select(Deployment, ModelVersion)
        .join(ModelVersion, Deployment.model_version_id == ModelVersion.id)
        .where(ModelVersion.owner_id == user.id)
        .order_by(Deployment.created_at.desc(), Deployment.id.desc())
    ).all()

    regions = _region_map(db)
    return [_to_out(dep, model, regions) for dep, model in rows]


@router.get("/{deployment_id}", response_model=DeploymentOut)
def get_deployment(
    deployment_id: int,
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> DeploymentOut:
    row = db.execute(
        select(Deployment, ModelVersion)
        .join(ModelVersion, Deployment.model_version_id == ModelVersion.id)
        .where(Deployment.id == deployment_id, ModelVersion.owner_id == user.id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    dep, model = row
    return _to_out(dep, model, _region_map(db))


@router.patch("/{deployment_id}/scale", response_model=DeploymentOut)
def scale_deployment(
    deployment_id: int,
    payload: DeploymentScale,
    user: User = Depends(require_role("engineer")),
    db: Session = Depends(get_db),
) -> DeploymentOut:
    """Change replica count, adjusting the region's held quota by the delta."""
    row = db.execute(
        select(Deployment, ModelVersion)
        .join(ModelVersion, Deployment.model_version_id == ModelVersion.id)
        .where(Deployment.id == deployment_id, ModelVersion.owner_id == user.id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    dep, model = row
    regions = _region_map(db)
    region = regions.get(dep.region)
    if region is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Region {dep.region!r} no longer exists",
        )

    current = capacity.gpus_requested(dep.replicas, dep.gpus_per_replica)
    target = capacity.gpus_requested(payload.replicas, dep.gpus_per_replica)
    delta = target - current

    if delta > 0:
        quota = capacity.fits(region.gpu_quota, region.gpu_used, delta)
        if not quota["ok"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Scaling to {payload.replicas} replica(s) needs {delta} more GPU(s); "
                    f"{quota['available']} available in {region.name}"
                ),
            )

    dep.replicas = payload.replicas
    region.gpu_used = max(region.gpu_used + delta, 0)
    db.commit()
    db.refresh(dep)
    db.refresh(region)

    return _to_out(dep, model, regions)


@router.delete("/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def teardown_deployment(
    deployment_id: int,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    row = db.execute(
        select(Deployment, ModelVersion)
        .join(ModelVersion, Deployment.model_version_id == ModelVersion.id)
        .where(Deployment.id == deployment_id, ModelVersion.owner_id == user.id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    dep, _model = row
    region = db.scalar(select(Region).where(Region.name == dep.region))
    if region is not None:
        # Release the quota this deployment was holding.
        held = capacity.gpus_requested(dep.replicas, dep.gpus_per_replica)
        region.gpu_used = max(region.gpu_used - held, 0)

    db.delete(dep)
    db.commit()
