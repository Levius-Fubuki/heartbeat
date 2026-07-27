"""Onboarding Stage 2 首次访谈探针:验「手动入口触发 + LLM 自主访谈 + 增量抽取 + 四路收尾」整条契约。

不起服务、不联网。Heartbeat in-process 构造,profile + episodic 都换临时路径(访谈归档会写
episodic,绝不污染真实 memory/)。gateway 的 react_interview/react_reply/extract_* 全桩成确定串
(快速 + 离线 + 可断言)。假 WS 客户端录广播。

  c1 start:on_start_interview → interview_start + 流式开场白 + interviewing/CONVERSATION + L1 含开场白。
  c2 回复走 react_interview:on_user_message → turns+1 + 流式访谈问句。
  c3 增量抽取:extract_facts 桩返 {interests:["编码"]} → profile.merge → to_prompt_block 含「编码」。
  c4 轮数上限(MAX=2)→ 第2轮注入告别 + chat_end 后收尾 → interviewed/IDLE/不卡 interviewing。
  c5 跳过:on_end_interview → 立即收尾 + L1 清空 + interview_end 广播。
  c6 静默超时:手置 last_agent_reply_at 过期 → _silence_check 走访谈分支收尾。
  c7 切人格中断:访谈中 on_select_persona → 先归档旧访谈(trigger=interview)→ 新人格不卡 interviewing。
  c8 重入守卫:on_start_interview 二次 → no-op(不发 interview_start)。
  c9 状态回归:收尾后 on_user_message → 走 react_reply(非 react_interview)。

用法:.venv/bin/python scripts/interview_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import PERSONAS  # noqa: E402,F401  (触发 personas 加载)
from backend.memory.l2 import Profile  # noqa: E402
from backend.memory.l3 import EpisodicMemory  # noqa: E402
from backend.ws import Hub  # noqa: E402
from backend.heartbeat import Heartbeat  # noqa: E402
import backend.heartbeat as hb_mod  # noqa: E402  (monkeypatch INTERVIEW_MAX_ROUNDS)
from backend.llm import gateway  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _stub(text: str):
    """造一个 yield 单串的 async generator(替 react_reply / react_switch_in)。"""
    async def gen():
        yield text
    return gen()


def _streamed_text(sent: list[dict]) -> str:
    return "".join(m.get("delta", "") for m in sent if m.get("type") == "chat_chunk")


def _had(msg_type: str, sent: list[dict]) -> bool:
    return any(m.get("type") == msg_type for m in sent)


def _patch_gateway() -> None:
    """桩掉所有联网 LLM 调用:访谈 yield 确定串(按 opening/last_round/普通轮区分);reply 吐 'reply';
    抽取/归纳返 {}(c3 会再 monkeypatch extract_facts 返确定 dict 验 merge)。"""
    async def _interview(persona_id, profile=None, history=None, turn=0, max_turns=8,
                         last_round=False, opening=False):
        if opening:
            yield f"[开场@{persona_id}]"
        elif last_round:
            yield f"[告别@{persona_id} t{turn}]"
        else:
            yield f"[问@{persona_id} t{turn}]"

    gateway.react_interview = _interview
    gateway.react_reply = lambda *a, **k: _stub("reply")
    gateway.react_switch_in = lambda new, old, **k: _stub(f"[{new}吐槽{old}]")

    async def _noop(*a, **k):
        return {}

    gateway.extract_facts = _noop
    gateway.extract_patterns = _noop


def _new_hb(d: str, hub: Hub, tag: str = "h") -> Heartbeat:
    """临时 profile + episodic 的 Heartbeat(不碰真实 memory/)。强制 zorya 起始人格 + 访谈态复位。"""
    hb = Heartbeat(hub)
    hb.profile = Profile(path=Path(d) / f"profile_{tag}.yaml")
    hb.episodic = EpisodicMemory(path=Path(d) / f"e_{tag}.jsonl")
    hb.persona_id = hb.profile.persona_id or "zorya"
    hb.histories = {hb.persona_id: []}
    hb.interviewing = False
    hb._interview_turns = 0
    hb._refresh_episodic_block()          # 用空 temp episodic 重置召回缓存
    return hb


async def _talk(hb: Heartbeat, text: str) -> None:
    await hb.on_user_message(text)


# ============================ cases ============================
async def case1_start(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("=== case1:on_start_interview → 开场白 + 访谈态 ===")
    hb = _new_hb(d, hub, "c1")
    fake.sent.clear()
    await hb.on_start_interview()
    streamed = _streamed_text(fake.sent)
    l1 = hb._history()
    ok = (_had("interview_start", fake.sent) and _had("chat_start", fake.sent)
          and streamed == "[开场@zorya]" and hb.interviewing and hb.state == "conversation"
          and len(l1) == 1 and l1[0]["role"] == "agent" and "[开场" in l1[0]["text"])
    print(f"  [{'✓' if ok else '✗'}] interview_start={_had('interview_start', fake.sent)}"
          f" | 流式={streamed!r} | interviewing={hb.interviewing} state={hb.state} | L1={len(l1)}条")
    return ok


async def case2_reply_uses_interview(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case2:访谈中回复走 react_interview + turns+1 ===")
    hb = _new_hb(d, hub, "c2")
    await hb.on_start_interview()
    fake.sent.clear()
    await _talk(hb, "我写代码")
    streamed = _streamed_text(fake.sent)
    ok = (hb._interview_turns == 1 and streamed == "[问@zorya t1]" and hb.interviewing)
    print(f"  [{'✓' if ok else '✗'}] turns={hb._interview_turns}(期望1) | 流式={streamed!r} | interviewing={hb.interviewing}")
    return ok


async def case3_incremental_extract(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case3:每轮增量抽取 → profile.merge ===")
    hb = _new_hb(d, hub, "c3")
    async def _extract(*a, **k):              # 桩返确定 dict 验 merge
        return {"interests": ["编码"]}
    gateway.extract_facts = _extract
    await hb.on_start_interview()
    await _talk(hb, "我写代码")
    if hb._extract_task is not None:
        await hb._extract_task                # 让抽取跑完
    block = hb.profile.to_prompt_block()
    ok = "编码" in block
    print(f"  [{'✓' if ok else '✗'}] profile 含「编码」={ok} | block={block!r}")
    return ok


async def case4_max_rounds(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case4:轮数上限(MAX=2)→ 第2轮告别 + 收尾 ===")
    # on_user_message 读 settings.interview_max_rounds(Phase 5+ 起改读 settings 单例;
    # heartbeat 模块已不 import INTERVIEW_MAX_ROUNDS),故 monkeypatch settings._data 而非 hb_mod 常量。
    from backend.memory import settings as sm
    saved = sm.settings._data.get("interview_max_rounds")
    sm.settings._data["interview_max_rounds"] = 2
    try:
        hb = _new_hb(d, hub, "c4")
        await hb.on_start_interview()
        await _talk(hb, "a")                  # t1
        fake.sent.clear()
        await _talk(hb, "b")                  # t2 = last_round
        streamed = _streamed_text(fake.sent)
        ok = ("告别" in streamed and hb.profile.interviewed and not hb.interviewing
              and hb.state == "idle")
        print(f"  [{'✓' if ok else '✗'}] 第2轮流式={streamed!r} | interviewed={hb.profile.interviewed}"
              f" interviewing={hb.interviewing} state={hb.state}")
        return ok
    finally:
        sm.settings._data["interview_max_rounds"] = saved


async def case5_skip(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case5:跳过访谈 → 立即收尾 + L1 清空 ===")
    hb = _new_hb(d, hub, "c5")
    await hb.on_start_interview()
    await _talk(hb, "x")
    fake.sent.clear()
    await hb.on_end_interview()
    ok = (hb.profile.interviewed and not hb.interviewing and hb.state == "idle"
          and len(hb._history()) == 0 and _had("interview_end", fake.sent))
    print(f"  [{'✓' if ok else '✗'}] interviewed={hb.profile.interviewed} interviewing={hb.interviewing}"
          f" state={hb.state} L1清空={len(hb._history())==0} interview_end={_had('interview_end', fake.sent)}")
    return ok


async def case6_silence_timeout(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case6:访谈静默超时 → 收尾 ===")
    hb = _new_hb(d, hub, "c6")
    await hb.on_start_interview()
    await _talk(hb, "x")
    hb.last_agent_reply_at = time.monotonic() - 70   # 超过 INTERVIEW_SILENCE_SEC(60)
    await hb._silence_check()
    ok = (hb.profile.interviewed and not hb.interviewing and hb.state == "idle")
    print(f"  [{'✓' if ok else '✗'}] 静默70s后 → interviewed={hb.profile.interviewed}"
          f" interviewing={hb.interviewing} state={hb.state}")
    return ok


async def case7_switch_persona_mid_interview(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case7:访谈中切人格 → 先归档旧访谈 + 新人格不卡 interviewing ===")
    hb = _new_hb(d, hub, "c7")
    await hb.on_start_interview()
    await _talk(hb, "聊点啥")
    await hb.on_select_persona("vesna")
    archived = hb.episodic._read_all()
    ok = (not hb.interviewing and hb.persona_id == "vesna"
          and bool(archived) and archived[-1].get("persona") == "zorya"
          and archived[-1].get("trigger", {}).get("category") == "interview")
    last = archived[-1] if archived else {}
    print(f"  [{'✓' if ok else '✗'}] 切vesna后 interviewing={hb.interviewing} persona={hb.persona_id}"
          f" | 归档 persona={last.get('persona')} trigger={last.get('trigger')}")
    return ok


async def case8_reentry_guard(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case8:on_start_interview 重入守卫 ===")
    hb = _new_hb(d, hub, "c8")
    await hb.on_start_interview()
    fake.sent.clear()
    await hb.on_start_interview()             # 二次 → 早返,不发任何消息
    ok = (len(fake.sent) == 0 and hb.interviewing)   # 仍 True(第一次设的)
    print(f"  [{'✓' if ok else '✗'}] 二次 start → 广播数={len(fake.sent)}(期望0) | interviewing={hb.interviewing}")
    return ok


async def case9_state_regress(hub: Hub, fake: FakeClient, d: str) -> bool:
    print("\n=== case9:收尾后 on_user_message → 走 react_reply(非访谈)===")
    hb = _new_hb(d, hub, "c9")
    await hb.on_start_interview()
    await _talk(hb, "x")
    await hb.on_end_interview()
    fake.sent.clear()
    await _talk(hb, "正常聊天")               # 收尾后应走 react_reply → "reply"
    streamed = _streamed_text(fake.sent)
    ok = (streamed == "reply" and not hb.interviewing)
    print(f"  [{'✓' if ok else '✗'}] 收尾后回复流式={streamed!r}(期望'reply') | interviewing={hb.interviewing}")
    return ok


async def main() -> None:
    _patch_gateway()
    hub = Hub()
    fake = FakeClient()
    hub.clients.add(fake)

    results = []
    with tempfile.TemporaryDirectory() as d:
        results.append(await case1_start(hub, fake, d))
        results.append(await case2_reply_uses_interview(hub, fake, d))
        results.append(await case3_incremental_extract(hub, fake, d))
        results.append(await case4_max_rounds(hub, fake, d))
        results.append(await case5_skip(hub, fake, d))
        results.append(await case6_silence_timeout(hub, fake, d))
        results.append(await case7_switch_persona_mid_interview(hub, fake, d))
        results.append(await case8_reentry_guard(hub, fake, d))
        results.append(await case9_state_regress(hub, fake, d))

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} {'✅ 全部通过' if ok else '❌ 有失败项,见上'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
