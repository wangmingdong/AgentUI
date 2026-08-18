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

import base64
import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".json": "application/json; charset=utf-8",
}


# 允许上传的图片后缀 -> mime
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _save_image(workspace: str, filename: str, data_b64: str):
    """把 base64 图片存到 workspace/uploads/ 下，返回 {rel,name,mime}。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in IMG_EXTS:
        ext = ".png"
    up_dir = os.path.join(workspace, "uploads")
    os.makedirs(up_dir, exist_ok=True)
    h = hashlib.md5((filename + str(time.time())).encode("utf-8")).hexdigest()[:12]
    fname = h + ext
    path = os.path.join(up_dir, fname)
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:
        raw = base64.b64decode(data_b64)
    with open(path, "wb") as f:
        f.write(raw)
    return {"rel": "uploads/" + fname, "name": filename,
            "mime": MIME.get(ext, "application/octet-stream")}


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
    handler.send_header("Connection", "close")
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


def _event_to_step(ev):
    """把流式事件映射回 step 结构（token 事件忽略，前端已实时显示）。"""
    t = ev.get("event")
    if t == "tool":
        return {"type": "tool", "tool": ev.get("tool"), "args": ev.get("args"), "output": ""}
    if t == "tool_result":
        return {"type": "_tool_result", "output": ev.get("output")}
    if t == "done":
        return {"type": "done", "text": ev.get("text")}
    if t == "error":
        return {"type": "error", "text": ev.get("text")}
    return None


def _rebuild_steps(events):
    """从事件流重建 steps 列表（合并 tool 与其 tool_result 的 output）。"""
    steps = []
    for ev in events:
        st = _event_to_step(ev)
        if st is None:
            continue
        if st.get("type") == "_tool_result":
            for s in reversed(steps):
                if s.get("type") == "tool" and not s.get("output"):
                    s["output"] = st["output"]
                    break
            continue
        steps.append(st)
    return steps


def _summary_from_steps(steps):
    for s in reversed(steps):
        if s.get("type") in ("done", "error") and s.get("text"):
            return s["text"]
    for s in reversed(steps):
        if s.get("type") == "llm" and s.get("text"):
            return s["text"]
    return ""


def _list_dir_safe(ws, sub):
    """列出 workspace 内某子目录，返回 (items, err)。越界/不存在 err 非 None。"""
    base = os.path.realpath(ws)
    if sub:
        cand = os.path.realpath(os.path.join(base, sub))
        base_n = os.path.normcase(base)
        cand_n = os.path.normcase(cand)
        if cand_n != base_n and not cand_n.startswith(base_n + os.sep):
            return None, "路径越界"
        target = cand
    else:
        target = base
    if not os.path.isdir(target):
        return None, "目录不存在"
    items = []
    try:
        for name in sorted(os.listdir(target)):
            p = os.path.join(target, name)
            if os.path.isdir(p):
                items.append({"name": name, "type": "dir", "size": 0})
            else:
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = 0
                items.append({"name": name, "type": "file", "size": sz})
    except OSError as e:
        return None, str(e)
    return items, None


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

        # 1.5) 安全工作区文件访问（用于展示上传的图片，防越界）
        if method == "GET" and path == "/file":
            qs = parse_qs(parsed.query)
            ws = qs.get("ws", [""])[0]
            name = qs.get("name", [""])[0]
            if not store.is_valid_workspace(ws):
                print(f"[file] 403 workspace not in whitelist: {ws}")
                self.send_error(403)
                return
            base = os.path.realpath(ws)
            target = os.path.realpath(os.path.join(base, name))
            # Windows 下用 normcase 做大小写不敏感的安全边界检查
            base_n = os.path.normcase(base)
            target_n = os.path.normcase(target)
            if target_n != base_n and not target_n.startswith(base_n + os.sep):
                print(f"[file] 403 path escape: base={base} name={name} target={target}")
                self.send_error(403)
                return
            if not os.path.isfile(target):
                self.send_error(404)
                return
            _send_file(self, target)
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

        # 6.5) 工作区间管理（增加/删/列）
        if path == "/api/workspaces":
            if method == "GET":
                _send_json(self, {"workspaces": store.get_workspaces(),
                                  "default": store.WORKSPACE_DIR,
                                  "app_dir": BASE_DIR})  # 一键把本工具所在项目目录加入工作区间
                return
            if method == "POST":
                body = _read_body(self)
                ok, msg = store.add_workspace(body.get("path", ""))
                _send_json(self, {"ok": ok, "message": msg,
                                  "workspaces": store.get_workspaces()})
                return
            if method == "DELETE":
                body = _read_body(self)
                ok, msg = store.remove_workspace(body.get("path", ""))
                _send_json(self, {"ok": ok, "message": msg,
                                  "workspaces": store.get_workspaces()})
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

        # 7.6.1) 对话重命名 / 删除某条消息
        if method == "POST" and path == "/api/conversation/rename":
            body = _read_body(self)
            ok = store.rename_conversation(body.get("id", ""), body.get("title", ""))
            _send_json(self, {"ok": ok})
            return
        if method == "POST" and path == "/api/message/delete":
            body = _read_body(self)
            try:
                idx = int(body.get("index", -1))
            except Exception:
                idx = -1
            ok = store.delete_message(body.get("conversation_id", ""), idx)
            _send_json(self, {"ok": ok})
            return

        # 7.6.2) 一键把代码写入工作区文件（安全锁 workspace 内）
        if method == "POST" and path == "/api/write-file":
            body = _read_body(self)
            ws = body.get("workspace") or ""
            if not store.is_valid_workspace(ws):
                _send_json(self, {"ok": False, "message": "工作区间不在白名单"}, 400)
                return
            rel = body.get("path", "")
            content = body.get("content", "")
            try:
                target = agent._safe_path(rel, ws)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
                _send_json(self, {"ok": True, "message": f"已写入 {rel}"})
            except ValueError as e:
                _send_json(self, {"ok": False, "message": str(e)}, 400)
            except Exception as e:
                _send_json(self, {"ok": False, "message": str(e)}, 500)
            return

        # 7.7) 安全工作区文件树（供右栏文件树 / @引用自动补全）
        if method == "GET" and path == "/api/fs/list":
            qs = parse_qs(parsed.query)
            ws = qs.get("ws", [""])[0]
            sub = qs.get("path", [""])[0]
            if not store.is_valid_workspace(ws):
                ws = store.WORKSPACE_DIR
            items, err = _list_dir_safe(ws, sub)
            if err:
                _send_json(self, {"error": err}, 400)
            else:
                _send_json(self, {"ws": ws, "path": sub or "", "items": items})
            return

        # 7.8) Agent 流式执行（SSE 实时推送事件，结束后持久化会话）
        if method == "POST" and path == "/api/agent/stream":
            body = _read_body(self)
            task = (body.get("task", "") or "").strip()
            ws = body.get("workspace") or ""
            if not store.is_valid_workspace(ws):
                ws = store.WORKSPACE_DIR
            vision_mode = bool(body.get("vision_mode"))
            files = body.get("files") or []
            saved_images = []
            for im in (body.get("images") or []):
                if im.get("data"):
                    si = _save_image(ws, im.get("filename", "image.png"), im["data"])
                    si["data"] = im["data"]
                    saved_images.append(si)
            regenerate = bool(body.get("regenerate"))
            cid = body.get("conversation_id") or ""
            conv = store.get_conversation(cid) if cid else None
            if regenerate:
                if not conv:
                    _send_json(self, {"error": "无法重生成：会话不存在"}, 400)
                    return
                last_user = store.delete_last_assistant(cid)
                if last_user:
                    task = last_user
                saved_images = []
                attachments = []
            else:
                if not conv:
                    conv = store.new_conversation()
                    cid = conv["id"]
                attachments = [{"rel": s["rel"], "name": s["name"], "ws": ws} for s in saved_images]
                store.append_message(cid, "user", task, attachments=attachments)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            events = []
            try:
                for ev in agent.run_agent_stream(task, workspace=ws, images=saved_images,
                                                 vision_mode=vision_mode, files=files):
                    events.append(ev)
                    data = json.dumps(ev, ensure_ascii=False).encode("utf-8")
                    try:
                        self.wfile.write(b"data: " + data + b"\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                steps = _rebuild_steps(events)
                summary = _summary_from_steps(steps)
                conv = store.append_message(cid, "assistant", summary, steps)
                end = {"event": "saved", "conversation_id": cid, "messages": conv["messages"]}
                try:
                    self.wfile.write(b"data: " + json.dumps(end, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except Exception as e:
                err = {"event": "error", "text": f"Agent 执行异常：{e}"}
                try:
                    self.wfile.write(b"data: " + json.dumps(err, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        # 8) Agent 执行（带会话持久化 + 工作区间 + 图片）
        if method == "POST" and path == "/api/agent":
            body = _read_body(self)
            task = (body.get("task", "") or "").strip()
            if not task:
                _send_json(self, {"error": "任务为空"}, 400)
                return
            # 工作区间：非法则退回主工作区
            ws = body.get("workspace") or ""
            if not store.is_valid_workspace(ws):
                ws = store.WORKSPACE_DIR
            vision_mode = bool(body.get("vision_mode"))
            # 存图
            saved_images = []
            for im in (body.get("images") or []):
                if im.get("data"):
                    si = _save_image(ws, im.get("filename", "image.png"), im["data"])
                    si["data"] = im["data"]  # 透传给 agent 做多模态
                    saved_images.append(si)

            cid = body.get("conversation_id") or ""
            conv = store.get_conversation(cid) if cid else None
            if not conv:
                conv = store.new_conversation()
                cid = conv["id"]
            # 用户消息带上图片附件（仅存展示所需 rel/name/ws）
            attachments = [{"rel": s["rel"], "name": s["name"], "ws": ws} for s in saved_images]
            store.append_message(cid, "user", task, attachments=attachments)
            try:
                steps = agent.run_agent(task, workspace=ws, images=saved_images,
                                        vision_mode=vision_mode)
            except Exception as e:
                steps = [{"type": "error", "text": f"Agent 执行异常：{e}"}]
            conv = store.append_message(cid, "assistant", _agent_summary(steps), steps)
            _send_json(self, {"conversation_id": cid, "steps": steps,
                              "messages": conv["messages"],
                              "workspace": ws,
                              "images": attachments})
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
