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
    body = dict(body)
    body.pop("fallback", None)  # fallback 是网关内部标记，不能传给上游
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
    """兜底到 OpenCode Zen 匿名通道（零门槛，无需任何 Key）。

    依次尝试该平台全部免费模型，任一成功即用；全部失败给出清晰人话提示，
    而不是扔一串难懂的 HTTP 报错。免费档可能被下线/限流，所以多试几个候选更稳。
    """
    info = PROVIDERS[FALLBACK_PROVIDER]
    url = info["base_url"].rstrip("/") + "/chat/completions"
    candidates = [m for m in (info.get("free_models") or []) if not m.startswith("#")]
    if FALLBACK_MODEL not in candidates:
        candidates.insert(0, FALLBACK_MODEL)
    errors = []
    for mid in candidates:
        out = dict(payload)
        out["model"] = mid
        out.pop("fallback", None)
        try:
            code, j = _do_request(url, _build_headers(info, ""), out)
            if code == 200:
                usage = j.get("usage") or {}
                add_usage(FALLBACK_PROVIDER, int(usage.get("total_tokens", 0) or 0))
                return code, j, FALLBACK_PROVIDER + "(fallback:" + mid + ")"
            err = json.dumps(j, ensure_ascii=False)[:200]
            errors.append(f"{mid}->{code}:{err}")
        except Exception as e:
            errors.append(f"{mid}->{e}")
    # 给错误分类，输出人话建议而不是纯 HTTP 码
    hints = []
    has_1010 = any("1010" in e or "access denied" in e.lower() for e in errors)
    has_403 = any("->403:" in e for e in errors)
    has_429 = any("->429:" in e for e in errors)
    if has_1010 or has_403:
        hints.append(
            "OpenCode Zen 匿名通道被拒绝访问（403 / error code 1010）。"
            "这通常是 Cloudflare 风控或该免费档对当前 IP/匿名用户关闭了入口。"
        )
    if has_429:
        hints.append("匿名通道也触发限流（429），免费额度已用完或请求太频繁。")
    if not hints:
        hints.append("兜底通道（OpenCode Zen 匿名）所有免费模型均不可用，可能免费档已下线或网络受限。")

    suggestion = (
        "建议：① 在设置页把默认模型换回你已配 token 的平台（如商汤 sensenova-6.8-flash-lite），"
        "避免走到匿名兜底；② 若是商汤 429，可等几分钟再试；③ 给 OpenCode Zen 绑定 GitHub 账号拿 Key 后走鉴权通道。"
    )

    return (502,
            {"error": {"message": " ".join(hints) + " " + suggestion +
               " 详情: " + " | ".join(errors)}},
            FALLBACK_PROVIDER)


def test_connection(provider_key: str):
    """设置页「测连通」：遍历该平台的免费模型，返回第一个能通的。"""
    info = PROVIDERS.get(provider_key)
    if info is None:
        return False, f"未知 provider: {provider_key}"
    token = get_token(provider_key)
    if info["requires_key"] and not token:
        return False, "该平台需要 API Key，请先配置后再测"

    models = [m for m in (info.get("free_models") or []) if not m.startswith("#")]
    if not models:
        models = ["gpt-oss-20b"]

    try:
        headers = _build_headers(info, token)
    except RuntimeError as e:
        return False, str(e)
    url = info["base_url"].rstrip("/") + "/chat/completions"

    last_err = ""
    for model_id in models:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "fallback": False,
        }
        try:
            code, j = _do_request(url, headers, payload)
            if code == 200:
                return True, f"连通成功（{info['name']}，模型 {model_id}）"
            err = json.dumps(j, ensure_ascii=False)[:300]
            last_err = f"模型 {model_id} 返回 {code}: {err}"
            if code == 410:
                last_err += "；该模型免费端点可能已弃用，将尝试下一个候选模型"
            elif code == 400:
                last_err += (
                    "；通常是模型 ID 不存在或请求参数不兼容。"
                    "请检查模型名是否和官网代码示例完全一致（区分大小写、横杠、作者前缀），"
                    "并确认该模型 Free Endpoint 未下线。"
                )
        except Exception as e:
            last_err = f"模型 {model_id} 请求异常: {e}"

    # 全部失败：给出指向 discover 页的提示（对 NVIDIA NIM 等尤其有用）
    hint = ""
    if info.get("discover_url"):
        hint = f"；请到官方发现页查看最新可用模型：{info['discover_url']}"
    return False, f"所有候选模型均连通失败。{last_err}{hint}"


def _parse_sse_line(line: str):
    """解析一行 SSE：返回 JSON dict / 字符串 '[DONE]' / 或 None（注释/空行）。"""
    s = line.strip()
    if not s.startswith("data:"):
        return None
    data = s[len("data:"):].strip()
    if data == "[DONE]":
        return "[DONE]"
    try:
        return json.loads(data)
    except Exception:
        return None


def _stream_once(url, headers, body, provider_key):
    """对单个上游发 stream 请求，yield 文本增量(str)。

    逻辑：逐块读取 chunked 响应，按行解析 SSE（data: {...} / [DONE]）；
    若上游忽略了 stream、直接吐完整 JSON，则在流结束后整体解析并一次性 yield，
    实现「非 SSE 降级」。用量在结束时记一次。
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        pending = ""
        buffer = ""
        usage = None
        got = False
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", "ignore")
            buffer += text
            pending += text
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                p = _parse_sse_line(line)
                if p == "[DONE]":
                    break
                if isinstance(p, dict):
                    d = (p.get("choices") or [{}])[0].get("delta") or {}
                    c = d.get("content") or ""
                    if c:
                        got = True
                        yield c
                    u = p.get("usage")
                    if u:
                        usage = u
        if usage:
            add_usage(provider_key, int((usage or {}).get("total_tokens", 0) or 0))
        # 上游没返回任何 token（可能忽略了 stream，直接吐完整 JSON）
        if not got and buffer.strip():
            try:
                j = json.loads(buffer)
                if isinstance(j, dict):
                    c = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    if c:
                        add_usage(provider_key, int(((j.get("usage") or {}).get("total_tokens", 0) or 0)))
                        yield c
                    else:
                        err = (j.get("error") or {}).get("message")
                        if err:
                            yield "[错误] " + err
            except Exception:
                pass


def chat_completion_stream(payload: dict):
    """流式版 chat_completion：yield 文本片段(str)。

    路由/鉴权/fallback 复用与非流式一致；主平台失败（且允许 fallback）时
    降级到 OpenCode Zen 匿名通道（也走 stream）。上游不支持 stream 时，
    _stream_once 内部降级为一次性完整文本。

    注意：这里把 fallback 做"透明"处理——主平台挂掉时不把错误吐给调用方，
    而是继续试兜底；只有兜底也失败才一次性返回聚合错误。
    """
    model = payload.get("model", "")
    default_provider, _ = get_defaults()
    provider_key, model_id, info = get_provider(model, default_provider)
    if info is None:
        yield f"[错误] 未知 provider: {provider_key}"
        return

    want_fallback = bool(payload.get("fallback")) and provider_key != FALLBACK_PROVIDER
    plan = [(provider_key, model_id)]
    if want_fallback:
        fb_info = PROVIDERS[FALLBACK_PROVIDER]
        fb_models = [m for m in (fb_info.get("free_models") or []) if not m.startswith("#")]
        fb_model = FALLBACK_MODEL if FALLBACK_MODEL in fb_models else (fb_models[0] if fb_models else FALLBACK_MODEL)
        plan.append((FALLBACK_PROVIDER, fb_model))

    last_err = ""
    for pk, mid in plan:
        pinfo = PROVIDERS[pk]
        token = get_token(pk)
        try:
            headers = _build_headers(pinfo, token)
        except RuntimeError as e:
            last_err = f"{pk}->{e}"
            continue
        url = pinfo["base_url"].rstrip("/") + "/chat/completions"
        out = dict(payload)
        out["model"] = mid
        out["stream"] = True
        out.pop("fallback", None)
        try:
            for piece in _stream_once(url, headers, out, pk):
                yield piece
            return
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            last_err = f"{pk}->{e.code}:{err_body}"
            continue
        except Exception as e:
            last_err = f"{pk}->{e}"
            continue

    # 最终兜底也失败：给人话建议
    detail = last_err
    advice = ""
    if "1010" in detail or "403" in detail:
        advice = "OpenCode Zen 匿名通道被 403 / 1010 拒绝（Cloudflare 风控或匿名入口关闭）。"
    if "429" in detail:
        advice += (" " if advice else "") + "平台或兜底通道触发限流（429）。"
    if not advice:
        advice = "主平台与兜底通道均调用失败。"
    yield (
        "[错误] " + advice +
        " 建议：① 在设置页把默认模型换回已配 token 的平台（如商汤 sensenova-6.8-flash-lite）；"
        "② 若刚触发 429，等几分钟再试；③ 给 OpenCode Zen 绑 GitHub Key 走鉴权通道。"
        f" 详情: {detail}"
    )
