# -*- coding: utf-8 -*-
"""
provider 目录（catalog）
===================
内置各大平台的接入信息，重点是「免费」通道。

业务逻辑（用人话讲）：
- 这个壳子的核心就是「统一中转」。你在设置页给某个平台贴上 token，
  它就变成了一个 OpenAI 兼容的接口；Agent 和聊天都只跟我们自己的
  网关说话，网关再按模型名把请求转发到对应平台。
- 每个平台有：base_url（OpenAI 兼容地址）、auth（怎么带鉴权）、
  免费模型列表、限速说明、是否需要 key。
- 模型命名约定：`<provider_key>/<model_id>`，例如 `zen/deepseek-v4-flash-free`、
  `nim/z-ai/glm5.2`。只写 model_id 时走默认平台。

免费平台的「免费」大多指有额度/限速的免费档，不是无限。真正零门槛的是
OpenCode Zen：连 key 都不用（匿名按 IP 限速）就能调。
"""

PROVIDERS = {
    "opencode_zen": {
        "name": "OpenCode Zen",
        "base_url": "https://opencode.ai/zen/v1",
        "auth": "bearer_or_anonymous",   # 有 key 用 Bearer；没 key 匿名也能调
        "requires_key": False,
        "free": True,
        "free_models": [
            "nemotron-3-ultra-free",
            "mimo-v2.5-free",
            "deepseek-v4-flash-free",
            "big-pickle",
        ],
        "rate_limit": "匿名按 IP 限速（约 新用户 100~400/天，老用户 50~200/天）",
        "note": "免 Key 匿名也能调；带 GitHub 登录拿 Key 可用付费模型。最省事的零门槛入口。",
    },
    "sensenova": {
        "name": "商汤 SenseNova",
        "base_url": "https://token.sensenova.cn/v1",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "sensenova-6.7-flash-lite",
            "deepseek-v4-flash",
            "glm-5.2",
        ],
        "rate_limit": "sensenova-6.7-flash-lite 1500/5h；deepseek-v4-flash 500/5h",
        "note": "注册开放平台拿 API Key；token.sensenova.cn 是 OpenAI 兼容的 token 端点。",
    },
    "nvidia_nim": {
        "name": "英伟达 NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "discover_url": "https://build.nvidia.com/explore/discover",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            # 以下模型经 build.nvidia.com 验证 Free Endpoint = Available（2026-08-18）
            "z-ai/glm5.2",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "minimaxai/minimax-m3",
            "stepfun-ai/step-3.7-flash",
            # 已Deprecated/下线的免费模型（保留注释备忘）：
            # "deepseek-ai/deepseek-v4-flash" -> Free Endpoint Deprecated，调用会 410 Gone
            # "qwen/qwen3.5-122b-a10b"        -> Free Endpoint Deprecated
            # "stepfun-ai/step-3.5-flash"     -> Free Endpoint Deprecated
            # "moonshotai/kimi-k2.6"          -> 页面 404，正确名可能是 moonshotai/kimi-k2-instruct
        ],
        "rate_limit": "40 RPM",
        "note": "注册 NVIDIA 开发者账号拿 Key。官网发现页（查当前可用免费模型）：https://build.nvidia.com/explore/discover 。"
              "注意：base_url 必须是 integrate.api.nvidia.com/v1 才能调用。若测连通返回 410 Gone，通常表示该模型 Free Endpoint 已被弃用，"
              "请去 discover 页确认最新可用免费模型列表；不是 base_url 错误。",
    },
    "zhipu": {
        "name": "智谱 Zhipu / BigModel",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "GLM-4-Flash",
            "GLM-4.7-Flash",
            "GLM-4-Flash-250414",
        ],
        "rate_limit": "GLM-4-Flash 等免费档有并发/额度限制",
        "note": "智谱开放平台（bigmodel.cn）注册拿 Key；Flash 系列常免费用。也可以走 NVIDIA NIM 的 z-ai/glm5.2。",
    },
    "aliyun_bailian": {
        "name": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "qwen-plus",
            "qwen-turbo",
            "qwen-max",
            "qwen3-coder-plus",
        ],
        "rate_limit": "新用户有免费额度，按 token 计费",
        "note": "百炼控制台创建 API-Key；Qwen 系列中文与代码都不错。也可经 OpenRouter/ModelScope/NVIDIA 拿免费 Qwen。",
    },
    "minimax": {
        "name": "MiniMax（经 NVIDIA NIM 免费）",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "discover_url": "https://build.nvidia.com/explore/discover",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "minimaxai/minimax-m3",
        ],
        "rate_limit": "随 NVIDIA NIM 40 RPM",
        "note": "MiniMax 官方也开放 API，但免费额度需申请；最省事是走 NVIDIA NIM 的 minimaxai/minimax-m3 免费模型（同一个 NIM Key）。",
    },
    "stepfun": {
        "name": "阶跃星辰 StepFun（经 NVIDIA NIM 免费）",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "discover_url": "https://build.nvidia.com/explore/discover",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "stepfun-ai/step-3.7-flash",
            # "stepfun-ai/step-3.5-flash" -> Free Endpoint Deprecated
        ],
        "rate_limit": "随 NVIDIA NIM 40 RPM",
        "note": "阶跃官方也开放平台；免费路径走 NVIDIA NIM 的 stepfun-ai/step-3.x-flash（同一个 NIM Key）。也可走 OpenRouter/Kilo 免费档。",
    },
    "openrouter": {
        "name": "OpenRouter（聚合）",
        "base_url": "https://openrouter.ai/api/v1",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "openrouter/auto",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "cohere/north-mini-code:free",
            "poolside/laguna-s-2.1:free",
        ],
        "rate_limit": "20 RPM / 200 RPD（每模型）",
        "note": "一个 Key 打通上百家用模型，含很多 :free 档。适合当统一入口。",
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow（聚合）",
        "base_url": "https://api.siliconflow.cn/v1",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
            "Qwen/Qwen3-8B",
            "THUDM/glm-4-9b-chat",
            "THUDM/GLM-4.1V-9B-Thinking",
        ],
        "rate_limit": "1000 RPM（每模型）",
        "note": "国产聚合，Qwen/GLM/DeepSeek 免费模型多，中文友好。",
    },
    "modelscope": {
        "name": "ModelScope 魔搭（聚合）",
        "base_url": "https://api-inference.modelscope.cn/v1/",
        "auth": "bearer",
        "requires_key": True,
        "free": True,
        "free_models": [
            "ZhipuAI/GLM-5.1",
            "MiniMax/MiniMax-M2.5",
            "Qwen/Qwen3-235B-A22B-Instruct-2507",
            "Qwen/Qwen3-Coder-480B-A35B-Instruct",
            "deepseek-ai/DeepSeek-V4-Flash",
        ],
        "rate_limit": "2000 RPD",
        "note": "阿里系聚合，含智谱/阶跃/Qwen/DeepSeek 免费模型，额度较宽松。",
    },
}


# 模型命名用的短前缀（model 形如 "zen/deepseek-v4-flash-free"）
# key 是内部标识（也用于 token 存储），prefix 是给人用的短名。
PREFIX_TO_KEY = {
    "zen": "opencode_zen",
    "sensenova": "sensenova",
    "nim": "nvidia_nim",
    "zhipu": "zhipu",
    "bailian": "aliyun_bailian",
    "minimax": "minimax",
    "stepfun": "stepfun",
    "openrouter": "openrouter",
    "siliconflow": "siliconflow",
    "modelscope": "modelscope",
}
KEY_TO_PREFIX = {v: k for k, v in PREFIX_TO_KEY.items()}


def prefix_of(key: str) -> str:
    return KEY_TO_PREFIX.get(key, key)


def list_providers(config_tokens: dict) -> list:
    """给前端用的列表：标注哪些已经配了 token、哪些免费。"""
    out = []
    for key, p in PROVIDERS.items():
        has_key = bool(config_tokens.get(key, {}).get("api_key"))
        out.append({
            "key": key,
            "name": p["name"],
            "free": p["free"],
            "requires_key": p["requires_key"],
            "has_token": has_key,
            "base_url": p["base_url"],
            "discover_url": p.get("discover_url", ""),
            "auth": p["auth"],
            "prefix": prefix_of(key),
            "free_models": p["free_models"],
            "rate_limit": p["rate_limit"],
            "note": p["note"],
        })
    return out


def get_provider(model: str, default_provider: str):
    """
    解析模型名，返回 (provider_key, model_id, provider_info)。
    - 'zen/deepseek-v4-flash-free' -> ('opencode_zen', 'deepseek-v4-flash-free', info)
    - 'deepseek-v4-flash-free'     -> 用 default_provider，model_id 不变
    """
    if "/" in model:
        head, model_id = model.split("/", 1)
        provider_key = PREFIX_TO_KEY.get(head, head)  # head 可能是前缀，也可能是内部 key
    else:
        provider_key, model_id = default_provider, model
    info = PROVIDERS.get(provider_key)
    return provider_key, model_id, info
