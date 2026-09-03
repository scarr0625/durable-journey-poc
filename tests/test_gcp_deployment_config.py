from __future__ import annotations

from scripts import deploy_agent


def configure_required_environment(monkeypatch) -> None:
    values = {
        "DATABASE_URL": (
            "postgresql+psycopg://journey:password@db.example.com/durable_journey"
        ),
        "DEPLOY_JOURNEY_AGENT_MODEL": "gemini-2.5-flash",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_database_url_is_passed_to_runtime(monkeypatch) -> None:
    configure_required_environment(monkeypatch)

    result = deploy_agent.deployment_environment()

    assert result["DATABASE_URL"] == (
        "postgresql+psycopg://journey:password@db.example.com/durable_journey"
    )
    assert "GOOGLE_CLOUD_PROJECT" not in result
    assert "CLOUD_SQL_INSTANCE" not in result
    assert "DB_PASSWORD" not in result


def test_database_url_is_required(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    try:
        deploy_agent.deployment_environment()
    except SystemExit as exc:
        assert str(exc) == "Missing required setting DATABASE_URL in .env.gcp"
    else:
        raise AssertionError("deployment_environment() should require DATABASE_URL")
