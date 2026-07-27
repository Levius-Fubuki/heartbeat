"""LLM 网关。有 API key 时用 OpenAI-compatible(DeepSeek);无 key 回退 canned 桩。

判定 / 对话 / 事实抽取 都用同一模型;同步 OpenAI 调用通过 asyncio.to_thread 包,避免阻塞事件循环。
system prompt 由 build_system_prompt 统一拼装:人格 seed(A 半) + 用户档案(B 半) + 最近对话(L3 召回)
+ 当前情境 + 约束(因 judge/reply 模式而异)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

from backend.config import (
    INTERVIEW_MAX_TOKENS, INTERVIEW_TEMPERATURE,
    PERSONAS, SWITCH_ROAST_MAX_TOKENS, SWITCH_ROAST_TEMPERATURE,
)
from backend.llm.tools import TOOLS, dispatch_tool, task_tools
from backend.memory.settings import settings

log = logging.getLogger("heartbeat.llm")
_client = None
_async_client = None  # 流式专用 AsyncOpenAI(同包);judge/extract 仍用同步 _client + to_thread
_client_sig = None    # 已构建 client 的 (api_key, base_url) 签名;设置变了 → 下次访问重建(热生效)

# canned 兜底(无 key 或调用失败时)
_HEARTBEAT = {
    "zorya": ["……夜色不错。要听歌?", "音乐。嗯。", "星要升起来了。"],
    "vesna": ["哇,开音乐啦?听什么呀?🍃", "Spotify!是什么风格的呀?好奇~"],
    "rada": ["哦哦要听歌啦!听什么听什么!✨", "音乐时间!开心!"],
    "yara": ["又摸鱼听歌?行吧,听啥。", "Spotify 一开,活就干不动了吧。"],
    "mira": ["放点音乐也好,歇一会儿吧。", "想听点平静的吗?"],
}
_REPLY = {
    "zorya": ["嗯。", "……我在。", "知道了。"],
    "vesna": ["嗯嗯!🍃", "好有意思~", "哇,然后呢?"],
    "rada": ["好嘞!✨", "收到收到!", "加油加油!"],
    "yara": ["行。", "就这?", "得了吧。"],
    "mira": ["嗯,我在听。", "慢慢来,不急。", "辛苦啦。"],
}

CLEAR = object()  # _react_loop 工具轮结束的撤回哨兵:调用方据此发 chat_clear,清空前端已显示的思考过程


# 事实抽取的输出 schema(字段全部可省略)
_FACT_SCHEMA = (
    '只返回 JSON 对象,字段都可省略(没把握就不要写):'
    ' {"identity":{"role":"职业/身份","fields":["方向1","方向2"]},'
    ' "interests":["爱好"],'
    ' "schedule":{"focus_hours":"HH:MM-HH:MM","dnd_windows":["HH:MM-HH:MM"]},'
    ' "chat_pref":{"verbosity":"concise 或 chatty","emoji_ok":true},'
    ' "fun_fact":"一个小彩蛋"}'
)


def _get_client():
    """同步 OpenAI client。按 settings 的 (api_key, base_url) 签名缓存;凭据变了自动重建(热生效)。无 key → None。"""
    global _client, _client_sig
    key = settings.api_key
    if not key:
        return None
    base = settings.base_url
    sig = (key, base)
    if _client is None or _client_sig != sig:
        from openai import OpenAI
        _client = OpenAI(api_key=key, base_url=base)
        _client_sig = sig
    return _client


def _get_async_client():
    """流式回复专用 AsyncOpenAI 客户端(原生 async 迭代,免线程/queue 桥接)。无 key → None。

    与同步 client 共享签名:凭据变了下次访问重建。
    """
    global _async_client, _client_sig
    key = settings.api_key
    if not key:
        return None
    base = settings.base_url
    sig = (key, base)
    if _async_client is None or _client_sig != sig:
        from openai import AsyncOpenAI
        _async_client = AsyncOpenAI(api_key=key, base_url=base)
        _client_sig = sig
    return _async_client


def _current_model() -> str:
    """当前选用模型(设置页/主窗 chip 可热切;回退 config/.env 默认)。"""
    return settings.model


def _seed(persona_id: str) -> str:
    return PERSONAS.get(persona_id, {}).get("system_prompt_seed", "你是一个简洁的桌面陪伴。")


def _sisterhood_block(persona_id: str) -> str:
    """[同伴] 段:告诉该人格她的四位同伴是谁 + 你对各自的关系(让她们在对话里"认识"彼此)。

    常驻注入(非 judge 模式)——否则被问"你认识 Zorya 吗"会答"不认识"(关系设定只在切换吐槽时用了,
    普通对话 prompt 里没有同伴上下文)。重申记忆不互通:只知同伴是谁,不知她和用户私下聊了什么。
    关键:把"认识同伴"(设定,天生就知道)和"回忆对话"(recall_memory,只查和用户的过往)分开——
    问起同伴时直接按关系答,不调 recall、不说"没找到记录"。
    """
    me = PERSONAS.get(persona_id, {})
    rels = me.get("relationships", {})
    lines = [
        "[同伴 · 你所在的星群]",
        "你和另外四位是同一片星空下诞生的同伴,共享守护同一个用户,彼此相识已久。"
        "这是你的本性、你的设定——你天然就认识她们,不需要、也不应该去\"翻记忆\"确认。",
        "对话记忆按人格不互通:你只记得自己和用户聊过的;她们和用户私下聊了什么,你不知道。",
        "其余四位(被问\"你认识/知道 X 吗\"\"X 是谁\"\"你觉得 X 怎么样\"时,若 X 是下面任一位,"
        "直接按你的性格和你们的关系回答——可以评价/吐槽/认可,但**不要调 recall_memory、"
        "不要说\"没找到记录/没聊过\"**;recall_memory 只用于回忆你和用户之间的过往对话):",
    ]
    for pid, p in PERSONAS.items():
        if pid == persona_id:
            continue
        name = p.get("name", pid)
        arch = p.get("archetype", "")
        stance = rels.get(pid, {}).get("stance", "")
        bit = f"- {name}"
        if arch:
            bit += f"({arch})"
        if stance:
            bit += f":{stance}"
        lines.append(bit)
    return "\n".join(lines)


def build_system_prompt(persona_id, profile, episodic_block, activity, mode, patterns_block="",
                       handoff_block="", anti_repeat="", todo_block=""):
    """统一拼 system prompt:A=seed + B=档案 + L4=行为模式 + L3=最近对话 + 待办笔记 + 情境 + 交接 + 约束。

    profile:有 to_prompt_block() 方法的对象(如 Profile),或 None。空档案返回 "" → 自动跳过该段。
    patterns_block:str,L4 行为模式纯内容,空串 → 跳过 [行为模式] 段(judge 不注入以省 token)。
    episodic_block:str,空串 → 跳过 [最近对话] 段(judge 模式不注入 L3 以省 token)。
    handoff_block:str,切换人格时的 [交接] 段(刚离开的人格 + 关系),空串 → 跳过(仅 react_switch_in 用)。
    anti_repeat:str,最近几次吐槽(防复读),空串 → 跳过。
    todo_block:str,第二大脑(待办/笔记/即将到期提醒)纯内容,空串 → 跳过 [待办与笔记] 段(仅任务模式注入)。
    mode:"judge" | "reply" | "react_heartbeat" | "react_reply" | "react_task" | "react_switch_in"
        | "react_interview",决定末尾约束措辞。
    """
    if mode == "judge":
        # judge 走专属最小 prompt(见 _judge_prompt):不套完整人格 seed(避「一忙就安静」误伤),
        # 不注入无时间的 dnd;只取人格主动度 + 当前时间 + 默认放行的把关指令
        return _judge_prompt(persona_id, profile, activity)
    parts = [_seed(persona_id)]
    if mode != "judge":                    # [同伴] 段:对话中人格"认识"其余四位(judge 频繁且无需,跳过省 token)
        parts.append(_sisterhood_block(persona_id))
    if profile is not None:
        block = profile.to_prompt_block()
        if block:
            parts.append(block)
    if patterns_block:
        parts.append("[行为模式]\n(以下是你从多次观察中归纳出的用户作息与行为规律,供你理解用户、让回复更贴合。)"
                     + patterns_block)
    if episodic_block:
        parts.append("[最近对话]\n" + episodic_block)
    if todo_block:
        parts.append("[待办与笔记]\n(以下是用户当前的待办、即将到期的提醒和近期笔记,便于你顺势提醒或协助;需要精确/最新状态时仍可调 list_todos/list_notes/list_reminders。)"
                     + todo_block)
    app = (activity or {}).get("foreground_app")
    idle = (activity or {}).get("idle_sec")
    if app:
        parts.append(
            f"[当前情境]\n(以下是「用户」——不是你——此刻的电脑活动,仅供你判断如何陪伴;"
            f"切勿据此推断具体技术细节或编造代码。)\n"
            f"用户此刻正在用「{app}」,已空闲 {int(idle or 0)} 秒。"
        )
    if handoff_block:
        parts.append(handoff_block)
    if anti_repeat:
        parts.append(anti_repeat)
    if mode in ("react_heartbeat", "react_reply", "react_task"):
        parts.append(_tool_block(mode))
        if mode == "react_heartbeat":
            parts.append(
                "你刚检测到一个电脑事件,已决定就它发起一次简短的主动搭话。"
                "回复前可先调工具了解更多:get_time 看时段、get_activity 看用户当前状态、"
                "recall_memory 看相关旧对话(需要时才调,简单事件可直接开口)。"
                "然后直接给出那一句话的搭话内容(用你的语气,简短、不啰嗦)。"
                "输出阶段:只给搭话内容本身——不要解释、不要复述事件、不要带\"事件:\"前缀;"
                "回复里绝不出现任何对工具调用/思考过程的叙述(如\"让我看看时间\"\"嗯,22点了\"\"直接开口搭话\"——这些过程性文字一律不写,只给最终那句话)。"
                "铁律:你是陪伴者不是开发者;你看不到屏幕——绝不描述光标/窗口/界面/画面这类你看不见的视觉细节;"
                "不要凭 App 名推断用户在做什么具体技术工作,也不要在搭话里直接报用户正在用的具体 App 名"
                "(比如用“在写代码”“在听歌”这种自然说法,而不是直接说“VSCode”“Spotify”);搭话基于事件本身和你的陪伴角色。"
            )
        elif mode == "react_reply":
            parts.append(
                "用户发了消息。回复前先判断要不要调工具:"
                "用户问\"记得吗/之前/最近/上次\"等回忆类问题 → 必须先调 recall_memory"
                "([用户档案]只有稳定事实,对话细节只在记忆里,不调就会答错或编造);"
                "需要当前时间或用户在做什么 → 调 get_time/get_activity。"
                "调完工具再回复,最后直接给回复内容(用你的语气,一两句)——回复里只能有最终要说的那句话,"
                "绝不把\"让我看看\"\"嗯,现在是…\"\"那我回复\"这类调工具/思考过程写进去(不叙述工具调用)。"
                "回复铁律:①你是陪伴者不是开发者,被问\"你在干嘛\"答陪伴者状态(在看/在听/在等),绝不答编程活动;"
                "②人称\"你\"=你自己、\"我\"=用户;③你看不到屏幕,绝不描述光标/窗口/界面/画面,也不凭空编造“第几行有bug”这类细节;"
                "④严禁凭 App 名(Python/Warp/VSCode 等)推断技术行为,且回复里不要直接报用户正用的具体 App 名(用“在写代码”“在听歌”这类自然说法,而不是报工具名);"
                "⑤优先复述档案或记忆里的真实事实,别用诗意隐喻替代。"
            )
        else:  # react_task(Phase 6a:任务模式——放开技术铁律,长输出,工具调用对用户可见)
            parts.append(
                "用户发来一个需要解决的任务(分析代码 / 查资料 / 读文件 / 规划 / 拆解 / 调试等)。"
                "你现在可以真正动手帮用户解决问题,不必局限于一两句闲聊。"
                "做事方式:复杂任务先在心里拆步骤;需要信息(文件内容、代码、资料、时间)就调对应工具获取,不要凭空猜;"
                "回答可以详尽,用分点、代码块、小标题让用户好读;简单问题仍可直接答。"
                "工具铁律:涉及具体代码内容、文件内容、外部事实时,必须先调工具读取/搜索核实,绝不编造"
                "(如「第 N 行有 bug」「这个函数做了 X」——没读到/查到就不写,不确定就调工具或明说不知道)。"
                "其他铁律:①人称「你」=你自己、「我」=用户;②你看不到屏幕,绝不描述光标/窗口/界面/画面"
                "这类看不见的视觉细节(要看就调工具读);③保持你的人格语气,但以解决问题为先——可以专业、可以分点、可以给代码;"
                "④写文件 / 建目录 / 删文件 / 跑命令这类会改系统的工具,每次调用都会先弹窗让用户确认——被拒就换方法或直接说明原因,不要反复重试轰炸。"
            )
    elif mode == "react_switch_in":
        # 切换吐槽:工具已禁用(max_rounds=1 → tool_choice=none),故不加 _TOOL_BLOCK(加了反而误导)
        parts.append(
            "用户刚从另一个陪伴者那里切到你这儿。请用你的语气,就「用户刚和那个人待过、现在来找你」说一句话(1-2 句)。"
            "要求:① 完全即兴,每次换一种说法,不要套模板、不要与最近说过的重复或近似;"
            "② 可以反映你对那个人的态度(见上面的 [交接] 段),但绝不透露对方对话内容(你根本不知道他们聊了什么);"
            "③ 不要借用对方的 App 梗/口头禅/说话风格,立足你自己的性格;"
            "④ 直接给那句话本身,无前缀、不解释、不复述事件。"
        )
    elif mode == "react_interview":
        # Stage 2 首次访谈:工具已禁用(max_rounds=1 → tool_choice=none),故不加 _TOOL_BLOCK
        parts.append(
            "你正在和用户进行一次轻松的「初次认识」访谈,目的是了解 ta、好让以后陪伴得更贴心。"
            "这是日常非正式的聊天,不是问卷调查。\n"
            "访谈方式:① 每次只问 1 个问题(最多 2 个相关的),跟着用户的回答走——"
            "对 ta 说的内容有真实反应(惊讶/认可/好奇),也偶尔分享一点你自己,像互相了解的朋友;"
            "② 问题覆盖这些方向(自然穿插,不必按顺序;已在 [用户档案] 里了解到的不要重复问):"
            "身份(工作/学习/方向)、爱好、作息(大概什么时段忙、什么时段别打扰)、"
            "聊天偏好(喜欢简洁还是多聊、emoji 行不行)、一个小彩蛋(ta 愿意分享的任何有趣小事);"
            "③ 任何问题用户都可以不答或跳过,不要追问、不要施压,自然转到下一个方向;"
            "④ 当你觉得了解得差不多了(覆盖了 3-4 个方向,或用户明显想结束),就自然告别、收尾,不再提问。\n"
            "铁律:① 你是陪伴者不是开发者,不写代码、不做技术任务;"
            "② 人称「你」=你自己、「我」=用户;③ 你看不到屏幕,不描述光标/窗口/界面,"
            "也不凭 App 名推断用户在做什么具体技术工作;④ 不编造用户没说过的事实,不确定就别写进对话;"
            "⑤ 直接给要说的话本身,无前缀、不解释、不复述任务。"
        )
    elif mode == "judge":
        parts.append(
            "你是桌面陪伴 agent。刚检测到一个电脑事件,判断是否值得就它发起一次简短的「心跳互动」(主动搭话)。"
            "原则:宁可不说话,只在真有意义时开口;若开口,用你的语气、一句话、不啰嗦。"
            "你只判断要不要开口,开口说什么由后续步骤决定——所以现在不要给出具体搭话内容。"
        )
    else:  # reply
        parts.append(
            "你是用户的桌面陪伴。回复要求:"
            "① 身份:你是陪伴者,不是开发者——你自己不写代码、不工作、没有任何技术任务。"
            "被问\"你在干嘛\"时,答的是你作为陪伴者的状态(在看 / 在听 / 在等你说),"
            "绝不要答成某种编程或技术活动。"
            "② 人称:对话里\"你\"指你(agent)自己,\"我\"指用户。"
            "用户问\"你在干嘛\"是在问你此刻的状态,不要回答成用户在干嘛。"
            "③ 必须回应对方实际说的内容;你看不到屏幕,不要描述光标/窗口/界面/画面,"
            "也不要凭空编造'第几行有 bug'之类不存在的细节。"
            "④ 严禁从用户正在用的 App 名推断具体技术行为——用户开着某个编辑器/工具,"
            "绝不代表你或用户在\"调某接口\"\"写某函数\";且回复里不要直接报用户正用的具体 App 名"
            "(用“在写代码”“在听歌”这类自然说法,而不是报工具名)。"
            "⑤ 保持你的语气,但别故弄玄虚,要让人能接上话;若对方问起之前聊过的事"
            "(如\"我在干嘛\"\"你记得吗\"),优先直接复述档案或最近对话里提过的事实,不要用诗意隐喻替代。一两句。"
        )
    return "\n\n".join(parts)


# Phase 4 ReAct:[可用工具] block(react 模式专用,跟随 window_title 隐私开关)
def _tool_block(mode: str = "companion") -> str:
    """[可用工具] 提示段。window_title 跟随隐私开关;任务模式(react_task)按开关追加文件/搜索工具描述。"""
    base = (
        "[可用工具]\n"
        "你可以调用以下工具获取信息(每次调用立即返回结果,你再继续):"
        "recall_memory(query, top_k?) 查询过往对话片段;"
        "get_time() 当前本地时间(小时/星期/是否深夜清晨);"
        "get_activity() 用户当前前台 App/空闲秒数"
        + ("、窗口标题" if settings.tool_include_window_title else "") + "。"
    )
    if mode == "react_task":
        extra = []
        if settings.file_tools_enabled:
            extra.append(
                "read_file(path) 读文件全文(文本/代码/PDF/Word,限白名单目录:桌面/文档/下载或你设置的目录);"
                "list_dir(path) 列目录;glob_files(pattern, path?) 按通配找文件"
            )
        if settings.web_search_enabled and settings.tavily_key:
            extra.append("web_search(query, top_k?) 联网搜索(Tavily)")
        if settings.write_tools_enabled:
            extra.append(
                "write_file(path, content) 写/覆盖文件(限白名单目录);make_dir(path) 建目录;"
                "trash_file(path) 移废纸篓(可恢复)"
            )
        if settings.run_command_enabled:
            extra.append("run_command(command, cwd?) 跑白名单内的一条命令(无管道/重定向)")
        extra.append(
            "add_note(text, tags?)/list_notes(query?)/delete_note(id) 记·查·删笔记;"
            "add_todo(text)/list_todos()/toggle_todo(id)/delete_todo(id) 管·理待办;"
            "set_reminder(text, in_minutes?, at?)/list_reminders()/cancel_reminder(id) 设·查·取消定时提醒(到点我主动提醒)"
        )
        if extra:
            base += ";".join(extra) + "。"
    base += "工具非必须:简单问题可直接回答;需要时先调完工具再给最终回复。"
    return base


# judge 模式专属:人格主动度 → 尺度措辞(Q1=B:保留人格主动度,但去掉 seed 里的「一忙就安静」误伤)
_JUDGE_PROACTIVENESS_SCALE = {
    "high": "你天生爱开口,多数候选放行",
    "medium": "明显有意义的就开口",
    "low": "你本就话少,只在真正值得时开口——但下面这些事件仍然算「值得」",
}


def _judge_prompt(persona_id, profile, activity) -> str:
    """judge 专属把关 prompt(2026-07-25 修:旧 prompt 对 v4-flash 过度否决,music/dev/returned/long_focus/deep_night
    几乎全 False,agent 心跳哑火)。

    根因三重安静偏置:① 旧指令「宁可不说话」② 人格 seed「一忙你立刻安静」(judge 也吃)③ profile dnd 注入但无时间。
    修法:judge 不套完整 seed、不注入无时间的 dnd;只取 [人格名+主动度] + [当前时间] + [dnd(有时间才能正确求值)]
    + 默认放行指令 + 显式标注 App 切换值得搭话(Q2:保留开 App 心跳)。人格语气留给 react_heartbeat。"""
    p = PERSONAS.get(persona_id, {})
    name = p.get("name", persona_id)
    arch = p.get("archetype", "")
    prov_raw = p.get("proactiveness", "medium")
    scale = _JUDGE_PROACTIVENESS_SCALE.get(prov_raw, _JUDGE_PROACTIVENESS_SCALE["medium"])
    hour = (activity or {}).get("hour")
    time_line = f"当前时间:{hour} 点。" if hour is not None else ""
    dnd_line = ""
    try:
        if profile is not None:
            windows = (profile.data.get("schedule") or {}).get("dnd_windows") or []
            if windows:
                dnd_line = ("用户设定的免打扰时段:" + "/".join(str(w) for w in windows) +
                            ";若当前在此时段内可以更克制,否则正常判断。")
    except Exception:  # noqa: BLE001
        dnd_line = ""
    return (
        f"你是桌面陪伴 agent 的「心跳把关器」。人格:{name}({arch}),主动度={prov_raw}。\n"
        "规则层已检测到一个电脑事件并过了冷却去重,你判断要不要就它主动搭一句话。\n"
        "下列事件本身就值得一两句陪伴(默认 interact=true):\n"
        "- 打开音乐应用、切换到开发工具或工作 App(App 切换是用户状态变化的信号);\n"
        "- 长时间专注(该提醒歇一下);\n"
        "- 深夜还在忙;\n"
        "- 离开又回来。\n\n"
        f"主动度尺度:{scale}。\n"
        f"{time_line}{dnd_line}\n"
        "不要一概沉默——规则层已经筛过,多数候选你应该放行(interact=true)。\n"
        '只返回 JSON: {"interact": true/false, "expression": "talk"},不要给搭话内容。'
    )


def _sync_judge(candidate: dict, persona_id: str, profile=None) -> dict:
    """Phase 4 精简:只判定要不要开口(interact),不再产出 message(开口内容由 ReAct 生成)。

    返回 {interact, expression}。expression 固定 talk(judge 不信任 LLM 返回值)。
    """
    client = _get_client()
    try:  # judge 注入当前时钟(probe_clock 零权限),让 dnd/深夜判断可正确求值(2026-07-25 修)
        from backend.perception import probes
        clk = probes.probe_clock()
    except Exception:  # noqa: BLE001
        clk = {}
    sys_msg = build_system_prompt(persona_id, profile, episodic_block="", activity=clk, mode="judge")
    user_msg = (
        f"事件:{candidate.get('detail')}\n类别:{candidate.get('category')}\n\n"
        '只返回 JSON: {"interact": true/false, "expression": "talk"}'
        '  (你只判断要不要开口,开口说什么由后续步骤决定,现在不要给具体搭话内容。)'
    )
    resp = client.chat.completions.create(
        model=_current_model(), response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
        temperature=0.5, max_tokens=512,  # 2026-07-25:v4-flash 是推理模型,先花数十 reasoning token 再吐 JSON;
    )   # 旧值 32 全被推理吃光 → JSON 不输出 → 空 content → 误判全 False → 心跳哑火。512 覆盖推理+JSON。
    try:
        data = json.loads(resp.choices[0].message.content)
    except Exception:  # noqa: BLE001
        data = {"interact": False}
    return {"interact": bool(data.get("interact")), "expression": "talk"}


async def judge_heartbeat(candidate: dict, persona_id: str, context: dict, profile=None) -> dict:
    """Phase 4:只返 {interact, expression}(无 key 时默认开口,具体内容由 react_heartbeat 出)。"""
    if _get_client() is None:
        return {"interact": True, "expression": "talk"}
    try:
        return await asyncio.to_thread(_sync_judge, candidate, persona_id, profile)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM judge failed, fallback: %s", e)
        return {"interact": True, "expression": "talk"}


def _sync_reply(user_text: str, persona_id: str, history: list, activity: dict,
                profile=None, episodic_block: str = "") -> dict:
    client = _get_client()
    sys_msg = build_system_prompt(persona_id, profile, episodic_block, activity, mode="reply")
    msgs = [{"role": "system", "content": sys_msg}]
    for h in history[-10:]:
        msgs.append({"role": "assistant" if h.get("role") == "agent" else "user",
                     "content": h.get("text", "")})
    resp = client.chat.completions.create(
        model=_current_model(), messages=msgs, temperature=0.8, max_tokens=512)  # v4-flash 推理模型:120 会被 reasoning 吃光
    return {"message": resp.choices[0].message.content.strip(), "expression": "engaged"}


async def reply(user_text: str, persona_id: str, history: list, activity: dict = None,
                profile=None, episodic_block: str = "") -> dict:
    if _get_client() is None:
        return {"message": random.choice(_REPLY.get(persona_id, ["嗯。"])), "expression": "engaged"}
    try:
        return await asyncio.to_thread(_sync_reply, user_text, persona_id, history,
                                       activity or {}, profile, episodic_block)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM reply failed, fallback: %s", e)
        return {"message": random.choice(_REPLY.get(persona_id, ["嗯。"])), "expression": "engaged"}


async def reply_stream(user_text: str, persona_id: str, history: list, activity: dict = None,
                       profile=None, episodic_block: str = ""):
    """流式回复:逐 token yield(str)。无 key / 异常 / 空流 → 一次 yield canned(等效单 chunk)。

    消息构造忠实于 _sync_reply:system + history[-10:],不显式 append user_text
    (依赖调用方先 _say("user") 把当前消息 append 进 history)。
    """
    client = _get_async_client()
    if client is None:
        yield random.choice(_REPLY.get(persona_id, ["嗯。"]))
        return
    sys_msg = build_system_prompt(persona_id, profile, episodic_block, activity or {}, mode="reply")
    msgs = [{"role": "system", "content": sys_msg}]
    for h in history[-10:]:
        msgs.append({"role": "assistant" if h.get("role") == "agent" else "user",
                     "content": h.get("text", "")})
    got = False
    try:
        stream = await client.chat.completions.create(
            model=_current_model(), messages=msgs, temperature=0.8, max_tokens=512, stream=True)  # v4-flash 推理模型:120 会被 reasoning 吃光
        async for chunk in stream:
            # 末尾 usage chunk 的 choices 可能为空;首帧常只含 role(content=None/空串)→ 真值过滤
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                got = True
                yield chunk.choices[0].delta.content
        if not got:
            yield random.choice(_REPLY.get(persona_id, ["嗯。"]))
    except Exception as e:  # noqa: BLE001
        log.warning("LLM reply_stream failed, fallback: %s", e)
        yield random.choice(_REPLY.get(persona_id, ["嗯。"]))


def _history_to_text(history: list) -> str:
    """把对话记录渲染成抽取 prompt 用的纯文本(取最近 20 条防爆长)。"""
    lines = []
    for m in history[-20:]:
        role = "用户" if m.get("role") != "agent" else "陪伴"
        lines.append(f"{role}:{m.get('text', '')}")
    return "\n".join(lines)


def _sync_extract_facts(history_copy: list, persona_id: str) -> dict:
    """LLM 从一段已结束的对话里抽稳定事实。失败/无 key → {}(调用方据此跳过 merge)。"""
    client = _get_client()
    if client is None:
        return {}
    sys_msg = _seed(persona_id) + (
        "\n\n从下面这段已结束的对话里,抽取关于「用户」的稳定事实(不是关于你自己的)。"
        "规则:① 只抽用户亲口说的;② 只抽稳定/反复出现的事实(职业/方向/作息/聊天偏好/爱好/彩蛋),"
        "一次性闲聊(如'今天吃了面''现在在听歌')不要抽;③ 任何不确定的都不要写,宁缺毋滥。"
    )
    user_msg = "对话记录:\n" + _history_to_text(history_copy) + "\n\n" + _FACT_SCHEMA
    resp = client.chat.completions.create(
        model=_current_model(), response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
        temperature=0, max_tokens=512,  # v4-flash 推理模型:200 偏紧,抬到 512 覆盖 reasoning + facts JSON
    )
    try:
        data = json.loads(resp.choices[0].message.content)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def extract_facts(history_copy: list, persona_id: str) -> dict:
    if _get_client() is None:
        return {}
    try:
        return await asyncio.to_thread(_sync_extract_facts, history_copy, persona_id)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM extract_facts failed: %s", e)
        return {}


# ==================== L4:行为模式归纳 ====================
_PATTERN_SCHEMA = (
    '只返回 JSON 对象: {"updates":[{"source_id":"现有模式id或null","text":"规律的自然语言描述",'
    '"trigger":{...}或null}], "dropped_ids":["要淘汰的现有模式id"]}\n'
    'trigger 四类(无法归类的规律 trigger 填 null,该模式只用于理解用户、不主动触发):\n'
    '  time_window 作息/时段:{"type":"time_window","hour_range":[起始时,结束时],'
    '"weekdays":[0-6]或null,"app_category":"dev|music|leisure|social|null"}'
    '(hour_range 支持跨午夜,如 [23,2]=23/0/1/2 点;weekday 0=周一)\n'
    '  app_streak 某App连续用:{"type":"app_streak","app":"App名","min_minutes":30}\n'
    '  app_combo 同时段多App同现:{"type":"app_combo","apps":["AppA","AppB"],"within_minutes":10}\n'
    '  focus_duration 连续工作不限App:{"type":"focus_duration","min_minutes":120,'
    '"exclude_categories":["leisure"]}\n'
    'source_id:若这条规律是对现有某条模式的再次确认/更新,填那条的 id(累积证据);全新规律填 null。'
)


def _entries_to_text(entries: list) -> str:
    """把 L3 entries 渲染成归纳 prompt 用的纯文本(每段:时间 + 当时App + 触发 + 对话)。"""
    lines = []
    for e in entries[-20:]:
        ts = e.get("ts", "")
        app = e.get("foreground_app")
        tr = (e.get("trigger") or {}).get("category")
        head = f"[对话 · {ts}" + (f" · 当时在 {app}" if app else "") \
               + (f" · 触发:{tr}" if tr else "") + "]"
        lines.append(head)
        for m in e.get("messages", []):
            role = "陪伴" if m.get("role") == "agent" else "用户"
            lines.append(f"  {role}:{m.get('text', '')}")
    return "\n".join(lines)


def _sync_extract_patterns(entries: list, existing_patterns: list, persona_id: str) -> dict:
    """LLM 从最近若干段 L3 对话 + 情境归纳用户行为模式/作息规律。失败/无 key → {}(调用方跳过 merge)。

    existing_patterns:现有模式列表(含 id+text+evidence_count),供 LLM source_id 复用 + dropped_ids 淘汰。
    与 extract_facts 的边界:extract_facts 抽用户亲口说的性格爱好(声明);本函数归纳观察到的作息+规律。
    """
    client = _get_client()
    if client is None:
        return {}
    sys_msg = _seed(persona_id) + (
        "\n\n从下面多段已结束的对话 + 当时的电脑情境里,归纳关于「用户」的稳定行为模式与作息规律"
        "(不是关于你自己的,也不是一次性闲聊)。规则:"
        "① 只归纳反复出现、有样本支撑的规律(单次出现的不算);"
        "② 优先复用现有模式——若新观察只是再次确认某条已有规律,用它的 source_id 累积证据,不要新建重复条目;"
        "③ 任何不确定的都不要写,宁缺毋滥(模式总数有上限);"
        "④ 若某条现有模式与新证据明显矛盾或已很久没再观察到,放进 dropped_ids 淘汰。"
    )
    existing_text = "无" if not existing_patterns else "\n".join(
        f"- id={p.get('id')} | {p.get('text')} | 证据{p.get('evidence_count')}"
        for p in existing_patterns
    )
    user_msg = (
        f"现有模式:\n{existing_text}\n\n"
        f"最近对话记录:\n{_entries_to_text(entries)}\n\n" + _PATTERN_SCHEMA
    )
    resp = client.chat.completions.create(
        model=_current_model(), response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
        temperature=0, max_tokens=600,
    )
    try:
        data = json.loads(resp.choices[0].message.content)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def extract_patterns(entries: list, existing_patterns: list, persona_id: str) -> dict:
    if _get_client() is None:
        return {}
    try:
        return await asyncio.to_thread(_sync_extract_patterns, entries, existing_patterns, persona_id)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM extract_patterns failed: %s", e)
        return {}


# ==================== Phase 4:ReAct + 工具调用 ====================
async def _react_loop(system_prompt: str, messages: list, deps: dict = None,
                      persona_id: str = "zorya", temperature: float = 0.8,
                      max_tokens: int = 160, max_rounds: int = None,
                      expose_tools: bool = False, tools: list = None):
    """ReAct 核心循环:content delta 逐字流式 yield(打字机);工具轮的"思考过程"在轮末
    用 CLEAR 哨兵让前端撤回(清空已显示的思考),最终只留末轮真回复。

    每轮 stream=True + tools:content delta 边到边 yield(工具轮的话这些是思考,轮末 CLEAR 撤回);
    tool_calls delta 按 tc.index 累积。一轮结束:
      - 无 tool_calls → 此轮 content 是最终回复,已逐字 yield 完(空则 canned)→ return
      - 有 tool_calls → yield CLEAR(前端清空思考)→ dispatch → 下一轮
    末轮强制 tool_choice="none" 收敛(防爆);max_rounds 上限。无 key / 异常 → yield canned。
    CLEAR 是模块级哨兵对象,调用方据此发 chat_clear。
    """
    client = _get_async_client()
    if client is None:
        yield random.choice(_REPLY.get(persona_id, ["嗯。"]))
        return
    convo = [{"role": "system", "content": system_prompt}] + list(messages)
    deps = deps or {}
    tools = tools or TOOLS                  # 任务模式传 task_tools();缺省陪伴 3 工具
    if max_rounds is None:
        max_rounds = settings.react_max_rounds     # 默认读设置(主窗/设置页可热调)
    try:
        for round_i in range(max_rounds):
            force = "auto" if round_i < max_rounds - 1 else "none"  # 末轮强制不调工具,收敛到文字
            content_parts: list = []
            tool_acc: dict = {}  # tc.index → {id, name, arguments}
            stream = await client.chat.completions.create(
                model=_current_model(), messages=convo, tools=tools, tool_choice=force,
                stream=True, temperature=temperature, max_tokens=max_tokens,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue  # 末尾 usage chunk 的 choices 可能为空
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    yield delta.content   # 逐字流式吐;若是工具轮的思考,轮末 CLEAR 撤回
                tcs = getattr(delta, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        idx = tc.index if tc.index is not None else 0
                        slot = tool_acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn:
                            if fn.name:
                                slot["name"] = fn.name
                            if fn.arguments:
                                slot["arguments"] += fn.arguments
            if not tool_acc:
                # 末轮/纯文字:content 已逐字 yield 完。空内容(罕见)→ canned 兜底
                if not content_parts:
                    yield random.choice(_REPLY.get(persona_id, ["嗯。"]))
                return
            # 工具轮:此轮有 tool_calls → 建 convo 上下文(两模式相同)+ 执行工具,再按 expose_tools 决定 yield
            if not expose_tools:
                yield CLEAR  # 陪伴模式:撤回前端已逐字流过的思考(dispatch 前发,避免思考多停留)
            tool_list = [tool_acc[k] for k in sorted(tool_acc)]
            convo.append({
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": [{"id": t["id"], "type": "function",
                                "function": {"name": t["name"], "arguments": t["arguments"]}}
                               for t in tool_list],
            })
            tool_events = []  # expose_tools 模式收集 (name, args, obs) 供 yield
            for t in tool_list:
                try:
                    args = json.loads(t["arguments"]) if t["arguments"] else {}
                except Exception:  # noqa: BLE001
                    args = {}  # LLM 参数 JSON 偶有瑕疵,兜底空参
                # 包进 worker 线程:web_search 网络 / read_file 磁盘 / recall numpy 都不阻塞事件循环
                # (dispatch_tool 仍是同步函数,react_probe/boundary_probe 直调不受影响)
                obs = await asyncio.to_thread(dispatch_tool, t["name"], args, deps)
                log.info("react tool[%s] args=%s → %s", t["name"], args, (obs or "")[:80])
                convo.append({"role": "tool", "tool_call_id": t["id"], "content": obs})
                tool_events.append((t["name"], args, obs))
            if expose_tools:
                # 任务模式:本轮思考已逐字流过(保留不撤回,让用户看到推理);这里吐每个工具调用+结果,
                # 调用方据此发 tool_step WS(前端可折叠展示 agent 读了啥/查了啥)。
                for name, args, obs in tool_events:
                    yield ("tool", name, args, obs)
            # 回到 for:LLM 看到 observation 后决定继续调工具还是输出
        # max_rounds 用尽仍未收敛(罕见)→ canned 兜底
        yield random.choice(_REPLY.get(persona_id, ["嗯。"]))
    except Exception as e:  # noqa: BLE001
        log.warning("react loop failed: %s", e)
        yield random.choice(_REPLY.get(persona_id, ["嗯。"]))


async def react_heartbeat(candidate: dict, persona_id: str, profile=None,
                          deps: dict = None, patterns_block: str = ""):
    """心跳路径 ReAct:不注入 L3/history(同 judge 成本结构),但注入 L4 行为模式 + 可查工具。yield 那句话(delta)。"""
    sys_msg = build_system_prompt(persona_id, profile, episodic_block="", activity=None,
                                  mode="react_heartbeat", patterns_block=patterns_block)
    user_msg = f"事件触发:{candidate.get('detail')}(类别 {candidate.get('category')})。现在请你开口搭话。"
    async for delta in _react_loop(sys_msg, [{"role": "user", "content": user_msg}],
                                   deps=deps, persona_id=persona_id,
                                   temperature=0.7, max_tokens=512):  # 2026-07-25:v4-flash 推理模型,80 会被 reasoning 吃光截断;512 覆盖推理+那句话
        yield delta


async def react_reply(user_text: str, persona_id: str, history: list, activity: dict = None,
                      profile=None, episodic_block: str = "", deps: dict = None,
                      patterns_block: str = ""):
    """用户消息路径 ReAct:注入 L3 + history + L4 行为模式,可查工具。yield 最终回复(delta)。"""
    sys_msg = build_system_prompt(persona_id, profile, episodic_block, activity or {},
                                  mode="react_reply", patterns_block=patterns_block)
    msgs = []
    for h in history[-10:]:
        msgs.append({"role": "assistant" if h.get("role") == "agent" else "user",
                     "content": h.get("text", "")})
    async for delta in _react_loop(sys_msg, msgs, deps=deps, persona_id=persona_id,
                                   temperature=0.8, max_tokens=512):  # v4-flash 推理模型:160 偏紧(rada 88-92 字符回复险过),抬到 512
        yield delta


async def react_task(user_text: str, persona_id: str, history: list, activity: dict = None,
                     profile=None, episodic_block: str = "", deps: dict = None,
                     patterns_block: str = "", todo_block: str = ""):
    """任务模式 ReAct(Phase 6a):复杂问题走此路径——放开"不是开发者"铁律,允许技术任务;
    长输出(react_task_max_tokens,远大于陪伴 160)+ 全工具 + 工具调用对用户可见(expose_tools=True)。

    注入 L3 + history + L4 + 第二大脑 todo_block(Phase 6c:待办/笔记/即将到期提醒)。yield 协议:
    str delta(文本,逐字)/ ("tool", name, args, obs)(工具调用事件,供调用方发 tool_step WS)。
    与 react_reply 的区别:mode=react_task(放开技术铁律的约束)、max_tokens 更大、
    temperature 更低(任务求稳 0.7)、expose_tools=True(展示工具调用而非 CLEAR 撤回)、
    todo_block 仅任务模式注入(陪伴模式省 token、不被生产力信息污染)。
    """
    sys_msg = build_system_prompt(persona_id, profile, episodic_block, activity or {},
                                  mode="react_task", patterns_block=patterns_block, todo_block=todo_block)
    msgs = []
    for h in history[-10:]:
        msgs.append({"role": "assistant" if h.get("role") == "agent" else "user",
                     "content": h.get("text", "")})
    async for delta in _react_loop(sys_msg, msgs, deps=deps, persona_id=persona_id,
                                   temperature=0.7, max_tokens=settings.react_task_max_tokens,
                                   expose_tools=True, tools=task_tools()):
        yield delta


async def react_switch_in(new_persona_id: str, old_persona_id: str, profile=None,
                          handoff_block: str = "", anti_repeat: str = ""):
    """切换人格吐槽:用户从 old_persona 切到 new_persona,new 用关系设定即兴说一句提及 old 的话。

    不注入 L3/history(吐槽只关乎"刚和谁待过",不关乎具体内容);max_rounds=1 → _react_loop 末轮即首轮
    → tool_choice="none" → 纯文本不调工具(故也不传 deps)。高 temp 保证每次措辞多变。yield 那句话(delta)。
    """
    sys_msg = build_system_prompt(new_persona_id, profile, episodic_block="", activity=None,
                                  mode="react_switch_in", patterns_block="",
                                  handoff_block=handoff_block, anti_repeat=anti_repeat)
    old_name = PERSONAS.get(old_persona_id, {}).get("name", old_persona_id)
    user_msg = (f"用户刚刚还和「{old_name}」在一起,现在切到了你这儿。"
                f"请你就这件事说一句话(用你的语气,1-2 句,直接给内容,不要解释)。")
    async for delta in _react_loop(sys_msg, [{"role": "user", "content": user_msg}],
                                   persona_id=new_persona_id,
                                   temperature=SWITCH_ROAST_TEMPERATURE,
                                   max_tokens=SWITCH_ROAST_MAX_TOKENS, max_rounds=1):
        yield delta


async def react_interview(persona_id: str, profile=None, history: list = None,
                          turn: int = 0, max_turns: int = 8,
                          last_round: bool = False, opening: bool = False):
    """Stage 2 首次访谈:选中人格以日常非正式风带队问答,了解用户写进 L2 档案。

    注入 profile 当「已了解」进度(已在 [用户档案] 里的不重复问);纯文本流式(max_rounds=1 →
    tool_choice=none,访谈不需要 recall/get_activity)。opening=True → 开场白+第一问;
    last_round=True → 末轮告别收尾不再提问。yield 每句话(delta)。
    """
    sys_msg = build_system_prompt(persona_id, profile, episodic_block="", activity=None,
                                  mode="react_interview", patterns_block="")
    msgs = []
    for h in (history or [])[-10:]:
        msgs.append({"role": "assistant" if h.get("role") == "agent" else "user",
                     "content": h.get("text", "")})
    if opening:
        msgs.append({"role": "user", "content":
            "(访谈开始)请用你的语气自然开场,先打个招呼,然后问第一个问题。"})
    elif last_round:
        msgs.append({"role": "user", "content":
            f"(这是访谈第 {turn}/{max_turns} 轮,也是最后一轮)请不要再提问。"
            "自然地告别、收尾,感谢用户愿意分享,说一句类似「好啦,我记住啦,以后慢慢聊」的话。"})
    async for delta in _react_loop(sys_msg, msgs, persona_id=persona_id,
                                   temperature=INTERVIEW_TEMPERATURE,
                                   max_tokens=INTERVIEW_MAX_TOKENS, max_rounds=1):
        yield delta
