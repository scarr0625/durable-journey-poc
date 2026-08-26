from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cloud_journey import approval_backend
from cloud_journey.agent import root_agent
from cloud_journey.state_machine import InvalidTransition


def waiting_journey(service) -> str:
    journey_id = service.start("700800", "sam")["journey_id"]
    service.continue_journey(journey_id)
    return journey_id


def test_cloud_compass_exposes_no_decision_tool() -> None:
    tool_names = {tool.__name__ for tool in root_agent.tools}

    assert "approve_journey" not in tool_names
    assert "reject_journey" not in tool_names
    assert "check_approval_authorization" not in tool_names
    assert "wait_for_external_approval" in tool_names
    assert "resume_journey_after_approval" in tool_names


def test_wait_does_not_approve_or_change_state(service) -> None:
    journey_id = waiting_journey(service)

    result = service.wait_for_approval(
        journey_id, timeout_seconds=0, poll_interval_seconds=1
    )

    assert result["timed_out"] is True
    assert result["approval_observed"] is False
    assert result["current_state"] == "WAITING_FOR_APPROVAL"
    assert result["version"] == 7


def test_external_backend_approves_then_cloud_compass_resumes(service) -> None:
    journey_id = waiting_journey(service)

    with ThreadPoolExecutor(max_workers=2) as executor:
        waiting = executor.submit(
            service.wait_for_approval,
            journey_id,
            2,
            1,
        )
        backend = executor.submit(
            service.record_external_approval, journey_id, "reviewer"
        )
        backend_result = backend.result()
        observed = waiting.result()

    assert backend_result["current_state"] == "APPROVED"
    assert observed["approval_observed"] is True
    assert observed["current_state"] == "APPROVED"
    approval_event = next(
        event for event in observed["history"] if event["to_state"] == "APPROVED"
    )
    assert approval_event["actor_type"] == "APPROVAL_BACKEND"
    assert approval_event["actor_id"] == "reviewer"

    completed = service.resume_after_approval(journey_id)
    assert completed["current_state"] == "COMPLETED"


def test_cloud_compass_cannot_resume_before_external_approval(service) -> None:
    journey_id = waiting_journey(service)

    with pytest.raises(InvalidTransition):
        service.resume_after_approval(journey_id)

    assert service.status(journey_id)["current_state"] == "WAITING_FOR_APPROVAL"


def test_delayed_backend_simulator_is_separate_from_agent(
    service, monkeypatch
) -> None:
    journey_id = waiting_journey(service)
    monkeypatch.setattr(approval_backend, "get_service", lambda: service)

    result = approval_backend.simulate_backend_decision(
        journey_id,
        reviewer_name="reviewer",
        delay_seconds=0.01,
    )

    assert result["current_state"] == "APPROVED"
