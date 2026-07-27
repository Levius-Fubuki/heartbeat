"""L2 用户档案:稳定事实(身份/爱好/作息/偏好),始终注入 system prompt 的 B 半。

存储 memory/profile.yaml,原子写(tmp + os.replace)。同时承担「上次选的人格」与「是否完成选人格」
两个 onboarding 持久化字段(放同一份档案,避免多文件)。
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

from backend.config import DEFAULT_COOLDOWN_MODE, MEMORY_DIR

log = logging.getLogger("heartbeat.memory.l2")

_PROFILE_PATH = MEMORY_DIR / "profile.yaml"

# 默认空档案(冷启动)。嵌套结构用 deepcopy 避免多实例共享引用。
_EMPTY = {
    "persona": None,                                  # 上次选的人格(启动据此覆盖 DEFAULT_PERSONA)
    "onboarded": False,                               # 是否已完成选人格
    "cooldown_mode": None,                            # 打扰频率档位(conservative/balanced/active);None→DEFAULT
    "interviewed": False,                             # 是否完成过 Stage 2 首次访谈(手动触发,可重复)
    "identity": {"role": None, "fields": []},         # 工作/学习/方向
    "interests": [],                                  # 爱好
    "schedule": {"focus_hours": None, "dnd_windows": []},  # 作息与免打扰
    "chat_pref": {"verbosity": None, "emoji_ok": None},   # concise|chatty / bool
    "fun_fact": None,                                 # 一个小彩蛋
    "updated_at": None,
}


class Profile:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _PROFILE_PATH
        self._lock = threading.RLock()       # 可重入:merge/set_onboarded 持锁时调 save
        self._data: dict = copy.deepcopy(_EMPTY)
        self.load()                          # 启动即加载,失败用空档案

    @staticmethod
    def _fresh_empty() -> dict:
        return copy.deepcopy(_EMPTY)

    def load(self) -> None:
        """读 yaml;文件不存在或损坏 → 保持空档案,仅 log warning,不抛。"""
        with self._lock:
            if not self.path.exists():
                self._data = self._fresh_empty()
                return
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                base = self._fresh_empty()
                for k, v in data.items():                 # 合并到默认结构,补缺失字段
                    if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                        base[k].update(v)
                    else:
                        base[k] = v
                self._data = base
            except Exception:  # noqa: BLE001
                log.warning("profile load failed: %s, using empty", self.path)
                self._data = self._fresh_empty()

    def save(self) -> None:
        """原子写:dump 到同目录 tmp 文件 → os.replace。失败仅 log,不抛。"""
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
                log.warning("profile save failed: %s", self.path)

    def merge(self, new_facts: dict) -> bool:
        """增量合并 LLM 抽取的事实:嵌套 dict 走一层、数组并集去重、标量非空覆盖。

        返回是否有变更;有变更则写 updated_at 并 save。空入参或无变更不写盘。
        """
        if not new_facts:
            return False
        with self._lock:
            changed = False
            for k, v in new_facts.items():
                if v is None or v == "":
                    continue
                cur = self._data.get(k)
                if isinstance(v, dict) and isinstance(cur, dict):
                    for sk, sv in v.items():
                        if sv is None or (isinstance(sv, str) and sv == ""):
                            continue
                        if isinstance(sv, list) and isinstance(cur.get(sk), list):
                            merged = list(cur[sk])
                            for item in sv:
                                if item not in merged:
                                    merged.append(item)
                            if merged != cur[sk]:
                                cur[sk] = merged
                                changed = True
                        elif cur.get(sk) != sv:
                            cur[sk] = sv
                            changed = True
                elif isinstance(v, list) and isinstance(cur, list):
                    merged = list(cur)
                    for item in v:
                        if item not in merged:
                            merged.append(item)
                    if merged != cur:
                        self._data[k] = merged
                        changed = True
                elif cur != v:
                    self._data[k] = v
                    changed = True
            if changed:
                self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self.save()
        return changed

    def to_prompt_block(self) -> str:
        """格式化为可注入文本(空档案 → "")。"""
        with self._lock:
            d = self._data
            lines = []
            ident = d.get("identity") or {}
            role = ident.get("role")
            fields = ident.get("fields") or []
            if role or fields:
                s = str(role) if role else ""
                if fields:
                    s += (";方向 " if s else "方向 ") + "、".join(str(x) for x in fields)
                lines.append(f"- 身份:{s}")
            if d.get("interests"):
                lines.append("- 爱好:" + "、".join(str(x) for x in d["interests"]))
            sched = d.get("schedule") or {}
            parts = []
            if sched.get("focus_hours"):
                parts.append(f"{sched['focus_hours']} 敲代码")
            if sched.get("dnd_windows"):
                parts.append("、".join(str(x) for x in sched["dnd_windows"]) + " 别打扰")
            if parts:
                lines.append("- 作息:" + ";".join(parts))
            cp = d.get("chat_pref") or {}
            pref_bits = []
            if cp.get("verbosity"):
                pref_bits.append("简洁回复" if cp["verbosity"] == "concise" else "可以多聊")
            if cp.get("emoji_ok") is False:
                pref_bits.append("不用 emoji")
            if pref_bits:
                lines.append("- 偏好:" + "、".join(pref_bits))
            if d.get("fun_fact"):
                lines.append("- 彩蛋:" + str(d["fun_fact"]))
            if not lines:
                return ""
            return "[用户档案]\n" + "\n".join(lines)

    def set_onboarded(self, persona_id: str) -> None:
        """选人格后落盘:persona + onboarded=True。"""
        with self._lock:
            self._data["persona"] = persona_id
            self._data["onboarded"] = True
            self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.save()

    @property
    def data(self) -> dict:
        """只读视图(直传 gateway 拼 prompt,不复制)。"""
        with self._lock:
            return self._data

    @property
    def persona_id(self) -> Optional[str]:
        with self._lock:
            pid = self._data.get("persona")
            return pid if pid else None

    @property
    def onboarded(self) -> bool:
        with self._lock:
            return bool(self._data.get("onboarded"))

    @property
    def interviewed(self) -> bool:
        with self._lock:
            return bool(self._data.get("interviewed"))

    @property
    def cooldown_mode(self) -> str:
        with self._lock:
            return self._data.get("cooldown_mode") or DEFAULT_COOLDOWN_MODE

    def set_cooldown_mode(self, mode: str) -> None:
        """用户在主界面切换打扰频率档位 → 落盘(照抄 set_onboarded 模式)。"""
        with self._lock:
            self._data["cooldown_mode"] = mode
            self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.save()

    def set_interviewed(self) -> None:
        """Stage 2 首次访谈收尾 → 落盘 interviewed=True(照抄 set_cooldown_mode 模式;可重复访谈)。"""
        with self._lock:
            self._data["interviewed"] = True
            self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.save()
