"""运行时切换人格探针:验证「上线后点人格名 → 换人」的整条契约(后端零改,前端新增入口)。

不起服务、不调 LLM、不下载模型。Heartbeat 直接 in-process 构造,profile 换成临时路径
(绝不污染真实 memory/profile.yaml)。假 WS 客户端录下广播的 meta。

  case1 select→meta 回弹:对 5 个人格各发 select_persona → persona_id 更新 + 广播 meta.id 正确。
  case2 meta payload 形状:含 id/name/archetype/accent(pet.js 据此换图,chat.js 据此换名/配色)。
  case3 持久化:set_onboarded 落盘 persona_id(写到临时 profile,不碰真实档案)。
  case4 拒绝未知人格:on_select_persona("nope") 早返,persona_id 不变(防前端误传)。
  case5 /api/personas 数据形状:current 是合法人格 + 每个 persona 含 id/name/archetype/accent
        (openSwitcher fetch 它来高亮当前项 + 渲染网格)。

用法:.venv/bin/python scripts/persona_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import PERSONAS                       # noqa: E402
from backend.memory.l2 import Profile                     # noqa: E402
from backend.ws import Hub                                # noqa: E402
from backend.heartbeat import Heartbeat                   # noqa: E402


class FakeClient:
    """录下所有广播 payload 的假 WS 客户端(加进 hub.clients 即被 hub.send 命中)。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _api_personas_payload(hb: Heartbeat) -> dict:
    """照搬 app.py /api/personas 路由体(纯读 hb 状态),验其数据形状,免起服务。"""
    return {"current": hb.persona_id, "personas": [
        {"id": p["id"], "name": p["name"], "archetype": p.get("archetype", ""),
         "accent": p.get("accent", "#cccccc")}
        for p in PERSONAS.values()
    ]}


async def run() -> bool:
    hub = Hub()
    fake = FakeClient()
    hub.clients.add(fake)

    with tempfile.TemporaryDirectory() as d:
        hb = Heartbeat(hub)                                   # 读真实 memory(只读,安全)
        hb.profile = Profile(path=Path(d) / "profile.yaml")   # 换临时路径:set_onboarded 不碰真实档案

        all_ok = True
        order = list(PERSONAS.keys())
        print(f"=== case1/2/3:对 {len(order)} 个人格各发 select_persona ===")
        for pid in order:
            fake.sent.clear()
            await hb.on_select_persona(pid)
            ok_state = hb.persona_id == pid
            metas = [m for m in fake.sent if m.get("type") == "meta"]
            ok_meta = bool(metas) and metas[-1].get("persona", {}).get("id") == pid
            p = metas[-1].get("persona", {}) if metas else {}
            ok_shape = all(k in p for k in ("id", "name", "archetype", "accent")) if metas else False
            ok_persist = hb.profile.persona_id == pid        # set_onboarded 落盘(临时 profile)
            ok = ok_state and ok_meta and ok_shape and ok_persist
            all_ok &= ok
            print(f"  [{'✓' if ok else '✗'}] {pid:6} → persona_id={hb.persona_id} "
                  f"meta.id={p.get('id')} keys={sorted(p.keys())} 持久化={hb.profile.persona_id}")

        print("\n=== case4:拒绝未知人格(防前端误传脏 id) ===")
        before = hb.persona_id
        fake.sent.clear()
        await hb.on_select_persona("definitely-not-a-persona")
        ok_unknown = hb.persona_id == before and not any(m.get("type") == "meta" for m in fake.sent)
        all_ok &= ok_unknown
        print(f"  [{'✓' if ok_unknown else '✗'}] 未知 id → persona_id 不变({before}),不广播 meta")

        print("\n=== case5:/api/personas 数据形状(openSwitcher fetch 它高亮当前项) ===")
        payload = _api_personas_payload(hb)
        ok_current = payload["current"] in PERSONAS
        ok_keys = all(p.get("id") and p.get("name") and "archetype" in p and "accent" in p
                      for p in payload["personas"])
        ok_count = len(payload["personas"]) == len(PERSONAS)
        ok = ok_current and ok_keys and ok_count
        all_ok &= ok
        print(f"  [{'✓' if ok else '✗'}] current={payload['current']}(合法={ok_current}) "
              f"persona 数={len(payload['personas'])} 各项四键齐全={ok_keys}")

        return all_ok


def main() -> None:
    ok = asyncio.run(run())
    print(f"\n{'✅ 全部通过' if ok else '❌ 有失败项,见上'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
