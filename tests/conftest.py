from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core import clock
from backend.core.db import init_db
from backend.seed.data import SEED_START_TIME, seed_database


def _make_memory_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = _make_memory_engine()
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with factory() as session:
        clock.set_time(session, SEED_START_TIME)
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture()
def seeded_session() -> Generator[Session, None, None]:
    engine = _make_memory_engine()
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with factory() as session:
        seed_database(session)
        yield session
    engine.dispose()
