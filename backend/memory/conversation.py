"""对话日志:逐条持久化所有 user / agent 消息(UI 历史专用),重启后回灌主窗。

与 episodic.jsonl(段级快照,给 LLM 召回用)职责分离:本文件逐条记录
**UI 看到的消息**(显示文字 + 附件名,不是给 LLM 的含附件全文),供握手 /
切换人格时按人格过滤回灌最近 N 条到前端 `#chat`。

append-only jsonl,崩溃安全(逐行 write + flush + fsync);读时跳过损坏行。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from backend.config import MEMORY_DIR, CONVERSATION_REPLAY_MAX

log = logging.getLogger("heartbeat.memory.conversation")

_PATH = MEMORY_DIR / "conversation.jsonl"


class ConversationLog:
    """逐条对话日志。一行一条 {ts, persona, role, text, attachment_name?}。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _PATH

    def append(self, role: str, text: str, persona_id: str,
               attachment_name: Optional[str] = None) -> None:
        """追加一条消息。失败仅 log(主循环不能崩;丢一条不致命)。"""
        rec: dict = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "persona": persona_id,
            "role": role,
            "text": text or "",
        }
        if attachment_name:
            rec["attachment_name"] = attachment_name
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:  # noqa: BLE001
            log.warning("conversation append failed: %s", self.path)

    def recent_messages(self, persona_id: str,
                        limit: int = CONVERSATION_REPLAY_MAX) -> List[dict]:
        """返回该人格最近 limit 条消息(旧→新),供前端回灌。损坏行跳过。

        返回元素形如 {role, text, persona, attachment_name?}(前端 addChat 直接消费)。
        limit<=0 表示全部。文件不存在 / 读失败 → []。
        """
        if not self.path.exists():
            return []
        out: List[dict] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue                      # 跳过损坏行
                    if rec.get("persona") != persona_id:
                        continue                      # 按人格隔离(与 L1/L3 一致)
                    out.append({
                        "role": rec.get("role", "agent"),
                        "text": rec.get("text", ""),
                        "persona": rec.get("persona", persona_id),
                        "attachment_name": rec.get("attachment_name"),
                    })
        except Exception:  # noqa: BLE001
            log.warning("conversation read failed: %s", self.path)
            return []
        return out[-limit:] if limit > 0 else out
