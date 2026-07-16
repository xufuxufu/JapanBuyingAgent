from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import database_url, ensure_data_directories


class Base(DeclarativeBase):
    pass


def build_engine(url: str | None = None) -> Engine:
    ensure_data_directories()
    db_url = url or database_url()
    engine = create_engine(db_url, connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {})

    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

