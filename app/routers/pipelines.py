"""Pipelines: validate a DAG, run it, evaluate gates, promote."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import limit_write, require_role
from ..engine import digest as digest_engine
from ..engine import pipeline as pipeline_engine
from ..models import Pipeline, PipelineRun, User
from ..schemas import (
    PipelineCreate,
    PipelineOut,
    PipelineValidateRequest,
    PromoteRequest,
    RunCreate,
    RunOut,
)

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])

_PROMOTABLE = "promoted"


def _validate(nodes: list[dict[str, object]]) -> dict[str, object]:
    return pipeline_engine.validate(nodes)


def _to_out(pipeline: Pipeline) -> PipelineOut:
    result = _validate(pipeline.nodes or [])
    return PipelineOut(
        id=pipeline.id,
        name=pipeline.name,
        description=pipeline.description,
        nodes=pipeline.nodes or [],
        gates=pipeline.gates or {},
        validated=bool(result["valid"]),
        order=list(result["order"]),  # type: ignore[arg-type]
        warnings=list(result["warnings"]),  # type: ignore[arg-type]
        created_at=pipeline.created_at.isoformat() if pipeline.created_at else "",
    )


@router.post(
    "/validate",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(limit_write)],
)
def validate_pipeline(
    payload: PipelineValidateRequest,
    _user: User = Depends(require_role("viewer")),
) -> dict[str, object]:
    """Validate a graph without storing it — the editor calls this on every edit."""
    nodes = [n.model_dump() for n in payload.nodes]
    return _validate(nodes)


@router.post(
    "",
    response_model=PipelineOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_write)],
)
def create_pipeline(
    payload: PipelineCreate,
    user: User = Depends(require_role("engineer")),
    db: Session = Depends(get_db),
) -> PipelineOut:
    nodes = [n.model_dump() for n in payload.nodes]
    result = _validate(nodes)
    if not result["valid"]:
        # A pipeline that cannot be interpreted must not be stored: it would
        # fail later, somewhere further from the mistake.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Pipeline graph is invalid", "errors": result["errors"]},
        )

    pipeline = Pipeline(
        owner_id=user.id,
        name=payload.name,
        description=payload.description,
        nodes=nodes,
        gates=payload.gates,
    )
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)
    return _to_out(pipeline)


@router.get("", response_model=list[PipelineOut])
def list_pipelines(
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> list[PipelineOut]:
    pipelines = db.scalars(
        select(Pipeline)
        .where(Pipeline.owner_id == user.id)
        .order_by(Pipeline.created_at.desc(), Pipeline.id.desc())
    ).all()
    return [_to_out(p) for p in pipelines]


@router.get("/{pipeline_id}", response_model=PipelineOut)
def get_pipeline(
    pipeline_id: int,
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> PipelineOut:
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None or pipeline.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    return _to_out(pipeline)


@router.post(
    "/{pipeline_id}/runs",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_write)],
)
def create_run(
    pipeline_id: int,
    payload: RunCreate,
    user: User = Depends(require_role("engineer")),
    db: Session = Depends(get_db),
) -> RunOut:
    """Record a run with its observed metrics and evaluate the gates."""
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None or pipeline.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")

    validation = _validate(pipeline.nodes or [])
    gate_result = pipeline_engine.evaluate_gates(pipeline.gates or {}, payload.observed)

    run = PipelineRun(
        pipeline_id=pipeline.id,
        status=payload.status,
        observed=payload.observed,
        gate_result=gate_result,
        order=list(validation["order"]),  # type: ignore[arg-type]
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    promotable, reason = pipeline_engine.can_promote(run.status, gate_result)

    return RunOut(
        id=run.id,
        pipeline_id=run.pipeline_id,
        status=run.status,  # type: ignore[arg-type]
        observed=run.observed or {},
        gate_result=gate_result,
        order=run.order or [],
        promotable=promotable,
        promotion_reason=reason,
        created_at=run.created_at.isoformat() if run.created_at else "",
    )


@router.get("/{pipeline_id}/runs", response_model=list[RunOut])
def list_runs(
    pipeline_id: int,
    user: User = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
) -> list[RunOut]:
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None or pipeline.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")

    runs = db.scalars(
        select(PipelineRun)
        .where(PipelineRun.pipeline_id == pipeline.id)
        .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
    ).all()

    out: list[RunOut] = []
    for run in runs:
        gate_result = run.gate_result or {}
        promotable, reason = pipeline_engine.can_promote(run.status, gate_result)
        out.append(
            RunOut(
                id=run.id,
                pipeline_id=run.pipeline_id,
                status=run.status,  # type: ignore[arg-type]
                observed=run.observed or {},
                gate_result=gate_result,
                order=run.order or [],
                promotable=promotable,
                promotion_reason=reason,
                created_at=run.created_at.isoformat() if run.created_at else "",
            )
        )
    return out


@router.post("/{pipeline_id}/runs/{run_id}/promote", response_model=dict)
def promote_run(
    pipeline_id: int,
    run_id: int,
    payload: PromoteRequest,
    user: User = Depends(require_role("engineer")),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Promote a gated run. Refuses when the run or its gates do not qualify.

    This endpoint deliberately does not deploy anything: promotion marks the run
    as promoted and hands back the parameters a deploy call would need. Making
    promotion itself create deployments would let one click touch every region.
    """
    pipeline = db.get(Pipeline, pipeline_id)
    if pipeline is None or pipeline.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")

    run = db.get(PipelineRun, run_id)
    if run is None or run.pipeline_id != pipeline.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    promotable, reason = pipeline_engine.can_promote(run.status, run.gate_result or {})
    if not promotable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)

    if run.status == _PROMOTABLE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Run is already promoted")

    run.status = _PROMOTABLE
    db.commit()
    db.refresh(run)

    return {
        "promoted": True,
        "run_id": run.id,
        "pipeline_id": pipeline.id,
        "target_region": payload.target_region,
        "environment": payload.environment,
        "replicas": payload.replicas,
        "digest_note": "Deploy with POST /api/deployments using the registered model version.",
        "summary": digest_engine.digest_short(str(run.id)),
    }
