"""Application service and Google ADK tool functions."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, TypeVar

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from cloud_journey.database import SessionLocal, init_db
from cloud_journey.models import JourneyEvent, JourneyOperation
from cloud_journey.state_machine import (
    Actor,
    ConcurrentTransition,
    InvalidTransition,
    JourneyError,
    JourneyState,
    StateMachine,
    TransitionResult,
)

AGENT_ACTOR = Actor("AGENT", "project-factory-agent")
T = TypeVar("T")


@dataclass(frozen=True)
class SimulatedUser:
    name: str
    email: str
    role: str


SIMULATED_USERS: dict[str, SimulatedUser] = {
    "sam": SimulatedUser("sam", "sam@example.com", "PROJECT_OWNER"),
    "reviewer": SimulatedUser("reviewer", "reviewer@example.com", "REVIEWER"),
    "developer": SimulatedUser("developer", "developer@example.com", "DEVELOPER"),
}
APPROVER_ROLES = frozenset({"PROJECT_OWNER", "REVIEWER"})


class AuthorizationDenied(JourneyError):
    def __init__(self, user_name: str, role: str):
        super().__init__(f"Authorization denied: {user_name} ({role}) cannot approve or reject")
        self.user_name = user_name
        self.role = role


class UnknownUser(JourneyError):
    def __init__(self, user_name: str):
        super().__init__(f"Unknown simulated user: {user_name}")
        self.user_name = user_name


def _iso(value: datetime) -> str:
    return value.isoformat()


def _transition_dict(result: TransitionResult) -> dict[str, Any]:
    return {
        "from_state": result.from_state.value,
        "to_state": result.to_state.value,
        "version": result.version,
    }


class JourneyService:
    """Orchestrates simulated work while delegating every state change."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory
        self.state_machine = StateMachine(session_factory)

    @staticmethod
    def _user(user_name: str) -> SimulatedUser:
        normalized = user_name.strip().lower()
        try:
            return SIMULATED_USERS[normalized]
        except KeyError as exc:
            raise UnknownUser(user_name) from exc

    def _start_operation(self, journey_id: str, operation_type: str) -> str:
        operation_id = f"O-{uuid.uuid4().hex.upper()}"
        with self.session_factory.begin() as session:
            session.add(
                JourneyOperation(
                    id=operation_id,
                    journey_id=journey_id,
                    operation_type=operation_type,
                    status="RUNNING",
                    result=None,
                )
            )
        return operation_id

    def _finish_operation(
        self, operation_id: str, *, status: str, result: dict[str, Any]
    ) -> None:
        with self.session_factory.begin() as session:
            session.execute(
                update(JourneyOperation)
                .where(JourneyOperation.id == operation_id)
                .values(status=status, result=result)
            )

    def _run_operation(
        self,
        journey_id: str,
        operation_type: str,
        action: Callable[[], T],
    ) -> T:
        # Validate first so an unknown ID becomes JourneyNotFound rather than a
        # foreign-key error while creating its operation record.
        self.state_machine.get_journey(journey_id)
        operation_id = self._start_operation(journey_id, operation_type)
        try:
            result = action()
        except Exception as exc:
            self._finish_operation(
                operation_id,
                status="FAILED",
                result={"error": type(exc).__name__, "message": str(exc)},
            )
            raise
        self._finish_operation(
            operation_id,
            status="COMPLETED",
            result={"outcome": "success"},
        )
        return result

    def start(self, apm_id: str, user_name: str) -> dict[str, Any]:
        apm_id = apm_id.strip()
        if not apm_id:
            raise ValueError("apm_id must not be empty")
        user = self._user(user_name)
        journey = self.state_machine.create_journey(
            apm_id=apm_id,
            requested_by=user.name,
            requested_by_email=user.email,
            role=user.role,
            context={"apm_validation": "simulated"},
        )

        def action() -> list[TransitionResult]:
            return [
                self.state_machine.transition(
                    journey.id,
                    JourneyState.VALIDATING_APM,
                    actor=AGENT_ACTOR,
                    message="Started simulated APM validation",
                ),
                self.state_machine.transition(
                    journey.id,
                    JourneyState.APM_VALIDATED,
                    actor=AGENT_ACTOR,
                    message="Simulated APM validation succeeded",
                    metadata={"apm_id": apm_id},
                ),
            ]

        transitions = self._run_operation(journey.id, "START_JOURNEY", action)
        return self._response(journey.id, transitions)

    def continue_journey(self, journey_id: str) -> dict[str, Any]:
        pipeline = {
            JourneyState.APM_VALIDATED: JourneyState.COLLECTING_INVENTORY,
            JourneyState.COLLECTING_INVENTORY: JourneyState.INVENTORY_COMPLETE,
            JourneyState.INVENTORY_COMPLETE: JourneyState.GENERATING_PLAN,
            JourneyState.GENERATING_PLAN: JourneyState.WAITING_FOR_APPROVAL,
        }

        def action() -> list[TransitionResult]:
            transitions: list[TransitionResult] = []
            while True:
                current = JourneyState(self.state_machine.get_journey(journey_id).status)
                if current == JourneyState.WAITING_FOR_APPROVAL:
                    break
                target = pipeline.get(current)
                if target is None:
                    # Route the error through the central validator for a consistent error.
                    target = JourneyState.COLLECTING_INVENTORY
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        target,
                        actor=AGENT_ACTOR,
                        message=f"Simulated step completed: {target.value}",
                    )
                )
            return transitions

        transitions = self._run_operation(journey_id, "CONTINUE_JOURNEY", action)
        return self._response(
            journey_id,
            transitions,
            note="Human approval is required before provisioning can continue.",
        )

    def approve(self, journey_id: str, user_name: str) -> dict[str, Any]:
        user = self._user(user_name)

        def action() -> list[TransitionResult]:
            if user.role not in APPROVER_ROLES:
                self.state_machine.record_event(
                    journey_id,
                    event_type="AUTHORIZATION_DENIED",
                    actor=Actor("USER", user.name),
                    message=f"Approval denied for role {user.role}",
                    metadata={"role": user.role, "action": "approve"},
                )
                raise AuthorizationDenied(user.name, user.role)

            transitions: list[TransitionResult] = []
            current = JourneyState(self.state_machine.get_journey(journey_id).status)
            if current == JourneyState.WAITING_FOR_APPROVAL:
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        JourneyState.APPROVED,
                        actor=Actor("USER", user.name),
                        message=f"Journey approved by {user.name}",
                        metadata={"role": user.role},
                    )
                )
            pipeline = {
                JourneyState.APPROVED: JourneyState.PROVISIONING,
                JourneyState.PROVISIONING: JourneyState.VALIDATING_RESULT,
                JourneyState.VALIDATING_RESULT: JourneyState.COMPLETED,
            }
            while True:
                current = JourneyState(self.state_machine.get_journey(journey_id).status)
                if current == JourneyState.COMPLETED:
                    break
                target = pipeline.get(current)
                if target is None:
                    # This deliberately fails at the state-machine boundary.
                    target = JourneyState.APPROVED
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        target,
                        actor=AGENT_ACTOR,
                        message=f"Simulated execution completed: {target.value}",
                    )
                )
            return transitions

        transitions = self._run_operation(journey_id, "APPROVE_JOURNEY", action)
        return self._response(journey_id, transitions, note="Provisioning was simulated.")

    def reject(self, journey_id: str, user_name: str, reason: str) -> dict[str, Any]:
        user = self._user(user_name)
        reason = reason.strip()
        if not reason:
            raise ValueError("A rejection reason is required")

        def action() -> list[TransitionResult]:
            if user.role not in APPROVER_ROLES:
                self.state_machine.record_event(
                    journey_id,
                    event_type="AUTHORIZATION_DENIED",
                    actor=Actor("USER", user.name),
                    message=f"Rejection denied for role {user.role}",
                    metadata={"role": user.role, "action": "reject"},
                )
                raise AuthorizationDenied(user.name, user.role)
            return [
                self.state_machine.transition(
                    journey_id,
                    JourneyState.REJECTED,
                    actor=Actor("USER", user.name),
                    message=reason,
                    metadata={"role": user.role, "rejection_reason": reason},
                )
            ]

        transitions = self._run_operation(journey_id, "REJECT_JOURNEY", action)
        return self._response(journey_id, transitions, note=f"Rejection reason: {reason}")

    def status(self, journey_id: str) -> dict[str, Any]:
        return self._response(journey_id, [])

    def _response(
        self,
        journey_id: str,
        transitions: list[TransitionResult],
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        journey = self.state_machine.get_journey(journey_id)
        events = self.state_machine.get_events(journey_id)
        history = [self._event_dict(event) for event in events]
        response: dict[str, Any] = {
            "ok": True,
            "journey_id": journey.id,
            "apm_id": journey.apm_id,
            "current_state": journey.status,
            "current_step": journey.current_step,
            "version": journey.version,
            "requested_by": journey.requested_by,
            "requested_by_email": journey.requested_by_email,
            "role": journey.role,
            "last_error": journey.last_error,
            "transitions": [_transition_dict(item) for item in transitions],
            "state_path": [
                event.to_state
                for event in events
                if event.event_type in {"JOURNEY_CREATED", "STATE_TRANSITION"}
            ],
            "history": history,
        }
        if note:
            response["note"] = note
        return response

    @staticmethod
    def _event_dict(event: JourneyEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "event_type": event.event_type,
            "from_state": event.from_state,
            "to_state": event.to_state,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "message": event.message,
            "metadata": event.event_metadata,
            "created_at": _iso(event.created_at),
        }


_service: JourneyService | None = None
_service_lock = threading.Lock()


def get_service() -> JourneyService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                init_db()
                _service = JourneyService(SessionLocal)
    return _service


def _tool_call(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return call()
    except AuthorizationDenied as exc:
        return {"ok": False, "status_code": 403, "error": "AuthorizationDenied", "message": str(exc)}
    except InvalidTransition as exc:
        return {
            "ok": False,
            "status_code": 409,
            "error": "InvalidTransition",
            "message": str(exc),
            "current_state": exc.from_state.value,
        }
    except ConcurrentTransition as exc:
        return {"ok": False, "status_code": 409, "error": "ConcurrentTransition", "message": str(exc)}
    except JourneyError as exc:
        return {"ok": False, "status_code": 404, "error": type(exc).__name__, "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "status_code": 400, "error": "InvalidInput", "message": str(exc)}


def start_journey(apm_id: str, user_name: str) -> dict[str, Any]:
    """Start a Cloud Journey for an APM ID as a simulated user."""
    return _tool_call(lambda: get_service().start(apm_id, user_name))


def continue_journey(journey_id: str) -> dict[str, Any]:
    """Continue a Journey through plan generation, stopping for human approval."""
    return _tool_call(lambda: get_service().continue_journey(journey_id))


def approve_journey(journey_id: str, user_name: str) -> dict[str, Any]:
    """Approve a waiting Journey as a simulated user, then simulate execution."""
    return _tool_call(lambda: get_service().approve(journey_id, user_name))


def reject_journey(journey_id: str, user_name: str, reason: str) -> dict[str, Any]:
    """Reject a waiting Journey as a simulated approver and persist the reason."""
    return _tool_call(lambda: get_service().reject(journey_id, user_name, reason))


def get_journey_status(journey_id: str) -> dict[str, Any]:
    """Read the authoritative Journey state and complete audit history from the database."""
    return _tool_call(lambda: get_service().status(journey_id))
