#!/usr/bin/env python3
"""Phase 6d 端到端(需 DeepSeek key + 本地起 uvicorn):任务模式写文件 / 跑命令 → 自动应答授权 → 验 tool_step + 真执行。

起服务:`.venv/bin/python -m uvicorn backend.app:app --port 8765 --log-level warning`(后台)
跑本脚本:`.venv/bin/python scripts/exec_e2e.py`

流程:① POST 开 write_tools/run_command + 白名单 → ② WS 发「!写文件」→ ③ 收 confirm_request →
回 confirm_response(approved)→ ④ 验 tool_step write_file + 文件落盘 → ⑤ 再发「!跑 echo」→ 同样验。
最后还原设置(write/run 关)+ 删测试文件。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import websockets

API = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765/ws"
DESKTOP = Path.home() / "Desktop"
TEST_FILE = DESKTOP / "hb6d_e2e.txt"


async def wait_ready(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{API}/api/settings", timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception:
            await asyncio.sleep(0.5)
    return None


def post_settings(payload):
    return httpx.post(f"{API}/api/settings", json=payload, timeout=10).json()


async def round(ws, text, expect_tool, approve, deadline):
    """发一条任务消息,自动应答 confirm_request,收集事件直到 chat_end 或 deadline。返 (events, ok)。"""
    await ws.send(json.dumps({"type": "user_message", "text": text}))
    events, saw_confirm, saw_tool, saw_end = [], False, False, False
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            if saw_end:
                break
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        t = d.get("type")
        if t in ("meta", "history", "settings_changed", "cooldown_mode", "expression"):
            continue
        events.append(d)
        if t == "confirm_request":
            saw_confirm = True
            print(f"    ← confirm_request: {d.get('tool')} | {d.get('summary')}")
            # 走 HTTP(与真实前端一致):on_user_message 持锁期间该连接 WS receive_loop 读不到 WS 回包
            httpx.post(f"{API}/api/confirm", json={"id": d.get("id"), "approved": approve}, timeout=10)
            print(f"    → POST /api/confirm(approved={approve})")
        elif t == "tool_step":
            saw_tool = saw_tool or (d.get("name") == expect_tool)
            print(f"    ← tool_step: {d.get('name')} args={str(d.get('args'))[:60]}")
        elif t == "confirm_close":
            print(f"    ← confirm_close approved={d.get('approved')}")
        elif t == "chat_end":
            saw_end = True
            print(f"    ← chat_end (reply len={len(d.get('text') or '')})")
    return events, saw_confirm, saw_tool


async def main():
    view = await wait_ready()
    if not view:
        print("✗ 后端未在 :8765 起来(先跑 uvicorn backend.app:app)"); return 1
    if not view.get("llm", {}).get("has_key"):
        print("✗ 无 LLM key,跳过真实 e2e(需 DeepSeek key)"); return 1
    print(f"model={view['llm']['model']} has_key=True → 开始 e2e")

    # 开写/执行 + 白名单
    post_settings({"config": {"write_tools_enabled": True, "run_command_enabled": True, "task_mode_enabled": True},
                   "paths": {"command_allowlist": ["echo", "ls", "cat", "python3", "git"]}})
    print("已开 write_tools/run_command + 白名单")

    if TEST_FILE.exists():
        TEST_FILE.unlink()
    fails = 0

    async with websockets.connect(WS, max_size=None) as ws:
        await asyncio.sleep(0.5)  # 让握手 meta/history 发完

        # ---------- 轮1:写文件 ----------
        print("\n[轮1] !写文件 → 期待 confirm_request → 应答允许 → tool_step write_file")
        ev, saw_conf, saw_tool = await round(
            ws, f"!在 {TEST_FILE} 这个文件里写入内容:hello from heartbeat 6d",
            "write_file", True, time.monotonic() + 60)
        ok_conf = saw_conf
        ok_tool = saw_tool
        ok_file = TEST_FILE.exists() and "hello" in TEST_FILE.read_text(encoding="utf-8")
        print(f"  confirm_request 收到={ok_conf}  tool_step write_file={ok_tool}  文件落盘={ok_file}")
        if not (ok_conf and ok_tool and ok_file):
            fails += 1; print("  ✗ 轮1 未完整通过(LLM 可能没调工具,看 events)")
        else:
            print("  ✓ 轮1 写文件链路通")

        # ---------- 轮2:跑命令 ----------
        print("\n[轮2] !跑 echo → 期待 confirm_request → 应答允许 → tool_step run_command")
        ev2, saw_conf2, saw_tool2 = await round(
            ws, "!用命令行跑一下 echo hb6d_e2e_ok", "run_command", True, time.monotonic() + 60)
        ok_conf2 = saw_conf2
        ok_tool2 = saw_tool2
        print(f"  confirm_request 收到={ok_conf2}  tool_step run_command={ok_tool2}")
        if not (ok_conf2 and ok_tool2):
            fails += 1; print("  ✗ 轮2 未完整通过")
        else:
            print("  ✓ 轮2 跑命令链路通")

    # ---------- 还原 + 清理 ----------
    post_settings({"config": {"write_tools_enabled": False, "run_command_enabled": False}})
    print("\n已还原 write_tools/run_command = False")
    if TEST_FILE.exists():
        TEST_FILE.unlink()
        print(f"已删测试文件 {TEST_FILE}")

    print(f"\n{'='*40}\nexec_e2e: {'全过 ✓' if fails == 0 else f'{fails} 项未过'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
