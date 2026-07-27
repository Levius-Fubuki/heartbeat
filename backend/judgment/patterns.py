"""判定层 - L4 模式触发。纯代码匹配结构化 trigger,产 pattern_match 候选(零 LLM 成本)。

与 rules.find_candidates 合流:`candidates = rules.find_candidates(...) + patterns.find_triggered(...)`,
统一过 CooldownTracker。pattern_match 优先级 PATTERN_PRIORITY(< rules 关心类 deep_night5/long_focus4),
是"锦上添花"型触发——同 tick 若 rules 也产候选,rules 胜出,pattern 待 cooldown 过期再有机会。

trigger 4 类(由 gateway.extract_patterns 归纳时产出,LLM 尽量归到这几类,归不进→trigger=None 只注入):
  time_window     作息时段:hour_range(跨午夜,[23,2]=23/0/1/2 点)+ weekdays(0=周一)+ app_category
  app_streak      某 App 连续用:min_minutes
  app_combo       同时段多 App 组合:apps + within_minutes(最近窗口内出现全部)
  focus_duration  连续工作不限 App:min_minutes + exclude_categories

误触兜底:evidence < PATTERN_MIN_EVIDENCE 的模式不进 for_trigger(在 PatternStore 层挡)。
"""
from __future__ import annotations

from typing import List

from backend.config import HEARTBEAT_INTERVAL_SEC, PATTERN_PRIORITY


def _in_hour_window(hour: int, hour_range: list) -> bool:
    """小时是否在 [start, end] 范围(支持跨午夜,如 [23,2] = 23/0/1/2 点)。无范围 → 不限。"""
    if not hour_range or len(hour_range) < 2:
        return True
    start, end = int(hour_range[0]), int(hour_range[1])
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end       # 跨午夜


def _app_category(app, title=None) -> str:
    """复用 rules._classify 判类别(music/dev/social/leisure/neutral)。本地导入避循环依赖。"""
    from backend.judgment.rules import _classify
    return _classify(app, title)


def _continuous_focus_minutes(l0, exclude_categories: set) -> int:
    """从最新快照往回,连续(idle<30 且 App 类别不在 exclude)的累计分钟数(同 long_focus 的 idle<30 口径)。"""
    snaps = getattr(l0, "snapshots", None)
    if not snaps:
        return 0
    secs = 0
    for s in reversed(snaps):
        i = s.get("idle_sec")
        app = s.get("foreground_app")
        if i is None or i >= 30:                       # idle≥30s 视为中断
            break
        if app and exclude_categories and \
                _app_category(app, s.get("window_title")) in exclude_categories:
            break
        secs += HEARTBEAT_INTERVAL_SEC
    return secs // 60


def _matches(trigger: dict, snap: dict, l0) -> bool:
    """单模式 trigger 是否命中当前 snap + L0 聚合。纯代码,零 LLM。"""
    if not trigger or not isinstance(trigger, dict):
        return False
    t = trigger.get("type")
    app = snap.get("foreground_app")
    hour = snap.get("hour")

    if t == "time_window":
        if hour is None:
            return False
        if not _in_hour_window(hour, trigger.get("hour_range") or []):
            return False
        wds = trigger.get("weekdays")
        if wds and snap.get("weekday") not in wds:     # weekday 约束(0=周一);None/空=不限
            return False
        cat = trigger.get("app_category")
        if cat and app and _app_category(app, snap.get("window_title")) != cat:
            return False                               # 要求当前 App 属某类但不符
        return True

    if t == "app_streak":
        target = trigger.get("app")
        if not target or app != target:
            return False
        min_min = int(trigger.get("min_minutes", 0) or 0)
        if min_min <= 0:
            return True
        ticks = l0.current_app_streak_ticks(app) if hasattr(l0, "current_app_streak_ticks") else 0
        return ticks * HEARTBEAT_INTERVAL_SEC // 60 >= min_min

    if t == "app_combo":
        apps = trigger.get("apps") or []
        if not apps:
            return False
        within = int(trigger.get("within_minutes", 10) or 10)
        ticks = max(1, within * 60 // HEARTBEAT_INTERVAL_SEC)
        recent_apps = set()
        for s in list(getattr(l0, "snapshots", []))[-ticks:]:
            a = s.get("foreground_app")
            if a:
                recent_apps.add(a)
        return all(a in recent_apps for a in apps)

    if t == "focus_duration":
        min_min = int(trigger.get("min_minutes", 0) or 0)
        if min_min <= 0:
            return False
        exclude = set(trigger.get("exclude_categories") or [])
        return _continuous_focus_minutes(l0, exclude) >= min_min

    return False


def find_triggered(snap: dict, l0, store) -> List[dict]:
    """遍历 store.for_trigger(),用 _matches 判断;命中产 pattern_match 候选(同 tick 每模式最多一条)。

    候选 detail = 模式 text(自然语言,进 judge/react 用);_pattern_id 本地用(heartbeat mark_fired)。
    """
    if not snap or store is None:
        return []
    cands = []
    for p in store.for_trigger():
        if _matches(p.get("trigger") or {}, snap, l0):
            cands.append({
                "key": f"pattern:{p.get('id')}",
                "category": "pattern_match",
                "priority": PATTERN_PRIORITY,
                "detail": p.get("text") or "匹配到一条行为模式",
                "_pattern_id": p.get("id"),           # 本地用(不进云端)
            })
    return cands
