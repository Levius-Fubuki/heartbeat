"""联网搜索(Phase 6b 任务模式 web_search 工具)。Tavily /search,同步 httpx。

为何同步:dispatch_tool 是同步函数(被 react_probe/boundary_probe 直调,改异步会牵连),网关里
_react_loop 把 dispatch 调用包进 asyncio.to_thread,故这里的 httpx 网络调用不会阻塞事件循环。

Tavily key 在设置页填(走 secrets 段 + 掩码),或 HEARTBEAT_TAVILY_KEY 环境变量。失败降级返提示串,
LLM 据此放弃搜索、改口或明说搜不到。
"""
from __future__ import annotations

import logging

from backend.memory.settings import settings

log = logging.getLogger("heartbeat.llm.web")

TAVILY_URL = "https://api.tavily.com/search"
_TIMEOUT = 20.0


def tavily_search(query: str, top_k: int = 5) -> str:
    """Tavily /search → 拼接好的中文友好文本(编号 + 标题 + url + 摘要)。无 key/无结果/异常 → 提示串。"""
    key = settings.tavily_key
    if not key:
        return "未配置搜索 API key(在设置页「工具与权限」填 Tavily key)。"
    query = (query or "").strip()
    if not query:
        return "搜索词为空。"
    try:
        import httpx
        r = httpx.post(TAVILY_URL, json={
            "api_key": key,
            "query": query,
            "max_results": max(1, int(top_k or 5)),
            "search_depth": "basic",
        }, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("tavily search failed: %s", e)
        return f"搜索失败:{e}"
    results = (data or {}).get("results") or []
    if not results:
        return "没有找到相关结果。"
    lines = []
    for i, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        content = (item.get("content") or "").strip()
        lines.append(f"[{i}] {title}\n{url}\n{content}" if title else f"[{i}] {url}\n{content}")
    text = "\n\n".join(lines)
    # 与 read_file 同口径截断,防搜索结果过长占爆 token
    from backend.config import ATTACH_MAX_CHARS
    if len(text) > ATTACH_MAX_CHARS:
        text = text[:ATTACH_MAX_CHARS] + f"\n…(已截断,全文 {len(text)} 字)"
    return text
