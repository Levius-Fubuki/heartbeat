"""执行审计日志(Phase 6d):逐条记录**已授权并执行**的 write / make_dir / trash / run_command。

append-only jsonl,崩溃安全(write + flush + fsync),读时跳过损坏行。只记真的执行了的操作——
用户否决 / 闸门(白名单/沙箱/破坏性)拒绝**不记**(那些是没发生的事)。作为可追溯的操作痕迹,
不进 LLM 上下文。memory/exec_log.jsonl(.gitignore)。

照搬 ConversationLog 的崩溃安全 jsonl 范式。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.config import MEMORY_DIR

log = logging.getLogger("heartbeat.memory.execlog")

_PATH = MEMORY_DIR / "exec_log.jsonl"
_DETAIL_MAX = 500   # 单字段截断防爆长日志


class ExecLog:
    """已执行操作的审计日志。一行一条 {ts, persona, tool, args, outcome, detail}。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _PATH

    def append(self, persona_id: str, tool: str, args_summary: str,
               outcome: str, detail: str = "") -> None:
        """记一条已执行操作。失败仅 log(主循环不能崩;丢一条不致命)。"""
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "persona": persona_id,
            "tool": tool,
            "args": (args_summary or "")[:_DETAIL_MAX],
            "outcome": (outcome or "")[:_DETAIL_MAX],
            "detail": (detail or "")[:_DETAIL_MAX],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:  # noqa: BLE001
            log.warning("exec_log append failed: %s", self.path)
