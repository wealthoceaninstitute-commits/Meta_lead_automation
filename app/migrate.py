"""
Tiny auto-migration: ALTER TABLE ... ADD COLUMN for every model column that is
missing in the live database. Safe to run on every start-up; never drops or
rewrites anything. Needed because the production DB (Neon) already has tables
created by older versions of the code.
"""
import logging
from sqlalchemy import inspect, text, Boolean

log = logging.getLogger(__name__)


def add_missing_columns(engine, base) -> list[str]:
    added: list[str] = []
    insp = inspect(engine)
    for table in base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=engine.dialect)
            default = ""
            if isinstance(col.type, Boolean):
                default = " DEFAULT FALSE" if engine.dialect.name == "postgresql" else " DEFAULT 0"
            stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                added.append(f"{table.name}.{col.name}")
                log.warning("migrate: %s", stmt)
            except Exception as exc:  # another worker may have added it concurrently
                log.error("migrate failed for %s.%s: %s", table.name, col.name, exc)
    if added:
        print(f"[migrate] added columns: {', '.join(added)}", flush=True)
    return added
