"""任务模式分流器验证(Phase 6a):纯函数 + 总开关 monkeypatch,无需起服务。

跑:.venv/bin/python scripts/router_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.judgment.router import classify_task  # noqa: E402

# (text, has_attachment, expected_task, 说明)
CASES = [
    ("帮我分析这段代码", False, True, "任务词「分析」「这段代码」→ 任务"),
    ("在听歌", False, False, "纯闲聊 → 陪伴"),
    ("你在干嘛", False, False, "陪伴招牌问句 → 陪伴"),
    ("今天天气不错", False, False, "闲聊 → 陪伴"),
    ("帮我看看这个 bug", False, True, "任务词「bug」→ 任务"),
    ("", True, True, "带附件空文本 → 任务"),
    ("随便聊聊", True, True, "带附件 → 任务(即使文本是闲聊)"),
    ("!随便聊聊", False, True, "「!」强制 → 任务"),
    ("  !  分析下", False, True, "前导空白 + ! → 任务"),
    ("帮我听歌", False, False, "「帮我听歌」不含任务词 → 陪伴(关键不误判)"),
    ("帮我查一下这个库怎么用", False, True, "任务词「查一下」→ 任务"),
    ("解释一下这段代码在做什么", False, True, "任务词「解释」→ 任务"),
    ("加油", False, False, "短闲聊 → 陪伴"),
    ("a" * 61, False, True, "超 60 字符 → 任务(长诉求)"),
    ("a" * 60, False, False, "恰好 60 字符(不 > 60)→ 陪伴"),
    ("debug 这个报错", False, True, "任务词「debug」「报错」→ 任务"),
    ("晚上好", False, False, "问候闲聊 → 陪伴"),
    ("帮我总结一下今天的对话", False, True, "任务词「总结」→ 任务"),
    ("对比一下这两个方案", False, True, "任务词「对比」→ 任务"),
    ("哈哈好的", False, False, "短回应闲聊 → 陪伴"),
    # Phase 6d 写入 / 执行意图(不带 ! 也应自动进任务模式,工具才可见)
    ("在桌面写个 hello.txt", False, True, "写意图「写个」→ 任务"),
    ("新建一个文件夹叫 test", False, True, "写意图「新建一个」→ 任务"),
    ("把这段保存到文件里", False, True, "写意图「保存到文件」→ 任务"),
    ("删掉桌面那个临时文件", False, True, "删除意图「删掉」→ 任务"),
    ("跑一下 echo hi", False, True, "执行意图「跑一下」→ 任务"),
    ("用命令行运行 python3 main.py", False, True, "执行意图「命令行」→ 任务"),
    ("执行这个脚本", False, True, "执行意图「执行」→ 任务"),
    ("运行命令 make build", False, True, "执行意图「运行命令」→ 任务"),
    # 误伤(已接受:任务模式只是更长回复 + 工具可见,无害)
    ("写个故事给我听", False, True, "误伤「写个」→ 任务(接受;写作本就是任务)"),
    ("我去跑一下步", False, True, "误伤「跑一下」→ 任务(接受;罕见,影响小)"),
    # 不误伤:不含任何写/执行/任务词的闲聊仍陪伴
    ("在写代码呢", False, False, "「在写代码」无写文件/执行意图词 → 陪伴"),
    ("写到一半了", False, False, "「写到一半」无关键词 → 陪伴"),
    ("我去跑个步", False, False, "「跑个步」无「跑个脚本」→ 陪伴(不误判)"),
]


def test_switch_off() -> tuple:
    """总开关 task_mode_enabled=False 时一律陪伴(即使命中所有任务信号)。"""
    import backend.memory.settings as sm
    saved = sm.settings._data.get("task_mode_enabled")
    sm.settings._data["task_mode_enabled"] = False
    try:
        cases = [
            ("帮我分析这段代码", True),    # 命中任务词 + ... 但开关关
            ("!" + "a" * 100, True),       # 强制 + 超长,但开关关
            ("whatever", False),           # 闲聊,开关关仍陪伴
        ]
        results = [(t, a, classify_task(t, a)) for t, a in cases]
        ok = all(r is False for _, _, r in results)
    finally:
        sm.settings._data["task_mode_enabled"] = saved
    return ok, results


def main() -> int:
    print("[router_probe] 分流器 case 验证\n", flush=True)
    passed = 0
    for text, has_attach, expected, desc in CASES:
        got = classify_task(text, has_attach)
        ok = got is expected
        mark = "✓" if ok else "✗"
        tag = "任务" if got else "陪伴"
        shown = text if len(text) <= 30 else text[:27] + "..."
        print(f"  {mark} [{tag}] {shown!r:34.34} attach={str(has_attach):5} {desc}", flush=True)
        if ok:
            passed += 1
        else:
            print(f"        期望 {'任务' if expected else '陪伴'},实际 {'任务' if got else '陪伴'}", flush=True)
    print(f"\n[router_probe] 分流 case {passed}/{len(CASES)} 通过", flush=True)

    print("\n[router_probe] 总开关 task_mode_enabled=False 验证", flush=True)
    off_ok, off_results = test_switch_off()
    for t, a, r in off_results:
        shown = t if len(t) <= 30 else t[:27] + "..."
        mark = "✓" if r is False else "✗"
        print(f"  {mark} [陪伴] {shown!r:34.34} attach={a} (开关关,应一律陪伴)", flush=True)
    print(f"[router_probe] 总开关 {'通过' if off_ok else '失败'}", flush=True)

    all_ok = passed == len(CASES) and off_ok
    print(f"\n[router_probe] 总计 {'全过 ✓' if all_ok else '有失败 ✗'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
