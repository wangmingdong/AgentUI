// AgentShell 前端逻辑（原生 JS，无框架依赖）

const $ = (sel) => document.querySelector(sel);

// ---------- Tab 切换 ----------
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("#" + t.dataset.tab).classList.add("active");
  });
});

// ---------- 加载配置 + 平台 ----------
let USAGE = {}; // 全局用量（来自 /api/config）

async function refresh() {
  const [provRes, cfgRes] = await Promise.all([
    fetch("/api/providers").then((r) => r.json()),
    fetch("/api/config").then((r) => r.json()),
  ]);
  USAGE = cfgRes.usage || {};
  renderModelSelect(provRes.providers, cfgRes);
  renderProviders(provRes.providers);
  renderUsage(USAGE, provRes.providers);
}

function renderModelSelect(providers, cfg) {
  const sel = $("#modelSelect");
  sel.innerHTML = "";
  providers.forEach((p) => {
    const og = document.createElement("optgroup");
    og.label = p.name;
      (p.free_models || []).forEach((m) => {
        const opt = document.createElement("option");
        opt.value = (p.prefix || p.key) + "/" + m;
        opt.textContent = m + (p.free ? "（免费）" : "");
        og.appendChild(opt);
      });
    sel.appendChild(og);
  });
  // 选中默认
  const def = cfg.default_provider + "/" + cfg.default_model;
  if ([...sel.options].some((o) => o.value === def)) sel.value = def;
  else if (cfg.default_model) {
    // 裸模型名也能选
    const bare = [...sel.options].find((o) => o.value.endsWith("/" + cfg.default_model));
    if (bare) sel.value = bare.value;
  }
}

function renderProviders(providers) {
  const box = $("#providers");
  box.innerHTML = "";
  providers.forEach((p) => {
    const card = document.createElement("div");
    card.className = "pcard";
    const tags =
      (p.free ? '<span class="tag free">免费</span>' : '<span class="tag paid">付费</span>') +
      (p.requires_key ? "" : '<span class="tag noreq">免Key</span>');
    const discovery = p.discover_url
      ? `<div class="meta"><a href="${p.discover_url}" target="_blank" rel="noopener">🔗 官方发现页</a></div>`
      : "";
    card.innerHTML = `
      <h3>${p.name} ${tags}</h3>
      <div class="meta">${p.base_url}</div>
      <div class="meta">限速：${p.rate_limit}</div>
      <div class="models">免费模型：${(p.free_models || []).join("、") || "—"}</div>
      ${discovery}
      <div class="meta note">${p.note}</div>
      <input type="password" class="tok" placeholder="API Key（${p.has_token ? "已配置" : "未配置"}）" />
      <div class="usage">
        <div class="ubar"><div class="ubar-fill"></div></div>
        <div class="umeta"></div>
      </div>
      <div class="actions">
        <button class="btn" data-act="save">保存</button>
        <button class="btn" data-act="test">测连通</button>
        <span class="tstatus"></span>
      </div>`;

    const tokInput = card.querySelector(".tok");
    const saveBtn = card.querySelector('[data-act="save"]');
    const testBtn = card.querySelector('[data-act="test"]');
    const st = card.querySelector(".tstatus");
    const fill = card.querySelector(".ubar-fill");
    const umeta = card.querySelector(".umeta");

    // 用量进度条（累计调用 vs 参考限额）
    const u = USAGE[p.key] || { calls: 0, tokens: 0 };
    const quota = p.quota_hint || 0;
    const pct = quota ? Math.min(u.calls / quota, 1) * 100 : 0;
    fill.style.width = pct.toFixed(0) + "%";
    fill.classList.toggle("warn", pct >= 70 && pct < 90);
    fill.classList.toggle("danger", pct >= 90);
    umeta.textContent = `用量：已调用 ${u.calls} 次 · 约 ${u.tokens} tokens（参考上限 ${quota || "?"} 次/周期，仅供参考）`;

    // 保存：反馈 + 防抖
    saveBtn.addEventListener("click", async () => {
      if (saveBtn.disabled) return; // 防抖：请求期间禁止重复点击
      const key = tokInput.value.trim();
      saveBtn.disabled = true;
      const orig = saveBtn.textContent;
      saveBtn.textContent = "保存中…";
      saveBtn.classList.remove("ok", "fail");
      try {
        const r = await fetch("/api/token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider_key: p.key, api_key: key }),
        });
        if (r.ok) {
          saveBtn.textContent = "✓ 已保存";
          saveBtn.classList.add("ok");
          tokInput.placeholder = key ? "已配置" : "未配置";
          if (key) tokInput.value = ""; // 清空明文，避免残留
        } else {
          saveBtn.textContent = "✗ 保存失败";
          saveBtn.classList.add("fail");
        }
      } catch (e) {
        saveBtn.textContent = "✗ 保存失败";
        saveBtn.classList.add("fail");
      } finally {
        setTimeout(() => {
          saveBtn.textContent = orig;
          saveBtn.disabled = false;
          saveBtn.classList.remove("ok", "fail");
        }, 2200);
      }
    });

    // 测连通：loading 提示 + 防抖
    testBtn.addEventListener("click", async () => {
      if (testBtn.disabled) return; // 防抖
      testBtn.disabled = true;
      st.textContent = "测试中…";
      st.className = "tstatus";
      try {
        const r = await fetch("/api/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider_key: p.key }),
        }).then((x) => x.json());
        st.textContent = r.message;
        st.className = "tstatus " + (r.ok ? "status-ok" : "status-fail");
        // 测连通成功说明调用数 +1，刷新进度条
        if (r.ok) refresh();
      } catch (e) {
        st.textContent = "请求出错：" + e.message;
        st.className = "tstatus status-fail";
      } finally {
        testBtn.disabled = false;
      }
    });

    box.appendChild(card);
  });
}

function renderUsage(usage, providers) {
  const box = $("#usage");
  const quotaMap = {};
  const nameMap = {};
  (providers || []).forEach((p) => {
    quotaMap[p.key] = p.quota_hint || 0;
    nameMap[p.key] = p.name;
  });
  const keys = Object.keys(usage || {});
  if (!keys.length) {
    box.innerHTML = '<div class="meta">还没有调用记录。用一次就会显示在这里和每张平台卡片上。</div>';
    return;
  }
  box.innerHTML =
    '<div class="usage-list">' +
    keys
      .map((k) => {
        const u = usage[k];
        const quota = quotaMap[k] || 0;
        const pct = quota ? Math.min(u.calls / quota, 1) * 100 : 0;
        const cls = pct >= 90 ? "danger" : pct >= 70 ? "warn" : "";
        return `<div class="usage-row">
          <div class="urow-head"><span class="urow-name">${nameMap[k] || k}</span>
          <span class="urow-num">${u.calls} 次 · 约 ${u.tokens} tokens</span></div>
          <div class="ubar"><div class="ubar-fill ${cls}" style="width:${pct.toFixed(0)}%"></div></div>
        </div>`;
      })
      .join("") +
    "</div>";
}

// ---------- 保存默认模型 ----------
$("#saveDefault").addEventListener("click", async () => {
  const v = $("#modelSelect").value; // provider/model
  const [dp, dm] = v.split("/", 1).length ? [v.split("/")[0], v.split("/").slice(1).join("/")] : ["", v];
  await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ default_provider: dp, default_model: dm }),
  });
  const msg = $("#defaultMsg");
  msg.textContent = "已保存默认模型：" + v;
  setTimeout(() => (msg.textContent = ""), 2500);
});

// ============================================================
//  多对话 + Agent 交互（按钮 loading 防抖、回车提交）
// ============================================================
let currentConvId = null;

async function loadConversations() {
  const r = await fetch("/api/conversations").then((x) => x.json());
  renderConvList(r.conversations || []);
  if (!currentConvId && (r.conversations || []).length) {
    selectConv(r.conversations[0].id);
  }
}

function renderConvList(convs) {
  const box = $("#convItems");
  box.innerHTML = "";
  if (!convs.length) {
    box.innerHTML = '<div class="conv-empty">还没有对话</div>';
    return;
  }
  convs.forEach((c) => {
    const item = document.createElement("div");
    item.className = "conv-item" + (c.id === currentConvId ? " active" : "");
    const title = document.createElement("span");
    title.className = "conv-title";
    title.textContent = c.title || "新对话";
    title.addEventListener("click", () => selectConv(c.id));
    const del = document.createElement("button");
    del.className = "conv-del";
    del.title = "删除对话";
    del.textContent = "×";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除这个对话？")) return;
      await fetch("/api/conversations/" + c.id, { method: "DELETE" });
      if (c.id === currentConvId) {
        currentConvId = null;
        $("#messages").innerHTML = '<div class="empty-hint">已删除，点「+ 新建对话」开始。</div>';
      }
      loadConversations();
    });
    item.append(title, del);
    box.appendChild(item);
  });
}

async function selectConv(id) {
  currentConvId = id;
  const list = await fetch("/api/conversations").then((x) => x.json());
  renderConvList(list.conversations || []);
  const r = await fetch("/api/conversations/" + id).then((x) => x.json());
  renderMessages(r.conversation ? r.conversation.messages : []);
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function renderMessages(messages) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!messages || !messages.length) {
    box.innerHTML = '<div class="empty-hint">这个对话还是空的，输入任务开聊吧。</div>';
    return;
  }
  messages.forEach((m) => box.appendChild(renderMessage(m)));
  box.scrollTop = box.scrollHeight;
}

function renderMessage(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (m.role === "user" ? "msg-user" : "msg-bot");
  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (m.role === "user") {
    bubble.textContent = m.content;
  } else if (m.steps && m.steps.length) {
    const sum = document.createElement("div");
    sum.className = "bot-summary";
    sum.textContent = m.content || "（无文本回答）";
    bubble.appendChild(sum);
    const toggle = document.createElement("button");
    toggle.className = "step-toggle";
    const stepsBox = document.createElement("div");
    stepsBox.className = "steps hidden";
    m.steps.forEach((s) => stepsBox.appendChild(renderStep(s)));
    toggle.textContent = "▸ 查看执行过程 (" + m.steps.length + " 步)";
    toggle.addEventListener("click", () => {
      const hidden = stepsBox.classList.toggle("hidden");
      toggle.textContent = (hidden ? "▸" : "▾") + " 查看执行过程 (" + m.steps.length + " 步)";
    });
    bubble.appendChild(toggle);
    bubble.appendChild(stepsBox);
  } else {
    bubble.textContent = m.content || "（无内容）";
  }

  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = fmtTime(m.ts);
  wrap.append(bubble, meta);
  return wrap;
}

function renderStep(s) {
  const div = document.createElement("div");
  div.className = "step";
  if (s.type === "llm") {
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = "模型思考（" + (s.provider || "") + "）";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = s.text || "";
    div.append(label, text);
  } else if (s.type === "tool") {
    const label = document.createElement("div");
    label.className = "label";
    const t = document.createElement("span");
    t.className = "tool";
    t.textContent = s.tool;
    label.textContent = "调用工具：";
    label.appendChild(t);
    const pre = document.createElement("pre");
    pre.textContent =
      "参数: " + JSON.stringify(s.args, null, 2) + "\n\n结果:\n" + (s.output || "");
    div.append(label, pre);
  } else if (s.type === "done") {
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = "最终回答";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = s.text || "";
    div.append(label, text);
  } else if (s.type === "error") {
    const label = document.createElement("div");
    label.className = "label";
    label.style.color = "var(--red)";
    label.textContent = "出错";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = s.text || "";
    div.append(label, text);
  }
  return div;
}

// ---------- 运行 Agent（loading 防抖 + 回车触发） ----------
function setRunning(running) {
  const btn = $("#runAgent");
  const input = $("#taskInput");
  btn.disabled = running;
  input.disabled = running;
  btn.textContent = running ? "运行中…" : "运行 Agent";
  btn.classList.toggle("loading", running);
}

$("#runAgent").addEventListener("click", () => runAgent());

$("#taskInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    runAgent();
  }
});

async function runAgent() {
  const input = $("#taskInput");
  const task = input.value.trim();
  if (!task) return;
  const status = $("#agentStatus");

  setRunning(true);
  status.textContent = "Agent 执行中…";

  // 先放一个临时「思考中」气泡，避免等待期间无反馈
  const box = $("#messages");
  const emptyHint = box.querySelector(".empty-hint");
  if (emptyHint) box.innerHTML = "";
  const tmp = document.createElement("div");
  tmp.className = "msg msg-bot";
  tmp.innerHTML = '<div class="bubble loading-bubble">⏳ 正在规划与执行，请稍候…</div>';
  box.appendChild(tmp);
  box.scrollTop = box.scrollHeight;

  try {
    const r = await fetch("/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, conversation_id: currentConvId || "" }),
    }).then((x) => x.json());

    if (r.error) {
      status.textContent = "失败：" + r.error;
      tmp.querySelector(".bubble").textContent = "⚠️ " + r.error;
      return;
    }
    currentConvId = r.conversation_id;
    input.value = "";
    renderMessages(r.messages);
    status.textContent = "完成";
    loadConversations(); // 刷新左侧标题/列表
  } catch (err) {
    status.textContent = "请求出错：" + err.message;
    if (currentConvId) {
      const r2 = await fetch("/api/conversations/" + currentConvId).then((x) => x.json());
      renderMessages(r2.conversation ? r2.conversation.messages : []);
    } else {
      tmp.querySelector(".bubble").textContent = "⚠️ 网络或服务器错误：" + err.message;
    }
  } finally {
    setRunning(false);
  }
}

// ---------- 新建对话 ----------
$("#newConv").addEventListener("click", async () => {
  const r = await fetch("/api/conversations", { method: "POST" })
    .then((x) => x.json());
  currentConvId = r.conversation.id;
  await loadConversations();
  $("#messages").innerHTML = '<div class="empty-hint">新对话已建好，输入任务开聊吧。</div>';
  $("#taskInput").focus();
});

refresh();
loadConversations();
