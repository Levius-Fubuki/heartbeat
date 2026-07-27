"""开发用 WS 探针:连上 /ws,打印后端推送的所有事件;可选定时发一条用户消息。

用法:
  .venv/bin/python scripts/ws_probe.py                       # 只听 24s(验证心跳互动路径)
  .venv/bin/python scripts/ws_probe.py --duration 35 --send-at 3 --text "在吗"  # 验证正常互动 + 静默收尾
"""
from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def run(uri: str, duration: float, send_at: float, text: str) -> None:
    loop = asyncio.get_event_loop()
    async with websockets.connect(uri) as ws:
        print(f"[probe] connected to {uri}", flush=True)
        start = loop.time()

        async def sender():
            if send_at <= 0:
                return
            await asyncio.sleep(send_at)
            await ws.send(json.dumps({"type": "user_message", "text": text}))
            print(f"[probe] >>> sent user_message: {text!r}", flush=True)

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
                print(f"[probe] <<< {raw}", flush=True)
        finally:
            send_task.cancel()
    print("[probe] done", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=24.0)
    p.add_argument("--send-at", type=float, default=0.0)
    p.add_argument("--text", default="在吗")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    asyncio.run(run(f"ws://127.0.0.1:{a.port}/ws", a.duration, a.send_at, a.text))


if __name__ == "__main__":
    main()
