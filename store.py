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
        "default_provider": "sensenova",
        "default_model": "sensenova-6.8-flash-lite",   # 商汤 6.8 flash lite 免费、看图、流式、工具调用都支持；限额 1500/5h 比 deepseek-v4-flash 更宽松
        "usage": {},           # {provider_key: {"calls": n, "tokens": n}}
        "user_workspaces": [], # 用户额外添加的工作目录（绝对路径）
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
# 工作区间（workspaces）
# 主工作区固定为 agent_workspace/；用户可在 Agent 页「增加工作区间」，
# 把 Agent 指向自己已有的项目目录。Agent 只允许在这些目录及其子目录内活动。
# ============================================================

def get_workspaces():
    """返回所有允许的工作区绝对路径（主工作区 + 用户添加的，去重）。"""
    s = load()
    bases = [os.path.normpath(WORKSPACE_DIR)]
    for p in s.get("user_workspaces", []):
        np_ = os.path.normpath(p)
        if np_ not in bases:
            bases.append(np_)
    return bases


def add_workspace(path: str):
    """新增一个工作区间。要求为已存在的绝对路径；返回 (ok, msg)。"""
    if not path:
        return False, "路径为空"
    ap = os.path.abspath(path)
    if not os.path.isdir(ap):
        return False, f"目录不存在：{ap}"
    s = load()
    bases = [os.path.normpath(WORKSPACE_DIR)]
    if ap in bases:
        return False, "这就是默认主工作区，无需重复添加"
    for p in s.get("user_workspaces", []):
        if os.path.normpath(p) == ap:
            return False, "该工作区间已存在"
    s.setdefault("user_workspaces", []).append(ap)
    save()
    return True, f"已添加工作区间：{ap}"


def remove_workspace(path: str):
    """移除用户添加的工作区间（主工作区不可删）。返回 (ok, msg)。"""
    ap = os.path.abspath(path) if path else ""
    s = load()
    for i, p in enumerate(s.get("user_workspaces", [])):
        if os.path.normpath(p) == ap:
            s["user_workspaces"].pop(i)
            save()
            return True, f"已移除：{ap}"
    if os.path.normpath(ap) == os.path.normpath(WORKSPACE_DIR):
        return False, "主工作区不可删除"
    return False, "未找到该工作区间"


def is_valid_workspace(path: str):
    """path 是否为允许的工作区间之一。"""
    if not path:
        return False
    np_ = os.path.normpath(path)
    return np_ in get_workspaces()


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


def append_message(cid, role, content, steps=None, attachments=None):
    """追加一条消息到对话；对话不存在则自动新建。返回对话对象。"""
    s = load_conversations()
    c = s["conversations"].get(cid)
    if not c:
        c = new_conversation()
        cid = c["id"]
    msg = {"role": role, "content": content, "ts": time.time()}
    if steps is not None:
        msg["steps"] = steps
    if attachments is not None:
        msg["attachments"] = attachments
    # 首条用户消息自动作为对话标题
    if role == "user" and len(c["messages"]) == 0:
        c["title"] = (content[:24] + ("…" if len(content) > 24 else "")) or "新对话"
    c["messages"].append(msg)
    c["updated_at"] = time.time()
    save_conversations()
    return c


def rename_conversation(cid, title):
    """重命名对话标题。"""
    s = load_conversations()
    c = s["conversations"].get(cid)
    if not c:
        return False
    c["title"] = (title or "新对话").strip()[:60]
    c["updated_at"] = time.time()
    save_conversations()
    return True


def delete_message(cid, idx):
    """删除对话中第 idx 条消息（按当前顺序）。"""
    s = load_conversations()
    c = s["conversations"].get(cid)
    if not c:
        return False
    msgs = c.get("messages", [])
    if 0 <= idx < len(msgs):
        msgs.pop(idx)
        c["updated_at"] = time.time()
        save_conversations()
        return True
    return False


def delete_last_assistant(cid):
    """重生成前处理：删掉对话末尾最后一条 assistant 消息，返回末尾 user 消息内容。"""
    s = load_conversations()
    c = s["conversations"].get(cid)
    if not c:
        return None
    msgs = c.get("messages", [])
    while msgs and msgs[-1].get("role") != "assistant":
        msgs.pop()
    if msgs and msgs[-1].get("role") == "assistant":
        msgs.pop()
    last_user = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break
    c["updated_at"] = time.time()
    save_conversations()
    return last_user


