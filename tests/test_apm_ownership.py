from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from google.adk.tools import FunctionTool

from cloud_journey.agent import root_agent
from cloud_journey import tools
from cloud_journey.state_machine import DuplicateApmId, JourneyPersistenceError
from cloud_journey.tools import ApmAccessDenied


@dataclass(frozen=True)
class ToolContextStub:
    user_id: str
    state: dict[str, str] = field(default_factory=dict)


def test_apm_id_is_globally_unique_and_same_group_gets_existing(service) -> None:
    first = service.start("100401", "sam", "owner-subject-a")

    repeated = service.start("100401", "ivan", "owner-subject-b")

    assert first["created"] is True
    assert repeated["created"] is False
    assert repeated["journey_id"] == first["journey_id"]

    with pytest.raises(ApmAccessDenied):
        service.start("100401", "abdur", "owner-subject-c")

    # The database constraint is the final guard if application-level prechecks
    # race or are bypassed.
    with pytest.raises(DuplicateApmId):
        service.state_machine.create_journey(
            apm_id="100401",
            requested_by="sam",
            requested_by_email="sam@example.com",
            role="PROJECT_OWNER",
            access_group_id="GROUP_1",
            owner_subject="owner-subject-b",
        )


def test_non_duplicate_database_constraint_is_not_reported_as_apm_unavailable(
    service,
) -> None:
    with pytest.raises(JourneyPersistenceError):
        service.state_machine.create_journey(
            apm_id="999999",
            requested_by="sam",
            requested_by_email="sam@example.com",
            role="PROJECT_OWNER",
            access_group_id="GROUP_DOES_NOT_EXIST",
        )


def test_new_session_with_same_owner_can_recover_status_by_apm(
    service, monkeypatch
) -> None:
    monkeypatch.setattr(tools, "get_service", lambda: service)
    original_session = ToolContextStub("owner-subject-a")
    restarted_session = ToolContextStub("owner-subject-a")

    created = tools.start_journey("100401", "sam", original_session)
    service.continue_journey(created["journey_id"])

    # Recreate the service as well as the chat context to prove recovery does
    # not depend on an in-memory agent/session object.
    restarted_service = tools.JourneyService(service.session_factory)
    monkeypatch.setattr(tools, "get_service", lambda: restarted_service)

    tools.select_simulated_identity("ivan", restarted_session)
    recovered = tools.get_journey_status_by_apm_id("100401", restarted_session)

    assert recovered["ok"] is True
    assert recovered["journey_id"] == created["journey_id"]
    assert recovered["current_state"] == "WAITING_FOR_APPROVAL"
    assert recovered["lookup"] == "apm_id"


def test_different_group_cannot_discover_apm_or_journey_details(
    service, monkeypatch
) -> None:
    monkeypatch.setattr(tools, "get_service", lambda: service)
    owner = ToolContextStub("owner-subject-a")
    other_user = ToolContextStub("owner-subject-b")
    created = tools.start_journey("100401", "sam", owner)
    tools.select_simulated_identity("abdur", other_user)

    by_apm = tools.get_journey_status_by_apm_id("100401", other_user)
    missing_apm = tools.get_journey_status_by_apm_id("does-not-exist", other_user)
    by_journey_id = tools.get_journey_status(created["journey_id"], other_user)
    missing_journey = tools.get_journey_status("J-DOES-NOT-EXIST", other_user)

    assert by_apm["status_code"] == 403
    assert by_journey_id["status_code"] == 404
    assert by_apm["message"] == missing_apm["message"]
    assert by_journey_id["message"] == missing_journey["message"]
    assert "100401" not in str(by_apm)
    assert created["journey_id"] not in str(by_journey_id)


def test_non_project_owner_cannot_start_journey(service, monkeypatch) -> None:
    monkeypatch.setattr(tools, "get_service", lambda: service)

    result = tools.start_journey(
        "100401", "developer", ToolContextStub("developer-subject")
    )

    assert result["status_code"] == 403
    assert result["error"] == "ProjectOwnerRequired"


def test_runtime_owner_identity_is_not_exposed_to_the_model_schema() -> None:
    for tool in root_agent.tools:
        declaration = FunctionTool(tool)._get_declaration()
        properties = declaration.parameters_json_schema["properties"]
        assert "tool_context" not in properties
        assert "owner_subject" not in properties
