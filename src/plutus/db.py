"""SQLAlchemy engine/session setup.

SQLite today; schema and access are kept portable so a Postgres move is a
connection-string change.
"""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from plutus.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(db_url: str | None = None) -> Engine:
    url = db_url or get_settings().db_url
    return create_engine(url)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
