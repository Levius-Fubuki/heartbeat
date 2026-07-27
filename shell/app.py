"""pyobjc 桌宠壳入口:NSApplication + AppDelegate;后台线程跑 uvicorn;透明桌宠窗 + 主窗口。

main.py 调用这里的 main()。
"""
from __future__ import annotations

import json
import logging
import os
import threading

from AppKit import (
    NSApplication, NSApplicationActivationPolicyRegular, NSMakePoint, NSMakeRect, NSMenu, NSOpenPanel,
    NSScreen, NSTimer,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from shell.window import (
    build_input_window, build_main_window, build_settings_window, build_transparent_window,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("heartbeat.shell")

HOST, PORT = "127.0.0.1", 8765
PET_URL = f"http://{HOST}:{PORT}/static/pet.html"
MAIN_URL = f"http://{HOST}:{PORT}/static/chat.html"
INPUT_URL = f"http://{HOST}:{PORT}/static/input.html"
SETTINGS_URL = f"http://{HOST}:{PORT}/static/settings.html"

_APP = None  # AppDelegate 引用(供 message handler 回调)


def _install_menu():
    """装主菜单 + Edit 菜单。

    pyobjc 默认不建 Edit 菜单 → macOS 的 ⌘C/⌘V/⌘X/⌘A/⌘Z 经「主菜单 key equivalent」派发时
    找不到对应菜单项,WKWebView 收不到这些编辑命令(表现为:选中聊天正文拷不了、输入框粘不进)。
    加 Edit 菜单(标准 copy:/paste:/cut:/selectAll:/undo:/redo: selector,target=nil 自动走响应链
    到首响应者 WKWebView),快捷键即恢复;选中非编辑区文本也能拷贝。"""
    app = NSApplication.sharedApplication()
    main = app.mainMenu()
    if main is None:
        main = NSMenu.alloc().init()
        app.setMainMenu_(main)
    # App 菜单(隐藏/退出;已存在则跳过)
    if main.itemWithTitle_("Heartbeat") is None:
        ai = main.addItemWithTitle_action_keyEquivalent_("Heartbeat", None, "")
        sub = NSMenu.alloc().initWithTitle_("Heartbeat")
        sub.addItemWithTitle_action_keyEquivalent_("隐藏 Heartbeat", "hide:", "h")
        sub.addItemWithTitle_action_keyEquivalent_("隐藏其他", "hideOtherApplications:", "")
        sub.addItemWithTitle_action_keyEquivalent_("全部显示", "unhideAllApplications:", "")
        sub.addItemWithTitle_action_keyEquivalent_("退出 Heartbeat", "terminate:", "q")
        ai.setSubmenu_(sub)
    # Edit 菜单(关键:让 ⌘C/⌘V/⌘X/⌘A/⌘Z 生效)
    if main.itemWithTitle_("编辑") is None:
        ei = main.addItemWithTitle_action_keyEquivalent_("编辑", None, "")
        esub = NSMenu.alloc().initWithTitle_("编辑")
        for title, action, key in [
            ("撤销", "undo:", "z"), ("重做", "redo:", "Z"),
            ("剪切", "cut:", "x"), ("拷贝", "copy:", "c"),
            ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a"),
        ]:
            esub.addItemWithTitle_action_keyEquivalent_(title, action, key)
        ei.setSubmenu_(esub)


def start_backend():
    import uvicorn
    from backend.app import app as fastapi_app

    def run():
        uvicorn.run(fastapi_app, host=HOST, port=PORT, log_level="warning")

    threading.Thread(target=run, daemon=True).start()


class PetMessageHandler(NSObject):
    def userContentController_didReceiveScriptMessage_(self, controller, message):  # noqa: N802
        body = message.body()
        log.info("pet → py: %s", body)
        if _APP is None:
            return
        if body == "open_main":
            _APP.open_main_window()
        elif body == "open_input":
            _APP.toggle_input_window()
        elif body == "open_settings":
            _APP.open_settings_window()
        elif body == "attach_file":
            _APP.pick_attach_file()
        elif body == "pick_folder":
            _APP.pick_allowed_dir()
        elif body == "drag_start":
            _APP.begin_pet_drag()
        elif body == "drag_end":
            _APP.end_pet_drag()
        elif isinstance(body, str) and body.startswith("{"):
            try:
                data = json.loads(body)
            except Exception:  # noqa: BLE001
                return
            if data.get("type") == "drag_move":
                _APP.drag_pet(float(data.get("dx", 0)), float(data.get("dy", 0)))


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):  # noqa: N802
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        _install_menu()                              # Edit 菜单:让 ⌘C/⌘V/⌘X/⌘A 在 WKWebView 里生效
        # 辅助功能权限(窗口标题探针需要):首次弹系统授权框;未授权探针静默降级,不崩
        from backend.perception import probes
        if not probes.check_accessibility_permission(prompt=True):
            log.warning("无辅助功能权限——窗口标题探针将降级(系统设置→隐私与安全性→辅助功能 开启)")
        start_backend()

        self.main_win = None
        self.main_web = None
        self.input_win = None
        self.input_web = None
        self.settings_win = None
        self.settings_web = None

        # 桌宠初始位置:主屏右下角。NSScreen.frame() 是全局坐标——主屏(菜单栏屏)
        # origin=(0,0),但外接屏 origin 非零(在左则 origin.x<0)。必须 + origin,否则多屏时
        # 位置会算到屏外(单屏 origin 恒 0,0 故无此问题)。mainScreen() 极端情况返回 None 时
        # 回退 screens()[0](主屏)。
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        sf = screen.frame()
        # 窗口 230×300:精灵贴底,上方留气泡空间(避免裁切);origin.y +270 让整体(含输入框)上移(用户从 +210 调到 +270)
        rect = NSMakeRect(sf.origin.x + sf.size.width - 230 - 40,
                          sf.origin.y + 270, 230, 300)
        self.handler = PetMessageHandler.alloc().init()
        self.pet_win, self.pet_web = build_transparent_window(rect, PET_URL, self.handler)
        # 首屏自动开主窗(未 onboarding 时是选人格,否则聊天);延时等 backend 起来
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.8, self, "openMainAfterLaunch:", None, False)
        log.info("shell up — 透明桌宠浮现;主窗即将自动打开")

    def openMainAfterLaunch_(self, timer):  # noqa: N802  # pyobjc:中间下划线会被当 selector 冒号,须 camelCase
        if self.main_win is None:
            self.open_main_window()

    def open_main_window(self):
        try:
            if self.main_win is None:
                rect = NSMakeRect(140, 140, 380, 560)
                self.main_win, self.main_web = build_main_window(rect, MAIN_URL, self.handler, "main")
                log.info("main window opened")
            else:
                self.main_win.makeKeyAndOrderFront_(None)
                log.info("main window refocused")
        except Exception:  # noqa: BLE001
            log.exception("open_main_window failed")

    def open_settings_window(self):
        """设置窗:主窗齿轮触发打开(普通标题栏窗,520×640 居中)。已开则前置。"""
        try:
            if self.settings_win is None:
                screen = NSScreen.mainScreen() or NSScreen.screens()[0]
                sf = screen.frame()
                iw, ih = 520, 640                                   # 设置窗宽高
                rect = NSMakeRect(sf.origin.x + (sf.size.width - iw) / 2,      # 水平居中
                                  sf.origin.y + (sf.size.height - ih) / 2,     # 垂直居中
                                  iw, ih)
                self.settings_win, self.settings_web = build_settings_window(
                    rect, SETTINGS_URL, self.handler, "settings")
                log.info("settings window opened")
            else:
                self.settings_win.makeKeyAndOrderFront_(None)
                log.info("settings window refocused")
        except Exception:  # noqa: BLE001
            log.exception("open_settings_window failed")

    def pick_attach_file(self):
        """➕ 附件:原生 NSOpenPanel 选一个文件,选完 evaluateJavaScript 回填主窗 pendingAttach。

        选文件(非目录);解析在后端(heartbeat._build_user_text → filereader.read_file)。
        message handler 在主线程派发,runModal 阻塞主线程(用户点击触发,OK);uvicorn/心跳在 daemon
        线程不受影响,仅主窗 JS 暂停等回执。
        """
        try:
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_("附加")
            panel.setMessage_("选一个文件附加到这条消息(笔记/代码/PDF/Word,内容会发给模型)")
            if panel.runModal() == 1 and panel.URLs():     # 1 = NSFileHandlingPanelOKButton
                path = panel.URLs()[0].path()
                name = os.path.basename(path)
                js = "window.HB_onAttachPicked && window.HB_onAttachPicked(%s, %s);" \
                     % (json.dumps(name), json.dumps(path))
                if self.main_web is not None:
                    self.main_web.evaluateJavaScript_completionHandler_(js, None)
                log.info("attach file picked: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("pick_attach_file failed")

    def pick_allowed_dir(self):
        """工具与权限卡「选择文件夹」:原生 NSOpenPanel 选一个目录,加入文件工具白名单。

        选目录(非文件);选完 evaluateJavaScript 回填设置窗(HB_onDirPicked 把它加进待存的
        allowed_dirs 列表,点保存才落盘)。message handler 主线程派发,runModal 阻塞主线程 OK;
        uvicorn/心跳在 daemon 线程不受影响。
        """
        try:
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(False)
            panel.setCanChooseDirectories_(True)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_("添加")
            panel.setMessage_("选一个文件夹,允许它读取这里的文件(桌面/文档/下载默认已允许)")
            if panel.runModal() == 1 and panel.URLs():     # 1 = NSFileHandlingPanelOKButton
                path = panel.URLs()[0].path()
                js = "window.HB_onDirPicked && window.HB_onDirPicked(%s);" % json.dumps(path)
                if self.settings_web is not None:
                    self.settings_web.evaluateJavaScript_completionHandler_(js, None)
                log.info("allowed dir picked: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("pick_allowed_dir failed")

    def open_input_window(self):
        """桌宠就地输入框:在桌宠正下方弹出普通小窗(能成为 key 接收键盘)。"""
        try:
            pf = self.pet_win.frame()
            iw, ih = 246, 130                                   # 输入框宽高(桌宠 origin.y=140 → iy=10,贴屏底不出屏)
            ix = pf.origin.x + (pf.size.width - iw) / 2         # 水平居中对齐桌宠
            iy = pf.origin.y - ih                               # 紧贴桌宠底部下方(macOS Y 轴向上)
            rect = NSMakeRect(ix, iy, iw, ih)
            if self.input_win is None:
                self.input_win, self.input_web = build_input_window(rect, INPUT_URL)
                log.info("input window opened")
            else:
                self.input_win.setFrame_display_(rect, True)
                self.input_win.makeKeyAndOrderFront_(None)
                log.info("input window refocused")
        except Exception:  # noqa: BLE001
            log.exception("open_input_window failed")

    def toggle_input_window(self):
        """单击桌宠:toggle 输入框(可见→收起;隐藏→展开并重定位到桌宠底部)。"""
        try:
            if self.input_win is not None and self.input_win.isVisible():
                self.input_win.orderOut_(None)
                log.info("input window hidden")
            else:
                self.open_input_window()
        except Exception:  # noqa: BLE001
            log.exception("toggle_input_window failed")

    def begin_pet_drag(self):
        self._dragging_pet = True
        log.info("pet drag begin (origin=%s)", self.pet_win.frame().origin)

    def drag_pet(self, dx, dy):
        """长按拖拽:按鼠标增量移动桌宠窗口。macOS 原点左下、Y 向上;浏览器 movementY 向下为正 → 反向。
        若就地输入框可见,同步跟随(保持贴在桌宠下方)。"""
        if not getattr(self, "_dragging_pet", False):
            return
        try:
            o = self.pet_win.frame().origin
            self.pet_win.setFrameOrigin_(NSMakePoint(o.x + dx, o.y - dy))
            if self.input_win is not None and self.input_win.isVisible():
                io = self.input_win.frame().origin
                self.input_win.setFrameOrigin_(NSMakePoint(io.x + dx, io.y - dy))
        except Exception:  # noqa: BLE001
            log.exception("drag_pet failed")

    def end_pet_drag(self):
        self._dragging_pet = False
        log.info("pet drag end (origin=%s)", self.pet_win.frame().origin)


def main():
    global _APP
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().init()
    _APP = delegate
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
