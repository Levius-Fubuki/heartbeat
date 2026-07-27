"""语音朗读(TTS):edge-tts 在线神经语音为主(自然如真人),硅基流动 CosyVoice2 为可克隆升级,
macOS AVSpeechSynthesizer 为备(断网/出错自动回退,不崩)。

后端(daemon 线程,uvicorn 事件循环)在 agent 回复收尾时调 ``Speaker.speak()``:
  - engine == "edge"(默认):起后台 task 跑 edge-tts 生成 mp3 → ``afplay`` 播放;
    生成/播放失败(断网等)→ 回退 AV。**回复文本会发微软公共 TTS 端点**(隐私权衡,见 voice_engine 旋钮)。
  - engine == "cosyvoice":硅基流动 CosyVoice2(``POST /v1/audio/speech`` → mp3 → afplay);
    5 人格预设声线,丢参考音频可升级到声音克隆;需 siliconflow key,无 key/出错 → 回退 AV。
    **回复文本会发硅基流动**(隐私权衡,见 voice_engine 旋钮)。
  - engine == "av":仅本机 AVSpeechSynthesizer(离线,偏机械)。

edge/sf 后台 task 都是 fire-and-forget:不阻塞 chat_end;新回复开始 / 用户开口 / 静音 时 ``stop()`` 掐断
(取消 task + kill afplay + AV stopSpeaking)。AV 路径经 NSObject 桥 ``performSelectorOnMainThread``
派发到主线程。朗读在**后端触发一次**(非每前端窗口,避回声)。

AVFoundation 懒导入:缺失/失败 → AV 后端标 broken,edge/sf 仍可用;三路皆败 → 静默 no-op。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import tempfile
import threading
from typing import Optional

from Foundation import NSObject, NSDictionary

import objc  # objc.super —— ObjC 子类 init 必须用 objc.super 而非 Python super(否则 ObjCSuperWarning)

log = logging.getLogger("heartbeat.voice")

# 单段朗读字符上限(超长只念前缀;全文仍在聊天气泡里)。env 可调。
VOICE_MAX_CHARS = int(os.getenv("HEARTBEAT_VOICE_MAX_CHARS", "600"))

# emoji / 图文字符正则:剥掉后送 TTS(否则会被当字面念出来,如"火球 emoji")。
# 覆盖:旗区指示符、各类符号图字(emoticons/pictographs/transport/dingbats/misc symbols)、
# 变体选择符(FE0F/FE0E)、零宽连字(200D)、包围 keycap(20E3)等。
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"   # regional indicators(旗帜)
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical symbols
    "\U0001F780-\U0001F7FF"   # geometric shapes ext
    "\U0001F800-\U0001F8FF"   # supplemental arrows-C
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"   # chess symbols
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs ext-A
    "\U00002600-\U000026FF"   # misc symbols(☀★☎✓⚡…)
    "\U00002700-\U000027BF"   # dingbats(✂✈✉✏✓✨❤…)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows(⭐⬛⬜…)
    "\U00002300-\U000023FF"   # misc technical(⌚⌛⏰⏳…)
    "\U0001F004"              # mahjong tile
    "\U0001F0CF"              # playing card
    "\U0000FE0F"              # variation selector-16(emoji 呈现)
    "\U0000FE0E"              # variation selector-15
    "\U0000200D"              # zero-width joiner(复合 emoji)
    "\U000020E3"              # combining enclosing keycap
    "]"
)
_WS_RE = re.compile(r"\s+")


def _strip_for_speech(text: str) -> str:
    """剥 emoji/图字 + 规整空白(多空白合一、首尾去白)。空 → ""(调用方据此跳过朗读)。"""
    text = _EMOJI_RE.sub("", text or "")
    text = _WS_RE.sub(" ", text)
    return text.strip()

# ============================ AV 后端(本机,离线回退)============================
DEFAULT_VOICE_LANG = "zh-CN"
# 5 人格 AV 声线(婷婷 compact voice + pitch/rate_mul 调感;偏机械,仅当 edge 不可用时用)。
PERSONA_VOICE = {
    "vesna": {"pitch": 1.10, "rate_mul": 1.06},
    "rada":   {"pitch": 1.22, "rate_mul": 1.15},
    "yara":   {"pitch": 0.82, "rate_mul": 0.90},
    "mira":   {"pitch": 0.92, "rate_mul": 0.88},
    "zorya":  {"pitch": 0.86, "rate_mul": 0.94},
}
_AV_DEFAULT_CFG = {"pitch": 1.0, "rate_mul": 1.0}

# AVSpeechBoundary.Immediate 的原始枚举值(本版 PyObjC 未导出符号常量;0=立即,1=词边界)。
_BOUNDARY_IMMEDIATE = 0


def _resolve_voice(lang: str = DEFAULT_VOICE_LANG, name: str = None):
    """AV voice 解析(按语言查稳返婷婷;name 给出则按名覆盖)。AVFoundation 懒导入,失败返 None。"""
    try:
        from AVFoundation import AVSpeechSynthesisVoice
    except Exception:  # noqa: BLE001
        return None
    try:
        if name:
            for v in (AVSpeechSynthesisVoice.speechVoices() or []):
                try:
                    if (v.name() or "") == name:
                        return v
                except Exception:  # noqa: BLE001
                    continue
        v = AVSpeechSynthesisVoice.voiceWithLanguage_(lang)
        if v is not None:
            return v
        return AVSpeechSynthesisVoice.defaultVoice()
    except Exception:  # noqa: BLE001
        log.exception("resolve voice failed")
        return None


class _Bridge(NSObject):
    """AV 主线程执行体:synth 必须主线程建/用。壳可 off-main alloc().init();synth 懒建于首次 speak_。"""

    def init(self):
        self = objc.super(_Bridge, self).init()
        if self is None:
            return None
        self._synth = None
        self._voice = None
        return self

    def _ensure(self) -> bool:
        if self._synth is not None:
            return True
        try:
            from AVFoundation import AVSpeechSynthesizer
            self._synth = AVSpeechSynthesizer.alloc().init()
            self._voice = _resolve_voice()
            return True
        except Exception:  # noqa: BLE001
            log.exception("AVSpeechSynthesizer init failed")
            return False

    def speak_(self, payload):
        if not self._ensure():
            return
        try:
            from AVFoundation import AVSpeechUtterance, AVSpeechUtteranceDefaultSpeechRate
            text = payload.objectForKey_("text") or ""
            utt = AVSpeechUtterance.alloc().initWithString_(str(text))
            if self._voice is not None:
                utt.setVoice_(self._voice)
            utt.setPitchMultiplier_(float(payload.objectForKey_("pitch") or 1.0))
            utt.setRate_(AVSpeechUtteranceDefaultSpeechRate * float(payload.objectForKey_("rate_mul") or 1.0))
            utt.setVolume_(max(0.0, min(1.0, float(payload.objectForKey_("volume") or 0.9))))
            self._synth.stopSpeakingAtBoundary_(_BOUNDARY_IMMEDIATE)
            self._synth.speakUtterance_(utt)
        except Exception:  # noqa: BLE001
            log.exception("AV speak failed")

    def stop_(self, sender):
        if self._synth is None:
            return
        try:
            self._synth.stopSpeakingAtBoundary_(_BOUNDARY_IMMEDIATE)
        except Exception:  # noqa: BLE001
            log.exception("AV stop failed")


# ============================ edge-tts 后端(在线神经,主路径)============================
# 5 人格 edge 声线:Xiaoxiao(微软旗舰暖亮)/Xiaoyi(年轻柔和)× rate/pitch 区分。
# rate "+10%" / pitch "+10Hz"(神经声纹移调,不像 compact voice 那样失真)。
PERSONA_EDGE = {
    "vesna": {"voice": "zh-CN-XiaoyiNeural",  "rate": "+6%",  "pitch": "+6Hz"},   # 年轻柔和·好奇
    "rada":  {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+10%", "pitch": "+10Hz"},  # 暖亮·热烈
    "yara":  {"voice": "zh-CN-XiaoxiaoNeural", "rate": "-10%", "pitch": "-16Hz"},  # 冷·低沉
    "mira":  {"voice": "zh-CN-XiaoyiNeural",  "rate": "-8%",  "pitch": "-2Hz"},   # 柔慢·治愈
    "zorya": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "-6%",  "pitch": "-8Hz"},   # 稳·夜感
}
_EDGE_DEFAULT_CFG = {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz"}


class _EdgeBackend:
    """edge-tts 生成 mp3 → afplay 播放。fire-and-forget task;stop() 取消 task + kill afplay。

    一次只播一句:launch 新句前 cancel 旧 task(Speaker.stop 也会调)。
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None

    def launch(self, coro) -> None:
        """取消当前 task + 调度新 coro(必须在 running loop 里调)。"""
        self.stop()
        try:
            self._task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # 无 running loop → 直接跑(阻塞);仅极端情况
            try:
                asyncio.run(coro)
            except Exception:  # noqa: BLE001
                log.exception("edge run (no-loop) failed")

    def stop(self) -> None:
        """取消正在生成/播放的 task + kill afplay 子进程。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass

    async def run(self, text: str, voice: str, rate: str, pitch: str, volume: float) -> None:
        """生成 mp3 到临时文件 → afplay 播放完。任意步骤失败向上抛(由调用方回退 AV)。"""
        import edge_tts  # 懒导入:无网络/未装时 speak 路径才触发
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(path)
            self._proc = await asyncio.create_subprocess_exec(
                "afplay", "-v", f"{volume:.2f}", path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await self._proc.wait()
        finally:
            self._proc = None
            try:
                os.unlink(path)
            except OSError:
                pass


# ============================ SiliconFlow CosyVoice2 后端(在线神经,可声音克隆)============================
# 5 人格 CosyVoice2 预设声线 + 语气 instruction(默认;yaml tts.cosyvoice 可覆盖;丢参考音频可升级到真克隆)。
# 预设声线 id 形如 "<model>:<voice>";声线音色以官方为准,此处是可调默认,不够 5 个则两人格共享。
# instruction 经 <|endofprompt|> 标记前缀注入,给每个人格固定一种情绪基调,让声线本身带人格味。
PERSONA_COSYVOICE = {
    "vesna": {"voice": "FunAudioLLM/CosyVoice2-0.5B:diana", "speed": 1.0,
              "instruction": "用温柔、好奇、带着笑意的语气说"},          # 柔·好奇
    "rada":  {"voice": "FunAudioLLM/CosyVoice2-0.5B:claire", "speed": 1.05,
              "instruction": "用开心、热情、充满活力的语气说"},          # 暖亮·热烈
    "yara":  {"voice": "FunAudioLLM/CosyVoice2-0.5B:bella", "speed": 0.98,
              "instruction": "用清冷、机智、带点调侃的语气说"},          # 冷·调侃
    "mira":  {"voice": "FunAudioLLM/CosyVoice2-0.5B:claire", "speed": 0.92,
              "instruction": "用轻柔、缓慢、平静、抚慰的语气说"},        # 柔慢·治愈
    "zorya": {"voice": "FunAudioLLM/CosyVoice2-0.5B:anna",  "speed": 0.95,
              "instruction": "用平静、克制、轻声、空灵的语气说"},        # 稳·夜感
}
_SF_DEFAULT_CFG = {"voice": "FunAudioLLM/CosyVoice2-0.5B:anna", "speed": 1.0}
_SF_TIMEOUT = 30.0


class _SFBackend:
    """硅基流动 CosyVoice2:POST /v1/audio/speech → mp3 → afplay。fire-and-forget task;stop 取消 + kill afplay。

    voice 与 references **互斥**:cfg 带 references 走声音克隆(参考音频+转写,base64 内联),否则用预设 voice。
    无 key / 断网 / API 错 → 抛异常(由 _speak_sf_or_av 回退 AV)。
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[asyncio.subprocess.Process] = None

    def launch(self, coro) -> None:
        """取消当前 task + 调度新 coro(必须在 running loop 里调)。"""
        self.stop()
        try:
            self._task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # 无 running loop → 直接跑(阻塞);仅极端情况
            try:
                asyncio.run(coro)
            except Exception:  # noqa: BLE001
                log.exception("cosyvoice run (no-loop) failed")

    def stop(self) -> None:
        """取消正在生成/播放的 task + kill afplay 子进程。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass

    async def run(self, text: str, cfg: dict, volume: float) -> None:
        """调硅基流动 TTS 生成 mp3 → afplay 播放。任意失败向上抛(由调用方回退 AV)。"""
        from backend.memory.settings import settings
        from backend.config import SILICONFLOW_TTS_URL, SILICONFLOW_TTS_MODEL
        key = settings.siliconflow_key
        if not key:
            raise RuntimeError("未配置硅基流动 key(在设置页「工具与权限」填,或换 edge/av 引擎)")
        # 可选 instruction(情感/语气)经 <|endofprompt|> 标记前缀注入(CosyVoice2 指令格式)
        instruction = str(cfg.get("instruction") or "").strip()
        inp = f"{instruction} <|endofprompt|>{text}" if instruction else text
        body = {
            "model": cfg.get("model") or SILICONFLOW_TTS_MODEL,
            "input": inp,
            "response_format": "mp3",
            "speed": float(cfg.get("speed") or 1.0),
        }
        refs = cfg.get("references")
        if refs:                                # 声音克隆:references 与 voice 互斥
            body["references"] = refs
        else:
            body["voice"] = cfg.get("voice") or _SF_DEFAULT_CFG["voice"]
        import httpx  # 懒导入:无网络/未装时 speak 路径才触发
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            async with httpx.AsyncClient(timeout=_SF_TIMEOUT) as client:
                r = await client.post(
                    SILICONFLOW_TTS_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=body,
                )
            r.raise_for_status()                # HTTP 错 → 抛(回退 AV)
            with open(path, "wb") as f:
                f.write(r.content)              # 二进制音频(非 JSON)
            self._proc = await asyncio.create_subprocess_exec(
                "afplay", "-v", f"{volume:.2f}", path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await self._proc.wait()
        finally:
            self._proc = None
            try:
                os.unlink(path)
            except OSError:
                pass


# ============================ Speaker 门面 ============================
class Speaker:
    """语音门面:按 settings.voice_engine 选 edge(主)/AV(备);失败回退;stop 掐两路。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # AV 后端
        self._av_bridge = None
        self._av_broken = False
        # edge 后端
        self._edge = _EdgeBackend()
        # SiliconFlow CosyVoice2 后端
        self._sf = _SFBackend()

    # ---------- AV 桥懒建 ----------
    def _ensure_av_bridge(self) -> None:
        if self._av_bridge is not None or self._av_broken:
            return
        with self._lock:
            if self._av_bridge is not None or self._av_broken:
                return
            try:
                from AVFoundation import AVSpeechSynthesizer  # 探测可用
                self._av_bridge = _Bridge.alloc().init()
                log.info("AV voice bridge ready (fallback)")
            except Exception:  # noqa: BLE001
                log.warning("AVFoundation 不可用——AV 回退禁用(edge 仍可用)")
                self._av_broken = True

    # ---------- 声线配置 ----------
    def _av_cfg_for(self, persona_id: str) -> dict:
        from backend.config import PERSONAS
        cfg = dict(_AV_DEFAULT_CFG)
        cfg.update(PERSONA_VOICE.get(persona_id, {}))
        tts = (PERSONAS.get(persona_id) or {}).get("tts") or {}
        for k in ("pitch", "rate_mul"):
            if tts.get(k) is not None:
                try:
                    cfg[k] = float(tts[k])
                except (TypeError, ValueError):
                    pass
        return cfg

    def _edge_cfg_for(self, persona_id: str) -> dict:
        from backend.config import PERSONAS
        cfg = dict(_EDGE_DEFAULT_CFG)
        cfg.update(PERSONA_EDGE.get(persona_id, {}))
        edge = ((PERSONAS.get(persona_id) or {}).get("tts") or {}).get("edge") or {}
        for k in ("voice", "rate", "pitch"):
            if edge.get(k):
                cfg[k] = str(edge[k])
        return cfg

    def _sf_cfg_for(self, persona_id: str) -> dict:
        """CosyVoice2 声线配置:_SF_DEFAULT_CFG ← PERSONA_COSYVOICE[pid] ← persona yaml tts.cosyvoice。

        yaml tts.cosyvoice 可覆盖 voice/speed/instruction;若给了 reference{path, transcript} 且文件
        存在 → 走声音克隆(base64 data-uri 内联 references,与 voice 互斥),否则预设 voice。
        """
        from backend.config import PERSONAS, SILICONFLOW_TTS_MODEL
        cfg = {
            "voice": _SF_DEFAULT_CFG["voice"],
            "speed": _SF_DEFAULT_CFG["speed"],
            "model": SILICONFLOW_TTS_MODEL,
            "instruction": "",
        }
        cfg.update(PERSONA_COSYVOICE.get(persona_id, {}))
        cv = ((PERSONAS.get(persona_id) or {}).get("tts") or {}).get("cosyvoice") or {}
        for k in ("voice", "speed", "instruction"):
            if cv.get(k) is not None:
                cfg[k] = cv[k]
        # 声音克隆:参考音频 + 转写 → base64 内联(与 voice 互斥)
        ref = cv.get("reference") or {}
        ref_path = str(ref.get("path") or "").strip()
        transcript = str(ref.get("transcript") or "").strip()
        if ref_path and transcript and os.path.exists(ref_path):
            try:
                with open(ref_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                cfg["references"] = [{"audio": f"data:audio/wav;base64,{b64}", "text": transcript}]
                cfg.pop("voice", None)          # references 与 voice 互斥
            except OSError:  # noqa: BLE001
                log.warning("cosyvoice 参考音频读取失败(%s)——退回预设声线", ref_path)
        return cfg

    # ---------- 朗读 ----------
    def speak(self, text: str, persona_id: str) -> None:
        """朗读一段 agent 回复。禁用/空/超长 → no-op;否则按引擎派发(edge 失败自动回退 AV)。"""
        from backend.memory.settings import settings
        if not settings.voice_enabled:
            return
        text = _strip_for_speech(text)            # 剥 emoji/图字 + 规整空白(只影响朗读,气泡里 emoji 保留)
        if not text:
            return
        if len(text) > VOICE_MAX_CHARS:
            text = text[:VOICE_MAX_CHARS]
        self.stop()                       # 先掐断旧朗读(避叠音)
        vol = max(0, min(100, settings.voice_volume)) / 100.0
        if settings.voice_engine == "edge":
            try:
                asyncio.get_running_loop()           # 确认在事件循环里(heartbeat 调用必在)
            except RuntimeError:                      # 同步上下文 → 直接 AV
                self._speak_av(text, persona_id, vol)
                return
            cfg = self._edge_cfg_for(persona_id)
            self._edge.launch(self._speak_edge_or_av(text, cfg, vol, persona_id))
            return
        if settings.voice_engine == "cosyvoice":
            try:
                asyncio.get_running_loop()           # 确认在事件循环里(heartbeat 调用必在)
            except RuntimeError:                      # 同步上下文 → 直接 AV
                self._speak_av(text, persona_id, vol)
                return
            cfg = self._sf_cfg_for(persona_id)
            self._sf.launch(self._speak_sf_or_av(text, cfg, vol, persona_id))
            return
        # engine == "av":仅本机
        self._speak_av(text, persona_id, vol)

    async def _speak_edge_or_av(self, text: str, cfg: dict, vol: float, persona_id: str) -> None:
        """edge 生成+播放;任意失败(断网/edge 未装)→ 回退 AV。Cancelled(被 stop)不回退。"""
        try:
            await self._edge.run(text, cfg["voice"], cfg["rate"], cfg["pitch"], vol)
            log.info("edge spoke (%d chars, voice=%s)", len(text), cfg["voice"])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("edge-tts 不可用(%s)——回退本机 AV", e)
            try:
                self._speak_av(text, persona_id, vol)
            except Exception:  # noqa: BLE001
                log.exception("AV 回退也失败——本句静默")

    async def _speak_sf_or_av(self, text: str, cfg: dict, vol: float, persona_id: str) -> None:
        """CosyVoice2 生成+播放;任意失败(无 key/断网/API 错)→ 回退 AV。Cancelled(被 stop)不回退。"""
        try:
            await self._sf.run(text, cfg, vol)
            mode = "clone" if cfg.get("references") else cfg.get("voice", "preset")
            log.info("cosyvoice spoke (%d chars, %s)", len(text), mode)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cosyvoice 不可用(%s)——回退本机 AV", e)
            try:
                self._speak_av(text, persona_id, vol)
            except Exception:  # noqa: BLE001
                log.exception("AV 回退也失败——本句静默")

    def _speak_av(self, text: str, persona_id: str, vol: float) -> None:
        """AV 路径:派发到主线程。AV 不可用 → no-op。"""
        if self._av_broken:
            return
        self._ensure_av_bridge()
        if self._av_bridge is None:
            return
        cfg = self._av_cfg_for(persona_id)
        payload = NSDictionary.dictionaryWithDictionary_({
            "text": text,
            "pitch": cfg["pitch"],
            "rate_mul": cfg["rate_mul"],
            "volume": vol,
        })
        try:
            self._av_bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
                "speak:", payload, False)
        except Exception:  # noqa: BLE001
            log.warning("AV speak dispatch failed——AV 禁用")
            self._av_broken = True

    # ---------- 停止 ----------
    def stop(self) -> None:
        """掐断当前朗读(edge/sf task + afplay + AV stopSpeaking 全发,谁在播谁停)。"""
        self._edge.stop()
        self._sf.stop()
        if not self._av_broken and self._av_bridge is not None:
            try:
                self._av_bridge.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "stop:", None, False)
            except Exception:  # noqa: BLE001
                pass
