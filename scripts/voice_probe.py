"""语音朗读(TTS)探针:edge 在线神经(主)+ cosyvoice 硅基流动(可克隆)+ AV 本机(备)三后端。

不起服务、不联网、不播音。AV 原生桥用 FakeAVBridge 替,edge/sf 后端用 FakeEdge/FakeSF 替(录 launch/run,
可选模拟失败测回退)。settings 用 _data 直注。

  A1-A5  AV 路径(engine=av):禁用/空不派发;正常派发带 cfg+音量;超长截断;音量夹取。
  B6-B11 声线解析:_av_cfg_for/_edge_cfg_for/_sf_cfg_for(预设>默认 + tts.cosyvoice 覆盖/克隆)。
  C11-13 stop:edge.stop + sf.stop + AV stop 都调;_broken/_bridge None 不抛。
  G14-21 路由:edge(engine=edge 主/回退/av 旁路)+ cosyvoice(engine=cosyvoice:launch/cfg/失败回退/无 key 抛)。
  E18-20 heartbeat 集成:_begin/_finish helper 发 chat_start/chat_end payload 不变 + voice.stop/speak。
  F21-23 KNOBS:_coerce choice(voice_engine edge/av/cosyvoice/非法→None)+ int 夹取 + public_view 含键。

用法:.venv/bin/python scripts/voice_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import PERSONAS  # noqa: E402
from backend.memory.l2 import Profile  # noqa: E402
from backend.memory.l3 import EpisodicMemory  # noqa: E402
from backend.memory.settings import settings, _coerce, _KNOB_BY_KEY  # noqa: E402
from backend.voice import Speaker, PERSONA_VOICE, PERSONA_EDGE, PERSONA_COSYVOICE, VOICE_MAX_CHARS, _strip_for_speech  # noqa: E402
import backend.voice as voice_mod  # noqa: E402
from backend.ws import Hub  # noqa: E402
from backend.heartbeat import Heartbeat  # noqa: E402


# ---- 假 AV 原生桥(录派发)----
class FakeAVBridge:
    def __init__(self) -> None:
        self.dispatched: list = []

    def performSelectorOnMainThread_withObject_waitUntilDone_(self, sel, payload, wait):  # noqa: N802
        d = {}
        try:
            for k in ("text", "pitch", "rate_mul", "volume"):
                d[k] = payload.objectForKey_(k)
        except Exception:  # noqa: BLE001
            d = {"_raw": str(payload)}
        self.dispatched.append((sel, d))


# ---- 假 edge 后端(录 launch/run,可选失败)----
class FakeEdge:
    def __init__(self, fail: bool = False) -> None:
        self.coros: list = []
        self.runs: list = []
        self.stops = 0
        self._fail = fail

    def launch(self, coro) -> None:
        self.coros.append(coro)

    def stop(self) -> None:
        self.stops += 1

    async def run(self, text, voice, rate, pitch, volume):
        self.runs.append({"text": text, "voice": voice, "rate": rate, "pitch": pitch, "volume": volume})
        if self._fail:
            raise RuntimeError("simulated edge failure")


# ---- 假 SiliconFlow(CosyVoice2)后端(录 launch/run,可选失败)----
class FakeSF:
    def __init__(self, fail: bool = False) -> None:
        self.coros: list = []
        self.runs: list = []
        self.stops = 0
        self._fail = fail

    def launch(self, coro) -> None:
        self.coros.append(coro)

    def stop(self) -> None:
        self.stops += 1

    async def run(self, text, cfg, volume):
        self.runs.append({"text": text, "cfg": dict(cfg), "volume": volume})
        if self._fail:
            raise RuntimeError("simulated cosyvoice failure")


def _av_speaker() -> tuple[Speaker, FakeAVBridge]:
    """engine=av + 装 FakeAVBridge 的 Speaker(_av_bridge 预置 → 不碰 AVFoundation)。"""
    s = Speaker()
    fb = FakeAVBridge()
    s._av_bridge = fb
    s._av_broken = False
    _set_knobs(voice_engine="av")
    return s, fb


def _set_knobs(**kv):
    old = {k: settings._data.get(k) for k in kv}
    for k, v in kv.items():
        settings._data[k] = v
    return old


def _restore(old: dict) -> None:
    for k, v in old.items():
        settings._data[k] = v


def _last_av_speak(fb: FakeAVBridge):
    for sel, d in reversed(fb.dispatched):
        if sel == "speak:":
            return d
    return None


# ============================ A: AV 路径(engine=av)============================
def case_a() -> int:
    fails = 0
    print("=== A:AV 路径(engine=av;禁用/空/正常/截断/音量)===")

    s, fb = _av_speaker()
    _set_knobs(voice_enabled=False, voice_volume=90)
    s.speak("hi", "rada")
    ok = len(fb.dispatched) == 0
    print(f"  [{'✓' if ok else '✗'}] A1 voice_enabled=False → 不派发(dispatched={len(fb.dispatched)})")
    fails += not ok

    s, fb = _av_speaker()
    _set_knobs(voice_enabled=True, voice_volume=90)
    s.speak("   ", "rada")
    ok = len(fb.dispatched) == 0
    print(f"  [{'✓' if ok else '✗'}] A2 空白文本 → 不派发")
    fails += not ok

    s, fb = _av_speaker()
    _set_knobs(voice_enabled=True, voice_volume=90)
    s.speak("你好呀", "rada")
    d = _last_av_speak(fb)
    cfg = PERSONA_VOICE["rada"]
    ok = (d and d["text"] == "你好呀" and abs(d["pitch"] - cfg["pitch"]) < 1e-6
          and abs(d["rate_mul"] - cfg["rate_mul"]) < 1e-6 and abs(d["volume"] - 0.9) < 1e-6)
    print(f"  [{'✓' if ok else '✗'}] A3 正常 rada → AV 派发 cfg+音量(d={d})")
    fails += not ok

    s, fb = _av_speaker()
    _set_knobs(voice_enabled=True, voice_volume=50)
    saved = voice_mod.VOICE_MAX_CHARS
    voice_mod.VOICE_MAX_CHARS = 10
    try:
        s.speak("abcdefghijklmnopqrstuvwxyz", "zorya")
    finally:
        voice_mod.VOICE_MAX_CHARS = saved
    d = _last_av_speak(fb)
    ok = d and d["text"] == "abcdefghij" and abs(d["volume"] - 0.5) < 1e-6
    print(f"  [{'✓' if ok else '✗'}] A4 超 VOICE_MAX_CHARS → 截前缀(text={d['text'] if d else None!r})")
    fails += not ok

    s, fb = _av_speaker()
    _set_knobs(voice_enabled=True, voice_volume=200)
    s.speak("loud", "vesna")
    d1 = _last_av_speak(fb)
    s2, fb2 = _av_speaker()
    _set_knobs(voice_enabled=True, voice_volume=-5)
    s2.speak("quiet", "vesna")
    d2 = _last_av_speak(fb2)
    ok = d1 and abs(d1["volume"] - 1.0) < 1e-6 and d2 and abs(d2["volume"] - 0.0) < 1e-6
    print(f"  [{'✓' if ok else '✗'}] A5 音量夹取(200→{d1['volume'] if d1 else None}, -5→{d2['volume'] if d2 else None})")
    fails += not ok

    return fails


# ============================ B: 声线解析 ============================
def case_b() -> int:
    fails = 0
    print("\n=== B:声线解析(AV _av_cfg_for + edge _edge_cfg_for)===")

    s, _ = _av_speaker()
    cfg = s._av_cfg_for("rada")
    ok = (abs(cfg["pitch"] - PERSONA_VOICE["rada"]["pitch"]) < 1e-6
          and abs(cfg["rate_mul"] - PERSONA_VOICE["rada"]["rate_mul"]) < 1e-6)
    print(f"  [{'✓' if ok else '✗'}] B6 _av_cfg_for rada → PERSONA_VOICE(cfg={cfg})")
    fails += not ok

    cfg = s._edge_cfg_for("rada")
    exp = PERSONA_EDGE["rada"]
    ok = cfg["voice"] == exp["voice"] and cfg["rate"] == exp["rate"] and cfg["pitch"] == exp["pitch"]
    print(f"  [{'✓' if ok else '✗'}] B7 _edge_cfg_for rada → PERSONA_EDGE(cfg={cfg})")
    fails += not ok

    cfg = s._edge_cfg_for("__nope__")
    ok = cfg["voice"] == "zh-CN-XiaoxiaoNeural"   # _EDGE_DEFAULT_CFG
    print(f"  [{'✓' if ok else '✗'}] B8 _edge_cfg_for 未知人格 → 默认 Xiaoxiao(cfg.voice={cfg['voice']})")
    fails += not ok

    pid = "__tts_edge__"
    PERSONAS[pid] = {"tts": {"edge": {"voice": "zh-CN-XiaoyiNeural", "rate": "+20%", "pitch": "-30Hz"}}}
    try:
        cfg = s._edge_cfg_for(pid)
        ok = (cfg["voice"] == "zh-CN-XiaoyiNeural" and cfg["rate"] == "+20%" and cfg["pitch"] == "-30Hz")
    finally:
        PERSONAS.pop(pid, None)
    print(f"  [{'✓' if ok else '✗'}] B9 persona yaml tts.edge 全覆盖(cfg={cfg})")
    fails += not ok

    # 5 人格声线各不同(voice 或 pitch 至少有区分)
    voices = {pid: (PERSONA_EDGE[pid]["voice"], PERSONA_EDGE[pid]["pitch"]) for pid in PERSONA_EDGE}
    ok = len(voices) == 5 and len(set(voices.values())) == 5
    print(f"  [{'✓' if ok else '✗'}] B10 5 人格 edge 声线(voice,pitch)两两不同({voices})")
    fails += not ok

    # B11 _sf_cfg_for:预设默认 + instruction + tts.cosyvoice 覆盖 + reference 克隆 + 文件缺失回退
    cfg = s._sf_cfg_for("rada")
    ok = (cfg["voice"] == PERSONA_COSYVOICE["rada"]["voice"]
          and abs(cfg["speed"] - PERSONA_COSYVOICE["rada"]["speed"]) < 1e-6
          and cfg.get("model") and cfg.get("instruction") == PERSONA_COSYVOICE["rada"]["instruction"]
          and cfg.get("instruction") and "references" not in cfg)
    print(f"  [{'✓' if ok else '✗'}] B11a _sf_cfg_for rada → PERSONA_COSYVOICE 预设+instruction(cfg={cfg})")
    fails += not ok

    pid = "__tts_cv__"
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.write(fd, b"RIFF\x00\x00\x00\x00WAVEfmt ")      # 假 wav 字节(_sf_cfg_for 只读字节 base64,不校验格式)
    os.close(fd)
    PERSONAS[pid] = {"tts": {"cosyvoice": {
        "voice": "FunAudioLLM/CosyVoice2-0.5B:claire", "speed": 1.1, "instruction": "开心地说",
        "reference": {"path": wav, "transcript": "你好,这是一段参考音频"},
    }}}
    try:
        cfg = s._sf_cfg_for(pid)
        ok_clone = ("references" in cfg and "voice" not in cfg
                    and cfg["references"][0]["audio"].startswith("data:audio/wav;base64,")
                    and cfg["references"][0]["text"] == "你好,这是一段参考音频"
                    and cfg["instruction"] == "开心地说" and abs(cfg["speed"] - 1.1) < 1e-6)
        # reference 文件不存在 → 退回预设 voice(不抛)
        PERSONAS[pid]["tts"]["cosyvoice"]["reference"]["path"] = "/no/such/file.wav"
        cfg2 = s._sf_cfg_for(pid)
        ok_fallback = "references" not in cfg2 and cfg2["voice"] == "FunAudioLLM/CosyVoice2-0.5B:claire"
    finally:
        PERSONAS.pop(pid, None)
        try:
            os.unlink(wav)
        except OSError:
            pass
    print(f"  [{'✓' if ok_clone else '✗'}] B11b tts.cosyvoice.reference → 克隆 references(base64)+ 删 voice + 覆盖 instruction/speed")
    fails += not ok_clone
    print(f"  [{'✓' if ok_fallback else '✗'}] B11c reference 文件不存在 → 退回预设 voice(不抛)")
    fails += not ok_fallback

    return fails


# ============================ C: stop ============================
def case_c() -> int:
    fails = 0
    print("\n=== C:stop(edge.stop + AV stop 都调;降级不抛)===")

    s, fb = _av_speaker()
    s._edge = FakeEdge()
    s._sf = FakeSF()
    s.stop()
    ok = s._edge.stops == 1 and s._sf.stops == 1 and any(sel == "stop:" for sel, _ in fb.dispatched)
    print(f"  [{'✓' if ok else '✗'}] C11 stop → edge.stop={s._edge.stops} + sf.stop={s._sf.stops} + AV stop 派发={any(sel=='stop:' for sel,_ in fb.dispatched)}")
    fails += not ok

    s = Speaker(); s._av_broken = True; s._edge = FakeEdge()
    try:
        s.stop(); ok = True
    except Exception:  # noqa: BLE001
        ok = False
    print(f"  [{'✓' if ok else '✗'}] C12 stop(_av_broken) → no-op 不抛")
    fails += not ok

    s = Speaker(); s._av_bridge = None; s._edge = FakeEdge()
    try:
        s.stop(); ok = True
    except Exception:  # noqa: BLE001
        ok = False
    print(f"  [{'✓' if ok else '✗'}] C13 stop(_av_bridge=None) → no-op 不抛")
    fails += not ok

    return fails


# ============================ G: edge 路由 + 回退 ============================
async def case_g() -> int:
    fails = 0
    print("\n=== G:edge 路由(engine=edge;主路径/回退/av 旁路)===")

    # G14 engine=edge → 起 edge.launch 1 次,不碰 AV
    s = Speaker()
    s._edge = FakeEdge()
    s._av_bridge = None          # 确保没走 AV
    _set_knobs(voice_engine="edge", voice_enabled=True, voice_volume=90)
    s.speak("你好", "rada")
    ok = len(s._edge.coros) == 1
    print(f"  [{'✓' if ok else '✗'}] G14 engine=edge speak → edge.launch={len(s._edge.coros)} 次(期望 1)")
    fails += not ok

    # G15 cfg 正确透传(await coro → FakeEdge.run 记录)
    await s._edge.coros[0]
    r = s._edge.runs[-1] if s._edge.runs else None
    exp = PERSONA_EDGE["rada"]
    ok = (r and r["voice"] == exp["voice"] and r["rate"] == exp["rate"]
          and r["pitch"] == exp["pitch"] and abs(r["volume"] - 0.9) < 1e-6 and r["text"] == "你好")
    print(f"  [{'✓' if ok else '✗'}] G15 edge run 收到 rada cfg + 文本 + 音量(run={r})")
    fails += not ok

    # G16 edge run 失败 → 回退 AV(_speak_edge_or_av 调 _speak_av)
    s2 = Speaker()
    s2._edge = FakeEdge(fail=True)
    fb = FakeAVBridge()
    s2._av_bridge = fb
    s2._av_broken = False
    _set_knobs(voice_engine="edge", voice_enabled=True, voice_volume=80)
    await s2._speak_edge_or_av("坏了", s2._edge_cfg_for("yara"), 0.8, "yara")
    d = _last_av_speak(fb)
    ok = (len(s2._edge.runs) == 1 and d and d["text"] == "坏了"        # edge 试过 + AV 兜底派发
          and abs(d["volume"] - 0.8) < 1e-6)
    print(f"  [{'✓' if ok else '✗'}] G16 edge run 失败 → 回退 AV 派发(edge.runs={len(s2._edge.runs)}, AV d={d})")
    fails += not ok

    # G17 engine=av → 走 _speak_av,edge 不动
    s3, fb3 = _av_speaker()
    s3._edge = FakeEdge()
    _set_knobs(voice_engine="av", voice_enabled=True, voice_volume=90)
    s3.speak("只走本机", "zorya")
    ok = len(s3._edge.coros) == 0 and _last_av_speak(fb3) is not None
    print(f"  [{'✓' if ok else '✗'}] G17 engine=av speak → edge.launch=0 + AV 派发={_last_av_speak(fb3) is not None}")
    fails += not ok

    # G18 engine=cosyvoice → sf.launch 1 次,不碰 AV
    s4 = Speaker()
    s4._sf = FakeSF()
    s4._av_bridge = None          # 确保没走 AV
    _set_knobs(voice_engine="cosyvoice", voice_enabled=True, voice_volume=90)
    s4.speak("你好", "rada")
    ok = len(s4._sf.coros) == 1
    print(f"  [{'✓' if ok else '✗'}] G18 engine=cosyvoice speak → sf.launch={len(s4._sf.coros)} 次(期望 1)")
    fails += not ok

    # G19 cfg 透传(await coro → FakeSF.run 记录 PERSONA_COSYVOICE)
    await s4._sf.coros[0]
    r = s4._sf.runs[-1] if s4._sf.runs else None
    exp = PERSONA_COSYVOICE["rada"]
    ok = (r and r["cfg"]["voice"] == exp["voice"] and abs(r["cfg"]["speed"] - exp["speed"]) < 1e-6
          and r["cfg"].get("model") and r["text"] == "你好" and abs(r["volume"] - 0.9) < 1e-6)
    print(f"  [{'✓' if ok else '✗'}] G19 sf run 收到 rada cfg(voice/speed/model)+ 文本 + 音量(run={r})")
    fails += not ok

    # G20 sf run 失败 → 回退 AV(_speak_sf_or_av 调 _speak_av)
    s5 = Speaker()
    s5._sf = FakeSF(fail=True)
    fb5 = FakeAVBridge()
    s5._av_bridge = fb5
    s5._av_broken = False
    _set_knobs(voice_engine="cosyvoice", voice_enabled=True, voice_volume=80)
    await s5._speak_sf_or_av("坏了", s5._sf_cfg_for("yara"), 0.8, "yara")
    d = _last_av_speak(fb5)
    ok = (len(s5._sf.runs) == 1 and d and d["text"] == "坏了" and abs(d["volume"] - 0.8) < 1e-6)
    print(f"  [{'✓' if ok else '✗'}] G20 sf run 失败 → 回退 AV 派发(sf.runs={len(s5._sf.runs)}, AV d={d})")
    fails += not ok

    # G21 真 _SFBackend.run 无 key → 抛 RuntimeError(无网络;gate 测)
    import backend.memory.settings as settings_mod
    saved_env = settings_mod.HEARTBEAT_SILICONFLOW_KEY
    saved_data = settings._data.get("siliconflow_key")
    settings_mod.HEARTBEAT_SILICONFLOW_KEY = ""
    settings._data["siliconflow_key"] = None
    try:
        raised = False
        try:
            await Speaker()._sf.run("x", Speaker()._sf_cfg_for("rada"), 0.9)
        except RuntimeError as e:
            raised = "key" in str(e)
        ok = raised
    finally:
        settings_mod.HEARTBEAT_SILICONFLOW_KEY = saved_env
        settings._data["siliconflow_key"] = saved_data
    print(f"  [{'✓' if ok else '✗'}] G21 _SFBackend.run 无 key → 抛 RuntimeError(无网络 gate 测)")
    fails += not ok

    return fails


# ============================ E: heartbeat 集成 ============================
class FakeSpeaker:
    def __init__(self) -> None:
        self.stops = 0
        self.speaks: list = []

    def stop(self) -> None:
        self.stops += 1

    def speak(self, text, persona_id) -> None:
        self.speaks.append((text, persona_id))


class FakeClient:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, text: str) -> None:
        self.sent.append(__import__("json").loads(text))


def _new_hb(d: str, hub: Hub) -> Heartbeat:
    hb = Heartbeat(hub)
    hb.profile = Profile(path=Path(d) / "profile_vp.yaml")
    hb.episodic = EpisodicMemory(path=Path(d) / "e_vp.jsonl")
    hb.persona_id = "rada"
    hb.histories = {hb.persona_id: []}
    hb._refresh_episodic_block()
    return hb


async def case_e(d: str, hub: Hub, fake: FakeClient) -> int:
    fails = 0
    print("\n=== E:heartbeat 集成(helper 发 chat_start/chat_end 不变 + voice 调用)===")

    hb = _new_hb(d, hub)
    fs = FakeSpeaker()
    hb.voice = fs

    fake.sent.clear()
    await hb._begin_agent_utterance(task=True)
    starts = [m for m in fake.sent if m.get("type") == "chat_start"]
    ok = (len(starts) == 1 and starts[0].get("task") is True and starts[0].get("role") == "agent" and fs.stops == 1)
    print(f"  [{'✓' if ok else '✗'}] E18 _begin_agent_utterance(task=True) → chat_start={starts[-1] if starts else None} | voice.stop={fs.stops}")
    fails += not ok

    fake.sent.clear()
    await hb._finish_agent_utterance("hello world")
    ends = [m for m in fake.sent if m.get("type") == "chat_end"]
    ok = (len(ends) == 1 and ends[0].get("text") == "hello world"
          and len(fs.speaks) == 1 and fs.speaks[0] == ("hello world", "rada"))
    print(f"  [{'✓' if ok else '✗'}] E19 _finish_agent_utterance → chat_end.text='hello world' | voice.speak={fs.speaks}")
    fails += not ok

    fake.sent.clear()
    await hb._begin_agent_utterance()
    starts = [m for m in fake.sent if m.get("type") == "chat_start"]
    ok = len(starts) == 1 and "task" not in starts[0]
    print(f"  [{'✓' if ok else '✗'}] E20 _begin_agent_utterance() 无 task → chat_start 不含 task 键")
    fails += not ok

    return fails


# ============================ H: emoji 剥离(送 TTS 前清图字)============================
def case_h() -> int:
    fails = 0
    print("\n=== H:emoji 剥离(_strip_for_speech + speak 不念 emoji)===")

    cases = [
        ("嘿嘿～我在这！😆🔥 走起", "嘿嘿～我在这！ 走起"),
        ("爱你 ❤️", "爱你"),
        ("中国 🇨🇳 加油", "中国 加油"),
        ("✓ 完成 ★ 标记", "完成 标记"),
    ]
    ok_all = True
    for src, exp in cases:
        out = _strip_for_speech(src)
        if out != exp:
            ok_all = False
            print(f"    ✗ {src!r} → {out!r}(期望 {exp!r})")
    print(f"  [{'✓' if ok_all else '✗'}] H1 _strip_for_speech 剥 emoji/旗帜/图字,保留正文")
    fails += not ok_all

    ok = _strip_for_speech("🔥🔥🔥😆") == ""
    print(f"  [{'✓' if ok else '✗'}] H2 全 emoji → 空(→speak 跳过朗读)")
    fails += not ok

    # speak:emoji-only 不派发;text+emoji 派发的是已剥 emoji 的干净文本(engine=av + FakeAVBridge)
    s, fb = _av_speaker()
    _set_knobs(voice_enabled=True, voice_volume=90)
    s.speak("🔥😆", "rada")
    d0 = _last_av_speak(fb)
    s.speak("我在呢 😆🔥", "rada")
    d1 = _last_av_speak(fb)
    ok = (d0 is None and d1 and "😆" not in d1["text"] and "🔥" not in d1["text"]
          and d1["text"] == "我在呢")
    print(f"  [{'✓' if ok else '✗'}] H3 speak:'🔥😆' 不派发(d0={d0});'我在呢 😆🔥' 派发干净文本(d1.text={d1['text'] if d1 else None!r})")
    fails += not ok

    return fails


# ============================ F: KNOBS / _coerce ============================
def case_f() -> int:
    fails = 0
    print("\n=== F:KNOBS(choice _coerce + public_view 含键)===")

    ek = _KNOB_BY_KEY["voice_engine"]
    vk = _KNOB_BY_KEY["voice_volume"]
    ok = (_coerce(ek, "edge") == "edge" and _coerce(ek, "av") == "av"
          and _coerce(ek, "cosyvoice") == "cosyvoice"
          and _coerce(ek, "bogus") is None and _coerce(ek, "") is None)
    print(f"  [{'✓' if ok else '✗'}] F21 _coerce choice voice_engine:edge/av/cosyvoice 接受、bogus/空→None({_coerce(ek,'edge')}/{_coerce(ek,'av')}/{_coerce(ek,'cosyvoice')}/{_coerce(ek,'bogus')})")
    fails += not ok

    ok = (_coerce(vk, 150) == 100 and _coerce(vk, -5) == 0)
    print(f"  [{'✓' if ok else '✗'}] F22 _coerce int voice_volume 夹 0-100(150→{_coerce(vk,150)}, -5→{_coerce(vk,-5)})")
    fails += not ok

    view = settings.public_view()
    cfg = view.get("config", {})
    ok = ("voice_engine" in cfg and "voice_enabled" in cfg and "voice_volume" in cfg
          and ek["default"] == "edge" and ek["type"] == "choice" and ek["options"] == ["edge", "av", "cosyvoice"])
    print(f"  [{'✓' if ok else '✗'}] F23 public_view.config 含 voice_engine/enabled/volume + 元数据 default=edge choice[edge,av,cosyvoice]")
    fails += not ok

    return fails


async def amain() -> int:
    old = _set_knobs(voice_enabled=True, voice_volume=90, voice_engine="edge")
    total = 0
    try:
        total += case_a()
        total += case_b()
        total += case_c()
        total += case_h()
        total += await case_g()
        with tempfile.TemporaryDirectory() as d:
            hub = Hub()
            fake = FakeClient()
            hub.clients.add(fake)
            total += await case_e(d, hub, fake)
        total += case_f()
    finally:
        _restore(old)
    return total


def main() -> int:
    total = asyncio.run(amain())
    print(f"\n{'='*48}\nvoice_probe: {total} 失败" + (" — 全过 ✅" if total == 0 else " — 有失败 ❌"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
