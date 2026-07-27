"""任务模式端到端验证(Phase 6a):连真实服务,发任务消息,看自动分流 + react_task 流式 + tool_step 序列。

先起服务(不开窗口):
  .venv/bin/python -c "import uvicorn; from backend.app import app; uvicorn.run(app, host='127.0.0.1', port=8765)"
再跑:
  .venv/bin/python scripts/task_probe.py --text "!帮我看看这个项目的目录结构"

校验:chat_start 带 task:true → (可能 tool_step×N)→ chat_chunk×N → chat_end。
任务模式不应收到 chat_clear(那是陪伴模式撤回思考的哨兵)。tool_step 出现 = 工具调用对用户可见。
"""
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def run(uri, text, persona, wait):
    async with websockets.connect(uri) as ws:
        print(f"[task_probe] connected {uri}", flush=True)
        loop = asyncio.get_event_loop()
        start = loop.time()
        onboarded = None
        chunks = []
        tool_steps = []
        chat_clears = 0
        chat_end_text = None
        task_flag = None

        async def sender():
            nonlocal onboarded
            await asyncio.sleep(0.8)
            if onboarded is False:
                await ws.send(json.dumps({"type": "select_persona", "id": persona}))
                print(f"[task_probe] >>> select_persona: {persona}", flush=True)
                await asyncio.sleep(0.5)
            await ws.send(json.dumps({"type": "user_message", "text": text}))
            print(f"[task_probe] >>> user_message: {text!r}", flush=True)

        send_task = asyncio.ensure_future(sender())
        try:
            while True:
                remaining = wait - (loop.time() - start)
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = data.get("type")
                if t == "onboarding_select":
                    onboarded = False
                    print(f"[task_probe] <<< onboarding_select", flush=True)
                elif t == "meta":
                    onboarded = True
                    print(f"[task_probe] <<< meta: {data.get('persona', {}).get('name')}", flush=True)
                elif t == "expression":
                    print(f"[task_probe]     expr: {data.get('state')}", flush=True)
                elif t == "chat_start":
                    task_flag = data.get("task")
                    print(f"[task_probe] <<< chat_start  task={task_flag}", flush=True)
                elif t == "tool_step":
                    tool_steps.append(data)
                    argstr = json.dumps(data.get("args"), ensure_ascii=False) if data.get("args") else ""
                    print(f"[task_probe] <<< tool_step: {data.get('name')} {argstr} → {(data.get('result') or '')[:60]}",
                          flush=True)
                elif t == "chat_chunk":
                    chunks.append(data.get("delta", ""))
                elif t == "chat_clear":
                    chat_clears += 1
                    print(f"[task_probe] <<< chat_clear(任务模式不应出现!)", flush=True)
                elif t == "chat_end":
                    chat_end_text = data.get("text")
                    print(f"[task_probe] <<< chat_end ({len(chat_end_text or '')} 字符)", flush=True)
        finally:
            send_task.cancel()

    print("\n[task_probe] === 总结 ===", flush=True)
    flag_ok = task_flag is True
    print(f"chat_start.task = {task_flag}  {'✓ 任务模式' if flag_ok else '✗ 未标 task(True)'}", flush=True)
    print(f"tool_step 数: {len(tool_steps)}  {'(LLM 调了工具,步骤可见)' if tool_steps else '(本轮未调工具,纯文本回复)'}", flush=True)
    print(f"chat_clear 数: {chat_clears}  {'✓ 无撤回(任务模式正确)' if chat_clears == 0 else '✗ 任务模式不应撤回'}", flush=True)
    print(f"chat_chunk 数: {len(chunks)}", flush=True)
    streamed = "".join(chunks)
    shown = streamed if len(streamed) <= 300 else streamed[:300] + "..."
    print(f"流式拼接({len(streamed)} 字符): {shown}", flush=True)
    ok = flag_ok and chat_clears == 0
    print(f"\n[task_probe] {'通过 ✓' if ok else '有异常 ✗'}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--text", default="!帮我看看这个项目的目录结构")
    p.add_argument("--persona", default="zorya")
    p.add_argument("--wait", type=float, default=40.0, help="收消息总时长(秒)")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    asyncio.run(run(f"ws://127.0.0.1:{a.port}/ws", a.text, a.persona, a.wait))


if __name__ == "__main__":
    main()
