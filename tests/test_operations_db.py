from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite
from extensions import operations


class OperationsDatabaseContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_db_context_sets_row_factory_and_closes_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original_path = operations.DB_PATH
            operations.DB_PATH = str(Path(directory) / "operations-test.sqlite3")
            try:
                async with operations._db() as db:
                    self.assertIs(db.row_factory, aiosqlite.Row)
                    await db.execute("CREATE TABLE context_test (value INTEGER NOT NULL)")
                    await db.execute("INSERT INTO context_test(value) VALUES (7)")
                    await db.commit()
                    cursor = await db.execute("SELECT value FROM context_test")
                    row = await cursor.fetchone()
                    self.assertEqual(row["value"], 7)
                with self.assertRaises(ValueError):
                    await db.execute("SELECT 1")
            finally:
                operations.DB_PATH = original_path
