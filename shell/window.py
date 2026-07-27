"""pyobjc 窗口构造:透明桌宠窗 + 常规主窗口。"""
from __future__ import annotations

import logging

from AppKit import (
    NSBackingStoreBuffered, NSColor, NSFloatingWindowLevel, NSMakeRect, NSNormalWindowLevel,
    NSPanel, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable, NSWindowStyleMaskFullSizeContentView, NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled, NSWindowTitleHidden,
)
from Foundation import NSMutableURLRequest, NSURL, NSURLRequest, NSURLRequestReloadIgnoringLocalCacheData
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore, WKUserContentController

log = logging.getLogger("heartbeat.shell.window")


class PetWindow(NSPanel):
    """透明无边框置顶面板。用 NSPanel 而非 NSWindow:NSPanel 默认 canBecomeKey=YES,
    borderless NSWindow 默认 NO 且覆写常不生效 → 桌宠输入框收不到键盘。"""

    def canBecomeKey(self):
        return True

    def canBecomeMain(self):
        return True


def build_transparent_window(rect, html_url, msg_handler, msg_name="pet"):
    win = PetWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
    win.setLevel_(NSFloatingWindowLevel)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setHasShadow_(False)
    win.setIgnoresMouseEvents_(False)
    win.setAcceptsMouseMovedEvents_(True)        # hover 必需
    win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
    win.setMovableByWindowBackground_(False)
    win.setHidesOnDeactivate_(False)            # 切到别的 app 时桌宠不要消失
    win.setTitle_("")

    cv = win.contentView()
    cv.setWantsLayer_(True)
    try:
        cv.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
    except Exception:
        pass

    config = WKWebViewConfiguration.alloc().init()
    try:
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())  # 禁磁盘缓存:换图后重启必出新图
    except Exception:
        pass
    uc = WKUserContentController.alloc().init()
    uc.addScriptMessageHandler_name_(msg_handler, msg_name)
    config.setUserContentController_(uc)

    web = WKWebView.alloc().initWithFrame_configuration_(cv.bounds(), config)
    _make_webview_transparent(web)
    cv.addSubview_(web)

    req = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
        NSURL.URLWithString_(html_url), NSURLRequestReloadIgnoringLocalCacheData, 10)
    web.loadRequest_(req)
    win.makeKeyAndOrderFront_(None)
    log.info("pet window created: %s", html_url)
    return win, web


def build_main_window(rect, html_url, msg_handler=None, msg_name="main"):
    """常规标题栏主窗口(不透明),承载完整聊天。

    msg_handler 非 None 时注册 JS→Python 桥接(chat.js 经 window.webkit.messageHandlers[<msg_name>]
    发消息,如齿轮开设置窗)。照搬 build_transparent_window 的 userContentController 接法。
    """
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect,
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
        NSBackingStoreBuffered, False)
    win.setTitle_("Heartbeat")
    win.setLevel_(NSNormalWindowLevel)
    win.setReleasedWhenClosed_(False)            # 关闭后保留对象,可再 makeKeyAndOrderFront

    config = WKWebViewConfiguration.alloc().init()
    try:
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())  # 禁磁盘缓存:换图后重启必出新图
    except Exception:
        pass
    if msg_handler is not None:
        uc = WKUserContentController.alloc().init()
        uc.addScriptMessageHandler_name_(msg_handler, msg_name)
        config.setUserContentController_(uc)
    web = WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(), config)
    web.setAutoresizingMask_(18)                 # 跟随窗口缩放
    win.contentView().addSubview_(web)

    req = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
        NSURL.URLWithString_(html_url), NSURLRequestReloadIgnoringLocalCacheData, 10)
    web.loadRequest_(req)
    win.makeKeyAndOrderFront_(None)
    log.info("main window created: %s", html_url)
    return win, web


def build_settings_window(rect, html_url, msg_handler=None, msg_name="settings"):
    """设置窗:普通标题栏 NSWindow(更大,520×640),承载设置页。能成为 key、可关/最小化。
    保存走 HTTP(/api/settings);msg_handler 非 None 时注册 JS→Python 桥接(Phase 6b:选文件夹按钮
    经 window.webkit.messageHandlers.settings 发 pick_folder)。深底 + underPageBg 防加载白闪。"""
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect,
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
        NSBackingStoreBuffered, False)
    win.setTitle_("设置")
    win.setLevel_(NSNormalWindowLevel)
    win.setReleasedWhenClosed_(False)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.10, 0.11, 0.20, 0.98))

    config = WKWebViewConfiguration.alloc().init()
    try:
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())  # 禁磁盘缓存
    except Exception:
        pass
    if msg_handler is not None:                       # 照搬 build_main_window 的桥接块
        uc = WKUserContentController.alloc().init()
        uc.addScriptMessageHandler_name_(msg_handler, msg_name)
        config.setUserContentController_(uc)
    web = WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(), config)
    web.setAutoresizingMask_(18)
    try:
        web.setUnderPageBackgroundColor_(           # 防加载白闪,与窗口底色一致
            NSColor.colorWithRed_green_blue_alpha_(0.10, 0.11, 0.20, 0.98))
    except Exception:  # noqa: BLE001  # 旧版 macOS 无此 API
        pass
    win.contentView().addSubview_(web)

    req = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
        NSURL.URLWithString_(html_url), NSURLRequestReloadIgnoringLocalCacheData, 10)
    web.loadRequest_(req)
    win.makeKeyAndOrderFront_(None)
    log.info("settings window created: %s", html_url)
    return win, web


def build_input_window(rect, html_url):
    """就地输入框窗口:普通 NSWindow(非浮动、非透明),确保能成为 key 接收键盘。

    透明+浮动+borderless 窗口在 macOS 拒绝成为 key(见 PetWindow 的坑),所以输入框
    必须用普通窗口载体。这里保留 titled 以确保 canBecomeKey,同时把标题栏透明+隐藏,
    视觉接近无边框小窗;透明标题栏区仍可作拖动柄。
    Fallback:若实测键盘仍进不去,去掉 setTitlebarAppearsTransparent_,保留极简实心标题栏。
    """
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect,
        NSWindowStyleMaskTitled | NSWindowStyleMaskFullSizeContentView,
        NSBackingStoreBuffered, False)
    win.setTitle_("")
    win.setTitlebarAppearsTransparent_(True)        # 标题栏透明:视觉无边框,但保留 key 能力
    win.setTitleVisibility_(NSWindowTitleHidden)
    win.setLevel_(NSNormalWindowLevel)              # 普通层级(能 key 的关键,与 MainWindow 一致)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.10, 0.11, 0.20, 0.94))
    win.setHasShadow_(True)                          # 浮窗质感
    win.setReleasedWhenClosed_(False)               # 关闭后保留对象,可再 makeKeyAndOrderFront
    win.setMovableByWindowBackground_(True)         # 透明标题栏区可作拖动柄

    config = WKWebViewConfiguration.alloc().init()
    try:
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())  # 禁磁盘缓存:换图后重启必出新图
    except Exception:
        pass
    web = WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(), config)
    web.setAutoresizingMask_(18)                    # 跟随窗口缩放
    try:
        web.setUnderPageBackgroundColor_(           # 防加载白闪,与窗口底色一致
            NSColor.colorWithRed_green_blue_alpha_(0.10, 0.11, 0.20, 0.94))
    except Exception:  # noqa: BLE001  # 旧版 macOS 无此 API
        pass
    win.contentView().addSubview_(web)

    req = NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
        NSURL.URLWithString_(html_url), NSURLRequestReloadIgnoringLocalCacheData, 10)
    web.loadRequest_(req)
    win.makeKeyAndOrderFront_(None)
    log.info("input window created: %s", html_url)
    return win, web


def _make_webview_transparent(web):
    try:
        web.setUnderPageBackgroundColor_(NSColor.clearColor())
    except Exception:
        pass
    try:
        web.setValue_forKey_(False, "drawsBackground")
    except Exception:
        pass
