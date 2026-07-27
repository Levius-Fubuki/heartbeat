"""本地文件 → 纯文本(附件用)。按扩展名分派:文本/代码直读,PDF/Word lazy import 解析。

附件(per-message):用户 ➕ 选一个文件,全文随这条消息进上下文发给 agent(不持久、不向量化)。
解析纯本地;内容进 history 后随 LLM 调用发云端(与对话同等)。pypdf/python-docx 缺失时
对应类型读空(文本/代码仍可用)。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("heartbeat.memory.filereader")


def read_file(path: str) -> str:
    """读文件 → 纯文本。按扩展名分派:.pdf→pypdf / .docx→python-docx / 其余按 utf-8 文本。
    异常 / 读空 → 返回 ""(调用方据此提示「文件为空或无法读取」)。"""
    if not path:
        return ""
    path = os.path.expanduser(path)   # LLM/工具可能传 ~ 路径;展开(NSOpenPanel 附件已是绝对路径,无影响)
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            return _read_pdf(path)
        if ext == ".docx":
            return _read_docx(path)
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        log.warning("attach read failed: %s", path)
        return ""


def _read_pdf(path: str) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


def _read_docx(path: str) -> str:
    import docx
    return "\n".join(p.text for p in docx.Document(path).paragraphs)
