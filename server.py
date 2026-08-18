# -*- coding: utf-8 -*-
"""
本地服务入口（server）
====================
一个零依赖的标准库 HTTP 服务，把三件事合到一起：
1. 托管桌面 Web UI（static/）
2. 暴露管理接口（provider 列表、token 配置、测连通、用量）
3. 暴露 OpenAI 兼容网关 + Agent 执行接口

启动：  python server.py
默认端口 8787，浏览器打开 http://localhost:8787
Tauri 壳子直接把这个地址当 devUrl 加载，就变成了原生桌面窗口。
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import catalog
import store
import gateway
import agent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PORT = 8787

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


def _send_json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_file(handler, path):
    if not os.path.isfile(path):
        handler.send_error(404)
        return
    ext = os.path.splitext(path)[1]
    with open(path, "rb") as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _agent_summary(steps):
    """从 steps 里挑出给用户的「最终回答」文本。"""
    for s in reversed(steps or []):
        if s.get("type") in ("done", "error") and s.get("text"):
            return s["text"]
    for s in reversed(steps or []):
        if s.get("type") == "llm" and s.get("text"):
            return s["text"]
    return ""


class Handler(BaseHTTPRequestHandler):
    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path
        method = self.command

        # 1) 静态资源
        if method == "GET" and (path == "/" or path == "/index.html"):
            _send_file(self, os.path.join(STATIC_DIR, "index.html"))
            return
        if method == "GET" and path.startswith("/static/"):
            rel = path[len("/static/"):]
            _send_file(self, os.path.join(STATIC_DIR, rel))
            return

        # 2) provider 列表
        if method == "GET" and path == "/api/providers":
            _send_json(self, {"providers": catalog.list_providers(store.get_tokens())})
            return

        # 3) 配置读取
        if method == "GET" and path == "/api/config":
            dp, dm = store.get_defaults()
            _send_json(self, {"default_provider": dp, "default_model": dm,
                              "usage": store.load()["usage"]})
            return

        # 4) 设置默认 provider/model
        if method == "POST" and path == "/api/config":
            body = _read_body(self)
            store.set_defaults(body.get("default_provider"), body.get("default_model"))
            _send_json(self, {"ok": True})
            return

        # 5) 保存某平台 token
        if method == "POST" and path == "/api/token":
            body = _read_body(self)
            store.set_token(body.get("provider_key", ""), body.get("api_key", ""))
            _send_json(self, {"ok": True})
            return

        # 6) 测连通
        if method == "POST" and path == "/api/test":
            body = _read_body(self)
            ok, msg = gateway.test_connection(body.get("provider_key", ""))
            _send_json(self, {"ok": ok, "message": msg})
            return

        # 7) OpenAI 兼容网关
        if method == "POST" and path == "/v1/chat/completions":
            body = _read_body(self)
            code, j, used = gateway.chat_completion(body)
            _send_json(self, j, code)
            return

        # 7.5) 多对话：列表 / 新建
        if path == "/api/conversations":
            if method == "GET":
                _send_json(self, {"conversations": store.list_conversations()})
                return
            if method == "POST":
                body = _read_body(self)
                conv = store.new_conversation(body.get("title") or "新对话")
                _send_json(self, {"conversation": conv})
                return

        # 7.6) 多对话：详情 / 删除
        if path.startswith("/api/conversations/"):
            cid = path[len("/api/conversations/"):]
            if method == "GET":
                conv = store.get_conversation(cid)
                if not conv:
                    _send_json(self, {"error": "会话不存在"}, 404)
                    return
                _send_json(self, {"conversation": conv})
                return
            if method == "DELETE":
                ok = store.delete_conversation(cid)
                _send_json(self, {"ok": ok})
                return

        # 8) Agent 执行（带会话持久化）
        if method == "POST" and path == "/api/agent":
            body = _read_body(self)
            task = (body.get("task", "") or "").strip()
            if not task:
                _send_json(self, {"error": "任务为空"}, 400)
                return
            cid = body.get("conversation_id") or ""
            conv = store.get_conversation(cid) if cid else None
            if not conv:
                conv = store.new_conversation()
                cid = conv["id"]
            store.append_message(cid, "user", task)
            try:
                steps = agent.run_agent(task)
            except Exception as e:
                steps = [{"type": "error", "text": f"Agent 执行异常：{e}"}]
            conv = store.append_message(cid, "assistant", _agent_summary(steps), steps)
            _send_json(self, {"conversation_id": cid, "steps": steps,
                              "messages": conv["messages"]})
            return

        self.send_error(404)

    def do_GET(self):
        try:
            self._dispatch()
        except Exception as e:
            _send_json(self, {"error": str(e)}, 500)

    def do_POST(self):
        try:
            self._dispatch()
        except Exception as e:
            _send_json(self, {"error": str(e)}, 500)

    def do_DELETE(self):
        try:
            self._dispatch()
        except Exception as e:
            _send_json(self, {"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass  # 静默日志，避免刷屏


def main():
    os.makedirs(store.WORKSPACE_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"AgentShell 已启动 -> http://127.0.0.1:{PORT}")
    print(f"Agent 工作区: {store.WORKSPACE_DIR}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
