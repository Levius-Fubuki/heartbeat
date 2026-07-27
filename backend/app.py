"""FastAPI 应用:静态前端 + WebSocket + 心跳生命周期。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import ws as ws_mod
from backend.config import DEFAULT_PERSONA, PERSONAS, PORT
from backend.heartbeat import Heartbeat
from backend.memory.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("heartbeat.app")
for _n in ("httpx", "httpcore", "openai"):
    logging.getLogger(_n).setLevel(logging.WARNING)  # 压住每次 LLM 调用的 HTTP 刷屏

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

hub = ws_mod.hub
heartbeat = Heartbeat(hub, persona_id=DEFAULT_PERSONA)
hub.on_user_message = heartbeat.on_user_message
hub.on_select_persona = heartbeat.on_select_persona
hub.on_set_cooldown_mode = heartbeat.on_set_cooldown_mode
hub.on_start_interview = heartbeat.on_start_interview
hub.on_end_interview = heartbeat.on_end_interview


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 后台预热 embedding 模型 + 建 L3 向量索引(首次下载 ~130MB;不阻塞启动,失败仅降级 recall)
    asyncio.create_task(asyncio.to_thread(_warmup_embed))
    task = asyncio.create_task(heartbeat.run())
    log.info("serving frontend: %s  (ws on :%s)", FRONTEND_DIR, PORT)
    try:
        yield
    finally:
        task.cancel()


def _warmup_embed() -> None:
    """后台加载 fastembed + 建 episodic 向量索引。失败仅 log(recall 降级,recent_* 仍可用)。"""
    try:
        from backend.config import EMBED_ENABLED
        if not EMBED_ENABLED:
            return
        from backend.memory.embeddings import get_model
        get_model()                       # 触发首次下载/加载(~130MB)
        heartbeat.episodic.build_index()  # 建/加载向量索引(迁移补 id + 缓存)
        log.info("embed warmup + episodic index done")
    except Exception:  # noqa: BLE001
        log.exception("embed warmup failed (recall disabled until restart; recent_* still work)")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
# 人格美术图直读 personas/image/(每人格一个子目录,如 /personas/image/zorya/zorya-idle.png);
# 用户在此目录换图即生效(配合 webview 禁缓存),无需拷到 assets。
app.mount("/personas", StaticFiles(directory=ROOT / "personas"), name="personas")


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


def _persona_card(p: dict) -> dict:
    """前端人格卡数据:id/name/西里尔/原型/配色 + 背景叙事(name_meaning)/性格(core_traits)/招牌(signature)。
    get_personas 与 onboarding_select 共用,保持两处卡片数据形状一致。"""
    return {
        "id": p["id"], "name": p["name"], "name_cyr": p.get("name_cyr", ""),
        "archetype": p.get("archetype", ""), "accent": p.get("accent", "#cccccc"),
        "backstory": p.get("name_meaning", ""),
        "traits": p.get("core_traits", []),
        "signature": p.get("signature", ""),
    }


@app.get("/api/personas")
async def get_personas():
    return {"current": heartbeat.persona_id, "personas": [_persona_card(p) for p in PERSONAS.values()]}


@app.get("/api/settings")
async def get_settings():
    """设置(掩码):LLM 凭据/模型 + 拉取到的模型列表 + 旋钮当前值与元数据。永不返回完整 api_key。"""
    return settings.public_view()


@app.post("/api/settings")
async def post_settings(req: Request):
    """保存设置:body {llm?:{provider?,base_url?,api_key?,model?}, config?:{knob:value},
    secrets?:{tavily_key?}, paths?:{allowed_dirs?:[...]}}(Phase 6b 后两段为文件/搜索工具配置)。
    更新后广播 settings_changed(三窗刷新);gateway 凭据/模型下次调用即热生效。"""
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    settings.update(body.get("llm"), body.get("config"),
                    secrets=body.get("secrets"), paths=body.get("paths"))
    view = settings.public_view()
    await hub.send({"type": "settings_changed", "settings": view})
    log.info("settings updated (model=%s, has_key=%s)", view["llm"]["model"], view["llm"]["has_key"])
    return view


@app.post("/api/llm/models")
async def llm_models(req: Request):
    """用 body 里给的 base_url/api_key(否则用已存设置)打 {base_url}/models 拉取模型列表。
    成功 → {ok:true, models:[id...]} 并缓存进 settings.models;失败 → {ok:false, error}。"""
    try:
        body = await req.json() or {}
    except Exception:  # noqa: BLE001
        body = {}
    base = (body.get("base_url") or settings.base_url or "").strip()
    key = (body.get("api_key") or settings.api_key or "").strip()
    if not key:
        return {"ok": False, "error": "请先填写 API key", "models": []}
    try:
        ids = await asyncio.to_thread(_fetch_models, key, base)
    except Exception as e:  # noqa: BLE001
        log.warning("fetch models failed: %s", e)
        return {"ok": False, "error": f"拉取失败:{e}", "models": []}
    settings.set_models(ids)
    log.info("fetched %d models from %s", len(ids), base)
    return {"ok": True, "models": ids}


def _fetch_models(api_key: str, base_url: str) -> list:
    """同步:用 OpenAI 兼容 client 打 /models,返回模型 id 列表(在线程里跑)。"""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.models.list()
    return [m.id for m in resp.data]


@app.post("/api/voice/stop")
async def voice_stop():
    """立即停止当前语音朗读(主窗静音钮按下时调,避免长回复继续念到尾)。降级态也 no-op。"""
    heartbeat.voice.stop()
    return {"ok": True}


@app.post("/api/confirm")
async def confirm_action(req: Request):
    """Phase 6d:用户在主窗确认卡点了「允许/拒绝」→ 解开对应写/执行授权 Future。

    走 HTTP 而非 WS:写/执行工具是在 on_user_message 持 reply_lock 期间(await gen 里)发起的,
    而该连接的 WS receive_loop 正阻塞在 ``await on_user_message`` 上读不到新消息 → confirm_response
    若走 WS 会死锁到超时。HTTP 请求由 uvicorn 并发处理(独立 task),不经 receive_loop,安全解开 Future。
    """
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    await heartbeat.on_confirm_response(str(body.get("id", "")), bool(body.get("approved", False)))
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await hub.attach(websocket)
    # 握手:已完成选人格 → 推当前 persona;否则推选人格浮层数据(onboarding Stage 1)
    if heartbeat.profile.onboarded:
        await heartbeat._push_meta()
        await heartbeat._push_history()            # 重启后回灌当前人格对话历史到主窗
    else:
        await hub.send({"type": "onboarding_select",
                        "personas": [_persona_card(p) for p in PERSONAS.values()]})
    await hub.send({"type": "cooldown_mode", "mode": heartbeat.profile.cooldown_mode})
    await hub.send({"type": "settings_changed", "settings": settings.public_view()})
    await hub.receive_loop(websocket)
