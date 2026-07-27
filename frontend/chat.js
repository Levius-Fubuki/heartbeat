// 主窗口聊天前端:状态行 + 聊天记录 + 输入 + 选人格浮层。WS 带重连。与桌宠窗共享对话。
window.HB = window.HB || {};

(function () {
  const personaName = document.getElementById("persona-name");
  const personaCyr = document.getElementById("persona-cyr");
  const personaArch = document.getElementById("persona-arch");
  const sepDot = document.getElementById("sep");
  const personaTrigger = document.getElementById("persona-trigger");
  const stateLabel = document.getElementById("state-label");
  const sep2 = document.getElementById("sep2");
  const interviewTrigger = document.getElementById("interview-trigger");
  const skipBtn = document.getElementById("skip-interview");
  const settingsTrigger = document.getElementById("settings-trigger");
  const voiceToggle = document.getElementById("voice-toggle");
  const modelChip = document.getElementById("model-chip");
  const chat = document.getElementById("chat");
  const form = document.getElementById("input-bar");
  const input = document.getElementById("msg");
  const onboarding = document.getElementById("onboarding");
  const onbClose = document.getElementById("onb-close");
  const onbTitle = document.getElementById("onb-title");
  const onbHint = document.getElementById("onb-hint");
  const personaGrid = document.getElementById("persona-grid");
  const cooldownBtns = document.querySelectorAll(".cd-btn");
  const attachBtn = document.getElementById("attach-btn");
  const attachMenu = document.getElementById("attach-menu");
  const attachFileItem = document.getElementById("attach-file-item");
  const attachBar = document.getElementById("attach-bar");
  const attachName = document.getElementById("attach-name");
  const attachRemove = document.getElementById("attach-remove");
  const confirmOverlay = document.getElementById("confirm-overlay");     // Phase 6d 逐次授权
  const confirmTool = document.getElementById("confirm-tool");
  const confirmArgs = document.getElementById("confirm-args");
  const confirmSummary = document.getElementById("confirm-summary");
  const confirmAllow = document.getElementById("confirm-allow");
  const confirmDeny = document.getElementById("confirm-deny");

  let personasCache = [];                      // 缓存人格列表(onboarding_select 或 /api/personas)
  let pendingAttach = null;                    // 待发附件 {name, path} 或 null(pyobjc 选完回填)
  let pendingConfirmId = null;                 // Phase 6d:当前待确认的写/执行授权请求 id

  function setCooldownMode(mode) {
    cooldownBtns.forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  }

  // 流式回复状态:streamEl = 正在逐 token 增长的 agent 消息 div;null = 无流式进行
  let streamEl = null;
  let streamBody = null;       // streamEl 内的正文容器(头像挂左,正文进 body,避免 flex 横排)
  let streamText = "";
  let isTaskStream = false;   // Phase 6a 任务模式:节点化渲染(思考段 + 工具步骤 + 回复段)
  let curTextEl = null;        // 任务模式当前文本 span(tool_step 后重建,让正文被工具步骤分段)
  let currentPersona = "zorya";   // 当前人格 id(收 meta 更新);agent 消息头像 + 消息 persona 兜底用

  function setInputDisabled(disabled) {
    input.disabled = disabled;                 // 流式期间禁用输入,防锁排队期狂发
    if (!disabled) { try { input.focus(); } catch (e) {} }
  }

  // Phase 6d:写/删/执行前逐次授权确认卡
  function showConfirm(data) {
    pendingConfirmId = data.id || null;
    confirmTool.textContent = "🔧 " + (data.tool || "操作");
    confirmSummary.textContent = data.summary || "";
    if (data.args && typeof data.args === "object" && Object.keys(data.args).length) {
      confirmArgs.textContent = Object.keys(data.args)
        .map((k) => k + ": " + data.args[k]).join("\n");
      confirmArgs.hidden = false;
    } else {
      confirmArgs.textContent = "";
      confirmArgs.hidden = true;
    }
    confirmOverlay.hidden = false;
    setInputDisabled(true);
  }
  function hideConfirm() {
    confirmOverlay.hidden = true;
    pendingConfirmId = null;
    setInputDisabled(false);
  }
  function respondConfirm(approved) {
    if (pendingConfirmId === null) return;
    const id = pendingConfirmId;
    hideConfirm();                              // 乐观隐藏;后端 confirm_close 兜底(超时/重复)
    // 走 HTTP 而非 WS:写/执行工具在 on_user_message 持锁期间发起,该连接 WS receive_loop 阻塞读不到 WS 回包
    fetch("/api/confirm", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ id: id, approved: !!approved }),
    }).catch(() => {});
  }

  const STATE_TEXT = {
    idle: "呼吸中…", alert: "咦?", settle: "…", talk: "想说话",
    engaged: "聊天中", thinking: "思考…", waiting: "等你", doze_off: "打盹…",
  };

  function setExpr(s) { stateLabel.textContent = STATE_TEXT[s] || s; }

  // 模型 chip(左上角):显示当前模型,下拉切换;读 /api/settings 或 WS settings_changed
  function updateModelChip(view) {
    const llm = (view && view.llm) || {};
    const models = (view && view.models) || [];
    const cur = llm.model || "";
    modelChip.innerHTML = "";
    const opts = models.slice();
    if (cur && !opts.includes(cur)) opts.unshift(cur);
    if (!opts.length) opts.push(cur);
    opts.forEach((m) => {
      const o = document.createElement("option"); o.value = m; o.textContent = m || "—"; modelChip.appendChild(o);
    });
    modelChip.value = cur || opts[0] || "";
  }
  async function loadModelChip() {               // 初始拉一次(WS 握手也会推 settings_changed)
    try { const r = await fetch("/api/settings"); const v = await r.json(); updateModelChip(v); updateVoice(v); } catch (e) {}
  }
  // 语音朗读开关:读 settings.config.voice_enabled 显图标(默认开;仅明确 false 才静音)。
  // 图标用内联 SVG 线条(与齿轮 ⚙ 同调;开=喇叭+声波,关=喇叭+叉),currentColor 随按钮配色。
  const VOICE_SVG_ON = '<svg class="voice-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 9a4 4 0 0 1 0 6"/><path d="M18 6.5a8 8 0 0 1 0 11"/></svg>';
  const VOICE_SVG_OFF = '<svg class="voice-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="m16 9 5 6"/><path d="m21 9-5 6"/></svg>';
  let voiceEnabled = true;
  function setVoiceIcon(on) {
    voiceEnabled = !!on;
    voiceToggle.innerHTML = voiceEnabled ? VOICE_SVG_ON : VOICE_SVG_OFF;
    voiceToggle.classList.toggle("muted", !voiceEnabled);
    voiceToggle.title = voiceEnabled ? "语音朗读(点击静音)" : "已静音(点击开启)";
  }
  function updateVoice(view) {
    const on = !(view && view.config && view.config.voice_enabled === false);   // 默认开
    setVoiceIcon(on);
  }
  // 人格头像 URL(engaged 状态的人格图,直读 /personas/image/<pid>/)。
  function personaAvatarUrl(pid) {
    return `/personas/image/${pid}/${pid}-engaged.png`;
  }
  // 给 agent 消息左侧挂头像。图加载失败 → 移除 img + 打 .no-avatar(消息退化为无头像,不影响可读)。
  function addAgentAvatar(el, pid) {
    const img = document.createElement("img");
    img.className = "avatar";
    img.src = personaAvatarUrl(pid || currentPersona);
    img.alt = "";
    img.draggable = false;
    img.onerror = () => { img.remove(); el.classList.add("no-avatar"); };
    el.appendChild(img);
  }
  function addChat(role, text, attachment, persona) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    const body = document.createElement("div");
    body.className = "msg-body";
    if (role === "agent") {                          // agent:左挂头像 + 记 data-persona(切换人格后历史消息仍可区分)
      const pid = persona || currentPersona;
      el.dataset.persona = pid;
      addAgentAvatar(el, pid);
    }
    if (attachment && attachment.name) {            // 附件 chip(仅显示文件名,全文在后端 history 里)
      const chip = document.createElement("div");
      chip.className = "attach-chip";
      chip.textContent = "📎 " + attachment.name;
      body.appendChild(chip);
    }
    if (text) {
      const tx = document.createElement("span");
      tx.textContent = text;
      body.appendChild(tx);
    }
    el.appendChild(body);
    chat.appendChild(el);
    chat.scrollTop = chat.scrollHeight;
  }

  // 访谈入口 + 跳过按钮显隐:interviewed 定入口文案(认识我/重新认识),interviewing 控制访谈态 UI
  function setInterviewUI(interviewed, interviewing) {
    if (interviewing) {
      interviewTrigger.hidden = true;
      sep2.hidden = true;
      skipBtn.hidden = false;
      stateLabel.textContent = "初次认识";
    } else {
      skipBtn.hidden = true;
      interviewTrigger.textContent = interviewed ? "重新认识" : "认识我";
      interviewTrigger.hidden = false;
      sep2.hidden = false;
    }
  }

  function renderPersonaGrid(personas, currentId) {
    personaGrid.innerHTML = "";
    personas.forEach((p) => {
      const card = document.createElement("div");
      card.className = "persona-card" + (p.id === currentId ? " current" : "");
      card.style.setProperty("--p-accent", p.accent || "#cccccc");
      card.tabIndex = 0;                    // 键盘可达 → :focus-within 也能展开详情
      // idle 头像(浮层陈列用 idle 静态态;聊天气泡用 engaged)。始终显示作身份标识
      const av = document.createElement("img");
      av.className = "pavatar";
      av.src = `/personas/image/${p.id}/${p.id}-idle.png`;
      av.alt = ""; av.draggable = false;
      av.onerror = () => av.remove();
      const meta = document.createElement("div"); meta.className = "meta";
      const nm = document.createElement("div"); nm.className = "pname"; nm.textContent = p.name || p.id;
      const cy = document.createElement("div"); cy.className = "pcyr"; cy.textContent = p.name_cyr || "";
      const tg = document.createElement("div"); tg.className = "ptag"; tg.textContent = p.archetype || "";
      meta.appendChild(nm); if (p.name_cyr) meta.appendChild(cy); meta.appendChild(tg);
      // 详细(背景/性格/招牌)包进 .details:默认折叠,hover / focus-within 展开(手风琴)
      const details = document.createElement("div"); details.className = "details";
      if (p.backstory) {
        const bk = document.createElement("div"); bk.className = "pback"; bk.textContent = p.backstory;
        details.appendChild(bk);
      }
      if (Array.isArray(p.traits) && p.traits.length) {
        const tr = document.createElement("div"); tr.className = "ptraits";
        p.traits.forEach((t) => {
          const c = document.createElement("span"); c.className = "ptrait"; c.textContent = t;
          tr.appendChild(c);
        });
        details.appendChild(tr);
      }
      if (p.signature) {
        const sg = document.createElement("div"); sg.className = "psig"; sg.textContent = p.signature;
        details.appendChild(sg);
      }
      meta.appendChild(details);
      const check = document.createElement("div"); check.className = "pcheck"; check.textContent = "✓";
      card.appendChild(av); card.appendChild(meta); card.appendChild(check);
      card.addEventListener("click", () => window.HB.send({ type: "select_persona", id: p.id }));
      personaGrid.appendChild(card);
    });
  }

  function openOverlay(title, hint, closable, currentId) {
    onbTitle.textContent = title;
    onbHint.textContent = hint;
    onbClose.hidden = !closable;              // onboarding 不可跳过;切换可取消
    renderPersonaGrid(personasCache, currentId);
    personaTrigger.classList.toggle("open", true);
    onboarding.hidden = false;
    form.style.display = "none";              // 浮层盖住全屏,藏起输入栏
  }

  function closeOverlay() {
    onboarding.hidden = true;
    personaTrigger.classList.toggle("open", false);
    form.style.display = "flex";
    try { input.focus(); } catch (e) {}
  }

  function showOnboarding(personas) {
    personasCache = personas || [];
    openOverlay("想让我是哪一种存在?", "选一个开始,之后还能再换。", false, null);
  }

  async function openSwitcher() {
    if (!onboarding.hidden) { closeOverlay(); return; }   // 再点触发器 = 收起
    let currentId = null;
    try {                                     // /api/personas 拿最新列表 + 当前人格
      const r = await fetch("/api/personas");
      const j = await r.json();
      personasCache = j.personas || personasCache;
      currentId = j.current;
    } catch (e) {}
    openOverlay("换个陪伴?", "随时切换,记忆和对话都不会丢。", true, currentId);
  }

  window.HB.onMessage = function (data) {
    if (data.type === "meta") {
      const p = data.persona || {};
      personaName.textContent = p.name || "";
      personaCyr.textContent = p.name_cyr || "";          // 西里尔小字(Зоря),Dawn Observatory 点睛
      personaCyr.hidden = !p.name_cyr;
      personaArch.textContent = p.archetype || "";        // 原型(克制·神秘)
      sepDot.hidden = !p.archetype;                        // 无原型则藏起与状态间的分隔点
      if (p.accent) document.documentElement.style.setProperty("--accent", p.accent);
      if (p.id) currentPersona = p.id;            // 缓存当前人格 id(agent 消息头像 / data-persona 兜底)
      if (!onboarding.hidden) closeOverlay();   // 选完人格 → 收起浮层(切换/onboarding 通用)
      setInterviewUI(p.interviewed === true, data.interviewing === true);  // 入口文案 + 跳过按钮(重连恢复)
    } else if (data.type === "expression") {
      setExpr(data.state);
    } else if (data.type === "chat_start" && data.role === "agent") {
      streamEl = document.createElement("div");
      streamEl.className = "msg agent" + (data.task ? " task" : "");
      const pid = data.persona || currentPersona;     // 该条消息的发出人格(后端 chat_start 带;兜底当前人格)
      streamEl.dataset.persona = pid;
      addAgentAvatar(streamEl, pid);                  // 左挂头像(切换人格后历史消息仍能区分是谁说的)
      streamBody = document.createElement("div");
      streamBody.className = "msg-body";
      streamEl.appendChild(streamBody);
      chat.appendChild(streamEl);
      streamText = "";
      isTaskStream = !!data.task;   // 任务模式:节点化(思考/工具/回复分段);陪伴:纯 textContent
      curTextEl = null;
      chat.scrollTop = chat.scrollHeight;
      setInputDisabled(true);
    } else if (data.type === "chat_clear" && data.role === "agent") {
      streamText = "";                       // 思考过程撤回:清空正文(头像保留)
      curTextEl = null;                      // 下一段 chunk 须重建文本 span
      if (streamBody) streamBody.textContent = "";
    } else if (data.type === "chat_chunk" && data.role === "agent" && streamEl) {
      if (isTaskStream) {
        if (!curTextEl) {                 // 首段或 tool_step 后的下一段:新建文本 span
          curTextEl = document.createElement("span");
          curTextEl.className = "tseg";
          streamBody.appendChild(curTextEl);
        }
        curTextEl.textContent += data.delta || "";
      } else {
        streamText += data.delta || "";
        streamBody.textContent = streamText;
      }
      chat.scrollTop = chat.scrollHeight;
    } else if (data.type === "tool_step") {
      // 任务模式工具调用:在流式消息内插可折叠步骤(🔧 工具名+参数 → 结果摘要);正文被它自然分段
      if (streamEl && isTaskStream) {
        curTextEl = null;                  // 结束当前文本段,后续 chat_chunk 建新 span
        const det = document.createElement("details");
        det.className = "tool-step";
        const sum = document.createElement("summary");
        const argStr = data.args && Object.keys(data.args).length ? JSON.stringify(data.args) : "";
        sum.textContent = "🔧 " + (data.name || "tool") + (argStr ? "  " + argStr : "");
        const res = document.createElement("div");
        res.className = "tool-result";
        res.textContent = data.result || "";
        det.appendChild(sum);
        det.appendChild(res);
        streamBody.appendChild(det);
        chat.scrollTop = chat.scrollHeight;
      }
    } else if (data.type === "chat_end" && data.role === "agent") {
      if (!isTaskStream && streamBody && data.text) streamBody.textContent = data.text;   // 陪伴:以全文为准(防丢 chunk);任务:已节点化不覆盖
      streamEl = null;
      streamBody = null;
      streamText = "";
      isTaskStream = false;
      curTextEl = null;
      setInputDisabled(false);
    } else if (data.type === "history") {
      // 重启 / 切换人格回灌:清空聊天区,按序重渲染该人格最近 N 条(带 persona → 头像)
      chat.innerHTML = "";
      streamEl = null; streamBody = null; streamText = "";
      isTaskStream = false; curTextEl = null;
      (data.messages || []).forEach((m) => {
        addChat(m.role, m.text, m.attachment_name ? { name: m.attachment_name } : null, m.persona);
      });
      chat.scrollTop = chat.scrollHeight;
    } else if (data.type === "chat") {
      addChat(data.role, data.text, data.attachment, data.persona);
    } else if (data.type === "onboarding_select") {
      showOnboarding(data.personas || []);
    } else if (data.type === "cooldown_mode") {
      setCooldownMode(data.mode);
    } else if (data.type === "interview_start") {
      setInterviewUI(false, true);
    } else if (data.type === "interview_end") {
      setInterviewUI(true, false);
    } else if (data.type === "settings_changed") {
      updateModelChip(data.settings);            // 模型/设置变了 → 刷新左上角 chip
      updateVoice(data.settings);                // 语音开关变了 → 刷新 🔊/🔇 图标
    } else if (data.type === "confirm_request") {
      showConfirm(data);                         // Phase 6d:agent 要写/删/执行 → 弹确认卡
    } else if (data.type === "confirm_close") {
      hideConfirm();                             // 用户已响应 / 超时 → 收起确认卡
    }
  };

  let ws;
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (ev) => { try { window.HB.onMessage(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => { setTimeout(connect, 1500); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  window.HB.send = (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); };

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const t = input.value.trim();
    if (!t && !pendingAttach) return;             // 有附件时允许空文本发送(无附件仍要求文本)
    const msg = { type: "user_message", text: t };
    if (pendingAttach) msg.attachment = pendingAttach;
    window.HB.send(msg);
    input.value = "";
    clearPendingAttach();
  });

  // ➕ 附件:点加号 → 小菜单 → Attach File → 主窗桥接触发原生选择器 → pyobjc 回填 HB_onAttachPicked
  function clearPendingAttach() {
    pendingAttach = null;
    attachBar.hidden = true;
    attachName.textContent = "";
  }
  function showAttachMenu(open) { attachMenu.hidden = !open; }
  attachBtn.addEventListener("click", (e) => {
    e.preventDefault();
    showAttachMenu(attachMenu.hidden);
  });
  attachFileItem.addEventListener("click", () => {
    showAttachMenu(false);
    try { window.webkit.messageHandlers.main.postMessage("attach_file"); }
    catch (err) { alert("桌面端桥接不可用(浏览器预览无法选文件)"); }
  });
  attachRemove.addEventListener("click", clearPendingAttach);
  document.addEventListener("click", (e) => {       // 点菜单外 → 关菜单
    if (!attachMenu.hidden && !attachMenu.contains(e.target) && e.target !== attachBtn) {
      showAttachMenu(false);
    }
  });
  // pyobjc 选完文件回填(主线程 evaluateJavaScript 调用)
  window.HB_onAttachPicked = function (name, path) {
    if (!name || !path) return;
    pendingAttach = { name: name, path: path };
    attachName.textContent = name;
    attachBar.hidden = false;
    try { input.focus(); } catch (e) {}
  };

  cooldownBtns.forEach((b) =>
    b.addEventListener("click", () => window.HB.send({ type: "set_cooldown_mode", mode: b.dataset.mode }))
  );

  // 运行时切换人格:点人格名 → 开浮层(再点收起);键盘 Enter/Space 同效;浮层右上角可取消。
  personaTrigger.addEventListener("click", openSwitcher);
  personaTrigger.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSwitcher(); }
  });
  onbClose.addEventListener("click", closeOverlay);

  // Stage 2 访谈:点「认识我」开始;点「跳过访谈」收尾
  interviewTrigger.addEventListener("click", () => window.HB.send({ type: "start_interview" }));
  interviewTrigger.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); window.HB.send({ type: "start_interview" }); }
  });
  skipBtn.addEventListener("click", () => window.HB.send({ type: "end_interview" }));

  // Phase 6d:确认卡 拒绝/允许 → 回发 confirm_response(后端据此放行或跳过写/执行)
  confirmAllow.addEventListener("click", () => respondConfirm(true));
  confirmDeny.addEventListener("click", () => respondConfirm(false));

  // 齿轮开设置窗(经主窗 JS→Python 桥接);模型 chip 切换 → 保存
  settingsTrigger.addEventListener("click", () => {
    try { window.webkit.messageHandlers.main.postMessage("open_settings"); }
    catch (e) {}                                  // 浏览器(agent-browser)无桥接,静默
  });
  modelChip.addEventListener("change", async () => {
    try {
      await fetch("/api/settings", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({llm: {model: modelChip.value}}),
      });
    } catch (e) {}                                // 失败静默;WS settings_changed 会回推正确值
  });
  // 语音朗读开关:点击切换;静音时同时 POST /api/voice/stop 即时掐断当前朗读(否则长回复会念到尾)
  voiceToggle.addEventListener("click", async () => {
    const next = !voiceEnabled;
    setVoiceIcon(next);                                   // 乐观即时反馈(广播回来再校正)
    try {
      await fetch("/api/settings", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({config: {voice_enabled: next}}),
      });
      if (!next) { try { await fetch("/api/voice/stop"); } catch (e) {} }
    } catch (e) { setVoiceIcon(!next); }                  // 失败回滚
  });

  loadModelChip();
  connect();
})();
