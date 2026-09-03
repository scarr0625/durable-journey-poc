from __future__ import annotations

from scripts import deploy_agent


def test_runtime_requirements_include_cloud_sql_connector() -> None:
    assert any(
        requirement.startswith("cloud-sql-python-connector[pg8000]")
        for requirement in deploy_agent.runtime_requirements()
    )


def configure_required_environment(monkeypatch) -> None:
    values = {
        "CLOUD_SQL_INSTANCE": "example-project:us-central1:journey-db",
        "CLOUD_SQL_IP_TYPE": "PUBLIC",
        "CLOUD_SQL_IAM_AUTH": "false",
        "DB_USER": "journey",
        "DB_PASSWORD": "password",
        "DB_NAME": "durable_journey",
        "DEPLOY_JOURNEY_AGENT_MODEL": "gemini-2.5-flash",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_cloud_sql_configuration_is_passed_to_runtime(monkeypatch) -> None:
    configure_required_environment(monkeypatch)

    result = deploy_agent.deployment_environment()

    assert result["CLOUD_SQL_INSTANCE"] == (
        "example-project:us-central1:journey-db"
    )
    assert result["CLOUD_SQL_IP_TYPE"] == "PUBLIC"
    assert result["CLOUD_SQL_IAM_AUTH"] == "false"
    assert result["DB_USER"] == "journey"
    assert result["DB_PASSWORD"] == "password"
    assert result["DB_NAME"] == "durable_journey"
    assert "DATABASE_URL" not in result
    assert "GOOGLE_CLOUD_PROJECT" not in result


def test_cloud_sql_instance_is_required(monkeypatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.delenv("CLOUD_SQL_INSTANCE", raising=False)

    try:
        deploy_agent.deployment_environment()
    except SystemExit as exc:
        assert str(exc) == (
            "Missing required setting CLOUD_SQL_INSTANCE in .env.gcp"
        )
    else:
        raise AssertionError(
            "deployment_environment() should require CLOUD_SQL_INSTANCE"
        )


def test_iam_database_auth_does_not_require_password(monkeypatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("CLOUD_SQL_IAM_AUTH", "true")
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    result = deploy_agent.deployment_environment()

    assert result["CLOUD_SQL_IAM_AUTH"] == "true"
    assert "DB_PASSWORD" not in result
