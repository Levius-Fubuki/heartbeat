"""判定层 - 规则预筛。基于 L0 快照 diff + 聚合 + 时段,产出候选事件(带优先级)。

无候选 → 上层不调 LLM(省钱,一天 1440 次心跳的关键)。
多候选 → CooldownTracker.take 按 priority 排序,_tick 只处理 fresh[0](取最高优先级)。

候选 category 共 9 类:
  Phase 1 保留:music_app_opened / returned_from_idle
  Phase 3 新增:dev_app_opened / leisure_app_opened / long_focus / distracted
              leisure_too_long / deep_night_working / morning_greet

window_title 仅用于本地浏览器内容细分(_classify),绝不写进 detail 发云端 judge。
"""
from __future__ import annotations

import time
from typing import List, Optional

from backend.config import (
    APP_CATEGORIES, COOLDOWN_PROFILES, DEV_TITLE_HINTS, DISTRACTED_APP_COUNT,
    DISTRACTED_WINDOW_TICKS, FOCUS_TICKS, HEARTBEAT_INTERVAL_SEC, LEISURE_TICKS, LEISURE_TITLE_HINTS,
)

_IDLE_AWAY_SEC = 60.0     # 空闲 ≥ 这么多秒算"离开了"
_IDLE_BACK_SEC = 5.0      # 回到 ≤ 这么多秒算"回来了"


def _classify(app: Optional[str], window_title: Optional[str]) -> str:
    """App → 类别: music/dev/social/leisure/neutral。
    music 优先(最具体);浏览器用 window_title 关键词细分内容;无法判断 → neutral。"""
    if not app:
        return "neutral"
    if app in APP_CATEGORIES["music"]:
        return "music"
    if app in APP_CATEGORIES["dev"]:
        return "dev"
    if app in APP_CATEGORIES["social"]:
        return "social"
    if app in APP_CATEGORIES["leisure"]:
        return "leisure"
    if app in APP_CATEGORIES["browser"]:
        t = window_title or ""
        if any(h in t for h in LEISURE_TITLE_HINTS):
            return "leisure"
        if any(h in t for h in DEV_TITLE_HINTS):
            return "dev"
        return "neutral"
    return "neutral"


def find_candidates(prev: dict, curr: dict, l0) -> List[dict]:
    """快照 diff + L0 聚合 + 时段 → 候选事件列表。空列表 = 无候选(不调 LLM)。
    每条带 priority;多候选时 CooldownTracker.take 按 priority 取最高。"""
    candidates: List[dict] = []
    if not curr:
        return candidates

    pa = (prev or {}).get("foreground_app")
    ca = curr.get("foreground_app")
    ctitle = curr.get("window_title")
    cat = _classify(ca, ctitle)
    ci = curr.get("idle_sec")
    pi = (prev or {}).get("idle_sec")

    # 冷启动首 tick prev=None:当前前台 App 不算"刚切换过来"(否则每次启动必误触发
    # dev/leisure 搭话,如"开始写代码了?")。时段类(morning/deep_night)与聚合类
    # (long_focus/distracted/leisure_too_long)不依赖 prev,冷启动照常触发。
    app_switched = prev is not None and pa != ca and ca is not None

    # 1) 前台 App 切换 → 按类别产出
    if app_switched:
        if cat == "music":
            candidates.append({"key": f"app:music:{ca}", "category": "music_app_opened",
                               "priority": 1, "detail": f"用户打开了音乐应用 {ca}"})
        elif cat == "dev":
            candidates.append({"key": f"app:dev:{ca}", "category": "dev_app_opened",
                               "priority": 3, "detail": f"用户打开了开发工具 {ca}"})
        elif cat in ("leisure", "social"):
            tag = "摸鱼" if cat == "leisure" else "社交"
            candidates.append({"key": f"app:{cat}:{ca}", "category": "leisure_app_opened",
                               "priority": 2, "detail": f"用户打开了 {ca}({tag})"})

    # 2) 空闲恢复(离开后回来)—— pi/ci 非 None 才判断(权限缺失时 None 跳过)
    if pi is not None and ci is not None and pi >= _IDLE_AWAY_SEC and ci <= _IDLE_BACK_SEC:
        candidates.append({"key": "idle:returned", "category": "returned_from_idle",
                           "priority": 3, "detail": f"用户离开了 {int(pi)} 秒后回来了"})

    # 3) 长时间专注(streak ≥ 阈值 + idle 持续低 + idle 非 None);娱乐/社交 App 由 leisure_too_long 专门处理,不重复
    streak_ticks = l0.current_app_streak_ticks(ca) if ca else 0
    if ca and cat not in ("leisure", "social") and streak_ticks >= FOCUS_TICKS:
        min_idle = l0.min_idle_in_streak()
        if min_idle is not None and min_idle < 30:
            minutes = streak_ticks * HEARTBEAT_INTERVAL_SEC // 60
            candidates.append({"key": f"focus:{ca}", "category": "long_focus",
                               "priority": 4,
                               "detail": f"用户在 {ca} 持续专注约 {minutes} 分钟没怎么动,可能该歇会儿"})

    # 4) 频繁切换(分心)—— 最近窗口内不同 App ≥ 阈值
    distinct = l0.distinct_apps_in_last(DISTRACTED_WINDOW_TICKS)
    if distinct >= DISTRACTED_APP_COUNT:
        candidates.append({"key": "distracted:recent", "category": "distracted",
                           "priority": 3,
                           "detail": f"最近用户切换了 {distinct} 个应用,看着有点忙/分心"})

    # 5) 娱乐/社交太久 → 提醒回工作
    if cat in ("leisure", "social") and ca and streak_ticks >= LEISURE_TICKS:
        minutes = streak_ticks * HEARTBEAT_INTERVAL_SEC // 60
        candidates.append({"key": f"leisure_long:{ca}", "category": "leisure_too_long",
                           "priority": 4,
                           "detail": f"用户在 {ca} 待了约 {minutes} 分钟了,要不要回去干活?"})

    # 6) 深夜还在敲代码(最高优先级关心)
    if curr.get("is_deep_night") and cat == "dev" and ci is not None and ci < 60:
        hour = curr.get("hour")
        candidates.append({"key": "deep_night:working", "category": "deep_night_working",
                           "priority": 5,
                           "detail": f"都 {hour} 点了用户还在 {ca} 敲代码,提醒早点歇吧"})

    # 7) 早晨首次活动(刚坐下)
    if curr.get("is_early_morning") and ci is not None and 5 < ci < 120:
        candidates.append({"key": "morning:greet", "category": "morning_greet",
                           "priority": 3, "detail": "早上好,用户刚坐下开始新的一天"})

    return candidates


class CooldownTracker:
    """运行期可切档的冷却追踪器(替换旧模块级 _recent + 全局 _COOLDOWN_SEC + filter_cooldown)。

    set_mode(来自 WS 回调)与 take(来自 _tick)都在同一 asyncio loop,单线程不抢占,无需锁。
    切档不清空 _recent:只影响后续新触发的冷却值,防切到"活跃"档瞬间刷屏。
    """

    def __init__(self, mode: str = "balanced") -> None:
        self.mode = mode if mode in COOLDOWN_PROFILES else "balanced"
        self._recent: dict = {}      # key → 上次触发 monotonic 时间戳

    def set_mode(self, mode: str) -> None:
        if mode in COOLDOWN_PROFILES:
            self.mode = mode

    def _cooldown_for(self, category: str) -> float:
        profile = COOLDOWN_PROFILES[self.mode]
        return profile.get(category, profile["default"])

    def take(self, candidates: List[dict]) -> List[dict]:
        """返回通过冷却的候选(按 priority 降序),并把它们的 key 标记为已触发。"""
        now = time.monotonic()
        fired: List[dict] = []
        for c in sorted(candidates, key=lambda x: x.get("priority", 0), reverse=True):
            cd = self._cooldown_for(c["category"])
            ts = self._recent.get(c["key"])
            if ts is not None and now - ts < cd:
                continue
            self._recent[c["key"]] = now
            fired.append(c)
        return fired
