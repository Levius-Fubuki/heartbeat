// WebSocket 客户端:接 meta / expression / chat,送出 user_message。
window.HB = window.HB || {};

(function () {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  function send(obj) {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  ws.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (window.HB.onMessage) window.HB.onMessage(data);
  };
  ws.onclose = () => {
    const s = document.getElementById("state-label");
    if (s) s.textContent = "(连接已断开)";
  };

  window.HB.send = send;
})();
