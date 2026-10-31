"""SQLAlchemy 2.0 ORM models.

SQLite for development, PostgreSQL in compose. Column types stay portable:
JSON rather than JSONB, no server-side defaults that differ between engines, and
integer ids assigned by the database in both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    model_versions: Mapped[list[ModelVersion]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Region(Base):
    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    gpu_quota: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usd_per_gpu_hour: Mapped[str] = mapped_column(String(16), nullable=False, default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelVersion(Base):
    """A registered model artefact, identified by the digest of its manifest."""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", "version", name="uq_model_name_version"),
        Index("ix_model_owner_digest", "owner_id", "digest"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # The manifest exactly as submitted, and its digest.
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped[User] = relationship(back_populates="model_versions")
    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="model_version", cascade="all, delete-orphan"
    )


class Deployment(Base):
    """A model placed into a region, with its own effective manifest and digest."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("model_version_id", "region", "environment", name="uq_deployment_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_version_id: Mapped[int] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False
    )

    region: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="production")

    replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    gpus_per_replica: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # The manifest after overrides are applied — this is what actually serves
    # traffic, so this is what drift compares.
    effective_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    model_version: Mapped[ModelVersion] = relationship(back_populates="deployments")


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    gates: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    runs: Mapped[list[PipelineRun]] = relationship(
        back_populates="pipeline", cascade="all, delete-orphan"
    )


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_run_pipeline_status", "pipeline_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pipeline_id: Mapped[int] = mapped_column(
        ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    observed: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    gate_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    order: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    pipeline: Mapped[Pipeline] = relationship(back_populates="runs")
