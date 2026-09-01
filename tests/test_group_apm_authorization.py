from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete

from cloud_journey import tools
from cloud_journey.models import AccessGroupMember
from cloud_journey.tools import ApmAccessDenied


@dataclass
class ToolContextStub:
    user_id: str
    state: dict[str, str] = field(default_factory=dict)


@pytest.mark.parametrize(
    ("user_name", "allowed_apm", "denied_apm"),
    [
        ("sam", "100401", "100403"),
        ("ivan", "100402", "100404"),
        ("adi", "100401", "100403"),
        ("abdur", "100403", "100401"),
        ("ajir", "100404", "100402"),
    ],
)
def test_start_enforces_group_to_apm_mapping(
    service, user_name: str, allowed_apm: str, denied_apm: str
) -> None:
    assert service.start(allowed_apm, user_name)["created"] is True

    with pytest.raises(ApmAccessDenied) as exc_info:
        service.start(denied_apm, user_name)

    assert denied_apm not in str(exc_info.value)


def test_same_group_member_can_read_existing_journey(service, monkeypatch) -> None:
    monkeypatch.setattr(tools, "get_service", lambda: service)
    sam_session = ToolContextStub("runtime-subject-sam")
    ivan_session = ToolContextStub("runtime-subject-ivan")

    created = tools.start_journey("100401", "sam", sam_session)
    identity = tools.select_simulated_identity("ivan", ivan_session)
    updated = tools.record_application_inventory(
        created["journey_id"],
        "Shared application",
        "Tier 2",
        "VMware",
        "test and production",
        "PostgreSQL",
        "internal",
        "99.9%",
        ivan_session,
    )
    status = tools.get_journey_status_by_apm_id("100401", ivan_session)

    assert identity["apm_group"] == "GROUP_1"
    assert identity["available_apm_ids"] == ["100401", "100402"]
    assert updated["current_state"] == "INVENTORY_COMPLETE"
    assert status["ok"] is True
    assert status["journey_id"] == created["journey_id"]
    assert status["access_group_id"] == "GROUP_1"
    inventory_event = next(
        event for event in status["history"] if event["event_type"] == "CONTEXT_UPDATED"
    )
    assert inventory_event["actor_id"] == "ivan"


def test_session_identity_is_required_and_cannot_be_switched(
    service, monkeypatch
) -> None:
    monkeypatch.setattr(tools, "get_service", lambda: service)
    session = ToolContextStub("anonymous-runtime-subject")

    missing = tools.get_journey_status_by_apm_id("100401", session)
    selected = tools.select_simulated_identity("sam", session)
    switched = tools.select_simulated_identity("abdur", session)

    assert missing["status_code"] == 401
    assert missing["error"] == "SimulatedIdentityRequired"
    assert selected["user_name"] == "sam"
    assert switched["status_code"] == 403
    assert switched["error"] == "SimulatedIdentityConflict"


def test_unmapped_and_cross_group_apm_have_same_denial(service, monkeypatch) -> None:
    monkeypatch.setattr(tools, "get_service", lambda: service)
    session = ToolContextStub("runtime-subject-abdur")
    tools.select_simulated_identity("abdur", session)

    cross_group = tools.get_journey_status_by_apm_id("100401", session)
    unmapped = tools.get_journey_status_by_apm_id("999999", session)

    assert cross_group["status_code"] == 403
    assert cross_group["message"] == unmapped["message"]
    assert "100401" not in str(cross_group)
    assert "999999" not in str(unmapped)


def test_database_membership_is_the_authorization_source(service) -> None:
    with service.session_factory.begin() as session:
        session.execute(
            delete(AccessGroupMember).where(
                AccessGroupMember.group_id == "GROUP_1",
                AccessGroupMember.user_subject == "sam",
            )
        )

    with pytest.raises(ApmAccessDenied):
        service.start("100401", "sam")
