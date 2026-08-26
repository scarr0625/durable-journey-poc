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
- Seven purpose-specific Google ADK tools and a `root_agent` suitable for ADK Web
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

## Approval boundary

Cloud Compass has no approve or reject control. It creates knowledge and plans,
then stops at `WAITING_FOR_APPROVAL`. A separate approval backend owns the human
decision and writes `APPROVED` or `REJECTED` to PostgreSQL.

The local simulator models that backend with these identities:

| User | Business role | Simulated AD groups | Cloud Compass access | Approval-backend access |
|---|---|---|---|---|
| `sam` | `PROJECT_OWNER` | none | Requester | None |
| `reviewer` | `REVIEWER` | `CLOUD_JOURNEY_APPROVERS` | Status only | Approve/reject others' requests |
| `developer` | `DEVELOPER` | none | Knowledge/status | None |

Cloud Compass can only poll the database and observe the backend decision. It can
resume provisioning after it reads `APPROVED`; it cannot create that state. The
simulator waits 60 seconds by default. A real seven-day approval must use an
event/callback or durable workflow timer rather than keeping an ADK request open.

## ADK tools

| Tool | Purpose | Changes Journey state |
|---|---|---:|
| `start_journey(apm_id, user_name)` | Create the durable Journey and simulate APM validation | Yes |
| `get_cloud_journey_guidance(question, journey_id)` | Answer using persisted Journey context and identify missing discovery facts | No |
| `record_application_inventory(...)` | Persist application, platform, dependency, data, and availability knowledge | Yes |
| `generate_cloud_plan(...)` | Persist a proposed target plan and submit it for independent review | Yes |
| `wait_for_external_approval(journey_id, timeout_seconds, poll_interval_seconds)` | Poll PostgreSQL for an external decision for up to two minutes | No |
| `resume_journey_after_approval(journey_id)` | Continue simulated execution only if PostgreSQL already says `APPROVED` | Yes |
| `get_journey_status(journey_id)` | Read current state, version, requester, and complete audit history | No |

Neither approval nor rejection is registered as an ADK tool. The backend-only
simulator is a separate module, `cloud_journey.approval_backend`. All state changes
still pass through the central state machine. The original `continue_journey`
function remains a compatibility API for the initial PoC contract, but Cloud
Compass cannot call it.

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

### Demo 1: knowledge discovery, external approval, and automatic resume

```text
Before I start, explain what Cloud Compass can help me with during an application cloud journey.

Start a Cloud Journey for APM 100200 as sam.

What do you need to know about this application before recommending a cloud plan?

The application is Customer Orders API. It is a Tier 1 business service currently
running on on-premises VMware. It has development, test, and production environments.
Its dependencies are PostgreSQL, Active Directory, and an external payment gateway.
It processes confidential customer data and requires 99.95% availability with
disaster recovery.

Based on this inventory, explain reasonable migration options and the tradeoffs
between GKE, Cloud Run, and a VM-based migration.

Create a proposed plan targeting Google Kubernetes Engine with Cloud SQL for
PostgreSQL. The objectives are improved resilience and reduced infrastructure
operations. Constraints are no more than 15 minutes of cutover downtime and all
application traffic must use private connectivity.

Show me the proposed plan and current Journey status.

Who is responsible for approving this Journey, and can approval happen here?
```

Expected checkpoints:

- Cloud Compass begins as a knowledge assistant, not as a workflow command menu.
- `sam` creates the durable request as `PROJECT_OWNER`.
- The discovery question lists the application facts still needed.
- The owner-provided inventory is persisted before any plan is generated.
- Cloud Compass explains options using the captured inventory.
- Plan generation produces `COLLECTING_INVENTORY -> INVENTORY_COMPLETE ->
  GENERATING_PLAN -> WAITING_FOR_APPROVAL`, version 7.
- Cloud Compass explains that the external approval backend owns the decision and
  no approval action is available in chat.

In a second terminal, mimic the approval backend. Replace the ID and use 60–120
seconds for the visible demo:

```powershell
python -m cloud_journey.approval_backend J-XXXXXXXX --decision approve --reviewer reviewer --delay-seconds 60
```

Immediately return to Cloud Compass and send:

```text
Wait up to two minutes for the external approval decision. If the database says
APPROVED, resume the Journey and show me the completed transition history.
```

Cloud Compass polls without changing state. After the backend writes `APPROVED`,
it observes that value and invokes the separate resume tool:

```text
WAITING_FOR_APPROVAL
→ APPROVED                  Actor: APPROVAL_BACKEND / reviewer
→ PROVISIONING              Actor: AGENT / project-factory-agent
→ VALIDATING_RESULT         Actor: AGENT / project-factory-agent
→ COMPLETED                 Actor: AGENT / project-factory-agent
```

### Demo 2: independent reviewer rejects with a persisted reason

```text
Start a Cloud Journey for APM 200300 as sam.

The application is Partner Portal. It is a Tier 2 service on Windows VMs with
development and production environments. Dependencies are SQL Server, corporate
Active Directory, and an SMTP relay. It contains internal confidential data and
requires 99.9% availability.

Create a proposed plan targeting Compute Engine. The objective is a low-change
migration. Constraints are private connectivity and the existing Windows runtime.

Who is responsible for the decision? Confirm that I cannot reject it from Cloud Compass.
```

In the separate backend terminal:

```powershell
python -m cloud_journey.approval_backend J-XXXXXXXX --decision reject --reviewer reviewer --delay-seconds 60 --reason "Network firewall design is incomplete"
```

Then ask Cloud Compass:

```text
Wait up to two minutes for the external decision and show the current status.
```

Expected final state: `REJECTED`. Cloud Compass observes it and does not resume.

### Demo 3: restart proves durable recovery

```text
Start a Cloud Journey for APM 300400 as sam.

The application is Reporting Service. It is Tier 2, runs on Linux VMs, has test
and production environments, depends on PostgreSQL and SFTP, contains internal
data, and requires 99.9% availability.

Create a proposed plan targeting Cloud Run with Cloud SQL. The objective is to
reduce operations effort. Constraints are private database access and a phased cutover.
```

Record the returned ID, stop ADK Web, restart it, open a new conversation, then
send:

```text
Show status for journey J-XXXXXXXX.

Wait up to two minutes for an external approval of journey J-XXXXXXXX. If it is
approved, resume execution.
```

The first response after restart must load `WAITING_FOR_APPROVAL` from PostgreSQL.
Run the backend simulator in another terminal; Cloud Compass then detects its
database update and resumes without any approval action in the chat UI.

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
