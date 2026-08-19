"use strict";
/* AgentShell 前端：三栏（对话 / 聊天 / 上下文）+ 流式 + Markdown + @引用 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const API = {
  providers: "/api/providers",
  config: "/api/config",
  token: "/api/token",
  test: "/api/test",
  workspaces: "/api/workspaces",
  conversations: "/api/conversations",
  conv: (id) => `/api/conversations/${id}`,
  agentStream: "/api/agent/stream",
  fsList: (ws, path) =>
    `/api/fs/list?ws=${encodeURIComponent(ws)}&path=${encodeURIComponent(path || "")}`,
  file: (ws, name) =>
    `/file?ws=${encodeURIComponent(ws)}&name=${encodeURIComponent(name)}`,
};

const state = {
  providers: [],
  currentConvId: null,
  workspaces: [],
  currentWorkspace: "",
  images: [], // {filename, data}
  streams: new Map(), // convId -> {convId, abort, text, steps[], dom, finished}
  toolCards: [], // 右栏步骤回填
  mentionCache: null, // {ws, files:[]}
  mentionOpen: false,
  mentionItems: [],
  mentionIndex: 0,
  mentionStart: 0,
  mentionQuery: "",
};

/* ---------- 多对话运行状态 ---------- */
function isRunningConv(convId) {
  const c = state.streams.get(convId);
  return !!(c && !c.finished);
}
function refreshRunButtons() {
  const running = isRunningConv(state.currentConvId);
  const runBtn = $("#runAgent");
  const stopBtn = $("#stopAgent");
  if (runBtn) runBtn.hidden = running;
  if (stopBtn) stopBtn.hidden = !running;
  const st = $("#agentStatus");
  if (st) st.textContent = running ? "Agent 运行中…（可点停止）" : "";
}

/* ---------- 工具 ---------- */
async function fetchJson(url) {
  const r = await fetch(url);
  return r.json();
}
async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function modelKeyOf(val) {
  const i = val.indexOf("/");
  return i < 0 ? "" : val.slice(0, i);
}
function modelIdOf(val) {
  const i = val.indexOf("/");
  return i < 0 ? val : val.slice(i + 1);
}

/* ---------- Markdown + 高亮 ---------- */
marked.setOptions({ gfm: true, breaks: true });
function renderMD(text) {
  try {
    return marked.parse(text || "");
  } catch (e) {
    return escapeHtml(text || "");
  }
}
function highlightWithin(root) {
  $$("pre code", root).forEach((el) => {
    try {
      hljs.highlightElement(el);
    } catch (e) {}
  });
  $$("pre", root).forEach((pre) => {
    if (pre.querySelector(".code-actions")) return;
    const wrap = document.createElement("div");
    wrap.className = "code-actions";
    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.textContent = "复制";
    copyBtn.addEventListener("click", () => {
      const code = pre.querySelector("code");
      const txt = code ? code.innerText : pre.innerText;
      navigator.clipboard.writeText(txt).then(() => {
        copyBtn.textContent = "已复制";
        setTimeout(() => (copyBtn.textContent = "复制"), 1200);
      });
    });
    const wBtn = document.createElement("button");
    wBtn.className = "write-file-btn";
    wBtn.textContent = "写入文件";
    wBtn.addEventListener("click", () => writeCodeToFile(pre, wBtn));
    wrap.appendChild(copyBtn);
    wrap.appendChild(wBtn);
    pre.appendChild(wrap);
  });
}
async function writeCodeToFile(pre, btn) {
  const code = pre.querySelector("code");
  const txt = code ? code.innerText : pre.innerText;
  const name = prompt("保存为文件名（相对当前工作区间，如 src/app.py）：");
  if (!name) return;
  btn.disabled = true;
  btn.textContent = "写入中…";
  try {
    const r = await postJson("/api/write-file", {
      workspace: state.currentWorkspace,
      path: name,
      content: txt,
    });
    btn.textContent = r.ok ? "✓ 已写入" : "✗ " + (r.message || "失败");
  } catch (e) {
    btn.textContent = "✗ 出错";
  }
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = "写入文件";
  }, 2000);
}

/* ---------- 初始化 ---------- */
async function init() {
  bindEvents();
  initTheme();
  await loadProviders();
  await loadConfig();
  await loadWorkspaces();
  await loadConversations();
  if (state.currentWorkspace) loadFileTree(state.currentWorkspace, "");
  renderTokenStatus();
  updateVisionHint();
  refreshRunButtons();
}

/* ---------- 主题（深色 / 浅色 / 跟随系统） ---------- */
function initTheme() {
  let t = localStorage.getItem("as-theme");
  if (!t) {
    t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  applyTheme(t);
}
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  const dark = t === "dark";
  const d = $("#hljsDark"), l = $("#hljsLight");
  if (d) d.disabled = !dark;
  if (l) l.disabled = dark;
  const btn = $("#themeToggle");
  if (btn) btn.textContent = dark ? "☀️ 浅色" : "🌙 深色";
  localStorage.setItem("as-theme", t);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "light";
  applyTheme(cur === "dark" ? "light" : "dark");
}

/* ---------- 导出当前对话为 Markdown ---------- */
async function exportCurrentConv() {
  if (!state.currentConvId) { alert("当前没有可导出的对话"); return; }
  const conv = await fetchJson(API.conv(state.currentConvId));
  const c = conv.conversation;
  let md = `# ${c.title || "对话"}\n\n`;
  (c.messages || []).forEach((m) => {
    md += `## ${m.role === "user" ? "你" : "Agent"}\n\n${m.content || ""}\n\n`;
  });
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (c.title || "conversation").replace(/[\\/:*?"<>|]/g, "_") + ".md";
  a.click();
  URL.revokeObjectURL(a.href);
}

function bindEvents() {
  $("#newConv").addEventListener("click", newConversation);
  $("#convSearch").addEventListener("input", filterConvs);
  $("#runAgent").addEventListener("click", runAgent);
  $("#stopAgent").addEventListener("click", stopAgent);
  $("#taskInput").addEventListener("keydown", onTaskKeydown);
  $("#taskInput").addEventListener("input", onTaskInput);
  $("#imgBtn").addEventListener("click", () => $("#imgInput").click());
  $("#imgInput").addEventListener("change", onImages);
  $("#modelSelect").addEventListener("change", onModelChange);
  $("#modelSelect2").addEventListener("change", () => {
    $("#modelSelect").value = $("#modelSelect2").value;
    onModelChange();
  });
  $("#saveDefault").addEventListener("click", saveDefault);
  $("#openSettings").addEventListener("click", () => ($("#settingsModal").hidden = false));
  $("#closeSettings").addEventListener("click", () => ($("#settingsModal").hidden = true));
  $("#settingsMask").addEventListener("click", () => ($("#settingsModal").hidden = true));
  $("#themeToggle").addEventListener("click", toggleTheme);
  $("#exportConv").addEventListener("click", exportCurrentConv);
  // 工作区间
  $("#wsAdd").addEventListener("click", () => {
    const p = prompt("输入要添加的项目目录绝对路径：");
    if (p) addWorkspace(p);
  });
  $("#wsAddApp").addEventListener("click", addAppWorkspace);
  $("#wsDel").addEventListener("click", delWorkspace);
  $("#wsSelect").addEventListener("change", () => {
    state.currentWorkspace = $("#wsSelect").value;
    loadFileTree(state.currentWorkspace, "");
  });
  // 右栏 tabs
  $$(".ctx-tab").forEach((t) =>
    t.addEventListener("click", () => {
      $$(".ctx-tab").forEach((x) => x.classList.remove("active"));
      $$(".ctx-panel").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $("#ctx" + t.dataset.ctx.charAt(0).toUpperCase() + t.dataset.ctx.slice(1)).classList.add("active");
    })
  );
  $("#fsRefresh").addEventListener("click", () => loadFileTree(state.currentWorkspace, ""));
  // 全局点击关闭 mention
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#mentionBox") && e.target !== $("#taskInput")) closeMentions();
  });
}

/* ---------- Providers / 模型 ---------- */
async function loadProviders() {
  const data = await fetchJson(API.providers);
  state.providers = data.providers || [];
}
function renderModelSelect() {
  const opts = (sel) => {
    sel.innerHTML = "";
    state.providers.forEach((p) => {
      const og = document.createElement("optgroup");
      og.label = p.name;
      (p.free_models || []).forEach((mid) => {
        const o = document.createElement("option");
        o.value = `${p.key}/${mid}`;
        o.textContent = `${p.name} · ${mid}`;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
  };
  opts($("#modelSelect"));
  opts($("#modelSelect2"));
}
async function loadConfig() {
  const data = await fetchJson(API.config);
  renderModelSelect();
  const val = `${data.default_provider}/${data.default_model}`;
  if ($(`#modelSelect option[value="${CSS.escape(val)}"]`)) {
    $("#modelSelect").value = val;
    $("#modelSelect2").value = val;
  }
  await loadUsage(data.usage);
}
async function loadUsage(usage) {
  usage = usage || (await fetchJson(API.config)).usage;
  const box = $("#usage");
  const rows = state.providers
    .map((p) => {
      const u = usage[p.key] || { calls: 0, tokens: 0 };
      const q = p.quota_hint || 1;
      const pct = Math.min(100, Math.round((u.calls / q) * 100));
      const cls = pct > 85 ? "danger" : pct > 60 ? "warn" : "";
      return `<div class="usage-row"><div class="urow-head"><span class="urow-name">${escapeHtml(
        p.name
      )}</span><span class="urow-num">${u.calls} 次 / ${u.tokens} tok</span></div>
        <div class="ubar"><div class="ubar-fill ${cls}" style="width:${pct}%"></div></div></div>`;
    })
    .join("");
  box.innerHTML = `<div class="usage-list">${rows}</div>`;
}
function onModelChange() {
  const val = $("#modelSelect").value;
  $("#modelSelect2").value = val;
  const pk = modelKeyOf(val);
  const mid = modelIdOf(val);
  postJson(API.config, { default_provider: pk, default_model: mid });
  updateVisionHint();
}
async function saveDefault() {
  onModelChange();
  $("#defaultMsg").textContent = "已保存默认模型 ✓";
  setTimeout(() => ($("#defaultMsg").textContent = ""), 2000);
  const data = await fetchJson(API.config);
  await loadUsage(data.usage);
}
function updateVisionHint() {
  const val = $("#modelSelect").value;
  const pk = modelKeyOf(val);
  const mid = modelIdOf(val);
  const p = state.providers.find((x) => x.key === pk);
  const hint = $("#visionHint");
  if (!p) {
    hint.textContent = "";
    return;
  }
  const vm = p.vision_models || [];
  if (vm.includes(mid)) {
    hint.textContent = "✅ 当前模型支持图片输入";
    hint.className = "ws-hint ok";
  } else if (vm.length) {
    hint.textContent = `⚠️ 该模型不支持看图，支持的有：${vm.join("、")}`;
    hint.className = "ws-hint fail";
  } else {
    hint.textContent = "该平台暂未标注视觉模型";
    hint.className = "ws-hint";
  }
}

/* ---------- 平台 Token 配置（设置弹层） ---------- */
function renderProviders() {
  const box = $("#providers");
  box.innerHTML = "";
  state.providers.forEach((p) => {
    const card = document.createElement("div");
    card.className = "pcard";
    const tag = p.free ? "free" : "paid";
    const tagText = p.free ? "免费" : "付费";
    card.innerHTML = `
      <h3>${escapeHtml(p.name)} <span class="tag ${tag}">${tagText}</span></h3>
      <div class="meta">${escapeHtml(p.rate_limit || "")}</div>
      <div class="models">模型：${escapeHtml((p.free_models || []).join("，"))}</div>
      <div class="note">${escapeHtml(p.note || "")}</div>
      <div class="portal">
        ${p.console_url ? `<a class="portal-link" href="${p.console_url}" target="_blank" rel="noopener">🔗 访问入口页</a>` : ""}
        ${p.discover_url ? `<a class="portal-link" href="${p.discover_url}" target="_blank" rel="noopener">🧭 发现页</a>` : ""}
      </div>
      <div class="tok"><input type="password" placeholder="${p.requires_key ? "填 API Key（留空不改）" : "可不填（匿名）"}" /></div>
      <div class="actions">
        <button class="btn small save">保存</button>
        <button class="btn small test">测连通</button>
      </div>
      <div class="tstatus"></div>`;
    const input = card.querySelector("input");
    card.querySelector(".save").addEventListener("click", async () => {
      const v = input.value.trim();
      if (!v) return;
      await postJson(API.token, { provider_key: p.key, api_key: v });
      input.value = "";
      const st = card.querySelector(".tstatus");
      st.className = "tstatus status-ok";
      st.textContent = "已保存 Token ✓";
      await loadProviders();
      renderTokenStatus();
    });
    card.querySelector(".test").addEventListener("click", async () => {
      const btn = card.querySelector(".test");
      const st = card.querySelector(".tstatus");
      btn.disabled = true;
      st.className = "tstatus";
      st.textContent = "连通测试中…";
      const r = await postJson(API.test, { provider_key: p.key });
      st.className = "tstatus " + (r.ok ? "status-ok" : "status-fail");
      st.textContent = (r.ok ? "✅ " : "❌ ") + r.message;
      btn.disabled = false;
      await loadProviders();
      renderTokenStatus();
    });
    box.appendChild(card);
  });
}
function renderTokenStatus() {
  const box = $("#tokenStatus");
  box.innerHTML = "";
  state.providers.forEach((p) => {
    const row = document.createElement("div");
    row.className = "token-row";
    row.innerHTML = `<span class="tr-name">${escapeHtml(p.name)}</span>
      <span class="tr-state ${p.has_token ? "configured" : "empty"}">${p.has_token ? "已配 Key" : "未配置"}</span>`;
    const btn = document.createElement("button");
    btn.className = "btn small";
    btn.textContent = "测连通";
    const res = document.createElement("span");
    res.className = "ws-hint tr-res";
    res.style.minWidth = "0";
    res.style.overflow = "hidden";
    res.style.textOverflow = "ellipsis";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      res.textContent = "测试中…";
      const r = await postJson(API.test, { provider_key: p.key });
      res.textContent = r.ok ? "✅ " + r.message : "❌ " + r.message;
      res.className = "ws-hint " + (r.ok ? "ok" : "fail");
      btn.disabled = false;
    });
    row.appendChild(btn);
    row.appendChild(res);
    box.appendChild(row);
  });
}

/* ---------- 工作区间 ---------- */
async function loadWorkspaces() {
  const data = await fetchJson(API.workspaces);
  state.workspaces = data.workspaces || [];
  state.currentWorkspace = data.default || state.workspaces[0] || "";
  const sel = $("#wsSelect");
  sel.innerHTML = "";
  state.workspaces.forEach((w) => {
    const o = document.createElement("option");
    o.value = w;
    o.textContent = w;
    sel.appendChild(o);
  });
  sel.value = state.currentWorkspace;
}
async function addWorkspace(p) {
  const r = await postJson(API.workspaces, { path: p });
  $("#wsHint").textContent = r.message;
  $("#wsHint").className = "ws-hint " + (r.ok ? "ok" : "fail");
  if (r.ok) await loadWorkspaces();
  setTimeout(() => ($("#wsHint").textContent = ""), 2500);
}
async function addAppWorkspace() {
  const data = await fetchJson(API.workspaces);
  await addWorkspace(data.app_dir);
}
async function delWorkspace() {
  const w = $("#wsSelect").value;
  const r = await postJson(API.workspaces, { path: w });
  const m = await fetch(`${API.workspaces}?x=1`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: w }) });
  await loadWorkspaces();
}

/* ---------- 对话列表 ---------- */
async function loadConversations() {
  const data = await fetchJson(API.conversations);
  renderConvList(data.conversations || []);
}
function dayGroup(ts) {
  const d = new Date((ts || 0) * 1000);
  const now = new Date();
  const sod = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const diff = Math.round((sod(now) - sod(d)) / 86400000);
  if (diff <= 0) return "今天";
  if (diff === 1) return "昨天";
  if (diff < 7) return "近 7 天";
  return "更早";
}

function renderConvList(convs) {
  const box = $("#convItems");
  box.innerHTML = "";
  if (!convs.length) {
    box.innerHTML = '<div class="conv-empty">还没有对话</div>';
    return;
  }
  const q = ($("#convSearch").value || "").toLowerCase();
  const filtered = convs.filter((c) => !q || (c.title || "").toLowerCase().includes(q));
  const groups = {};
  filtered.forEach((c) => {
    const g = dayGroup(c.updated_at || 0);
    (groups[g] = groups[g] || []).push(c);
  });
  const order = ["今天", "昨天", "近 7 天", "更早"];
  let any = false;
  order.forEach((g) => {
    if (!groups[g] || !groups[g].length) return;
    const title = document.createElement("div");
    title.className = "conv-group-title";
    title.textContent = g;
    box.appendChild(title);
    groups[g].forEach((c) => box.appendChild(renderConvItem(c)));
    any = true;
  });
  if (!any) box.innerHTML = '<div class="conv-empty">没有匹配的对话</div>';
}

function renderConvItem(c) {
  const item = document.createElement("div");
  item.className = "conv-item" + (c.id === state.currentConvId ? " active" : "");
  const span = document.createElement("span");
  span.className = "conv-title";
  span.textContent = c.title || "新对话";
  item.appendChild(span);
  const del = document.createElement("button");
  del.className = "conv-del";
  del.title = "删除";
  del.textContent = "✕";
  item.appendChild(del);
  span.addEventListener("click", () => selectConv(c.id));
  span.addEventListener("dblclick", () => startRename(c, span, item));
  del.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm("删除该对话？")) return;
    await fetch(API.conv(c.id), { method: "DELETE" });
    if (state.currentConvId === c.id) {
      state.currentConvId = null;
      $("#messages").innerHTML = '<div class="empty-hint">还没有对话，点左侧「＋ 新建对话」开始，或直接输入任务。</div>';
    }
    await loadConversations();
  });
  return item;
}

function startRename(c, span, item) {
  const input = document.createElement("input");
  input.className = "conv-title-input";
  input.value = c.title || "新对话";
  item.replaceChild(input, span);
  input.focus();
  input.select();
  const commit = async () => {
    const v = input.value.trim() || "新对话";
    await postJson("/api/conversation/rename", { id: c.id, title: v });
    await loadConversations();
  };
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    else if (e.key === "Escape") loadConversations();
  });
}
function filterConvs() {
  loadConversations();
}
async function newConversation() {
  const conv = await postJson(API.conversations, { title: "新对话" });
  await loadConversations();
  await selectConv(conv.conversation.id);
}
async function selectConv(id) {
  // 解绑旧对话正在跑的流（让它后台继续，但不再写即将销毁的 DOM）
  const old = state.streams.get(state.currentConvId);
  if (old) old.dom = null;
  state.currentConvId = id;
  $$(".conv-item").forEach((x) => x.classList.remove("active"));
  const conv = await fetchJson(API.conv(id));
  renderMessages(conv.conversation.messages || []);
  // 若切到正在跑的对话，把实时流式行接回视图；否则清空右栏步骤
  const ctx = state.streams.get(id);
  if (ctx && !ctx.finished) {
    attachStreamDom(id);
  } else {
    $("#stepList").innerHTML = "";
  }
  await loadConversations();
  refreshRunButtons();
}

/* ---------- 消息渲染 ---------- */
function renderMessages(messages) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!messages.length) {
    box.innerHTML = '<div class="empty-hint">还没有对话，点左侧「＋ 新建对话」开始，或直接输入任务。</div>';
    return;
  }
  messages.forEach((m, i) => box.appendChild(renderMessage(m, i)));
  box.scrollTop = box.scrollHeight;
}
function renderMessage(m, idx) {
  const row = document.createElement("div");
  row.className = "msg-row " + (m.role === "user" ? "msg-user" : "msg-bot");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (m.role === "user") {
    bubble.textContent = m.content || "";
    if (m.attachments && m.attachments.length) {
      const gal = document.createElement("div");
      gal.className = "attach-gallery";
      m.attachments.forEach((a) => {
        const img = document.createElement("img");
        img.className = "attach-img";
        img.src = API.file(a.ws, a.rel);
        gal.appendChild(img);
      });
      bubble.appendChild(gal);
    }
  } else {
    const inner = document.createElement("div");
    inner.className = "bubble-inner";
    inner.innerHTML = renderMD(m.content || "");
    highlightWithin(inner);
    bubble.appendChild(inner);
    if (m.steps && m.steps.length) bubble.appendChild(renderSteps(m.steps));
  }
  row.appendChild(bubble);
  if (idx != null) row.appendChild(buildMsgActions(m, idx));
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = fmtTime(m.ts) + (m.role === "user" ? "" : " · Agent");
  row.appendChild(meta);
  return row;
}

function buildMsgActions(m, idx) {
  const wrap = document.createElement("div");
  wrap.className = "msg-actions";
  const copyBtn = document.createElement("button");
  copyBtn.className = "mabtn";
  copyBtn.textContent = "复制";
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(m.content || "").then(() => {
      copyBtn.textContent = "已复制";
      setTimeout(() => (copyBtn.textContent = "复制"), 1200);
    });
  });
  wrap.appendChild(copyBtn);
  if (m.role === "assistant") {
    const reg = document.createElement("button");
    reg.className = "mabtn";
    reg.textContent = "重生成";
    reg.addEventListener("click", () => regenerate());
    wrap.appendChild(reg);
  }
  const del = document.createElement("button");
  del.className = "mabtn danger";
  del.textContent = "删除";
  del.addEventListener("click", async () => {
    if (!confirm("删除这条消息？")) return;
    await postJson("/api/message/delete", { conversation_id: state.currentConvId, index: idx });
    const conv = await fetchJson(API.conv(state.currentConvId));
    renderMessages(conv.conversation.messages || []);
  });
  wrap.appendChild(del);
  return wrap;
}

function renderSteps(steps) {
  const wrap = document.createElement("div");
  wrap.style.marginTop = "8px";
  const toggle = document.createElement("button");
  toggle.className = "step-toggle";
  toggle.textContent = "▸ 查看执行步骤";
  const box = document.createElement("div");
  box.className = "steps hidden";
  toggle.addEventListener("click", () => {
    box.classList.toggle("hidden");
    toggle.textContent = (box.classList.contains("hidden") ? "▸" : "▾") + " 查看执行步骤";
  });
  steps.forEach((s) => box.appendChild(renderStepCard(s, false)));
  wrap.appendChild(toggle);
  wrap.appendChild(box);
  return wrap;
}
function renderStepCard(s, live) {
  const card = document.createElement("div");
  card.className = "step-card";
  if (s.type === "tool") {
    card.innerHTML = `<div class="sc-head"><span class="badge tool">工具</span> ${escapeHtml(
      s.tool || ""
    )}</div><div class="sc-text">${escapeHtml(JSON.stringify(s.args || {}, null, 2))}</div>`;
    const out = document.createElement("pre");
    out.style.display = "none";
    card.appendChild(out);
  } else if (s.type === "done") {
    card.innerHTML = `<div class="sc-head"><span class="badge done">完成</span></div>`;
  } else if (s.type === "error") {
    card.innerHTML = `<div class="sc-head"><span class="badge error">出错</span></div><div class="sc-text">${escapeHtml(
      s.text || ""
    )}</div>`;
  } else {
    card.innerHTML = `<div class="sc-head"><span class="badge llm">思考</span></div><div class="sc-text">${escapeHtml(
      s.text || ""
    )}</div>`;
  }
  return card;
}

/* ---------- 本地即时气泡 ---------- */
function appendLocalUser(task, images) {
  const box = $("#messages");
  const row = document.createElement("div");
  row.className = "msg-row msg-user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = task || "(空任务)";
  if (images && images.length) {
    const gal = document.createElement("div");
    gal.className = "attach-gallery";
    images.forEach((im) => {
      const img = document.createElement("img");
      img.className = "attach-img";
      img.src = "data:" + (im.mime || "image/png") + ";base64," + im.data;
      gal.appendChild(img);
    });
    bubble.appendChild(gal);
  }
  row.appendChild(bubble);
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
}
function makeAssistantRow() {
  const box = $("#messages");
  const row = document.createElement("div");
  row.className = "msg-row msg-bot";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const inner = document.createElement("div");
  inner.className = "bubble-inner";
  inner.style.whiteSpace = "pre-wrap";
  const textSpan = document.createElement("span");
  const cursor = document.createElement("span");
  cursor.className = "cursor";
  inner.appendChild(textSpan);
  inner.appendChild(cursor);
  bubble.appendChild(inner);
  row.appendChild(bubble);
  box.appendChild(row);
  box.scrollTop = box.scrollHeight;
  return { row, inner, textSpan, cursor };
}
function appendLocalAssistant() {
  return makeAssistantRow();
}

/* 切回正在运行的对话时，把实时流式行接回视图（重建文本+步骤） */
function attachStreamDom(convId) {
  const ctx = state.streams.get(convId);
  if (!ctx || ctx.finished) return;
  const a = makeAssistantRow();
  ctx.dom = a;
  a.textSpan.textContent = ctx.text || "";
  const stepList = $("#stepList");
  stepList.innerHTML = "";
  ctx.toolCards = [];
  (ctx.steps || []).forEach((s) => {
    const card = renderStepCard(s, true);
    stepList.appendChild(card);
    if (s.type === "tool") ctx.toolCards.push(card);
  });
  switchCtx("steps");
}

/* ---------- 运行 Agent（流式，支持多对话并行） ---------- */
function stopAgent() {
  const ctx = state.streams.get(state.currentConvId);
  if (ctx && ctx.abort) ctx.abort.abort();
}
async function streamAgent(body, convId) {
  const ctx = state.streams.get(convId);
  if (!ctx) return;
  const abort = new AbortController();
  ctx.abort = abort;
  try {
    const resp = await fetch(API.agentStream, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abort.signal,
    });
    if (!resp.ok) {
      if (ctx.dom) {
        ctx.dom.textSpan.textContent = "⚠️ 服务返回 " + resp.status;
        ctx.dom.cursor.remove();
      }
      ctx.finished = true;
      refreshRunButtons();
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        chunk
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .forEach((l) => {
            const d = l.slice(5).trim();
            if (d) handleEvent(JSON.parse(d), convId);
          });
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      if (ctx.dom) ctx.dom.textSpan.textContent += "\n\n[已停止]";
    } else {
      if (ctx.dom) ctx.dom.textSpan.textContent = "⚠️ 请求出错：" + e.message;
    }
  } finally {
    if (ctx.dom) ctx.dom.cursor.remove();
    ctx.finished = true;
    refreshRunButtons();
  }
}

async function runAgent() {
  const convId = state.currentConvId;
  if (!convId || isRunningConv(convId)) return;
  const raw = $("#taskInput").value.trim();
  if (!raw) return;
  const files = extractMentions(raw);
  const task = raw.replace(/@[^\s@]+/g, "").replace(/\s+/g, " ").trim();
  if (!task && !files.length && !state.images.length) return;

  // 发送后立即清空输入框与图片预览
  $("#taskInput").value = "";
  closeMentions();
  const imagesToSend = state.images;
  state.images = [];
  renderImgPreview();

  appendLocalUser(task, imagesToSend);
  const a = appendLocalAssistant();
  state.streams.set(convId, { convId, abort: null, text: "", steps: [], toolCards: [], dom: a, finished: false });
  $("#stepList").innerHTML = "";
  switchCtx("steps");
  refreshRunButtons();
  const body = {
    task,
    workspace: state.currentWorkspace,
    vision_mode: $("#visionChk").checked,
    files,
    images: imagesToSend,
    conversation_id: convId || "",
  };
  await streamAgent(body, convId);
}

async function regenerate() {
  const convId = state.currentConvId;
  if (!convId || isRunningConv(convId)) return;
  const a = appendLocalAssistant();
  state.streams.set(convId, { convId, abort: null, text: "", steps: [], toolCards: [], dom: a, finished: false });
  $("#stepList").innerHTML = "";
  switchCtx("steps");
  refreshRunButtons();
  const body = {
    task: "",
    workspace: state.currentWorkspace,
    vision_mode: $("#visionChk").checked,
    files: [],
    images: [],
    conversation_id: convId,
    regenerate: true,
  };
  await streamAgent(body, convId);
  state.images = [];
  renderImgPreview();
}
function handleEvent(ev, convId) {
  const ctx = state.streams.get(convId);
  if (!ctx) return;
  const dom = ctx.dom;
  switch (ev.event) {
    case "llm_start":
      break;
    case "token":
      ctx.text += ev.text;
      if (dom) {
        dom.textSpan.textContent = ctx.text;
        $("#messages").scrollTop = $("#messages").scrollHeight;
      }
      break;
    case "tool":
      switchCtx("steps");
      ctx.steps.push({ type: "tool", tool: ev.tool, args: ev.args });
      if (dom) {
        const card = renderStepCard({ type: "tool", tool: ev.tool, args: ev.args }, true);
        $("#stepList").appendChild(card);
        ctx.toolCards.push(card);
      }
      break;
    case "tool_result":
      ctx.steps.push({ type: "tool_result", output: ev.output });
      if (dom) {
        const card = ctx.toolCards.shift();
        if (card) {
          const out = card.querySelector("pre");
          out.style.display = "block";
          out.textContent = ev.output || "";
        }
      }
      break;
    case "done":
      if (dom) {
        dom.inner.innerHTML = renderMD(ev.text || "");
        highlightWithin(dom.inner);
      }
      ctx.steps.push({ type: "done" });
      {
        const card = renderStepCard({ type: "done" }, true);
        $("#stepList").appendChild(card);
      }
      break;
    case "error":
      if (dom) {
        dom.inner.innerHTML = renderMD("⚠️ " + (ev.text || "出错"));
        highlightWithin(dom.inner);
      }
      ctx.steps.push({ type: "error", text: ev.text });
      {
        const card = renderStepCard({ type: "error", text: ev.text }, true);
        $("#stepList").appendChild(card);
      }
      break;
    case "saved":
      // 仅当完成的是当前正在查看的对话时才刷新视图，避免后台任务完成强行切走用户视线
      if (ev.conversation_id === state.currentConvId) {
        state.currentConvId = ev.conversation_id;
        renderMessages(ev.messages);
      }
      loadConversations();
      state.streams.delete(convId);
      refreshRunButtons();
      break;
  }
}
function switchCtx(name) {
  $$(".ctx-tab").forEach((x) => x.classList.remove("active"));
  $$(".ctx-panel").forEach((x) => x.classList.remove("active"));
  const tab = $(`.ctx-tab[data-ctx="${name}"]`);
  if (tab) tab.classList.add("active");
  $("#ctx" + name.charAt(0).toUpperCase() + name.slice(1)).classList.add("active");
}

/* ---------- @引用提取 ---------- */
function extractMentions(text) {
  const set = new Set();
  const re = /@([^\s@]+)/g;
  let m;
  while ((m = re.exec(text))) set.add(m[1]);
  return Array.from(set);
}

/* ---------- 输入：发送 / mention ---------- */
function onTaskKeydown(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    if (state.mentionOpen && state.mentionItems.length) {
      e.preventDefault();
      applyMention();
      return;
    }
    e.preventDefault();
    runAgent();
  } else if (e.key === "ArrowDown" && state.mentionOpen) {
    e.preventDefault();
    state.mentionIndex = (state.mentionIndex + 1) % state.mentionItems.length;
    renderMentionBox();
  } else if (e.key === "ArrowUp" && state.mentionOpen) {
    e.preventDefault();
    state.mentionIndex = (state.mentionIndex - 1 + state.mentionItems.length) % state.mentionItems.length;
    renderMentionBox();
  } else if (e.key === "Escape") {
    closeMentions();
  }
}
function onTaskInput() {
  const ta = $("#taskInput");
  const pos = ta.selectionStart;
  const before = ta.value.slice(0, pos);
  const m = before.match(/@([^\s@]*)$/);
  if (m) openMentions(m[1], pos - 1 - m[1].length);
  else closeMentions();
}
async function ensureFileCandidates() {
  if (state.mentionCache && state.mentionCache.ws === state.currentWorkspace)
    return state.mentionCache.files;
  const files = [];
  const ignore = new Set([
    "node_modules",
    ".git",
    "__pycache__",
    "uploads",
    ".workbuddy",
    "dist",
    "build",
    ".venv",
    "venv",
    ".idea",
  ]);
  async function walk(path, depth) {
    if (depth > 5 || files.length > 600) return;
    const data = await fetchJson(API.fsList(state.currentWorkspace, path));
    if (!data || data.error) return;
    for (const it of data.items || []) {
      const rel = path ? path + "/" + it.name : it.name;
      if (it.type === "dir") {
        if (ignore.has(it.name)) continue;
        await walk(rel, depth + 1);
      } else {
        files.push(rel);
      }
    }
  }
  await walk("", 0);
  state.mentionCache = { ws: state.currentWorkspace, files };
  return files;
}
async function openMentions(query, start) {
  const all = await ensureFileCandidates();
  const q = query.toLowerCase();
  const items = all.filter((f) => f.toLowerCase().includes(q)).slice(0, 50);
  state.mentionItems = items;
  state.mentionQuery = query;
  state.mentionStart = start;
  state.mentionIndex = 0;
  if (!items.length) {
    closeMentions();
    return;
  }
  state.mentionOpen = true;
  renderMentionBox();
}
function renderMentionBox() {
  const box = $("#mentionBox");
  box.innerHTML = "";
  box.hidden = false;
  state.mentionItems.forEach((f, i) => {
    const div = document.createElement("div");
    div.className = "mention-item" + (i === state.mentionIndex ? " active" : "");
    const isDir = f.endsWith("/");
    div.innerHTML = `<span class="mi-icon">${isDir ? "📁" : "📄"}</span>
      <span>${escapeHtml(f.split("/").pop())}</span>
      <span class="mi-path">${escapeHtml(f)}</span>`;
    div.addEventListener("click", () => {
      state.mentionIndex = i;
      applyMention();
    });
    box.appendChild(div);
  });
}
function applyMention() {
  const rel = state.mentionItems[state.mentionIndex];
  if (!rel) return closeMentions();
  const ta = $("#taskInput");
  const val = ta.value;
  const pre = val.slice(0, state.mentionStart);
  const post = val.slice(state.mentionStart + 1 + state.mentionQuery.length);
  ta.value = pre + "@" + rel + " " + post;
  ta.focus();
  ta.selectionStart = ta.selectionEnd = pre.length + 1 + rel.length + 1;
  closeMentions();
}
function closeMentions() {
  state.mentionOpen = false;
  state.mentionItems = [];
  $("#mentionBox").hidden = true;
  $("#mentionBox").innerHTML = "";
}

/* ---------- 图片 ---------- */
function onImages(e) {
  const files = Array.from(e.target.files || []);
  files.forEach((f) => {
    const reader = new FileReader();
    reader.onload = () => {
      const b64 = reader.result.split(",")[1];
      state.images.push({ filename: f.name, data: b64, mime: f.type });
      renderImgPreview();
    };
    reader.readAsDataURL(f);
  });
  e.target.value = "";
}
function renderImgPreview() {
  const box = $("#imgPreview");
  box.innerHTML = "";
  state.images.forEach((im, i) => {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    thumb.innerHTML = `<img src="data:${im.mime};base64,${im.data}" />
      <button class="thumb-del">✕</button>`;
    thumb.querySelector(".thumb-del").addEventListener("click", () => {
      state.images.splice(i, 1);
      renderImgPreview();
    });
    box.appendChild(thumb);
  });
}

/* ---------- 右栏文件树 ---------- */
async function loadFileTree(ws, path, container) {
  container = container || $("#fileTree");
  const data = await fetchJson(API.fsList(ws, path));
  if (!data || data.error) {
    container.innerHTML = `<div class="tree-loading">${escapeHtml(data ? data.error : "加载失败")}</div>`;
    return;
  }
  if (!path) {
    container.innerHTML = "";
    $("#fsPath").textContent = ws;
  }
  const items = (data.items || []).slice().sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  items.forEach((it) => {
    const rel = path ? path + "/" + it.name : it.name;
    const item = document.createElement("div");
    item.className = "tree-item " + it.type;
    const size = it.type === "file" ? (it.size >= 1024 ? (it.size / 1024).toFixed(0) + "K" : it.size + "B") : "";
    item.innerHTML = `<span class="ti-icon">${it.type === "dir" ? "📁" : "📄"}</span>
      <span class="ti-name">${escapeHtml(it.name)}</span>
      <span class="ti-size">${size}</span>`;
    if (it.type === "dir") {
      let expanded = false;
      item.addEventListener("click", async () => {
        if (!expanded) {
          expanded = true;
          const child = document.createElement("div");
          child.className = "tree-children";
          child.innerHTML = '<div class="tree-loading">加载中…</div>';
          item.appendChild(child);
          await loadFileTree(ws, rel, child);
        } else {
          expanded = false;
          const c = item.querySelector(".tree-children");
          if (c) c.remove();
        }
      });
    } else {
      item.addEventListener("click", () => {
        const ta = $("#taskInput");
        const cur = ta.value;
        ta.value = (cur ? cur + " " : "") + "@" + rel + " ";
        ta.focus();
      });
    }
    container.appendChild(item);
  });
}

/* ---------- 启动 ---------- */
// 设置弹层打开时刷新 providers
$("#openSettings").addEventListener("click", () => {
  renderProviders();
});
init();
