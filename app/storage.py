"""Atomic local state with one owning process per canonical data directory."""
import contextlib
import json
import sqlite3
from pathlib import Path

from .instance import FileLock


class Store:
    def __init__(self, directory: str):
        path = Path(directory).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = path
        self.connection = None
        self.instance_lock = FileLock(path / ".instance.lock")
        # This precedes every database read/write, including controller startup's
        # initial save, and applies equally to the launcher and direct uvicorn.
        self.instance_lock.acquire()
        try:
            self.connection = sqlite3.connect(path / "trader.sqlite3", check_same_thread=False)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            self.connection.commit()
        except BaseException:
            # No controller/engine exists yet, so failed initialization may
            # safely release ownership after best-effort connection cleanup.
            if self.connection is not None:
                with contextlib.suppress(Exception):
                    self.connection.close()
                self.connection = None
            self.instance_lock.release()
            raise

    def load(self):
        row = self.connection.execute("SELECT value FROM snapshot WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, value):
        with self.connection:
            self.connection.execute("INSERT INTO snapshot (id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value", (json.dumps(value, ensure_ascii=False, allow_nan=False),))

    def close(self):
        if self.connection is not None:
            # If closing the DB fails, retain ownership rather than allow a new
            # writer beside a connection whose shutdown is not established.
            self.connection.close()
            self.connection = None
        self.instance_lock.release()
