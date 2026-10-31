"""Model registry: register a manifest, get its digest, list and inspect."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import limit_write, require_role
from ..engine import digest as digest_engine
from ..models import Deployment, ModelVersion, User
from ..schemas import ModelCreate, ModelOut

router = APIRouter(prefix="/api/models", tags=["models"])


def _to_out(model: ModelVersion, deployment_count: int = 0) -> ModelOut:
    manifest = model.manifest or {}
    return ModelOut(
        id=model.id,
        name=model.name,
        version=model.version,
        description=model.description,
        digest=model.digest,
        digest_short=digest_engine.digest_short(model.digest),
        component_digests=digest_engine.component_digests(manifest),
        summary=digest_engine.manifest_summary(manifest),
        deployment_count=deployment_count,
        created_at=model.created_at.isoformat() if model.created_at else "",
    )


@router.post(
    "",
    response_model=ModelOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_write)],
)
def create_model(
    payload: ModelCreate,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
) -> ModelOut:
    """Register a model version. Registry writes are admin-only."""
    # Register name/version live in columns, not in the manifest: they are
    # labels, not behaviour. Keeping them out means two names for the same
    # artefact share a digest, which is what makes the digest an identity.
    manifest = {
        "components": payload.manifest.components,
        "params": payload.manifest.params,
    }

    try:
        digest = digest_engine.manifest_digest(manifest)
    except digest_engine.ManifestError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    model = ModelVersion(
        owner_id=user.id,
        name=payload.name,
        version=payload.version,
        description=payload.description,
        manifest=manifest,
        digest=digest,
    )
    db.add(model)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{payload.name} {payload.version} is already registered",
        ) from None
    db.refresh(model)

    return _to_out(model, deployment_count=0)


@router.get("", response_model=list[ModelOut])
def list_models(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> list[ModelOut]:
    models: Sequence[ModelVersion] = db.scalars(
        select(ModelVersion)
        .where(ModelVersion.owner_id == user.id)
        .order_by(ModelVersion.created_at.desc(), ModelVersion.id.desc())
    ).all()

    counts: dict[int, int] = {}
    if models:
        rows = db.execute(
            select(Deployment.model_version_id, func.count(Deployment.id))
            .where(Deployment.model_version_id.in_([m.id for m in models]))
            .group_by(Deployment.model_version_id)
        ).all()
        counts = {int(mid): int(count) for mid, count in rows}

    return [_to_out(m, counts.get(m.id, 0)) for m in models]


@router.get("/{model_id}", response_model=ModelOut)
def get_model(
    model_id: int,
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> ModelOut:
    model = db.get(ModelVersion, model_id)
    # 404 for another user's model, not 403: a 403 confirms the id exists.
    if model is None or model.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")

    count = db.scalar(
        select(func.count(Deployment.id)).where(Deployment.model_version_id == model.id)
    )
    return _to_out(model, int(count or 0))


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_model(
    model_id: int,
    user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    model = db.get(ModelVersion, model_id)
    if model is None or model.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")

    live = db.scalar(
        select(func.count(Deployment.id)).where(
            Deployment.model_version_id == model.id,
            Deployment.status == "active",
        )
    )
    if live:
        # Deleting a model with live deployments would orphan traffic. Force the
        # operator to tear those down explicitly first.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{int(live)} active deployment(s) reference this model; tear them down first",
        )

    db.delete(model)
    db.commit()
