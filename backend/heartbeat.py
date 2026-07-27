"""心跳核心:周期循环 + 状态机 + 静默计时,驱动宠物表情与互动。

状态:IDLE(监听呼吸)/ CONVERSATION(正常互动中)。
心跳检查在所有状态持续;但仅在 IDLE 才允许触发新的心跳互动。

Phase 2 记忆:对话结束(静默超时)把该段对话归档到 L3,后台异步抽取 L2 用户档案;
新对话注入最近 1~2 段情景 + 用户档案进 system prompt。跨对话/重启保持记忆。
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from typing import Optional

from backend.config import (
    ATTACH_MAX_CHARS, BEAT_PAUSE_SEC, COOLDOWN_PROFILES, DEFAULT_PERSONA, EPISODIC_RECALL_N,
    PERSONAS, PATTERN_MINE_EVERY_N_CONVOS, PATTERN_MINE_ENTRY_N,
    SWITCH_ROAST_ANTI_REPEAT_N, SWITCH_ROAST_MIN_SUBSTANCE,
)
from backend.memory.settings import settings
from backend.perception import probes
from backend.judgment import rules, patterns, router
from backend.memory.l0 import StateCache
from backend.memory.l2 import Profile
from backend.memory.l3 import EpisodicMemory
from backend.memory.filereader import read_file
from backend.memory.l4 import PatternStore
from backend.memory.conversation import ConversationLog
from backend.memory.execlog import ExecLog
from backend.memory.secondbrain import NotesStore, RemindersStore, TodosStore
from backend.voice import Speaker
from backend.approval import ApprovalGate
from backend.llm import gateway

log = logging.getLogger("heartbeat.core")

IDLE = "idle"
CONVERSATION = "conversation"


class Heartbeat:
    def __init__(self, hub, persona_id: str = DEFAULT_PERSONA) -> None:
        self.hub = hub
        self.profile = Profile()
        # 启动时若用户已选过人格,覆盖默认(onboarding 持久化)
        self.persona_id = self.profile.persona_id or persona_id
        self.state = IDLE
        self.l0 = StateCache()
        self.histories: dict = {self.persona_id: []}   # L1 工作记忆(按人格隔离;活跃=当前人格那条)
        self.last_agent_reply_at = None
        # === Phase 2 记忆 ===
        self.episodic = EpisodicMemory()
        self.conversation = ConversationLog()       # UI 对话日志(逐条;重启/切人格回灌,与 episodic 段级召回分离)
        self.episodic_blocks: dict = {}            # L3 召回缓存(按人格隔离;空串=不注入)
        self._extract_task: Optional[asyncio.Task] = None  # 进行中的 L2 抽取任务(防重叠)
        self._last_candidate: Optional[dict] = None       # 最近一次心跳候选(归档时作 trigger)
        self._reply_lock = None                           # 流式回复串行锁(lazy,避 3.9 loop 绑定)
        self.cooldown = rules.CooldownTracker(self.profile.cooldown_mode)  # 打扰频率冷却(可切档)
        # === Phase 4+ L4 模式沉淀 ===
        self.patterns = PatternStore()
        self.patterns_block: str = ""                # L4 模式注入缓存,空串=不注入
        self._pattern_task: Optional[asyncio.Task] = None  # 进行中的 L4 归纳任务(防重叠)
        self._convs_since_mine: int = 0              # 自上次归纳以来归档的对话数(满 N 触发归纳)
        # === Phase 6c 第二大脑(笔记 / 待办 / 提醒;persona 共享,任务模式工具 + 到点提醒作陪伴主动搭话)===
        self.notes = NotesStore()
        self.todos = TodosStore()
        self.reminders = RemindersStore()
        # === 语音朗读(agent 回复收尾经原生 AVSpeechSynthesizer 朗读;主线程派发,缺 AVFoundation 静默降级)===
        self.voice = Speaker()
        # === Phase 6d 写入 / 执行(逐次授权闸门 + 审计日志;任务模式工具 dispatch 经 deps 注入)===
        self.approvals = ApprovalGate(self.hub)     # 弹窗授权(loop 在 _react_deps 懒绑);写/删/执行前过此闸
        self.exec_log = ExecLog()                   # 已执行操作审计(只记已授权并执行了的)
        # === 切换吐槽 ===
        self._recent_roasts = collections.deque(maxlen=SWITCH_ROAST_ANTI_REPEAT_N * 2)  # 最近 N 句吐槽(防复读注入)
        # === Onboarding Stage 2 首次访谈(手动入口触发)===
        self.interviewing: bool = False              # 访谈进行中(运行时;访谈态屏蔽心跳 + 用短静默超时)
        self._interview_turns: int = 0               # 访谈中用户回答轮数(达 MAX 注入告别收尾)
        self._refresh_episodic_block()               # 启动预载最近 2 段(当前人格)
        self._refresh_patterns_block()               # 启动预载 L4 模式

    @property
    def reply_lock(self) -> asyncio.Lock:
        """串行化多窗口并发的 on_user_message:防 history 交错污染 + chunk 在线上乱序。

        lazy 创建(同 ws.py):Python 3.9 的 asyncio.Lock() 构造即绑 loop,
        而 __init__ 在 uvicorn 子线程 import 时跑、无 loop 会崩。
        """
        if self._reply_lock is None:
            self._reply_lock = asyncio.Lock()
        return self._reply_lock

    # ---- 按人格隔离的记忆访问器(L1 history / L3 episodic 召回)----
    def _history(self, persona_id: Optional[str] = None) -> list:
        """取(或惰性创建)指定人格的 L1 history;默认当前人格。归档路径要显式指定 old 人格。"""
        pid = persona_id if persona_id is not None else self.persona_id
        return self.histories.setdefault(pid, [])

    def _episodic_block(self) -> str:
        """当前人格的 L3 召回缓存(空串=不注入)。"""
        return self.episodic_blocks.get(self.persona_id, "")

    # ---- 表达 ----
    async def _express(self, state: str) -> None:
        log.info("express: %s (conv_state=%s)", state, self.state)
        await self.hub.send({"type": "expression", "state": state})

    async def _say(self, role: str, text: str) -> None:
        log.info("say [%s] %s", role, text)
        await self.hub.send({
            "type": "chat", "role": role, "text": text,
            "persona": self.persona_id if role == "agent" else "user",
        })
        self._history().append({"role": role, "text": text})

    async def _push_meta(self) -> None:
        """推 meta(人格 id/name/name_cyr/archetype/accent + 访谈状态),供前端更新名字/西里尔小字/配色/入口文案。"""
        p = PERSONAS.get(self.persona_id, {})
        await self.hub.send({
            "type": "meta",
            "persona": {
                "id": self.persona_id,
                "name": p.get("name", self.persona_id),
                "name_cyr": p.get("name_cyr", ""),
                "archetype": p.get("archetype", ""),
                "accent": p.get("accent", "#c9d6e5"),
                "interviewed": self.profile.interviewed,
            },
            "interviewing": self.interviewing,
        })

    def _log_conversation(self, role: str, text: str,
                          attachment_name: Optional[str] = None) -> None:
        """逐条落盘对话(UI 历史,与 L3 段级召回分离)。重启 / 切换人格后回灌用。"""
        self.conversation.append(role, text, self.persona_id, attachment_name)

    async def _push_history(self) -> None:
        """回灌当前人格最近 N 条对话到前端(重启 / 切换人格后让主窗看到历史记录)。"""
        msgs = self.conversation.recent_messages(self.persona_id)
        await self.hub.send({"type": "history", "messages": msgs})

    async def _begin_agent_utterance(self, **extra) -> None:
        """agent 开口前:打断旧朗读 + 广播 chat_start(payload 形状不变)。

        收拢 5 处 chat_start 发送点,统一在「新回复开始」时停掉上一句朗读(避免叠音)。
        extra 透传 chat_start 额外字段(如 task=is_task)。
        """
        self.voice.stop()
        # persona 随消息发出:前端据此给该条 agent 消息挂对应人格头像(切换人格后历史消息仍能区分是谁说的)
        payload = {"type": "chat_start", "role": "agent", "persona": self.persona_id}
        payload.update(extra)
        await self.hub.send(payload)

    async def _finish_agent_utterance(self, full_text: str) -> None:
        """agent 回复收尾:广播 chat_end(payload 形状不变)+ 朗读全文(若启用)。

        收拢 5 处 chat_end 发送点。朗读在后端触发一次(非每窗口各读),Speaker 派发到主线程。
        """
        await self.hub.send({"type": "chat_end", "role": "agent", "text": full_text})
        self._log_conversation("agent", full_text)   # 落盘 UI 历史(收拢 5 路 chat_end;重启回灌)
        self.voice.speak(full_text, self.persona_id)

    def _refresh_episodic_block(self, persona_id: Optional[str] = None) -> None:
        """重读指定人格(默认当前)最近 N 段情景记忆到缓存。纯内存 + 一次尾部读,极快。失败 → ""。"""
        pid = persona_id if persona_id is not None else self.persona_id
        try:
            self.episodic_blocks[pid] = self.episodic.recent_text(EPISODIC_RECALL_N, persona_id=pid)
        except Exception:  # noqa: BLE001
            self.episodic_blocks[pid] = ""

    def _refresh_patterns_block(self) -> None:
        """重读 L4 行为模式到注入缓存(纯内存,极快)。失败 → ""。"""
        try:
            self.patterns_block = self.patterns.to_prompt_block()
        except Exception:  # noqa: BLE001
            self.patterns_block = ""

    def _react_deps(self) -> dict:
        """构造 ReAct 工具的依赖注入(recall_memory 用 episodic + 当前人格,召回按人格隔离;
        第二大脑工具用 notes/todos/reminders;Phase 6d 写/执行工具用 authorizer + exec_log)。每次现造。"""
        # Phase 6d:授权闸门懒绑事件循环(__init__ 不在 loop 线程,get_running_loop 会抛;此处 on-loop 安全)
        self.approvals.bind_loop(asyncio.get_running_loop())
        return {
            "episodic": self.episodic,
            "persona_id": self.persona_id,
            "notes": self.notes,
            "todos": self.todos,
            "reminders": self.reminders,
            "authorizer": self.approvals,     # Phase 6d:写/删/执行工具调用前 request() 弹窗等用户确认
            "exec_log": self.exec_log,        # Phase 6d:已执行操作审计
        }

    async def on_confirm_response(self, id: str, approved: bool) -> None:
        """Phase 6d:用户在主窗点了「允许/拒绝」→ 解开对应授权 Future。

        不持 reply_lock(只解 Future,无共享可变态);与持锁等 gen 的 on_user_message 不抢锁。
        未知/超时已清的 id → resolve no-op。
        """
        self.approvals.resolve(str(id), bool(approved))

    @staticmethod
    def _truncate_args(args) -> dict:
        """tool_step 给前端的 args 副本:每值截断 ~200 字(write_file content / run_command 命令不撑爆 summary)。"""
        if not isinstance(args, dict):
            return args
        out = {}
        for k, v in args.items():
            s = v if isinstance(v, str) else repr(v)
            if len(s) > 200:
                s = s[:200] + f"…(共 {len(s)} 字符)"
            out[k] = s
        return out

    def _second_brain_block(self) -> str:
        """任务模式注入的 [待办与笔记] 纯内容(待办 + 即将到期提醒 + 近期笔记,各段小标题,空段跳过)。

        纯内存读,每次现算(todos 随对话变化,不缓存)。整体空 → ""(空串跳过整段)。陪伴模式不调(省 token)。
        """
        parts = []
        todos = self.todos.to_prompt_block()
        if todos:
            parts.append("待办:\n" + todos)
        rems = self.reminders.to_prompt_block()
        if rems:
            parts.append("即将到期提醒:\n" + rems)
        notes = self.notes.to_prompt_block()
        if notes:
            parts.append("近期笔记:\n" + notes)
        return "\n\n".join(parts)

    # ---- 心跳周期 ----
    async def _tick(self) -> None:
        if self.interviewing:
            return                                  # 访谈进行中:跳过感知/候选(屏蔽心跳打扰 + 避免 _last_candidate 污染归档)
        # IDLE 期 history 防涨:心跳开口进 history 但不归档,避免无限堆积(保留最近几条供承接)
        if self.state == IDLE:
            h = self._history()
            if len(h) > 20:
                self.histories[self.persona_id] = h[-6:]   # 切片是新 list,须重绑 slot
        snap = probes.run_probes()                       # 1) 感知
        self.l0.push(snap)                               # 2) 先入 L0(聚合才含本次快照)
        prev = self.l0.previous                          # 3) 上次快照(push 后 previous=上次)
        # 4) 候选合流:规则候选(9类)+ L4 模式触发(pattern_match),统一过冷却
        candidates = rules.find_candidates(prev, snap, self.l0) \
            + patterns.find_triggered(snap, self.l0, self.patterns)
        log.info("tick: app=%s idle=%s title=%s hour=%s",
                 snap.get("foreground_app"), snap.get("idle_sec"),
                 snap.get("window_title"), snap.get("hour"))

        # 5) 仅 IDLE 态、候选通过冷却,才考虑心跳互动
        if self.state != IDLE:
            return
        fresh = self.cooldown.take(candidates)           # 按 priority 排序 + 冷却过滤
        if not fresh:
            return                                       # 无候选 → 静默,不调 LLM

        cand = fresh[0]
        self._last_candidate = cand                      # 归档时作 trigger
        if cand.get("_pattern_id"):                      # L4 模式命中:记 last_fired(诊断用)
            self.patterns.mark_fired(cand["_pattern_id"])
        log.info("candidate event: %s", cand)
        await self._express("alert")                     # 5) 抬头
        await asyncio.sleep(BEAT_PAUSE_SEC)

        verdict = await gateway.judge_heartbeat(         # 6) LLM 精判(注入 profile,不注入 L3/L4)
            cand, self.persona_id, {}, profile=self.profile)
        if not verdict["interact"]:
            await self._express("settle")                # 招牌:抬头又趴回去
            await asyncio.sleep(BEAT_PAUSE_SEC)
            await self._express("idle")
            return
        # interact=True → ReAct 流式生成搭话(可先查工具),三段式推送(chat_start/chunk/end)
        await self._express("thinking")                  # ReAct 期间显示思考表情
        await self._begin_agent_utterance()
        full: list = []
        talk_sent = False
        try:
            async for delta in gateway.react_heartbeat(
                cand, self.persona_id, profile=self.profile, deps=self._react_deps(),
                patterns_block=self.patterns_block,
            ):
                if delta is gateway.CLEAR:               # 思考过程撤回(工具轮结束)
                    await self.hub.send({"type": "chat_clear", "role": "agent"})
                    full.clear()
                    continue
                if not talk_sent and delta.strip():
                    await self._express("talk")          # 心跳互动固定 talk(首 token 切,保持招牌)
                    talk_sent = True
                full.append(delta)
                await self.hub.send({"type": "chat_chunk", "role": "agent", "delta": delta})
        finally:
            full_text = "".join(full) or "……"
            # 进 history 供用户回复承接;不切 CONVERSATION(保持 IDLE 让心跳继续跑)
            self._history().append({"role": "agent", "text": full_text})
            self.last_agent_reply_at = time.monotonic()
            await self._finish_agent_utterance(full_text)
            if not talk_sent:                            # 无输出(罕见)→ 收回表情防卡 thinking
                await self._express("settle")
                await asyncio.sleep(BEAT_PAUSE_SEC)
                await self._express("idle")
            log.info("react heartbeat done (%d chunks, %d chars): %s",
                     len(full), len(full_text), full_text)

    async def _build_user_text(self, text: str, attachment: Optional[dict]) -> tuple:
        """附件 → 读文件解析、截断 → (chip 元数据, 进 history 的全文)。

        无附件 → (None, text)。文件读用 to_thread(PDF 解析可能慢,不阻塞事件循环)。
        广播给前端的只是短文本 + chip;history 存全文(react_reply 注入 history → LLM 见)。
        """
        if not attachment or not attachment.get("path"):
            return None, text
        path = attachment["path"]
        name = attachment.get("name") or os.path.basename(path)
        try:
            content = (await asyncio.to_thread(read_file, path) or "").strip()
        except Exception:  # noqa: BLE001
            content = ""
        if not content:
            content = "(文件为空或无法读取)"
        if len(content) > ATTACH_MAX_CHARS:
            content = content[:ATTACH_MAX_CHARS] + f"\n…(已截断,原文约 {len(content)} 字符)"
        meta = {"name": name, "chars": len(content)}
        full = f"[附件:{name}]\n{content}" + (f"\n\n{text}" if text else "")
        return meta, full

    # ---- 用户消息(进入 / 延续 正常互动,流式回复)----
    async def on_user_message(self, text: str, attachment: Optional[dict] = None) -> None:
        async with self.reply_lock:                     # 串行化多窗口并发(单例 history 共享)
            self.voice.stop()                           # 用户开口 → 立刻停掉当前朗读(别对着用户的话继续念)
            self.state = CONVERSATION
            self.last_agent_reply_at = None             # 用户开口=对话活跃:置空静默锚点,挡 _silence_check 用上次回复的旧时间戳误判超时(agent 回复完 finally 会重设)
            await self._express("thinking")
            # 附件:读文件解析 → full_text 含全文进 history(LLM 经 react_reply 注入 history 见);广播短文本+chip
            attach_meta, full_text = await self._build_user_text(text, attachment)
            await self.hub.send({"type": "chat", "role": "user", "text": text,
                                 "attachment": attach_meta, "persona": "user"})
            self._log_conversation("user", text, attach_meta.get("name") if attach_meta else None)
            self._history().append({"role": "user", "text": full_text})   # history 给 LLM 的含全文
            log.info("say [user] %s%s", text, f" +附件[{attach_meta['name']}]{attach_meta['chars']}字" if attach_meta else "")
            is_task = (not self.interviewing) and router.classify_task(text, attachment is not None)
            await self._begin_agent_utterance(task=is_task)
            full: list = []
            engaged_sent = False
            last_round = False
            if self.interviewing:                       # Stage 2 访谈:走 react_interview + 计轮数
                self._interview_turns += 1
                last_round = self._interview_turns >= settings.interview_max_rounds
            try:
                if self.interviewing:
                    gen = gateway.react_interview(      # 访谈:注入 profile 当进度,纯文本(无工具)
                        self.persona_id, self.profile, self._history(),
                        turn=self._interview_turns, max_turns=settings.interview_max_rounds,
                        last_round=last_round)
                else:
                    # Phase 6a 自动分流:复杂任务走 react_task,闲聊走 react_reply(陪伴零改动)
                    common = dict(
                        activity=self.l0.latest or {},
                        profile=self.profile,
                        episodic_block=self._episodic_block(),  # 注入当前人格 L3 召回 + 用户档案
                        deps=self._react_deps(),             # Phase 4:ReAct 可查工具
                        patterns_block=self.patterns_block,  # L4 行为模式注入
                    )
                    if is_task:
                        # Phase 6c:任务模式额外注入 [待办与笔记](陪伴不注入省 token、不污染)
                        gen = gateway.react_task(text, self.persona_id, self._history(),
                                                 **common, todo_block=self._second_brain_block())
                    else:
                        gen = gateway.react_reply(text, self.persona_id, self._history(), **common)
                async for delta in gen:
                    if delta is gateway.CLEAR:           # 陪伴模式思考撤回(工具轮结束;访谈 max_rounds=1 无工具轮,不触发)
                        await self.hub.send({"type": "chat_clear", "role": "agent"})
                        full.clear()
                        continue
                    if isinstance(delta, tuple):         # 任务模式工具调用事件 → tool_step(前端可折叠展示 agent 读了啥/查了啥/写了啥)
                        _, t_name, t_args, t_obs = delta
                        await self.hub.send({"type": "tool_step", "name": t_name,
                                             "args": self._truncate_args(t_args), "result": (t_obs or "")[:200]})
                        log.info("tool_step[%s] → %s", t_name, (t_obs or "")[:80])
                        continue
                    if not engaged_sent:
                        await self._express("engaged")  # 首 token 即切 engaged(替代旧的 sleep 假延迟)
                        engaged_sent = True
                    full.append(delta)
                    await self.hub.send({"type": "chat_chunk", "role": "agent", "delta": delta})
            finally:
                # history 一次性存拼好的整句(role 必须是 "agent",与现有一致);finally 保证 chat_end 必发
                full_text = "".join(full) or "…"
                self._history().append({"role": "agent", "text": full_text})
                self.last_agent_reply_at = time.monotonic()
                await self._finish_agent_utterance(full_text)
                log.info("stream reply done (%d chunks, %d chars): %s",
                         len(full), len(full_text), full_text)
                if self.interviewing:
                    # 每轮增量抽取稳定事实(不归档,归档留到收尾);_extract_task 守卫去重
                    if self._extract_task is None or self._extract_task.done():
                        self._extract_task = asyncio.create_task(
                            self._run_extract([dict(m) for m in self._history()], self.persona_id))
                    if last_round:                      # 轮数上限:该轮告别后立即收尾
                        await self._end_interview()

    # ---- 选人格 / 运行时切换(onboarding Stage 1 + 上线后随时换)----
    async def on_select_persona(self, persona_id: str) -> None:
        if persona_id not in PERSONAS:
            log.warning("unknown persona: %s", persona_id)
            return
        if self.interviewing:                          # 访谈中切人格:先静默归档旧访谈再走切换(防状态泄漏)
            await self._end_interview(archive=True, farewell=False)
        old_id = self.persona_id
        self.persona_id = persona_id
        self.profile.set_onboarded(persona_id)           # 落盘 persona + onboarded=True
        self.histories.setdefault(persona_id, [])        # 确保新人格 L1 槽位存在
        await self._push_meta()
        await self._push_history()                       # 回灌新人格历史(切换 / 重选都刷新主窗)

        if old_id == persona_id:
            return                                       # 重选同一人格:不归档不吐槽(仅 meta 重推)

        # 切换关键段:早置 CONVERSATION 挡住并发的 _tick 心跳打断吐槽(_tick 见非 IDLE 即早返)
        self.state = CONVERSATION
        self._refresh_episodic_block()                   # 预热新人格 L3 召回缓存

        old_history = self.histories.get(old_id, [])
        if old_id is not None and self._has_substance(old_history):
            async with self.reply_lock:                  # 与 in-flight on_user_message 串行
                await self._archive_and_extract(persona_id=old_id, history=old_history)
            self.histories[old_id] = []                  # 清旧 L1(归档已深拷贝)
            await self._switch_in_roast(old_id)          # 仅"真聊过"才吐槽上一人格
        log.info("persona selected: %s (was %s)", persona_id, old_id)

    # ---- 切换吐槽:新人格用关系设定即兴说一句提及旧人格的话(看不到旧对话内容)----
    def _has_substance(self, history: list) -> bool:
        """是否"真聊过":≥ MIN_SUBSTANCE 条 且 含至少一个 user 轮(纯 roast/独白不算)。"""
        if len(history) < SWITCH_ROAST_MIN_SUBSTANCE:
            return False
        return any(m.get("role") != "agent" for m in history)

    def _build_handoff_block(self, new_id: str, old_id: str) -> str:
        """[交接] 段:刚离开的人格 + 我(new)对她的关系设定(stance/voice),供吐槽时立足人设。"""
        new_p = PERSONAS.get(new_id, {})
        old_p = PERSONAS.get(old_id, {})
        rel = new_p.get("relationships", {}).get(old_id, {})
        old_name = old_p.get("name", old_id)
        lines = ["[交接]", f"用户刚从「{old_name}」({old_p.get('name_meaning', '')}) 那儿过来。"]
        if rel.get("stance"):
            lines.append(f"你对 {old_name} 的态度:{rel['stance']}")
        if rel.get("voice"):
            lines.append(f"吐槽方向 / 语气提示(非固定句,即兴发挥):{rel['voice']}")
        lines.append("现在用你的语气,就「用户刚和另一个陪伴者待过、现在来找你」说一句话(1-2 句)。")
        lines.append("铁律:① 绝不透露对方对话内容(你不知道他们聊了什么);② 不借用对方的 App 梗/口头禅/风格;③ 立足你自己的性格。")
        return "\n".join(lines)

    def _render_anti_repeat(self) -> str:
        """最近几次吐槽 → 防复读注入(让 LLM 换一种说法)。空 → ""(跳过)。"""
        if not self._recent_roasts:
            return ""
        recent = list(self._recent_roasts)[-SWITCH_ROAST_ANTI_REPEAT_N:]
        return "[最近几次交接吐槽(请换一种说法,不要重复或近似)]\n" + "\n".join(f"- {r}" for r in recent)

    async def _switch_in_roast(self, old_id: str) -> None:
        """切换吐槽:新人格流式说一句提及 old 的话(三段式 chat_start/chunk/end,仿心跳路径)。"""
        new_id = self.persona_id
        handoff_block = self._build_handoff_block(new_id, old_id)
        anti_repeat = self._render_anti_repeat()
        async with self.reply_lock:                  # 与 in-flight on_user_message 串行(多窗口)
            await self._express("alert")
            await asyncio.sleep(BEAT_PAUSE_SEC)
            await self._express("thinking")
            await self._begin_agent_utterance()
            full: list = []
            talk_sent = False
            try:
                async for delta in gateway.react_switch_in(
                    new_id, old_id, profile=self.profile,
                    handoff_block=handoff_block, anti_repeat=anti_repeat,
                ):
                    if delta is gateway.CLEAR:           # 思考过程撤回(工具轮结束)
                        await self.hub.send({"type": "chat_clear", "role": "agent"})
                        full.clear()
                        continue
                    if not talk_sent and delta.strip():
                        await self._express("talk")   # 首 token 切 talk(与心跳一致;engaged 留给用户发言)
                        talk_sent = True
                    full.append(delta)
                    await self.hub.send({"type": "chat_chunk", "role": "agent", "delta": delta})
            finally:
                full_text = "".join(full) or "……"
                self._history().append({"role": "agent", "text": full_text})  # 进新人格 L1 供后续承接
                self._recent_roasts.append(full_text)                          # 防复读
                self.state = CONVERSATION                                       # 用户回复则续,静默则归档
                self.last_agent_reply_at = time.monotonic()
                await self._finish_agent_utterance(full_text)
                if not talk_sent:                                              # 无输出(罕见)→ 收回表情防卡 thinking
                    await self._express("settle")
                    await asyncio.sleep(BEAT_PAUSE_SEC)
                    await self._express("idle")
                log.info("switch-in roast (%s<-%s): %s", new_id, old_id, full_text)

    async def on_set_cooldown_mode(self, mode: str) -> None:
        """用户切换打扰频率档位:更新 tracker + 落盘 + 广播同步所有窗口。"""
        if mode not in COOLDOWN_PROFILES:
            log.warning("unknown cooldown mode: %s", mode)
            return
        self.cooldown.set_mode(mode)
        self.profile.set_cooldown_mode(mode)
        await self.hub.send({"type": "cooldown_mode", "mode": mode})
        log.info("cooldown mode set: %s", mode)

    # ---- Onboarding Stage 2 首次访谈(手动入口触发;LLM 自主带队)----
    async def on_start_interview(self) -> None:
        """用户点「认识我」→ 进入访谈:人格流式说开场白 + 第一问。重入守卫(interviewing 早返)。"""
        if self.interviewing:
            return
        async with self.reply_lock:                  # 与 in-flight on_user_message 串行(多窗口)
            self.interviewing = True
            self._interview_turns = 0
            self.state = CONVERSATION                # 挡 _tick 心跳打断
            await self._express("alert")
            await asyncio.sleep(BEAT_PAUSE_SEC)
            await self._express("thinking")
            await self.hub.send({"type": "interview_start"})
            await self._begin_agent_utterance()
            full: list = []
            talk_sent = False
            try:
                async for delta in gateway.react_interview(
                    self.persona_id, self.profile, self._history(),
                    turn=0, max_turns=settings.interview_max_rounds, opening=True):
                    if not talk_sent and delta.strip():
                        await self._express("talk")  # 首 token 切 talk(人格主动开口,与心跳一致)
                        talk_sent = True
                    full.append(delta)
                    await self.hub.send({"type": "chat_chunk", "role": "agent", "delta": delta})
            finally:
                full_text = "".join(full) or "……"
                # 开场白进 L1:静默计时锚点 + 后续承接上下文 + 归档首条
                self._history().append({"role": "agent", "text": full_text})
                self.last_agent_reply_at = time.monotonic()
                await self._finish_agent_utterance(full_text)
                if not talk_sent:                    # 无输出(罕见)→ 收回表情防卡 thinking
                    await self._express("settle")
                    await asyncio.sleep(BEAT_PAUSE_SEC)
                    await self._express("idle")
                log.info("interview opened (%d chars): %s", len(full_text), full_text)

    async def on_end_interview(self) -> None:
        """用户点「跳过访谈」→ 立即收尾。"""
        if not self.interviewing:
            return
        async with self.reply_lock:
            await self._end_interview(archive=True, farewell=False)

    async def _end_interview(self, archive: bool = True, farewell: bool = False) -> None:
        """访谈统一收尾(四路汇聚:静默/上限/跳过/切人格中断)。

        归档 L3(可选,trigger 显式标 interview 避开 _last_candidate 心跳事件)+ 标 interviewed +
        回 IDLE + 广播 interview_end。防状态泄漏:interviewing/turns/state/L1 全部复位。
        调用方应在 reply_lock 内(on_start/on_end/on_user_message)或主循环串行(_silence_check);
        本方法不再获取 reply_lock(asyncio.Lock 不可重入,否则与调用方死锁)。
        """
        if archive and any(m.get("role") != "agent" for m in self._history()):
            # 归档访谈对话:只要用户参与过(含 user 轮)就值得存 L3 供未来 recall;
            # trigger 显式标 interview,避开 _last_candidate 的心跳事件污染。
            # (不用 _has_substance 的 ≥4 门槛——那是切换吐槽的门槛,访谈哪怕 1 轮也该归档)
            trigger = {"category": "interview", "detail": "首次访谈"}
            hist_snapshot = [dict(m) for m in self._history()]
            try:
                last_snap = self.l0.latest or {}
                entry = EpisodicMemory.build_entry(
                    self.persona_id, trigger, last_snap.get("foreground_app"), hist_snapshot)
                await asyncio.to_thread(self.episodic.append, entry)
                if self._extract_task is None or self._extract_task.done():
                    self._extract_task = asyncio.create_task(
                        self._run_extract(hist_snapshot, self.persona_id))
                self._refresh_episodic_block(self.persona_id)
                log.info("interview archived (persona=%s, %d msgs)", self.persona_id, len(hist_snapshot))
            except Exception:  # noqa: BLE001
                log.exception("interview archive failed (non-fatal)")
        self.profile.set_interviewed()
        self.interviewing = False
        self._interview_turns = 0
        self.state = IDLE
        self.histories[self.persona_id] = []            # 清当前人格 L1(归档已深拷贝)
        await self.hub.send({"type": "interview_end", "interviewed": True})
        await self._express("settle")
        await asyncio.sleep(BEAT_PAUSE_SEC)
        await self._express("idle")
        log.info("interview ended (interviewed=True)")

    # ---- 静默超时 → 归档 + 结束正常互动 ----
    async def _silence_check(self) -> None:
        if self.state != CONVERSATION or self.last_agent_reply_at is None:
            return
        timeout = settings.interview_silence if self.interviewing else settings.silence_timeout
        if time.monotonic() - self.last_agent_reply_at > timeout:
            if self.interviewing:
                log.info("interview silence timeout → end interview")
                await self._end_interview(archive=True, farewell=False)
                return
            log.info("silence timeout → end conversation")
            await self._archive_and_extract()
            self.state = IDLE
            self.histories[self.persona_id] = []        # 清当前人格 L1
            await self._express("doze_off")
            await asyncio.sleep(BEAT_PAUSE_SEC)
            await self._express("idle")

    async def _archive_and_extract(self, persona_id: Optional[str] = None,
                                    history: Optional[list] = None) -> None:
        """归档 L3 + 起 L2 抽取任务 + 刷新召回。必须在清 history 之前调;全程不抛。

        persona_id/history 默认取当前人格/当前 L1(沉默路径零改动);切换路径显式传 old 人格
        + 其 L1。user 轮守卫:无任何 user 消息(lone roast / 纯心跳独白)不归档,挡 L3 污染。
        """
        pid = persona_id if persona_id is not None else self.persona_id
        hist = history if history is not None else self._history(pid)
        try:
            hist_snapshot = [dict(m) for m in hist]   # 深拷贝,后续清空不影响归档与抽取
            if not hist_snapshot:
                return                                # 没内容不归档(避免空段污染 L3)
            if not any(m.get("role") != "agent" for m in hist_snapshot):
                return                                # 无 user 轮(lone roast)不归档,挡 L3 污染
            last_snap = self.l0.latest or {}
            trigger = self._last_candidate or {"category": "conversation", "detail": "用户主动"}
            foreground = last_snap.get("foreground_app")
            entry = EpisodicMemory.build_entry(pid, trigger, foreground, hist_snapshot)
            await asyncio.to_thread(self.episodic.append, entry)
            log.info("episodic archived (persona=%s, %d msgs, fg=%s)", pid, len(hist_snapshot), foreground)
            if self._extract_task is None or self._extract_task.done():
                self._extract_task = asyncio.create_task(self._run_extract(hist_snapshot, pid))
            else:
                log.info("previous extract still running, skip")
            self._refresh_episodic_block(pid)         # 刷新被归档人格的召回缓存
            # L4 模式归纳:累积 N 段对话后批量触发(与 L2 抽取并行,独立 task + 独立 flag)
            self._convs_since_mine += 1
            if self._convs_since_mine >= PATTERN_MINE_EVERY_N_CONVOS and (
                    self._pattern_task is None or self._pattern_task.done()):
                self._convs_since_mine = 0
                self._pattern_task = asyncio.create_task(self._run_pattern_mine())
        except Exception:  # noqa: BLE001
            log.exception("archive_and_extract failed (non-fatal)")

    async def _run_extract(self, hist_snapshot: list, persona_id: Optional[str] = None) -> None:
        """后台抽取稳定事实 → merge 进档案。异常吞掉(主循环不能崩)。persona_id 默认当前(切换归档传旧)。"""
        try:
            pid = persona_id if persona_id is not None else self.persona_id
            new_facts = await gateway.extract_facts(hist_snapshot, pid)
            if new_facts:
                changed = self.profile.merge(new_facts)
                if changed:
                    log.info("profile updated: %s", list(new_facts.keys()))
        except Exception:  # noqa: BLE001
            log.exception("extract_facts task failed (non-fatal)")

    async def _run_pattern_mine(self) -> None:
        """后台归纳 L4 行为模式:读最近 N 段 L3 + 现有模式 → extract_patterns → merge → 刷新注入。异常吞掉。"""
        try:
            recent = self.episodic.recent(PATTERN_MINE_ENTRY_N)
            if not recent:
                return
            result = await gateway.extract_patterns(
                recent, self.patterns.data.get("patterns", []), self.persona_id)
            if result and self.patterns.merge(result):
                self._refresh_patterns_block()
                log.info("patterns updated: %d patterns", len(self.patterns))
        except Exception:  # noqa: BLE001
            log.exception("pattern mine task failed (non-fatal)")

    # ---- 到期提醒(Phase 6c:到点主动搭话,复用心跳 react_heartbeat 路径)----
    async def _check_reminders(self) -> None:
        """每个 tick 查到期未触发的提醒,逐条流式搭话提醒用户(三段式 chat_start/chunk/end,仿心跳)。

        - 立即触发(含 CONVERSATION 态):提醒时间敏感,迟到更糟;持 reply_lock 与 on_user_message 串行防流交错。
        - 访谈态跳过(不打断访谈);触发后立即 mark_fired 持久化,防崩溃/重启重复触发。
        - 不调 judge(提醒是用户预设的明确意图,无需"该不该打扰"精判)、不加 gateway mode——
          合成 candidate 直送 react_heartbeat。state 不改(IDLE 保持 IDLE 让心跳继续;CONVERSATION 保持加入当前对话)。
        """
        if self.interviewing:
            return
        due = self.reminders.due()
        if not due:
            return
        for r in due:
            rid = r.get("id")
            text = r.get("text") or "提醒"
            cand = {"key": f"reminder:{rid}", "category": "reminder",
                    "detail": f"你设定的提醒(现在到期):{text}"}
            async with self.reply_lock:        # 与 in-flight on_user_message / 切换吐槽串行(多窗口)
                await self._express("alert")
                await asyncio.sleep(BEAT_PAUSE_SEC)
                await self._express("thinking")
                await self._begin_agent_utterance()
                full: list = []
                talk_sent = False
                try:
                    async for delta in gateway.react_heartbeat(
                        cand, self.persona_id, profile=self.profile,
                        deps=self._react_deps(), patterns_block=self.patterns_block,
                    ):
                        if delta is gateway.CLEAR:        # 思考过程撤回(工具轮结束)
                            await self.hub.send({"type": "chat_clear", "role": "agent"})
                            full.clear()
                            continue
                        if not talk_sent and delta.strip():
                            await self._express("talk")   # 首 token 切 talk(与心跳一致)
                            talk_sent = True
                        full.append(delta)
                        await self.hub.send({"type": "chat_chunk", "role": "agent", "delta": delta})
                finally:
                    full_text = "".join(full) or "……"
                    self._history().append({"role": "agent", "text": full_text})
                    self.last_agent_reply_at = time.monotonic()
                    await self._finish_agent_utterance(full_text)
                    if not talk_sent:                    # 无输出(罕见)→ 收回表情防卡 thinking
                        await self._express("settle")
                        await asyncio.sleep(BEAT_PAUSE_SEC)
                        await self._express("idle")
                    log.info("reminder fired (id=%s): %s", rid, full_text)
                self.reminders.mark_fired(rid)           # 持久化已触发,防崩溃后重复触发

    # ---- 主循环 ----
    async def run(self) -> None:
        log.info("heartbeat started (interval=%ss, persona=%s, onboarded=%s)",
                 settings.heartbeat_interval, self.persona_id, self.profile.onboarded)
        await self._express("idle")
        while True:
            try:
                await self._tick()
                await self._silence_check()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            try:
                await self._check_reminders()     # Phase 6c:到期提醒(独立 try,异常不伤 tick)
            except Exception:  # noqa: BLE001
                log.exception("reminder check failed")
            await asyncio.sleep(settings.heartbeat_interval)
