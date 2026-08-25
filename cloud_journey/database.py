"""Database configuration kept independent from the ADK agent."""

from __future__ import annotations

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


def normalize_database_url(url: str) -> str:
    """Select psycopg 3 when a platform provides a generic PostgreSQL URL."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def build_engine(database_url: str | None = None, *, echo: bool | None = None) -> Engine:
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
