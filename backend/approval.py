"""逐次授权闸门(Phase 6d)。写 / 删 / 执行工具调用前,经此闸门弹窗让用户确认。

dispatch_tool 同步跑在 worker 线程(经 ``asyncio.to_thread``),所以 ``request()`` 必须**同步阻塞**
等用户响应。线程模型:

- worker 线程调 ``request()`` → 建 ``concurrent.futures.Future``(跨线程 set_result 安全;**勿用 asyncio.Future**,
  它跨线程 set_result 不安全)→ 经 ``asyncio.run_coroutine_threadsafe`` 把 ``confirm_request`` 广播回事件循环
  → ``fut.result(timeout=EXEC_APPROVAL_TIMEOUT_SEC)`` 阻塞 worker 线程。
- 用户在主窗点「允许/拒绝」→ ws ``confirm_response`` → hub 回调 → ``resolve(id, approved)``
  → ``fut.set_result(approved)`` → worker 线程解阻塞。
- 超时未响应 → 自动否决 + 广播 ``confirm_close``(前端据此隐藏确认卡,无论响应还是超时)。

关键不变量:事件循环在等待期间空闲(可处理 ``confirm_response``);reply_lock 由 on_user_message 持有,
串行无交错;ReAct 一次只调一个工具 → 同时最多一个 pending。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import uuid
from typing import Optional, Tuple

from backend.config import EXEC_APPROVAL_TIMEOUT_SEC

log = logging.getLogger("heartbeat.approval")

_ARG_PREVIEW_MAX = 400   # confirm_request 给前端展示的 args 单值截断(write_file content 不撑爆对话框)


class ApprovalGate:
    """逐次授权闸门。线程安全。可注入(probe 传伪造的 request 即可绕过真实弹窗)。"""

    def __init__(self, hub) -> None:
        self.hub = hub
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict = {}                  # id -> concurrent.futures.Future
        self._lock = threading.Lock()
        self.current_id: Optional[str] = None     # 单条串行(供观测;ReAct 一次一工具)

    def bind_loop(self, loop) -> None:
        """懒绑事件循环(__init__ 不在 loop 线程;在 _react_deps 内 get_running_loop 绑)。幂等。"""
        if self._loop is None and loop is not None:
            self._loop = loop

    def request(self, tool: str, args: dict, summary: str, persona_id: str,
                timeout: float = EXEC_APPROVAL_TIMEOUT_SEC) -> Tuple[bool, str]:
        """同步(worker 线程内调):弹窗请求授权,阻塞直到用户响应或超时。返 (approved, reason)。

        reason:approved=True → ""(放行);否决 → 给 LLM 看的中文原因(它据此换做法,不重试轰炸)。
        """
        if self._loop is None:
            return False, "授权闸门未就绪(事件循环未绑定),已跳过。"
        req_id = uuid.uuid4().hex
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending[req_id] = fut
            self.current_id = req_id
        payload = {
            "type": "confirm_request", "id": req_id, "tool": tool,
            "args": _scrub_args(args), "summary": summary, "persona": persona_id,
        }
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)
        except Exception as e:  # noqa: BLE001
            log.warning("approval broadcast failed: %s", e)
            self._drop(req_id)
            return False, "授权请求发送失败,已跳过。"
        try:
            approved = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            self._drop(req_id)
            self._close(req_id, False, "确认超时,已自动跳过。")
            return False, "确认超时,已跳过。"
        except Exception as e:  # noqa: BLE001
            log.warning("approval wait failed: %s", e)
            self._drop(req_id)
            return False, "授权等待异常,已跳过。"
        self._drop(req_id)
        return (bool(approved), "" if approved else "你拒绝了这次操作。")

    def resolve(self, req_id: str, approved: bool) -> bool:
        """用户响应到达(loop 线程,hub 回调):解 Future。返是否命中 pending(未命中=超时已清,no-op)。"""
        with self._lock:
            fut = self._pending.get(req_id)
        if fut is None:
            return False                       # 超时已清 / 未知 id → no-op
        try:
            fut.set_result(bool(approved))
        except Exception:  # noqa: BLE001     # 重复 set(理论上不会)→ 忽略
            pass
        self._close(req_id, bool(approved), "" if approved else "已拒绝。")
        return True

    # ---------- 内部 ----------
    def _drop(self, req_id: str) -> None:
        with self._lock:
            self._pending.pop(req_id, None)
            if self.current_id == req_id:
                self.current_id = None

    def _close(self, req_id: str, approved: bool, reason: str) -> None:
        """广播 confirm_close 让前端隐藏确认卡(用户响应 / 超时都发)。失败仅 log。"""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._broadcast({"type": "confirm_close", "id": req_id,
                                 "approved": approved, "reason": reason}),
                self._loop)
        except Exception:  # noqa: BLE001
            pass

    async def _broadcast(self, payload: dict) -> None:
        try:
            await self.hub.send(payload)
        except Exception as e:  # noqa: BLE001
            log.warning("approval hub.send failed: %s", e)


def _scrub_args(args: dict) -> dict:
    """给前端展示的 args 副本:每值截断到 _ARG_PREVIEW_MAX(write_file content 等不撑爆确认卡)。"""
    out = {}
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else repr(v)
        if len(s) > _ARG_PREVIEW_MAX:
            s = s[:_ARG_PREVIEW_MAX] + f"…(已截断,共 {len(s)} 字符)"
        out[k] = s
    return out
