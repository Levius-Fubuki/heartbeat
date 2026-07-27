"""验证 L3 向量召回 + 老 entry 迁移补 id。

不起服务,直接构造 EpisodicMemory(tmp 路径),插几条 entry,build_index,recall。
首次运行会下载 bge-small-zh ONNX ~130MB 到 ~/.cache/fastembed(之后秒级)。

用法:.venv/bin/python scripts/embed_probe.py
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


def _mk(app, user_text):
    return EpisodicMemory.build_entry(
        persona_id="zorya", trigger={"rule": "test"}, foreground_app=app,
        messages=[{"role": "user", "text": user_text}])


def main():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "e.jsonl"
        em = EpisodicMemory(path=path)
        em.append(_mk("Spotify", "我在听古典音乐放松"))
        em.append(_mk("VSCode", "这个 Python bug 调了一下午,头疼"))
        em.append(_mk("Slack", "老板又催项目进度,压力大"))

        print("=== build_index(首次会下载模型 ~130MB)===")
        ok = em.build_index()
        print(f"build_index ok={ok}  entries={len(em._ids)}  matrix={getattr(em._matrix, 'shape', None)}")

        print("\n=== recall 测试(期望语义命中)===")
        cases = [("音乐放松", 0), ("工作压力 bug", 1), ("老板催进度", 2)]
        all_pass = True
        for q, expect_idx in cases:
            hits = em.recall(q, top_k=1)
            if not hits:
                print(f"  ✗ q={q!r:18} → (无命中)")
                all_pass = False
                continue
            # 用文本前缀判断命中哪条
            txt = hits[0]["messages"][0]["text"]
            tag = "音乐" if "音乐" in txt else ("bug" if "bug" in txt else ("老板" if "老板" in txt else "?"))
            ok_hit = (tag == ["音乐", "bug", "老板"][expect_idx])
            all_pass = all_pass and ok_hit
            print(f"  {'✓' if ok_hit else '✗'} q={q!r:18} → {tag}: {txt[:24]}")

        print("\n=== 迁移测试:写一条无 id 老 entry,重建索引 ===")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": "2025-01-01T00:00:00", "persona": "zorya",
                "trigger": {}, "foreground_app": "Book",
                "messages": [{"role": "user", "text": "旧的没有 id 的记忆条目"}],
            }, ensure_ascii=False) + "\n")
        em2 = EpisodicMemory(path=path)
        em2.build_index()
        all_entries = em2._read_all()
        all_have_id = bool(all_entries) and all(e.get("id") for e in all_entries)
        print(f"  迁移后 entry 数={len(all_entries)}  全部有 id={all_have_id}")
        all_pass = all_pass and all_have_id and len(all_entries) == 4

        print("\n=== recall 跨迁移(查旧记忆)===")
        hits = em2.recall("以前的旧记忆", top_k=2)
        for h in hits:
            print(f"  → {h['messages'][0]['text'][:30]}")
        all_pass = all_pass and len(hits) >= 1

        print(f"\n{'✅ 全部通过' if all_pass else '❌ 有失败项,见上'}")
        sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
