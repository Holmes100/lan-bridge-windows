// LAN Bridge (Windows) 前端 —— 启停转发、状态、设置、实时日志。
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);
const els = {};

function setDot(s) { els.dot.className = "dot " + s; }
function setYes(el, val) { el.textContent = val ? "是" : "否"; el.className = "v " + (val ? "yes" : "no"); }

async function refresh() {
  let s;
  try { s = await invoke("gateway_status"); }
  catch (e) { els.stateText.textContent = "状态获取失败：" + e; return; }

  els.lanUrl.value = s.lan_url || "";
  setYes(els.vRunning, s.running);
  setYes(els.vListen, !!s.listening);
  if (s.running) {
    els.vSession.textContent = s.session_acquired ? "是" : "…";
    els.vSession.className = "v " + (s.session_acquired ? "yes" : "warn");
    setYes(els.vContent, !!s.content_listening);
    if (s.listening && s.session_acquired) { setDot("green"); els.stateText.textContent = "运行中"; }
    else { setDot("yellow"); els.stateText.textContent = "启动中…"; }
    els.toggleBtn.textContent = "停止转发";
    els.toggleBtn.className = "primary stop";
  } else {
    setDot("red"); els.stateText.textContent = "已停止";
    els.vSession.textContent = "—"; els.vSession.className = "v";
    els.vContent.textContent = "—"; els.vContent.className = "v";
    els.toggleBtn.textContent = "启动转发";
    els.toggleBtn.className = "primary";
  }
  els.toggleBtn.disabled = false;
}

function fillConfig(c) {
  els.appPort.value = c.app_port;
  els.contentPort.value = c.content_port;
  els.lanPort.value = c.lan_port;
  els.lanContentPort.value = c.lan_content_port;
  els.wslDistro.value = c.wsl_distro || "";
  els.sandboxHome.value = c.sandbox_home;
  els.scienceBin.value = c.science_bin;
  els.token.value = c.token;
  els.autostart.checked = !!c.autostart;
}

function appendLog(line) {
  const pre = els.log;
  pre.textContent += line + "\n";
  const lines = pre.textContent.split("\n");
  if (lines.length > 600) pre.textContent = lines.slice(-600).join("\n");
  pre.scrollTop = pre.scrollHeight;
}

window.addEventListener("DOMContentLoaded", async () => {
  for (const k of ["dot","stateText","toggleBtn","lanUrl","copyBtn","vRunning","vListen","vSession","vContent",
    "appPort","contentPort","lanPort","lanContentPort","wslDistro","sandboxHome","scienceBin","token","autostart",
    "saveBtn","saveMsg","log","clearLog"]) { els[k] = $(k); }

  // 先挂日志监听，再拉配置/状态，避免漏掉启动日志
  try { await listen("lan://log", (e) => appendLog(e.payload)); }
  catch (e) { appendLog("[日志监听失败] " + e); }

  let cfg = null;
  try { cfg = await invoke("get_config"); fillConfig(cfg); } catch (e) { appendLog("[配置读取失败] " + e); }

  els.toggleBtn.onclick = async () => {
    els.toggleBtn.disabled = true;
    const stopping = els.toggleBtn.classList.contains("stop");
    try { await invoke(stopping ? "gateway_stop" : "gateway_start"); }
    catch (e) { appendLog("[错误] " + e); }
    refresh();
  };

  els.copyBtn.onclick = async () => {
    const u = els.lanUrl.value;
    if (!u) return;
    try { await navigator.clipboard.writeText(u); els.copyBtn.textContent = "已复制 ✓"; setTimeout(() => els.copyBtn.textContent = "复制", 1200); }
    catch (e) { appendLog("[复制失败] " + e); }
  };

  els.saveBtn.onclick = async () => {
    const c = {
      app_port: +els.appPort.value, content_port: +els.contentPort.value,
      lan_port: +els.lanPort.value, lan_content_port: +els.lanContentPort.value,
      wsl_distro: els.wslDistro.value.trim(),
      sandbox_home: els.sandboxHome.value.trim(), science_bin: els.scienceBin.value.trim(),
      token: els.token.value, autostart: els.autostart.checked,
    };
    try { await invoke("set_config", { cfg: c }); els.saveMsg.textContent = "已保存 ✓"; setTimeout(() => els.saveMsg.textContent = "", 1500); }
    catch (e) { els.saveMsg.textContent = "保存失败：" + e; }
  };

  els.clearLog.onclick = () => { els.log.textContent = ""; };

  await refresh();
  setInterval(refresh, 2000);

  // 自启
  if (cfg && cfg.autostart) {
    try { await invoke("gateway_start"); }
    catch (e) { appendLog("[自启失败] " + e); }
  }
});
