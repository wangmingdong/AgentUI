# -*- coding: utf-8 -*-
"""
统一网关（gateway）
=================
把「各家平台」收敛成一个 OpenAI 兼容接口。Agent 和聊天页只跟本网关对话，
网关负责：按模型名路由到对应平台、正确带鉴权、失败自动降级、记用量。

为什么自己写而不直接上 LiteLLM：
- 这个壳子的免费平台大多已是 OpenAI 兼容，转发逻辑很简单；
- 用标准库零依赖，保证在你机器上 `python server.py` 就能跑，先把闭环跑通；
- 后续想换 LiteLLM/New API 当内核，只需替换本文件的转发实现，接口不变。
"""

import json
import traceback
import urllib.error
import urllib.request

from catalog import PROVIDERS, get_provider
from store import get_token, get_defaults, add_usage

HTTP_TIMEOUT = 120
FALLBACK_PROVIDER = "opencode_zen"
FALLBACK_MODEL = "deepseek-v4-flash-free"


def _do_request(url: str, headers: dict, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        code = resp.getcode()
        text = resp.read().decode("utf-8", "ignore")
    try:
        j = json.loads(text)
    except Exception:
        j = {"raw": text}
    return code, j


def _build_headers(info: dict, token: str):
    headers = {"Content-Type": "application/json"}
    # bearer_or_anonymous 且无 token -> 不加鉴权（如 OpenCode Zen 匿名通道）
    if info["auth"] == "bearer_or_anonymous" and not token:
        return headers
    if token:
        headers["Authorization"] = "Bearer " + token
    elif info["requires_key"]:
        raise RuntimeError(f"{info['name']} 需要 API Key，请先在设置页配置")
    return headers


def chat_completion(payload: dict):
    """
    转发一次 chat/completions。
    返回 (status_code, json_body, provider_used)
    - 支持 model 形如 'zen/deepseek-v4-flash-free' 或裸 model_id（用默认平台）
    - payload 里带 "fallback": true 时，主平台失败会兜底到 OpenCode Zen 匿名通道
    """
    model = payload.get("model", "")
    default_provider, _ = get_defaults()
    provider_key, model_id, info = get_provider(model, default_provider)

    if info is None:
        return 400, {"error": {"message": f"未知 provider: {provider_key}"}}, provider_key

    token = get_token(provider_key)
    try:
        headers = _build_headers(info, token)
    except RuntimeError as e:
        return 401, {"error": {"message": str(e)}}, provider_key

    url = info["base_url"].rstrip("/") + "/chat/completions"
    out = dict(payload)
    out["model"] = model_id
    out.pop("fallback", None)

    try:
        code, j = _do_request(url, headers, out)
        if code == 200:
            usage = j.get("usage") or {}
            add_usage(provider_key, int(usage.get("total_tokens", 0) or 0))
        if code != 200 and payload.get("fallback") and provider_key != FALLBACK_PROVIDER:
            return _fallback(payload)
        return code, j, provider_key
    except urllib.error.HTTPError as e:
        err_text = e.read().decode("utf-8", "ignore")
        try:
            j = json.loads(err_text)
        except Exception:
            j = {"error": {"message": err_text[:500]}}
        if payload.get("fallback") and provider_key != FALLBACK_PROVIDER:
            return _fallback(payload)
        return e.code, j, provider_key
    except Exception as e:
        if payload.get("fallback") and provider_key != FALLBACK_PROVIDER:
            return _fallback(payload)
        return 502, {"error": {"message": f"网关转发失败: {e}", "detail": traceback.format_exc()[-300:]}}, provider_key


def _fallback(payload: dict):
    """兜底到 OpenCode Zen 匿名通道（零门槛，无需任何 Key）。"""
    info = PROVIDERS[FALLBACK_PROVIDER]
    url = info["base_url"].rstrip("/") + "/chat/completions"
    out = dict(payload)
    out["model"] = FALLBACK_MODEL
    out.pop("fallback", None)
    try:
        code, j = _do_request(url, _build_headers(info, ""), out)
        if code == 200:
            usage = j.get("usage") or {}
            add_usage(FALLBACK_PROVIDER, int(usage.get("total_tokens", 0) or 0))
        return code, j, FALLBACK_PROVIDER + "(fallback)"
    except Exception as e:
        return 502, {"error": {"message": f"兜底通道也失败: {e}"}}, FALLBACK_PROVIDER


def test_connection(provider_key: str):
    """设置页「测连通」：用该平台的免费模型发一条极短请求。"""
    info = PROVIDERS.get(provider_key)
    if info is None:
        return False, f"未知 provider: {provider_key}"
    token = get_token(provider_key)
    if info["requires_key"] and not token:
        return False, "该平台需要 API Key，请先配置后再测"
    model_id = (info["free_models"][0] if info["free_models"]
                else (info.get("free_models") or ["gpt-oss-20b"])[0])
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "fallback": False,
    }
    # 直接调用转发（不走 fallback，test 需明确结果）
    try:
        headers = _build_headers(info, token)
    except RuntimeError as e:
        return False, str(e)
    url = info["base_url"].rstrip("/") + "/chat/completions"
    try:
        code, j = _do_request(url, headers, payload)
        if code == 200:
            return True, f"连通成功（{info['name']}，模型 {model_id}）"
        return False, f"返回 {code}：" + json.dumps(j, ensure_ascii=False)[:300]
    except Exception as e:
        return False, f"请求异常: {e}"
