"""设置存储 + gateway 热读 单测(秒级,不联网)。

验:① Settings 回退 config/.env 默认 ② update 持久化 + reload ③ public_view 掩码(无完整 key)
④ KNOBS coerce/clamp ⑤ gateway _current_model() 跟随 settings + client 按 (key,base) 签名重建。
用临时 settings.yaml + monkeypatch gateway.settings,不碰真实 memory/。
用法:.venv/bin/python scripts/settings_probe.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.memory import settings as settings_mod
from backend.memory.settings import Settings, _coerce, KNOBS
from backend.llm import gateway

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("  ✓" if cond else "  ✗ FAIL") + " " + name)
    if cond:
        PASS += 1
    else:
        FAIL += 1


def run():
    global PASS, FAIL
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "settings.yaml"

        print("[1] Settings 回退 config/.env 默认(空文件)")
        s = Settings(path=path)
        check("api_key 回退(.env)", bool(s.api_key))
        check("base_url 回退", s.base_url.startswith("http"))
        check("model 回退 deepseek-chat", s.model == "deepseek-chat")
        check("heartbeat_interval=8", s.heartbeat_interval == 8)
        check("recall_top_k=3", s.recall_top_k == 3)
        check("tool_include_window_title=True", s.tool_include_window_title is True)

        print("[2] update 持久化 + reload")
        changed = s.update(llm={"model": "my-model", "api_key": "sk-test1234567890"},
                           config={"recall_top_k": 7, "tool_include_window_title": False,
                                   "heartbeat_interval": 30})
        check("update 返回有变更", changed)
        check("文件落盘", path.exists())
        s2 = Settings(path=path)  # reload
        check("model 持久化", s2.model == "my-model")
        check("api_key 持久化", s2.api_key == "sk-test1234567890")
        check("recall_top_k=7", s2.recall_top_k == 7)
        check("tool_include_window_title=False", s2.tool_include_window_title is False)
        check("heartbeat_interval=30", s2.heartbeat_interval == 30)

        print("[3] public_view 掩码(永不返完整 key)")
        v = s2.public_view()
        check("llm 四键齐全", set(["provider", "base_url", "model", "has_key", "key_hint"]).issubset(v["llm"].keys()))
        check("无完整 key 泄漏", "sk-test1234567890" not in str(v))
        check("key_hint 含尾4位 abcd-ish", v["llm"]["key_hint"].endswith("7890"))
        check("config 含全部 knob", set(v["config"].keys()) == {k["key"] for k in KNOBS})

        print("[4] KNOBS coerce/clamp")
        topk = next(k for k in KNOBS if k["key"] == "recall_top_k")
        check("int clamp 上限", _coerce(topk, 999) == topk["max"])
        check("int clamp 下限", _coerce(topk, -5) == topk["min"])
        check("int 容忍 '8.0'", _coerce(topk, "8.0") == 8)
        check("junk→None", _coerce(topk, "abc") is None)
        wint = next(k for k in KNOBS if k["key"] == "tool_include_window_title")
        check("bool True 变体", _coerce(wint, "开") is True)
        check("bool False 变体", _coerce(wint, 0) is False)
        check("bool junk→None", _coerce(wint, "maybe") is None)
        # update 时空 api_key 不误清
        s3 = Settings(path=path)
        s3.update(llm={"api_key": ""})  # 空 = 保持
        check("空 api_key 不误清", s3.api_key == "sk-test1234567890")

        print("[5] gateway 热读:_current_model 跟随 + client 签名重建")
        saved = gateway.settings
        try:
            gt = Settings(path=path)
            gateway.settings = gt
            check("_current_model 跟随 settings", gateway._current_model() == "my-model")
            # client 按 (key, base) 签名:换 key → 重建
            gateway._client = None
            gateway._async_client = None
            gateway._client_sig = None
            gt.update(llm={"api_key": "sk-AAAA", "base_url": "https://a.example.com"})
            c1 = gateway._get_client()
            sig1 = gateway._client_sig
            gt.update(llm={"api_key": "sk-BBBB", "base_url": "https://b.example.com"})
            c2 = gateway._get_client()
            check("换 key/base 后签名变", gateway._client_sig != sig1)
            check("client 对象重建(c1 is not c2)", c1 is not c2)
            # 同签名不重建
            c3 = gateway._get_client()
            check("同签名不重建(c2 is c3)", c2 is c3)
            # 无 key → None:须同时清 settings 覆盖与 .env 回退(property 走 or LLM_API_KEY)
            gt._data["api_key"] = ""
            saved_env_key = settings_mod.LLM_API_KEY
            settings_mod.LLM_API_KEY = ""
            gateway._client = None
            try:
                check("无 key → None", gateway._get_client() is None)
            finally:
                settings_mod.LLM_API_KEY = saved_env_key
        finally:
            gateway.settings = saved
            gateway._client = None
            gateway._async_client = None
            gateway._client_sig = None

        print("[6] Phase 6b:secrets(tavily_key)/paths(allowed_dirs)段")
        s6 = Settings(path=Path(d) / "s6.yaml")       # 独立空文件,隔离 _data
        check("tavily_key 默认回退 env", s6.tavily_key == settings_mod.HEARTBEAT_TAVILY_KEY)
        check("allowed_dirs 默认空", s6.allowed_dirs == [])
        ch = s6.update(secrets={"tavily_key": "tvly-abcdefghijklmnop"})
        check("secrets 有变更", ch)
        check("tavily_key 持久化", s6.tavily_key == "tvly-abcdefghijklmnop")
        v6 = s6.public_view()
        check("secrets 块齐全", set(["has_tavily_key", "tavily_key_hint"]).issubset(v6["secrets"].keys()))
        check("tavily_key 不泄漏完整", "tvly-abcdefghijklmnop" not in str(v6))
        check("tavily_key_hint 尾4位", v6["secrets"]["tavily_key_hint"].endswith("mnop"))
        s6.update(secrets={"tavily_key": ""})          # 空 = 保持(不误清)
        check("空 tavily_key 不误清", s6.tavily_key == "tvly-abcdefghijklmnop")
        s6.update(paths={"allowed_dirs": ["/Users/x/proj", "/tmp/y"]})
        check("allowed_dirs 覆盖", s6.allowed_dirs == ["/Users/x/proj", "/tmp/y"])
        v6b = s6.public_view()
        check("paths 块含 allowed_dirs(不掩码)", v6b["paths"]["allowed_dirs"] == ["/Users/x/proj", "/tmp/y"])
        s6.update(paths={"allowed_dirs": []})          # 空列表 = 清空
        check("空 allowed_dirs 清空", s6.allowed_dirs == [])

        print("[7] Phase 语音-SF:siliconflow_key 段(cosyvoice 引擎用,镜像 tavily_key)")
        s7 = Settings(path=Path(d) / "s7.yaml")       # 独立空文件,隔离 _data
        check("siliconflow_key 默认回退 env", s7.siliconflow_key == settings_mod.HEARTBEAT_SILICONFLOW_KEY)
        ch = s7.update(secrets={"siliconflow_key": "sk-sf-abcdefghijklmn"})
        check("siliconflow_key 有变更", ch)
        check("siliconflow_key 持久化", s7.siliconflow_key == "sk-sf-abcdefghijklmn")
        v7 = s7.public_view()
        check("secrets 块含 siliconflow", set(["has_siliconflow_key", "siliconflow_key_hint"]).issubset(v7["secrets"].keys()))
        check("siliconflow_key 不泄漏完整", "sk-sf-abcdefghijklmn" not in str(v7))
        check("siliconflow_key_hint 尾4位", v7["secrets"]["siliconflow_key_hint"].endswith("klmn"))
        s7.update(secrets={"siliconflow_key": ""})      # 空 = 保持(不误清)
        check("空 siliconflow_key 不误清", s7.siliconflow_key == "sk-sf-abcdefghijklmn")

    print(f"\n{'ALL PASS' if not FAIL else str(FAIL) + ' FAILED'} ({PASS} passed)")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(run())
