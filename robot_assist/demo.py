import json
import sys
from pathlib import Path

try:
    from . import store
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from robot_assist import store


def seed():
    store.init()
    runner = {"type": "skill", "local_demo": True, "command": sys.executable, "cwd": str(Path.cwd())}
    script = str(Path(__file__).resolve())
    configs = [
        ("demo-room-search", "tool", {**runner, "effect": "read", "args": [script, "search"]}),
        ("demo-room-reserve", "tool", {**runner, "effect": "write", "args": [script, "reserve"]}),
        ("demo-booking", "flow", {
            "name": "会议室预订（本地样例）", "executor": "flow", "approval": "booking",
            "tools": ["demo-room-search", "demo-room-reserve"],
            "steps": [{"type": "tool", "tool": key} for key in ("demo-room-search", "demo-room-reserve")],
        }),
    ]
    with store.connect(True) as db:
        db.executemany("INSERT OR IGNORE INTO config VALUES(?,?,?)",
                       [(key, kind, store.encode(body)) for key, kind, body in configs])
        for key, kind, body in configs[:2]:
            previous = store.config(db, key, kind)
            if previous.get("local_demo"):
                db.execute("UPDATE config SET body=? WHERE id=?", (store.encode(body), key))
        existing = store.config(db, "demo-booking", "flow")
        if existing.get("tools") == ["demo-room-search", "demo-room-reserve"] and "approval" not in existing:
            existing["approval"] = "booking"
            db.execute("UPDATE config SET body=? WHERE id=?", (store.encode(existing), "demo-booking"))
        db.execute("""CREATE TABLE IF NOT EXISTS demo_bookings (
            id TEXT PRIMARY KEY, date TEXT NOT NULL, slot TEXT NOT NULL, room TEXT NOT NULL,
            UNIQUE(date,slot,room)
        )""")


def operation(mode, args):
    from datetime import date
    day = date.fromisoformat(args["date"]).isoformat()
    slot = args.get("slot", "10:00–11:00")
    room = args.get("room", "A301")
    if room not in {"A301", "B202"} or slot not in {"10:00–11:00", "14:00–15:00"}:
        raise ValueError("样例仅支持 A301/B202 和两个固定时段")
    with store.connect(mode == "reserve") as db:
        existing = db.execute(
            "SELECT id FROM demo_bookings WHERE date=? AND slot=? AND room=?", (day, slot, room)
        ).fetchone()
        if mode == "search":
            return {"date": day, "slot": slot, "room": room, "available": existing is None}
        if existing:
            return {"reserved": False, "reason": "时段已占用", "date": day, "slot": slot, "room": room}
        key = store.new_id()
        db.execute("INSERT INTO demo_bookings VALUES(?,?,?,?)", (key, day, slot, room))
        return {"reserved": True, "booking_id": key, "date": day, "slot": slot, "room": room}


if __name__ == "__main__":
    if len(sys.argv) == 1:
        seed()
        print("Demo tools and workflow ready.")
    else:
        payload = json.loads(sys.stdin.buffer.read())
        print(json.dumps(operation(sys.argv[1], payload)))
