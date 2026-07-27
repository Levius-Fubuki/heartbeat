"""端到端记忆闭环验证:模拟「对话 A → 静默归档 → 对话 B 问"记得吗"」。

先起服务(不开窗口):
  .venv/bin/python -c "import uvicorn; from backend.app import app; uvicorn.run(app, host='127.0.0.1', port=8765)"
再跑:
  .venv/bin/python scripts/mem_probe.py

默认时序:3s 发对话 A → 等 120s 静默超时归档 → 135s 发对话 B 问"记得吗" → 165s 结束。
有 LLM key:对话 B 的回复应复述对话 A 提过的事实。
无 key:走 canned,但 episodic.jsonl 仍会写入(验证管道)。
跑完检查:cat memory/episodic.jsonl ; cat memory/profile.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def run(uri, duration, send_at, text, send_at2, text2, persona):
    async with websockets.connect(uri) as ws:
        print(f"[probe] connected to {uri}", flush=True)
        loop = asyncio.get_event_loop()
        start = loop.time()
        state = {"onboarded": None, "replies": []}      # onboarded: None=未知 True/False

        async def sender():
            # 握手后稍等,据 onboarding 状态决定是否先选人格
            await asyncio.sleep(0.8)
            if state["onboarded"] is False:
                await ws.send(json.dumps({"type": "select_persona", "id": persona}))
                print(f"[probe] >>> select_persona: {persona} (首次 onboarding)", flush=True)
                await asyncio.sleep(0.5)
            # 对话 A
            wait = send_at - (loop.time() - start)
            if wait > 0:
                await asyncio.sleep(wait)
            await ws.send(json.dumps({"type": "user_message", "text": text}))
            print(f"[probe] >>> 对话A: {text!r}", flush=True)
            # 对话 B(在静默超时之后,触发归档与召回刷新)
            wait = send_at2 - (loop.time() - start)
            if wait > 0:
                await asyncio.sleep(wait)
            await ws.send(json.dumps({"type": "user_message", "text": text2}))
            print(f"[probe] >>> 对话B: {text2!r}", flush=True)

        send_task = asyncio.ensure_future(sender())
        try:
            while True:
                remaining = duration - (loop.time() - start)
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
                    n = len(data.get("personas", []))
                    print(f"[probe] <<< onboarding_select ({n} 人格可选)", flush=True)
                    state["onboarded"] = False
                elif t == "meta":
                    p = data.get("persona", {})
                    print(f"[probe] <<< meta: {p.get('name')} ({p.get('id')})", flush=True)
                    state["onboarded"] = True
                elif t == "chat" and data.get("role") == "agent":
                    print(f"[probe] <<< agent: {data.get('text')!r}", flush=True)
                    state["replies"].append(data.get("text", ""))
                elif t == "expression":
                    print(f"[probe]     expr: {data.get('state')}", flush=True)
        finally:
            send_task.cancel()

    print("\n[probe] === 总结 ===", flush=True)
    print(f"agent 回复数: {len(state['replies'])}", flush=True)
    if state["replies"]:
        print(f"最后一条: {state['replies'][-1]!r}", flush=True)
    print("检查归档与档案:", flush=True)
    print("  cat memory/episodic.jsonl", flush=True)
    print("  cat memory/profile.yaml", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=165.0)
    p.add_argument("--send-at", type=float, default=3.0, help="对话 A 发送时刻(秒)")
    p.add_argument("--text", default="我在写一个 AI 桌面陪伴 agent,叫 Heartbeat")
    p.add_argument("--send-at-2", type=float, default=135.0, help="对话 B 发送时刻(秒,须 > send-at + 120 静默)")
    p.add_argument("--text-2", default="你记得我在干嘛吗?")
    p.add_argument("--persona", default="zorya")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    asyncio.run(run(f"ws://127.0.0.1:{a.port}/ws", a.duration, a.send_at, a.text,
                    a.send_at_2, a.text_2, a.persona))


if __name__ == "__main__":
    main()
