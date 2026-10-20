"""Atomic local state, separate from credentials and the checked-in source."""
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, directory: str):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path / "trader.sqlite3", check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        self.connection.commit()

    def load(self):
        row = self.connection.execute("SELECT value FROM snapshot WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, value):
        with self.connection:
            self.connection.execute("INSERT INTO snapshot (id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value", (json.dumps(value, ensure_ascii=False, allow_nan=False),))

    def close(self):
        self.connection.close()
