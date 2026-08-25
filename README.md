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
- Human approval boundary with PROJECT_OWNER / REVIEWER authorization
- `journey_operations` records for command outcomes and future idempotency/retry work
- Five Google ADK tools and a `root_agent` suitable for ADK Web
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

## Run ADK Web

From the repository root, run:

```powershell
adk web .
```

Then select `cloud_journey` in the web interface.

Suggested demo conversation:

```text
Start a Cloud Journey for APM 100200 as sam.
Continue the journey.
Approve the journey as developer.
Show status for the journey.
Approve the journey as sam.
Show the full history for the journey.
```

To demonstrate durability, stop ADK Web after the continue command, restart it,
open a new chat, and ask `Show status for journey J-XXXXXXXX`. The expected state
is `WAITING_FOR_APPROVAL`.

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
