// M0 shell: provider-mode badges, sim clock, commitments, signals, agent log.
// The map and the decision timeline arrive in M3.

const $ = (id) => document.getElementById(id);

const FRESHNESS_LABEL = {
  live: "即時",
  fixture: "回放",
  stale: "過期",
  unavailable: "無資料",
};

function badge(text, kind) {
  const el = document.createElement("span");
  el.className = `badge badge--${kind}`;
  el.textContent = text;
  return el;
}

function renderBadges(snapshot) {
  const host = $("badges");
  host.replaceChildren();
  host.appendChild(badge(snapshot.mode === "live" ? "LIVE 模式" : "回放模式", "mode"));
  for (const [name, mode] of Object.entries(snapshot.provider_modes ?? {})) {
    host.appendChild(badge(`${name}: ${FRESHNESS_LABEL[mode] ?? mode}`, mode));
  }
}

function renderClock(snapshot) {
  const now = new Date(snapshot.now);
  $("clock").textContent = now.toLocaleTimeString("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function renderCommitments(snapshot) {
  const host = $("commitments");
  host.replaceChildren();
  for (const c of snapshot.commitments ?? []) {
    const li = document.createElement("li");
    const start = new Date(c.start).toLocaleTimeString("zh-TW", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    li.innerHTML =
      `<strong>${start} ${c.title}</strong>` +
      `<div class="meta">${c.room}・重要性 ${c.importance}` +
      `${c.user_confirmed ? "" : "・未經使用者確認"}</div>`;
    host.appendChild(li);
  }
  if (!host.children.length) {
    host.innerHTML = '<li class="meta">今日沒有行程</li>';
  }
}

function renderSignals(snapshot) {
  const host = $("signals");
  host.replaceChildren();
  for (const [kind, signal] of Object.entries(snapshot.signals ?? {})) {
    const li = document.createElement("li");
    const observed = new Date(signal.observed_at).toLocaleTimeString("zh-TW", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    const value = signal.error ? signal.error : JSON.stringify(signal.value);
    li.innerHTML =
      `<strong>${kind}</strong> <span class="meta">${FRESHNESS_LABEL[signal.freshness] ?? signal.freshness}` +
      `・觀測於 ${observed}</span><div class="meta">${value}</div>`;
    host.appendChild(li);
  }
}

function renderGraphSummary(graph) {
  const dl = $("graph-summary");
  dl.replaceChildren();
  const rows = [
    ["節點", graph.nodes.length],
    ["路段", graph.edges.length],
    ["教室", graph.rooms.length],
    ["停用設施", graph.blocked_ids.length],
    ["待確認回報", graph.unconfirmed_ids.length],
    ["圖資狀態", graph.draft ? "草稿（座標未校正）" : "已校正"],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.append(dt, dd);
  }
}

function appendLog(entry) {
  const host = $("agent-log");
  const li = document.createElement("li");
  const at = new Date(entry.at).toLocaleTimeString("zh-TW", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  li.innerHTML =
    `<strong>${entry.phase}</strong> ${entry.summary}` +
    `<div class="meta">${at}${entry.tool ? `・工具 ${entry.tool}` : ""}</div>`;
  host.appendChild(li);
  host.scrollTop = host.scrollHeight;
}

function renderLog(snapshot) {
  $("agent-log").replaceChildren();
  for (const entry of snapshot.agent_log ?? []) appendLog(entry);
}

async function refresh() {
  const [snapshot, graph] = await Promise.all([
    fetch("/state").then((r) => r.json()),
    fetch("/graph").then((r) => r.json()),
  ]);
  renderBadges(snapshot);
  renderClock(snapshot);
  renderCommitments(snapshot);
  renderSignals(snapshot);
  renderLog(snapshot);
  renderGraphSummary(graph);
}

function connectStream() {
  const source = new EventSource("/events");
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "agent_log") appendLog(event.data);
    if (event.type === "clock" || event.type === "plan" || event.type === "trigger") refresh();
  };
  source.onerror = () => {
    // EventSource reconnects on its own; surface the gap rather than hiding it.
    $("mode-badge")?.replaceWith(badge("連線中斷，重試中", "unavailable"));
  };
}

$("btn-reset").addEventListener("click", async () => {
  await fetch("/replay/reset", { method: "POST" });
  await refresh();
});

$("btn-advance").addEventListener("click", async () => {
  await fetch("/replay/advance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds: 300 }),
  });
  await refresh();
});

refresh().then(connectStream);
