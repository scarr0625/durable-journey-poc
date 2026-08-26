from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from cloud_journey.state_machine import ConcurrentTransition, InvalidTransition


def test_concurrent_approval_and_rejection_only_one_succeeds(service) -> None:
    journey_id = service.start("500600", "sam")["journey_id"]
    service.continue_journey(journey_id)

    def approve() -> str:
        try:
            service.record_external_approval(journey_id, "reviewer")
            return "approved"
        except (InvalidTransition, ConcurrentTransition):
            return "conflict"

    def reject() -> str:
        try:
            service.record_external_rejection(
                journey_id, "reviewer", "concurrent rejection"
            )
            return "rejected"
        except (InvalidTransition, ConcurrentTransition):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [executor.submit(approve), executor.submit(reject)]
        results = [future.result() for future in outcomes]

    assert results.count("conflict") == 1
    assert ("approved" in results) != ("rejected" in results)
    assert service.status(journey_id)["current_state"] in {"APPROVED", "REJECTED"}
