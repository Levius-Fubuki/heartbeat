// 就地输入框前端:桌宠底部弹出的迷你对话窗(独立普通 NSWindow,能成为 key 接收键盘)。
// WS 带重连;Enter 发送 {type:"user_message"}。仅渲染最近几条对话(迷你),expression/meta
// 交给桌宠气泡与主窗处理。与 chat.js / pet.js 共享 ws://host/ws(同一段对话)。
window.HB = window.HB || {};

(function () {
  const reply = document.getElementById("reply");
  const form = document.getElementById("input-bar");
  const input = document.getElementById("msg");
  const sendBtn = form.querySelector("button");

  // 流式回复状态:streamEl = 正在逐 token 增长的 agent 消息 div;null = 无流式进行
  let streamEl = null;
  let streamText = "";

  function setInputDisabled(disabled) {
    input.disabled = disabled;                 // 流式期间禁用,防锁排队期狂发
    if (sendBtn) sendBtn.disabled = disabled;
    if (!disabled) { try { input.focus(); } catch (e) {} }
  }

  function addChat(role, text) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    el.textContent = text;                         // textContent 防 XSS
    reply.appendChild(el);
    while (reply.children.length > 6) reply.removeChild(reply.firstChild);   // 迷你窗只留最近 6 条
    reply.scrollTop = reply.scrollHeight;
  }

  function trimTo6() {
    while (reply.children.length > 6) reply.removeChild(reply.firstChild);   // 迷你窗只留最近 6 条
    reply.scrollTop = reply.scrollHeight;
  }

  window.HB.onMessage = function (data) {
    if (data.type === "chat_start" && data.role === "agent") {
      streamEl = document.createElement("div");
      streamEl.className = "msg agent";
      streamEl.textContent = "";
      reply.appendChild(streamEl);
      streamText = "";
      reply.scrollTop = reply.scrollHeight;
      setInputDisabled(true);
    } else if (data.type === "chat_clear" && data.role === "agent") {
      streamText = "";                       // 思考过程撤回:清空当前流式消息
      if (streamEl) streamEl.textContent = "";
    } else if (data.type === "chat_chunk" && data.role === "agent" && streamEl) {
      streamText += data.delta || "";
      streamEl.textContent = streamText;
      reply.scrollTop = reply.scrollHeight;
    } else if (data.type === "chat_end" && data.role === "agent") {
      if (streamEl && data.text) streamEl.textContent = data.text;   // 以全文为准(防中途丢 chunk)
      streamEl = null;
      streamText = "";
      trimTo6();                       // 流式收尾才裁 6 条(中途裁会删掉正在增长的 agent el)
      setInputDisabled(false);
    } else if (data.type === "chat") {
      addChat(data.role === "agent" ? "agent" : "user", data.text);
    }
    // expression / meta 由桌宠气泡与主窗处理,迷你窗保持极简,忽略。
  };

  form.addEventListener("submit", (e) => {
    e.preventDefault();                            // input 里 Enter 自动触发 submit
    const t = input.value.trim();
    if (!t) return;
    window.HB.send({ type: "user_message", text: t });
    input.value = "";
  });

  // 窗口一打开就聚焦输入框(普通窗口能成为 key,键盘进得来)
  try { input.focus(); } catch (e) {}
  window.addEventListener("focus", () => { try { input.focus(); } catch (e) {} });

  let ws;
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (ev) => { try { window.HB.onMessage(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => { setTimeout(connect, 1500); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  window.HB.send = (obj) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); };

  connect();
})();
