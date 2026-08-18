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
from gateway import chat_completion
from catalog import PROVIDERS

MAX_ITER = 12
CMD_TIMEOUT = 60

SYSTEM_PROMPT = """你是一个运行在用户本地的编程 Agent。你可以读写文件、执行终端命令来完成任务。
当前工作目录（工作区间）是：
{workspace}
所有相对路径都相对于它。

当你需要使用工具时，严格按以下格式输出一个调用块（一次只调用一个）：
<call>
{{"name": "工具名", "args": {{参数}}}}
</call>

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


def _extract_call(text: str):
    """从模型输出里提取第一个 <call>...</call> 块，返回 (name, args) 或 None。"""
    m = re.search(r"<call>\s*(\{.*?\})\s*</call>", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
        if obj.get("name") in TOOL_NAMES:
            return obj["name"], obj.get("args", {})
    except Exception:
        return None
    return None


def run_agent(task: str, workspace: str = None, images: list = None, vision_mode: bool = False):
    """
    执行一个任务，返回步骤列表（供 UI 展示）：
    每步: {"type": "llm"|"tool"|"done", "text", "tool", "args", "output"}

    workspace: 当前工作区间绝对路径（必须是允许区间之一，否则退回主工作区）
    images:    已存盘的图片列表 [{"rel":..., "data":base64, "mime":...}]
    vision_mode: 是否把图片作为多模态内容直接传给模型（需模型支持视觉）
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

    # 构造消息
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(workspace=os.path.realpath(ws))},
    ]
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
        if code != 200:
            steps.append({"type": "error", "text": f"模型调用失败（{used}）: " +
                          json.dumps(j, ensure_ascii=False)[:400]})
            break

        content = (j.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        call = _extract_call(content)

        if call is None:
            steps.append({"type": "llm", "text": content, "provider": used})
            steps.append({"type": "done", "text": content})
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

    return steps
