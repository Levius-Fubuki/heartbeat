"""运行时切换人格探针:验「记忆隔离 + 切换吐槽」整条契约(Phase 4+ 新增)。

不起服务、不联网。Heartbeat in-process 构造,**profile + episodic 都换临时路径**
(切换归档会写 episodic,绝不污染真实 memory/episodic.jsonl)。gateway 的 react_switch_in /
react_reply / extract_* 全桩成确定串(快速 + 离线 + 可断言)。假 WS 客户端录广播。

  case1 L1 隔离 + 实质才吐槽 + 归档打 persona 标:与 zorya 聊 4 条 → 切 vesna →
         vesna L1 仅含自己的吐槽(看不到 zorya 内容)+ zorya L1 已清 + episodic 新条 persona=zorya。
  case2 无实质不吐槽(冷启动 onboarding):全新空历史切人格 → 无 chat_start。
  case3 重选同一人格 = no-op:old==new → 不归档不吐槽,persona_id 不变。
  case4 召回按人格(l3):手造双人格索引 → recall(persona_id=A) 只返 A(recent_text 同理)。
  case5 _has_substance:<4 条 / 全 agent → False;含 user 轮且 ≥4 → True。
  case6 _build_handoff_block:含 old 的 name_meaning + new 对 old 的关系 stance/voice。
  case7 链式 A→B→C→A:每跳为新者吐槽刚离开者,不泄露;A 重入 L1 已清。

用法:.venv/bin/python scripts/switch_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from backend.config import PERSONAS  # noqa: E402
from backend.memory.l2 import Profile  # noqa: E402
from backend.memory.l3 import EpisodicMemory  # noqa: E402
from backend.memory import embeddings as emb_mod  # noqa: E402
from backend.ws import Hub  # noqa: E402
from backend.heartbeat import Heartbeat  # noqa: E402
from backend.llm import gateway  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _stub(text: str):
    """造一个 yield 单串的 async generator 工厂(替 react_switch_in / react_reply,离线确定)。"""
    async def gen():
        yield text
    return gen()


def _streamed_text(sent: list[dict]) -> str:
    return "".join(m.get("delta", "") for m in sent if m.get("type") == "chat_chunk")


def _had_chat_start(sent: list[dict]) -> bool:
    return any(m.get("type") == "chat_start" for m in sent)


def _patch_gateway() -> None:
    """桩掉所有联网 LLM 调用:吐槽/回复 yield 确定串;抽取/归纳返空。"""
    gateway.react_switch_in = lambda new, old, **k: _stub(f"[{new}吐槽{old}]")
    gateway.react_reply = lambda *a, **k: _stub("reply")

    async def _noop(*a, **k):
        return {}

    gateway.extract_facts = _noop
    gateway.extract_patterns = _noop


def _new_hb(d: str, hub: Hub, fake: FakeClient, tag: str = "h") -> Heartbeat:
    """造一个 profile + episodic 都走临时路径的 Heartbeat(不碰真实 memory/)。

    tag 区分各 case 的临时文件,避免同目录 case 间 episodic 互相污染。
    构造时读的是真实 profile(运行期可能是任意人格),换 temp 后强制回到 zorya,
    保证各 case 起始人格确定(不依赖真实 profile.yaml 当前值)。
    """
    hb = Heartbeat(hub)
    hb.profile = Profile(path=Path(d) / f"profile_{tag}.yaml")
    hb.episodic = EpisodicMemory(path=Path(d) / f"e_{tag}.jsonl")
    hb.persona_id = hb.profile.persona_id or "zorya"
    hb.histories = {hb.persona_id: []}    # 强制重置,与 temp profile 对齐
    hb._refresh_episodic_block()          # 用空 temp episodic 重置召回缓存
    return hb


async def _talk(hb: Heartbeat, text: str) -> None:
    """模拟一轮用户发言(走 on_user_message → stub reply)。"""
    await hb.on_user_message(text)


# ============================ cases ============================
async def case1_isolation_roast_archive(d: str, hub: Hub, fake: FakeClient) -> bool:
    print("=== case1:L1 隔离 + 实质才吐槽 + 归档打 persona 标 ===")
    hb = _new_hb(d, hub, fake, "c1")
    await _talk(hb, "hi")
    await _talk(hb, "second")            # zorya L1 = [u,a,u,a] 4 条,含 user 轮 → 有实质
    z_pre = len(hb.histories.get("zorya", []))
    fake.sent.clear()
    await hb.on_select_persona("vesna")  # 切到 vesna

    vesna_l1 = hb._history()             # 当前人格 = vesna
    zorya_l1 = hb.histories.get("zorya", [])
    roasted = _had_chat_start(fake.sent)
    roast_text = _streamed_text(fake.sent)
    isolated = all("hi" not in m.get("text", "") and "second" not in m.get("text", "")
                   for m in vesna_l1) and not any(m.get("role") == "user" for m in vesna_l1)
    archived = hb.episodic._read_all()
    ok_archive = bool(archived) and archived[-1].get("persona") == "zorya" and len(archived[-1]["messages"]) == z_pre

    ok = (z_pre == 4 and roasted and roast_text == "[vesna吐槽zorya]"
          and len(zorya_l1) == 0 and isolated and ok_archive)
    print(f"  [{'✓' if ok else '✗'}] zorya 聊 {z_pre} 条 → 切 vesna:"
          f" 吐槽={roasted}({roast_text!r}) | zorya L1 清空={len(zorya_l1)==0}"
          f" | vesna L1 隔离={isolated} | 归档 persona=zorya({len(archived[-1]['messages'])} msgs)={ok_archive}")
    return ok


async def case2_no_roast_without_substance(d: str, hub: Hub, fake: FakeClient) -> bool:
    print("\n=== case2:无实质(冷启动)不吐槽 ===")
    hb = _new_hb(d, hub, fake, "c2")           # zorya L1 空
    fake.sent.clear()
    await hb.on_select_persona("vesna")  # old=zorya 空 → 无实质
    ok = not _had_chat_start(fake.sent) and len(hb._history()) == 0
    print(f"  [{'✓' if ok else '✗'}] 空历史切人格 → chat_start={_had_chat_start(fake.sent)}(期望 False),"
          f" vesna L1 空={len(hb._history())==0}")
    return ok


async def case3_same_persona_noop(d: str, hub: Hub, fake: FakeClient) -> bool:
    print("\n=== case3:重选同一人格 = no-op ===")
    hb = _new_hb(d, hub, fake, "c3")
    await _talk(hb, "hi")
    await _talk(hb, "two")               # zorya 4 条
    before = list(hb.histories["zorya"])
    fake.sent.clear()
    await hb.on_select_persona("zorya")  # old==new
    ok = (not _had_chat_start(fake.sent) and hb.persona_id == "zorya"
          and hb.histories["zorya"] == before and len(hb.episodic._read_all()) == 0)
    print(f"  [{'✓' if ok else '✗'}] old==new → chat_start={_had_chat_start(fake.sent)}(期望 False),"
          f" L1 不变={hb.histories['zorya']==before}, 未归档={len(hb.episodic._read_all())==0}")
    return ok


def case4_persona_scoped_recall(d: str) -> bool:
    print("\n=== case4:召回按人格(l3 recall + recent_text 过滤)===")
    em = EpisodicMemory(path=Path(d) / "r.jsonl")
    ea = EpisodicMemory.build_entry("zorya", {"category": "x"}, "App", [{"role": "user", "text": "zorya 的事"}])
    eb = EpisodicMemory.build_entry("vesna", {"category": "x"}, "App", [{"role": "user", "text": "vesna 的事"}])
    em.append(ea)
    em.append(eb)
    # recent_text 按人格过滤(文件级,不依赖向量)
    rt_z = em.recent_text(5, persona_id="zorya")
    rt_v = em.recent_text(5, persona_id="vesna")
    rt_all = em.recent_text(5)
    ok_recent = ("zorya 的事" in rt_z and "vesna 的事" not in rt_z
                 and "vesna 的事" in rt_v and "zorya 的事" not in rt_v
                 and "zorya 的事" in rt_all and "vesna 的事" in rt_all)
    # recall 按人格过滤:手造双人格正交索引 + 桩 embed_query,验 mask 只返匹配人格
    ida, idb = ea["id"], eb["id"]
    em._ids = [ida, idb]
    em._matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)  # 正交:query=[1,0] 原本两都沾边
    em._entries_cache = {ida: ea, idb: eb}
    orig_eq = emb_mod.embed_query
    emb_mod.embed_query = lambda q: np.array([1.0, 0.0], dtype=np.float32)  # 桩:避免加载 ~130MB 模型
    try:
        hits_z = em.recall("x", top_k=5, persona_id="zorya")
        hits_v = em.recall("x", top_k=5, persona_id="vesna")
    finally:
        emb_mod.embed_query = orig_eq
    ok_recall = (len(hits_z) == 1 and hits_z[0]["persona"] == "zorya"
                 and len(hits_v) == 1 and hits_v[0]["persona"] == "vesna")
    ok = ok_recent and ok_recall
    print(f"  [{'✓' if ok else '✗'}] recent_text 按人格={ok_recent} | recall mask 按人格={ok_recall}"
          f" (zorya→{[h['persona'] for h in hits_z]}, vesna→{[h['persona'] for h in hits_v]})")
    return ok


async def case5_has_substance(hb_factory) -> bool:
    print("\n=== case5:_has_substance 守卫 ===")
    hb = hb_factory()
    u, a = {"role": "user", "text": "x"}, {"role": "agent", "text": "y"}
    c_short = hb._has_substance([u, a])                 # <4
    c_real = hb._has_substance([u, a, u, a])           # 含 user 轮 ≥4
    c_allagent = hb._has_substance([a, a, a, a])       # 全 agent
    ok = (not c_short) and c_real and (not c_allagent)
    print(f"  [{'✓' if ok else '✗'}] <4条={c_short}(期望F) | 含user≥4={c_real}(期望T) | 全agent={c_allagent}(期望F)")
    return ok


def case6_handoff_block() -> bool:
    print("\n=== case6:_build_handoff_block 含关系设定 ===")
    hb = Heartbeat(Hub())  # 仅用其方法,不跑循环
    block = hb._build_handoff_block("yara", "zorya")
    z_meaning = PERSONAS["zorya"]["name_meaning"]
    stance = PERSONAS["yara"]["relationships"]["zorya"]["stance"]
    voice = PERSONAS["yara"]["relationships"]["zorya"]["voice"]
    ok = ("[交接]" in block and z_meaning in block and stance in block and voice in block)
    print(f"  [{'✓' if ok else '✗'}] 含 [交接]/old内涵/stance/voice = {ok}")
    return ok


def case8_recall_memory_persona_scoped(d: str) -> bool:
    """recall_memory 工具(reach_reply 走的路径)也必须按人格隔离:dispatch 传 persona_id 只返该人格。"""
    print("\n=== case8:recall_memory 工具按人格隔离(dispatch 路径)===")
    from backend.llm.tools import dispatch_tool
    em = EpisodicMemory(path=Path(d) / "rm.jsonl")
    ea = EpisodicMemory.build_entry("zorya", {"category": "x"}, "App",
                                    [{"role": "user", "text": "zorya 秘密"}])
    eb = EpisodicMemory.build_entry("vesna", {"category": "x"}, "App",
                                    [{"role": "user", "text": "vesna 秘密"}])
    em.append(ea); em.append(eb)
    em._ids = [ea["id"], eb["id"]]
    em._matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    em._entries_cache = {ea["id"]: ea, eb["id"]: eb}
    orig_eq = emb_mod.embed_query
    emb_mod.embed_query = lambda q: np.array([1.0, 0.0], dtype=np.float32)
    try:
        scoped = dispatch_tool("recall_memory", {"query": "秘密", "top_k": 5},
                                {"episodic": em, "persona_id": "zorya"})
        # 不传 persona_id = 全局(向后兼容,boundary_probe 等老路径)
        global_ = dispatch_tool("recall_memory", {"query": "秘密", "top_k": 5},
                                 {"episodic": em})
    finally:
        emb_mod.embed_query = orig_eq
    ok = ("zorya 秘密" in scoped and "vesna 秘密" not in scoped
          and "zorya 秘密" in global_ and "vesna 秘密" in global_)
    print(f"  [{'✓' if ok else '✗'}] persona_id=zorya → 只返 zorya={'zorya 秘密' in scoped and 'vesna 秘密' not in scoped}"
          f" | 不传=全局两都返={'zorya 秘密' in global_ and 'vesna 秘密' in global_}")
    return ok


async def case7_chain(d: str, hub: Hub, fake: FakeClient) -> bool:
    print("\n=== case7:链式 zorya→vesna→rada→zorya(每跳吐槽刚离开者,不泄露)===")
    hb = _new_hb(d, hub, fake, "c7")
    # 先和 zorya 聊出实质
    await _talk(hb, "hi")
    await _talk(hb, "two")
    seq = []
    for nxt in ["vesna", "rada", "zorya"]:
        # 给当前人格补两轮,确保切走时有实质(每段都"真聊过")
        await _talk(hb, "msg-a")
        await _talk(hb, "msg-b")
        fake.sent.clear()
        await hb.on_select_persona(nxt)
        seq.append((nxt, _had_chat_start(fake.sent), _streamed_text(fake.sent)))
    # 每跳都吐槽了(因为切走前都聊满 4 条);末跳 zorya 不含 rada 的 "msg-a/b"
    all_roasted = all(started for _, started, _ in seq)
    zorya_l1 = hb.histories.get("zorya", [])
    no_leak = not any("msg-a" in m.get("text", "") or "msg-b" in m.get("text", "")
                      for m in zorya_l1)  # rada 的对话内容不进 zorya L1(zorya L1 只有自己的吐槽)
    ok = all_roasted and no_leak
    print(f"  [{'✓' if ok else '✗'}] 每跳吐槽={all_roasted} {[t for _, _, t in seq]} | 无泄露={no_leak}")
    return ok


async def main() -> None:
    _patch_gateway()
    hub = Hub()
    fake = FakeClient()
    hub.clients.add(fake)

    results = []
    with tempfile.TemporaryDirectory() as d:
        results.append(await case1_isolation_roast_archive(d, hub, fake))
        results.append(await case2_no_roast_without_substance(d, hub, fake))
        results.append(await case3_same_persona_noop(d, hub, fake))
        results.append(case4_persona_scoped_recall(d))
        results.append(await case5_has_substance(lambda: _new_hb(d, hub, fake, "c5")))
        results.append(case6_handoff_block())
        results.append(await case7_chain(d, hub, fake))
        results.append(case8_recall_memory_persona_scoped(d))

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} {'✅ 全部通过' if ok else '❌ 有失败项,见上'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
