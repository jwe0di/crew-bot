"""Versioned SQLite migrations and structural database validation.

The bot's existing startup reconciler remains responsible for legacy data moves.
This module adds an explicit, transactional migration ledger for new schema changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Callable, Iterable


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def run_migrations(database_path: str, migrations: Iterable[Migration]) -> int:
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    versions = [item.version for item in ordered]
    if any(v < 1 for v in versions) or len(versions) != len(set(versions)):
        raise ValueError("Migration versions must be unique positive integers")
    if versions and versions != list(range(1, versions[-1] + 1)):
        raise ValueError("Migration registry must be contiguous from version 1")
    con = sqlite3.connect(database_path, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )""")
        con.commit()
        latest_known = versions[-1] if versions else 0
        pragma_version = int(con.execute("PRAGMA user_version").fetchone()[0])
        applied_rows = con.execute("SELECT version,name FROM schema_migrations ORDER BY version").fetchall()
        applied = {int(row[0]): str(row[1]) for row in applied_rows}
        if pragma_version > latest_known:
            raise RuntimeError(f"Database version {pragma_version} is newer than this bot supports ({latest_known})")
        if applied and max(applied) != pragma_version:
            raise RuntimeError("schema_migrations and PRAGMA user_version disagree")
        known = {m.version: m for m in ordered}
        unknown = set(applied) - set(known)
        if unknown:
            raise RuntimeError(f"Unknown applied migration versions: {sorted(unknown)}")
        for migration in ordered:
            if migration.version in applied:
                if applied[migration.version] != migration.name:
                    raise RuntimeError(f"Migration {migration.version} name mismatch")
                continue
            if migration.version <= pragma_version:
                raise RuntimeError(f"Migration ledger is missing version {migration.version}")
            con.execute("BEGIN IMMEDIATE")
            try:
                migration.apply(con)
                stamp = datetime.now(timezone.utc).isoformat()
                con.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                    (migration.version, migration.name, stamp),
                )
                con.execute(f"PRAGMA user_version={migration.version}")
                con.commit()
            except Exception:
                con.rollback()
                raise
        return int(con.execute("PRAGMA user_version").fetchone()[0])
    finally:
        con.close()


def validate_database_file(
    path: str,
    schema_sql: str,
    indexes_sql: str,
    migration_indexes: Iterable[str] = (),
    require_migrations: bool = False,
) -> tuple[bool, list[str]]:
    """Validate integrity plus required tables, columns, and index names.

    Extra tables/columns are allowed for forward-compatible application data.
    """
    errors: list[str] = []
    expected = sqlite3.connect(":memory:")
    actual = None
    try:
        expected.executescript(schema_sql)
        expected.executescript(indexes_sql)
        expected_tables = {
            row[0] for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_columns = {
            table: {row[1] for row in expected.execute(f'PRAGMA table_info("{table}")')}
            for table in expected_tables
        }
        expected_indexes = {
            row[0] for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        } | set(migration_indexes)
        actual = sqlite3.connect(path, timeout=15)
        actual.execute("PRAGMA busy_timeout=15000")
        check = actual.execute("PRAGMA integrity_check").fetchall()
        if not check or any(row[0] != "ok" for row in check):
            errors.append("SQLite integrity_check failed")
        fk_errors = actual.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            errors.append(f"Foreign-key violations: {len(fk_errors)}")
        actual_tables = {
            row[0] for row in actual.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing_tables = expected_tables - actual_tables
        if missing_tables:
            errors.append("Missing tables: " + ", ".join(sorted(missing_tables)))
        for table in sorted(expected_tables & actual_tables):
            actual_columns = {row[1] for row in actual.execute(f'PRAGMA table_info("{table}")')}
            missing = expected_columns[table] - actual_columns
            if missing:
                errors.append(f"{table} missing columns: " + ", ".join(sorted(missing)))
        actual_indexes = {
            row[0] for row in actual.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing_indexes = expected_indexes - actual_indexes
        if missing_indexes:
            errors.append("Missing indexes: " + ", ".join(sorted(missing_indexes)))
        if "schema_migrations" not in actual_tables:
            if require_migrations:
                errors.append("Missing schema_migrations ledger")
            if int(actual.execute("PRAGMA user_version").fetchone()[0]) != 0:
                errors.append("PRAGMA user_version is set but migration ledger is missing")
        else:
            ledger = actual.execute("SELECT version,name,applied_at FROM schema_migrations ORDER BY version").fetchall()
            user_version = int(actual.execute("PRAGMA user_version").fetchone()[0])
            ledger_versions = [int(row[0]) for row in ledger]
            if ledger and ledger_versions != list(range(1, ledger_versions[-1] + 1)):
                errors.append("Migration ledger contains missing version numbers")
            if ledger and int(ledger[-1][0]) != user_version:
                errors.append("Migration ledger version does not match PRAGMA user_version")
            if not ledger and user_version:
                errors.append("PRAGMA user_version is set but migration ledger is empty")
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        errors.append(f"Database validation error: {type(exc).__name__}")
    finally:
        if actual is not None:
            actual.close()
        expected.close()
    return not errors, errors