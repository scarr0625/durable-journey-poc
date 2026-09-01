# Deploy Cloud Compass to Agent Runtime and Agent Registry

This guide moves the ADK process to Gemini Enterprise Agent Platform Agent
Runtime while keeping Journey business state in the existing Cloud SQL for
PostgreSQL database.

## Resulting architecture

```text
Agent Registry Playground
          |
          v
Agent Runtime (ADK AdkApp + managed chat sessions)
          |
          | Cloud SQL Python Connector / TLS / IAM
          v
Existing Cloud SQL PostgreSQL
          |
          +-- journeys (unique apm_id + owner_subject)
          +-- apm_group_access (APM ID -> authorized group)
          +-- journey_events
          +-- journey_operations
```

ADK's managed session resource stores conversation history. It does not replace
the Journey tables: every status tool still reads the authoritative business
state from Cloud SQL.

## Required: migrate the existing database

This revision adds `journeys.owner_subject` and a database-enforced unique APM
ID. SQLAlchemy's `create_all()` creates these fields for a fresh database but
does not alter an existing Cloud SQL table.

For a completely empty database, apply all files in order, beginning with
`migrations/000_initial_schema.sql`. For an existing database that already has
the three Journey tables, do not apply `000`; use the upgrade checks and commands
below for `001` and `002`.

To intentionally discard an existing PoC database and rebuild it through a
locally running Cloud SQL Auth Proxy, first stop agents that connect to it, then
run:

```powershell
.\scripts\recreate_cloud_sql_database.ps1 `
    -AdminUser postgres `
    -ApplicationUser journey `
    -DatabaseName durable_journey
```

This drops only the named database, not the Cloud SQL instance. The script
requires confirmation, recreates the database with `journey` as owner, and
applies `000`, `001`, and `002` in filename order.

With the Cloud SQL Auth Proxy already running, first check for existing
duplicates:

```sql
SELECT apm_id, COUNT(*)
FROM journeys
GROUP BY apm_id
HAVING COUNT(*) > 1;
```

Resolve any returned rows deliberately; do not arbitrarily delete Journey audit
records. Then apply the migration through the proxy:

```powershell
psql "host=127.0.0.1 port=5432 dbname=durable_journey user=journey sslmode=disable" -v ON_ERROR_STOP=1 -f migrations/001_apm_uniqueness_and_ownership.sql
psql "host=127.0.0.1 port=5432 dbname=durable_journey user=journey sslmode=disable" -v ON_ERROR_STOP=1 -f migrations/002_group_apm_authorization.sql
```

The migration backfills legacy `owner_subject` values from the PoC's
`requested_by` value, for example `sam`. That preserves local behavior. Before
using old rows in Agent Runtime, map each legacy owner to the stable runtime user
ID that will own those rows:

```sql
UPDATE journeys
SET owner_subject = 'STABLE_RUNTIME_USER_ID'
WHERE owner_subject = 'sam';
```

Do this only from an administrator-controlled migration process. Never accept an
owner subject from a chat prompt. New Journeys bind it automatically from ADK's
injected `ToolContext.user_id`.

## 1. Choose the Cloud SQL network path

For the fastest PoC deployment, use a Cloud SQL instance that has a public IP and
set `DEPLOY_CLOUD_SQL_IP_TYPE=PUBLIC`. The Cloud SQL Python Connector uses IAM,
ephemeral certificates, and TLS; it does not require an authorized-network entry.

If the instance is private-IP-only, setting `PRIVATE` is not sufficient by itself.
Agent Runtime runs in a Google-managed network, so configure an Agent Runtime
Private Service Connect interface into the VPC that can route to Cloud SQL. Use
`PSC` only when the Cloud SQL instance itself is configured for PSC. Complete that
networking work before deployment or the runtime will time out connecting.

## 2. Set local gcloud context

Install the Google Cloud CLI, then authenticate the account that will deploy the
agent:

```powershell
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Enable the required services:

```powershell
gcloud services enable aiplatform.googleapis.com
gcloud services enable sqladmin.googleapis.com
gcloud services enable storage.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable telemetry.googleapis.com
gcloud services enable logging.googleapis.com
gcloud services enable monitoring.googleapis.com
gcloud services enable agentregistry.googleapis.com
```

## 3. Create the runtime service account

```powershell
gcloud iam service-accounts create cloud-compass-agent --display-name="Cloud Compass Agent Runtime"
```

Grant only the project roles needed by this PoC:

```powershell
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:cloud-compass-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/cloudsql.client"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:cloud-compass-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/aiplatform.user"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:cloud-compass-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID --member="serviceAccount:cloud-compass-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com" --role="roles/monitoring.metricWriter"
```

The deploying user also needs `roles/aiplatform.user`, permission to write to the
staging bucket, and `roles/iam.serviceAccountUser` on this service account. Ask a
project administrator to grant these if deployment returns `PERMISSION_DENIED`.
Do not create or download a service-account key; Agent Runtime supplies runtime
credentials automatically.

## 4. Create the staging bucket

Keep the bucket in the same region as the runtime where possible:

```powershell
gcloud storage buckets create gs://YOUR_UNIQUE_STAGING_BUCKET --location=YOUR_REGION --uniform-bucket-level-access
```

## 5. Configure the database credential

### Option A: existing PostgreSQL user and password

Create a Secret Manager secret named `cloud-compass-db-password` and add the
existing database user's password as its latest version. Using the console avoids
placing the password in shell history.

Grant Secret Accessor on that one secret to:

1. `cloud-compass-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com`
2. The Agent Platform service agent
   `service-YOUR_PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com`

The second principal resolves the secret while assembling the runtime deployment;
the first is the least-surprise runtime permission.

The database user needs `CONNECT` on the database, `USAGE` on the target schema,
and DML/sequence access to the three existing Journey tables. If tables have not
been created yet, initialize them through the local proxy first or grant temporary
schema `CREATE` permission for the initial startup.

### Option B: automatic IAM database authentication

Enable IAM database authentication on the Cloud SQL instance, add
`cloud-compass-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com` as a Cloud SQL IAM
service-account database user, and grant that database principal access to the
Journey schema and tables. Then set:

```text
DEPLOY_CLOUD_SQL_IAM_AUTH=true
DEPLOY_DB_USER=cloud-compass-agent@YOUR_PROJECT_ID.iam
```

The PostgreSQL IAM username omits `.gserviceaccount.com`. This option avoids a
database-password secret but requires the IAM database user and SQL grants to be
configured correctly.

## 6. Create the deployment configuration

```powershell
Copy-Item .env.gcp.example .env.gcp
```

Edit `.env.gcp`:

```text
GCP_PROJECT_ID=your-project-id
GCP_LOCATION=us-central1
AGENT_STAGING_BUCKET=gs://your-unique-staging-bucket
AGENT_SERVICE_ACCOUNT=cloud-compass-agent@your-project-id.iam.gserviceaccount.com

DEPLOY_CLOUD_SQL_INSTANCE=your-project-id:cloud-sql-region:instance-name
DEPLOY_CLOUD_SQL_IP_TYPE=PUBLIC
DEPLOY_DB_NAME=durable_journey
DEPLOY_DB_USER=journey
DEPLOY_CLOUD_SQL_IAM_AUTH=false
DB_PASSWORD_SECRET=cloud-compass-db-password
```

Do not set `DATABASE_URL` for Agent Runtime. `DATABASE_URL` remains the local Auth
Proxy configuration; `CLOUD_SQL_INSTANCE` selects the managed connector in GCP.

## 7. Install the deployment SDK and deploy

Use Python 3.11 or newer:

```powershell
py -3.11 -m venv .venv-gcp
.venv-gcp\Scripts\Activate.ps1
python -m pip install -r requirements-agent-runtime.txt
python scripts/deploy_agent.py
```

Deployment takes several minutes. Save the returned resource name in `.env.gcp`:

```text
AGENT_RESOURCE_NAME=projects/PROJECT_ID/locations/REGION/reasoningEngines/RESOURCE_ID
```

Agent Runtime deployments are registered automatically in Agent Registry. The
managed ADK application also provides the streaming operation required by the
console Playground.

## 8. Open the managed Playground

In Google Cloud console:

1. Open **Agent Registry**.
2. Select the same project and region used for deployment.
3. Open **Durable Cloud Compass**.
4. Select **Playground** and create a new session.

The equivalent route is **Agent Platform > Deployments > Durable Cloud Compass >
Playground**.

Start with a read-only database proof using a mapped APM ID and a simulated user
from its group:

```text
I am sam. Could you give me the current status of APM ID 100401?
```

If that succeeds, Agent Runtime is reading the same Cloud SQL database as the
local Auth Proxy configuration. Then start a new Journey in the Playground.

### Identity boundary for the PoC

There is deliberately no authentication provider in this revision. A user types
a simulated identity such as `sam`; the backend binds that name once to ADK
session state and uses the predefined demo group. This tests policy behavior but
is not a security boundary: anyone can open a new session and claim another demo
name. `ToolContext.user_id` is retained as creator audit data only.

For production, replace `SIMULATED_USERS` with group claims from a trusted
authenticated frontend or identity provider. Keep the database-backed APM policy
and session-switch protection, but never authorize from a name supplied in chat.

## 9. Optional SDK smoke test

After setting `AGENT_RESOURCE_NAME`:

```powershell
python scripts/query_remote_agent.py
```

The SDK caller explicitly supplies `AGENT_TEST_USER` as ADK audit/session data.
Authorization in this PoC comes from the simulated name in each new session, so
use messages that select `sam`/`ivan` for same-group access and `abdur` for denial:

```powershell
$env:AGENT_TEST_USER = "project-owner-a"
$env:AGENT_TEST_MESSAGE = "I am sam. Could you give me the current status of APM ID 100401?"
python scripts/query_remote_agent.py

$env:AGENT_TEST_USER = "different-user-b"
python scripts/query_remote_agent.py
```

## 10. Verify durability in GCP

1. As simulated user `sam`, create APM `100401` in the managed Playground.
2. Capture inventory and generate the plan until `WAITING_FOR_APPROVAL`.
3. Close the Playground session and create a new one.
4. Select simulated user `ivan` and ask: `Could you give me the current status of
   APM ID 100401?`
5. Confirm that Cloud Compass reloads `WAITING_FOR_APPROVAL` from Cloud SQL.
6. Start another new session as simulated user `abdur` and ask the same question.
7. Confirm that `abdur` receives only the generic inaccessible/not-found response
   and sees no APM, Journey, owner, status, plan, or history data.
8. As `abdur`, try to start APM `100401`; confirm creation fails generically and no
   second database row appears.

Verify the database invariant directly:

```sql
SELECT apm_id, COUNT(*)
FROM journeys
WHERE apm_id = '100401'
GROUP BY apm_id;
```

The result must be exactly one row with count `1`.

Agent Runtime sessions may preserve conversation history, but this test uses a new
session deliberately. PostgreSQL—not the managed chat session—must recover the
Journey state.

## Troubleshooting

### `403` from Cloud SQL Admin API

Confirm the runtime service account has `roles/cloudsql.client` and that the Cloud
SQL Admin API is enabled.

### Database connection timeout

Check `DEPLOY_CLOUD_SQL_IP_TYPE`. `PUBLIC` requires a public IP on the instance.
`PRIVATE` requires Agent Runtime PSC-interface connectivity to the VPC. The local
Auth Proxy working does not prove that the managed runtime has a private network
route.

### PostgreSQL authentication failure

For password mode, verify the secret version, database user, and grants. For IAM
mode, verify IAM DB authentication is enabled, the service-account database user
exists, and the PostgreSQL username omits `.gserviceaccount.com`.

### Agent deploys but tools fail on first use

Agent creation does not necessarily open a database connection. The first tool
call initializes the PoC tables and connection pool, so inspect Agent Runtime logs
for the exact database or IAM error.

### Playground tab is missing

Open the Agent Runtime deployment as well as its Agent Registry entry. Confirm the
resource was deployed as framework `google-adk`; the packaged `AdkApp` supplies
the streaming query method used by Playground.

### Secret deployment permission failure

Grant `roles/secretmanager.secretAccessor` on the password secret to both the
runtime service account and the Agent Platform service agent described above.
