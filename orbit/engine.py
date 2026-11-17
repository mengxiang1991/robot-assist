import json
import time
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store, tools


def clock_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        if name == "UTC":
            return UTC
        raise ValueError("缺少该 IANA 时区数据，请在 Worker 安装 tzdata")


def snapshot(db, body, source):
    flow = store.config(db, body["flow"], "flow")
    tool_map = {key: store.config(db, key, "tool") for key in flow.get("tools", [])}
    for step in flow.get("steps", []):
        if step["type"] == "tool" and step["tool"] not in tool_map:
            raise ValueError("Flow 节点引用了未绑定的工具")
    requires_model = flow.get("executor") == "agent" or any(
        step["type"] == "model" for step in flow.get("steps", [])
    )
    model_id = flow.get("model")
    if requires_model and not model_id:
        raise ValueError("当前 Flow 需要绑定模型")
    return {
        "flow": flow, "tools": tool_map,
        "model": store.config(db, model_id, "model") if model_id else None,
        "source": source, "input": body.get("input", {}),
        "commit": bool(body.get("commit", False)),
        "schedule_id": body.get("schedule_id"),
    }


def booking_approval(spec, result):
    flow = spec["flow"]
    nodes = flow.get("steps", [])
    outputs = (result or {}).get("steps", [])
    if (flow.get("approval") != "booking" or flow.get("executor", "flow") != "flow"
            or spec["commit"] or len(nodes) < 2 or len(outputs) != len(nodes)):
        return None
    search, reserve = nodes[-2:]
    if (search["type"] != "tool" or reserve["type"] != "tool"
            or "arguments" in reserve
            or spec["tools"][search["tool"]]["effect"] != "read"
            or spec["tools"][reserve["tool"]]["effect"] != "write"):
        return None
    candidate, preview = outputs[-2:]
    if (not isinstance(candidate, dict) or candidate.get("available") is not True
            or not isinstance(preview, dict) or preview.get("skipped") is not True):
        return None
    return {**reserve, "arguments": candidate}


def emit(run_id, name, **payload):
    with store.connect(True) as db:
        store.event(db, run_id, name, **payload)
        db.execute("UPDATE runs SET lease=?,updated=? WHERE id=?",
                   (time.time() + 300, time.time(), run_id))


def run_flow(run_id, spec):
    flow = spec["flow"]
    model = spec["model"]

    def invoke(key, arguments):
        if key not in spec["tools"] or not isinstance(arguments, dict):
            raise ValueError("工具不在 Flow 授权范围内，或参数不是对象")
        tool = spec["tools"][key]
        if tool.get("effect", "write") == "write" and not spec["commit"]:
            return {"skipped": True, "reason": "预演模式，跳过写操作"}
        emit(run_id, "tool_start", tool=key, effect=tool.get("effect", "write"))
        result = tools.invoke(tool, arguments)
        emit(run_id, "tool_end", tool=key)
        return result

    if flow.get("executor") == "agent":
        mapping = {f"tool_{index}": key for index, key in enumerate(spec["tools"])}
        functions = [{
            "type": "function", "function": {
                "name": name, "description": spec["tools"][key].get("description") or key,
                "parameters": spec["tools"][key].get(
                    "schema", {"type": "object", "additionalProperties": True}
                ),
            }
        } for name, key in mapping.items()]
        messages = [
            {"role": "system", "content": flow.get("instruction") or "完成用户配置的任务。"},
            {"role": "user", "content": store.encode(spec["input"])},
        ]
        for iteration in range(flow.get("max_turns", 4)):
            emit(run_id, "model_step", iteration=iteration + 1)
            reply = tools.chat(model, messages, functions)
            calls = reply.get("tool_calls") or []
            if not calls:
                return {"output": reply.get("content")}
            if len(calls) > 8:
                raise ValueError("单轮工具调用超过上限")
            messages.append(reply)
            for call in calls:
                fn = call["function"]
                result = invoke(mapping.get(fn["name"]), json.loads(fn["arguments"]))
                messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": store.encode(result),
                })
        raise ValueError("已达到 Agent 步数上限")

    current = spec["input"]
    outputs = []
    for index, step in enumerate(flow.get("steps", [])):
        emit(run_id, "step", index=index, type=step["type"])
        if step["type"] == "model":
            reply = tools.chat(model, [
                {"role": "system", "content": step["instruction"]},
                {"role": "user", "content": store.encode(current)},
            ])
            content = reply.get("content")
            current = json.loads(content) if step.get("json") else {"text": content}
        elif step["type"] == "tool":
            args = step.get("arguments", current)
            current = invoke(step["tool"], args)
        else:
            raise ValueError("未知节点类型")
        outputs.append(current)
    return {"output": current, "steps": outputs}


def schedule_due():
    now = time.time()
    with store.connect(True) as db:
        for row in db.execute("SELECT * FROM schedules").fetchall():
            entry = json.loads(row["body"])
            if not entry["enabled"]:
                continue
            local = datetime.fromtimestamp(now, clock_zone(entry["timezone"]))
            slot = local.strftime("%Y-%m-%d %H:%M")
            if local.strftime("%H:%M") != entry["time"] or row["last_slot"] == slot:
                continue
            # The slot and its runs commit together, so polling cannot duplicate them.
            db.execute("UPDATE schedules SET last_slot=? WHERE id=?", (slot, row["id"]))
            for item in entry["snapshots"]:
                sample = {**item, "input": dict(item["input"])}
                if "date_offset_days" in sample["input"]:
                    offset = sample["input"].pop("date_offset_days")
                    sample["input"].setdefault("date", (local.date() + timedelta(days=offset)).isoformat())
                enqueue_schedule(db, sample, row["id"])


def tick():
    now = time.time()
    with store.connect(True) as db:
        expired = db.execute(
            "SELECT id FROM runs WHERE status='running' AND lease<?", (now,)
        ).fetchall()
        for row in expired:
            db.execute("UPDATE runs SET status='unknown',error=?,updated=? WHERE id=?",
                       ("执行器中断，请核验外部结果。", now, row["id"]))
            store.event(db, row["id"], "unknown")
        row = db.execute(
            "SELECT id,snapshot FROM runs WHERE status='queued' ORDER BY created LIMIT 1"
        ).fetchone()
        if not row:
            return False
        run_id = row["id"]
        db.execute("UPDATE runs SET status='running',lease=?,updated=? WHERE id=?",
                   (now + 300, now, run_id))
        store.event(db, run_id, "running")

    spec = json.loads(row["snapshot"])
    result, error = None, None
    try:
        result = run_flow(run_id, spec)
        status = "succeeded"
    except Exception as exc:
        with store.connect() as db:
            writes = db.execute(
                "SELECT body FROM events WHERE run_id=? AND event='tool_start'", (run_id,)
            ).fetchall()
        status = "unknown" if any(json.loads(row["body"])["effect"] == "write" for row in writes) else "failed"
        error = f"{type(exc).__name__}: {exc}" if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__
    with store.connect(True) as db:
        db.execute(
            "UPDATE runs SET status=?,result=?,error=?,updated=?,lease=NULL WHERE id=? AND status='running'",
            (status, store.encode(result) if result is not None else None, error, time.time(), run_id),
        )
        store.event(db, run_id, status)
    return True


def enqueue_schedule(db, spec, schedule_id):
    return store.enqueue(db, {**spec, "schedule_id": schedule_id})
