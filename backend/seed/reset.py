from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from backend.core.db import Base, engine, init_db
from backend.seed.data import seed_database


def reset_and_seed(target_engine: Engine | None = None) -> dict[str, int]:
    """Drop all tables, recreate schema, and load clean deterministic seed data."""
    eng = target_engine or engine
    import backend.core.models  # noqa: F401

    Base.metadata.drop_all(bind=eng)
    init_db(eng)

    local_session = sessionmaker(
        bind=eng, autoflush=False, autocommit=False, expire_on_commit=False
    )
    with local_session() as session:
        return seed_database(session)


if __name__ == "__main__":
    summary = reset_and_seed()
    print(f"Database reset and seeded cleanly: {summary}")
