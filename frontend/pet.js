// 桌宠前端:表情 + 气泡。单击 → 桌宠底部弹出就地输入框;双击 → 打开主窗口。WS 带重连。
// (就地输入框用独立普通窗口承载——透明浮动窗 macOS 不让成为 key,见 shell/window.py build_input_window。)
// 精灵图按「当前人格 + 状态」动态拼 URL(/personas/image/<persona>/<persona>-<state>.png):
// 切人格(meta)/切状态(expression)都由此换图;CSS 只管各状态的动画与形变。
window.HB = window.HB || {};

(function () {
  const pet = document.getElementById("pet");
  const petBody = pet.querySelector(".pet-body");
  const speech = document.getElementById("speech");
  let currentPersona = "zorya";            // 当前人格 id(meta 到了更新;默认 zorya 兜底)
  let currentState = "idle";                // 跟踪当前状态,切人格时用它重设图

  // 气泡:agent 说话后显示,7 秒无操作(鼠标未悬停气泡)自动隐藏;悬停时暂停,离开重新计时。
  let speechTimer = null;
  const SPEECH_AUTOHIDE_MS = 7000;
  let streaming = false;                      // 流式输出中:抑制 7s 自动隐藏,chat_end 才起计时
  function hideSpeech() {
    if (speechTimer) { clearTimeout(speechTimer); speechTimer = null; }
    speech.classList.add("hidden");
  }
  function scheduleHideSpeech() {
    if (speechTimer) clearTimeout(speechTimer);
    speechTimer = setTimeout(hideSpeech, SPEECH_AUTOHIDE_MS);
  }
  function showSpeech(text) {
    speech.textContent = text;
    speech.classList.remove("hidden");
    scheduleHideSpeech();
  }
  speech.addEventListener("mouseenter", () => { if (speechTimer) { clearTimeout(speechTimer); speechTimer = null; } });
  speech.addEventListener("mouseleave", scheduleHideSpeech);

  function petImage(state) {
    // 按「当前人格 + 状态」拼图 URL;切人格/切状态都走这里换图。
    petBody.style.backgroundImage = `url("/personas/image/${currentPersona}/${currentPersona}-${state}.png")`;
  }
  function setExpr(state) {
    currentState = state;
    pet.className = "state-" + state;
    petImage(state);
    if (state === "idle" || state === "doze_off") hideSpeech();
  }

  window.HB.onMessage = function (data) {
    if (data.type === "meta") {
      const p = data.persona || {};
      if (p.id) { currentPersona = p.id; petImage(currentState); }   // 切人格:换当前状态的图
      if (p.accent) document.documentElement.style.setProperty("--accent", p.accent);
    } else if (data.type === "expression") {
      setExpr(data.state);
    } else if (data.type === "chat_start" && data.role === "agent") {
      streaming = true;
      speech.textContent = "";
      speech.classList.remove("hidden");
      if (speechTimer) { clearTimeout(speechTimer); speechTimer = null; }   // 流式期间不起 7s
    } else if (data.type === "chat_clear" && data.role === "agent") {
      speech.textContent = "";               // 思考过程撤回:清空气泡
    } else if (data.type === "chat_chunk" && data.role === "agent") {
      if (streaming) speech.textContent += data.delta || "";
    } else if (data.type === "chat_end" && data.role === "agent") {
      streaming = false;
      if (data.text) speech.textContent = data.text;     // 以全文为准(防中途丢 chunk)
      scheduleHideSpeech();                               // 流完才开始 7s 自动隐藏
    } else if (data.type === "chat") {
      if (data.role === "agent") showSpeech(data.text);   // judge 心跳一句话仍走原子 chat
    } else if (data.type === "confirm_request") {
      showSpeech("✋ 有个操作要你确认一下,双击我开主窗口");   // Phase 6d:写/执行前授权(主窗弹确认卡)
    } else if (data.type === "confirm_close") {
      // 授权结束:若仍在流式,清空 nudge 但保留可见气泡给后续 chunk;否则收起
      if (streaming) { speech.textContent = ""; speech.classList.remove("hidden"); }
      else { hideSpeech(); }
    }
  };

  // 交互:单击 → 弹就地输入框;双击 → 开主窗(计时器区分,单击延迟 250ms);长按 → 拖拽移动桌宠。
  let clickTimer = null;
  let suppressClick = false;                       // 长按拖拽刚结束时抑制紧接的 click,防误开输入框
  function postToNative(msg) { try { window.webkit.messageHandlers.pet.postMessage(msg); } catch (e) {} }
  pet.addEventListener("click", () => {
    if (suppressClick) { suppressClick = false; return; }
    if (clickTimer) return;                        // 抑制 dblclick 触发的第 2 次 click
    clickTimer = setTimeout(() => { clickTimer = null; postToNative("open_input"); }, 250);
  });
  pet.addEventListener("dblclick", () => {
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    postToNative("open_main");
  });

  // 长按 400ms 进入拖拽:setPointerCapture 让鼠标移出 webview 仍收 pointermove;
  // movementX/Y 发增量给原生移窗(macOS Y 轴向上,原生侧 dy 反向叠加)。
  let dragTimer = null;
  let dragging = false;
  pet.addEventListener("pointerdown", (e) => {
    const pid = e.pointerId;
    dragTimer = setTimeout(() => {
      dragging = true;
      try { pet.setPointerCapture(pid); } catch (_) {}
      postToNative("drag_start");
    }, 400);
  });
  pet.addEventListener("pointermove", (e) => {
    if (dragTimer && !dragging && (Math.abs(e.movementX) > 3 || Math.abs(e.movementY) > 3)) {
      clearTimeout(dragTimer); dragTimer = null;   // 按住后 400ms 内移动 = 不是长按,取消
    }
    if (dragging) {
      postToNative(JSON.stringify({ type: "drag_move", dx: e.movementX || 0, dy: e.movementY || 0 }));
    }
  });
  function endDrag(e) {
    if (dragTimer) { clearTimeout(dragTimer); dragTimer = null; }
    if (dragging) {
      suppressClick = true;                        // 拖拽刚结束,抑制接下来那次 click
      dragging = false;
      try { pet.releasePointerCapture(e.pointerId); } catch (_) {}
      postToNative("drag_end");
    }
  }
  pet.addEventListener("pointerup", endDrag);
  pet.addEventListener("pointercancel", endDrag);

  let ws;
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (ev) => { try { window.HB.onMessage(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => { setTimeout(connect, 1500); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  window.HB.send = (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); };

  petImage("idle");   // 默认 zorya-idle 兜底;握手 meta 到了再切到用户选的人格
  connect();
})();
