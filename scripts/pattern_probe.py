"""L4 模式沉淀探针:验证 find_triggered(4 类 trigger)+ PatternStore(merge/淘汰)+ extract_patterns + 端到端。

不起服务。A/B/D 不连 LLM(秒级);C 调真 LLM 归纳(有 key 才跑,无 key 跳过)。

用法:.venv/bin/python scripts/pattern_probe.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.memory.l0 import StateCache  # noqa: E402
from backend.memory.l4 import PatternStore  # noqa: E402
from backend.judgment import patterns  # noqa: E402


def snap(app="VSCode", idle=5, title=None, hour=14, weekday=0):
    return {"foreground_app": app, "idle_sec": idle, "window_title": title,
            "hour": hour, "weekday": weekday}


class _FakeStore:
    """find_triggered 只用 store.for_trigger();fake store 直接返给定列表(不过滤,过滤用真 store 测)。"""
    def __init__(self, pats):
        self._pats = pats

    def for_trigger(self):
        return self._pats


def case_find_triggered() -> bool:
    print("=== A: find_triggered(4 类 trigger 纯代码匹配)===")
    all_ok = True

    # 1) time_window 跨午夜命中 [23,2] hour=1
    s = _FakeStore([{"id": "p1", "text": "深夜活跃", "evidence_count": 3,
                     "trigger": {"type": "time_window", "hour_range": [23, 2], "weekdays": None, "app_category": None}}])
    c = patterns.find_triggered(snap(hour=1), StateCache(), s)
    ok = len(c) == 1 and c[0]["category"] == "pattern_match" and c[0]["key"] == "pattern:p1"
    print(f"  [{'✓' if ok else '✗'}] time_window [23,2] hour=1 → 触发 key={c[0]['key'] if c else '-'}")
    all_ok &= ok

    # 2) time_window 范围外不命中
    c = patterns.find_triggered(snap(hour=14), StateCache(), s)
    ok = len(c) == 0
    print(f"  [{'✓' if ok else '✗'}] time_window [23,2] hour=14 → 不触发")
    all_ok &= ok

    # 3) time_window + app_category 约束(VSCode=dev 命中,Spotify=music 不命中)
    s2 = _FakeStore([{"id": "p2", "text": "深夜写代码", "evidence_count": 3,
                      "trigger": {"type": "time_window", "hour_range": [23, 2], "weekdays": None, "app_category": "dev"}}])
    ok = len(patterns.find_triggered(snap(app="VSCode", hour=1), StateCache(), s2)) == 1
    ok &= len(patterns.find_triggered(snap(app="Spotify", hour=1), StateCache(), s2)) == 0
    print(f"  [{'✓' if ok else '✗'}] time_window+app_category=dev:VSCode 触发 / Spotify 不触发")
    all_ok &= ok

    # 4) app_streak 命中(VSCode 91 tick≈12min ≥ 30)/ 不足不命中
    l0 = StateCache()
    for _ in range(91):
        l0.push(snap(app="VSCode", idle=5))
    s3 = _FakeStore([{"id": "p3", "text": "长时间编码", "evidence_count": 3,
                      "trigger": {"type": "app_streak", "app": "VSCode", "min_minutes": 10}}])
    ok = len(patterns.find_triggered(snap(app="VSCode"), l0, s3)) == 1   # 91 tick≈12min≥10
    l0short = StateCache()
    for _ in range(10):
        l0short.push(snap(app="VSCode", idle=5))                          # 10 tick≈1.3min<10
    ok &= len(patterns.find_triggered(snap(app="VSCode"), l0short, s3)) == 0
    print(f"  [{'✓' if ok else '✗'}] app_streak VSCode≥10min(91tick≈12min触发 / 10tick≈1min不触发)")
    all_ok &= ok

    # 5) app_combo 命中(窗口内有 VSCode+Spotify)/ 缺一不命中
    l0c = StateCache()
    for a in ["VSCode", "Spotify", "VSCode", "VSCode"]:
        l0c.push(snap(app=a, idle=5))
    s4 = _FakeStore([{"id": "p4", "text": "边写边听", "evidence_count": 3,
                      "trigger": {"type": "app_combo", "apps": ["VSCode", "Spotify"], "within_minutes": 10}}])
    ok = len(patterns.find_triggered(snap(app="VSCode"), l0c, s4)) == 1
    l0c2 = StateCache()
    for _ in range(3):
        l0c2.push(snap(app="VSCode", idle=5))
    ok &= len(patterns.find_triggered(snap(app="VSCode"), l0c2, s4)) == 0
    print(f"  [{'✓' if ok else '✗'}] app_combo [VSCode,Spotify]:都有触发 / 缺Spotify不触发")
    all_ok &= ok

    # 6) focus_duration 命中(20 tick 连续低 idle≈2.6min ≥ 2)/ idle 中断后不命中
    l0f = StateCache()
    for _ in range(20):
        l0f.push(snap(app="VSCode", idle=5))
    s5 = _FakeStore([{"id": "p5", "text": "连续工作", "evidence_count": 3,
                      "trigger": {"type": "focus_duration", "min_minutes": 2, "exclude_categories": ["leisure"]}}])
    ok = len(patterns.find_triggered(snap(app="VSCode"), l0f, s5)) == 1
    l0f2 = StateCache()
    for _ in range(10):
        l0f2.push(snap(app="VSCode", idle=5))
    l0f2.push(snap(app="VSCode", idle=60))       # idle≥30 中断
    for _ in range(10):
        l0f2.push(snap(app="VSCode", idle=5))
    ok &= len(patterns.find_triggered(snap(app="VSCode"), l0f2, s5)) == 0
    print(f"  [{'✓' if ok else '✗'}] focus_duration:连续低idle触发 / idle中断后不足不触发")
    all_ok &= ok

    # 7) evidence<2 不触发(用真 PatternStore,for_trigger 生效)
    with tempfile.TemporaryDirectory() as d:
        st = PatternStore(path=Path(d) / "p.yaml")
        st.merge({"updates": [{"source_id": None, "text": "低证据",
                               "trigger": {"type": "time_window", "hour_range": [0, 23]}}]})  # evidence=1
        ok = len(patterns.find_triggered(snap(hour=14), StateCache(), st)) == 0
    print(f"  [{'✓' if ok else '✗'}] evidence=1(<MIN_EVIDENCE=2)→ 不触发(只注入)")
    all_ok &= ok

    # 8) trigger=None 不触发(即使 evidence 高)
    with tempfile.TemporaryDirectory() as d:
        st = PatternStore(path=Path(d) / "p.yaml")
        st.merge({"updates": [{"source_id": None, "text": "纯描述", "trigger": None}]})
        pid = st._patterns[0]["id"]
        st.merge({"updates": [{"source_id": pid, "text": "纯描述", "trigger": None}]})  # evidence=2
        ok = len(patterns.find_triggered(snap(hour=14), StateCache(), st)) == 0
    print(f"  [{'✓' if ok else '✗'}] trigger=None(evidence=2)→ 不触发(只注入)")
    all_ok &= ok

    return all_ok


def case_pattern_store() -> bool:
    print("\n=== B: PatternStore merge / 淘汰 / 持久化 ===")
    all_ok = True

    with tempfile.TemporaryDirectory() as d:
        st = PatternStore(path=Path(d) / "p.yaml")

        # 1) 新增
        st.merge({"updates": [{"source_id": None, "text": "深夜写代码听音乐",
                               "trigger": {"type": "time_window", "hour_range": [23, 2]}}]})
        ok = len(st) == 1 and st._patterns[0]["evidence_count"] == 1
        print(f"  [{'✓' if ok else '✗'}] 新增 → {len(st)}条 evidence={st._patterns[0]['evidence_count']}")
        all_ok &= ok
        pid = st._patterns[0]["id"]

        # 2) source_id 复用 → 累积
        st.merge({"updates": [{"source_id": pid, "text": "深夜写代码听音乐",
                               "trigger": {"type": "time_window", "hour_range": [23, 2]}}]})
        ok = len(st) == 1 and st._patterns[0]["evidence_count"] == 2
        print(f"  [{'✓' if ok else '✗'}] source_id 复用 → evidence={st._patterns[0]['evidence_count']} 不新建")
        all_ok &= ok

        # 3) text 包含兜底(旧⊆新)→ 累积不新建
        st.merge({"updates": [{"source_id": None, "text": "用户深夜写代码听音乐", "trigger": None}]})
        ok = len(st) == 1 and st._patterns[0]["evidence_count"] == 3
        print(f"  [{'✓' if ok else '✗'}] text 包含兜底(旧⊆新)→ evidence={st._patterns[0]['evidence_count']} trigger 覆盖为 None")
        all_ok &= ok

        # 4) 全新模式新增
        st.merge({"updates": [{"source_id": None, "text": "周一早上开会",
                               "trigger": {"type": "time_window", "hour_range": [9, 10], "weekdays": [0]}}]})
        ok = len(st) == 2
        print(f"  [{'✓' if ok else '✗'}] 全新模式 → {len(st)}条")
        all_ok &= ok

        # 5) dropped_ids 淘汰
        drop_id = st._patterns[1]["id"]
        st.merge({"updates": [], "dropped_ids": [drop_id]})
        ok = len(st) == 1
        print(f"  [{'✓' if ok else '✗'}] dropped_ids 淘汰 → {len(st)}条")
        all_ok &= ok

        # 6) 落盘后 reload
        st2 = PatternStore(path=Path(d) / "p.yaml")
        ok = len(st2) == 1 and st2._patterns[0]["evidence_count"] == 3
        print(f"  [{'✓' if ok else '✗'}] 落盘 reload → {len(st2)}条 evidence={st2._patterns[0]['evidence_count']}")
        all_ok &= ok

        # 7) to_prompt_block 非空
        block = st.to_prompt_block()
        ok = "深夜写代码" in block and block.startswith("- ")
        print(f"  [{'✓' if ok else '✗'}] to_prompt_block → {block[:30]}...")
        all_ok &= ok

    # 8) 淘汰:低证据 + 超期 → 删
    with tempfile.TemporaryDirectory() as d:
        st = PatternStore(path=Path(d) / "p.yaml")
        old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")  # 20天 > 14天
        st._patterns = [{"id": "old1", "text": "过时低证据",
                         "trigger": {"type": "time_window", "hour_range": [0, 23]},
                         "evidence_count": 1, "first_seen": old, "last_seen": old, "last_fired": None}]
        # merge 一个新模式触发流程(空 merge 会早返回不淘汰);旧超期模式应被 _prune_stale 删
        st.merge({"updates": [{"source_id": None, "text": "新模式", "trigger": None}]})
        ok = len(st) == 1 and st._patterns[0]["text"] == "新模式"
        print(f"  [{'✓' if ok else '✗'}] 淘汰:低证据(evidence=1)+20天未见 → 旧删新留 剩{len(st)}条")
        all_ok &= ok

    return all_ok


def case_extract_patterns() -> bool:
    print("\n=== C: extract_patterns(真 LLM 归纳,有 key 才跑)===")
    from backend.llm.gateway import _get_client
    if _get_client() is None:
        print("  ⏭ 无 LLM key,跳过(逻辑层已由 A/B/D 覆盖)")
        return True
    import asyncio
    from backend.llm.gateway import extract_patterns
    # 构造 8 段有规律的 L3 entries(深夜 + 编码 + 音乐 反复)
    entries = []
    for i, (app, u, a) in enumerate([
        ("VSCode", "又在熬夜调这个异步 bug", "嗯,慢慢来"),
        ("Spotify", "深夜写代码必听点音乐", "嗯"),
        ("VSCode", "这个 bug 还是没搞定", "歇会儿吧"),
        ("Spotify", "换个歌单继续", "嗯"),
        ("VSCode", "终于跑通了", "辛苦了"),
        ("Spotify", "庆祝一下放首歌", "嗯"),
        ("VSCode", "又开始写新功能了", "加油"),
        ("Spotify", "继续听音乐写代码", "嗯"),
    ]):
        entries.append({"ts": f"2026-07-{20+i}T23:30", "persona": "zorya",
                        "trigger": {"category": "deep_night_working" if app == "VSCode" else "music_app_opened"},
                        "foreground_app": app,
                        "messages": [{"role": "user", "text": u}, {"role": "agent", "text": a}]})
    result = asyncio.run(extract_patterns(entries, [], "zorya"))
    ups = result.get("updates", [])
    print(f"  归纳出 {len(ups)} 条模式,dropped={result.get('dropped_ids', [])}")
    for u in ups:
        print(f"    - {u.get('text')} | trigger={u.get('trigger')}")
    ok = len(ups) >= 1
    print(f"  [{'✓' if ok else '✗'}] 至少归纳出 1 条模式(如深夜写代码/听音乐)")
    return ok


def case_end_to_end() -> bool:
    print("\n=== D: 端到端(merge→注入→触发 链路)===")
    all_ok = True
    with tempfile.TemporaryDirectory() as d:
        st = PatternStore(path=Path(d) / "p.yaml")
        # 模拟 extract_patterns 三次确认同一条(深夜写代码,trigger time_window)
        for _ in range(3):
            sid = st._patterns[0]["id"] if st._patterns else None
            st.merge({"updates": [{"source_id": sid, "text": "深夜常写代码",
                                   "trigger": {"type": "time_window", "hour_range": [23, 2], "app_category": "dev"}}]})
        ok = len(st) == 1 and st._patterns[0]["evidence_count"] == 3
        print(f"  [{'✓' if ok else '✗'}] 3 次确认 → 1条 evidence={st._patterns[0]['evidence_count']}")
        all_ok &= ok

        block = st.to_prompt_block()
        ok = "深夜常写代码" in block
        print(f"  [{'✓' if ok else '✗'}] 注入 to_prompt_block: {block}")
        all_ok &= ok

        l0 = StateCache()
        trig = patterns.find_triggered(snap(app="VSCode", hour=1), l0, st)
        no = patterns.find_triggered(snap(app="VSCode", hour=14), l0, st)
        ok = len(trig) == 1 and len(no) == 0
        print(f"  [{'✓' if ok else '✗'}] 触发:深夜1点+VSCode→{[x['detail'] for x in trig]} / 白天14点→不触发")
        all_ok &= ok
    return all_ok


def main():
    a = case_find_triggered()
    b = case_pattern_store()
    c = case_extract_patterns()
    d = case_end_to_end()
    all_pass = a and b and c and d
    print(f"\n{'✅ 全部通过' if all_pass else '❌ 有失败项,见上'}")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
