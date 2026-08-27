"""SQLAlchemy models for durable Journey state and its audit trail."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Journey(Base):
    __tablename__ = "journeys"
    __table_args__ = (UniqueConstraint("apm_id", name="uq_journeys_apm_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    apm_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    current_step: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by_email: Mapped[str] = mapped_column(String(320), nullable=False)
    owner_subject: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    events: Mapped[list["JourneyEvent"]] = relationship(
        back_populates="journey", order_by="JourneyEvent.id"
    )
    operations: Mapped[list["JourneyOperation"]] = relationship(
        back_populates="journey", order_by="JourneyOperation.created_at"
    )


class JourneyEvent(Base):
    __tablename__ = "journey_events"
    __table_args__ = (Index("ix_journey_events_journey_created", "journey_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    journey: Mapped[Journey] = relationship(back_populates="events")


class JourneyOperation(Base):
    __tablename__ = "journey_operations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    journey: Mapped[Journey] = relationship(back_populates="operations")
