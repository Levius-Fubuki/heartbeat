// 设置窗前端:LLM 凭据/模型 + 陪伴参数旋钮。GET/POST /api/settings、POST /api/llm/models。
// WS 收 settings_changed 刷新(别处改了同步);用户正在编辑时不打断。
window.HB = window.HB || {};

(function () {
  const $ = (id) => document.getElementById(id);
  const provider = $("provider"), baseUrl = $("base-url"), apiKey = $("api-key"),
        keyHint = $("key-hint"), modelSelect = $("model-select"), modelManual = $("model-manual"),
        fetchBtn = $("fetch-btn"), fetchStatus = $("fetch-status"),
        knobsBox = $("knobs"), saveBtn = $("save-btn"), saveStatus = $("save-status"),
        allowedDirs = $("allowed-dirs"), pickDir = $("pick-dir"),
        tavilyKey = $("tavily-key"), tavilyHint = $("tavily-hint"),
        siliconflowKey = $("siliconflow-key"), siliconflowHint = $("siliconflow-hint"),
        cmdAllowlist = $("cmd-allowlist");

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  }

  function fillModels(list, current) {
    modelSelect.innerHTML = "";
    const opts = (list || []).slice();
    if (current && !opts.includes(current)) opts.unshift(current);
    if (!opts.length) opts.push("");
    opts.forEach((m) => {
      const o = document.createElement("option");
      o.value = m; o.textContent = m || "(尚未拉取 —— 手动输入或先拉取)";
      modelSelect.appendChild(o);
    });
    modelSelect.value = current || opts[0] || "";
  }

  function renderKnobs(knobs, config) {
    knobsBox.innerHTML = "";
    knobs.forEach((k) => {
      const cur = config[k.key];
      const wrap = document.createElement("div"); wrap.className = "knob";
      const lab = document.createElement("div"); lab.className = "klab";
      lab.innerHTML = escapeHtml(k.label)
        + (k.restart ? ' <span class="badge restart" title="需重启生效">↻ 重启</span>' : '')
        + (k.private ? ' <span class="badge priv" title="涉及隐私">🔒</span>' : '');
      const desc = document.createElement("div"); desc.className = "kdesc"; desc.textContent = k.desc;
      let inp;
      if (k.type === "bool") {
        inp = document.createElement("select");
        [["1", "开"], ["0", "关"]].forEach(([val, txt]) => {
          const o = document.createElement("option"); o.value = val; o.textContent = txt; inp.appendChild(o);
        });
        inp.value = cur ? "1" : "0";
      } else if (k.type === "choice") {
        inp = document.createElement("select");
        (k.options || []).forEach((opt) => {
          const o = document.createElement("option"); o.value = opt; o.textContent = opt; inp.appendChild(o);
        });
        inp.value = cur;
      } else {
        inp = document.createElement("input"); inp.type = "number";
        if (k.min != null) inp.min = k.min;
        if (k.max != null) inp.max = k.max;
        inp.value = cur;
      }
      inp.dataset.key = k.key; inp.dataset.type = k.type;
      const unit = document.createElement("span"); unit.className = "unit"; unit.textContent = k.unit || "";
      const ctrl = document.createElement("div"); ctrl.className = "kctrl";
      ctrl.appendChild(inp); ctrl.appendChild(unit);
      wrap.appendChild(lab); wrap.appendChild(desc); wrap.appendChild(ctrl);
      knobsBox.appendChild(wrap);
    });
  }

  function addDirItem(path) {
    if (!path || !allowedDirs) return;
    const existing = Array.from(allowedDirs.querySelectorAll("li")).map((li) => li.dataset.path);
    if (existing.includes(path)) return;                 // 去重
    const li = document.createElement("li"); li.className = "dir-item"; li.dataset.path = path;
    const span = document.createElement("span"); span.className = "dir-path"; span.textContent = path;
    const btn = document.createElement("button"); btn.type = "button"; btn.className = "dir-rm"; btn.textContent = "×";
    btn.title = "移除"; btn.addEventListener("click", () => { li.remove(); });
    li.appendChild(span); li.appendChild(btn);
    allowedDirs.appendChild(li);
  }

  function renderAllowedDirs(dirs) {
    if (!allowedDirs) return;
    allowedDirs.innerHTML = "";
    (dirs || []).forEach(addDirItem);
  }

  function applyView(v) {
    const llm = (v && v.llm) || {};
    if (llm.base_url) baseUrl.value = llm.base_url;
    keyHint.textContent = llm.has_key ? ("已设 " + (llm.key_hint || "")) : "未设置";
    apiKey.value = "";
    apiKey.placeholder = llm.has_key ? "已设置(留空保持不变)" : "粘贴你的 key";
    fillModels(v && v.models, llm.model);
    modelManual.value = "";
    renderKnobs((v && v.knobs) || [], (v && v.config) || {});
    renderAllowedDirs((v && v.paths && v.paths.allowed_dirs) || []);
    if (cmdAllowlist) cmdAllowlist.value = ((v && v.paths && v.paths.command_allowlist) || []).join("\n");
    const sec = (v && v.secrets) || {};
    if (tavilyHint) tavilyHint.textContent = sec.has_tavily_key ? ("已设 " + (sec.tavily_key_hint || "")) : "未设置";
    if (tavilyKey) { tavilyKey.value = ""; tavilyKey.placeholder = sec.has_tavily_key ? "已设置(留空保持不变)" : "粘贴 Tavily key"; }
    if (siliconflowHint) siliconflowHint.textContent = sec.has_siliconflow_key ? ("已设 " + (sec.siliconflow_key_hint || "")) : "未设置";
    if (siliconflowKey) { siliconflowKey.value = ""; siliconflowKey.placeholder = sec.has_siliconflow_key ? "已设置(留空保持不变)" : "粘贴硅基流动 key"; }
  }

  async function load() {
    try { const r = await fetch("/api/settings"); applyView(await r.json()); }
    catch (e) { saveStatus.textContent = "✕ 加载设置失败"; }
  }

  fetchBtn.addEventListener("click", async () => {
    fetchStatus.textContent = "拉取中…"; fetchBtn.disabled = true;
    const body = { base_url: baseUrl.value.trim() };
    if (apiKey.value.trim()) body.api_key = apiKey.value.trim();   // 用户新填的 key 优先;否则用已存
    try {
      const r = await fetch("/api/llm/models", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
      });
      const j = await r.json();
      if (j.ok) { fillModels(j.models || [], modelSelect.value); fetchStatus.textContent = `✓ 拉到 ${j.models.length} 个模型`; }
      else fetchStatus.textContent = "✕ " + (j.error || "拉取失败");
    } catch (e) { fetchStatus.textContent = "✕ 网络错误"; }
    finally { fetchBtn.disabled = false; }
  });

  // 工具与权限:选文件夹(原生 NSOpenPanel 桥接 → 回填 allowed_dirs 列表;保存才落盘)
  if (pickDir) {
    pickDir.addEventListener("click", () => {
      try { window.webkit.messageHandlers.settings.postMessage("pick_folder"); }
      catch (e) { /* 浏览器无桥接(截图/非 app)静默 */ }
    });
  }
  window.HB_onDirPicked = function (path) { addDirItem(path); };

  saveBtn.addEventListener("click", async () => {
    saveStatus.textContent = "保存中…"; saveBtn.disabled = true;
    const llm = {
      provider: provider.value,
      base_url: baseUrl.value.trim(),
      model: modelManual.value.trim() || modelSelect.value,        // 手动输入优先于下拉
    };
    if (apiKey.value.trim()) llm.api_key = apiKey.value.trim();    // 留空 = 保持已存(后端不覆盖)
    const config = {};
    knobsBox.querySelectorAll("[data-key]").forEach((inp) => {
      const key = inp.dataset.key;
      if (inp.dataset.type === "bool") config[key] = inp.value === "1";
      else if (inp.dataset.type === "choice") config[key] = inp.value;
      else { const n = parseInt(inp.value, 10); if (!Number.isNaN(n)) config[key] = n; }
    });
    const secrets = {};
    if (tavilyKey && tavilyKey.value.trim()) secrets.tavily_key = tavilyKey.value.trim();  // 留空=保持
    if (siliconflowKey && siliconflowKey.value.trim()) secrets.siliconflow_key = siliconflowKey.value.trim();  // 留空=保持
    // Phase 6d:run_command 白名单(一行/逗号一个,取首词;去重;空=禁用执行)
    const cmdSeen = {}, cmdList = [];
    (cmdAllowlist ? cmdAllowlist.value : "").split(/[\s,]+/).forEach((c) => {
      c = c.trim();
      if (c && !cmdSeen[c]) { cmdSeen[c] = 1; cmdList.push(c); }
    });
    const paths = { allowed_dirs: allowedDirs
        ? Array.from(allowedDirs.querySelectorAll("li")).map((li) => li.dataset.path) : [],
        command_allowlist: cmdList };
    try {
      const r = await fetch("/api/settings", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({llm, config, secrets, paths}),
      });
      const v = await r.json();
      applyView(v);
      saveStatus.textContent = "✓ 已保存";
    } catch (e) { saveStatus.textContent = "✕ 保存失败"; }
    finally {
      saveBtn.disabled = false;
      setTimeout(() => { saveStatus.textContent = ""; }, 2500);
    }
  });

  // WS: 收 settings_changed 刷新(别处改了同步);用户正在编辑字段时不打断
  let ws;
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.type === "settings_changed") {
          const ae = document.activeElement;
          if (ae && (ae.tagName === "INPUT" || ae.tagName === "SELECT" || ae.tagName === "TEXTAREA")) return;  // 编辑中不打断
          applyView(d.settings);
        }
      } catch (e) {}
    };
    ws.onclose = () => setTimeout(connect, 1500);
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  load();
  connect();
})();
