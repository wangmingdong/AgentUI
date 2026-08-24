# -*- coding: utf-8 -*-
"""
Agent 执行层（agent）
====================
一个轻量的「写程序 / 干活的循环」。思路复用 OpenCode/Cline：让模型当「大脑」做规划，
我们提供几个工具（读文件、写文件、跑命令、列目录）当「手脚」，循环执行直到任务完成。

为什么用 prompt 式工具协议而不是原生 function calling：
- 免费模型对 function calling 支持参差，统一的 <call> 文本格式最稳；
- 整个循环完全可控，方便加审批、日志、沙箱。

安全边界：所有文件/命令都锁在「当前工作区间」目录内，路径想逃出去会被拒绝。
跑命令是真实执行，只在当前工作区间内，且有超时；后续可加「危险命令需确认」。

新增能力：
- 工作区间可指定（默认 agent_workspace/，也可由用户在界面「增加工作区间」指向自己的项目目录）；
- 支持图片：图片会存进当前工作区的 uploads/ 目录，并（在开启「视觉输入」时）作为多模态
  内容直接传给支持视觉的模型，让模型真正「看见」图片。
"""

import json
import os
import re
import subprocess

from store import WORKSPACE_DIR, get_defaults, is_valid_workspace
from gateway import chat_completion, chat_completion_stream
from catalog import PROVIDERS

MAX_ITER = 12
CMD_TIMEOUT = 60

SYSTEM_PROMPT = """你是一个运行在用户本地的编程 Agent。你可以读写文件、执行终端命令来完成任务。
当前工作目录（工作区间）是：
{workspace}
所有相对路径都相对于它。

当你需要使用工具时，必须严格按以下格式输出一个调用块：
<call>
{{"name": "工具名", "args": {{参数}}}}
</call>

重要约束：
- 一次只调用一个工具，不要把多个调用拼在一起，也不要输出 JSON 数组。
- 必须完整包裹在 <call>...</call> 标签内；只输出裸 JSON 会被视为最终回答，不会执行工具。

可用工具：
- read_file: {{"path": "相对路径"}} -> 读取文件内容
- write_file: {{"path": "相对路径", "content": "完整文件内容"}} -> 创建/覆盖文件
- run_command: {{"command": "shell 命令"}} -> 在工作目录执行命令，返回 stdout/stderr
- list_dir: {{"path": "相对路径或留空"}} -> 列出目录内容

规则：
1. 先理解任务，再动手；小步快跑，每步验证。
2. 写代码后务必用 run_command 跑测试/编译确认能跑通。
3. 不需要工具时，直接给出最终回答（不要输出 <call>）。
4. 用中文和用户交流。
5. 用户可能附带图片（已存入工作区的 uploads/ 目录）。若任务是关于图片的，
   可用 read_file 查看或结合上下文处理；若当前模型支持图像理解，图片已直接传入。
6. 当你已经获得足够信息时，必须立即停止调用工具，直接输出清晰、完整的最终回答/总结。
   不要只罗列中间步骤或工具结果，用户需要看到可读的结论、代码解释或操作结果。
"""

TOOL_NAMES = {"read_file", "write_file", "run_command", "list_dir"}


def _safe_path(rel: str, base: str) -> str:
    """把路径解析到 base 工作区内，越界则抛错。

    规则（用人话）：
    - 相对路径：直接 join 到 base 工作区。
    - 绝对路径：如果它落在 base 工作区之内（含工作区自身），自动 rebasing 成
      相对路径再解析——这样模型即便传了完整绝对路径（如 E:\\workspace\\agentUI\\foo.py），
      只要它确实在当前工作区间内，也能正常工作，不会被误判越界。
    - 一旦解析结果逃出 base 工作区（含用 ../ 往上层跳），一律拒绝。
    """
    rel = rel or "."
    base_real = os.path.realpath(base)
    # 绝对路径且位于 base 之内 -> 改成相对 base 的写法，避免 os.path.join 把它当逃逸路径
    if os.path.isabs(rel):
        cand = os.path.realpath(rel)
        if cand == base_real or cand.startswith(base_real + os.sep):
            rel = os.path.relpath(cand, base_real)
    target = os.path.realpath(os.path.join(base_real, rel))
    if target != base_real and not target.startswith(base_real + os.sep):
        raise ValueError(
            f"路径越界，拒绝访问：{rel}。请只使用相对于当前工作区间（{base_real}）的路径。")
    return target


def _run_tool(name: str, args: dict, base: str) -> str:
    if name == "read_file":
        p = _safe_path(args.get("path", ""), base)
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {args.get('path')}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 12000:
            content = content[:12000] + "\n...（内容过长已截断）"
        return content

    if name == "write_file":
        p = _safe_path(args.get("path", ""), base)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args.get("content", ""))
        return f"[成功] 已写入 {args.get('path')}（{len(args.get('content', ''))} 字符）"

    if name == "run_command":
        cmd = args.get("command", "")
        try:
            r = subprocess.run(cmd, shell=True, cwd=base,
                               capture_output=True, text=True, timeout=CMD_TIMEOUT)
            out = (r.stdout or "") + (r.stderr or "")
            if len(out) > 8000:
                out = out[:8000] + "\n...（输出过长已截断）"
            return f"[exit={r.returncode}]\n{out}"
        except subprocess.TimeoutExpired:
            return f"[错误] 命令超时（>{CMD_TIMEOUT}s）"
        except Exception as e:
            return f"[错误] {e}"

    if name == "list_dir":
        p = _safe_path(args.get("path", ""), base)
        if not os.path.isdir(p):
            return f"[错误] 目录不存在: {args.get('path')}"
        items = sorted(os.listdir(p))
        return "\n".join(items) if items else "[空目录]"

    return f"[错误] 未知工具: {name}"


def _extract_json_objects(text: str):
    """从文本中安全提取所有顶层 JSON 对象/数组（支持嵌套花括号），返回字符串列表。"""
    results = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            in_str = False
            esc = False
            start = i
            while i < n:
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            results.append(text[start:i + 1])
                            break
                i += 1
            i += 1
        elif text[i] == "[":
            depth = 0
            in_str = False
            esc = False
            start = i
            while i < n:
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                        if depth == 0:
                            results.append(text[start:i + 1])
                            break
                i += 1
            i += 1
        else:
            i += 1
    return results


def _extract_call(text: str):
    """从模型输出里提取第一个工具调用，返回 (name, args) 或 None。

    兼容三种模型输出形态：
    1. 规范格式：<call>{"name":"...","args":{...}}</call>
    2. 裸 JSON 对象：{"name":"...","args":{...}}
    3. JSON 数组或连写多个对象：[{...},{...}] 或 {...}{...}{...}
       只取第一个合法工具调用，其余忽略（一次只执行一个工具）。
    """
    # 1. 优先规范 <call> 格式
    m = re.search(r"<call>\s*(\{.*?\})\s*</call>", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            if obj.get("name") in TOOL_NAMES:
                return obj["name"], obj.get("args", {})
        except Exception:
            pass

    # 2. 扫描文本中所有 JSON 对象/数组
    for raw in _extract_json_objects(text):
        try:
            obj_or_list = json.loads(raw)
        except Exception:
            continue
        candidates = []
        if isinstance(obj_or_list, dict):
            candidates.append(obj_or_list)
        elif isinstance(obj_or_list, list):
            candidates.extend(obj_or_list)
        for obj in candidates:
            if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES:
                return obj["name"], obj.get("args", {})

    return None


def _history_to_messages(history):
    """把存储的对话历史转成 OpenAI 消息格式（只取 role/content，过滤空内容）。

    历史里 assistant 消息的 content 是对话最终总结文本，user 消息是原始提问，
    足够支撑多轮上下文；工具调用的中间过程不回灌，避免上下文爆炸。
    最多取最近 30 条，防止超长。
    """
    if not history:
        return []
    out = []
    for m in history[-30:]:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


def run_agent(task: str, workspace: str = None, images: list = None,
              vision_mode: bool = False, history: list = None):
    """
    执行一个任务，返回步骤列表（供 UI 展示）：
    每步: {"type": "llm"|"tool"|"done", "text", "tool", "args", "output"}

    workspace: 当前工作区间绝对路径（必须是允许区间之一，否则退回主工作区）
    images:    已存盘的图片列表 [{"rel":..., "data":base64, "mime":...}]
    vision_mode: 是否把图片作为多模态内容直接传给模型（需模型支持视觉）
    history:   本轮之前的对话历史（store 里的 messages），用于多轮上下文
    """
    # 校验工作区间，非法则退回主工作区
    ws = workspace if is_valid_workspace(workspace) else WORKSPACE_DIR
    images = images or []

    # 任务文本：附带图片引用
    full_task = task
    if images:
        rels = [im.get("rel", "") for im in images if im.get("rel")]
        if rels:
            full_task += "\n\n[用户附带图片，已存入工作区 uploads/ 目录，相对路径：\n" + "\n".join(rels) + "\n]"
            if not vision_mode:
                full_task += "\n当前未开启「视觉输入」，模型看不到图本身；如需让模型看图，请改用支持多模态的模型并勾选「视觉输入」。你可用 read_file/list_dir 处理这些文件。"

    # 构造消息：系统提示 + 历史上下文 + 当前任务
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(workspace=os.path.realpath(ws))},
    ]
    # 历史上下文（多轮记忆）：若历史最后一条已是当前 user 提问（重生成场景）则跳过，避免重复
    hist = _history_to_messages(history)
    if hist and not (hist[-1]["role"] == "user" and hist[-1]["content"] == full_task):
        messages.extend(hist)
    if vision_mode and images:
        # 多模态：把图片以 data URL 内联进首条 user 消息
        content = [{"type": "text", "text": full_task}]
        for im in images:
            if im.get("data") and im.get("mime"):
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"},
                })
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": full_task})

    steps = []
    default_provider, default_model = get_defaults()

    # 视觉模式检查：当前模型必须在该平台的 vision_models 列表里
    if vision_mode and images:
        info = PROVIDERS.get(default_provider, {})
        vision_models = info.get("vision_models") or []
        if vision_models and default_model not in vision_models:
            steps.append({
                "type": "error",
                "text": (
                    f"当前默认模型「{default_model}」不支持图片输入。"
                    f"请在设置页换一个支持视觉的模型（如 {', '.join(vision_models)}），"
                    f"或取消勾选「视觉输入」让 Agent 把图片当文件处理。"
                ),
            })
            return steps

    done = False
    used_provider = default_provider
    for i in range(MAX_ITER):
        payload = {
            "model": default_model,
            "messages": messages,
            "temperature": 0.3,
            "fallback": True,
        }
        # 有视觉图片时禁用 fallback：避免降级到不支持视觉的模型导致报错
        if vision_mode and images:
            payload["fallback"] = False

        code, j, used = chat_completion(payload)
        used_provider = used
        if code != 200:
            err = json.dumps(j, ensure_ascii=False)[:500]
            text = f"模型调用失败（{used}）: {err}"
            if "OpenCode Zen" in err or "兜底" in err or "1010" in err:
                text += "\n\n👉 建议到设置页把默认模型换回已配 token 的平台（如商汤 sensenova-6.8-flash-lite），避免走匿名兜底通道。"
            steps.append({"type": "error", "text": text})
            break

        content = (j.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        call = _extract_call(content)

        if call is None:
            steps.append({"type": "llm", "text": content, "provider": used})
            steps.append({"type": "done", "text": content or "（模型未返回可见文本）"})
            done = True
            break

        name, args = call
        steps.append({"type": "llm", "text": content, "provider": used})
        try:
            output = _run_tool(name, args, ws)
        except ValueError as e:
            output = f"[拒绝] {e}"
        steps.append({"type": "tool", "tool": name, "args": args, "output": output})

        # 把工具结果作为 user 消息回灌，让模型继续
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"工具 {name} 的结果：\n{output}"})

    # 若达到最大轮数仍未给出结论，强制让模型基于已有信息做最终总结
    if not done:
        messages.append({
            "role": "user",
            "content": "已达到最大思考轮数。请基于你已经收集到的信息，直接给出最终回答/总结，不要调用任何工具。"
        })
        payload = {
            "model": default_model,
            "messages": messages,
            "temperature": 0.3,
            "fallback": True,
        }
        if vision_mode and images:
            payload["fallback"] = False
        code, j, used = chat_completion(payload)
        if code == 200:
            content = (j.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
            steps.append({"type": "llm", "text": content, "provider": used})
            steps.append({"type": "done", "text": content or "（模型未返回可见文本，请查看右侧执行步骤）"})
        else:
            err = json.dumps(j, ensure_ascii=False)[:500]
            text = f"模型调用失败（{used}）: {err}"
            if "OpenCode Zen" in err or "兜底" in err or "1010" in err:
                text += "\n\n👉 建议到设置页把默认模型换回已配 token 的平台（如商汤 sensenova-6.8-flash-lite），避免走匿名兜底通道。"
            steps.append({"type": "error", "text": text})

    return steps


def run_agent_stream(task: str, workspace: str = None, images: list = None,
                     vision_mode: bool = False, files: list = None, history: list = None):
    """流式版 run_agent：yield 事件字典的生成器。

    事件类型：
    - {"event":"llm_start","iter": i}               一轮 LLM 开始
    - {"event":"token","text": "..."}              文本增量（逐块，用于打字机）
    - {"event":"tool","tool": name,"args": args}   工具调用开始
    - {"event":"tool_result","output": "..."}      工具执行结果
    - {"event":"done","text": "..."}               最终回答（完整文本）
    - {"event":"error","text": "..."}              出错

    files: @引用的相对路径列表，会被读入并作为上下文注入 prompt（安全锁在 workspace 内）。
    """
    ws = workspace if is_valid_workspace(workspace) else WORKSPACE_DIR
    images = images or []
    files = files or []

    # 任务文本：附带图片引用 + @文件引用
    full_task = task
    if images:
        rels = [im.get("rel", "") for im in images if im.get("rel")]
        if rels:
            full_task += "\n\n[用户附带图片，已存入工作区 uploads/ 目录，相对路径：\n" + "\n".join(rels) + "\n]"
            if not vision_mode:
                full_task += "\n当前未开启「视觉输入」，模型看不到图本身；如需让模型看图，请改用支持多模态的模型并勾选「视觉输入」。你可用 read_file/list_dir 处理这些文件。"
    if files:
        refs = []
        for f in files:
            try:
                p = _safe_path(f, ws)
            except ValueError as e:
                refs.append(f"[引用文件 {f} 越界被忽略：{e}]")
                continue
            if not os.path.isfile(p):
                refs.append(f"[引用文件 {f} 不存在]")
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception as e:
                refs.append(f"[引用文件 {f} 读取失败：{e}]")
                continue
            if len(content) > 8000:
                content = content[:8000] + "\n...（内容过长已截断）"
            refs.append(f"### 引用文件：{f}\n```\n{content}\n```")
        if refs:
            full_task += "\n\n[用户引用了以下工作区文件作为上下文]\n" + "\n\n".join(refs)

    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(workspace=os.path.realpath(ws))}]
    # 历史上下文（多轮记忆）：若历史最后一条已是当前提问（重生成场景）则跳过，避免重复
    hist = _history_to_messages(history)
    if hist and not (hist[-1]["role"] == "user" and hist[-1]["content"] == full_task):
        messages.extend(hist)
    if vision_mode and images:
        content = [{"type": "text", "text": full_task}]
        for im in images:
            if im.get("data") and im.get("mime"):
                content.append({"type": "image_url", "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": full_task})

    default_provider, default_model = get_defaults()

    if vision_mode and images:
        info = PROVIDERS.get(default_provider, {})
        vision_models = info.get("vision_models") or []
        if vision_models and default_model not in vision_models:
            yield {"event": "error", "text": (
                f"当前默认模型「{default_model}」不支持图片输入。"
                f"请在设置页换一个支持视觉的模型（如 {', '.join(vision_models)}），"
                f"或取消勾选「视觉输入」让 Agent 把图片当文件处理。")}
            return

    done = False
    for i in range(MAX_ITER):
        payload = {"model": default_model, "messages": messages, "temperature": 0.3, "fallback": True}
        if vision_mode and images:
            payload["fallback"] = False

        yield {"event": "llm_start", "iter": i}
        full_content = ""
        err_text = None
        for piece in chat_completion_stream(payload):
            if piece.startswith("[错误]"):
                err_text = piece
                break
            full_content += piece
            yield {"event": "token", "text": piece}
        if err_text:
            yield {"event": "error", "text": err_text}
            break

        # 把本轮 LLM 输出也记录到 steps，便于后端兜底提取最后一段回复
        if full_content:
            yield {"event": "llm", "text": full_content, "provider": default_provider}

        call = _extract_call(full_content)
        if call is None:
            yield {"event": "done", "text": full_content or "（模型未返回可见文本）"}
            done = True
            break

        name, args = call
        yield {"event": "tool", "tool": name, "args": args}
        try:
            output = _run_tool(name, args, ws)
        except ValueError as e:
            output = f"[拒绝] {e}"
        yield {"event": "tool_result", "output": output}

        messages.append({"role": "assistant", "content": full_content})
        messages.append({"role": "user", "content": f"工具 {name} 的结果：\n{output}"})

    # 达到最大轮数仍未给出结论：强制让模型基于已有信息做最终总结
    if not done:
        messages.append({
            "role": "user",
            "content": "已达到最大思考轮数。请基于你已经收集到的信息，直接给出最终回答/总结，不要调用任何工具。"
        })
        payload = {"model": default_model, "messages": messages, "temperature": 0.3, "fallback": True}
        if vision_mode and images:
            payload["fallback"] = False
        yield {"event": "llm_start", "iter": "final"}
        full_content = ""
        err_text = None
        for piece in chat_completion_stream(payload):
            if piece.startswith("[错误]"):
                err_text = piece
                break
            full_content += piece
            yield {"event": "token", "text": piece}
        if err_text:
            yield {"event": "error", "text": err_text}
        else:
            yield {"event": "done", "text": full_content or "（模型未返回可见文本，请查看右侧执行步骤）"}
