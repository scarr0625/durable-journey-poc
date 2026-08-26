"""Simulated identity and approval policy for the PoC.

The policy is deliberately independent of ADK and the state machine so the
simulated identities can later be replaced by validated OAuth/SSO claims.
"""

from __future__ import annotations

from dataclasses import dataclass

APPROVAL_GROUP = "CLOUD_JOURNEY_APPROVERS"
SUPPORTED_APPROVAL_ACTIONS = frozenset({"approve", "reject"})


@dataclass(frozen=True)
class SimulatedUser:
    name: str
    email: str
    role: str
    groups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    action: str
    user_name: str
    role: str
    groups: frozenset[str]
    required_group: str
    reason: str


SIMULATED_USERS: dict[str, SimulatedUser] = {
    # The requester/owner deliberately does not belong to the approval group.
    "sam": SimulatedUser("sam", "sam@example.com", "PROJECT_OWNER"),
    "reviewer": SimulatedUser(
        "reviewer",
        "reviewer@example.com",
        "REVIEWER",
        frozenset({APPROVAL_GROUP}),
    ),
    "developer": SimulatedUser("developer", "developer@example.com", "DEVELOPER"),
}


def get_simulated_user(user_name: str) -> SimulatedUser | None:
    return SIMULATED_USERS.get(user_name.strip().lower())


def evaluate_approval_authorization(
    *,
    user: SimulatedUser,
    action: str,
    requested_by: str,
) -> AuthorizationDecision:
    """Evaluate group membership and segregation-of-duties constraints."""
    normalized_action = action.strip().lower()
    if normalized_action not in SUPPORTED_APPROVAL_ACTIONS:
        raise ValueError("action must be 'approve' or 'reject'")

    if APPROVAL_GROUP not in user.groups:
        reason = (
            f"{user.name} is not a member of the required AD group "
            f"{APPROVAL_GROUP}. Project ownership does not grant approval authority."
        )
        allowed = False
    elif user.name == requested_by:
        reason = (
            f"{user.name} requested this Journey and cannot {normalized_action} "
            "their own request (separation of duties)."
        )
        allowed = False
    else:
        reason = (
            f"{user.name} may {normalized_action} because they belong to "
            f"{APPROVAL_GROUP} and did not request this Journey."
        )
        allowed = True

    return AuthorizationDecision(
        allowed=allowed,
        action=normalized_action,
        user_name=user.name,
        role=user.role,
        groups=user.groups,
        required_group=APPROVAL_GROUP,
        reason=reason,
    )
