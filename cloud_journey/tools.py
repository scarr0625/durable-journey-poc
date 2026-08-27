"""Application service and Google ADK tool functions."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, TypeVar

from google.adk.tools import ToolContext
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from cloud_journey.authorization import (
    AuthorizationDecision,
    SimulatedUser,
    evaluate_approval_authorization,
    get_simulated_user,
)
from cloud_journey.database import SessionLocal, init_db
from cloud_journey.models import JourneyEvent, JourneyOperation
from cloud_journey.state_machine import (
    Actor,
    ConcurrentTransition,
    DuplicateApmId,
    InvalidTransition,
    JourneyError,
    JourneyState,
    JourneyUnavailable,
    StateMachine,
    TransitionResult,
)

AGENT_ACTOR = Actor("AGENT", "project-factory-agent")
T = TypeVar("T")


class AuthorizationDenied(JourneyError):
    def __init__(self, decision: AuthorizationDecision):
        super().__init__(f"Authorization denied: {decision.reason}")
        self.user_name = decision.user_name
        self.role = decision.role
        self.decision = decision


class UnknownUser(JourneyError):
    def __init__(self, user_name: str):
        super().__init__(f"Unknown simulated user: {user_name}")
        self.user_name = user_name


class ProjectOwnerRequired(JourneyError):
    def __init__(self):
        super().__init__("Only a project owner can start an APM Journey")


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
        user = get_simulated_user(user_name)
        if user is None:
            raise UnknownUser(user_name)
        return user

    def check_approval_authorization(
        self, journey_id: str, user_name: str, action: str
    ) -> dict[str, Any]:
        """Return an explainable decision without changing Journey state."""
        user = self._user(user_name)
        journey = self.state_machine.get_journey(journey_id)
        decision = evaluate_approval_authorization(
            user=user,
            action=action,
            requested_by=journey.requested_by,
        )
        return {
            "ok": True,
            "journey_id": journey_id,
            "current_state": journey.status,
            "requested_by": journey.requested_by,
            "user_name": decision.user_name,
            "role": decision.role,
            "groups": sorted(decision.groups),
            "action": decision.action,
            "authorized": decision.allowed,
            "required_group": decision.required_group,
            "reason": decision.reason,
        }

    def _require_approval_authorization(
        self, journey_id: str, user: SimulatedUser, action: str
    ) -> AuthorizationDecision:
        journey = self.state_machine.get_journey(journey_id)
        decision = evaluate_approval_authorization(
            user=user,
            action=action,
            requested_by=journey.requested_by,
        )
        if not decision.allowed:
            self.state_machine.record_event(
                journey_id,
                event_type="AUTHORIZATION_DENIED",
                actor=Actor("USER", user.name),
                message=decision.reason,
                metadata={
                    "role": user.role,
                    "groups": sorted(user.groups),
                    "required_group": decision.required_group,
                    "action": decision.action,
                },
            )
            raise AuthorizationDenied(decision)
        return decision

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

    def require_owner(self, journey_id: str, owner_subject: str) -> None:
        self.state_machine.get_owned_journey(journey_id, owner_subject)

    def start(
        self, apm_id: str, user_name: str, owner_subject: str | None = None
    ) -> dict[str, Any]:
        apm_id = apm_id.strip()
        if not apm_id:
            raise ValueError("apm_id must not be empty")
        user = self._user(user_name)
        if user.role != "PROJECT_OWNER":
            raise ProjectOwnerRequired()
        subject = (owner_subject or user.name).strip()
        if not subject:
            raise ValueError("owner subject must not be empty")

        existing = self.state_machine.find_journey_by_apm_id(apm_id)
        if existing is not None:
            if existing.owner_subject != subject:
                raise DuplicateApmId(apm_id)
            response = self._response(existing.id, [])
            response.update(
                {
                    "created": False,
                    "note": "This APM ID already has a Journey owned by the current user; returning its durable status.",
                }
            )
            return response

        try:
            journey = self.state_machine.create_journey(
                apm_id=apm_id,
                requested_by=user.name,
                requested_by_email=user.email,
                role=user.role,
                owner_subject=subject,
                context={"apm_validation": "simulated"},
            )
        except DuplicateApmId:
            # Handles a concurrent create race without disclosing another owner.
            existing = self.state_machine.find_journey_by_apm_id(apm_id)
            if existing is not None and existing.owner_subject == subject:
                response = self._response(existing.id, [])
                response.update({"created": False, "note": "The APM Journey already exists."})
                return response
            raise

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
        response = self._response(journey.id, transitions)
        response["created"] = True
        return response

    def continue_journey(self, journey_id: str) -> dict[str, Any]:
        """Compatibility shortcut; the conversational agent uses explicit steps."""
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

    def guidance(self, question: str, journey_id: str = "") -> dict[str, Any]:
        """Provide state-aware discovery guidance without changing business state."""
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")

        response: dict[str, Any] = {
            "ok": True,
            "question": question,
            "guidance": (
                "Cloud Compass starts with application discovery: business criticality, "
                "current hosting, environments, dependencies, data classification, "
                "availability needs, target outcomes, and delivery constraints. It uses "
                "that knowledge to prepare a reviewable plan before provisioning."
            ),
        }
        if not journey_id.strip():
            response["recommended_next_action"] = (
                "Start a Journey when you want this discovery to become durable work."
            )
            return response

        journey = self.state_machine.get_journey(journey_id.strip())
        state = JourneyState(journey.status)
        next_steps: dict[JourneyState, tuple[list[str], str]] = {
            JourneyState.APM_VALIDATED: (
                [
                    "application name",
                    "business criticality",
                    "current platform",
                    "environments",
                    "dependencies",
                    "data classification",
                    "availability requirement",
                ],
                "Record the application inventory after the owner provides these facts.",
            ),
            JourneyState.INVENTORY_COMPLETE: (
                ["target platform", "migration objectives", "constraints"],
                "Discuss options, then generate a proposed Cloud plan.",
            ),
            JourneyState.WAITING_FOR_APPROVAL: (
                [],
                "The plan is ready. An independent CLOUD_JOURNEY_APPROVERS member must review it.",
            ),
            JourneyState.COMPLETED: ([], "The simulated Journey is complete."),
            JourneyState.REJECTED: (
                [],
                "Review the rejection reason before starting a revised Journey.",
            ),
        }
        required, next_action = next_steps.get(
            state,
            ([], f"The Journey is currently processing the {journey.current_step} step."),
        )
        response.update(
            {
                "journey_id": journey.id,
                "current_state": journey.status,
                "known_context": journey.context,
                "information_needed": required,
                "recommended_next_action": next_action,
            }
        )
        return response

    def record_inventory(
        self,
        journey_id: str,
        application_name: str,
        business_criticality: str,
        current_platform: str,
        environments: str,
        dependencies: str,
        data_classification: str,
        availability_requirement: str,
    ) -> dict[str, Any]:
        """Persist owner-provided application knowledge and complete discovery."""
        inventory = {
            "application_name": application_name.strip(),
            "business_criticality": business_criticality.strip(),
            "current_platform": current_platform.strip(),
            "environments": environments.strip(),
            "dependencies": dependencies.strip(),
            "data_classification": data_classification.strip(),
            "availability_requirement": availability_requirement.strip(),
        }
        missing = [key for key, value in inventory.items() if not value]
        if missing:
            raise ValueError(f"Missing inventory fields: {', '.join(missing)}")

        def action() -> list[TransitionResult]:
            transitions: list[TransitionResult] = []
            journey = self.state_machine.get_journey(journey_id)
            current = JourneyState(journey.status)
            if current == JourneyState.APM_VALIDATED:
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        JourneyState.COLLECTING_INVENTORY,
                        actor=AGENT_ACTOR,
                        message="Started application discovery and inventory collection",
                    )
                )
                current = JourneyState.COLLECTING_INVENTORY
            if current not in {
                JourneyState.COLLECTING_INVENTORY,
                JourneyState.INVENTORY_COMPLETE,
            }:
                self.state_machine.transition(
                    journey_id,
                    JourneyState.COLLECTING_INVENTORY,
                    actor=AGENT_ACTOR,
                )
            self.state_machine.merge_context(
                journey_id,
                {"inventory": inventory},
                actor=Actor("USER", journey.requested_by),
                message="Application inventory supplied by the Journey requester",
            )
            if current == JourneyState.COLLECTING_INVENTORY:
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        JourneyState.INVENTORY_COMPLETE,
                        actor=AGENT_ACTOR,
                        message="Application discovery and inventory are complete",
                    )
                )
            return transitions

        transitions = self._run_operation(journey_id, "RECORD_INVENTORY", action)
        return self._response(
            journey_id,
            transitions,
            note="Application knowledge is durable. Discuss the target approach before generating a plan.",
        )

    def generate_plan(
        self,
        journey_id: str,
        target_platform: str,
        migration_objectives: str,
        constraints: str,
    ) -> dict[str, Any]:
        """Generate and persist a transparent simulated plan for human review."""
        inputs = {
            "target_platform": target_platform.strip(),
            "migration_objectives": migration_objectives.strip(),
            "constraints": constraints.strip(),
        }
        missing = [key for key, value in inputs.items() if not value]
        if missing:
            raise ValueError(f"Missing planning fields: {', '.join(missing)}")

        def action() -> list[TransitionResult]:
            transitions: list[TransitionResult] = []
            current = JourneyState(self.state_machine.get_journey(journey_id).status)
            if current == JourneyState.INVENTORY_COMPLETE:
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        JourneyState.GENERATING_PLAN,
                        actor=AGENT_ACTOR,
                        message="Started generating a plan from captured application knowledge",
                    )
                )
                current = JourneyState.GENERATING_PLAN
            if current != JourneyState.GENERATING_PLAN:
                self.state_machine.transition(
                    journey_id,
                    JourneyState.GENERATING_PLAN,
                    actor=AGENT_ACTOR,
                )
            plan = {
                **inputs,
                "steps": [
                    "Validate target landing-zone and security prerequisites",
                    "Prepare application and dependency migration",
                    "Migrate data using an approved cutover approach",
                    "Run functional, security, and availability validation",
                    "Complete controlled cutover and post-migration review",
                ],
                "provisioning": "simulated",
            }
            self.state_machine.merge_context(
                journey_id,
                {"proposed_plan": plan},
                actor=AGENT_ACTOR,
                message="Generated a simulated Cloud Journey plan",
            )
            transitions.append(
                self.state_machine.transition(
                    journey_id,
                    JourneyState.WAITING_FOR_APPROVAL,
                    actor=AGENT_ACTOR,
                    message="Plan is ready for independent human review",
                )
            )
            return transitions

        transitions = self._run_operation(journey_id, "GENERATE_PLAN", action)
        return self._response(
            journey_id,
            transitions,
            note="The proposed plan is ready for independent approval; no resources were provisioned.",
        )

    def approve(self, journey_id: str, user_name: str) -> dict[str, Any]:
        """Compatibility API; Cloud Compass does not expose this to chat."""
        user = self._user(user_name)

        def action() -> list[TransitionResult]:
            decision = self._require_approval_authorization(
                journey_id, user, "approve"
            )

            transitions: list[TransitionResult] = []
            current = JourneyState(self.state_machine.get_journey(journey_id).status)
            if current == JourneyState.WAITING_FOR_APPROVAL:
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        JourneyState.APPROVED,
                        actor=Actor("USER", user.name),
                        message=f"Journey approved by {user.name}",
                        metadata={
                            "role": user.role,
                            "groups": sorted(user.groups),
                            "authorization_basis": decision.required_group,
                        },
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
        """Compatibility API; Cloud Compass does not expose this to chat."""
        user = self._user(user_name)
        reason = reason.strip()
        if not reason:
            raise ValueError("A rejection reason is required")

        def action() -> list[TransitionResult]:
            decision = self._require_approval_authorization(
                journey_id, user, "reject"
            )
            return [
                self.state_machine.transition(
                    journey_id,
                    JourneyState.REJECTED,
                    actor=Actor("USER", user.name),
                    message=reason,
                    metadata={
                        "role": user.role,
                        "groups": sorted(user.groups),
                        "authorization_basis": decision.required_group,
                        "rejection_reason": reason,
                    },
                )
            ]

        transitions = self._run_operation(journey_id, "REJECT_JOURNEY", action)
        return self._response(journey_id, transitions, note=f"Rejection reason: {reason}")

    def record_external_approval(
        self, journey_id: str, reviewer_name: str
    ) -> dict[str, Any]:
        """Approval-backend boundary: persist a decision without executing work."""
        reviewer = self._user(reviewer_name)

        def action() -> list[TransitionResult]:
            decision = self._require_approval_authorization(
                journey_id, reviewer, "approve"
            )
            return [
                self.state_machine.transition(
                    journey_id,
                    JourneyState.APPROVED,
                    actor=Actor("APPROVAL_BACKEND", reviewer.name),
                    message=f"External approval received from {reviewer.name}",
                    metadata={
                        "reviewer_role": reviewer.role,
                        "reviewer_groups": sorted(reviewer.groups),
                        "authorization_basis": decision.required_group,
                        "source": "external-approval-backend",
                    },
                )
            ]

        transitions = self._run_operation(journey_id, "EXTERNAL_APPROVAL", action)
        return self._response(
            journey_id,
            transitions,
            note="The external approval backend persisted APPROVED. Execution has not resumed yet.",
        )

    def record_external_rejection(
        self, journey_id: str, reviewer_name: str, reason: str
    ) -> dict[str, Any]:
        """Approval-backend boundary: persist an external rejection."""
        reviewer = self._user(reviewer_name)
        reason = reason.strip()
        if not reason:
            raise ValueError("A rejection reason is required")

        def action() -> list[TransitionResult]:
            decision = self._require_approval_authorization(
                journey_id, reviewer, "reject"
            )
            return [
                self.state_machine.transition(
                    journey_id,
                    JourneyState.REJECTED,
                    actor=Actor("APPROVAL_BACKEND", reviewer.name),
                    message=reason,
                    metadata={
                        "reviewer_role": reviewer.role,
                        "reviewer_groups": sorted(reviewer.groups),
                        "authorization_basis": decision.required_group,
                        "source": "external-approval-backend",
                        "rejection_reason": reason,
                    },
                )
            ]

        transitions = self._run_operation(journey_id, "EXTERNAL_REJECTION", action)
        return self._response(
            journey_id,
            transitions,
            note=f"The external approval backend persisted REJECTED: {reason}",
        )

    def wait_for_approval(
        self,
        journey_id: str,
        timeout_seconds: int = 120,
        poll_interval_seconds: int = 2,
    ) -> dict[str, Any]:
        """Poll durable state only; this method cannot create an approval."""
        if timeout_seconds < 0 or timeout_seconds > 120:
            raise ValueError("timeout_seconds must be between 0 and 120 for this PoC")
        if poll_interval_seconds < 1 or poll_interval_seconds > 30:
            raise ValueError("poll_interval_seconds must be between 1 and 30")

        deadline = time.monotonic() + timeout_seconds
        while True:
            observed_state = JourneyState(
                self.state_machine.get_journey(journey_id).status
            )
            if observed_state in {
                JourneyState.APPROVED,
                JourneyState.REJECTED,
                JourneyState.COMPLETED,
            }:
                break
            if observed_state != JourneyState.WAITING_FOR_APPROVAL:
                raise InvalidTransition(journey_id, observed_state, JourneyState.APPROVED)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(float(poll_interval_seconds), remaining))

        response = self._response(journey_id, [])
        response.update(
            {
                "approval_observed": observed_state
                in {JourneyState.APPROVED, JourneyState.COMPLETED},
                "decision": (
                    observed_state.value
                    if observed_state
                    in {JourneyState.APPROVED, JourneyState.REJECTED}
                    else None
                ),
                "timed_out": observed_state == JourneyState.WAITING_FOR_APPROVAL,
                "waited_up_to_seconds": timeout_seconds,
            }
        )
        if response["timed_out"]:
            response["note"] = (
                "No external decision was observed during this wait. The Journey "
                "remains WAITING_FOR_APPROVAL; check again later."
            )
        elif observed_state == JourneyState.APPROVED:
            response["note"] = (
                "External APPROVED was observed in PostgreSQL. Cloud Compass may now resume execution."
            )
        elif observed_state == JourneyState.REJECTED:
            response["note"] = (
                "External REJECTED was observed in PostgreSQL. Execution will not resume."
            )
        return response

    def resume_after_approval(self, journey_id: str) -> dict[str, Any]:
        """Resume simulated execution only after the database says APPROVED."""
        pipeline = {
            JourneyState.APPROVED: JourneyState.PROVISIONING,
            JourneyState.PROVISIONING: JourneyState.VALIDATING_RESULT,
            JourneyState.VALIDATING_RESULT: JourneyState.COMPLETED,
        }

        def action() -> list[TransitionResult]:
            transitions: list[TransitionResult] = []
            while True:
                current = JourneyState(self.state_machine.get_journey(journey_id).status)
                if current == JourneyState.COMPLETED:
                    break
                target = pipeline.get(current)
                if target is None:
                    # The central validator rejects execution without external approval.
                    target = JourneyState.PROVISIONING
                transitions.append(
                    self.state_machine.transition(
                        journey_id,
                        target,
                        actor=AGENT_ACTOR,
                        message=f"Resumed after external approval: {target.value}",
                    )
                )
            return transitions

        transitions = self._run_operation(journey_id, "RESUME_AFTER_APPROVAL", action)
        return self._response(
            journey_id,
            transitions,
            note="External approval was observed; provisioning and validation were simulated.",
        )

    def status(self, journey_id: str) -> dict[str, Any]:
        return self._response(journey_id, [])

    def status_by_apm_id(
        self, apm_id: str, owner_subject: str
    ) -> dict[str, Any]:
        apm_id = apm_id.strip()
        if not apm_id:
            raise ValueError("apm_id must not be empty")
        journey = self.state_machine.get_owned_journey_by_apm_id(
            apm_id, owner_subject
        )
        response = self._response(journey.id, [])
        response["lookup"] = "apm_id"
        return response

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
            "context": journey.context,
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
    except DuplicateApmId as exc:
        return {"ok": False, "status_code": 409, "error": "ApmIdUnavailable", "message": str(exc)}
    except ProjectOwnerRequired as exc:
        return {"ok": False, "status_code": 403, "error": "ProjectOwnerRequired", "message": str(exc)}
    except JourneyError as exc:
        return {"ok": False, "status_code": 404, "error": type(exc).__name__, "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "status_code": 400, "error": "InvalidInput", "message": str(exc)}


def start_journey(
    apm_id: str, user_name: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Start a Cloud Journey for an APM ID as a simulated user."""
    return _tool_call(
        lambda: get_service().start(apm_id, user_name, tool_context.user_id)
    )


def continue_journey(journey_id: str) -> dict[str, Any]:
    """Compatibility shortcut that advances through discovery and planning states."""
    return _tool_call(lambda: get_service().continue_journey(journey_id))


def get_cloud_journey_guidance(
    question: str, tool_context: ToolContext, journey_id: str = ""
) -> dict[str, Any]:
    """Answer a discovery question and explain what information the Journey needs next."""
    def call() -> dict[str, Any]:
        service = get_service()
        if journey_id.strip():
            service.require_owner(journey_id, tool_context.user_id)
        return service.guidance(question, journey_id)

    return _tool_call(call)


def record_application_inventory(
    journey_id: str,
    application_name: str,
    business_criticality: str,
    current_platform: str,
    environments: str,
    dependencies: str,
    data_classification: str,
    availability_requirement: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Record application discovery facts supplied by the owner in durable storage."""
    def call() -> dict[str, Any]:
        service = get_service()
        service.require_owner(journey_id, tool_context.user_id)
        return service.record_inventory(
            journey_id,
            application_name,
            business_criticality,
            current_platform,
            environments,
            dependencies,
            data_classification,
            availability_requirement,
        )

    return _tool_call(call)


def generate_cloud_plan(
    journey_id: str,
    target_platform: str,
    migration_objectives: str,
    constraints: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Generate a simulated Cloud plan from captured knowledge and submit it for review."""
    def call() -> dict[str, Any]:
        service = get_service()
        service.require_owner(journey_id, tool_context.user_id)
        return service.generate_plan(
            journey_id, target_platform, migration_objectives, constraints
        )

    return _tool_call(call)


def get_journey_status(
    journey_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Read the authoritative Journey state and complete audit history from the database."""
    def call() -> dict[str, Any]:
        service = get_service()
        service.require_owner(journey_id, tool_context.user_id)
        return service.status(journey_id)

    return _tool_call(call)


def get_journey_status_by_apm_id(
    apm_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Recover the current user's durable Journey by its globally unique APM ID."""
    return _tool_call(
        lambda: get_service().status_by_apm_id(apm_id, tool_context.user_id)
    )


def wait_for_external_approval(
    journey_id: str,
    tool_context: ToolContext,
    timeout_seconds: int = 120,
    poll_interval_seconds: int = 2,
) -> dict[str, Any]:
    """Wait briefly for an external backend to write APPROVED or REJECTED to PostgreSQL."""
    def call() -> dict[str, Any]:
        service = get_service()
        service.require_owner(journey_id, tool_context.user_id)
        return service.wait_for_approval(
            journey_id, timeout_seconds, poll_interval_seconds
        )

    return _tool_call(call)


def resume_journey_after_approval(
    journey_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Resume simulated execution only when PostgreSQL already contains APPROVED."""
    def call() -> dict[str, Any]:
        service = get_service()
        service.require_owner(journey_id, tool_context.user_id)
        return service.resume_after_approval(journey_id)

    return _tool_call(call)
