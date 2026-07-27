"""Heartbeat 配置中心。Phase 0 用开发期短间隔,便于观察完整周期;生产改 60s / 120s。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # .../heartbeat
PERSONAS_DIR = ROOT / "personas"
MEMORY_DIR = ROOT / "memory"
MEMORY_DIR.mkdir(exist_ok=True)                 # 模块级建目录,import 即确保存在(Phase 2 记忆落盘处)
load_dotenv(ROOT / ".env")                      # 读 .env 里的 LLM key 等

# --- 时序(开发期缩短;生产: HEARTBEAT=60, SILENCE=120) ---
HEARTBEAT_INTERVAL_SEC = 8      # 心跳检查间隔
SILENCE_TIMEOUT_SEC = 120       # 正常互动静默多久判定结束(dev 曾用 25s,会切碎对话,改回生产值)
BEAT_PAUSE_SEC = 1.4            # alert / settle 等微动画的展示停顿

# --- Onboarding Stage 2 首次访谈(手动入口触发;LLM 自主带队,后端轮数/静默兜底)---
INTERVIEW_MAX_ROUNDS = int(os.getenv("HEARTBEAT_INTERVIEW_MAX_ROUNDS", "8"))        # 用户回答轮数上限(达此值该轮注入告别收尾)
INTERVIEW_SILENCE_SEC = int(os.getenv("HEARTBEAT_INTERVIEW_SILENCE_SEC", "60"))     # 访谈静默超时(短于正常 120s,用户中途离开即收尾)
INTERVIEW_TEMPERATURE = float(os.getenv("HEARTBEAT_INTERVIEW_TEMPERATURE", "0.85"))  # 访谈回复温度(略高于 reply 0.8,更自然)
INTERVIEW_MAX_TOKENS = int(os.getenv("HEARTBEAT_INTERVIEW_MAX_TOKENS", "512"))      # 每轮回复上限(2026-07-25:v4-flash 推理模型,140 被 reasoning 吃光截断,抬到 512)

# --- 服务 ---
HOST = "127.0.0.1"
PORT = 8765

# --- LLM(OpenAI 兼容;DeepSeek)。有 key 就用真模型,无 key 回退 canned 桩 ---
LLM_API_KEY = os.getenv("HEARTBEAT_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("HEARTBEAT_LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("HEARTBEAT_LLM_MODEL", "deepseek-chat")
USE_REAL_LLM = bool(LLM_API_KEY)  # 有 key 即用真模型,否则 canned 桩

DEFAULT_PERSONA = "zorya"       # walking skeleton 默认人格(onboarding 之前;启动时若 profile 记录了已选人格则覆盖之)

# --- 记忆(Phase 2)---
MEMORY_EPISODIC_MAX_CHARS = 1200  # L3 recent_text 段级截断上限,控制 system prompt 长度
EPISODIC_RECALL_N = 2             # 新对话注入 system 时召回最近几段情景记忆
CONVERSATION_REPLAY_MAX = int(os.getenv("HEARTBEAT_CONVERSATION_REPLAY_MAX", "50"))  # 重启/切人格时回灌主窗的最近消息条数(UI 历史,与 episodic 段级召回分离)

# --- ReAct + 向量召回(Phase 4)---
EMBED_MODEL = os.getenv("HEARTBEAT_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")  # 本地中文嵌入(fastembed ONNX,~130MB 首次下载)
EMBED_ENABLED = os.getenv("HEARTBEAT_EMBED_ENABLED", "1") != "0"            # 总开关:出问题可关,降级到纯 recent_text
RECALL_TOP_K = int(os.getenv("HEARTBEAT_RECALL_TOP_K", "3"))                # recall_memory 工具向量召回条数
REACT_MAX_ROUNDS = int(os.getenv("HEARTBEAT_REACT_MAX_ROUNDS", "4"))        # ReAct 循环上限(防死循环,末轮强制 tool_choice=none)
TOOL_INCLUDE_WINDOW_TITLE = os.getenv("HEARTBEAT_TOOL_INCLUDE_WINDOW_TITLE", "1") != "0"  # get_activity 是否返回窗口标题(隐私开关;False 守"window_title 绝不发云端"旧边界)

# --- 任务模式(Phase 6a:自动分流——复杂问题走任务模式:长输出 + 全工具 + 放开"不是开发者"铁律;陪伴模式零改动)---
TASK_MIN_LEN = int(os.getenv("HEARTBEAT_TASK_MIN_LEN", "60"))                      # 消息长度 > 此值疑似任务(复杂诉求通常较长)
REACT_TASK_MAX_TOKENS = int(os.getenv("HEARTBEAT_REACT_TASK_MAX_TOKENS", "1500"))   # 任务模式回复上限(陪伴 react_reply=160,任务模式放宽)
TASK_KEYWORDS = (                                                                   # 命中任一(子串,大小写不敏感)→ 任务模式;精准集合,避免「帮我听歌」误判
    "分析", "总结", "对比", "解释", "梳理", "列出", "规划", "拆解", "重构", "调试",
    "查一下", "搜一下", "搜索", "查资料", "报错", "bug", "debug",
    "refactor", "explain", "analyze", "review", "文档", "项目结构", "看看这段",
    "读代码", "读文件", "读这个", "读那", "这段代码", "这个文件",
    # Phase 6c 第二大脑:笔记/待办/提醒 意图 → 任务模式(工具才可见)。bare 词(待办/笔记/提醒)
    # 偶有误伤(如「笔记本」)但无害(任务模式只是更长回复 + 工具可见),生产力意图优先
    "提醒我", "记一下", "记笔记", "加待办", "加笔记", "待办", "笔记", "提醒",
    # Phase 6d 写入 / 执行意图 → 任务模式(write_file/make_dir/trash_file/run_command 工具才在任务模式可见)。
    # bare 词偶有误伤(如「跑个步」「写个故事」),但任务模式只是更长回复 + 工具可见,无害;生产力意图优先。
    "写文件", "写个文件", "写个", "新建文件", "新建一个", "创建文件", "创建一个",
    "写到文件", "保存到文件", "保存成文件", "覆盖文件",
    "删除文件", "删掉", "删了", "移到废纸篓", "清空文件",
    "跑一下", "跑命令", "跑个脚本", "跑脚本", "运行命令", "运行脚本",
    "执行", "执行命令", "执行脚本", "命令行", "终端跑", "跑一下命令",
)

# --- L4 模式沉淀(从 L3 归纳作息+行为规律;既注入 system prompt 懂用户,又作主动搭话触发源)---
PATTERN_MINE_EVERY_N_CONVOS = int(os.getenv("HEARTBEAT_PATTERN_MINE_EVERY_N_CONVOS", "5"))  # 累积几段对话后归纳一次(单段抽不出规律)
PATTERN_MINE_ENTRY_N = int(os.getenv("HEARTBEAT_PATTERN_MINE_ENTRY_N", "8"))               # 归纳时读最近几段 L3 做素材(>EVERY_N,给 LLM 更多归纳依据)
PATTERN_MAX = int(os.getenv("HEARTBEAT_PATTERN_MAX", "20"))                                # 模式条数上限(防爆)
PATTERN_MAX_CHARS = int(os.getenv("HEARTBEAT_PATTERN_MAX_CHARS", "400"))                   # 注入 [行为模式] 段的字符上限
PATTERN_MIN_EVIDENCE = int(os.getenv("HEARTBEAT_PATTERN_MIN_EVIDENCE", "2"))               # 证据数 < 此值只注入不触发(防误触)
PATTERN_PRIORITY = int(os.getenv("HEARTBEAT_PATTERN_PRIORITY", "2"))                       # pattern_match 候选优先级(< deep_night5/long_focus4,锦上添花不抢占 rules 关心类)

# --- 人格切换吐槽(switch-in roast;切人格 A→B 时 B 用关系设定即兴吐槽 A,记忆隔离)---
SWITCH_ROAST_TEMPERATURE = float(os.getenv("HEARTBEAT_SWITCH_ROAST_TEMPERATURE", "0.95"))  # 高 temp 保证每次措辞多变
SWITCH_ROAST_MAX_TOKENS = int(os.getenv("HEARTBEAT_SWITCH_ROAST_MAX_TOKENS", "512"))        # 2026-07-25:v4-flash 推理模型,100 被 reasoning 吃光→吐槽砍在半句(如"你找对"/"...活力"),抬到 512
SWITCH_ROAST_ANTI_REPEAT_N = int(os.getenv("HEARTBEAT_SWITCH_ROAST_ANTI_REPEAT_N", "6"))    # 注入最近 N 句吐槽防复读(deque 容量 ×2)
SWITCH_ROAST_MIN_SUBSTANCE = int(os.getenv("HEARTBEAT_SWITCH_ROAST_MIN_SUBSTANCE", "4"))    # 上一人格 L1 ≥ 此消息数 且含 user 轮 才算"真聊过"→ 才归档/吐槽

# --- 附件(per-message 文件附件:➕选文件,全文随这条消息进上下文,不持久)---
ATTACH_MAX_CHARS = int(os.getenv("HEARTBEAT_ATTACH_MAX_CHARS", "8000"))          # 单附件字符上限(超则截断,防大 PDF 占爆 token)

# --- Phase 6b 只读工具(任务模式专用:read_file 复用 ATTACH_MAX_CHARS 截断;list_dir/glob/web_search 上限)---
HEARTBEAT_TAVILY_KEY = os.getenv("HEARTBEAT_TAVILY_KEY", "")                     # Tavily 联网搜索 key(也可在设置页填,settings.tavily_key 回退到此)
HEARTBEAT_SILICONFLOW_KEY = os.getenv("HEARTBEAT_SILICONFLOW_KEY", "")           # 硅基流动 key(CosyVoice2 TTS;也可在设置页填,settings.siliconflow_key 回退到此)
SILICONFLOW_TTS_URL = os.getenv("HEARTBEAT_SILICONFLOW_TTS_URL", "https://api.siliconflow.cn/v1/audio/speech")  # 硅基流动 TTS 端点
SILICONFLOW_TTS_MODEL = os.getenv("HEARTBEAT_SILICONFLOW_TTS_MODEL", "FunAudioLLM/CosyVoice2-0.5B")            # CosyVoice2 模型 id
FILE_LIST_MAX = int(os.getenv("HEARTBEAT_FILE_LIST_MAX", "200"))                 # list_dir 返回条目上限(超则截断+注明)
FILE_GLOB_MAX = int(os.getenv("HEARTBEAT_FILE_GLOB_MAX", "50"))                  # glob_files 返回匹配上限
WEB_SEARCH_TOP_K = int(os.getenv("HEARTBEAT_WEB_SEARCH_TOP_K", "5"))             # web_search 默认结果数

# --- 第二大脑(Phase 6c:笔记/待办/提醒;任务模式工具,到点提醒作陪伴主动搭话)---
NOTES_MAX = int(os.getenv("HEARTBEAT_NOTES_MAX", "100"))                         # 笔记条数上限(超则删最旧)
TODOS_MAX = int(os.getenv("HEARTBEAT_TODOS_MAX", "50"))                          # 待办条数上限(超则删最旧)
REMINDERS_MAX = int(os.getenv("HEARTBEAT_REMINDERS_MAX", "30"))                  # 提醒条数上限(超则先删已 fired 旧条)
REMINDER_DEFAULT_MINUTES = int(os.getenv("HEARTBEAT_REMINDER_DEFAULT_MINUTES", "30"))  # set_reminder 未给时间时的默认分钟数
NOTES_INJECT_MAX = int(os.getenv("HEARTBEAT_NOTES_INJECT_MAX", "3"))             # 注入 [待办与笔记] 的近期笔记条数
TODOS_INJECT_MAX = int(os.getenv("HEARTBEAT_TODOS_INJECT_MAX", "5"))             # 注入的未完成待办条数
REMINDERS_INJECT_MAX = int(os.getenv("HEARTBEAT_REMINDERS_INJECT_MAX", "3"))     # 注入的近期到期提醒条数
REMINDERS_INJECT_SOON_SEC = int(os.getenv("HEARTBEAT_REMINDERS_INJECT_SOON_SEC", "86400"))  # 注入「即将到期」的窗口(默认 24h)

# --- Phase 6d 写入 / 执行工具(任务模式专用:write_file/make_dir/trash_file/run_command;每次先弹窗让你确认)---
WRITE_MAX_CHARS = int(os.getenv("HEARTBEAT_WRITE_MAX_CHARS", "20000"))               # write_file 单次写入字符上限(超则拒,防 LLM 写超大文件 + 占爆 token)
RUN_COMMAND_TIMEOUT_SEC = int(os.getenv("HEARTBEAT_RUN_COMMAND_TIMEOUT_SEC", "30"))  # run_command 单条超时(超则 kill,返超时提示)
RUN_COMMAND_MAX_CHARS = int(os.getenv("HEARTBEAT_RUN_COMMAND_MAX_CHARS", "4000"))    # run_command 输出(stdout+stderr)截断上限
EXEC_APPROVAL_TIMEOUT_SEC = int(os.getenv("HEARTBEAT_EXEC_APPROVAL_TIMEOUT_SEC", "120"))  # 逐次授权等待上限(用户不点则自动否决)
# run_command 允许的二进制白名单(首词 basename 命中才放行;可在设置页改,留空=禁用 run_command)。
# 默认 sane 集:只读/构建类 + 常见语言运行时,不含 rm/dd/sudo(那些在 COMMAND_DANGEROUS 硬封)。
COMMAND_ALLOWLIST = tuple(os.getenv(
    "HEARTBEAT_COMMAND_ALLOWLIST",
    "git,ls,cat,echo,grep,find,head,tail,wc,mkdir,touch,cp,mv,python3,python,node,npm,pip3,rg,tree,sed,awk,sort,uniq,diff"
).split(","))
# 破坏性/高危命令硬封(即便进了白名单也拒;belt-and-suspenders,保底)。
COMMAND_DANGEROUS = frozenset(("rm", "rmdir", "dd", "mkfs", "sudo", "shutdown", "reboot", "halt",
                               "sh", "bash", "zsh", "curl", "wget", "chmod", "chown", "kill", "launchctl"))
# shell 元字符:命令串出现任一则拒(一次只跑一条命令,不支持管道/重定向/拼接;天然防注入 + 用户看 summary 清楚)。
COMMAND_FORBIDDEN_CHARS = frozenset(";&|<>$\n`")


def load_personas() -> dict:
    """从 personas/*.yaml 读取所有人格卡。"""
    personas: dict = {}
    for f in sorted(PERSONAS_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        if data and "id" in data:
            personas[data["id"]] = data
    return personas


PERSONAS = load_personas()


# --- App 分类(规则层用;未列入的 App 归 neutral,不触发 dev/leisure 规则,但仍受 long_focus/deep_night 等行为规则约束)---
APP_CATEGORIES = {
    "music": {"Spotify", "QQ音乐", "NeteaseMusic", "网易云音乐", "Music", "VLC", "Amazon Music"},
    "dev": {"VSCode", "Code", "Visual Studio Code", "Warp", "iTerm", "iTerm2", "Terminal",
            "PyCharm", "IntelliJ IDEA", "Xcode", "Sublime Text", "Neovim", "Zed", "Cursor"},
    "social": {"微信", "WeChat", "QQ", "Telegram", "Slack", "Discord", "微博", "小红书", "飞书", "钉钉"},
    "leisure": {"B站", "哔哩哔哩", "YouTube", "Netflix", "爱奇艺", "优酷", "腾讯视频", "Steam", "抖音", "快手"},
    "browser": {"Chrome", "Google Chrome", "Safari", "Microsoft Edge", "Firefox", "Arc", "Brave"},
}

# 浏览器窗口标题细分关键词(仅本地判断窗口内容类别;标题绝不发云端 judge):
LEISURE_TITLE_HINTS = ("YouTube", "B站", "哔哩哔哩", "Netflix", "剧", "番", "直播", "视频", "抖音", "快手")
DEV_TITLE_HINTS = ("GitHub", "GitLab", "Stack Overflow", "文档", "docs", "Issue", "PR", "src.", ".py", ".ts", ".js")

# --- 规则阈值(按时长/计数;tick 换算在下方,规则直接用整数 tick 比较)---
FOCUS_MIN_MINUTES = 12            # 同一前台 App 持续专注 ≥ 此值 → 提醒休息
LEISURE_TOO_LONG_MINUTES = 15     # 娱乐/社交 App 持续 ≥ 此值 → 提醒回工作
DISTRACTED_APP_COUNT = 5          # 最近窗口内不同 App ≥ 此值 → 分心提醒
DISTRACTED_WINDOW_TICKS = 15      # distracted 规则看最近多少 tick(≈2分钟)内的切换

FOCUS_TICKS = FOCUS_MIN_MINUTES * 60 // HEARTBEAT_INTERVAL_SEC     # 12min/8s = 90
LEISURE_TICKS = LEISURE_TOO_LONG_MINUTES * 60 // HEARTBEAT_INTERVAL_SEC  # 15min/8s = 112

# --- 打扰频率三档(每档是 category→冷却秒;default 兜底)---
# 保守:长冷却少打扰 / 平衡:推荐默认 / 活跃:短冷却常主动。用户在主界面可切换,持久化进 profile。
COOLDOWN_PROFILES = {
    "conservative": {
        "deep_night_working": 14400, "morning_greet": 86400, "long_focus": 7200,
        "leisure_too_long": 7200, "dev_app_opened": 7200, "leisure_app_opened": 7200,
        "distracted": 3600, "returned_from_idle": 1800, "music_app_opened": 3600,
        "pattern_match": 14400, "default": 3600,
    },
    "balanced": {
        "deep_night_working": 14400, "morning_greet": 86400, "long_focus": 3600,
        "leisure_too_long": 3600, "dev_app_opened": 1800, "leisure_app_opened": 1800,
        "distracted": 1800, "returned_from_idle": 600, "music_app_opened": 1800,
        "pattern_match": 3600, "default": 1800,
    },
    "active": {
        "deep_night_working": 7200, "morning_greet": 43200, "long_focus": 1800,
        "leisure_too_long": 1800, "dev_app_opened": 600, "leisure_app_opened": 600,
        "distracted": 600, "returned_from_idle": 300, "music_app_opened": 600,
        "pattern_match": 1800, "default": 600,
    },
}
DEFAULT_COOLDOWN_MODE = "balanced"
