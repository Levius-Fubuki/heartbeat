"""规则探针:构造快照序列验证 find_candidates 产出各类 candidate。不连后端、不调 LLM。

模拟 _tick 的真实顺序(push → previous → find_candidates),覆盖 9 类规则 + CooldownTracker 切档。

用法:.venv/bin/python scripts/rule_probe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.memory.l0 import StateCache
from backend.judgment import rules


def snap(app="Warp", idle=10, title=None, hour=14, deep=False, early=False):
    return {
        "foreground_app": app, "foreground_bundle": None,
        "idle_sec": idle, "window_title": title,
        "hour": hour, "weekday": 0,
        "is_deep_night": deep, "is_early_morning": early,
    }


def run(name, seed, seq, expect=None):
    """push seed → 逐个 push seq;对最后一个 curr 调 find_candidates,核对类别。

    seed:进入场景前的快照(会先 push,作为 previous 的来源)
    seq:本次要判定的快照序列(逐个 push,模拟连续 tick)
    expect:期望出现的 category 集合(None=只打印不核对)
    """
    l0 = StateCache()
    l0.push(seed)
    last_cands = []
    for c in seq:
        l0.push(c)                                       # 先入 L0(与 _tick 一致)
        prev = l0.previous
        last_cands = rules.find_candidates(prev, c, l0)
    cats = sorted({c["category"] for c in last_cands})
    ok = expect is None or set(cats) == set(expect)
    flag = "✓" if ok else "✗"
    print(f"[{flag}] {name}: {cats}")
    for c in last_cands:
        print(f"      p{c['priority']} {c['category']}: {c['detail']}")
    return ok


def main():
    print("=== 9 类规则 ===\n")
    all_ok = True
    all_ok &= run("打开 VSCode → dev_app_opened", snap("Spotify"), [snap("VSCode")], ["dev_app_opened"])
    all_ok &= run("打开 Spotify → music_app_opened", snap("Warp"), [snap("Spotify")], ["music_app_opened"])
    all_ok &= run("打开 B站 → leisure_app_opened", snap("Warp"), [snap("B站")], ["leisure_app_opened"])
    all_ok &= run("Chrome 看 YouTube → leisure(标题细分)",
                  snap("Warp"), [snap("Chrome", title="cat video - YouTube")], ["leisure_app_opened"])
    all_ok &= run("Chrome 看 GitHub → dev(标题细分)",
                  snap("Warp"), [snap("Chrome", title="repo / src / main.py - GitHub")], ["dev_app_opened"])
    all_ok &= run("深夜 VSCode → deep_night_working + dev_app_opened",
                  snap("Spotify"), [snap("VSCode", idle=10, hour=2, deep=True)],
                  ["deep_night_working", "dev_app_opened"])
    all_ok &= run("早晨刚坐下 → morning_greet",
                  snap("Warp", idle=200), [snap("Warp", idle=30, hour=7, early=True)], ["morning_greet"])

    # streak 型:连续 ≥ FOCUS_TICKS(90) 个同 App 低 idle
    all_ok &= run("专注 12+ 分钟 → long_focus",
                  snap("Spotify"), [snap("VSCode", idle=5)] * 91, ["long_focus"])
    all_ok &= run("B站 15+ 分钟 → leisure_too_long",
                  snap("Warp"), [snap("B站", idle=5)] * 113, ["leisure_too_long"])

    # 频繁切换:最近 15 tick 内 ≥5 个不同 App
    all_ok &= run("频繁切 6 个 App → distracted",
                  snap("X"), [snap(a) for a in ["A", "B", "C", "D", "E", "F"]], ["distracted"])

    # 无候选
    all_ok &= run("中性 App 平静 → 无候选", snap("Warp"), [snap("Finder")], [])

    print("\n=== 冷启动(prev=None,不误触发 app_switched)===")
    # 模拟首个 tick:push 一帧后 previous=None → find_candidates(None, curr, l0)。
    # 冷启动时当前前台 App 不应被当成"刚打开"(否则每次启动必搭话)。
    l0c = StateCache(); cs = snap("VSCode"); l0c.push(cs)
    cold = sorted({c["category"] for c in rules.find_candidates(None, cs, l0c)})
    ok0 = cold == []
    print(f"[{'✓' if ok0 else '✗'}] 冷启动首帧 dev(VSCode) → {cold or '(无候选,正确)'}")
    all_ok &= ok0
    # 时段类不依赖 prev,冷启动照常(只禁 app_switched,精确不误伤)
    l0c2 = StateCache(); cs2 = snap("VSCode", idle=10, hour=2, deep=True); l0c2.push(cs2)
    cold2 = sorted({c["category"] for c in rules.find_candidates(None, cs2, l0c2)})
    ok1 = cold2 == ["deep_night_working"]
    print(f"[{'✓' if ok1 else '✗'}] 冷启动+深夜 dev → {cold2}(时段类照常,app_switched 被禁)")
    all_ok &= ok1

    print("\n=== CooldownTracker 切档 ===")
    t = rules.CooldownTracker("balanced")
    print(f"balanced  dev_app_opened 冷却 = {t._cooldown_for('dev_app_opened')}s")
    t.set_mode("active")
    print(f"active    dev_app_opened 冷却 = {t._cooldown_for('dev_app_opened')}s")
    t.set_mode("conservative")
    print(f"conserv.  dev_app_opened 冷却 = {t._cooldown_for('dev_app_opened')}s")

    cands = [{"key": "app:dev:VSCode", "category": "dev_app_opened", "priority": 3, "detail": "x"}]
    fired1 = t.take(cands)
    fired2 = t.take(cands)
    print(f"\n第一次 take(应触发): {[c['category'] for c in fired1]}")
    print(f"第二次 take(应空,冷却中): {[c['category'] for c in fired2]}")
    ok_cd = bool(fired1) and not fired2
    print(f"[{'✓' if ok_cd else '✗'}] 冷却生效")
    all_ok &= ok_cd

    print(f"\n=== {'全部通过 ✓' if all_ok else '有失败 ✗,见上方 ✗ 行'} ===")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
