"""边界 case 探针:验证两个"逻辑清楚但未破坏性实测"的冷启动/隐私边界。

不起服务、不调 LLM、不下载模型(空记忆 build_index 在 entries 为空时直接返回)。

  边界1:冷启动空记忆 recall —— recall 返 [] + dispatch recall_memory 返"无相关历史对话。",
         不抛不崩(LLM 据此答"没聊过",不编造)。
  边界2:get_activity 的 window_title 开关 —— 关时返回 dict 不含 window_title 键(守"绝不发云端"),
         开时含该键。

用法:.venv/bin/python scripts/boundary_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 让 `from backend.*` 可 import(scripts/ 在 heartbeat/ 下,加父目录到 path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.memory.l3 import EpisodicMemory  # noqa: E402
from backend.memory import settings as settings_mod  # noqa: E402
from backend.llm import tools as tools_mod     # noqa: E402


def case_empty_recall() -> bool:
    """冷启动空记忆:向量索引空 → recall 返 [];dispatch recall_memory 返"无相关历史对话。"。"""
    print("=== 边界1:冷启动空记忆 recall ===")
    with tempfile.TemporaryDirectory() as d:
        em = EpisodicMemory(path=Path(d) / "e.jsonl")   # 空文件(模拟冷启动)
        build_ok = em.build_index()                      # 空文件不下载模型,直接就绪
        print(f"  build_index(空) ok={build_ok}  matrix={getattr(em._matrix, 'shape', None)}")

        hits = em.recall("任何查询内容", top_k=3)
        ok_recall = hits == []
        print(f"  [{'✓' if ok_recall else '✗'}] recall(空) → {hits}(期望 [])")

        obs = tools_mod.dispatch_tool("recall_memory", {"query": "x", "top_k": 3}, {"episodic": em})
        ok_dispatch = obs == "无相关历史对话。"
        print(f"  [{'✓' if ok_dispatch else '✗'}] dispatch recall_memory(空) → {obs!r}")
        # 不把 build_ok 纳入断言:无 numpy/fastembed 环境 build_index 返 False,但 recall 仍正确降级
        return ok_recall and ok_dispatch


def case_window_title_switch() -> bool:
    """settings.tool_include_window_title:关 → get_activity 不含 window_title;开 → 含。守"绝不发云端"。

    (Phase 5+ 后 tools.dispatch 改读 settings.tool_include_window_title 热开关,故 monkeypatch
    settings 单例内存值,而非旧模块常量 TOOL_INCLUDE_WINDOW_TITLE。)
    """
    print("\n=== 边界2:get_activity 的 window_title 开关 ===")
    s = settings_mod.settings
    orig = s._data.get("tool_include_window_title")
    try:
        # 关闭:工具返回的 dict 不应有 window_title 键(隐私边界)
        s._data["tool_include_window_title"] = 0
        off = json.loads(tools_mod.dispatch_tool("get_activity", {}, {}))
        ok_off = "window_title" not in off
        print(f"  [{'✓' if ok_off else '✗'}] 开关=关 → 键 {list(off.keys())}(期望不含 window_title)")

        # 开启:工具返回 window_title 键(即便值为 None 键也在;run_probes 在 macOS 真实跑,值可能 None)
        s._data["tool_include_window_title"] = 1
        on = json.loads(tools_mod.dispatch_tool("get_activity", {}, {}))
        ok_on = "window_title" in on
        print(f"  [{'✓' if ok_on else '✗'}] 开关=开 → 键 {list(on.keys())}(期望含 window_title)")
        return ok_off and ok_on
    finally:
        if orig is None:                       # 复位,防污染同进程后续
            s._data.pop("tool_include_window_title", None)
        else:
            s._data["tool_include_window_title"] = orig


def main():
    all_pass = case_empty_recall() and case_window_title_switch()
    print(f"\n{'✅ 全部通过' if all_pass else '❌ 有失败项,见上'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
