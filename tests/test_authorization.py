from __future__ import annotations

import pytest

from cloud_journey import tools
from cloud_journey.tools import AuthorizationDenied


def waiting_journey(service) -> str:
    journey_id = service.start("200300", "sam")["journey_id"]
    service.continue_journey(journey_id)
    return journey_id


def test_developer_cannot_approve_but_reviewer_can(service) -> None:
    journey_id = waiting_journey(service)

    with pytest.raises(AuthorizationDenied):
        service.approve(journey_id, "developer")

    denied_status = service.status(journey_id)
    assert denied_status["current_state"] == "WAITING_FOR_APPROVAL"
    assert denied_status["version"] == 7
    denied_event = denied_status["history"][-1]
    assert denied_event["event_type"] == "AUTHORIZATION_DENIED"
    assert denied_event["actor_id"] == "developer"

    assert service.approve(journey_id, "reviewer")["current_state"] == "COMPLETED"


def test_project_owner_cannot_approve_own_request(service) -> None:
    journey_id = waiting_journey(service)

    decision = service.check_approval_authorization(journey_id, "sam", "approve")
    assert decision["authorized"] is False
    assert decision["required_group"] == "CLOUD_JOURNEY_APPROVERS"
    assert "Project ownership does not grant approval authority" in decision["reason"]

    with pytest.raises(AuthorizationDenied):
        service.approve(journey_id, "sam")

    assert service.status(journey_id)["current_state"] == "WAITING_FOR_APPROVAL"


def test_adk_tool_returns_403_for_unauthorized_approval(service, monkeypatch) -> None:
    journey_id = waiting_journey(service)
    monkeypatch.setattr(tools, "get_service", lambda: service)

    result = tools.approve_journey(journey_id, "developer")

    assert result["ok"] is False
    assert result["status_code"] == 403
    assert result["error"] == "AuthorizationDenied"
    assert service.status(journey_id)["current_state"] == "WAITING_FOR_APPROVAL"


def test_authorization_check_tool_explains_group_decision(service, monkeypatch) -> None:
    journey_id = waiting_journey(service)
    monkeypatch.setattr(tools, "get_service", lambda: service)

    owner = tools.check_approval_authorization(journey_id, "sam", "approve")
    reviewer = tools.check_approval_authorization(
        journey_id, "reviewer", "approve"
    )

    assert owner["authorized"] is False
    assert owner["groups"] == []
    assert reviewer["authorized"] is True
    assert reviewer["groups"] == ["CLOUD_JOURNEY_APPROVERS"]


def test_reviewer_can_reject_and_reason_is_persisted(service) -> None:
    journey_id = waiting_journey(service)
    reason = "the generated plan is incorrect"

    result = service.reject(journey_id, "reviewer", reason)

    assert result["current_state"] == "REJECTED"
    event = result["history"][-1]
    assert event["from_state"] == "WAITING_FOR_APPROVAL"
    assert event["to_state"] == "REJECTED"
    assert event["actor_type"] == "USER"
    assert event["actor_id"] == "reviewer"
    assert event["message"] == reason
    assert event["metadata"]["rejection_reason"] == reason
