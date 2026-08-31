"""Simulated identity and approval policy for the PoC.

The policy is deliberately independent of ADK and the state machine so the
simulated identities can later be replaced by validated OAuth/SSO claims.
"""

from __future__ import annotations

from dataclasses import dataclass

APPROVAL_GROUP = "CLOUD_JOURNEY_APPROVERS"
APM_GROUP_1 = "GROUP_1"
APM_GROUP_2 = "GROUP_2"
SUPPORTED_APPROVAL_ACTIONS = frozenset({"approve", "reject"})

# Demo data for the unauthenticated PoC. The database table remains the source
# of truth for APM-to-group access; these rows only bootstrap a fresh database.
DEFAULT_APM_GROUP_ACCESS: dict[str, frozenset[str]] = {
    APM_GROUP_1: frozenset({"100401", "100402"}),
    APM_GROUP_2: frozenset({"100403", "100404"}),
}


@dataclass(frozen=True)
class SimulatedUser:
    name: str
    email: str
    role: str
    groups: frozenset[str] = frozenset()
    apm_group: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    action: str
    user_name: str
    role: str
    groups: frozenset[str]
    required_group: str
    reason: str


@dataclass(frozen=True)
class ApmAuthorizationDecision:
    allowed: bool
    apm_id: str
    user_name: str
    user_group: str | None
    reason: str


SIMULATED_USERS: dict[str, SimulatedUser] = {
    # These identities make group authorization testable before SSO is added.
    "sam": SimulatedUser(
        "sam",
        "sam@example.com",
        "PROJECT_OWNER",
        frozenset({APM_GROUP_1}),
        APM_GROUP_1,
    ),
    "ivan": SimulatedUser(
        "ivan",
        "ivan@example.com",
        "PROJECT_OWNER",
        frozenset({APM_GROUP_1}),
        APM_GROUP_1,
    ),
    "adi": SimulatedUser(
        "adi",
        "adi@example.com",
        "PROJECT_OWNER",
        frozenset({APM_GROUP_1}),
        APM_GROUP_1,
    ),
    "abdur": SimulatedUser(
        "abdur",
        "abdur@example.com",
        "PROJECT_OWNER",
        frozenset({APM_GROUP_2}),
        APM_GROUP_2,
    ),
    "ajir": SimulatedUser(
        "ajir",
        "ajir@example.com",
        "PROJECT_OWNER",
        frozenset({APM_GROUP_2}),
        APM_GROUP_2,
    ),
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


def evaluate_apm_authorization(
    *,
    user: SimulatedUser,
    apm_id: str,
    required_group: str | None,
) -> ApmAuthorizationDecision:
    """Evaluate the database-backed APM mapping without disclosing other groups."""
    allowed = required_group is not None and user.apm_group == required_group
    if allowed:
        reason = f"{user.name} may access the requested APM ID through {user.apm_group}."
    else:
        # Use one message for unmapped and cross-group IDs to avoid an APM oracle.
        reason = "The simulated user is not authorized to access the requested APM ID."
    return ApmAuthorizationDecision(
        allowed=allowed,
        apm_id=apm_id,
        user_name=user.name,
        user_group=user.apm_group,
        reason=reason,
    )


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
