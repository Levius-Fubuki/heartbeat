"""感知层探针。Phase 1:真实 macOS 探针(ForegroundApp + Idle);Phase 3:WindowTitle + Clock。

- ForegroundApp: NSWorkspace.frontmostApplication(无需权限,取 App 名/bundle)。
- Idle: CGEventSource 自上次输入以来的秒数(Quartz;需"输入监控"权限)。
- WindowTitle: AXUIElement 取前台窗口标题(ApplicationServices;需"辅助功能"权限)。
  默认仅本地规则判断浏览器内容类别;是否经 get_activity 工具发云端由 TOOL_INCLUDE_WINDOW_TITLE
  开关控制(默认开,见 llm/tools.py + config.py)。
- Clock: 本地时间(零权限),供时段规则(深夜/清晨)。

所有探针失败/无权限均静默降级返回 None 字段,不抛异常(主循环不能因探针崩)。
"""
from __future__ import annotations

import logging
import time as _time

log = logging.getLogger("heartbeat.perception")


def probe_foreground_app() -> dict:
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app:
            return {
                "foreground_app": app.localizedName(),
                "foreground_bundle": app.bundleIdentifier(),
            }
    except Exception:  # noqa: BLE001
        log.exception("foreground probe failed")
    return {"foreground_app": None, "foreground_bundle": None}


def probe_idle() -> dict:
    """空闲秒数。无"输入监控"权限 → 返回 None(而非 0.0,避免被规则误判为"永不空闲")。"""
    try:
        from Quartz import (
            CGEventSourceSecondsSinceLastEventType,
            kCGAnyInputEventType, kCGEventSourceStateHIDSystemState,
        )
        secs = CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateHIDSystemState, kCGAnyInputEventType)
        return {"idle_sec": float(secs)}
    except Exception as e:  # noqa: BLE001
        log.warning("idle probe failed (无输入监控权限?): %s", e)
        return {"idle_sec": None}


def probe_window_title() -> dict:
    """前台窗口标题(AXUIElement)。无辅助功能权限/失败 → None,不崩。"""
    try:
        from AppKit import NSWorkspace
        from ApplicationServices import (
            AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
            kAXFocusedWindowAttribute, kAXTitleAttribute,
        )
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if not app:
            return {"window_title": None}
        app_el = AXUIElementCreateApplication(app.processIdentifier())
        err, win = AXUIElementCopyAttributeValue(app_el, kAXFocusedWindowAttribute, None)
        if err != 0 or not win:
            return {"window_title": None}
        err2, title = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
        if err2 == 0 and title:
            return {"window_title": str(title)}
    except Exception:  # noqa: BLE001  (无辅助功能权限时 import/调用抛错)
        log.debug("window_title probe unavailable (无辅助功能权限?)")
    return {"window_title": None}


def probe_clock() -> dict:
    """本地时间维度(零权限)。is_deep_night=22-6 点;is_early_morning=6-9 点。"""
    lt = _time.localtime()
    h = lt.tm_hour
    return {
        "hour": h,
        "weekday": lt.tm_wday,
        "is_deep_night": h >= 22 or h < 6,
        "is_early_morning": 6 <= h < 9,
    }


def check_accessibility_permission(prompt: bool = False) -> bool:
    """是否拥有辅助功能权限。prompt=True 时首次会弹系统授权框。失败 → False。"""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        from Foundation import NSDictionary
        if prompt:
            opts = NSDictionary.dictionaryWithObject_forKey_(True, kAXTrustedCheckOptionPrompt)
        else:
            opts = None
        return bool(AXIsProcessTrustedWithOptions(opts))
    except Exception:  # noqa: BLE001
        return False


def run_probes() -> dict:
    """运行所有探针,合并成一次快照。"""
    snap: dict = {}
    snap.update(probe_foreground_app())
    snap.update(probe_idle())
    snap.update(probe_window_title())
    snap.update(probe_clock())
    return snap
