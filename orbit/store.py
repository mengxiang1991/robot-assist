import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA = Path(os.environ.get("ORBIT_DATA", "data"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, body TEXT NOT NULL, last_slot TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, snapshot TEXT NOT NULL,
    result TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
    lease REAL
);
CREATE INDEX IF NOT EXISTS run_queue ON runs(status, created);
CREATE TABLE IF NOT EXISTS approvals (
    preview_id TEXT PRIMARY KEY, commit_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    event TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL
);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def new_id():
    return uuid.uuid4().hex


@contextmanager
def connect(write=False):
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "orbit.db", timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if write:
            db.execute("BEGIN IMMEDIATE")
        yield db
        if write:
            db.commit()
    finally:
        db.close()


def init():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


def config(db, config_id, kind):
    row = db.execute(
        "SELECT body FROM config WHERE id=? AND kind=?", (config_id, kind)
    ).fetchone()
    if not row:
        raise ValueError(f"未找到 {kind} 配置：{config_id}")
    return json.loads(row["body"])


def event(db, run_id, name, **payload):
    db.execute(
        "INSERT INTO events(run_id,event,body,created) VALUES(?,?,?,?)",
        (run_id, name, encode(payload), time.time()),
    )


def enqueue(db, snapshot):
    run_id = new_id()
    now = time.time()
    db.execute(
        "INSERT INTO runs(id,status,snapshot,created,updated) VALUES(?,?,?,?,?)",
        (run_id, "queued", encode(snapshot), now, now),
    )
    event(db, run_id, "queued", source=snapshot["source"])
    return run_id
