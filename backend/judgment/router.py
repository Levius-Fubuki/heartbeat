"""任务模式分流器(Phase 6a):纯规则判断一条用户消息是闲聊(陪伴模式)还是复杂任务(任务模式)。

零 LLM 成本、即时。任务信号(任一命中 → 任务模式):
  ① 以「!」开头:显式强制(兜底规则误判,用户随时可强制走任务模式)
  ② 带附件:已经是要处理文件
  ③ 消息长度 > TASK_MIN_LEN:复杂诉求通常较长
  ④ 命中 TASK_KEYWORDS:精准任务词集合(避免「帮我听歌」误判)

task_mode_enabled 关闭时一律返 False(总开关,全部走陪伴模式)。
陪伴模式零改动是 Phase 6 的核心约束:分流的根本目的就是让陪伴模式不被能力扩张污染。
"""
from __future__ import annotations

from backend.config import TASK_KEYWORDS, TASK_MIN_LEN
from backend.memory.settings import settings


def classify_task(text: str, has_attachment: bool = False) -> bool:
    """这条消息是否走任务模式(长输出 + 全工具 + 放开技术铁律)。

    返回 False → 陪伴模式(现有 react_reply,零改动)。
    task_mode_enabled=False 时一律 False。
    """
    if not settings.task_mode_enabled:
        return False
    text = text or ""
    # ① 显式强制:「!」开头(容许前导空白)
    if text.lstrip().startswith("!"):
        return True
    # ② 带附件 → 任务(用户已选文件,显然要处理)
    if has_attachment:
        return True
    # ③ 长度超阈 → 疑似任务
    if len(text) > TASK_MIN_LEN:
        return True
    # ④ 命中任务词(子串,大小写不敏感;英文词 lower 匹配,中文 lower 不变)
    lower = text.lower()
    if any(kw in lower for kw in TASK_KEYWORDS):
        return True
    return False
