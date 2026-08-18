# -*- coding: utf-8 -*-
"""
Agent 执行层（agent）
====================
一个轻量的「写程序 / 干活的循环」。思路复用 OpenCode/Cline：让模型当「大脑」做规划，
我们提供几个工具（读文件、写文件、跑命令、列目录）当「手脚」，循环执行直到任务完成。

为什么用 prompt 式工具协议而不是原生 function calling：
- 免费模型对 function calling 支持参差，统一的 <call> 文本格式最稳；
- 整个循环完全可控，方便加审批、日志、沙箱。

安全边界：所有文件/命令都锁在 agent_workspace/ 目录内，路径想逃出去会被拒绝。
跑命令是真实执行，默认只在隔离工作区里，且有超时；后续可加「危险命令需确认」。
"""

import json
import os
import subprocess

from store import WORKSPACE_DIR, get_defaults
from gateway import chat_completion

MAX_ITER = 12
CMD_TIMEOUT = 60

SYSTEM_PROMPT = """你是一个运行在用户本地的编程 Agent。你可以读写文件、执行终端命令来完成任务。
工作目录是 agent_workspace/，所有相对路径都相对于它。

当你需要使用工具时，严格按以下格式输出一个调用块（一次只调用一个）：
<call>
{"name": "工具名", "args": {参数}}
</call>

可用工具：
- read_file: {"path": "相对路径"} -> 读取文件内容
- write_file: {"path": "相对路径", "content": "完整文件内容"} -> 创建/覆盖文件
- run_command: {"command": "shell 命令"} -> 在工作目录执行命令，返回 stdout/stderr
- list_dir: {"path": "相对路径或留空"} -> 列出目录内容

规则：
1. 先理解任务，再动手；小步快跑，每步验证。
2. 写代码后务必用 run_command 跑测试/编译确认能跑通。
3. 不需要工具时，直接给出最终回答（不要输出 <call>）。
4. 用中文和用户交流。
"""

TOOL_NAMES = {"read_file", "write_file", "run_command", "list_dir"}


def _safe_path(rel: str) -> str:
    """把相对路径解析到工作区内，越界则抛错。"""
    rel = rel or "."
    base = os.path.realpath(WORKSPACE_DIR)
    target = os.path.realpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"路径越界，拒绝访问：{rel}")
    return target


def _run_tool(name: str, args: dict) -> str:
    if name == "read_file":
        p = _safe_path(args.get("path", ""))
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {args.get('path')}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 12000:
            content = content[:12000] + "\n...（内容过长已截断）"
        return content

    if name == "write_file":
        p = _safe_path(args.get("path", ""))
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args.get("content", ""))
        return f"[成功] 已写入 {args.get('path')}（{len(args.get('content', ''))} 字符）"

    if name == "run_command":
        cmd = args.get("command", "")
        try:
            r = subprocess.run(cmd, shell=True, cwd=WORKSPACE_DIR,
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
        p = _safe_path(args.get("path", ""))
        if not os.path.isdir(p):
            return f"[错误] 目录不存在: {args.get('path')}"
        items = sorted(os.listdir(p))
        return "\n".join(items) if items else "[空目录]"

    return f"[错误] 未知工具: {name}"


def _extract_call(text: str):
    """从模型输出里提取第一个 <call>...</call> 块，返回 (name, args) 或 None。"""
    import re
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


def run_agent(task: str):
    """
    执行一个任务，返回步骤列表（供 UI 展示）：
    每步: {"type": "llm"|"tool"|"done", "text", "tool", "args", "output"}
    """
    default_provider, default_model = get_defaults()
    steps = []
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for i in range(MAX_ITER):
        payload = {
            "model": default_model,
            "messages": messages,
            "temperature": 0.3,
            "fallback": True,
        }
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
            output = _run_tool(name, args)
        except ValueError as e:
            output = f"[拒绝] {e}"
        steps.append({"type": "tool", "tool": name, "args": args, "output": output})

        # 把工具结果作为 user 消息回灌，让模型继续
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"工具 {name} 的结果：\n{output}"})

    return steps
