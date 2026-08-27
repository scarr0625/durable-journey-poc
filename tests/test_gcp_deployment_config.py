from __future__ import annotations

from scripts import deploy_agent


def configure_required_environment(monkeypatch) -> None:
    values = {
        "DEPLOY_CLOUD_SQL_INSTANCE": "example:us-central1:journeys",
        "DEPLOY_CLOUD_SQL_IP_TYPE": "PUBLIC",
        "DEPLOY_DB_NAME": "durable_journey",
        "DEPLOY_DB_USER": "journey",
        "DEPLOY_JOURNEY_AGENT_MODEL": "gemini-2.5-flash",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_password_mode_uses_secret_reference(monkeypatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("DEPLOY_CLOUD_SQL_IAM_AUTH", "false")
    monkeypatch.setenv("DB_PASSWORD_SECRET", "cloud-compass-db-password")

    result = deploy_agent.deployment_environment()

    assert result["CLOUD_SQL_INSTANCE"] == "example:us-central1:journeys"
    assert result["DB_PASSWORD"] == {
        "secret": "cloud-compass-db-password",
        "version": "latest",
    }
    assert "GOOGLE_CLOUD_PROJECT" not in result
    assert "DATABASE_URL" not in result


def test_iam_database_mode_needs_no_password_secret(monkeypatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("DEPLOY_CLOUD_SQL_IAM_AUTH", "true")
    monkeypatch.delenv("DB_PASSWORD_SECRET", raising=False)

    result = deploy_agent.deployment_environment()

    assert result["CLOUD_SQL_IAM_AUTH"] == "true"
    assert "DB_PASSWORD" not in result
