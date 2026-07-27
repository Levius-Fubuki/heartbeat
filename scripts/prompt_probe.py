"""prompt 质感 + ReAct 验证:直接调 gateway,打印 system prompt 拼装 + 实测回复/搭话。

不起 WS 服务、不等静默,几秒出结果。改 build_system_prompt / react 约束后用它快速验证。
react 模式会构造 EpisodicMemory(读生产 episodic.jsonl + build_index)作工具依赖,
服务端日志(本进程 stdout)会打印 `react tool[...]` 行确认 LLM 是否调了工具。

用法:
  .venv/bin/python scripts/prompt_probe.py                              # 默认 reply 模式
  .venv/bin/python scripts/prompt_probe.py --mode react_reply --text "你记得我在干嘛吗"
  .venv/bin/python scripts/prompt_probe.py --mode react_heartbeat --app Spotify
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# 让 `.venv/bin/python scripts/prompt_probe.py` 也能 import heartbeat 根下的 backend.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 开 INFO 才能看到 gateway 的 `react tool[...]` 日志(确认 LLM 是否真调了工具)
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
for _n in ("httpx", "httpcore", "openai"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from backend.llm import gateway  # noqa: E402
from backend.memory.l2 import Profile  # noqa: E402


async def main(text: str, app: str, persona: str, mode: str) -> None:
    profile = Profile()                                    # 读已有 profile.yaml
    activity = {"foreground_app": app, "idle_sec": 12} if app else {}

    # react 模式需要 episodic 做工具依赖(构造 + 建向量索引,首次下载模型)
    episodic = None
    if mode.startswith("react"):
        from backend.memory.l3 import EpisodicMemory
        episodic = EpisodicMemory()
        episodic.build_index()
        print(f"[probe] episodic 索引就绪: {len(episodic._ids)} 条记忆\n", flush=True)

    sp = gateway.build_system_prompt(persona, profile, episodic_block="", activity=activity, mode=mode)
    print("=" * 70)
    print(f"SYSTEM PROMPT({mode} 模式):")
    print("=" * 70)
    print(sp)
    print("=" * 70)

    if not gateway.LLM_API_KEY:
        print("\n[!] 无 LLM_API_KEY,跳过实测(走 canned 兜底)。上面已验证 prompt 拼装正确。")
        return

    print("\n--- 实测(看下方 react tool[...] 日志确认是否调工具)---")
    if mode == "reply":
        history = [{"role": "user", "text": text}]
        out = await gateway.reply(text, persona, history, activity=activity,
                                  profile=profile, episodic_block="")
        print("\nREPLY:", repr(out.get("message")))
    elif mode == "react_reply":
        deps = {"episodic": episodic}
        history = [{"role": "user", "text": text}]
        parts = []
        async for delta in gateway.react_reply(text, persona, history, activity=activity,
                                               profile=profile, episodic_block="", deps=deps):
            parts.append(delta)
            print(delta, end="", flush=True)
        print(f"\n\nREACT_REPLY 全文({len(parts)} chunk): {repr(''.join(parts))}")
    elif mode == "react_heartbeat":
        deps = {"episodic": episodic}
        cand = {"detail": f"用户打开了 {app or '某 App'}", "category": "dev_app_opened"}
        parts = []
        async for delta in gateway.react_heartbeat(cand, persona, profile=profile, deps=deps):
            parts.append(delta)
            print(delta, end="", flush=True)
        print(f"\n\nREACT_HEARTBEAT 全文({len(parts)} chunk): {repr(''.join(parts))}")
    elif mode == "judge":
        cand = {"detail": f"用户打开了 {app or '某 App'}", "category": "dev_app_opened"}
        out = await gateway.judge_heartbeat(cand, persona, {}, profile=profile)
        print("\nJUDGE:", out)


def cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--text", default="你在干嘛")
    p.add_argument("--app", default="Warp", help="模拟前台 App")
    p.add_argument("--persona", default="zorya")
    p.add_argument("--mode", default="reply",
                   choices=["reply", "react_reply", "react_heartbeat", "judge"])
    a = p.parse_args()
    asyncio.run(main(a.text, a.app, a.persona, a.mode))


if __name__ == "__main__":
    cli()
