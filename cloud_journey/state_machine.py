"""The sole authority for Journey state transitions."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from cloud_journey.models import Journey, JourneyEvent


class JourneyState(str, Enum):
    CREATED = "CREATED"
    VALIDATING_APM = "VALIDATING_APM"
    APM_VALIDATED = "APM_VALIDATED"
    COLLECTING_INVENTORY = "COLLECTING_INVENTORY"
    INVENTORY_COMPLETE = "INVENTORY_COMPLETE"
    GENERATING_PLAN = "GENERATING_PLAN"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PROVISIONING = "PROVISIONING"
    VALIDATING_RESULT = "VALIDATING_RESULT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


PROCESSING_STATES = {
    JourneyState.VALIDATING_APM,
    JourneyState.COLLECTING_INVENTORY,
    JourneyState.GENERATING_PLAN,
    JourneyState.PROVISIONING,
    JourneyState.VALIDATING_RESULT,
}

ALLOWED_TRANSITIONS: dict[JourneyState, frozenset[JourneyState]] = {
    JourneyState.CREATED: frozenset({JourneyState.VALIDATING_APM}),
    JourneyState.VALIDATING_APM: frozenset({JourneyState.APM_VALIDATED, JourneyState.FAILED}),
    JourneyState.APM_VALIDATED: frozenset({JourneyState.COLLECTING_INVENTORY}),
    JourneyState.COLLECTING_INVENTORY: frozenset(
        {JourneyState.INVENTORY_COMPLETE, JourneyState.FAILED}
    ),
    JourneyState.INVENTORY_COMPLETE: frozenset({JourneyState.GENERATING_PLAN}),
    JourneyState.GENERATING_PLAN: frozenset(
        {JourneyState.WAITING_FOR_APPROVAL, JourneyState.FAILED}
    ),
    JourneyState.WAITING_FOR_APPROVAL: frozenset(
        {JourneyState.APPROVED, JourneyState.REJECTED}
    ),
    JourneyState.APPROVED: frozenset({JourneyState.PROVISIONING}),
    JourneyState.REJECTED: frozenset(),
    JourneyState.PROVISIONING: frozenset(
        {JourneyState.VALIDATING_RESULT, JourneyState.FAILED}
    ),
    JourneyState.VALIDATING_RESULT: frozenset(
        {JourneyState.COMPLETED, JourneyState.FAILED}
    ),
    JourneyState.COMPLETED: frozenset(),
    JourneyState.FAILED: frozenset({JourneyState.RETRYING}),
    JourneyState.RETRYING: frozenset(PROCESSING_STATES),
}


class JourneyError(Exception):
    """Base class for user-visible Journey failures."""


class JourneyNotFound(JourneyError):
    def __init__(self, journey_id: str):
        super().__init__(f"Journey {journey_id} was not found")
        self.journey_id = journey_id


class InvalidTransition(JourneyError):
    def __init__(self, journey_id: str, from_state: JourneyState, to_state: JourneyState):
        super().__init__(
            f"Invalid transition for {journey_id}: {from_state.value} -> {to_state.value}"
        )
        self.journey_id = journey_id
        self.from_state = from_state
        self.to_state = to_state


class ConcurrentTransition(JourneyError):
    pass


class DuplicateApmId(JourneyError):
    def __init__(self, apm_id: str):
        super().__init__("Unable to create a Journey for the requested APM ID")
        self.apm_id = apm_id


class JourneyUnavailable(JourneyError):
    """Non-disclosing response for missing or inaccessible Journey data."""

    def __init__(self):
        super().__init__("No accessible Journey was found for the supplied identifier")


@dataclass(frozen=True)
class Actor:
    actor_type: str
    actor_id: str


@dataclass(frozen=True)
class TransitionResult:
    journey_id: str
    from_state: JourneyState
    to_state: JourneyState
    version: int


def step_name(state: JourneyState) -> str:
    return state.value.lower()


class StateMachine:
    """Validates and persists state transitions with lock + version protection."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def create_journey(
        self,
        *,
        apm_id: str,
        requested_by: str,
        requested_by_email: str,
        role: str,
        owner_subject: str | None = None,
        context: dict[str, Any] | None = None,
        journey_id: str | None = None,
    ) -> Journey:
        journey = Journey(
            id=journey_id or f"J-{secrets.token_hex(4).upper()}",
            apm_id=apm_id,
            status=JourneyState.CREATED.value,
            current_step=step_name(JourneyState.CREATED),
            version=1,
            requested_by=requested_by,
            requested_by_email=requested_by_email,
            owner_subject=owner_subject or requested_by,
            role=role,
            context=context or {},
        )
        try:
            with self._session_factory.begin() as session:
                session.add(journey)
                session.add(
                    JourneyEvent(
                        journey_id=journey.id,
                        event_type="JOURNEY_CREATED",
                        from_state=None,
                        to_state=JourneyState.CREATED.value,
                        actor_type="USER",
                        actor_id=requested_by,
                        message=f"Journey created for APM {apm_id}",
                        event_metadata={},
                    )
                )
        except IntegrityError as exc:
            raise DuplicateApmId(apm_id) from exc
        return journey

    def transition(
        self,
        journey_id: str,
        to_state: JourneyState,
        *,
        actor: Actor,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> TransitionResult:
        with self._session_factory.begin() as session:
            # PostgreSQL holds this row lock until the event and state commit together.
            journey = session.execute(
                select(Journey).where(Journey.id == journey_id).with_for_update()
            ).scalar_one_or_none()
            if journey is None:
                raise JourneyNotFound(journey_id)

            from_state = JourneyState(journey.status)
            if to_state not in ALLOWED_TRANSITIONS[from_state]:
                raise InvalidTransition(journey_id, from_state, to_state)

            next_version = journey.version + 1
            values: dict[str, Any] = {
                "status": to_state.value,
                "current_step": step_name(to_state),
                "version": next_version,
            }
            if to_state == JourneyState.FAILED:
                values["last_error"] = last_error or message
            elif to_state != JourneyState.RETRYING:
                values["last_error"] = None

            # The predicate is a second line of defense on databases where FOR UPDATE
            # is unavailable (including the lightweight SQLite unit-test backend).
            result = session.execute(
                update(Journey)
                .where(
                    Journey.id == journey_id,
                    Journey.status == from_state.value,
                    Journey.version == journey.version,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise ConcurrentTransition(
                    f"Journey {journey_id} changed during transition"
                )

            session.add(
                JourneyEvent(
                    journey_id=journey_id,
                    event_type="STATE_TRANSITION",
                    from_state=from_state.value,
                    to_state=to_state.value,
                    actor_type=actor.actor_type,
                    actor_id=actor.actor_id,
                    message=message,
                    event_metadata=metadata or {},
                )
            )
            return TransitionResult(journey_id, from_state, to_state, next_version)

    def record_event(
        self,
        journey_id: str,
        *,
        event_type: str,
        actor: Actor,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            journey = session.get(Journey, journey_id)
            if journey is None:
                raise JourneyNotFound(journey_id)
            session.add(
                JourneyEvent(
                    journey_id=journey_id,
                    event_type=event_type,
                    from_state=journey.status,
                    to_state=journey.status,
                    actor_type=actor.actor_type,
                    actor_id=actor.actor_id,
                    message=message,
                    event_metadata=metadata or {},
                )
            )

    def merge_context(
        self,
        journey_id: str,
        updates: dict[str, Any],
        *,
        actor: Actor,
        message: str,
    ) -> dict[str, Any]:
        """Persist gathered Journey knowledge without changing its state/version."""
        with self._session_factory.begin() as session:
            journey = session.execute(
                select(Journey).where(Journey.id == journey_id).with_for_update()
            ).scalar_one_or_none()
            if journey is None:
                raise JourneyNotFound(journey_id)
            merged = dict(journey.context or {})
            merged.update(updates)
            session.execute(
                update(Journey)
                .where(Journey.id == journey_id)
                .values(context=merged)
            )
            session.add(
                JourneyEvent(
                    journey_id=journey_id,
                    event_type="CONTEXT_UPDATED",
                    from_state=journey.status,
                    to_state=journey.status,
                    actor_type=actor.actor_type,
                    actor_id=actor.actor_id,
                    message=message,
                    event_metadata={"updated_sections": sorted(updates)},
                )
            )
            return merged

    def get_journey(self, journey_id: str) -> Journey:
        with self._session_factory() as session:
            journey = session.get(Journey, journey_id)
            if journey is None:
                raise JourneyNotFound(journey_id)
            session.expunge(journey)
            return journey

    def find_journey_by_apm_id(self, apm_id: str) -> Journey | None:
        """Internal unscoped lookup; callers must apply ownership before returning it."""
        with self._session_factory() as session:
            journey = session.scalar(select(Journey).where(Journey.apm_id == apm_id))
            if journey is not None:
                session.expunge(journey)
            return journey

    def get_owned_journey(
        self, journey_id: str, owner_subject: str
    ) -> Journey:
        """Return an owned Journey without revealing whether another owner's exists."""
        with self._session_factory() as session:
            journey = session.scalar(
                select(Journey).where(
                    Journey.id == journey_id,
                    Journey.owner_subject == owner_subject,
                )
            )
            if journey is None:
                raise JourneyUnavailable()
            session.expunge(journey)
            return journey

    def get_owned_journey_by_apm_id(
        self, apm_id: str, owner_subject: str
    ) -> Journey:
        with self._session_factory() as session:
            journey = session.scalar(
                select(Journey).where(
                    Journey.apm_id == apm_id,
                    Journey.owner_subject == owner_subject,
                )
            )
            if journey is None:
                raise JourneyUnavailable()
            session.expunge(journey)
            return journey

    def get_events(self, journey_id: str) -> list[JourneyEvent]:
        with self._session_factory() as session:
            if session.get(Journey, journey_id) is None:
                raise JourneyNotFound(journey_id)
            events = list(
                session.scalars(
                    select(JourneyEvent)
                    .where(JourneyEvent.journey_id == journey_id)
                    .order_by(JourneyEvent.id)
                )
            )
            for event in events:
                session.expunge(event)
            return events
