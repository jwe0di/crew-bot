from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database import Migration, run_migrations


class MigrationRunnerTests(unittest.TestCase):
    def test_migrations_are_idempotent_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "migration-test.sqlite3")

            def create_base(connection: sqlite3.Connection) -> None:
                connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

            def add_value(connection: sqlite3.Connection) -> None:
                connection.execute("ALTER TABLE sample ADD COLUMN value TEXT NOT NULL DEFAULT ''")

            migrations = (
                Migration(1, "create_sample", create_base),
                Migration(2, "add_sample_value", add_value),
            )

            self.assertEqual(run_migrations(database_path, migrations), 2)
            self.assertEqual(run_migrations(database_path, migrations), 2)

            with sqlite3.connect(database_path) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(sample)")}
                ledger = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
                version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertEqual(columns, {"id", "value"})
            self.assertEqual(ledger, [(1, "create_sample"), (2, "add_sample_value")])
            self.assertEqual(version, 2)

    def test_duplicate_migration_versions_are_rejected(self) -> None:
        migration = Migration(1, "same", lambda connection: None)
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "invalid-migrations.sqlite3")
            with self.assertRaises(ValueError):
                run_migrations(database_path, (migration, migration))
