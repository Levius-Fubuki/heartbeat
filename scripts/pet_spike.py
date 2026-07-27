"""桌宠透明窗口 spike v5:标准 GUI app 生命周期 + 可成为 key 的透明窗口。

变更(相对 v4):
  - PetWindow 子类覆写 canBecomeKey/canBecomeMain = YES(无边框窗口默认 NO)
  - makeKeyAndOrderFront 取代 orderFrontRegardless
  - content view layer-backed + 透明层
  - AppDelegate + AppHelper.runEventLoop() 标准生命周期
判读:原生按钮与 webview 都该能收事件。
"""
from __future__ import annotations

import logging

from AppKit import (
    NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
    NSBezelStyleRounded, NSButton, NSColor, NSFloatingWindowLevel, NSMakeRect, NSScreen,
    NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowStyleMaskBorderless,
)
from Foundation import NSObject
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController
from PyObjCTools import AppHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [spike] %(message)s")
log = logging.getLogger("spike")

PET_W, PET_H = 220, 340
_REFS = []

PET_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:transparent;overflow:hidden;font-family:-apple-system,sans-serif;}
#pet{position:absolute;left:50%;top:42%;transform:translate(-50%,-50%);
  width:108px;height:108px;border-radius:50% 50% 46% 46%;
  background:radial-gradient(circle at 35% 30%,#ffffffcc,#c9d6e5);
  box-shadow:0 10px 26px #00000055,0 0 26px #c9d6e5;transition:transform .15s,box-shadow .15s;}
#pet.hot{transform:translate(-50%,-50%) scale(1.14);box-shadow:0 12px 30px #00000066,0 0 44px #fff;}
#pet.bump{animation:bump .3s ease;}
.eye{position:absolute;width:10px;height:13px;background:#2a2b3d;border-radius:50%;top:46%;}
.eye.l{left:31%;}.eye.r{right:31%;}
@keyframes bump{0%{transform:translate(-50%,-50%) scale(1);}50%{transform:translate(-50%,-50%) scale(1.25);}100%{transform:translate(-50%,-50%) scale(1);}}
#status{position:absolute;bottom:6px;width:100%;text-align:center;color:#9aa;font-size:12px;}
</style></head><body>
<div id="pet"><div class="eye l"></div><div class="eye r"></div></div>
<div id="status">webview 区:悬停/点击</div>
<script>
function p(t){ try{ window.webkit.messageHandlers.pet.postMessage(t); }catch(e){} }
var pet=document.getElementById('pet'),status=document.getElementById('status');
var h=false,c=0;
function show(){status.textContent='webview  hover:'+(h?'Y':'-')+' click:'+c;}
document.addEventListener('mousemove',function(){if(!h){h=true;pet.classList.add('hot');p('hover_in');show();}});
document.addEventListener('mouseout',function(){if(h){h=false;pet.classList.remove('hot');p('hover_out');show();}});
document.addEventListener('click',function(){c++;pet.classList.remove('bump');void pet.offsetWidth;pet.classList.add('bump');p('click');show();});
p('ready');
</script></body></html>"""


class PetWindow(NSWindow):
    """无边框透明窗口,但允许成为 key/main(默认 NO,导致事件不派发)。"""

    def canBecomeKey(self):
        return True

    def canBecomeMain(self):
        return True


class Controller(NSObject):
    def onBtn_(self, sender):  # noqa: N802
        log.info("NATIVE BUTTON CLICKED ✓ → 窗口能收到事件")


class PetBridge(NSObject):
    def userContentController_didReceiveScriptMessage_(self, controller, message):  # noqa: N802
        log.info("JS: %s", message.body())


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):  # noqa: N802
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        win, controller, bridge, web = make_window()
        _REFS.extend([win, controller, bridge, web])
        log.info("v5 up — 上:桌宠(webview) 下:原生按钮。分别试")


def make_window():
    screen = NSScreen.mainScreen().frame()
    x = screen.size.width - PET_W - 70
    y = 120
    win = PetWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, PET_W, PET_H), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
    win.setLevel_(NSFloatingWindowLevel)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setHasShadow_(False)
    win.setIgnoresMouseEvents_(False)
    win.setAcceptsMouseMovedEvents_(True)
    win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
    win.setMovableByWindowBackground_(False)
    win.setTitle_("")

    cv = win.contentView()
    cv.setWantsLayer_(True)
    try:
        cv.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
    except Exception:
        pass

    controller = Controller.alloc().init()
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(25, 24, 170, 46))
    btn.setTitle_("原生按钮 · 点我测试")
    btn.setBezelStyle_(NSBezelStyleRounded)
    btn.setTarget_(controller)
    btn.setAction_("onBtn:")
    cv.addSubview_(btn)

    config = WKWebViewConfiguration.alloc().init()
    uc = WKUserContentController.alloc().init()
    bridge = PetBridge.alloc().init()
    uc.addScriptMessageHandler_name_(bridge, "pet")
    config.setUserContentController_(uc)
    web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 90, PET_W, 230), config)
    try:
        web.setUnderPageBackgroundColor_(NSColor.clearColor())
    except Exception:
        pass
    try:
        web.setValue_forKey_(False, "drawsBackground")
    except Exception:
        pass
    cv.addSubview_(web)
    web.loadHTMLString_baseURL_(PET_HTML, None)

    win.makeKeyAndOrderFront_(None)
    return win, controller, bridge, web


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
