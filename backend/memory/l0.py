"""L0 实时状态缓存:最近 N 次心跳快照的滑动窗口,用于变化检测。Heartbeat 独有层。"""
from __future__ import annotations

from collections import deque


class StateCache:
    def __init__(self, maxlen: int = 150) -> None:
        # maxlen=150 覆盖约 20 分钟(8s/tick),足够 streak 判定达到 FOCUS_TICKS(90)/LEISURE_TICKS(112)
        self.snapshots: deque = deque(maxlen=maxlen)

    def push(self, snapshot: dict) -> None:
        self.snapshots.append(snapshot)

    @property
    def latest(self):
        return self.snapshots[-1] if self.snapshots else None

    @property
    def previous(self):
        return self.snapshots[-2] if len(self.snapshots) >= 2 else None

    # ---- 聚合查询(Phase 3 规则层用)----
    def current_app_streak_ticks(self, app) -> int:
        """当前前台 App 从最新快照往回连续相同的 tick 数(判断"持续了多久")。
        调用方乘 HEARTBEAT_INTERVAL_SEC 换算秒。app 为 None 时返回 0。"""
        n = 0
        for s in reversed(self.snapshots):
            if app is not None and s.get("foreground_app") == app:
                n += 1
            else:
                break
        return n

    def distinct_apps(self) -> int:
        """窗口内出现过的不同前台 App 数。"""
        return len({s.get("foreground_app") for s in self.snapshots if s.get("foreground_app")})

    def distinct_apps_in_last(self, n_ticks: int) -> int:
        """最近 n_ticks 个快照内的不同前台 App 数(判断近期频繁切换/分心;窗口独立于 streak)。"""
        recent = list(self.snapshots)[-n_ticks:]
        return len({s.get("foreground_app") for s in recent if s.get("foreground_app")})

    def min_idle_in_streak(self):
        """当前 App 持续期间的最小 idle 秒数(判断真专注 vs 发呆)。
        idle_sec 为 None(权限缺失)的快照不参与;全 None → 返回 None。"""
        app = self.latest.get("foreground_app") if self.latest else None
        idles = []
        for s in reversed(self.snapshots):
            if app is not None and s.get("foreground_app") != app:
                break
            i = s.get("idle_sec")
            if i is not None:
                idles.append(i)
        return min(idles) if idles else None
