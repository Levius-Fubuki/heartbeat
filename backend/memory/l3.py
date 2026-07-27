"""L3 情景记忆:每段已结束的对话归档为 JSONL,新对话召回最近几段注入 system prompt。

存储 memory/episodic.jsonl,每行一条 JSON(崩溃安全:逐行 append + flush + fsync)。

Phase 4 向量召回:entry 加 id;build_index() 给每条算 embedding(bge-small-zh,本地 fastembed)
建 numpy 矩阵;recall(query) 按余弦相似度取 top_k。无 numpy/fastembed 或 EMBED_ENABLED=False 时
降级(recent/recent_text 仍可用,recall 返 [])。索引延迟构建——__init__ 不建(避 app.py 模块级
实例化时阻塞 import),由 lifespan 或调用方显式 build_index()。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from backend.config import (
    EPISODIC_RECALL_N, EMBED_ENABLED,
    MEMORY_DIR, MEMORY_EPISODIC_MAX_CHARS, RECALL_TOP_K,
)

log = logging.getLogger("heartbeat.memory.l3")

_EPISODIC_PATH = MEMORY_DIR / "episodic.jsonl"

try:
    import numpy as np  # noqa: F401
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def _embed_enabled() -> bool:
    """向量召回是否可用(总开关 + numpy 在位)。"""
    return EMBED_ENABLED and _HAS_NUMPY


class EpisodicMemory:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _EPISODIC_PATH
        # 向量索引(内存);由 build_index() 显式构建(__init__ 不建,避阻塞 import)
        self._ids: list = []                 # 与 _matrix 行一一对应
        self._matrix = None                  # np.ndarray shape=(N, dim) float32(归一化);None=未建
        self._entries_cache: dict = {}       # id → entry,recall 回查避免再扫文件
        self._index_lock = threading.Lock()  # 保护 append 增量更新与 recall 读的并发
        self._cache_path = self.path.with_suffix(".npz")  # 向量缓存跟随 jsonl 路径(生产/probe 互不污染)

    # ---- 索引构建(延迟;由 lifespan / 调用方显式调)----
    def build_index(self) -> bool:
        """全量读 jsonl → 老 entry 补 id(迁移回写)→ 加载/重算 embedding → 建 matrix。

        幂等。失败(fastembed 不可用等)→ _matrix=None,recall 降级返 []。
        返回是否成功。
        """
        if not _embed_enabled():
            log.info("episodic vector index disabled (EMBED_ENABLED=%s, numpy=%s)", EMBED_ENABLED, _HAS_NUMPY)
            return False
        try:
            from backend.memory.embeddings import embed_texts
            entries = self._read_all()
            if not entries:
                log.info("episodic index ready: 0 entries (empty)")
                return True  # 空,索引就绪(空矩阵)
            # 迁移:补 id
            dirty = False
            for e in entries:
                if not e.get("id"):
                    e["id"] = str(uuid.uuid4())
                    dirty = True
            if dirty:
                log.info("migrating %d episodic entries: backfilling id + rewriting jsonl", len(entries))
                self._rewrite_all(entries)
            with self._index_lock:
                self._entries_cache = {e["id"]: e for e in entries}
                self._ids = [e["id"] for e in entries]
            # 缓存命中?
            cached = self._load_cache()
            if cached is not None:
                with self._index_lock:
                    self._matrix = cached
                log.info("episodic index ready: %d entries (from cache)", len(self._ids))
                return True
            # 重算
            texts = [self._embed_text(e) for e in entries]
            mat = embed_texts(texts)
            with self._index_lock:
                self._matrix = mat
                self._save_cache()
            log.info("episodic index ready: %d entries (rebuilt, dim=%s)",
                     len(self._ids), mat.shape[1] if mat.size else "?")
            return True
        except Exception:  # noqa: BLE001
            log.exception("episodic index build failed (recall disabled, recent_* still work)")
            with self._index_lock:
                self._matrix = None
            return False

    def append(self, entry: dict) -> None:
        """崩溃安全 append + 增量更新向量索引。

        先补 id,再 fsync 写 jsonl;若索引已建(_matrix 非 None),算 embedding 增量 vstack + 回写缓存。
        索引未建时只写文件,下次 build_index 全量补(不丢)。
        """
        if not entry.get("id"):
            entry["id"] = str(uuid.uuid4())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:  # noqa: BLE001
            log.warning("episodic append failed: %s", self.path)
            return
        # 增量更新索引(索引未建则跳过,等下次 build_index 全量补)
        with self._index_lock:
            matrix_ready = self._matrix is not None
        if not matrix_ready:
            return
        try:
            from backend.memory.embeddings import embed_texts
            vec = embed_texts([self._embed_text(entry)])  # shape (1, dim)
            with self._index_lock:
                self._entries_cache[entry["id"]] = entry
                self._ids.append(entry["id"])
                self._matrix = np.vstack([self._matrix, vec])
                self._save_cache()
        except Exception:  # noqa: BLE001
            log.warning("episodic append index update failed (non-fatal; rebuild on next build_index)")

    def recall(self, query: str, top_k: int = RECALL_TOP_K,
               persona_id: Optional[str] = None) -> List[dict]:
        """向量召回 top_k 完整 entries(相似度降序)。空矩阵/未建索引/无 numpy → []。

        persona_id 非空时只在该人格的 entries 里召回(L1/L3 记忆按人格隔离);None=全局(默认)。
        过滤在打分后:非匹配项 score 置 -inf,argsort 自然沉底,返回时再剔除。索引/npz 不动。
        """
        if not _embed_enabled():
            return []
        with self._index_lock:
            if self._matrix is None or self._matrix.shape[0] == 0 or not self._ids:
                return []
            ids = list(self._ids)
            mat = self._matrix
            cache = dict(self._entries_cache)
        try:
            from backend.memory.embeddings import embed_query
            q = embed_query(query)       # (dim,),已归一化
            scores = mat @ q             # (N,),归一化点积 = 余弦
            if persona_id is not None:   # 按人格隔离:非匹配项置 -inf(下沉 + 返回时剔除)
                mask = np.array([
                    (cache[eid].get("persona") == persona_id) if eid in cache else False
                    for eid in ids
                ], dtype=bool)
                scores = np.where(mask, scores, -np.inf)
            k = min(top_k, len(ids))
            if k <= 0:
                return []
            top_idx = np.argsort(-scores)[:k]   # 降序取前 k(小数据,直接 argsort)
            return [cache[ids[i]] for i in top_idx
                    if ids[i] in cache and scores[i] != -np.inf]
        except Exception:  # noqa: BLE001
            log.exception("recall failed")
            return []

    # ---- 最近 N 段(注入 system prompt 用;不依赖向量)----
    def recent(self, n: int = EPISODIC_RECALL_N,
               persona_id: Optional[str] = None) -> List[dict]:
        """返回最近 n 条 entry(旧→新)。全文件读后取尾(文件小,简单可靠)。

        persona_id 非空时先按人格过滤再取尾(L1/L3 隔离);None=全局(默认)。
        """
        entries = self._read_all()
        if persona_id is not None:
            entries = [e for e in entries if e.get("persona") == persona_id]
        return entries[-n:] if n > 0 else entries

    def recent_text(self, n: int = EPISODIC_RECALL_N,
                    max_chars: int = MEMORY_EPISODIC_MAX_CHARS,
                    persona_id: Optional[str] = None) -> str:
        """格式化为可注入 system 的文本。无内容 → ""(调用方据此跳过注入)。

        段级截断(非字符级,避免切断单句):按旧→新累计,超 max_chars 从最旧整段丢,保最近一段完整。
        persona_id 非空时只取该人格的最近段(L1/L3 隔离)。
        """
        entries = self.recent(n, persona_id=persona_id)
        if not entries:
            return ""
        blocks = [self._format_entry(e) for e in entries]
        while sum(len(b) for b in blocks) > max_chars and len(blocks) > 1:
            blocks.pop(0)
        return "\n\n".join(blocks)

    # ---- 辅助 ----
    @staticmethod
    def _format_entry(e: dict) -> str:
        ts = e.get("ts", "")
        app = e.get("foreground_app")
        header = f"[对话 · {ts}" + (f" · 当时在 {app}]" if app else "]")
        lines = [header]
        for m in e.get("messages", []):
            role = "A" if m.get("role") == "agent" else "U"
            lines.append(f"  {role}: {m.get('text', '')}")
        return "\n".join(lines)

    @staticmethod
    def _embed_text(e: dict) -> str:
        """用于 embedding 的纯文本。复用 _format_entry(已含对话内容+上下文),
        不额外调 LLM 做 summary(MVP 省成本;后续可换 summary 提升精度)。"""
        return EpisodicMemory._format_entry(e)

    @staticmethod
    def build_entry(persona_id: str, trigger: Optional[dict],
                    foreground_app: Optional[str], messages: List[dict]) -> dict:
        """构造一条 entry,补 ts + id。messages 应由调用方传入深拷贝副本。"""
        return {
            "id": str(uuid.uuid4()),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "persona": persona_id,
            "trigger": trigger or {},
            "foreground_app": foreground_app,
            "messages": messages,
        }

    def _read_all(self) -> List[dict]:
        """全量读 jsonl(跳过损坏行)。抽出供 build_index/recent 共用。"""
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
                        out.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue            # 跳过损坏行
        except Exception:  # noqa: BLE001
            log.warning("episodic read failed: %s", self.path)
            return []
        return out

    def _rewrite_all(self, entries: List[dict]) -> None:
        """全量原子重写 jsonl(迁移补 id 用)。tmp + os.replace,崩溃安全。"""
        tmp = self.path.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self.path))
        except Exception:  # noqa: BLE001
            log.warning("episodic rewrite failed: %s", tmp)

    def _load_cache(self):
        """加载 npz 缓存。仅当 ids 顺序与当前一致才返回 matrix,否则 None(需重算)。"""
        if not self._cache_path.exists():
            return None
        try:
            data = np.load(self._cache_path, allow_pickle=True)
            cached_ids = [str(s) for s in data["ids"].tolist()]
            if cached_ids == self._ids:
                return np.asarray(data["matrix"], dtype=np.float32)
        except Exception:  # noqa: BLE001
            log.warning("embeddings cache load failed: %s", self._cache_path)
        return None

    def _save_cache(self) -> None:
        """回写 npz 缓存(ids + matrix)。tmp + os.replace 原子。需持 _index_lock 调用。"""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            # np.savez 若文件名不以 .npz 结尾会自动追加 .npz → os.replace 找不到源文件;
            # 故 tmp 名显式带 .npz,savez 不再追加,replace 源/目路径一致。
            tmp = self._cache_path.with_name(self._cache_path.stem + ".tmp.npz")
            np.savez(tmp, ids=np.asarray(self._ids, dtype=object), matrix=self._matrix)
            os.replace(str(tmp), str(self._cache_path))
        except Exception:  # noqa: BLE001
            log.warning("embeddings cache save failed")
