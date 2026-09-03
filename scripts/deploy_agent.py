"""Deploy Cloud Compass to Gemini Enterprise Agent Platform Agent Runtime."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env.gcp")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting {name} in .env.gcp")
    return value


def runtime_requirements() -> list[str]:
    lines = (PROJECT_ROOT / "requirements-agent-runtime.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def deployment_environment() -> dict[str, str]:
    iam_auth = os.getenv("CLOUD_SQL_IAM_AUTH", "false").strip().lower() == "true"
    environment = {
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
        "JOURNEY_AGENT_MODEL": os.getenv(
            "DEPLOY_JOURNEY_AGENT_MODEL", "gemini-2.5-flash"
        ),
        "CLOUD_SQL_INSTANCE": required("CLOUD_SQL_INSTANCE"),
        "CLOUD_SQL_IP_TYPE": os.getenv("CLOUD_SQL_IP_TYPE", "PUBLIC"),
        "CLOUD_SQL_IAM_AUTH": str(iam_auth).lower(),
        "DB_USER": required("DB_USER"),
        "DB_NAME": required("DB_NAME"),
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    }
    if not iam_auth:
        environment["DB_PASSWORD"] = required("DB_PASSWORD")
    return environment


def main() -> None:
    try:
        import vertexai
    except ImportError as exc:
        raise SystemExit(
            "Install deployment dependencies first: "
            "python -m pip install -r requirements-agent-runtime.txt"
        ) from exc

    # Import only after configuration validation and SDK availability checks.
    from cloud_journey.deployment import app

    project_id = required("GCP_PROJECT_ID")
    location = required("GCP_LOCATION")
    service_account = required("AGENT_SERVICE_ACCOUNT")
    staging_bucket = required("AGENT_STAGING_BUCKET")

    client = vertexai.Client(project=project_id, location=location)
    remote_agent = client.agent_engines.create(
        agent=app,
        config={
            "display_name": os.getenv(
                "AGENT_DISPLAY_NAME", "Durable Cloud Compass"
            ),
            "description": (
                "Knowledge-first Cloud Journey assistant with durable Cloud SQL state "
                "and an external approval boundary."
            ),
            "requirements": runtime_requirements(),
            "extra_packages": [str(PROJECT_ROOT / "cloud_journey")],
            "staging_bucket": staging_bucket,
            "service_account": service_account,
            "agent_framework": "google-adk",
            "env_vars": deployment_environment(),
            "min_instances": int(os.getenv("AGENT_MIN_INSTANCES", "0")),
            "max_instances": int(os.getenv("AGENT_MAX_INSTANCES", "2")),
            "labels": {"application": "durable-cloud-compass", "environment": "poc"},
        },
    )
    resource_name = (
        getattr(remote_agent, "resource_name", None)
        or getattr(remote_agent, "name", None)
        or getattr(getattr(remote_agent, "api_resource", None), "name", None)
    )
    print("Deployment complete.")
    print(f"Agent Runtime resource: {resource_name or remote_agent}")
    print("Open Agent Registry or Agent Platform Deployments, select this agent, then open Playground.")


if __name__ == "__main__":
    main()
