from __future__ import annotations

import pytest

from cloud_journey.state_machine import (
    Actor,
    InvalidTransition,
    JourneyState,
    StateMachine,
)


def test_normal_journey_stops_for_approval_then_completes(service) -> None:
    started = service.start("100401", "sam")
    journey_id = started["journey_id"]

    assert started["state_path"] == [
        "CREATED",
        "VALIDATING_APM",
        "APM_VALIDATED",
    ]
    guidance = service.guidance(
        "What do you need to know about this application?", journey_id
    )
    assert guidance["current_state"] == "APM_VALIDATED"
    assert "dependencies" in guidance["information_needed"]

    inventory = service.record_inventory(
        journey_id,
        "Customer Orders API",
        "Tier 1",
        "On-premises VMware",
        "development, test, and production",
        "PostgreSQL, Active Directory, and an external payment gateway",
        "confidential customer data",
        "99.95% availability with disaster recovery",
    )
    assert inventory["current_state"] == "INVENTORY_COMPLETE"
    assert inventory["context"]["inventory"]["application_name"] == "Customer Orders API"

    waiting = service.generate_plan(
        journey_id,
        "Google Kubernetes Engine with Cloud SQL for PostgreSQL",
        "improve resilience and reduce infrastructure operations",
        "no more than 15 minutes of cutover downtime; retain private connectivity",
    )
    assert waiting["current_state"] == "WAITING_FOR_APPROVAL"
    assert waiting["version"] == 7
    assert waiting["context"]["proposed_plan"]["provisioning"] == "simulated"

    external_decision = service.record_external_approval(journey_id, "reviewer")
    assert external_decision["current_state"] == "APPROVED"
    assert external_decision["version"] == 8

    completed = service.resume_after_approval(journey_id)
    assert completed["current_state"] == "COMPLETED"
    assert completed["version"] == 11
    assert completed["state_path"][-4:] == [
        "APPROVED",
        "PROVISIONING",
        "VALIDATING_RESULT",
        "COMPLETED",
    ]


def test_invalid_transition_does_not_update_database(session_factory) -> None:
    machine = StateMachine(session_factory)
    journey = machine.create_journey(
        apm_id="123456",
        requested_by="sam",
        requested_by_email="sam@example.com",
        role="PROJECT_OWNER",
    )

    with pytest.raises(InvalidTransition) as error:
        machine.transition(
            journey.id,
            JourneyState.COMPLETED,
            actor=Actor("AGENT", "test-agent"),
        )

    assert "CREATED -> COMPLETED" in str(error.value)
    unchanged = machine.get_journey(journey.id)
    assert unchanged.status == "CREATED"
    assert unchanged.version == 1
    assert len(machine.get_events(journey.id)) == 1


def test_processing_state_can_fail_and_enter_retrying(session_factory) -> None:
    machine = StateMachine(session_factory)
    journey = machine.create_journey(
        apm_id="123456",
        requested_by="sam",
        requested_by_email="sam@example.com",
        role="PROJECT_OWNER",
    )
    machine.transition(
        journey.id,
        JourneyState.VALIDATING_APM,
        actor=Actor("AGENT", "test-agent"),
    )
    machine.transition(
        journey.id,
        JourneyState.FAILED,
        actor=Actor("AGENT", "test-agent"),
        message="APM service unavailable",
        last_error="APM service unavailable",
    )

    failed = machine.get_journey(journey.id)
    assert failed.last_error == "APM service unavailable"
    machine.transition(
        journey.id,
        JourneyState.RETRYING,
        actor=Actor("AGENT", "test-agent"),
    )
    machine.transition(
        journey.id,
        JourneyState.VALIDATING_APM,
        actor=Actor("AGENT", "test-agent"),
    )
    assert machine.get_journey(journey.id).status == "VALIDATING_APM"
