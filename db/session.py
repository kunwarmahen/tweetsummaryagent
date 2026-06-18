"""Database engine, initialization, and session helpers."""
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from config import settings
from db import models  # noqa: F401  (ensures tables are registered on SQLModel.metadata)
from db.models import AppSettings

engine = create_engine(settings.database_url, echo=False)


def init_db() -> None:
    """Create the DB file, all tables, and a default settings row if missing."""
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _ensure_columns()
    with Session(engine) as session:
        existing = session.get(AppSettings, 1)
        if existing is None:
            session.add(AppSettings(id=1))
            session.commit()


# Lightweight additive migrations for columns added after a DB was first created.
_MIGRATIONS = {
    "digest_runs": {
        "telegram_sent": "BOOLEAN DEFAULT 0",
        "source_run_id": "INTEGER",
        "digest_style": "VARCHAR",
        "clustering_method": "VARCHAR",
        "ollama_model": "VARCHAR",
        "time_window_hours": "INTEGER",
        "max_themes": "INTEGER",
        "topics": "VARCHAR",
        "account_count": "INTEGER",
    },
    "settings": {
        "exclude_keywords": "VARCHAR DEFAULT ''",
        "clustering_method": "VARCHAR DEFAULT 'llm'",
        "embedding_model": "VARCHAR DEFAULT 'nomic-embed-text'",
        "similarity_threshold": "FLOAT DEFAULT 0.55",
        "stitch_threads": "BOOLEAN DEFAULT 1",
        "thread_mode": "VARCHAR DEFAULT 'reply'",
        "thread_gap_minutes": "INTEGER DEFAULT 10",
        "collection_enabled": "BOOLEAN DEFAULT 0",
        "collection_interval_hours": "INTEGER DEFAULT 3",
        "process_enabled": "BOOLEAN DEFAULT 0",
        "process_interval_hours": "INTEGER DEFAULT 4",
        "timezone": "VARCHAR DEFAULT 'America/New_York'",
    },
    "account_settings": {
        "important": "BOOLEAN DEFAULT 0",
        "color": "VARCHAR",
    },
}


def _ensure_columns() -> None:
    with engine.connect() as conn:
        for table, cols in _MIGRATIONS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # collection_runs was superseded by the unified job_runs table; drop the (empty) legacy
        # one if it's still hanging around. Guarded on row count so no real data is ever lost.
        rows = conn.exec_driver_sql("PRAGMA table_info(collection_runs)").fetchall()
        if rows and conn.exec_driver_sql("SELECT COUNT(*) FROM collection_runs").scalar() == 0:
            conn.exec_driver_sql("DROP TABLE collection_runs")
        conn.commit()


def get_session() -> Session:
    """Return a new session (caller manages the context)."""
    return Session(engine)


def get_settings(session: Session) -> AppSettings:
    """Fetch the single settings row, creating it if absent."""
    row = session.get(AppSettings, 1)
    if row is None:
        row = AppSettings(id=1)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row
