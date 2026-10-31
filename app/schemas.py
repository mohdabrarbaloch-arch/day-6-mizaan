"""Pydantic v2 request/response schemas.

Every inbound body is validated here. The manifest is deliberately typed
loosely (``dict``) at the component level — model manifests vary wildly between
teams — but the envelope (components present, params numeric and in range) is
strict, because those are the fields the digest depends on.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Role = Literal["viewer", "engineer", "admin"]
Environment = Literal["production", "staging", "canary", "shadow"]
RunStatus = Literal["pending", "running", "succeeded", "failed"]


# --------------------------------------------------------------------------- auth


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=72)
    display_name: str = Field(default="", max_length=120)
    role: Role = "engineer"

    @field_validator("password")
    @classmethod
    def password_not_trivial(cls, v: str) -> str:
        # Length is enforced above; this catches the laziest possible inputs
        # without inventing a rigid complexity policy.
        if len(set(v)) < 4:
            raise ValueError("password must use at least 4 distinct characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    role: Role


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    user: UserOut


# -------------------------------------------------------------------------- models


class ManifestIn(BaseModel):
    """The manifest envelope.

    Components are free-form objects but the four known names are type-checked
    when present, because the digest assumes they are objects.
    """

    components: dict[str, dict[str, Any]] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("components")
    @classmethod
    def known_components_are_objects(
        cls, v: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        for name, value in v.items():
            if not isinstance(value, dict):
                raise ValueError(f"components.{name} must be an object")
        return v

    @field_validator("params")
    @classmethod
    def params_are_sane(cls, v: dict[str, Any]) -> dict[str, Any]:
        temperature = v.get("temperature")
        if temperature is not None:
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
                raise ValueError("params.temperature must be a number")
            if not 0.0 <= float(temperature) <= 2.0:
                raise ValueError("params.temperature must be between 0.0 and 2.0")
        top_p = v.get("top_p")
        if top_p is not None:
            if not isinstance(top_p, (int, float)) or isinstance(top_p, bool):
                raise ValueError("params.top_p must be a number")
            if not 0.0 < float(top_p) <= 1.0:
                raise ValueError("params.top_p must be in (0.0, 1.0]")
        max_tokens = v.get("max_tokens")
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
                raise ValueError("params.max_tokens must be an integer")
            if max_tokens < 1:
                raise ValueError("params.max_tokens must be at least 1")
        return v


class ModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=2000)
    manifest: ManifestIn

    @field_validator("name", "version")
    @classmethod
    def no_surrounding_space(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: str
    description: str
    digest: str
    digest_short: str
    component_digests: dict[str, str]
    summary: dict[str, Any]
    deployment_count: int = 0
    created_at: str


# --------------------------------------------------------------- deployments


class DeploymentCreate(BaseModel):
    model_version_id: int = Field(gt=0)
    region: str = Field(min_length=1, max_length=64)
    environment: Environment = "production"
    replicas: int = Field(default=1, ge=1, le=64)
    gpus_per_replica: int = Field(default=1, ge=0, le=16)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def overrides_are_flat(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 32:
            raise ValueError("at most 32 override paths per deployment")
        for key in v:
            if not isinstance(key, str) or not key:
                raise ValueError("override keys must be non-empty strings")
            if not (key.startswith("components.") or key.startswith("params.")):
                raise ValueError(f"override {key!r} must start with 'components.' or 'params.'")
        return v


class DeploymentScale(BaseModel):
    replicas: int = Field(ge=1, le=64)


class DeploymentOut(BaseModel):
    id: int
    model_version_id: int
    model_name: str
    model_version: str
    region: str
    environment: Environment
    replicas: int
    gpus_per_replica: int
    gpus_total: int
    monthly_usd: str | None
    overrides: dict[str, Any]
    digest: str
    digest_short: str
    status: str
    created_at: str


# ------------------------------------------------------------------- pipelines


class PipelineNode(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    kind: Literal["ingest", "train", "eval", "register", "deploy"]
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    label: str = Field(default="", max_length=120)


class PipelineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    nodes: list[PipelineNode] = Field(min_length=1, max_length=50)
    gates: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("gates")
    @classmethod
    def gates_are_bounded(cls, v: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for metric, bound in v.items():
            if not isinstance(bound, dict):
                raise ValueError(f"gate {metric!r} must be an object with min and/or max")
            if not bound:
                raise ValueError(f"gate {metric!r} must specify min and/or max")
            for key, value in bound.items():
                if key not in ("min", "max"):
                    raise ValueError(f"gate {metric!r} has unknown bound {key!r}")
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(f"gate {metric!r}.{key} must be a number")
        return v


class PipelineValidateRequest(BaseModel):
    nodes: list[PipelineNode] = Field(min_length=1, max_length=50)


class PipelineOut(BaseModel):
    id: int
    name: str
    description: str
    nodes: list[dict[str, Any]]
    gates: dict[str, Any]
    validated: bool
    order: list[str]
    warnings: list[str]
    created_at: str


class RunCreate(BaseModel):
    observed: dict[str, float] = Field(default_factory=dict)
    status: RunStatus = "succeeded"

    @field_validator("observed")
    @classmethod
    def observed_are_numbers(cls, v: dict[str, float]) -> dict[str, float]:
        for metric, value in v.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"observed.{metric} must be a number")
        return v


class RunOut(BaseModel):
    id: int
    pipeline_id: int
    status: RunStatus
    observed: dict[str, Any]
    gate_result: dict[str, Any]
    order: list[str]
    promotable: bool
    promotion_reason: str
    created_at: str


class PromoteRequest(BaseModel):
    target_region: str | None = Field(default=None, max_length=64)
    environment: Environment = "production"
    replicas: int = Field(default=1, ge=1, le=64)
    gpus_per_replica: int = Field(default=1, ge=0, le=16)


# ---------------------------------------------------------------------- drift


class DiffOut(BaseModel):
    field: str
    a: Any
    b: Any


class DriftCellOut(BaseModel):
    a: int
    b: int
    a_region: str
    b_region: str
    a_environment: str
    b_environment: str
    identical: bool
    classes: list[str]
    severity: str | None
    diffs: list[DiffOut]
    summary: str


class DriftReport(BaseModel):
    model_version_id: int
    model_name: str
    model_version: str
    deployment_count: int
    cells: list[DriftCellOut]
    summary: dict[str, Any]


# ------------------------------------------------------------------- capacity


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    gpu_quota: int
    gpu_used: int
    usd_per_gpu_hour: str


class CapacityOut(BaseModel):
    summary: dict[str, Any]
    fleet: dict[str, Any]


class HealthOut(BaseModel):
    status: str
    version: str
    backend: str
    database: str
