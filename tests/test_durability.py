from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cloud_journey.tools import JourneyService


def test_state_survives_engine_and_service_restart(engine, service) -> None:
    journey_id = service.start("300400", "sam")["journey_id"]
    service.continue_journey(journey_id)
    database_url = engine.url.render_as_string(hide_password=False)

    engine.dispose()
    restarted_engine = create_engine(
        database_url, connect_args={"check_same_thread": False, "timeout": 30}
    )
    restarted_factory = sessionmaker(
        bind=restarted_engine, expire_on_commit=False, class_=Session
    )
    restarted_service = JourneyService(restarted_factory)

    recovered = restarted_service.status(journey_id)
    assert recovered["current_state"] == "WAITING_FOR_APPROVAL"
    assert recovered["version"] == 7
    assert recovered["requested_by"] == "sam"
    restarted_engine.dispose()


def test_full_audit_history_distinguishes_user_and_agent(service) -> None:
    journey_id = service.start("400500", "sam")["journey_id"]
    service.continue_journey(journey_id)
    result = service.approve(journey_id, "sam")

    assert result["state_path"] == [
        "CREATED",
        "VALIDATING_APM",
        "APM_VALIDATED",
        "COLLECTING_INVENTORY",
        "INVENTORY_COMPLETE",
        "GENERATING_PLAN",
        "WAITING_FOR_APPROVAL",
        "APPROVED",
        "PROVISIONING",
        "VALIDATING_RESULT",
        "COMPLETED",
    ]
    approval = next(
        event for event in result["history"] if event["to_state"] == "APPROVED"
    )
    provisioning = next(
        event for event in result["history"] if event["to_state"] == "PROVISIONING"
    )
    assert (approval["actor_type"], approval["actor_id"]) == ("USER", "sam")
    assert (provisioning["actor_type"], provisioning["actor_id"]) == (
        "AGENT",
        "project-factory-agent",
    )
