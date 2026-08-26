# Durable Cloud Journey PoC

A local-first Google ADK agent backed by PostgreSQL for authoritative business
state. Chat sessions and the ADK process are intentionally disposable: every
status response is rebuilt from `journeys` and the append-only `journey_events`
table.

## What is implemented

- Central transition validation in `cloud_journey/state_machine.py`
- PostgreSQL `SELECT ... FOR UPDATE` plus a version-guarded update
- One transaction per transition, numeric versions, and complete actor-aware audit history
- Resumable simulated inventory, planning, provisioning, and validation stages
- Segregated approval boundary based on `CLOUD_JOURNEY_APPROVERS` membership
- `journey_operations` records for command outcomes and future idempotency/retry work
- Six Google ADK tools and a `root_agent` suitable for ADK Web
- Self-contained unit/acceptance tests, including process restart and conflicting actions

No real cloud resources are provisioned and no OAuth tokens are stored.

## Prerequisites

- Python 3.11 or newer
- PostgreSQL 14+ (PostgreSQL 16 is provided in `compose.yaml` for local demos)
- A Gemini API key, or Vertex AI application credentials

## Local setup

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
docker compose up -d postgres
```

For the included container, set this value in `.env`:

```text
DATABASE_URL=postgresql+psycopg://journey:journey@127.0.0.1:5432/durable_journey
```

For Cloud SQL, run the Cloud SQL Auth Proxy (or use a private-IP connection) and
point `DATABASE_URL` at its PostgreSQL endpoint. The application creates the PoC
tables on the first tool call. In production, schema migration tooling should
replace this convenience behavior.

Set either `GOOGLE_API_KEY` for Google AI Studio or the Vertex AI variables shown
in `.env.example`.

## Run tests

```powershell
pytest
```

Tests use a temporary SQLite database so they need no external service. Runtime
configuration defaults to PostgreSQL, and the same transition code executes
`SELECT ... FOR UPDATE`; the optimistic version predicate adds protection on
backends that do not implement row locks.

## Simulated identities and separation of duties

| User | Business role | Simulated AD groups | May request | May approve/reject |
|---|---|---|---:|---:|
| `sam` | `PROJECT_OWNER` | none | Yes | No |
| `reviewer` | `REVIEWER` | `CLOUD_JOURNEY_APPROVERS` | Yes | Yes, except own requests |
| `developer` | `DEVELOPER` | none | Yes | No |

Project ownership does not imply approval authority. Approval and rejection
require membership in the separate `CLOUD_JOURNEY_APPROVERS` group, and the
requester cannot approve or reject their own request. This models an enterprise
AD-group decision without connecting to real Active Directory. The simulated
identity provider is isolated in `cloud_journey/authorization.py` so validated
SSO/OAuth claims can replace it later.

The read-only authorization-check tool explains a decision, but it is not a
security prerequisite. Both mutation tools evaluate the policy again before
changing state, so an LLM cannot bypass authorization by skipping the check.

## ADK tools

| Tool | Purpose | Changes Journey state |
|---|---|---:|
| `start_journey(apm_id, user_name)` | Create the durable Journey and simulate APM validation | Yes |
| `continue_journey(journey_id)` | Collect inventory and generate a plan, stopping at approval | Yes |
| `check_approval_authorization(journey_id, user_name, action)` | Explain whether `approve` or `reject` is allowed | No |
| `approve_journey(journey_id, user_name)` | Enforce approval policy and simulate provisioning through completion | Yes |
| `reject_journey(journey_id, user_name, reason)` | Enforce approval policy and persist the rejection reason | Yes |
| `get_journey_status(journey_id)` | Read current state, version, requester, and complete audit history | No |

All mutation tools delegate state changes to the central state machine. The ADK
agent and authorization layer cannot update Journey status directly.

## Run ADK Web

From the repository root, run:

```powershell
adk web .
```

Then select `cloud_journey` in the web interface.

## Practical demo conversation

Use the messages below in order. Replace `J-XXXXXXXX` with the Journey ID returned
by the first message when starting a new chat; within one chat, “the journey”
should resolve to the prior tool result.

### Demo 1: owner requests, owner and developer are denied, reviewer approves

```text
Start a Cloud Journey for APM 100200 as sam.

Continue the journey.

Can sam approve the journey?

Approve the journey as sam.

Show status for the journey.

Can developer approve the journey?

Approve the journey as developer.

Can reviewer approve the journey?

Approve the journey as reviewer.

Show the full history for the journey.
```

Expected checkpoints:

- `sam` creates the request as `PROJECT_OWNER`.
- Execution stops at `WAITING_FOR_APPROVAL`, version 7.
- Authorization checks for `sam` and `developer` return `authorized: false`.
- Their approval attempts return `403 / AuthorizationDenied`; state remains
  `WAITING_FOR_APPROVAL` and denial events appear in history.
- `reviewer` returns `authorized: true` due to approval-group membership.
- Reviewer approval produces `APPROVED -> PROVISIONING -> VALIDATING_RESULT -> COMPLETED`.

### Demo 2: independent reviewer rejects with a persisted reason

```text
Start a Cloud Journey for APM 200300 as sam.

Continue the journey.

Reject the journey as reviewer because the network firewall design is incomplete.

Show status for the journey.

Show the full history for the journey.
```

Expected final state: `REJECTED`. The history shows `reviewer` as the user actor
and preserves the complete rejection reason.

### Demo 3: restart proves durable recovery

```text
Start a Cloud Journey for APM 300400 as sam.

Continue the journey.
```

Record the returned ID, stop ADK Web, restart it, open a new conversation, then
send:

```text
Show status for journey J-XXXXXXXX.

Can reviewer approve journey J-XXXXXXXX?

Approve journey J-XXXXXXXX as reviewer.
```

The first response after restart must load `WAITING_FOR_APPROVAL` from PostgreSQL,
and the reviewer can then complete it.

## Data and transaction behavior

`journeys` is the current-state projection. `journey_events` is append-only and
includes the creation, every transition, denial events, actor type, actor ID, and
metadata such as rejection reasons. `journey_operations` tracks RUNNING,
COMPLETED, or FAILED command execution.

Every transition follows this sequence inside one database transaction:

1. Select the Journey row `FOR UPDATE`.
2. Validate the requested edge against the central transition map.
3. Update only when the stored state and version still match.
4. Increment the version and append the audit event.
5. Commit both changes together.

This makes concurrent approval and rejection mutually exclusive: once one
transaction commits, the other sees a changed state/version and fails.
