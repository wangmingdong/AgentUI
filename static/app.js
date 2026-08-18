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
async function refresh() {
  const [provRes, cfgRes] = await Promise.all([
    fetch("/api/providers").then((r) => r.json()),
    fetch("/api/config").then((r) => r.json()),
  ]);
  renderModelSelect(provRes.providers, cfgRes);
  renderProviders(provRes.providers);
  renderUsage(cfgRes.usage);
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
    card.innerHTML = `
      <h3>${p.name} ${tags}</h3>
      <div class="meta">${p.base_url}<br>限速：${p.rate_limit}</div>
      <div class="models">免费模型：${(p.free_models || []).join("、") || "—"}</div>
      <div class="meta">${p.note}</div>
      <input type="password" class="tok" placeholder="API Key（${p.has_token ? "已配置" : "未配置"}）" />
      <div class="actions">
        <button class="btn" data-act="save">保存</button>
        <button class="btn" data-act="test">测连通</button>
        <span class="tstatus"></span>
      </div>`;
    const tokInput = card.querySelector(".tok");
    card.querySelector('[data-act="save"]').addEventListener("click", async () => {
      await fetch("/api/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_key: p.key, api_key: tokInput.value.trim() }),
      });
      tokInput.placeholder = tokInput.value.trim() ? "已配置" : "未配置";
    });
    card.querySelector('[data-act="test"]').addEventListener("click", async (e) => {
      const st = card.querySelector(".tstatus");
      st.textContent = "测试中...";
      st.className = "tstatus";
      const r = await fetch("/api/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_key: p.key }),
      }).then((x) => x.json());
      st.textContent = r.message;
      st.className = "tstatus " + (r.ok ? "status-ok" : "status-fail");
    });
    box.appendChild(card);
  });
}

function renderUsage(usage) {
  const box = $("#usage");
  const keys = Object.keys(usage || {});
  if (!keys.length) {
    box.innerHTML = '<div class="meta">还没有调用记录。</div>';
    return;
  }
  box.innerHTML = keys
    .map((k) => `<div>${k}：调用 ${usage[k].calls} 次，约 ${usage[k].tokens} tokens</div>`)
    .join("");
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

// ---------- 运行 Agent ----------
$("#runAgent").addEventListener("click", async () => {
  const task = $("#taskInput").value.trim();
  if (!task) return;
  const stepsBox = $("#steps");
  const status = $("#agentStatus");
  stepsBox.innerHTML = "";
  status.textContent = "Agent 执行中...";
  $("#runAgent").disabled = true;

  const r = await fetch("/api/agent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task }),
  }).then((x) => x.json());

  (r.steps || []).forEach((s) => stepsBox.appendChild(renderStep(s)));
  status.textContent = "完成";
  $("#runAgent").disabled = false;
  stepsBox.scrollTop = stepsBox.scrollHeight;
});

function renderStep(s) {
  const div = document.createElement("div");
  div.className = "step";
  if (s.type === "llm") {
    div.innerHTML = `<div class="label">模型思考（${s.provider || ""}）</div><div class="text"></div>`;
    div.querySelector(".text").textContent = s.text || "";
  } else if (s.type === "tool") {
    div.innerHTML = `<div class="label">调用工具：<span class="tool">${s.tool}</span></div>
      <pre></pre>`;
    div.querySelector("pre").textContent =
      "参数: " + JSON.stringify(s.args, null, 2) + "\n\n结果:\n" + (s.output || "");
  } else if (s.type === "done") {
    div.innerHTML = `<div class="label">最终回答</div><div class="text"></div>`;
    div.querySelector(".text").textContent = s.text || "";
  } else if (s.type === "error") {
    div.innerHTML = `<div class="label" style="color:var(--red)">出错</div><div class="text"></div>`;
    div.querySelector(".text").textContent = s.text || "";
  }
  return div;
}

refresh();
