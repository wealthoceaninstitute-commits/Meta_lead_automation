from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

# Neon / serverless Postgres closes idle connections: pre_ping + recycle keeps the pool healthy.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create missing tables, then add any missing columns to existing tables
    (create_all never alters tables that already exist)."""
    from . import models  # noqa
    from .migrate import add_missing_columns
    Base.metadata.create_all(bind=engine)
    add_missing_columns(engine, Base)
