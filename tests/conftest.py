from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from backend.core import clock
from backend.core.db import create_db_engine, init_db
from backend.seed.data import SEED_START_TIME, seed_database


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with factory() as session:
        clock.set_time(session, SEED_START_TIME)
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture()
def seeded_session() -> Generator[Session, None, None]:
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with factory() as session:
        seed_database(session)
        yield session
    engine.dispose()
