"""ReAct + 工具调用端到端验证:连真实服务,发诱导召回的消息,看 WS 三段式 + 是否调工具。

先起服务(不开窗口):
  .venv/bin/python -c "import uvicorn; from backend.app import app; uvicorn.run(app, host='127.0.0.1', port=8765)"
再跑:
  .venv/bin/python scripts/react_probe.py --text "你记得我最近在干嘛吗"

服务端日志会打印 `react tool[recall_memory] args=... → ...`(若 LLM 调了工具)。
本脚本打印 WS 收到的 chat_start → chat_chunk×N → chat_end 顺序,并校验 chunk 拼接 == chat_end 全文。
"""
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def run(uri, text, persona, wait):
    async with websockets.connect(uri) as ws:
        print(f"[probe] connected {uri}", flush=True)
        loop = asyncio.get_event_loop()
        start = loop.time()
        onboarded = None
        chunks = []
        chat_end_text = None

        async def sender():
            await asyncio.sleep(0.8)
            if onboarded is False:
                await ws.send(json.dumps({"type": "select_persona", "id": persona}))
                print(f"[probe] >>> select_persona: {persona}", flush=True)
                await asyncio.sleep(0.5)
            await ws.send(json.dumps({"type": "user_message", "text": text}))
            print(f"[probe] >>> user_message: {text!r}", flush=True)

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
                    print(f"[probe] <<< onboarding_select ({len(data.get('personas', []))} 人格)", flush=True)
                elif t == "meta":
                    onboarded = True
                    print(f"[probe] <<< meta: {data.get('persona', {}).get('name')}", flush=True)
                elif t == "expression":
                    print(f"[probe]     expr: {data.get('state')}", flush=True)
                elif t == "chat_start":
                    print(f"[probe] <<< chat_start", flush=True)
                elif t == "chat_chunk":
                    chunks.append(data.get("delta", ""))
                elif t == "chat_end":
                    chat_end_text = data.get("text")
                    print(f"[probe] <<< chat_end: {chat_end_text!r}", flush=True)
                elif t == "chat" and data.get("role") == "agent":
                    print(f"[probe] <<< chat(agent 单条): {data.get('text')!r}", flush=True)
        finally:
            send_task.cancel()

    print("\n[probe] === 总结 ===", flush=True)
    streamed = "".join(chunks)
    print(f"chat_chunk 数: {len(chunks)}", flush=True)
    print(f"流式拼接: {streamed!r}", flush=True)
    if chat_end_text is not None:
        match = "✓ 一致" if chat_end_text == streamed else "✗ 与 chunk 拼接不一致"
        print(f"chat_end 全文: {chat_end_text!r}  {match}", flush=True)
    print("\n注:看服务端日志的 'react tool[...]' 行确认 LLM 是否调了工具。", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--text", default="你记得我最近在干嘛吗")
    p.add_argument("--persona", default="zorya")
    p.add_argument("--wait", type=float, default=30.0, help="收消息总时长(秒)")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    asyncio.run(run(f"ws://127.0.0.1:{a.port}/ws", a.text, a.persona, a.wait))


if __name__ == "__main__":
    main()
