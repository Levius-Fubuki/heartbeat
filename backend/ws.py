"""WebSocket hub:多客户端广播。桌宠窗 / 主窗 / 未来其它视图共享同一段对话。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("heartbeat.ws")


class Hub:
    def __init__(self) -> None:
        self.clients: set = set()
        self.on_user_message: Optional[Callable[[str, Optional[dict]], Awaitable[None]]] = None
        self.on_select_persona: Optional[Callable[[str], Awaitable[None]]] = None
        self.on_set_cooldown_mode: Optional[Callable[[str], Awaitable[None]]] = None
        self.on_start_interview: Optional[Callable[[], Awaitable[None]]] = None
        self.on_end_interview: Optional[Callable[[], Awaitable[None]]] = None
        self._lock = None  # 延迟创建(3.9 子线程 import 时无 loop,见项目记忆)

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def attach(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)
        log.info("client connected (total %d)", len(self.clients))

    async def detach(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)
        log.info("client disconnected (total %d)", len(self.clients))

    async def send(self, payload: dict) -> None:
        async with self.lock:
            clients = list(self.clients)
        if not clients:
            return
        text = json.dumps(payload, ensure_ascii=False)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception as e:  # noqa: BLE001
                log.warning("send failed: %s", e)
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)

    async def receive_loop(self, ws: WebSocket) -> None:
        """接收循环:消费前端消息(user_message / select_persona)。"""
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "user_message" and self.on_user_message:
                    await self.on_user_message(str(data.get("text", "")), data.get("attachment"))
                elif data.get("type") == "select_persona" and self.on_select_persona:
                    await self.on_select_persona(str(data.get("id", "")))
                elif data.get("type") == "set_cooldown_mode" and self.on_set_cooldown_mode:
                    await self.on_set_cooldown_mode(str(data.get("mode", "")))
                elif data.get("type") == "start_interview" and self.on_start_interview:
                    await self.on_start_interview()
                elif data.get("type") == "end_interview" and self.on_end_interview:
                    await self.on_end_interview()
        except WebSocketDisconnect:
            await self.detach(ws)
        except Exception as e:  # noqa: BLE001
            log.warning("receive loop ended: %s", e)
            await self.detach(ws)


hub = Hub()
