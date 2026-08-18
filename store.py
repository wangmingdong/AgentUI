# -*- coding: utf-8 -*-
"""
本地配置存储（store）
====================
- tokens.json：保存你给各平台填的 token（明文存本地，仅本机用；不要提交到 git）
- 同时保存默认模型、默认平台、简单用量计数

安全提示：token 是敏感凭证。这里为了「开箱即用」先用明文本地文件，
后续可升级为系统钥匙环（keyring）。生产环境请勿把 tokens.json 上传。
"""

import json
import os
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "tokens.json")
WORKSPACE_DIR = os.path.join(BASE_DIR, "agent_workspace")

_lock = threading.Lock()
_state = None  # 内存缓存


def _default_state():
    return {
        "tokens": {},          # {provider_key: {"api_key": "..."}}
        "default_provider": "opencode_zen",
        "default_model": "deepseek-v4-flash-free",   # 默认走匿名 Zen，零门槛
        "usage": {},           # {provider_key: {"calls": n, "tokens": n}}
    }


def load():
    global _state
    if _state is not None:
        return _state
    with _lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                base = _default_state()
                base.update(data)
                _state = base
            except Exception:
                _state = _default_state()
        else:
            _state = _default_state()
    # 确保工作区存在
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    return _state


def save():
    with _lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)


def get_tokens():
    return load()["tokens"]


def get_token(provider_key: str):
    return load()["tokens"].get(provider_key, {}).get("api_key", "")


def set_token(provider_key: str, api_key: str):
    s = load()
    s["tokens"].setdefault(provider_key, {})["api_key"] = api_key
    save()


def get_defaults():
    s = load()
    return s["default_provider"], s["default_model"]


def set_defaults(default_provider: str, default_model: str):
    s = load()
    if default_provider:
        s["default_provider"] = default_provider
    if default_model:
        s["default_model"] = default_model
    save()


def add_usage(provider_key: str, tokens_used: int = 0):
    s = load()
    u = s["usage"].setdefault(provider_key, {"calls": 0, "tokens": 0})
    u["calls"] += 1
    u["tokens"] += tokens_used
    save()


# ============================================================
# 多对话存储（conversations.json，本机数据，不提交 git）
# 结构：conversations[id] = {id, title, created_at, updated_at, messages:[]}
#       每条 message = {role, content, ts, steps?}  （assistant 才有 steps）
# ============================================================

CONV_PATH = os.path.join(BASE_DIR, "conversations.json")
_conv_state = None
_conv_lock = threading.Lock()


def _default_conv_state():
    return {"conversations": {}}


def load_conversations():
    global _conv_state
    if _conv_state is not None:
        return _conv_state
    with _conv_lock:
        if os.path.exists(CONV_PATH):
            try:
                with open(CONV_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                base = _default_conv_state()
                base.update(data)
                _conv_state = base
            except Exception:
                _conv_state = _default_conv_state()
        else:
            _conv_state = _default_conv_state()
    return _conv_state


def save_conversations():
    with _conv_lock:
        with open(CONV_PATH, "w", encoding="utf-8") as f:
            json.dump(_conv_state, f, ensure_ascii=False, indent=2)


def new_conversation(title="新对话"):
    s = load_conversations()
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    conv = {"id": cid, "title": title, "created_at": now, "updated_at": now, "messages": []}
    s["conversations"][cid] = conv
    save_conversations()
    return conv


def list_conversations():
    s = load_conversations()
    convs = list(s["conversations"].values())
    convs.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
    return [
        {"id": c["id"], "title": c["title"],
         "updated_at": c.get("updated_at", 0), "messages": len(c.get("messages", []))}
        for c in convs
    ]


def get_conversation(cid):
    return load_conversations()["conversations"].get(cid)


def delete_conversation(cid):
    s = load_conversations()
    if cid in s["conversations"]:
        del s["conversations"][cid]
        save_conversations()
        return True
    return False


def append_message(cid, role, content, steps=None):
    """追加一条消息到对话；对话不存在则自动新建。返回对话对象。"""
    s = load_conversations()
    c = s["conversations"].get(cid)
    if not c:
        c = new_conversation()
        cid = c["id"]
    msg = {"role": role, "content": content, "ts": time.time()}
    if steps is not None:
        msg["steps"] = steps
    # 首条用户消息自动作为对话标题
    if role == "user" and len(c["messages"]) == 0:
        c["title"] = (content[:24] + ("…" if len(content) > 24 else "")) or "新对话"
    c["messages"].append(msg)
    c["updated_at"] = time.time()
    save_conversations()
    return c

