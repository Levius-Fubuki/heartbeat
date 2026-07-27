#!/usr/bin/env python3
"""Phase 6b 只读工具 probe —— sandbox 白名单 + dispatch 门 + task_tools() 构造器。

确定性、不联网(web_search 用 monkeypatch 免真请求)。用临时目录挂进 allowed_dirs 做白名单
样本,不动用户真实的 桌面/文档/下载。run:`.venv/bin/python scripts/tools6b_probe.py`
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.judgment import sandbox                          # noqa: E402
from backend.llm import tools as tools_mod                    # noqa: E402
from backend.llm import web as web_mod                        # noqa: E402
from backend.llm.tools import TOOLS, dispatch_tool, task_tools  # noqa: E402
from backend.llm.tools import _NOTE_TOOLS as NOTE_TOOLS  # noqa: E402  (Phase 6c:任务模式额外 10 个第二大脑工具,无条件挂)
from backend.memory.settings import settings                  # noqa: E402

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
    for k in ("file_tools_enabled", "web_search_enabled", "tavily_key", "allowed_dirs"):
        _ORIG[k] = settings._data.get(k)


def restore():
    for k, v in _ORIG.items():
        settings._data[k] = v


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb6b_"))
    try:
        (tmp / "sub").mkdir()
        (tmp / "sub" / "a.txt").write_text("hello world", encoding="utf-8")
        (tmp / "b.txt").write_text("root file", encoding="utf-8")
        (tmp / "big.txt").write_text("x" * 9000, encoding="utf-8")
        link = tmp / "escape"
        try:
            os.symlink("/etc", str(link))
            have_symlink = True
        except OSError:  # noqa: BLE001
            have_symlink = False

        snap()
        settings._data["allowed_dirs"] = [str(tmp)]     # 把临时树挂成允许目录
        settings._data["file_tools_enabled"] = True
        settings._data["web_search_enabled"] = True
        settings._data["tavily_key"] = ""               # 默认无 key(测文件工具 + web「未配置」)

        print("[sandbox is_allowed]")
        check("allowed root 本身", sandbox.is_allowed(str(tmp)))
        check("根下文件", sandbox.is_allowed(str(tmp / "sub" / "a.txt")))
        check("根外拒绝", not sandbox.is_allowed("/etc/passwd"))
        check("空路径拒绝", not sandbox.is_allowed(""))
        check(".. 穿越拒绝", not sandbox.is_allowed(str(tmp / ".." / ".." / "etc" / "passwd")))
        if have_symlink:
            check("符号链接逃逸拒绝", not sandbox.is_allowed(str(link / "passwd")))
            check("符号链接自身拒绝", not sandbox.is_allowed(str(link)))

        print("[dispatch: read_file]")
        check("读文本全文", dispatch_tool("read_file", {"path": str(tmp / "sub" / "a.txt")}, {}) == "hello world")
        rb = dispatch_tool("read_file", {"path": str(tmp / "big.txt")}, {})
        check("超长截断+注明", rb.startswith("x" * 8000) and "已截断" in rb)
        check("越界拒绝", "无权访问" in dispatch_tool("read_file", {"path": "/etc/passwd"}, {}))
        settings._data["file_tools_enabled"] = False
        check("开关关→禁用", "禁用" in dispatch_tool("read_file", {"path": str(tmp / "b.txt")}, {}))
        settings._data["file_tools_enabled"] = True

        print("[dispatch: read_file · ~ 路径展开]")
        home_dot = Path.home() / ".hb6b_probe_tmp"
        home_dot.mkdir(exist_ok=True)
        (home_dot / "x.txt").write_text("tilde-ok", encoding="utf-8")
        saved_dirs = settings._data.get("allowed_dirs")
        settings._data["allowed_dirs"] = [str(home_dot)]   # 临时把家目录 dot-dir 挂成允许根
        try:
            check("read_file 展开 ~ 路径",
                  dispatch_tool("read_file", {"path": "~/.hb6b_probe_tmp/x.txt"}, {}) == "tilde-ok")
        finally:
            settings._data["allowed_dirs"] = saved_dirs
            shutil.rmtree(home_dot, ignore_errors=True)

        print("[dispatch: list_dir]")
        ld = dispatch_tool("list_dir", {"path": str(tmp)}, {})
        check("列出子目录", "sub/" in ld)
        check("列出文件", "b.txt" in ld)
        check("越界拒绝", "无权访问" in dispatch_tool("list_dir", {"path": "/etc"}, {}))
        settings._data["file_tools_enabled"] = False
        check("开关关→禁用", "禁用" in dispatch_tool("list_dir", {"path": str(tmp)}, {}))
        settings._data["file_tools_enabled"] = True

        print("[dispatch: glob_files]")
        gl = dispatch_tool("glob_files", {"pattern": "*.txt", "path": str(tmp)}, {})
        check("通配命中(顶层)", "b.txt" in gl and "big.txt" in gl)
        glr = dispatch_tool("glob_files", {"pattern": "**/*.txt", "path": str(tmp)}, {})
        check("递归通配命中子目录", "sub/a.txt" in glr)
        check("无命中提示", "没有匹配" in dispatch_tool("glob_files", {"pattern": "*.nosuch", "path": str(tmp)}, {}))
        check("越界拒绝", "无权访问" in dispatch_tool("glob_files", {"pattern": "*", "path": "/etc"}, {}))
        settings._data["file_tools_enabled"] = False
        check("开关关→禁用", "禁用" in dispatch_tool("glob_files", {"pattern": "*", "path": str(tmp)}, {}))
        settings._data["file_tools_enabled"] = True

        print("[dispatch: web_search 门]")
        check("无 key→未配置", "未配置" in dispatch_tool("web_search", {"query": "x"}, {}))
        settings._data["tavily_key"] = "fake-key"
        settings._data["web_search_enabled"] = False
        check("开关关→禁用", "禁用" in dispatch_tool("web_search", {"query": "x"}, {}))
        settings._data["web_search_enabled"] = True
        seen = {}

        def fake_tavily(query, top_k=5):
            seen["q"] = query
            seen["k"] = top_k
            return "[1] 标题\nhttps://x\n摘要"

        orig_ts = web_mod.tavily_search
        web_mod.tavily_search = fake_tavily           # dispatch 内 from backend.llm.web import 读到此属性
        try:
            out = dispatch_tool("web_search", {"query": "rust async", "top_k": 3}, {})
            check("mock 拼接+透参", out == "[1] 标题\nhttps://x\n摘要" and seen == {"q": "rust async", "k": 3})
        finally:
            web_mod.tavily_search = orig_ts

        print("[task_tools() 构造器]")
        n_note = len(NOTE_TOOLS)                  # Phase 6c 第二大脑工具数(=10),任务模式无条件挂
        settings._data["file_tools_enabled"] = True
        settings._data["web_search_enabled"] = True
        settings._data["tavily_key"] = "k"
        n7 = [t["function"]["name"] for t in task_tools()]
        check(f"全开={7 + n_note}", len(n7) == 7 + n_note)
        check("含 read_file/web_search", "read_file" in n7 and "web_search" in n7)
        check("含第二大脑工具", "add_note" in n7 and "set_reminder" in n7)
        settings._data["file_tools_enabled"] = False
        n4 = [t["function"]["name"] for t in task_tools()]
        check(f"文件关={4 + n_note}(3+web+笔记)", len(n4) == 4 + n_note and "read_file" not in n4 and "web_search" in n4)
        settings._data["file_tools_enabled"] = True
        settings._data["web_search_enabled"] = False
        n6 = [t["function"]["name"] for t in task_tools()]
        check(f"web 关={6 + n_note}(无 web)", len(n6) == 6 + n_note and "web_search" not in n6)
        settings._data["web_search_enabled"] = True
        settings._data["tavily_key"] = ""
        n6b = [t["function"]["name"] for t in task_tools()]
        check(f"无 key={6 + n_note}(无 web)", len(n6b) == 6 + n_note and "web_search" not in n6b)
        check("陪伴 TOOLS 恒 3(不含第二大脑)", len(TOOLS) == 3 and "add_note" not in [t["function"]["name"] for t in TOOLS])

    finally:
        restore()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 40}\nPhase 6b probe: {PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
