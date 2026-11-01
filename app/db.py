from __future__ import annotations

import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base


def make_engine(url: str | None = None):
    url = url or os.environ.get("PA_DATABASE_URL") or "sqlite:///data/partnership.db"
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    engine = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

    return engine


# Columns added after the first release. create_all() makes new tables but never alters existing
# ones, so a live database gets these with ALTER TABLE ... ADD COLUMN (safe: nullable or with default).
ADDED_COLUMNS = [
    ("documents", "supplier_id", "INTEGER REFERENCES suppliers(id)"),
    ("documents", "paid", "INTEGER NOT NULL DEFAULT 0"),
    ("journal_lines", "item_id", "INTEGER REFERENCES items(id)"),
    ("journal_lines", "supplier_id", "INTEGER REFERENCES suppliers(id)"),
    ("journal_lines", "quantity", "INTEGER NOT NULL DEFAULT 0"),
    ("document_items", "book_quantity", "INTEGER NOT NULL DEFAULT 0"),
    ("documents", "ref_no", "VARCHAR(100) NOT NULL DEFAULT ''"),
    ("projects", "sale_price", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "packaging_per_unit", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "labor_per_unit", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "other_per_unit", "INTEGER NOT NULL DEFAULT 0"),
]


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def make_sessionmaker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
