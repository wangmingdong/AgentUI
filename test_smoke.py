# -*- coding: utf-8 -*-
"""
本地冒烟测试（不依赖外网）
=======================
起一个本地 mock 的 OpenAI 兼容服务，验证：
1. 网关能正确路由 + 带鉴权 + 转发 + 记用量
2. Agent 接口能跑通（mock 模型不返回工具调用 -> 直接出最终回答）
"""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import catalog
import store
import gateway
import agent

MOCK_PORT = 8799
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization", "")
        # 校验：要求带 Bearer，且 model 透传
        resp = {
            "choices": [{"message": {"role": "assistant",
                                     "content": f"mock回包(model={body.get('model')}, auth={'有' if auth else '无'})"}}],
            "usage": {"total_tokens": 7},
        }
        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    start_mock()
    # 注册一个临时 mock provider
    catalog.PROVIDERS["mockprov"] = {
        "name": "Mock", "base_url": MOCK_BASE, "auth": "bearer",
        "requires_key": True, "free": True, "free_models": ["test-model"],
        "rate_limit": "-", "note": "测试用",
    }
    store.set_token("mockprov", "test-key")
    store.set_defaults("mockprov", "test-model")

    # 1) 网关转发
    code, j, used = gateway.chat_completion({
        "model": "mockprov/test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "fallback": False,
    })
    assert code == 200, f"网关应 200，实际 {code}: {j}"
    content = j["choices"][0]["message"]["content"]
    assert "mock回包" in content and "auth=有" in content, content
    print("[PASS] 网关转发 + Bearer 鉴权 OK ->", content)

    # 2) 用量记录
    usage = store.load()["usage"].get("mockprov", {})
    assert usage.get("calls", 0) >= 1, usage
    print("[PASS] 用量记录 OK ->", usage)

    # 3) Agent 循环
    steps = agent.run_agent("帮我写个 hello 程序")
    types = [s["type"] for s in steps]
    assert "done" in types, steps
    print("[PASS] Agent 循环 OK -> 步骤类型:", types)

    print("\n全部冒烟测试通过 ✅")


if __name__ == "__main__":
    main()
