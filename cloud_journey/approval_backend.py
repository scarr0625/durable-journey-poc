"""Separate delayed approval-backend simulator for local demonstrations.

This module is intentionally not registered as a Cloud Compass ADK tool.
"""

from __future__ import annotations

import argparse
import json
import time

from cloud_journey.tools import get_service


def simulate_backend_decision(
    journey_id: str,
    *,
    decision: str = "approve",
    reviewer_name: str = "reviewer",
    delay_seconds: float = 60,
    reason: str = "",
) -> dict[str, object]:
    """Sleep, then mimic an external approval system writing its decision."""
    if delay_seconds < 0:
        raise ValueError("delay_seconds must not be negative")
    normalized = decision.strip().lower()
    if normalized not in {"approve", "reject"}:
        raise ValueError("decision must be 'approve' or 'reject'")
    time.sleep(delay_seconds)
    service = get_service()
    if normalized == "approve":
        return service.record_external_approval(journey_id, reviewer_name)
    return service.record_external_rejection(
        journey_id,
        reviewer_name,
        reason or "Rejected by the external approval backend",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate a delayed decision from the external approval backend"
    )
    parser.add_argument("journey_id")
    parser.add_argument("--decision", choices=("approve", "reject"), default="approve")
    parser.add_argument("--reviewer", default="reviewer")
    parser.add_argument("--delay-seconds", type=float, default=60)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    result = simulate_backend_decision(
        args.journey_id,
        decision=args.decision,
        reviewer_name=args.reviewer,
        delay_seconds=args.delay_seconds,
        reason=args.reason,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
