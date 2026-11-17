import argparse
import hmac
import json
import os
import re
import time
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import engine, store, tools

WEB = Path(__file__).parent / "web"
TOKEN = os.environ.get("ROBOT_ASSIST_WEB_TOKEN", "")
CONFIG_KINDS = {"model", "tool", "flow", "market"}
ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def require_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("ID 只能含字母、数字、下划线或连字符")
    return value


def arguments(body):
    raw = body.get("input", {})
    dates = body.get("dates", [])
    if not isinstance(raw, dict) or not isinstance(dates, list) or len(dates) > 50:
        raise ValueError("input 需为对象，批量上限 50 项")
    offset = raw.get("date_offset_days", 0)
    if type(offset) is not int or not 0 <= offset <= 365:
        raise ValueError("date_offset_days 须为 0 至 365 的整数")
    if not dates:
        return [raw]
    from datetime import date
    return [{**raw, "date": date.fromisoformat(day).isoformat()} for day in dict.fromkeys(dates)]


def validate(kind, body):
    if kind in {"model", "market"}:
        if not isinstance(body.get("url"), str) or not body["url"].startswith(("http://", "https://")):
            raise ValueError("填写有效 HTTP(S) URL")
    if kind == "model" and not body.get("model"):
        raise ValueError("填写模型 ID")
    if kind == "tool":
        if body.get("type") not in {"skill", "mcp", "cli"} or body.get("effect") not in {"read", "write"}:
            raise ValueError("工具需要 type 和 effect")
        if body["type"] == "mcp" and body.get("transport", "http") == "http":
            if not body.get("url", "").startswith(("http://", "https://")) or not body.get("operation"):
                raise ValueError("填写 MCP URL 与工具操作")
        else:
            if not Path(body.get("command", "")).is_absolute():
                raise ValueError("可执行文件需要绝对路径")
        for name in ("args", "env"):
            values = body.get(name, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"{name} 需为字符串数组")
    if "timeout" in body and (
        type(body["timeout"]) is not int or not 1 <= body["timeout"] <= 120
    ):
        raise ValueError("超时须为 1 至 120 秒")
    if kind == "flow":
        if body.get("executor", "flow") not in {"flow", "agent"}:
            raise ValueError("执行方式须为 flow 或 agent")
        if type(body.get("max_turns", 4)) is not int or not 1 <= body.get("max_turns", 4) <= 8:
            raise ValueError("Agent 步数须为 1 至 8")
        steps = body.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 25 or any(
            not isinstance(step, dict) or step.get("type") not in {"model", "tool"}
            for step in steps
        ):
            raise ValueError("Flow 节点须为 model 或 tool，最多 25 项")


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, mime="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else store.encode(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def handle_api(self):
        if not self.headers.get("Host"):
            return self.reply(400, {"error": "缺少 Host"})
        host = urlsplit("//" + self.headers["Host"]).hostname
        if not TOKEN and host not in {"127.0.0.1", "localhost", "::1"}:
            return self.reply(403, {"error": "本地模式仅接受本机 Host"})
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        if self.command == "GET" and path in {"/", "/app.js", "/app.css"}:
            filename, mime = {
                "/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                "/app.css": ("app.css", "text/css"),
            }[path]
            return self.reply(200, (WEB / filename).read_bytes(), mime + "; charset=utf-8")
        if parts[0] != "api":
            return self.reply(404, {"error": "页面不存在"})
        if TOKEN and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
            return self.reply(401, {"error": "需要访问令牌"})
        if self.command == "POST":
            if self.headers.get_content_type() != "application/json":
                return self.reply(415, {"error": "需要 JSON"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 262144:
                raise ValueError("请求大小超出限制")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求须为 JSON 对象")
            return self.post(parts[1:], body)
        return self.get(parts[1:])

    def get(self, parts):
        with store.connect() as db:
            if parts == ["config"]:
                rows = db.execute("SELECT * FROM config ORDER BY kind,id")
                return self.reply(200, [{"id": row["id"], "kind": row["kind"],
                                         "body": json.loads(row["body"])} for row in rows])
            if parts == ["schedules"]:
                rows = db.execute("SELECT * FROM schedules ORDER BY id")
                result = []
                for row in rows:
                    body = json.loads(row["body"])
                    body.pop("snapshots")
                    result.append({"id": row["id"], **body, "last_slot": row["last_slot"]})
                return self.reply(200, result)
            if parts == ["runs"]:
                rows = db.execute("SELECT id,status,created,updated,error FROM runs ORDER BY created DESC LIMIT 80")
                return self.reply(200, [dict(row) for row in rows])
            if len(parts) == 2 and parts[0] == "runs":
                row = db.execute("SELECT * FROM runs WHERE id=?", (parts[1],)).fetchone()
                if row:
                    detail = dict(row)
                    snap = json.loads(detail.pop("snapshot"))
                    detail.update(source=snap["source"], input=snap["input"], commit=snap["commit"],
                                  result=json.loads(detail["result"]) if detail["result"] else None)
                    detail["approvable"] = (time.time() - row["updated"] < 600 and
                                            bool(engine.booking_approval(snap, detail["result"])))
                    detail["events"] = [
                        {**event, "body": json.loads(event["body"])}
                        for event in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (parts[1],))
                    ]
                    return self.reply(200, detail)
        return self.reply(404, {"error": "记录不存在"})

    def post(self, parts, body):
        if parts == ["market", "browse"]:
            with store.connect() as db:
                source = store.config(db, body["id"], "market")
            catalog = tools.request_json(source["url"], token=tools.secret(source.get("token_env")))
            if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
                raise ValueError("市场目录需返回 {items: [...]}")
            return self.reply(200, catalog["items"])
        if parts == ["market", "import"]:
            with store.connect() as db:
                source = store.config(db, body["source"], "market")
            catalog = tools.request_json(source["url"], token=tools.secret(source.get("token_env")))
            listing = next((item for item in catalog["items"]
                            if item["id"] == body["item"] and item["version"] == body["version"]), None)
            if not listing or listing["tool"]["type"] not in {"skill", "mcp"}:
                raise ValueError("找不到可导入的 Skill 或 MCP 版本")
            return self.reply(200, {
                **listing["tool"], "market_source": body["source"],
                "market_item": body["item"], "market_version": body["version"],
            })
        if parts in (["model", "test"], ["tool", "test"]):
            kind = parts[0]
            with store.connect() as db:
                configured = store.config(db, body["id"], kind)
            if kind == "model":
                answer = tools.chat(configured, [{"role": "user", "content": "Reply with OK."}])
                return self.reply(200, {"text": answer.get("content")})
            if configured["type"] == "mcp":
                return self.reply(200, tools.mcp(configured, {}, method="tools/list"))
            if configured["effect"] != "read":
                raise ValueError("写工具须在已授权的运行中测试")
            return self.reply(200, tools.invoke(configured, body.get("input", {})))
        with store.connect(True) as db:
            if parts == ["config"]:
                kind = body["kind"]
                key = require_id(body["id"])
                if kind not in CONFIG_KINDS or not isinstance(body["body"], dict):
                    raise ValueError("配置类型无效")
                validate(kind, body["body"])
                db.execute("INSERT INTO config VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,body=excluded.body",
                           (key, kind, store.encode(body["body"])))
                db.commit()
                return self.reply(200, {"id": key})
            if parts == ["runs"]:
                specs = [engine.snapshot(db, {**body, "input": item}, "manual") for item in arguments(body)]
                ids = [store.enqueue(db, spec) for spec in specs]
                db.commit()
                return self.reply(202, {"runs": ids})
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "approve":
                existing = db.execute("SELECT commit_id FROM approvals WHERE preview_id=?", (parts[1],)).fetchone()
                if existing:
                    return self.reply(200, {"run": existing["commit_id"]})
                row = db.execute("SELECT * FROM runs WHERE id=?", (parts[1],)).fetchone()
                if not row or row["status"] != "succeeded" or not row["result"]:
                    raise ValueError("预演尚未成功")
                spec = json.loads(row["snapshot"])
                preview = json.loads(row["result"])
                reservation = engine.booking_approval(spec, preview)
                if not reservation or time.time() - row["updated"] > 600:
                    raise ValueError("仅可确认 10 分钟内通过的会议室预演")
                spec["flow"]["steps"] = [reservation]
                spec.update(commit=True, source="approved_preview", preview_id=parts[1])
                run_id = store.enqueue(db, spec)
                db.execute("INSERT INTO approvals VALUES(?,?)", (parts[1], run_id))
                db.commit()
                return self.reply(202, {"run": run_id})
            if parts == ["schedules"]:
                key = require_id(body["id"])
                at = body["time"]
                if not isinstance(at, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at):
                    raise ValueError("时间须为 HH:MM")
                engine.clock_zone(body["timezone"])
                entry = {**body, "enabled": bool(body.get("enabled")), "snapshots": [
                    engine.snapshot(db, {**body, "input": item}, "schedule") for item in arguments(body)
                ]}
                db.execute("INSERT INTO schedules(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                           (key, store.encode(entry)))
                db.commit()
                return self.reply(200, {"id": key})
            if parts == ["schedules", "toggle"]:
                row = db.execute("SELECT body FROM schedules WHERE id=?", (body["id"],)).fetchone()
                if not row:
                    raise ValueError("没有该计划")
                entry = json.loads(row["body"])
                entry["enabled"] = bool(body["enabled"])
                db.execute("UPDATE schedules SET body=? WHERE id=?", (store.encode(entry), body["id"]))
                db.commit()
                return self.reply(200, {"enabled": entry["enabled"]})
        return self.reply(404, {"error": "接口不存在"})

    def dispatch(self):
        try:
            self.handle_api()
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except OSError as exc:
            self.reply(502, {"error": type(exc).__name__})

    do_GET = do_POST = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "worker"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    store.init()
    if args.role == "api":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not TOKEN:
            parser.error("非本机监听需配置 ROBOT_ASSIST_WEB_TOKEN")
        ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    else:
        def schedule_loop():
            while True:
                try:
                    engine.schedule_due()
                except Exception:
                    logging.exception("Scheduler tick failed")
                time.sleep(3)

        threading.Thread(target=schedule_loop, daemon=True).start()
        while True:
            if not engine.tick():
                time.sleep(1)


if __name__ == "__main__":
    main()
