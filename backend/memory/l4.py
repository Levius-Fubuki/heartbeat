"""L4 模式沉淀:从 L3 情景记忆归纳出的用户作息 + 行为规律。

存储 memory/patterns.yaml,原子写(tmp + os.replace)。每条模式:
  {id, text, trigger|None, evidence_count, first_seen, last_seen, last_fired}

与 L2 的边界:L2 = 用户亲口说的性格爱好(声明);L4 = agent 观察归纳的作息 + 规律。
双重用途:① to_prompt_block() 注入 system prompt 的 [行为模式] 段(懂用户);
② for_trigger() 返回可触发的模式,find_triggered() 纯代码匹配结构化 trigger → 主动搭话候选。

归纳由 gateway.extract_patterns 做(看最近 N 段 L3 + 现有模式,输出 updates + dropped_ids);
本类只负责存储 + merge(累积证据 / 淘汰)+ 格式化。merge 语义适配模式:source_id 复用累积证据、
text 包含兜底、时间/计数淘汰、上限裁剪。
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from backend.config import (
    MEMORY_DIR, PATTERN_MAX, PATTERN_MAX_CHARS, PATTERN_MIN_EVIDENCE,
)

log = logging.getLogger("heartbeat.memory.l4")

_PATTERN_PATH = MEMORY_DIR / "patterns.yaml"

# 淘汰(墙钟 last_seen):低证据短超期删,高证据长超期才删
_STALE_LOW_EVIDENCE = 3     # evidence < 此值视为低证据
_STALE_LOW_DAYS = 14        # 低证据模式超过这么多天没再观察到 → 删
_STALE_HIGH_DAYS = 30       # 任何模式超过这么多天没再观察到 → 删


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PatternStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _PATTERN_PATH
        self._lock = threading.RLock()       # 可重入:merge 持锁时调 save
        self._patterns: list = []
        self.load()                          # 启动即加载,失败用空

    def load(self) -> None:
        """读 yaml;文件不存在或损坏 → 空,仅 log,不抛。"""
        with self._lock:
            if not self.path.exists():
                self._patterns = []
                return
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                pats = data.get("patterns") if isinstance(data, dict) else None
                self._patterns = [p for p in (pats or []) if isinstance(p, dict) and p.get("id")]
            except Exception:  # noqa: BLE001
                log.warning("patterns load failed: %s, using empty", self.path)
                self._patterns = []

    def save(self) -> None:
        """原子写:dump 到 tmp.{pid} → os.replace。失败仅 log,不抛。"""
        with self._lock:        # RLock 可重入:merge 持锁调 save 安全
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
                tmp.write_text(
                    yaml.safe_dump({"patterns": self._patterns, "updated_at": _now_iso()},
                                   allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                os.replace(str(tmp), str(self.path))
            except Exception:  # noqa: BLE001
                log.warning("patterns save failed: %s", self.path)

    def _find(self, source_id: Optional[str], text: str):
        """按 source_id 精确匹配,或 text 包含兜底,找现有模式。找不到 → None。"""
        if source_id:
            for p in self._patterns:
                if p.get("id") == source_id:
                    return p
        if text:
            for p in self._patterns:
                pt = p.get("text") or ""
                # 互相包含兜底:同义不同措辞(如"深夜写代码听音乐" vs "深夜编码常开音乐")
                if pt and (text in pt or pt in text):
                    return p
        return None

    def merge(self, result: dict) -> bool:
        """合并 extract_patterns 的输出 {updates:[{source_id|None, text, trigger|None}], dropped_ids:[...]}。

        - source_id 匹配 / text 包含 → 累积 evidence_count + 更新 last_seen + 覆盖 text/trigger(新观察更准)
        - 未匹配 → 新增(补 id / first_seen)
        - dropped_ids → 删除
        - 淘汰:低证据超期 / 任何超期 / 超上限按 evidence 裁剪
        有变更才 save。返回 changed。
        """
        updates = (result or {}).get("updates") or []
        dropped = set((result or {}).get("dropped_ids") or [])
        if not updates and not dropped:
            return False
        now = _now_iso()
        with self._lock:
            changed = False
            # 先删 dropped
            if dropped:
                before = len(self._patterns)
                self._patterns = [p for p in self._patterns if p.get("id") not in dropped]
                if len(self._patterns) != before:
                    changed = True
            # 合并 updates
            for u in updates:
                text = (u.get("text") or "").strip()
                if not text:
                    continue
                trig = u.get("trigger")
                existing = self._find(u.get("source_id"), text)
                if existing is not None:
                    existing["evidence_count"] = int(existing.get("evidence_count", 0)) + 1
                    existing["last_seen"] = now
                    existing["text"] = text                  # 新观察更准,覆盖
                    existing["trigger"] = trig               # trigger 可能 None→结构化 或反之,以最新归纳为准
                    changed = True
                else:
                    self._patterns.append({
                        "id": str(uuid.uuid4()),
                        "text": text,
                        "trigger": trig,
                        "evidence_count": 1,
                        "first_seen": now,
                        "last_seen": now,
                        "last_fired": None,
                    })
                    changed = True
            # 淘汰超期
            if self._prune_stale():
                changed = True
            # 上限裁剪(按 evidence 降序保留)
            if len(self._patterns) > PATTERN_MAX:
                self._patterns.sort(key=lambda p: int(p.get("evidence_count", 0)), reverse=True)
                self._patterns = self._patterns[:PATTERN_MAX]
                changed = True
            if changed:
                self.save()
        return changed

    def _prune_stale(self) -> bool:
        """淘汰超期未见模式(墙钟 last_seen)。需持 _lock。返回是否有删。"""
        now = datetime.now()
        keep = []
        removed = False
        for p in self._patterns:
            ev = int(p.get("evidence_count", 0))
            ls = p.get("last_seen")
            try:
                last = datetime.fromisoformat(ls) if ls else None
            except Exception:  # noqa: BLE001
                last = None
            if last is None:
                keep.append(p)              # 无时间戳(老数据)保留
                continue
            days = (now - last).days
            if ev < _STALE_LOW_EVIDENCE and days > _STALE_LOW_DAYS:
                removed = True
                continue
            if days > _STALE_HIGH_DAYS:
                removed = True
                continue
            keep.append(p)
        if removed:
            self._patterns = keep
        return removed

    def to_prompt_block(self) -> str:
        """注入 system prompt [行为模式] 段的纯内容(不带标题,build_system_prompt 加)。空 → ""。
        按 evidence 降序,截 PATTERN_MAX_CHARS。"""
        with self._lock:
            if not self._patterns:
                return ""
            ordered = sorted(self._patterns, key=lambda p: int(p.get("evidence_count", 0)), reverse=True)
            lines = []
            total = 0
            for p in ordered:
                t = p.get("text") or ""
                if not t:
                    continue
                line = f"- {t}"
                if total + len(line) > PATTERN_MAX_CHARS:
                    break
                lines.append(line)
                total += len(line) + 1
            return "\n".join(lines)

    def for_trigger(self) -> list:
        """返回可触发的模式(trigger 非 None 且 evidence ≥ PATTERN_MIN_EVIDENCE)。find_triggered 用。"""
        with self._lock:
            return [p for p in self._patterns
                    if p.get("trigger") is not None
                    and int(p.get("evidence_count", 0)) >= PATTERN_MIN_EVIDENCE]

    def mark_fired(self, pattern_id: str) -> None:
        """记录模式最近触发时间(诊断用)。攒着不立即 save(触发频率>>归纳,减 IO,下次 merge 一起写)。"""
        with self._lock:
            for p in self._patterns:
                if p.get("id") == pattern_id:
                    p["last_fired"] = _now_iso()
                    return

    @property
    def data(self) -> dict:
        """只读快照(直传 extract_patterns 看现有模式;deepcopy 防 to_thread 并发改)。"""
        with self._lock:
            return {"patterns": copy.deepcopy(self._patterns)}

    def __len__(self) -> int:
        with self._lock:
            return len(self._patterns)
