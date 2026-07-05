import os
import sqlite3
from pathlib import Path


V2_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = V2_DIR / "data" / "v2.sqlite3"


def db_path() -> Path:
    return Path(os.getenv("V2_DB_PATH", str(DEFAULT_DB_PATH)))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_to_dicts(rows):
    return [dict(row) for row in rows]
