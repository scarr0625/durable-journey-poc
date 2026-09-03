"""Database configuration kept independent from the ADK agent."""

from __future__ import annotations

import atexit
import os
from collections.abc import Iterator
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from cloud_journey.models import Base

load_dotenv()

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://journey:journey@localhost:5432/durable_journey"
)

_cloud_sql_connector: object | None = None


def normalize_database_url(url: str) -> str:
    """Select psycopg 3 when a platform provides a generic PostgreSQL URL."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def build_engine(database_url: str | None = None, *, echo: bool | None = None) -> Engine:
    if database_url is None and os.getenv("CLOUD_SQL_INSTANCE"):
        return _build_cloud_sql_engine(echo=echo)

    url = normalize_database_url(
        database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(
        url,
        echo=echo if echo is not None else os.getenv("SQL_ECHO", "false").lower() == "true",
        **kwargs,
    )


def _build_cloud_sql_engine(*, echo: bool | None = None) -> Engine:
    """Build an Agent Runtime-friendly pool using the Cloud SQL connector."""
    global _cloud_sql_connector
    try:
        from google.cloud.sql.connector import Connector, IPTypes
    except ImportError as exc:
        raise RuntimeError(
            "CLOUD_SQL_INSTANCE is set, but cloud-sql-python-connector[pg8000] "
            "is not installed"
        ) from exc

    required = ["CLOUD_SQL_INSTANCE", "DB_USER", "DB_NAME"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Missing Cloud SQL configuration: {', '.join(missing)}"
        )

    iam_auth = os.getenv("CLOUD_SQL_IAM_AUTH", "false").lower() == "true"
    if not iam_auth and not os.getenv("DB_PASSWORD"):
        raise RuntimeError(
            "DB_PASSWORD is required unless CLOUD_SQL_IAM_AUTH=true"
        )

    ip_type_name = os.getenv("CLOUD_SQL_IP_TYPE", "PUBLIC").upper()
    ip_types = {
        "PUBLIC": IPTypes.PUBLIC,
        "PRIVATE": IPTypes.PRIVATE,
        "PSC": IPTypes.PSC,
    }
    if ip_type_name not in ip_types:
        raise RuntimeError("CLOUD_SQL_IP_TYPE must be PUBLIC, PRIVATE, or PSC")

    connector = Connector()
    _cloud_sql_connector = connector
    atexit.register(connector.close)

    def get_connection():
        connection_args: dict[str, object] = {
            "user": os.environ["DB_USER"],
            "db": os.environ["DB_NAME"],
        }
        if not iam_auth:
            connection_args["password"] = os.environ["DB_PASSWORD"]
        return connector.connect(
            os.environ["CLOUD_SQL_INSTANCE"],
            "pg8000",
            ip_type=ip_types[ip_type_name],
            enable_iam_auth=iam_auth,
            **connection_args,
        )

    return create_engine(
        "postgresql+pg8000://",
        creator=get_connection,
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "2")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        echo=echo
        if echo is not None
        else os.getenv("SQL_ECHO", "false").lower() == "true",
    )


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db(target_engine: Engine = engine) -> None:
    Base.metadata.create_all(target_engine)


@contextmanager
def session_scope(
    session_factory: sessionmaker[Session] = SessionLocal,
) -> Iterator[Session]:
    session = session_factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
