"""运行时可调设置:LLM 凭据/模型 + 部分 config 旋钮。落盘 memory/settings.yaml(原子写)。

模块级单例 ``settings``。每个访问器**回退到 config.py/env 默认**——首次无 settings.yaml 时
完全等同现状(``.env`` 继续作默认凭据源),向后兼容;用户在前端配置后 settings.yaml 覆盖默认。

照搬 Profile 范式:RLock + 原子写(tmp.{pid} + os.replace)+ load 合并默认。
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from backend.config import (
    COMMAND_ALLOWLIST, EMBED_ENABLED, HEARTBEAT_INTERVAL_SEC, HEARTBEAT_SILICONFLOW_KEY, HEARTBEAT_TAVILY_KEY,
    INTERVIEW_MAX_ROUNDS, INTERVIEW_SILENCE_SEC,
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MEMORY_DIR, REACT_MAX_ROUNDS, REACT_TASK_MAX_TOKENS,
    RECALL_TOP_K, SILENCE_TIMEOUT_SEC, TOOL_INCLUDE_WINDOW_TITLE,
)

log = logging.getLogger("heartbeat.memory.settings")

_PATH = MEMORY_DIR / "settings.yaml"

# 默认空设置(所有值 None = 回退 config 默认)。嵌套用 deepcopy 避免共享引用。
_EMPTY = {
    "provider": "deepseek",            # 厂商(目前仅 deepseek;UI 下拉可扩展)
    "base_url": None,                  # None → config.LLM_BASE_URL / .env
    "api_key": None,                   # None → config.LLM_API_KEY / .env
    "model": None,                     # None → config.LLM_MODEL / .env
    "models": [],                      # 从 /models 拉取的模型 id 缓存(供主窗 chip 下拉)
    # 旋钮(None = 回退 config 默认)
    "heartbeat_interval": None,
    "silence_timeout": None,
    "interview_silence": None,
    "interview_max_rounds": None,
    "recall_top_k": None,
    "tool_include_window_title": None,
    "react_max_rounds": None,
    "react_task_max_tokens": None,          # 任务模式回复长度上限(Phase 6a)
    "task_mode_enabled": None,              # 任务模式总开关(Phase 6a;关掉则全部走陪伴模式)
    "file_tools_enabled": None,             # 文件只读工具开关(Phase 6b;任务模式 read_file/list_dir/glob)
    "web_search_enabled": None,             # 联网搜索开关(Phase 6b;任务模式 web_search)
    "write_tools_enabled": None,            # 写入工具开关(Phase 6d;任务模式 write_file/make_dir/trash_file;默认关)
    "run_command_enabled": None,            # 执行命令开关(Phase 6d;任务模式 run_command;默认关)
    "embed_enabled": None,
    "voice_enabled": None,                  # 语音朗读开关(Phase 语音;agent 回复/心跳/提醒 TTS)
    "voice_volume": None,                   # 语音音量 0-100(整数百分比;避开 _coerce 不支持 float)
    "voice_engine": None,                   # 语音引擎 edge(在线神经,主)/av(本机回退)/cosyvoice(硅基流动,克隆 TTS)
    # Phase 6b/6d 非旋钮(str/list,_coerce 不支持,走 update 的 secrets/paths 段)
    "tavily_key": None,                     # None → config.HEARTBEAT_TAVILY_KEY / .env;public_view 掩码不回完整
    "siliconflow_key": None,                # None → config.HEARTBEAT_SILICONFLOW_KEY / .env;cosyvoice 引擎用,掩码不回完整
    "allowed_dirs": [],                     # 文件工具额外允许的目录(默认根 桌面/文档/下载 始终放行,这里只能加)
    "command_allowlist": None,              # run_command 白名单(None → config.COMMAND_ALLOWLIST 默认;空列表=禁用 run_command)
    "updated_at": None,
}

# 暴露给前端的旋钮元数据(标签/说明/类型/范围/是否重启生效/默认值/隐私标记)。
# 默认值取自 config(=当前 .env 或代码默认),前端据此显示与回退。
KNOBS = [
    {"key": "heartbeat_interval", "label": "心跳间隔", "type": "int", "min": 2, "max": 300,
     "unit": "秒", "restart": False,
     "desc": "多久做一次心跳检查(探测你的电脑状态)。越短越敏锐、也越费 token。",
     "default": HEARTBEAT_INTERVAL_SEC},
    {"key": "silence_timeout", "label": "静默结束对话", "type": "int", "min": 10, "max": 1800,
     "unit": "秒", "restart": False,
     "desc": "聊天中你多久不说话,它就认为这段对话结束、回到默默守候。",
     "default": SILENCE_TIMEOUT_SEC},
    {"key": "recall_top_k", "label": "记忆召回条数", "type": "int", "min": 1, "max": 10,
     "unit": "条", "restart": False,
     "desc": "回忆过往对话时向量检索返回几条。多则更全、也更费 token。",
     "default": RECALL_TOP_K},
    {"key": "tool_include_window_title", "label": "窗口标题喂模型", "type": "bool",
     "restart": False, "private": True,
     "desc": "是否把你当前的窗口标题告诉模型(让它更懂你在干嘛)。关掉=更隐私。",
     "default": bool(TOOL_INCLUDE_WINDOW_TITLE)},
    {"key": "react_max_rounds", "label": "工具调用轮数上限", "type": "int", "min": 1, "max": 8,
     "unit": "轮", "restart": False,
     "desc": "「先查后说」时最多连续调几次工具,防止反复调用。",
     "default": REACT_MAX_ROUNDS},
    {"key": "interview_max_rounds", "label": "访谈轮数上限", "type": "int", "min": 2, "max": 20,
     "unit": "轮", "restart": False,
     "desc": "首次访谈最多问几轮(到上限那轮自然告别)。",
     "default": INTERVIEW_MAX_ROUNDS},
    {"key": "embed_enabled", "label": "向量记忆", "type": "bool", "restart": True,
     "desc": "本地向量记忆(让它能语义回忆过往)。关掉则降级到纯最近对话。改后需重启生效。",
     "default": bool(EMBED_ENABLED)},
    {"key": "react_task_max_tokens", "label": "任务模式回复长度", "type": "int", "min": 200, "max": 4000,
     "unit": "token", "restart": False,
     "desc": "复杂任务(分析代码 / 查资料 / 拆解问题)时回复的最大长度。大则更详尽、也更费 token。",
     "default": REACT_TASK_MAX_TOKENS},
    {"key": "task_mode_enabled", "label": "任务模式", "type": "bool", "restart": False,
     "desc": "自动判断你的消息是闲聊还是复杂任务,任务时切到详尽 + 调工具的助手模式。关掉则全部按陪伴闲聊回复。",
     "default": True},
    {"key": "file_tools_enabled", "label": "文件工具", "type": "bool", "restart": False,
     "desc": "任务模式下允许它读取你本地的文件(只读,限桌面/文档/下载或你在「工具与权限」里添加的目录)。关掉则不能读文件。",
     "default": True},
    {"key": "web_search_enabled", "label": "联网搜索", "type": "bool", "restart": False,
     "desc": "任务模式下允许它联网查资料(Tavily,需在「工具与权限」填 key)。关掉则不联网。",
     "default": True},
    {"key": "write_tools_enabled", "label": "写入文件", "type": "bool", "restart": False,
     "desc": "任务模式下允许它写文件 / 建目录 / 删文件(删→废纸篓,可恢复)。默认关;即使开了,每次写入前都会先在主窗口弹窗等你确认。",
     "default": False},
    {"key": "run_command_enabled", "label": "执行命令", "type": "bool", "restart": False,
     "desc": "任务模式下允许它跑命令(仅白名单内,见「工具与权限」)。默认关;即使开了,每次执行前都会先弹窗等你确认,破坏性命令(rm/sudo 等)一律拒绝。",
     "default": False},
    {"key": "voice_enabled", "label": "语音朗读", "type": "bool", "restart": False,
     "desc": "把陪伴的回复念出来(你的消息回复 / 心跳搭话 / 到点提醒都会朗读)。关掉则只用文字。主窗口状态栏的 🔊 也能一键静音。",
     "default": True},
    {"key": "voice_volume", "label": "语音音量", "type": "int", "min": 0, "max": 100,
     "unit": "%", "restart": False,
     "desc": "语音朗读的音量(0-100%)。每个人的声线(语速/音高)由人格决定,这里只调响度。",
     "default": 90},
    {"key": "voice_engine", "label": "语音引擎", "type": "choice", "options": ["edge", "av", "cosyvoice"],
     "restart": False,
     "desc": "edge=微软在线神经语音(自然,免费,回复文本会发微软公共 TTS 端点)。av=只用 macOS 本机语音(离线,偏机械)。cosyvoice=硅基流动 CosyVoice2(可声音克隆,更有人格味,需在「工具与权限」填硅基流动 key,按请求计费;无 key/出错自动回退本机)。想要人声选 edge 或 cosyvoice。",
     "default": "edge"},
]
_KNOB_BY_KEY = {k["key"]: k for k in KNOBS}


def _mask(key: Optional[str]) -> str:
    """api_key 掩码:sk-…abcd(给前端 key_hint,绝不返完整 key)。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "••••"
    return key[:3] + "…" + key[-4:]


class Settings:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _PATH
        self._lock = threading.RLock()
        self._data: dict = copy.deepcopy(_EMPTY)
        self.load()

    @staticmethod
    def _fresh() -> dict:
        return copy.deepcopy(_EMPTY)

    def load(self) -> None:
        """读 yaml;不存在/损坏 → 保持默认(全 None=回退 config),仅 log warning,不抛。"""
        with self._lock:
            if not self.path.exists():
                self._data = self._fresh()
                return
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                base = self._fresh()
                for k, v in data.items():                 # 合并到默认结构
                    if k in base:
                        base[k] = v
                self._data = base
            except Exception:  # noqa: BLE001
                log.warning("settings load failed: %s, using defaults", self.path)
                self._data = self._fresh()

    def save(self) -> None:
        """原子写:dump 到同目录 tmp → os.replace。失败仅 log,不抛。"""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
                tmp.write_text(
                    yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                os.replace(str(tmp), str(self.path))
            except Exception:  # noqa: BLE001
                log.warning("settings save failed: %s", self.path)

    # ---------- LLM 凭据/模型(回退 config/.env)----------
    @property
    def provider(self) -> str:
        with self._lock:
            return self._data.get("provider") or "deepseek"

    @property
    def base_url(self) -> str:
        with self._lock:
            return self._data.get("base_url") or LLM_BASE_URL

    @property
    def api_key(self) -> str:
        with self._lock:
            return self._data.get("api_key") or LLM_API_KEY

    @property
    def model(self) -> str:
        with self._lock:
            return self._data.get("model") or LLM_MODEL

    @property
    def models(self) -> list:
        with self._lock:
            return list(self._data.get("models") or [])

    # ---------- 旋钮(回退 config 默认)----------
    def _knob(self, key: str):
        """旋钮当前生效值:settings 覆盖 → 否则 KNOBS 默认。"""
        with self._lock:
            v = self._data.get(key)
            if v is not None:
                return v
            return _KNOB_BY_KEY[key]["default"]

    @property
    def heartbeat_interval(self) -> int:
        return int(self._knob("heartbeat_interval"))

    @property
    def silence_timeout(self) -> int:
        return int(self._knob("silence_timeout"))

    @property
    def interview_silence(self) -> int:
        return int(self._knob("interview_silence")) if self._data.get("interview_silence") is not None \
            else INTERVIEW_SILENCE_SEC

    @property
    def interview_max_rounds(self) -> int:
        return int(self._knob("interview_max_rounds"))

    @property
    def recall_top_k(self) -> int:
        return int(self._knob("recall_top_k"))

    @property
    def tool_include_window_title(self) -> bool:
        return bool(self._knob("tool_include_window_title"))

    @property
    def react_max_rounds(self) -> int:
        return int(self._knob("react_max_rounds"))

    @property
    def embed_enabled(self) -> bool:
        return bool(self._knob("embed_enabled"))

    @property
    def react_task_max_tokens(self) -> int:
        return int(self._knob("react_task_max_tokens"))

    @property
    def task_mode_enabled(self) -> bool:
        return bool(self._knob("task_mode_enabled"))

    @property
    def file_tools_enabled(self) -> bool:
        return bool(self._knob("file_tools_enabled"))

    @property
    def web_search_enabled(self) -> bool:
        return bool(self._knob("web_search_enabled"))

    @property
    def write_tools_enabled(self) -> bool:
        return bool(self._knob("write_tools_enabled"))

    @property
    def run_command_enabled(self) -> bool:
        return bool(self._knob("run_command_enabled"))

    @property
    def voice_enabled(self) -> bool:
        return bool(self._knob("voice_enabled"))

    @property
    def voice_volume(self) -> int:
        return int(self._knob("voice_volume"))

    @property
    def voice_engine(self) -> str:
        return str(self._knob("voice_engine"))

    @property
    def tavily_key(self) -> str:
        with self._lock:
            return self._data.get("tavily_key") or HEARTBEAT_TAVILY_KEY

    @property
    def siliconflow_key(self) -> str:
        with self._lock:
            return self._data.get("siliconflow_key") or HEARTBEAT_SILICONFLOW_KEY

    @property
    def allowed_dirs(self) -> list:
        with self._lock:
            return [str(p) for p in (self._data.get("allowed_dirs") or []) if p]

    @property
    def command_allowlist(self) -> list:
        """run_command 白名单。settings 覆盖 → 否则 config.COMMAND_ALLOWLIST 默认。空列表=禁用 run_command。"""
        with self._lock:
            v = self._data.get("command_allowlist")
            if v is None:
                return [str(c) for c in COMMAND_ALLOWLIST if c]
            return [str(c).strip() for c in v if str(c).strip()]

    # ---------- 写入 ----------
    def update(self, llm: Optional[dict] = None, config: Optional[dict] = None,
               secrets: Optional[dict] = None, paths: Optional[dict] = None) -> bool:
        """增量更新。llm:{provider?,base_url?,api_key?,model?};config:{knob_key: value};
        secrets:{tavily_key?};paths:{allowed_dirs?: [...]}(Phase 6b 非旋钮,因 _coerce 不支持 str/list)。

        api_key/tavily_key 仅在传入非空字符串时覆盖(前端掩码字段未改时不发 → 不误清)。
        旋钮按 KNOBS 类型校验+clamp(int 取整夹在 min/max;bool 规范化)。返回是否有变更。
        """
        llm = llm or {}
        config = config or {}
        secrets = secrets or {}
        paths = paths or {}
        with self._lock:
            changed = False
            if llm.get("provider"):
                if self._data["provider"] != llm["provider"]:
                    self._data["provider"] = str(llm["provider"]); changed = True
            if llm.get("base_url") is not None:
                url = str(llm["base_url"]).strip()
                if self._data["base_url"] != url:
                    self._data["base_url"] = url or None; changed = True
            if llm.get("api_key"):                       # 非空才覆盖(空=未改,不误清)
                k = str(llm["api_key"]).strip()
                if self._data["api_key"] != k:
                    self._data["api_key"] = k; changed = True
            if llm.get("model") is not None:
                m = str(llm["model"]).strip()
                if self._data["model"] != m:
                    self._data["model"] = m or None; changed = True
            for key, raw in config.items():
                knob = _KNOB_BY_KEY.get(key)
                if not knob:
                    continue
                norm = _coerce(knob, raw)
                if norm is None:
                    continue
                if self._data.get(key) != norm:
                    self._data[key] = norm; changed = True
            if secrets.get("tavily_key"):            # 非空才覆盖(掩码字段未改不发 → 不误清)
                k = str(secrets["tavily_key"]).strip()
                if self._data.get("tavily_key") != k:
                    self._data["tavily_key"] = k; changed = True
            if secrets.get("siliconflow_key"):       # cosyvoice 引擎用;非空才覆盖(同 tavily 守卫)
                k = str(secrets["siliconflow_key"]).strip()
                if self._data.get("siliconflow_key") != k:
                    self._data["siliconflow_key"] = k; changed = True
            if "allowed_dirs" in paths:              # 显式提供即覆盖(空列表=清空额外目录)
                dirs = [str(p).strip() for p in (paths.get("allowed_dirs") or []) if str(p).strip()]
                if self._data.get("allowed_dirs") != dirs:
                    self._data["allowed_dirs"] = dirs; changed = True
            if "command_allowlist" in paths:         # Phase 6d:run_command 白名单(显式覆盖;空列表=禁用 run_command)
                cmds = [str(c).strip() for c in (paths.get("command_allowlist") or []) if str(c).strip()]
                if self._data.get("command_allowlist") != cmds:
                    self._data["command_allowlist"] = cmds; changed = True
            if changed:
                self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self.save()
        return changed

    def set_models(self, models: list) -> None:
        """缓存从 /models 拉取的模型 id 列表(供主窗 chip 下拉)。"""
        with self._lock:
            clean = [str(m) for m in (models or []) if m]
            if clean != self._data.get("models"):
                self._data["models"] = clean
                self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self.save()

    def public_view(self) -> dict:
        """掩码视图(GET /api/settings 用)。永不返回完整 api_key。"""
        with self._lock:
            key = self.api_key
            return {
                "llm": {
                    "provider": self.provider,
                    "base_url": self.base_url,
                    "model": self.model,
                    "has_key": bool(key),
                    "key_hint": _mask(key),
                },
                "models": self.models,
                "config": {k["key"]: self._knob(k["key"]) for k in KNOBS},
                "knobs": KNOBS,
                "secrets": {                                  # Phase 6b:Tavily key;cosyvoice 硅基流动 key(掩码,永不回完整)
                    "has_tavily_key": bool(self.tavily_key),
                    "tavily_key_hint": _mask(self.tavily_key),
                    "has_siliconflow_key": bool(self.siliconflow_key),
                    "siliconflow_key_hint": _mask(self.siliconflow_key),
                },
                "paths": {                                    # Phase 6b/6d:list 型策略(路径/命令白名单,非密,不掩码)
                    "allowed_dirs": self.allowed_dirs,
                    "command_allowlist": self.command_allowlist,
                },
            }


def _coerce(knob: dict, raw) -> Optional[object]:
    """按旋钮类型规范+校验输入。非法 → None(跳过)。"""
    t = knob["type"]
    try:
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return bool(raw)
            s = str(raw).strip().lower()
            if s in ("1", "true", "yes", "on", "开"):
                return True
            if s in ("0", "false", "no", "off", "关"):
                return False
            return None
        if t == "int":
            v = int(float(raw))                          # 容忍 "8.0"
            lo, hi = knob.get("min"), knob.get("max")
            if lo is not None and v < lo:
                v = lo
            if hi is not None and v > hi:
                v = hi
            return v
        if t == "choice":
            s = str(raw).strip()
            return s if s in knob.get("options", []) else None
    except (TypeError, ValueError):
        return None
    return None


settings = Settings()  # 模块级单例:只读 yaml(无网络),各处 from backend.memory.settings import settings
