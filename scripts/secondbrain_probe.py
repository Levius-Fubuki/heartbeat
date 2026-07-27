#!/usr/bin/env python3
"""Phase 6c 第二大脑 probe —— notes/todos/reminders 三 store + 9 工具 dispatch + 提醒 firing 端到端。

确定性、不联网、不起服务。三 store + Heartbeat 全走临时路径,**绝不碰真实 memory/**。
gateway.react_heartbeat 桩成确定串(快速 + 离线 + 可断言)。假 WS 客户端录广播。

  A. NotesStore:add/list(筛)/delete/cap/to_prompt_block
  B. TodosStore:add/list/toggle/delete/open_todos/cap/to_prompt_block
  C. RemindersStore:add/list/due/due_soon/mark_fired[持久化]/cancel/to_prompt_block/cap
  D. dispatch 9 工具(fake deps)+ set_reminder fire_at 三路(in_minutes / at / 默认)+ _parse_clock_at
  E. _second_brain_block:空→"";有内容含「待办/即将到期提醒/近期笔记」小标题
  F. _check_reminders 端到端:到期提醒 → 合成 candidate 含文本 → 流式 chat_start/chunk/end → mark_fired 持久化

用法:.venv/bin/python scripts/secondbrain_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.memory.secondbrain import NotesStore, RemindersStore, TodosStore  # noqa: E402
import backend.memory.secondbrain as sb  # noqa: E402  (改常量要改本模块绑定,from-import 是副本)
from backend.memory.l2 import Profile  # noqa: E402
from backend.memory.l3 import EpisodicMemory  # noqa: E402
from backend.llm import gateway  # noqa: E402
from backend.llm.tools import _parse_clock_at, dispatch_tool  # noqa: E402
from backend.ws import Hub  # noqa: E402
from backend.heartbeat import Heartbeat  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def _stores(d, tag):
    return (NotesStore(path=Path(d) / f"notes_{tag}.yaml"),
            TodosStore(path=Path(d) / f"todos_{tag}.yaml"),
            RemindersStore(path=Path(d) / f"reminders_{tag}.yaml"))


class FakeClient:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _new_hb(d, hub, tag="h"):
    """造一个全 store 走临时路径的 Heartbeat(不碰真实 memory/)。"""
    hb = Heartbeat(hub)
    hb.profile = Profile(path=Path(d) / f"profile_{tag}.yaml")
    hb.episodic = EpisodicMemory(path=Path(d) / f"e_{tag}.jsonl")
    hb.notes = NotesStore(path=Path(d) / f"notes_{tag}.yaml")
    hb.todos = TodosStore(path=Path(d) / f"todos_{tag}.yaml")
    hb.reminders = RemindersStore(path=Path(d) / f"reminders_{tag}.yaml")
    hb.persona_id = "zorya"
    hb.histories = {hb.persona_id: []}
    hb._refresh_episodic_block()
    return hb


async def main():
    global PASS, FAIL
    d = tempfile.mkdtemp(prefix="hb6c_")
    try:
        # ============================ A. NotesStore ============================
        print("[A NotesStore]")
        notes, _, _ = _stores(d, "A")
        nid = notes.add("买猫粮", ["生活", "猫"])
        check("add 返 id", bool(nid))
        notes.add("读论文 XYZ", ["工作"])
        notes.add("空标签自动过滤", ["", "  ", "ok"])
        check("空标签过滤", notes.list()[-1]["tags"] == ["ok"])
        check("list 全部=3", len(notes.list()) == 3)
        check("list query 命中 text", len(notes.list("猫")) == 1)
        check("list query 命中 tag", len(notes.list("工作")) == 1)
        check("list query 大小写不敏感", len(notes.list("XYZ")) == 1)
        check("delete 命中", notes.delete(nid) and len(notes.list()) == 2)
        check("delete 未命中", not notes.delete("nope"))
        block = notes.to_prompt_block()
        check("to_prompt_block 含内容无标题", "读论文" in block and "[行为模式]" not in block and "[待办与笔记]" not in block)
        empty = NotesStore(path=Path(d) / "notes_empty_A.yaml")
        check("空 store to_prompt_block → ''", empty.to_prompt_block() == "")
        # add 空文本 → ""
        check("add 空文本 → ''", notes.add("   ") == "")

        # cap
        small = NotesStore(path=Path(d) / "notes_cap_A.yaml")
        orig = sb.NOTES_MAX
        sb.NOTES_MAX = 3
        try:
            for i in range(5):
                small.add(f"n{i}")
            check("cap 超限裁剪到 MAX", len(small) == 3)
            data = small.list()
            check("cap 留最新", {n["text"] for n in data} == {"n2", "n3", "n4"})
        finally:
            sb.NOTES_MAX = orig

        # ============================ B. TodosStore ============================
        print("[B TodosStore]")
        _, todos, _ = _stores(d, "B")
        t1 = todos.add("写报告")
        t2 = todos.add("回邮件")
        check("list=2", len(todos.list()) == 2)
        check("open_todos 全未完成", len(todos.open_todos()) == 2)
        check("toggle 翻转", todos.toggle(t1) and todos.list()[0]["done"] in (True, False))
        # 找到 t1 的状态
        t1_done = next(t["done"] for t in todos.list() if t["id"] == t1)
        check("toggle 后 t1 done=True", t1_done is True)
        check("open_todos 排除已完成", len(todos.open_todos()) == 1)
        check("toggle 再翻回", todos.toggle(t1) and not next(t["done"] for t in todos.list() if t["id"] == t1))
        check("toggle 未命中", not todos.toggle("nope"))
        check("delete 命中", todos.delete(t2) and len(todos.list()) == 1)
        check("delete 未命中", not todos.delete("nope"))
        tb = todos.to_prompt_block()
        check("to_prompt_block 仅未完成+☐", "☐" in tb and "写报告" in tb and "回邮件" not in tb)
        check("空 to_prompt_block → ''", TodosStore(path=Path(d) / "todos_empty_B.yaml").to_prompt_block() == "")

        # cap(优先删已完成 + 最旧)
        tc = TodosStore(path=Path(d) / "todos_cap_B.yaml")
        orig = sb.TODOS_MAX
        sb.TODOS_MAX = 2
        try:
            a = tc.add("done-old"); tc.toggle(a)
            b = tc.add("done-newer"); tc.toggle(b)
            c = tc.add("open1")
            e = tc.add("open2")            # 超限触发裁剪
            data = tc.list()
            check("cap 裁到 MAX=2", len(data) == 2)
            check("cap 优先留未完成", all(not t["done"] for t in data))
        finally:
            sb.TODOS_MAX = orig

        # ============================ C. RemindersStore ============================
        print("[C RemindersStore]")
        _, _, rem = _stores(d, "C")
        now = time.time()
        r_past = rem.add("过去的", now - 10)         # 已到期
        r_future = rem.add("将来", now + 3600)       # 1h 后
        check("add 返 id", bool(r_past) and bool(r_future))
        check("add 空文本 → ''", rem.add("   ", now) == "")
        check("add 非法 fire_at → ''", rem.add("x", "not-a-number") == "")
        check("list=2", len(rem.list()) == 2)
        due = rem.due()
        check("due 只返已到期未触发", len(due) == 1 and due[0]["id"] == r_past)
        soon = rem.due_soon(7200)                    # 2h 窗口
        check("due_soon 含将来那条", len(soon) == 1 and soon[0]["id"] == r_future)
        check("due_soon 排除已到期", all(r["id"] != r_past for r in soon))
        # mark_fired 持久化
        check("mark_fired 命中", rem.mark_fired(r_past))
        check("mark_fired 后 due 空", rem.due() == [])
        check("mark_fired 未命中", not rem.mark_fired("nope"))
        # reload 验证持久化
        rem_reload = RemindersStore(path=Path(d) / f"reminders_C.yaml")
        check("mark_fired 持久化(reload 后仍 fired)", rem_reload.due() == [])
        check("reload list 仍 2 条", len(rem_reload.list()) == 2)
        # cancel
        check("cancel 命中", rem.cancel(r_future) and len(rem.list()) == 1)
        check("cancel 未命中", not rem.cancel("nope"))
        rb = RemindersStore(path=Path(d) / "rem_blk_C.yaml")
        rb.add("会议", now + 1800)
        check("to_prompt_block 含将到+文本", "会议" in rb.to_prompt_block() and "将到" in rb.to_prompt_block())
        check("空 to_prompt_block → ''", RemindersStore(path=Path(d) / "rem_empty_C.yaml").to_prompt_block() == "")
        # cap(优先删已 fired 旧条)
        rc = RemindersStore(path=Path(d) / "rem_cap_C.yaml")
        orig = sb.REMINDERS_MAX
        sb.REMINDERS_MAX = 2
        try:
            f1 = rc.add("fired-old", now - 100); rc.mark_fired(f1)
            f2 = rc.add("fired-newer", now - 10); rc.mark_fired(f2)
            o1 = rc.add("open1", now + 100)
            o2 = rc.add("open2", now + 200)          # 超限裁剪
            data = rc.list()
            check("cap 裁到 MAX=2", len(data) == 2)
            check("cap 优先留未 fired", all(not r["fired"] for r in data))
        finally:
            sb.REMINDERS_MAX = orig

        # ============================ D. dispatch 9 工具 + fire_at 三路 ============================
        print("[D dispatch 9 工具]")
        notes, todos, rem = _stores(d, "D")
        deps = {"notes": notes, "todos": todos, "reminders": rem}

        # notes
        out = dispatch_tool("add_note", {"text": "记个事", "tags": ["a"]}, deps)
        check("add_note 成功串", "已记下笔记" in out and len(notes.list()) == 1)
        check("add_note 空文本", "空" in dispatch_tool("add_note", {"text": "  "}, deps))
        check("list_notes 有内容", "记个事" in dispatch_tool("list_notes", {}, deps))
        nid = notes.list()[0]["id"]
        check("list_notes query 筛", "记个事" in dispatch_tool("list_notes", {"query": "记"}, deps))
        notes.add("另一条")
        check("list_notes 无命中提示", "没有相关笔记" in dispatch_tool("list_notes", {"query": "zzz"}, deps))
        check("delete_note 命中", "已删除" in dispatch_tool("delete_note", {"id": nid}, deps))
        check("delete_note 未命中", "没找到" in dispatch_tool("delete_note", {"id": "x"}, deps))

        # todos
        check("add_todo 成功", "已添加待办" in dispatch_tool("add_todo", {"text": "买菜"}, deps))
        check("add_todo 空文本", "空" in dispatch_tool("add_todo", {"text": ""}, deps))
        tid = todos.list()[0]["id"]
        check("list_todos 有内容", "买菜" in dispatch_tool("list_todos", {}, deps))
        check("toggle_todo 命中", "已更新" in dispatch_tool("toggle_todo", {"id": tid}, deps))
        check("toggle_todo 未命中", "没找到" in dispatch_tool("toggle_todo", {"id": "x"}, deps))
        check("delete_todo 命中", "已删除" in dispatch_tool("delete_todo", {"id": tid}, deps))

        # reminders — fire_at 三路
        now = time.time()
        # ① in_minutes
        out = dispatch_tool("set_reminder", {"text": "5分钟后喝水", "in_minutes": 5}, deps)
        r1 = rem.list()[-1]
        check("set_reminder in_minutes 串", "将于" in out and "喝水" in out)
        check("in_minutes → fire_at≈now+300", abs(r1["fire_at"] - (now + 300)) < 5)
        # ② at(HH:MM,必在未来或顺延明天)
        out = dispatch_tool("set_reminder", {"text": "晚上提醒", "at": "23:59"}, deps)
        r2 = rem.list()[-1]
        check("set_reminder at 串", "将于" in out)
        check("at → fire_at 在未来", r2["fire_at"] > now)
        out_bad = dispatch_tool("set_reminder", {"text": "x", "at": "25:99"}, deps)
        check("at 非法 → 错误串未设定", "未设定" in out_bad and "无法解析" in out_bad)
        # ③ 默认(无 in_minutes/at)
        before = len(rem.list())
        out = dispatch_tool("set_reminder", {"text": "默认时间"}, deps)
        r3 = rem.list()[-1]
        check("set_reminder 默认串", "将于" in out and len(rem.list()) == before + 1)
        check("默认 → fire_at≈now+默认分钟", abs(r3["fire_at"] - (now + 30 * 60)) < 5)
        check("set_reminder 空文本", "空" in dispatch_tool("set_reminder", {"text": "  "}, deps))
        # list / cancel
        check("list_reminders 有内容", "提醒" in dispatch_tool("list_reminders", {}, deps))
        rid = rem.list()[0]["id"]
        check("cancel_reminder 命中", "已取消" in dispatch_tool("cancel_reminder", {"id": rid}, deps))
        check("cancel_reminder 未命中", "没找到" in dispatch_tool("cancel_reminder", {"id": "x"}, deps))

        # store 缺失(deps 无对应键)→ 友好降级不抛
        for nm, msg in [("add_note", "笔记"), ("add_todo", "待办"), ("set_reminder", "提醒")]:
            check(f"{nm} deps 缺失 → 降级串", msg + "功能不可用" in dispatch_tool(nm, {"text": "x"}, {}))

        # _parse_clock_at 边界
        check("parse '14:30' → epoch > now", _parse_clock_at("14:30", now) is not None and _parse_clock_at("14:30", now) > now)
        check("parse '25:00' → None", _parse_clock_at("25:00", now) is None)
        check("parse '12:99' → None", _parse_clock_at("12:99", now) is None)
        check("parse 'abc' → None", _parse_clock_at("abc", now) is None)

        # ============================ E. _second_brain_block ============================
        print("[E _second_brain_block]")
        hub = Hub()
        hb = _new_hb(d, hub, "E")
        check("空 → ''", hb._second_brain_block() == "")
        hb.todos.add("未完成事")
        hb.reminders.add("将到期", time.time() + 1800)
        hb.notes.add("一条笔记")
        blk = hb._second_brain_block()
        check("含待办段", "待办" in blk and "未完成事" in blk)
        check("含提醒段", "即将到期提醒" in blk and "将到期" in blk)
        check("含笔记段", "近期笔记" in blk and "一条笔记" in blk)

        # ============================ F. _check_reminders 端到端 ============================
        print("[F _check_reminders 端到端]")
        hub = Hub()
        fake = FakeClient()
        hub.clients.add(fake)                    # 注册假客户端收广播
        hb = _new_hb(d, hub, "F")
        hb.reminders.add("该喝水了", time.time() - 1)   # 已到期未触发
        captured = {}

        async def fake_react_heartbeat(cand, persona_id, **k):
            captured["detail"] = cand.get("detail")
            captured["category"] = cand.get("category")
            captured["persona"] = persona_id
            yield "记得喝水哦~"

        orig = gateway.react_heartbeat
        gateway.react_heartbeat = fake_react_heartbeat
        try:
            await hb._check_reminders()
        finally:
            gateway.react_heartbeat = orig

        check("合成 candidate 含提醒文本", "该喝水了" in (captured.get("detail") or ""))
        check("candidate category=reminder", captured.get("category") == "reminder")
        check("persona 透传", captured.get("persona") == "zorya")
        types = [m.get("type") for m in fake.sent]
        check("广播含 chat_start", "chat_start" in types)
        check("广播含 chat_chunk", "chat_chunk" in types)
        check("广播含 chat_end", "chat_end" in types)
        chunk_text = "".join(m.get("delta", "") for m in fake.sent if m.get("type") == "chat_chunk")
        check("流式文本正确", chunk_text == "记得喝水哦~")
        end_msg = next((m for m in fake.sent if m.get("type") == "chat_end"), {})
        check("chat_end 带全文", end_msg.get("text") == "记得喝水哦~")
        check("history append agent 条", any(m.get("role") == "agent" and "记得喝水" in m.get("text", "") for m in hb._history()))
        check("mark_fired → due 空", hb.reminders.due() == [])
        # 持久化:reload 后 due 仍空(不会重复触发)
        rem_reload = RemindersStore(path=Path(d) / "reminders_F.yaml")
        check("fired 持久化(reload 不重复触发)", rem_reload.due() == [])
        # 无到期提醒 → 早返,不调 react_heartbeat
        captured.clear()
        fake.sent.clear()
        orig = gateway.react_heartbeat
        gateway.react_heartbeat = fake_react_heartbeat
        try:
            await hb._check_reminders()
        finally:
            gateway.react_heartbeat = orig
        check("无到期 → 不触发(captured 空)", captured == {} and fake.sent == [])
        # 访谈态 → 跳过
        hb.reminders.add("访谈期到期", time.time() - 1)
        hb.interviewing = True
        captured.clear()
        fake.sent.clear()
        orig = gateway.react_heartbeat
        gateway.react_heartbeat = fake_react_heartbeat
        try:
            await hb._check_reminders()
        finally:
            gateway.react_heartbeat = orig
            hb.interviewing = False
        check("访谈态 → 跳过不触发", captured == {} and fake.sent == [])

    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n{'=' * 40}\nPhase 6c probe: {PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
