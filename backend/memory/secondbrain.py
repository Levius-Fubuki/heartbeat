"""第二大脑(Phase 6c):笔记 / 待办 / 提醒。三份持久化小工具,落 memory/ 目录。

照搬 l4.PatternStore 的存储纪律(RLock + 原子写 tmp.{pid}+os.replace + load/save + to_prompt_block 返纯
内容无标题 + data deepcopy)。原子写纪律收拢进 _BaseYamlStore 一处,三个子类只定 entry 形状 + 访问器。

persona 共享(同 L2/L4/Pattern):描述同一个用户,不按人格隔离;到点提醒由「当前值班人格」送达。

entry 形状:
  NotesStore      {id, text, tags:[], ts}
  TodosStore      {id, text, done:bool, ts}
  RemindersStore  {id, text, fire_at:float(epoch), created_at, fired:bool}
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import yaml

from backend.config import (
    MEMORY_DIR, NOTES_INJECT_MAX, NOTES_MAX, REMINDERS_INJECT_MAX,
    REMINDERS_INJECT_SOON_SEC, REMINDERS_MAX, TODOS_INJECT_MAX, TODOS_MAX,
)

log = logging.getLogger("heartbeat.memory.secondbrain")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fmt_fire_at(fire_at) -> str:
    """epoch → 'MM-DD HH:MM' 本地时间展示串;非法 → 兜底。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(fire_at)))
    except (TypeError, ValueError):  # noqa: BLE001
        return "未知时间"


class _BaseYamlStore:
    """三 store 共享的 yaml 持久化纪律(原子写 + RLock + load/save + data 快照)。

    子类设类属性 _filename / _yaml_key,并按需覆盖 _trim()(超上限裁剪策略)。
    """

    _filename: str = ""
    _yaml_key: str = ""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (MEMORY_DIR / self._filename)
        self._lock = threading.RLock()       # 可重入:子类持锁调 save / 内部访问器
        self._items: list = []
        self.load()                          # 启动即加载,失败用空

    def load(self) -> None:
        """读 yaml;文件不存在或损坏 → 空,仅 log,不抛。"""
        with self._lock:
            if not self.path.exists():
                self._items = []
                return
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                items = data.get(self._yaml_key) if isinstance(data, dict) else None
                self._items = [it for it in (items or []) if isinstance(it, dict) and it.get("id")]
            except Exception:  # noqa: BLE001
                log.warning("%s load failed: %s, using empty", self._yaml_key, self.path)
                self._items = []

    def save(self) -> None:
        """原子写:dump 到 tmp.{pid} → os.replace。失败仅 log,不抛。"""
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
                tmp.write_text(
                    yaml.safe_dump({self._yaml_key: self._items, "updated_at": _now_iso()},
                                   allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                os.replace(str(tmp), str(self.path))
            except Exception:  # noqa: BLE001
                log.warning("%s save failed: %s", self._yaml_key, self.path)

    def _trim(self) -> None:
        """子类覆盖:超上限时的裁剪策略。默认不裁。需持 _lock。"""
        return

    @property
    def data(self) -> list:
        """只读深拷贝快照(防 to_thread 并发改)。"""
        with self._lock:
            return copy.deepcopy(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# ============================ 笔记 ============================
class NotesStore(_BaseYamlStore):
    _filename = "notes.yaml"
    _yaml_key = "notes"

    def add(self, text: str, tags: Optional[list] = None) -> str:
        """新增笔记;返 id(空文本返 "")。tags 去空白过滤。超 NOTES_MAX 删最旧。"""
        text = (text or "").strip()
        if not text:
            return ""
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        nid = str(uuid.uuid4())
        with self._lock:
            self._items.append({"id": nid, "text": text, "tags": tags, "ts": _now_iso()})
            self._trim()
            self.save()
        return nid

    def list(self, query: Optional[str] = None) -> list:
        """query 非空 → 按子串(大小写不敏感)筛 text 或 tags;None → 全部。"""
        with self._lock:
            if not query:
                return copy.deepcopy(self._items)
            q = query.lower()
            return copy.deepcopy([
                n for n in self._items
                if q in (n.get("text") or "").lower()
                or any(q in (str(t) or "").lower() for t in (n.get("tags") or []))
            ])

    def delete(self, nid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [n for n in self._items if n.get("id") != nid]
            if len(self._items) != before:
                self.save()
                return True
            return False

    def recent(self, n: int = NOTES_INJECT_MAX) -> list:
        with self._lock:
            ordered = sorted(self._items, key=lambda x: x.get("ts") or "", reverse=True)
            return copy.deepcopy(ordered[:n])

    def to_prompt_block(self) -> str:
        """注入 [待办与笔记] 的近期笔记段(纯内容,无标题)。空 → ""。"""
        with self._lock:
            if not self._items:
                return ""
            ordered = sorted(self._items, key=lambda x: x.get("ts") or "", reverse=True)
            lines = []
            for note in ordered[:NOTES_INJECT_MAX]:
                t = note.get("text") or ""
                if not t:
                    continue
                tags = note.get("tags") or []
                lines.append(f"- {t}" + (f" [{' / '.join(tags)}]" if tags else ""))
            return "\n".join(lines)

    def _trim(self) -> None:
        if len(self._items) > NOTES_MAX:
            self._items.sort(key=lambda x: x.get("ts") or "")
            self._items = self._items[-NOTES_MAX:]   # 留最新 NOTES_MAX


# ============================ 待办 ============================
class TodosStore(_BaseYamlStore):
    _filename = "todos.yaml"
    _yaml_key = "todos"

    def add(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        tid = str(uuid.uuid4())
        with self._lock:
            self._items.append({"id": tid, "text": text, "done": False, "ts": _now_iso()})
            self._trim()
            self.save()
        return tid

    def list(self) -> list:
        with self._lock:
            return copy.deepcopy(self._items)

    def toggle(self, tid: str) -> bool:
        """翻转 done。命中并改 → True(持久化);未命中 → False。"""
        with self._lock:
            for t in self._items:
                if t.get("id") == tid:
                    t["done"] = not bool(t.get("done"))
                    self.save()
                    return True
            return False

    def delete(self, tid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [t for t in self._items if t.get("id") != tid]
            if len(self._items) != before:
                self.save()
                return True
            return False

    def open_todos(self) -> list:
        with self._lock:
            return copy.deepcopy([t for t in self._items if not t.get("done")])

    def to_prompt_block(self) -> str:
        """注入 [待办与笔记] 的未完成待办段(纯内容,无标题)。空 → ""。"""
        with self._lock:
            opens = [t for t in self._items if not t.get("done")]
            if not opens:
                return ""
            opens.sort(key=lambda x: x.get("ts") or "", reverse=True)
            lines = []
            for t in opens[:TODOS_INJECT_MAX]:
                txt = t.get("text") or ""
                if txt:
                    lines.append(f"- ☐ {txt}")
            return "\n".join(lines)

    def _trim(self) -> None:
        if len(self._items) > TODOS_MAX:
            # 优先删已完成 + 最旧:sort key (not done, ts) → done=True 排前(被 [-N:] 丢掉)、未完成留尾部
            self._items.sort(key=lambda x: (not bool(x.get("done")), x.get("ts") or ""))
            self._items = self._items[-TODOS_MAX:]


# ============================ 提醒 ============================
class RemindersStore(_BaseYamlStore):
    _filename = "reminders.yaml"
    _yaml_key = "reminders"

    def add(self, text: str, fire_at: float) -> str:
        """新增提醒;fire_at 为 epoch 秒。返 id(空文本/非法时间 → "")。超 REMINDERS_MAX 先删已 fired 旧条。"""
        text = (text or "").strip()
        if not text:
            return ""
        try:
            fire_at = float(fire_at)
        except (TypeError, ValueError):  # noqa: BLE001
            return ""
        rid = str(uuid.uuid4())
        with self._lock:
            self._items.append({"id": rid, "text": text, "fire_at": fire_at,
                                "created_at": _now_iso(), "fired": False})
            self._trim()
            self.save()
        return rid

    def list(self) -> list:
        with self._lock:
            return copy.deepcopy(self._items)

    def due(self) -> list:
        """已到期未触发(fire_at <= now 且 not fired),按 fire_at 升序。_check_reminders 用。"""
        now = time.time()
        with self._lock:
            hits = [r for r in self._items
                    if not r.get("fired") and float(r.get("fire_at") or 0) <= now]
            hits.sort(key=lambda x: float(x.get("fire_at") or 0))
            return copy.deepcopy(hits)

    def due_soon(self, within_sec: int = REMINDERS_INJECT_SOON_SEC) -> list:
        """即将到期(now < fire_at <= now+within_sec 且 not fired),按 fire_at 升序。注入用。"""
        now = time.time()
        horizon = now + within_sec
        with self._lock:
            hits = [r for r in self._items
                    if not r.get("fired") and now < float(r.get("fire_at") or 0) <= horizon]
            hits.sort(key=lambda x: float(x.get("fire_at") or 0))
            return copy.deepcopy(hits)

    def mark_fired(self, rid: str) -> bool:
        """标记已触发并【立即持久化】——区别于 l4.PatternStore.mark_fired 的 deferred 写:
        提醒触发后必须落盘,防 app 崩溃/重启后同一提醒重复触发。命中 → True。"""
        with self._lock:
            for r in self._items:
                if r.get("id") == rid:
                    r["fired"] = True
                    self.save()
                    return True
            return False

    def cancel(self, rid: str) -> bool:
        """取消(删除)一条提醒。命中 → True(持久化)。"""
        with self._lock:
            before = len(self._items)
            self._items = [r for r in self._items if r.get("id") != rid]
            if len(self._items) != before:
                self.save()
                return True
            return False

    def to_prompt_block(self) -> str:
        """注入 [待办与笔记] 的近期到期提醒段(纯内容,无标题)。空 → ""。"""
        with self._lock:
            soon = self.due_soon(REMINDERS_INJECT_SOON_SEC)[:REMINDERS_INJECT_MAX]
            if not soon:
                return ""
            lines = []
            for r in soon:
                txt = r.get("text") or ""
                lines.append(f"- {txt}(将到:{_fmt_fire_at(r.get('fire_at'))})")
            return "\n".join(lines)

    def _trim(self) -> None:
        if len(self._items) > REMINDERS_MAX:
            # 优先删已 fired + 最旧:sort key (not fired, fire_at) → fired=True 排前、同组 fire_at 升序;
            # 留尾部 = 未 fired + 较新;被丢的是已 fired 的最旧条。
            self._items.sort(key=lambda x: (not bool(x.get("fired")), float(x.get("fire_at") or 0)))
            self._items = self._items[-REMINDERS_MAX:]
