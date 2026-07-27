#!/usr/bin/env python3
"""Phase 6d 写入/执行工具 probe —— ApprovalGate 闸门 + _check_command + dispatch 4 工具 + task_tools 计数 + exec_log。

确定性、不联网。ApprovalGate 用真实 asyncio loop(后台线程)+ FakeHub 录广播;dispatch 用 FakeAuthorizer
录调用并可控返「允许/拒绝」。临时目录挂进 sandbox 做白名单样本,不动用户真实目录。
trash 用 monkeypatch 免真移废纸篓。run:`.venv/bin/python scripts/exec_probe.py`
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.judgment import sandbox                            # noqa: E402
from backend.llm import tools as tools_mod                      # noqa: E402
from backend.llm.tools import (                                 # noqa: E402
    TOOLS, dispatch_tool, task_tools, _check_command,
    _WRITE_TOOLS, _EXEC_TOOLS,
)
from backend.approval import ApprovalGate                       # noqa: E402
from backend.memory.execlog import ExecLog                      # noqa: E402
from backend.memory.settings import settings                    # noqa: E402

PASS = 0
FAIL = 0
_ORIG = {}


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def snap():
    for k in ("file_tools_enabled", "web_search_enabled", "write_tools_enabled",
              "run_command_enabled", "command_allowlist", "tavily_key"):
        _ORIG[k] = settings._data.get(k)


def restore():
    for k, v in _ORIG.items():
        settings._data[k] = v


# ---------- ApprovalGate 测试桩 ----------
class FakeHub:
    """录所有广播 payload(线程安全)。send 是 async(被 run_coroutine_threadsafe 调度)。"""
    def __init__(self):
        self._lock = threading.Lock()
        self.payloads = []

    async def send(self, payload):
        with self._lock:
            self.payloads.append(payload)

    def find(self, ptype):
        with self._lock:
            return [p for p in self.payloads if p.get("type") == ptype]


def wait_for(cond, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


class LoopThread:
    """后台跑一个 asyncio loop(供 run_coroutine_threadsafe 调度)。"""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._th.join(timeout=3)


def approval_request_in_thread(gate, **kw):
    """在 worker 线程调 gate.request(模拟 dispatch_tool 在 to_thread 里),返 (thread, result_box)。"""
    box = {}

    def worker():
        box["r"] = gate.request(**kw)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t, box


# ---------- dispatch 用桩 ----------
class FakeAuthorizer:
    """录调用;按 queue 返预设 (approved, reason)。默认允许。"""
    def __init__(self, decisions=None):
        self.calls = []
        self._decisions = list(decisions or [])

    def request(self, tool, args, summary, persona_id, timeout=None):
        self.calls.append({"tool": tool, "args": args, "summary": summary, "persona": persona_id})
        if self._decisions:
            return self._decisions.pop(0)
        return (True, "")


def deps_for(tmp, authorizer=None, exec_log=None, persona="zorya"):
    d = {"persona_id": persona, "exec_log": exec_log or ExecLog(path=tmp / "exec.jsonl")}
    if authorizer is not None:
        d["authorizer"] = authorizer
    return d


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb6d_"))
    lt = LoopThread()
    try:
        snap()
        settings._data["file_tools_enabled"] = True
        settings._data["web_search_enabled"] = False
        settings._data["tavily_key"] = ""
        settings._data["write_tools_enabled"] = True
        settings._data["run_command_enabled"] = True
        settings._data["command_allowlist"] = ["ls", "cat", "echo", "python3", "git", "mkdir"]
        # 把临时树挂成允许根(替换默认 桌面/文档/下载,免碰用户真实目录)
        orig_roots = sandbox.allowed_roots
        sandbox.allowed_roots = lambda: [tmp.resolve()]

        # ===================== A. ApprovalGate =====================
        print("[ApprovalGate]")
        hub = FakeHub()
        gate = ApprovalGate(hub)
        gate.bind_loop(lt.loop)
        check("bind_loop 后 loop 已绑", gate._loop is lt.loop)

        # A1 用户允许
        t, box = approval_request_in_thread(
            gate, tool="write_file", args={"path": "/x", "content": "hi"},
            summary="写入 /x", persona_id="zorya", timeout=8)
        ok = wait_for(lambda: hub.find("confirm_request"))
        check("confirm_request 已广播", ok)
        if ok:
            req = hub.find("confirm_request")[-1]
            check("confirm_request 含 id/tool/args/summary", all(k in req for k in ("id", "tool", "args", "summary")))
            check("args 被截断预览(content 未原样全发)", req["args"].get("path") == "/x")
            gate.resolve(req["id"], True)
            t.join(timeout=5)
            check("允许 → 返 (True, '')", box.get("r") == (True, ""))
            check("resolve 后广播 confirm_close(approved=True)",
                  wait_for(lambda: any(p.get("type") == "confirm_close" and p.get("approved") is True for p in hub.payloads)))

        # A2 用户拒绝
        hub2 = FakeHub()
        gate2 = ApprovalGate(hub2); gate2.bind_loop(lt.loop)
        t2, box2 = approval_request_in_thread(
            gate2, tool="run_command", args={"command": "ls"}, summary="执行 ls", persona_id="rada", timeout=8)
        wait_for(lambda: hub2.find("confirm_request"))
        req2 = hub2.find("confirm_request")[-1]
        hit = gate2.resolve(req2["id"], False)
        t2.join(timeout=5)
        check("resolve 命中 pending 返 True", hit is True)
        check("拒绝 → 返 (False, 原因)", box2.get("r") and box2["r"][0] is False and box2["r"][1])

        # A3 超时否决(不 resolve)
        hub3 = FakeHub()
        gate3 = ApprovalGate(hub3); gate3.bind_loop(lt.loop)
        t3, box3 = approval_request_in_thread(
            gate3, tool="make_dir", args={"path": "/d"}, summary="建目录", persona_id="mira", timeout=0.6)
        t3.join(timeout=5)
        check("超时 → 返 (False, 超时原因)",
              box3.get("r") and box3["r"][0] is False and "超时" in box3["r"][1])
        check("超时后广播 confirm_close",
              wait_for(lambda: any(p.get("type") == "confirm_close" for p in hub3.payloads)))
        check("超时后 pending 已清", not gate3._pending)

        # A4 未知 id resolve no-op
        check("resolve 未知 id → False(no-op)", gate.resolve("nonexistent-id", True) is False)

        # A5 loop 未绑 → 优雅跳过
        gate_unbound = ApprovalGate(FakeHub())
        r = gate_unbound.request("write_file", {"path": "/x"}, "s", "zorya", timeout=2)
        check("loop 未绑 → 返否决(闸门未就绪)", r[0] is False and "未就绪" in r[1])

        # ===================== B. _check_command =====================
        print("[_check_command]")
        argv, reason = _check_command("ls -la")
        check("白名单内命令通过", not reason and argv == ["ls", "-la"])
        _, reason = _check_command("rm -rf /")
        check("破坏性命令硬封(rm)", "破坏性" in reason or "高危" in reason)
        _, reason = _check_command("sudo ls")
        check("破坏性命令硬封(sudo)", "破坏性" in reason or "高危" in reason)
        _, reason = _check_command("ls | grep x")
        check("含管道元字符拒绝", "禁止" in reason)
        _, reason = _check_command("cat a > b")
        check("含重定向元字符拒绝", "禁止" in reason)
        _, reason = _check_command("ls && rm x")
        check("含 & 拼接拒绝", "禁止" in reason)
        _, reason = _check_command("curl http://x")
        check("curl 硬封(高危)", "破坏性" in reason or "高危" in reason)
        _, reason = _check_command("brew install x")
        check("非白名单命令拒绝", "白名单" in reason)
        _, reason = _check_command("")
        check("空命令拒绝", reason == "命令为空。")

        # 空白名单 = 全拒(模拟 run_command 实际关闭)
        settings._data["command_allowlist"] = []
        _, reason = _check_command("ls")
        check("空白名单 → ls 也拒", "白名单" in reason)
        settings._data["command_allowlist"] = ["ls", "cat", "echo", "python3", "git", "mkdir"]

        # ===================== C. dispatch write_file / make_dir / trash_file =====================
        print("[dispatch 写入工具]")
        # write_file:新建
        auth = FakeAuthorizer()
        elog = ExecLog(path=tmp / "exec.jsonl")
        p_new = tmp / "hello.txt"
        r = dispatch_tool("write_file", {"path": str(p_new), "content": "Hi 6d"}, deps_for(tmp, auth, elog))
        check("write_file 新建成功提示", "新建" in r and str(p_new) in r)
        check("文件真写入磁盘", p_new.exists() and p_new.read_text(encoding="utf-8") == "Hi 6d")
        check("authorizer 被调(tool=write_file)", auth.calls and auth.calls[0]["tool"] == "write_file")
        check("exec_log 记了一条 write_file", p_new.parent and (tmp / "exec.jsonl").exists())

        # write_file:覆盖
        auth2 = FakeAuthorizer()
        r = dispatch_tool("write_file", {"path": str(p_new), "content": "overwritten"}, deps_for(tmp, auth2))
        check("write_file 覆盖成功", "覆盖" in r and p_new.read_text() == "overwritten")
        check("覆盖时 summary 含「覆盖」", auth2.calls and "覆盖" in auth2.calls[0]["summary"])

        # write_file:超长拒
        auth3 = FakeAuthorizer()
        r = dispatch_tool("write_file", {"path": str(tmp / "big.txt"), "content": "x" * 99999},
                          deps_for(tmp, auth3))
        check("超长内容拒(未打扰用户)", "过长" in r and not auth3.calls)

        # write_file:目标已是目录
        (tmp / "adir").mkdir()
        auth4 = FakeAuthorizer()
        r = dispatch_tool("write_file", {"path": str(tmp / "adir"), "content": "x"}, deps_for(tmp, auth4))
        check("目标是目录拒", "目录" in r and not auth4.calls)

        # write_file:沙箱外拒
        auth5 = FakeAuthorizer()
        r = dispatch_tool("write_file", {"path": "/etc/hb6d_test", "content": "x"}, deps_for(tmp, auth5))
        check("沙箱外写拒绝(未打扰用户)", ("无权" in r) and not auth5.calls)

        # write_file:开关关
        settings._data["write_tools_enabled"] = False
        r = dispatch_tool("write_file", {"path": str(tmp / "x.txt"), "content": "x"}, deps_for(tmp, FakeAuthorizer()))
        check("write_tools 关 → 提示禁用", "禁用" in r)
        settings._data["write_tools_enabled"] = True

        # write_file:用户拒绝
        auth_deny = FakeAuthorizer(decisions=[(False, "不要")])
        p_deny = tmp / "denied.txt"
        r = dispatch_tool("write_file", {"path": str(p_deny), "content": "x"}, deps_for(tmp, auth_deny))
        check("用户拒绝 → 返原因,未写盘", ("不要" in r) and not p_deny.exists())

        # write_file:无 authorizer 优雅跳过
        r = dispatch_tool("write_file", {"path": str(tmp / "noauth.txt"), "content": "x"}, deps_for(tmp, authorizer=None))
        check("无 authorizer → 优雅跳过", "授权闸门不可用" in r and not (tmp / "noauth.txt").exists())

        # make_dir
        auth_md = FakeAuthorizer()
        d_new = tmp / "newdir" / "sub"
        r = dispatch_tool("make_dir", {"path": str(d_new)}, deps_for(tmp, auth_md))
        check("make_dir 建多层目录", "就绪" in r and d_new.is_dir())

        # trash_file(monkeypatch _trash 免真移废纸篓)
        trash_target = tmp / "to_trash.txt"
        trash_target.write_text("bye", encoding="utf-8")
        orig_trash = tools_mod._trash
        tools_mod._trash = lambda p: (True, "")
        try:
            auth_tr = FakeAuthorizer()
            r = dispatch_tool("trash_file", {"path": str(trash_target)}, deps_for(tmp, auth_tr))
            check("trash_file 成功(monkeypatch)", "废纸篓" in r)
        finally:
            tools_mod._trash = orig_trash

        # trash_file:不存在
        r = dispatch_tool("trash_file", {"path": str(tmp / "nope.txt")}, deps_for(tmp, FakeAuthorizer()))
        check("trash 不存在路径 → 提示", "不存在" in r)

        # ===================== D. dispatch run_command =====================
        print("[dispatch run_command]")
        # 真跑一条白名单内命令(echo)
        auth_rc = FakeAuthorizer()
        r = dispatch_tool("run_command", {"command": "echo hello6d"}, deps_for(tmp, auth_rc))
        check("run_command echo 成功 + 含退出码", "退出码 0" in r and "hello6d" in r)
        check("authorizer 被调(run_command)", auth_rc.calls and auth_rc.calls[0]["tool"] == "run_command")

        # run_command:白名单外
        r = dispatch_tool("run_command", {"command": "brew install x"}, deps_for(tmp, FakeAuthorizer()))
        check("run_command 非白名单拒", "白名单" in r)

        # run_command:破坏性
        r = dispatch_tool("run_command", {"command": "rm -rf /"}, deps_for(tmp, FakeAuthorizer()))
        check("run_command 破坏性硬封", "破坏性" in r or "高危" in r)

        # run_command:用户拒绝
        auth_rc_deny = FakeAuthorizer(decisions=[(False, "别跑")])
        r = dispatch_tool("run_command", {"command": "echo x"}, deps_for(tmp, auth_rc_deny))
        check("run_command 用户拒绝 → 原因", "别跑" in r)

        # run_command:开关关
        settings._data["run_command_enabled"] = False
        r = dispatch_tool("run_command", {"command": "echo x"}, deps_for(tmp, FakeAuthorizer()))
        check("run_command 关 → 提示禁用", "禁用" in r)
        settings._data["run_command_enabled"] = True

        # run_command:cwd 沙箱外
        r = dispatch_command = dispatch_tool("run_command", {"command": "echo x", "cwd": "/etc"},
                                             deps_for(tmp, FakeAuthorizer()))
        check("run_command cwd 沙箱外拒", "无权" in r)

        # ===================== E. task_tools() 计数(增量断言,稳健)=====================
        print("[task_tools() 计数]")
        settings._data["write_tools_enabled"] = False
        settings._data["run_command_enabled"] = False
        base = len(task_tools())
        settings._data["write_tools_enabled"] = True
        check("write_tools 开 → +3", len(task_tools()) == base + len(_WRITE_TOOLS))
        settings._data["run_command_enabled"] = True
        check("再开 run_command → +1", len(task_tools()) == base + len(_WRITE_TOOLS) + len(_EXEC_TOOLS))
        settings._data["write_tools_enabled"] = False
        check("只开 run_command → base +1", len(task_tools()) == base + len(_EXEC_TOOLS))
        names = {t["function"]["name"] for t in TOOLS}
        check("陪伴 TOOLS 恒不含 write_file/run_command",
              "write_file" not in names and "run_command" not in names)
        settings._data["write_tools_enabled"] = True   # 双开,验全名
        tnames = {t["function"]["name"] for t in task_tools()}
        check("任务模式(双开)含 write_file/make_dir/trash_file/run_command",
              {"write_file", "make_dir", "trash_file", "run_command"} <= tnames)

        # ===================== F. exec_log 落盘 =====================
        print("[exec_log]")
        logf = tmp / "exec.jsonl"
        check("exec_log 文件已生成", logf.exists())
        lines = [ln for ln in logf.read_text(encoding="utf-8").splitlines() if ln.strip()]
        check("exec_log 有记录", len(lines) >= 2)
        if lines:
            import json
            rec = json.loads(lines[0])
            check("exec_log 记录形状含 ts/tool/outcome", all(k in rec for k in ("ts", "tool", "outcome")))

    finally:
        sandbox.allowed_roots = orig_roots
        restore()
        lt.stop()
        # 清临时目录(测试产物,不留)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*40}\nexec_probe: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
