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
- Globally unique APM IDs enforced by the database
- Database-backed group-to-APM authorization with session-bound simulated users
- Nine purpose-specific Google ADK tools and a `root_agent` suitable for ADK Web
- Self-contained unit/acceptance tests, including process restart and conflicting actions

No real cloud resources are provisioned and no OAuth tokens are stored.

For a fully managed GCP deployment using Agent Runtime, Agent Registry Playground,
and the existing Cloud SQL database, follow [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md).

## Prerequisites

- Python 3.11 or newer
- A Cloud SQL for PostgreSQL 14+ instance
- Cloud SQL Auth Proxy v2 and the PostgreSQL `psql` client
- A Gemini API key, or Vertex AI application credentials

## Local setup

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Start the Cloud SQL Auth Proxy in a separate PowerShell terminal:

```powershell
.\cloud-sql-proxy.exe --port 5432 PROJECT_ID:REGION:INSTANCE_NAME
```

Set the database connection in `.env`, using the password for the Cloud SQL
application database user:

```text
DATABASE_URL=postgresql+psycopg://journey:URL_ENCODED_PASSWORD@127.0.0.1:5432/durable_journey
```

### Recreate the Cloud SQL database from scratch

The repository includes a baseline migration followed by the two incremental
migrations. Stop any running agent that uses this database first. With the Cloud
SQL Auth Proxy listening on `127.0.0.1:5432`, permanently delete and recreate
only the `durable_journey` database by running:

```powershell
.\scripts\recreate_cloud_sql_database.ps1 `
    -AdminUser postgres `
    -ApplicationUser journey `
    -DatabaseName durable_journey
```

`postgres` must be a Cloud SQL database administrator that can drop/create
databases, and the `journey` database user must already exist on the Cloud SQL
instance. `psql` prompts for their database passwords. The script verifies the
proxy connection and requires typing `durable_journey` before deletion.

For non-interactive disposable environments only, add `-Force`:

```powershell
.\scripts\recreate_cloud_sql_database.ps1 `
    -AdminUser postgres `
    -ApplicationUser journey `
    -DatabaseName durable_journey `
    -Force
```

The migration order is:

1. `000_initial_schema.sql` — creates the Journey and normalized group-access
   tables.
2. `001_apm_uniqueness_and_ownership.sql` — safely preserves the previous
   upgrade path and database uniqueness boundary.
3. `002_group_apm_authorization.sql` — creates and seeds `access_groups`,
   `access_group_members`, and `apm_group_assignments`, then assigns each
   Journey an `access_group_id`.

After recreation, start the agent:

```powershell
adk web .
```

Then test an allowed request with `Start a journey with APM ID 100401, as sam.`
To test denial, open a different ADK session and try APM `100403` as `sam`.

The Auth Proxy exposes the local TCP endpoint; it does not create a local Docker
database. The application can create missing PoC tables on its first tool call,
but the migration sequence above is the reproducible setup path.

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

## Simulated group authorization and privacy boundary

An APM ID identifies one Journey globally, not one Journey per chat session. The
database has a unique constraint on `journeys.apm_id`, so two simultaneous agent
requests cannot create separate Journeys for `100401`.

This PoC intentionally has no authentication provider. `sam`, `ivan`, `adi`,
`abdur`, and `ajir` are simulated identities. The first identity selection or
`start_journey` call binds one simulated identity to ADK session state; switching
identity inside that session is rejected. This makes authorization behavior
testable, but a user can still open a new session and claim another name, so it
must not be treated as production security.

The normalized authorization tables are the source of truth:

- `access_groups` defines groups.
- `access_group_members` maps simulated users to groups.
- `apm_group_assignments` maps APM IDs to groups.
- `journeys.access_group_id` records the owning group durably.

Fresh PoC databases are seeded without overwriting existing rows:

| Simulated group | Users | Available APM IDs |
|---|---|---|
| `GROUP_1` | `sam`, `ivan`, `adi` | `100401`, `100402` |
| `GROUP_2` | `abdur`, `ajir` | `100403`, `100404` |

`journeys.owner_subject` remains an audit field containing ADK's runtime
`ToolContext.user_id`; it is no longer the Journey access boundary. Every ADK
read or change checks `journeys.access_group_id` against the simulated user's
database membership.

This gives the intended behavior:

- A same-group member can select their simulated identity in a new session and
  recover the Journey with its APM ID.
- Starting the same APM ID again as a same-group member returns the existing durable
  Journey instead of creating another row.
- A different-group user receives the same denial for a cross-group APM ID as for
  an unmapped APM ID. No Journey ID,
  requester, status, history, or plan is returned.
- The database uniqueness constraint remains the final duplicate guard during
  concurrent requests.

For an existing database, run
[`migrations/001_apm_uniqueness_and_ownership.sql`](migrations/001_apm_uniqueness_and_ownership.sql),
then [`migrations/002_group_apm_authorization.sql`](migrations/002_group_apm_authorization.sql)
before starting this version. The legacy `apm_group_access` table is no longer
read or seeded by the application.

## Approval boundary

Cloud Compass has no approve or reject control. It creates knowledge and plans,
then stops at `WAITING_FOR_APPROVAL`. A separate approval backend owns the human
decision and writes `APPROVED` or `REJECTED` to PostgreSQL.

The local simulator models that backend with these identities:

| User | Business role | Simulated AD groups | Cloud Compass access | Approval-backend access |
|---|---|---|---|---|
| `sam`, `ivan`, `adi` | `PROJECT_OWNER` | `GROUP_1` | APMs `100401`, `100402` | None |
| `abdur`, `ajir` | `PROJECT_OWNER` | `GROUP_2` | APMs `100403`, `100404` | None |
| `reviewer` | `REVIEWER` | `CLOUD_JOURNEY_APPROVERS` | General knowledge only | Approve/reject others' requests |
| `developer` | `DEVELOPER` | none | General knowledge only | None |

Cloud Compass can only poll the database and observe the backend decision. It can
resume provisioning after it reads `APPROVED`; it cannot create that state. The
simulator waits 60 seconds by default. A real seven-day approval must use an
event/callback or durable workflow timer rather than keeping an ADK request open.

## ADK tools

| Tool | Purpose | Changes Journey state |
|---|---|---:|
| `select_simulated_identity(user_name)` | Bind a demo user to the current ADK session and show that group's APM IDs | No |
| `start_journey(apm_id, user_name)` | Bind the demo user, authorize the APM mapping, and create or return the group's Journey | Yes on first call |
| `get_cloud_journey_guidance(question, journey_id)` | Answer using persisted Journey context and identify missing discovery facts | No |
| `record_application_inventory(...)` | Persist application, platform, dependency, data, and availability knowledge | Yes |
| `generate_cloud_plan(...)` | Persist a proposed target plan and submit it for independent review | Yes |
| `wait_for_external_approval(journey_id, timeout_seconds, poll_interval_seconds)` | Poll PostgreSQL for an external decision for up to two minutes | No |
| `resume_journey_after_approval(journey_id)` | Continue simulated execution only if PostgreSQL already says `APPROVED` | Yes |
| `get_journey_status(journey_id)` | Read current state, version, requester, and complete audit history | No |
| `get_journey_status_by_apm_id(apm_id)` | Recover a Journey authorized for the simulated user's group | No |

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

Start a Cloud Journey for APM 100401 as sam.

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
Start a Cloud Journey for APM 100402 as sam.

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

### Demo 3: new-session recovery and group isolation

```text
Start a Cloud Journey for APM 100403 as abdur.

The application is Reporting Service. It is Tier 2, runs on Linux VMs, has test
and production environments, depends on PostgreSQL and SFTP, contains internal
data, and requires 99.9% availability.

Create a proposed plan targeting Cloud Run with Cloud SQL. The objective is to
reduce operations effort. Constraints are private database access and a phased cutover.
```

Stop ADK Web or close the browser. Later, open a completely new conversation and
select another member of `GROUP_2` before asking for status. The new chat has no
Journey ID and no previous conversation state:

```text
I am ajir. Could you give me the current status of APM ID 100403?
```

Expected response: Cloud Compass calls `select_simulated_identity`, then
`get_journey_status_by_apm_id`, and reports `WAITING_FOR_APPROVAL` with the
Journey ID, captured plan, and durable history because `ajir` is in `GROUP_2`.

Now open another new session and repeat as a `GROUP_1` member:

```text
I am sam. Could you give me the current status of APM ID 100403?
```

Expected response:

```text
I could not find a Journey you can access for that APM ID.
```

It must not confirm that `100403` exists or reveal its Journey ID, requester,
state, plan, or history. Finally, open a new session as another `GROUP_2` member
and send:

```text
Start a Cloud Journey for APM 100403 as ajir.
```

Cloud Compass returns the existing Journey with `created=false`; it does not
create a duplicate. To finish the original workflow, ask:

```text
Wait up to two minutes for an external approval of APM ID 100403. If it is
approved, resume execution.
```

The agent first resolves the group's Journey by APM ID. Run the backend simulator
with the returned Journey ID in another terminal. Cloud Compass then detects the
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
