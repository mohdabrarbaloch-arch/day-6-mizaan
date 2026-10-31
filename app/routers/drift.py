"""Drift: the parity matrix across a model's deployments."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_role
from ..engine import drift as drift_engine
from ..models import Deployment, ModelVersion, Region, User
from ..schemas import DriftReport

router = APIRouter(prefix="/api/drift", tags=["drift"])


@router.get("/{model_id}", response_model=DriftReport)
def model_drift(
    model_id: int,
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> DriftReport:
    """Every deployment pair for one model, compared on effective manifest.

    Compares what each deployment is actually running (post-override), not the
    source manifest — comparing the source would hide the overrides you are
    looking for.
    """
    model = db.get(ModelVersion, model_id)
    if model is None or model.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")

    deployments = db.scalars(
        select(Deployment)
        .where(
            Deployment.model_version_id == model.id,
            Deployment.status == "active",
        )
        .order_by(Deployment.region.asc(), Deployment.environment.asc())
    ).all()

    rows = [
        {
            "id": d.id,
            "region": d.region,
            "environment": d.environment,
            "effective_manifest": d.effective_manifest or {},
            "digest": d.digest,
        }
        for d in deployments
    ]

    matrix = drift_engine.build_matrix(rows)

    return DriftReport(
        model_version_id=model.id,
        model_name=model.name,
        model_version=model.version,
        deployment_count=len(rows),
        cells=matrix["cells"],
        summary=matrix["summary"],
    )


@router.get("", response_model=dict)
def fleet_drift(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """A one-line parity verdict per model, for the dashboard.

    This is the number an operator actually watches: how many of my models are
    serving inconsistently across regions right now.
    """
    models = db.scalars(
        select(ModelVersion)
        .where(ModelVersion.owner_id == user.id)
        .order_by(ModelVersion.name.asc(), ModelVersion.version.asc())
    ).all()

    region_names = {r.name for r in db.scalars(select(Region)).all()}

    rows: list[dict[str, object]] = []
    drifting_models = 0
    worst_overall: str | None = None

    for model in models:
        deployments = db.scalars(
            select(Deployment).where(
                Deployment.model_version_id == model.id,
                Deployment.status == "active",
            )
        ).all()

        payload = [
            {
                "id": d.id,
                "region": d.region,
                "environment": d.environment,
                "effective_manifest": d.effective_manifest or {},
                "digest": d.digest,
            }
            for d in deployments
        ]
        matrix = drift_engine.build_matrix(payload)
        summary = matrix["summary"]
        is_drifting = not summary["all_identical"]

        if is_drifting:
            drifting_models += 1
            severity = summary["worst_severity"]
            if severity is not None and (
                worst_overall is None
                or drift_engine.SEVERITY_ORDER.index(str(severity))
                < drift_engine.SEVERITY_ORDER.index(worst_overall)
            ):
                worst_overall = str(severity)

        rows.append(
            {
                "model_version_id": model.id,
                "name": model.name,
                "version": model.version,
                "deployment_count": len(payload),
                "regions": sorted({d.region for d in deployments}),
                "unknown_regions": sorted({d.region for d in deployments} - region_names),
                "identical": summary["all_identical"],
                "drifting_pairs": summary["drifting_pairs"],
                "worst_severity": summary["worst_severity"],
                "summary": (
                    "All deployments identical"
                    if summary["all_identical"]
                    else (
                        f"{summary['drifting_pairs']} drifting pair(s); "
                        f"worst: {summary['worst_severity']}"
                    )
                ),
            }
        )

    return {
        "models": rows,
        "summary": {
            "model_count": len(rows),
            "drifting_models": drifting_models,
            "identical_models": len(rows) - drifting_models,
            "worst_severity": worst_overall,
            "all_identical": drifting_models == 0,
        },
    }
