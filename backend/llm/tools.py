"""ReAct 工具集。schema 给 LLM 看(TOOLS),dispatch 给本地执行(dispatch_tool)。

依赖注入:dispatch_tool 接受 deps dict(含 episodic),不持有全局状态,便于测试。
三个工具:recall_memory(向量召回 L3)/ get_time(probe_clock)/ get_activity(run_probes)。
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from typing import Any

from backend.config import (
    ATTACH_MAX_CHARS, COMMAND_DANGEROUS, COMMAND_FORBIDDEN_CHARS,
    FILE_GLOB_MAX, FILE_LIST_MAX, RECALL_TOP_K,
    REMINDER_DEFAULT_MINUTES, RUN_COMMAND_MAX_CHARS, RUN_COMMAND_TIMEOUT_SEC,
    WEB_SEARCH_TOP_K, WRITE_MAX_CHARS,
)
from backend.judgment import sandbox
from backend.memory import filereader
from backend.memory.settings import settings

log = logging.getLogger("heartbeat.llm.tools")


def _activity_title_suffix() -> str:
    """get_activity 描述里是否提及窗口标题(跟随隐私开关;读 settings 实时值)。"""
    return "、前台窗口标题" if settings.tool_include_window_title else ""


# ---- OpenAI 兼容 tool schema(DeepSeek 用同一格式)----
TOOLS: list = [
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": (
                "检索与当前话题相关的过往对话片段。当用户提到\"之前/记得吗/上次\"、"
                "或你想引用一段旧对话时调用,返回若干段对话原文。"
                "注意:稳定事实(职业/爱好/作息/聊天偏好)已在 system prompt 的[用户档案]里给出,"
                "无需调此工具;此工具只召回历史对话片段。没相关内容时会返回\"无相关历史对话\"。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词,用自然语言描述你想回忆的内容"},
                    "top_k": {"type": "integer", "description": f"返回条数,默认 {RECALL_TOP_K}", "default": RECALL_TOP_K},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前本地时间:小时(0-23)、星期几(0=周一)、是否深夜(22-6点)、是否清晨(6-9点)。需要时间相关判断时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity",
            "description": (
                "获取用户当前电脑活动状态:前台 App 名、空闲秒数"
                + _activity_title_suffix() + "。需要知道用户在做什么才能决定如何陪伴时调用。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ---- Phase 6b 任务模式扩展工具(陪伴模式不挂;task_tools() 按开关动态组装)----
_READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取一个本地文件的文本内容:支持 .txt/.md/.py/.js/.json/.yaml 等文本与代码,"
            "以及 .pdf / .docx(自动抽取文字)。仅允许读取:桌面/文档/下载 或用户在设置里添加的目录内的文件。"
            "需要某个文件的具体内容来回答时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径(绝对或相对,支持 ~)"},
            },
            "required": ["path"],
        },
    },
}

_LIST_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": (
            "列出一个目录下的文件和子目录(目录在前、按名排序,附文件大小),用来了解某处有哪些可读的内容。"
            "受同样的白名单限制(桌面/文档/下载 或用户添加的目录)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径(绝对或相对,支持 ~)"},
            },
            "required": ["path"],
        },
    },
}

_GLOB_TOOL = {
    "type": "function",
    "function": {
        "name": "glob_files",
        "description": (
            "在一个目录下按通配符匹配文件路径(如 '*.py'、'**/*.md'、'src/*.ts'),返回匹配到的文件列表。"
            "用来批量找某类文件。受白名单限制。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "通配符,如 '*.py'、'**/*.md'"},
                "path": {"type": "string", "description": "搜索的根目录(支持 ~);缺省为桌面"},
            },
            "required": ["pattern"],
        },
    },
}

_WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索(Tavily):返回若干条网页结果(标题、链接、摘要)。需要查外部资料、最新信息、"
            "或你自己不确定的事实时调用。无结果或失败会返回提示串。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "top_k": {"type": "integer", "description": f"返回条数,默认 {WEB_SEARCH_TOP_K}", "default": WEB_SEARCH_TOP_K},
            },
            "required": ["query"],
        },
    },
}

_FILE_TOOLS = [_READ_FILE_TOOL, _LIST_DIR_TOOL, _GLOB_TOOL]
_WEB_TOOLS = [_WEB_SEARCH_TOOL]


# ---- Phase 6c 第二大脑工具(任务模式专属:笔记 / 待办 / 提醒)----
_ADD_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "add_note",
        "description": "记一条笔记(用户说「记一下/记一笔/备忘」时用)。内容会持久保存,之后可按关键词查。可选加标签。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "笔记内容"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签列表(便于以后筛选)"},
            },
            "required": ["text"],
        },
    },
}

_LIST_NOTES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_notes",
        "description": "列出笔记(可按关键词筛选)。用户问「我记过什么/笔记里有没有 X」、或你要引用旧笔记时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "可选筛选词(匹配内容或标签);不给则列全部"},
            },
            "required": [],
        },
    },
}

_DELETE_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_note",
        "description": "按 id 删除一条笔记(id 从 list_notes 的结果里取)。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "笔记 id(list_notes 返回的 id)"}},
            "required": ["id"],
        },
    },
}

_ADD_TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "add_todo",
        "description": "添加一条待办(用户说「加个待办/别忘了做 X/待会儿要做 X」时用)。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "待办事项"}},
            "required": ["text"],
        },
    },
}

_LIST_TODOS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_todos",
        "description": "列出所有待办(含完成状态)。用户问「我还有什么没做/待办清单」时调用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_TOGGLE_TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "toggle_todo",
        "description": "按 id 切换一条待办的完成状态(未完成↔已完成)。用户说「这个做完了/勾掉 X」时调用。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "待办 id(list_todos 返回的 id)"}},
            "required": ["id"],
        },
    },
}

_DELETE_TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_todo",
        "description": "按 id 删除一条待办。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "待办 id"}},
            "required": ["id"],
        },
    },
}

_SET_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "设定一个定时提醒:到时间我会主动开口提醒你(用户说「X 点提醒我/N 分钟后提醒我/过会儿叫我」时用)。"
            f"时间用 in_minutes(多少分钟后)或 at(今天的 HH:MM,如 \"14:30\")二选一;"
            f"都不给则默认 {REMINDER_DEFAULT_MINUTES} 分钟后。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "提醒内容"},
                "in_minutes": {"type": "integer", "description": "多少分钟后提醒(与 at 二选一)"},
                "at": {"type": "string", "description": "今天几点提醒,HH:MM 格式(如 \"14:30\";与 in_minutes 二选一)"},
            },
            "required": ["text"],
        },
    },
}

_LIST_REMINDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": "列出所有提醒(含触发时间与是否已响)。设新提醒前可先查,避免重复设定。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_CANCEL_REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": "按 id 取消(删除)一条提醒。用户说「那个提醒不用了/取消 X」时调用。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "提醒 id(list_reminders 返回的 id)"}},
            "required": ["id"],
        },
    },
}

_NOTE_TOOLS = [
    _ADD_NOTE_TOOL, _LIST_NOTES_TOOL, _DELETE_NOTE_TOOL,
    _ADD_TODO_TOOL, _LIST_TODOS_TOOL, _TOGGLE_TODO_TOOL, _DELETE_TODO_TOOL,
    _SET_REMINDER_TOOL, _LIST_REMINDERS_TOOL, _CANCEL_REMINDER_TOOL,
]


# ---- Phase 6d 写入 / 执行工具(任务模式专用;陪伴 TOOLS 不挂;每次调用先弹窗让用户确认)----
_WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "把文本内容写入一个本地文件(整体覆盖已有文件;限白名单目录:桌面/文档/下载或设置里添加的目录)。"
            "支持文本/代码(.txt/.md/.py/.js/.json/.yaml 等)。用户要新建或修改文件时调用。"
            "每次调用都会先在主窗口弹窗让用户确认;被拒就别重试,换种方式或直接告诉用户原因。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径(绝对或相对,支持 ~)"},
                "content": {"type": "string", "description": "要写入的完整内容(会整体覆盖该文件)"},
            },
            "required": ["path", "content"],
        },
    },
}

_MAKE_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "make_dir",
        "description": (
            "创建一个目录(含必要的父目录;已存在则无影响)。限白名单目录。"
            "用户要新建文件夹、或为 write_file 准备目标位置时调用。每次调用都会先弹窗确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径(绝对或相对,支持 ~)"}},
            "required": ["path"],
        },
    },
}

_TRASH_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "trash_file",
        "description": (
            "把一个文件或空目录移到废纸篓(可恢复,不是永久删除)。限白名单目录。"
            "用户要删东西时优先用这个,别用命令行 rm。每次调用都会先弹窗确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "要删的文件/目录路径(绝对或相对,支持 ~)"}},
            "required": ["path"],
        },
    },
}

_RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "跑一条命令行命令(不经过 shell,一次只跑一条,不支持管道 | / 重定向 > / 命令拼接 &&)。"
            "首词必须在白名单内(git/ls/cat/python3/node 等常见安全命令);破坏性命令(rm/sudo/dd 等)一律拒绝。"
            "工作目录默认在桌面,或你指定的白名单内目录。返回 stdout+stderr(截断)和退出码。"
            "用户要执行命令、跑脚本、看 git 状态等时调用。每次调用都会先弹窗让用户确认,被拒就别重试。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令(单条,如 \"git status\"、\"python3 script.py\")"},
                "cwd": {"type": "string", "description": "工作目录(支持 ~;缺省=桌面;须在白名单内)"},
            },
            "required": ["command"],
        },
    },
}

_WRITE_TOOLS = [_WRITE_FILE_TOOL, _MAKE_DIR_TOOL, _TRASH_FILE_TOOL]
_EXEC_TOOLS = [_RUN_COMMAND_TOOL]


def task_tools() -> list:
    """任务模式工具集:陪伴 3 工具 + 按开关动态加文件/搜索工具 + 第二大脑工具(每次现造,反映实时设置,热切即生效)。"""
    tools = list(TOOLS)
    if settings.file_tools_enabled:
        tools += _FILE_TOOLS
    if settings.web_search_enabled and settings.tavily_key:
        tools += _WEB_TOOLS
    tools += _NOTE_TOOLS                      # Phase 6c 第二大脑:笔记/待办/提醒(写 memory/ 自洽,无条件挂)
    if settings.write_tools_enabled:          # Phase 6d:写入文件(写/建目录/移废纸篓)
        tools += _WRITE_TOOLS
    if settings.run_command_enabled:          # Phase 6d:执行命令(白名单内)
        tools += _EXEC_TOOLS
    return tools


def _denied() -> str:
    return "无权访问该路径(只允许:桌面/文档/下载,或你在设置页添加的目录)。"


def _file_tools_off() -> str:
    return "文件工具已禁用(在设置页开启)。"


def _write_tools_off() -> str:
    return "写入工具已禁用(在设置页「写入文件」开关开启)。"


def _exec_tools_off() -> str:
    return "执行命令已禁用(在设置页「执行命令」开关开启)。"


def _exec_log(deps: dict, tool: str, args_summary: str, outcome: str) -> None:
    """记一条已执行操作到 exec_log(deps 注入;无则跳过;失败静默,不阻塞工具)。"""
    lg = deps.get("exec_log")
    if lg is None:
        return
    try:
        lg.append(deps.get("persona_id", "zorya"), tool, args_summary, outcome)
    except Exception:  # noqa: BLE001
        pass


def _check_command(cmd_str: str):
    """校验命令(Phase 6d)。返 (argv_list | None, reason)——reason 非空即拒绝。

    三道防线:① 命令串不含 shell 元字符(一次一条,防 |/>/$/`;天然也无 shell 注入,因不经 shell);
    ② shlex.split 成 argv(首词 basename 不在 COMMAND_DANGEROUS → 硬拒,即便误进白名单);
    ③ 首词 basename 须在 settings.command_allowlist(空列表=全拒=run_command 实际关闭)。
    """
    import os
    s = (cmd_str or "").strip()
    if not s:
        return None, "命令为空。"
    for ch in COMMAND_FORBIDDEN_CHARS:
        if ch in s:
            return None, f"命令含被禁止的字符「{ch}」(一次只跑一条命令,不支持管道/重定向/拼接)。"
    try:
        argv = shlex.split(s)
    except ValueError as e:  # noqa: BLE001
        return None, f"命令解析失败:{e}"
    if not argv:
        return None, "命令为空。"
    binname = os.path.basename(argv[0])
    if binname in COMMAND_DANGEROUS:
        return None, f"命令「{binname}」属于破坏性/高危命令,一律拒绝。"
    allowlist = settings.command_allowlist
    if binname not in allowlist:
        shown = ", ".join(allowlist[:12]) + ("…" if len(allowlist) > 12 else "")
        return None, f"命令「{binname}」不在白名单内(允许:{shown})。"
    return argv, ""


def _trash(p) -> tuple:
    """把路径移到废纸篓(NSFileManager.trashItemAtURL,可恢复)。返 (ok, err_str)。

    失败(pyobjc 缺失/系统拒绝)→ 返 (False, err),**绝不 unlink 兜底**(不可逆操作守住)。
    """
    try:
        import objc
        from Foundation import NSFileManager, NSURL
    except Exception as e:  # noqa: BLE001
        return False, f"系统框架不可用:{e}"
    try:
        fm = NSFileManager.defaultManager()
        url = NSURL.fileURLWithPath_(str(p))
        ret = fm.trashItemAtURL_resultingItemURL_error_(url, objc.NULL, objc.NULL)
        # 传 objc.NULL 时 pyobjc 只返 BOOL;保险起见兼容 tuple 返回
        if isinstance(ret, tuple):
            success, err = bool(ret[0]), (ret[-1] if len(ret) >= 3 and ret[-1] else None)
        else:
            success, err = bool(ret), None
        if success:
            return True, ""
        return False, str(err) if err else "系统拒绝移到废纸篓"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _list_dir(path: str) -> str:
    """列目录:目录在前、按名排序、附文件大小;截断 FILE_LIST_MAX 项。"""
    from pathlib import Path
    try:
        p = Path(path).expanduser().resolve()
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except Exception as e:  # noqa: BLE001
        return f"读取目录失败:{e}"
    lines, n = [], 0
    for e in entries:
        if n >= FILE_LIST_MAX:
            lines.append(f"…(还有更多,已截断到前 {FILE_LIST_MAX} 项)")
            break
        try:
            if e.is_dir():
                lines.append(f"{e.name}/")
            else:
                lines.append(f"{e.name} ({e.stat().st_size} B)")
        except OSError:  # noqa: BLE001  # 权限等问题:仍列名,不带大小
            lines.append(e.name)
        n += 1
    return "\n".join(lines) if lines else "(空目录)"


def _glob_files(base: str, pattern: str) -> str:
    """按通配符匹配文件(相对 base 的路径);截断 FILE_GLOB_MAX,过滤掉白名单外的命中。"""
    from pathlib import Path
    if not pattern:
        return "未给出匹配模式。"
    try:
        root = Path(base).expanduser().resolve()
        matches = sorted(root.glob(pattern), key=lambda x: str(x).lower())
    except Exception as e:  # noqa: BLE001
        return f"匹配失败:{e}"
    matches = [m for m in matches if sandbox.is_allowed(m)]
    if not matches:
        return "没有匹配的文件。"
    lines = []
    for m in matches[:FILE_GLOB_MAX]:
        try:
            lines.append(str(m.relative_to(root)))
        except ValueError:  # noqa: BLE001  # 命中在根外(理论已被过滤),兜底用绝对路径
            lines.append(str(m))
    if len(matches) > FILE_GLOB_MAX:
        lines.append(f"…(还有更多,已截断到前 {FILE_GLOB_MAX} 项)")
    return "\n".join(lines)


# ---- Phase 6c 第二大脑:时间解析 + 列表格式化 helper ----
def _parse_clock_at(at, now: float):
    """解析 'HH:MM' → 今天该时刻 epoch 秒;今天此时已过则 +86400(明天同一点)。非法 → None。"""
    s = str(at).strip()
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:  # noqa: BLE001
        return None
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    lt = time.localtime(now)
    candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if candidate <= now:                  # 今天此时已过 → 顺延到明天
        candidate += 86400
    return candidate


def _fmt_fire(fire_at: float) -> str:
    """epoch → 'MM-DD HH:MM' 本地时间展示串。"""
    return time.strftime("%m-%d %H:%M", time.localtime(float(fire_at)))


def _fmt_notes(items: list) -> str:
    lines = []
    for n in items:
        tags = n.get("tags") or []
        tag = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"- {n.get('text', '')}{tag} (id={str(n.get('id', ''))[:8]})")
    return "\n".join(lines)


def _fmt_todos(items: list) -> str:
    lines = []
    for t in items:
        mark = "✓" if t.get("done") else "☐"
        lines.append(f"- {mark} {t.get('text', '')} (id={str(t.get('id', ''))[:8]})")
    return "\n".join(lines)


def _fmt_reminders(items: list) -> str:
    lines = []
    for r in items:
        flag = " ·已响" if r.get("fired") else ""
        when = _fmt_fire(float(r.get("fire_at") or 0))
        lines.append(f"- {r.get('text', '')} @ {when}{flag} (id={str(r.get('id', ''))[:8]})")
    return "\n".join(lines)


def dispatch_tool(name: str, args: dict, deps: dict) -> str:
    """本地执行工具,返回给 LLM 的字符串观测。失败 → 返回 'tool error: ...'(不抛,LLM 可据此放弃)。"""
    try:
        if name == "recall_memory":
            episodic = deps.get("episodic")
            if episodic is None:
                return "no memory available"
            hits = episodic.recall(args.get("query", ""), top_k=args.get("top_k") or settings.recall_top_k,
                                    persona_id=deps.get("persona_id"))  # 按人格隔离:只召回当前人格的对话;top_k 回退读设置
            if not hits:
                return "无相关历史对话。"
            from backend.memory.l3 import EpisodicMemory
            blocks = [EpisodicMemory._format_entry(h) for h in hits]
            return "\n\n".join(blocks)
        if name == "get_time":
            from backend.perception import probes
            return json.dumps(probes.probe_clock(), ensure_ascii=False)
        if name == "get_activity":
            from backend.perception import probes
            snap = probes.run_probes()
            out = {"foreground_app": snap.get("foreground_app"),
                   "idle_sec": snap.get("idle_sec")}
            if settings.tool_include_window_title:
                out["window_title"] = snap.get("window_title")  # 隐私开关关闭时不返回(守"不发云端"边界)
            return json.dumps(out, ensure_ascii=False)
        # ---- Phase 6b:文件 / 搜索工具(任务模式挂载;dispatch 层再加一道开关门,纵深防御)----
        if name in ("read_file", "list_dir", "glob_files"):
            if not settings.file_tools_enabled:
                return _file_tools_off()
            if name == "read_file":
                path = args.get("path", "")
                if not sandbox.is_allowed(path):
                    return _denied()
                content = filereader.read_file(path)
                if not content:
                    return "文件为空或无法读取。"
                if len(content) > ATTACH_MAX_CHARS:
                    content = content[:ATTACH_MAX_CHARS] + f"\n…(已截断,全文 {len(content)} 字)"
                return content
            if name == "list_dir":
                path = args.get("path", "")
                if not sandbox.is_allowed(path):
                    return _denied()
                return _list_dir(path)
            # glob_files
            base = args.get("path") or (sandbox.allowed_roots()[0] if sandbox.allowed_roots() else "")
            if not sandbox.is_allowed(base):
                return _denied()
            return _glob_files(str(base), args.get("pattern", ""))
        if name == "web_search":
            if not settings.web_search_enabled:
                return "联网搜索已禁用(在设置页开启)。"
            from backend.llm.web import tavily_search
            return tavily_search(args.get("query", ""), top_k=args.get("top_k") or WEB_SEARCH_TOP_K)
        # ---- Phase 6c 第二大脑:笔记 / 待办 / 提醒(任务模式挂载;消费 deps 三 store)----
        if name == "add_note":
            notes = deps.get("notes")
            if notes is None:
                return "笔记功能不可用。"
            nid = notes.add(args.get("text", ""), args.get("tags"))
            return f"已记下笔记(id={nid[:8]})。" if nid else "笔记内容为空,未保存。"
        if name == "list_notes":
            notes = deps.get("notes")
            if notes is None:
                return "笔记功能不可用。"
            items = notes.list(args.get("query"))
            return "没有相关笔记。" if not items else _fmt_notes(items)
        if name == "delete_note":
            notes = deps.get("notes")
            if notes is None:
                return "笔记功能不可用。"
            return "已删除笔记。" if notes.delete(args.get("id", "")) else "没找到该笔记(id 不匹配)。"
        if name == "add_todo":
            todos = deps.get("todos")
            if todos is None:
                return "待办功能不可用。"
            tid = todos.add(args.get("text", ""))
            return f"已添加待办(id={tid[:8]})。" if tid else "待办内容为空,未添加。"
        if name == "list_todos":
            todos = deps.get("todos")
            if todos is None:
                return "待办功能不可用。"
            items = todos.list()
            return "目前没有待办。" if not items else _fmt_todos(items)
        if name == "toggle_todo":
            todos = deps.get("todos")
            if todos is None:
                return "待办功能不可用。"
            return "已更新待办状态。" if todos.toggle(args.get("id", "")) else "没找到该待办(id 不匹配)。"
        if name == "delete_todo":
            todos = deps.get("todos")
            if todos is None:
                return "待办功能不可用。"
            return "已删除待办。" if todos.delete(args.get("id", "")) else "没找到该待办(id 不匹配)。"
        if name == "set_reminder":
            reminders = deps.get("reminders")
            if reminders is None:
                return "提醒功能不可用。"
            text = (args.get("text") or "").strip()
            if not text:
                return "提醒内容为空,未设定。"
            in_minutes = args.get("in_minutes")
            at = args.get("at")
            now = time.time()
            if in_minutes is not None:
                try:
                    fire_at = now + float(in_minutes) * 60
                except (TypeError, ValueError):  # noqa: BLE001
                    return f"in_minutes 值非法({in_minutes}),未设定。"
            elif at:
                fire_at = _parse_clock_at(at, now)
                if fire_at is None:
                    return f"无法解析时间「{at}」,请用 HH:MM(如 14:30)或给 in_minutes。未设定。"
            else:
                fire_at = now + REMINDER_DEFAULT_MINUTES * 60
            rid = reminders.add(text, fire_at)
            return f"已设定提醒:「{text}」,将于 {_fmt_fire(fire_at)} 提醒你。" if rid else "提醒设定失败。"
        if name == "list_reminders":
            reminders = deps.get("reminders")
            if reminders is None:
                return "提醒功能不可用。"
            items = reminders.list()
            return "目前没有提醒。" if not items else _fmt_reminders(items)
        if name == "cancel_reminder":
            reminders = deps.get("reminders")
            if reminders is None:
                return "提醒功能不可用。"
            return "已取消提醒。" if reminders.cancel(args.get("id", "")) else "没找到该提醒(id 不匹配)。"
        # ---- Phase 6d:写入 / 执行工具(任务模式挂载;开关门 → 沙箱 → authorizer → 执行 → exec_log)----
        if name in ("write_file", "make_dir", "trash_file"):
            if not settings.write_tools_enabled:
                return _write_tools_off()
            from pathlib import Path
            raw_path = args.get("path", "")
            if not sandbox.is_allowed(raw_path):
                return _denied()
            try:
                p = Path(raw_path).expanduser().resolve()
            except Exception:  # noqa: BLE001
                return _denied()
            authorizer = deps.get("authorizer")
            if authorizer is None:
                return "授权闸门不可用,已跳过。"
            pid = deps.get("persona_id", "zorya")
            if name == "write_file":
                content = args.get("content", "")
                if not isinstance(content, str):
                    content = str(content)
                if len(content) > WRITE_MAX_CHARS:
                    return f"内容过长({len(content)} 字),超过上限 {WRITE_MAX_CHARS},未写入。"
                if p.exists() and p.is_dir():
                    return f"目标已是目录,不能当文件写:{p}"
                existed = p.exists()
                summary = f"{'覆盖' if existed else '新建'}文件 {p}({len(content)} 字符)"
                approved, reason = authorizer.request("write_file", {"path": str(p), "content": content},
                                                      summary, pid)
                if not approved:
                    return reason or "你拒绝了写入。"
                try:
                    import os
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
                    tmp.write_text(content, encoding="utf-8")
                    os.replace(str(tmp), str(p))
                except Exception as e:  # noqa: BLE001
                    return f"写入失败:{e}"
                _exec_log(deps, "write_file", f"{p}({len(content)} 字符)", "ok")
                return f"已{'覆盖' if existed else '新建'} {p}({len(content)} 字符)。"
            if name == "make_dir":
                existed = p.exists()
                summary = f"{'(已存在)' if existed else '新建'}目录 {p}"
                approved, reason = authorizer.request("make_dir", {"path": str(p)}, summary, pid)
                if not approved:
                    return reason or "你拒绝了创建目录。"
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except Exception as e:  # noqa: BLE001
                    return f"创建目录失败:{e}"
                _exec_log(deps, "make_dir", str(p), "ok")
                return f"目录已就绪:{p}" + ("(已存在)" if existed else "")
            # trash_file
            if not p.exists():
                return f"路径不存在:{p}"
            summary = f"移到废纸篓(可恢复){p}"
            approved, reason = authorizer.request("trash_file", {"path": str(p)}, summary, pid)
            if not approved:
                return reason or "你拒绝了删除。"
            ok, err = _trash(p)
            if not ok:
                return f"移到废纸篓失败:{err}"
            _exec_log(deps, "trash_file", str(p), "ok")
            return f"已移到废纸篓(可恢复):{p}"
        if name == "run_command":
            if not settings.run_command_enabled:
                return _exec_tools_off()
            from pathlib import Path
            cmd = args.get("command", "")
            argv, creason = _check_command(cmd)
            if creason:
                return creason
            cwd_raw = args.get("cwd") or ""
            if cwd_raw:
                if not sandbox.is_allowed(cwd_raw):
                    return _denied()
                try:
                    cwd_p = str(Path(cwd_raw).expanduser().resolve())
                except Exception:  # noqa: BLE001
                    return _denied()
            else:
                roots = sandbox.allowed_roots()
                cwd_p = str(roots[0]) if roots else str(Path.home())
            authorizer = deps.get("authorizer")
            if authorizer is None:
                return "授权闸门不可用,已跳过。"
            pid = deps.get("persona_id", "zorya")
            summary = f"执行:{' '.join(argv)}\n工作目录:{cwd_p}"
            approved, areason = authorizer.request("run_command", {"command": cmd, "cwd": cwd_p},
                                                   summary, pid)
            if not approved:
                return areason or "你拒绝了执行命令。"
            try:
                proc = subprocess.run(argv, cwd=cwd_p, capture_output=True, text=True,
                                      timeout=RUN_COMMAND_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                _exec_log(deps, "run_command", cmd, "timeout")
                return f"命令超过 {RUN_COMMAND_TIMEOUT_SEC}s 未结束,已终止。"
            except FileNotFoundError:
                return f"命令不存在:{argv[0]}"
            except Exception as e:  # noqa: BLE001
                return f"执行失败:{e}"
            out = (proc.stdout or "")
            if proc.stderr:
                out += ("\n[stderr]\n" if out else "") + proc.stderr
            if len(out) > RUN_COMMAND_MAX_CHARS:
                out = out[:RUN_COMMAND_MAX_CHARS] + f"\n…(已截断,共 {len(out)} 字符)"
            _exec_log(deps, "run_command", cmd, f"exit={proc.returncode}")
            return f"[退出码 {proc.returncode}]\n{out}".strip()
        return f"unknown tool: {name}"
    except Exception as e:  # noqa: BLE001
        log.warning("tool %s failed: %s", name, e)
        return f"tool error: {e}"
