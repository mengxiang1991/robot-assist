import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

MAX_RESPONSE = 1024 * 1024


def secret(reference):
    if not reference:
        return None
    if reference not in os.environ:
        raise ValueError(f"Worker 缺少凭据环境变量：{reference}")
    return os.environ[reference]


def request_json(url, body=None, token=None, timeout=30, headers=None, read_headers=False):
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if not url.startswith(("https://", "http://")):
        raise ValueError("仅支持 HTTP(S) 地址")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        if res.headers.get_content_type() == "text/event-stream":
            response = None
            for _ in range(2000):
                raw_line = res.readline(MAX_RESPONSE + 1)
                if not raw_line or len(raw_line) > MAX_RESPONSE:
                    break
                if raw_line.startswith(b"data:"):
                    message = json.loads(raw_line[5:])
                    if isinstance(message, dict) and message.get("id") == body.get("id"):
                        response = message
                        break
        else:
            raw = res.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("响应超过 1 MB")
            response = json.loads(raw) if raw else None
        return (response, res.headers) if read_headers else response


def chat(model, messages, tools=None):
    data = {"model": model["model"], "messages": messages}
    if tools:
        data["tools"] = tools
    response = request_json(
        model["url"].rstrip("/") + "/chat/completions", data,
        secret(model.get("token_env")), model.get("timeout", 45),
    )
    return response["choices"][0]["message"]


def process_env(tool):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
           if key in os.environ}
    if "ROBOT_ASSIST_DATA" in os.environ and tool.get("local_demo"):
        env["ROBOT_ASSIST_DATA"] = os.environ["ROBOT_ASSIST_DATA"]
    for key in tool.get("env", []):
        env[key] = secret(key)
    return env


def executable(tool):
    path = Path(tool["command"])
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Worker 上找不到已配置的绝对可执行路径")
    if path.suffix.lower() in {".bat", ".cmd"}:
        raise ValueError("请配置实际可执行程序及独立参数")
    return str(path)


@contextmanager
def mcp_transport(tool):
    if tool.get("transport", "http") == "http":
        headers = {"Accept": "application/json, text/event-stream"}

        def send(message):
            response, reply_headers = request_json(
                tool["url"], message, secret(tool.get("token_env")),
                tool.get("timeout", 30), headers, read_headers=True,
            )
            session = reply_headers.get("Mcp-Session-Id")
            if session:
                headers["Mcp-Session-Id"] = session
            return response

        yield send, headers
        return

    args = [executable(tool), *tool.get("args", [])]
    with subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=tool.get("cwd") or None, env=process_env(tool),
    ) as child:
        replies = queue.Queue()

        def read():
            while line := child.stdout.readline(MAX_RESPONSE + 1):
                if len(line) > MAX_RESPONSE:
                    break
                replies.put(line)
            replies.put(None)

        threading.Thread(target=read, daemon=True).start()

        def send(message):
            child.stdin.write((json.dumps(message) + "\n").encode())
            child.stdin.flush()
            if "id" not in message:
                return None
            deadline = time.monotonic() + tool.get("timeout", 30)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("MCP stdio 等待响应超时")
                try:
                    line = replies.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("MCP stdio 等待响应超时") from exc
                if line is None:
                    raise ValueError("MCP 子进程未返回有效响应")
                response = json.loads(line)
                if response.get("id") == message["id"]:
                    return response

        try:
            yield send, {}
        finally:
            if child.poll() is None:
                child.kill()


def mcp(tool, arguments, method="tools/call"):
    with mcp_transport(tool) as (send, headers):
        def call(method, params, message_id=None):
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            if message_id is not None:
                payload["id"] = message_id
            reply = send(payload)
            if message_id is None:
                return None
            if not reply or "error" in reply:
                raise ValueError(f"MCP {method} 返回错误")
            return reply["result"]

        init = call("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "Robot Assist", "version": "0.1"},
        }, 1)
        headers["MCP-Protocol-Version"] = init["protocolVersion"]
        call("notifications/initialized", {})
        params = {"name": tool["operation"], "arguments": arguments} if method == "tools/call" else {}
        result = call(method, params, 2)
        if result.get("isError"):
            raise ValueError("MCP 工具执行失败")
        return result


def invoke(tool, arguments):
    kind = tool["type"]
    if kind == "mcp":
        return mcp(tool, arguments)
    if kind in {"cli", "skill"}:
        result = subprocess.run(
            [executable(tool), *tool.get("args", [])],
            input=json.dumps(arguments, ensure_ascii=False).encode(),
            capture_output=True, timeout=tool.get("timeout", 30),
            cwd=tool.get("cwd") or None, env=process_env(tool),
        )
        if result.returncode:
            raise ValueError(f"工具退出码：{result.returncode}")
        if len(result.stdout) > MAX_RESPONSE:
            raise ValueError("工具输出超过 1 MB")
        return json.loads(result.stdout)
    raise ValueError("工具类型不受支持")
