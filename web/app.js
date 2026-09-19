// Wires /state, /graph and the SSE stream onto the page. All numbers come from
// the API; nothing is computed here.

const $ = (id) => document.getElementById(id);

const FRESHNESS_LABEL = { live: "即時", fixture: "回放", stale: "過期", unavailable: "無資料" };
const MODE_LABEL = {
  walk: "步行",
  youbike: "YouBike",
  bus: "公車",
  bus_lowfloor: "低地板公車",
  paratransit: "復康巴士",
};
const PHASE_LABEL = { perceive: "感知", plan: "規劃", act: "行動", reflect: "檢視" };
const IMPORTANCE_LABEL = {
  routine: "一般",
  graded: "計分",
  lab: "實驗",
  presentation: "報告",
  exam: "考試",
  meeting: "會議",
};
const EVENT_LABEL = {
  day_start: "一天開始",
  notice_received: "收到公告",
  weather_update: "天氣更新",
  bike_status_update: "車輛數更新",
  flood_warning: "積水警示",
  bus_eta_update: "公車到站更新",
  user_not_departed: "仍未出門",
  report_submitted: "使用者回報",
};
const SIGNAL_LABEL = {
  rain: "降雨",
  flood: "積水",
  air_quality: "空品",
  bike_availability: "YouBike",
  bus_eta: "公車到站",
  departure_state: "出門狀態",
};

const hhmm = (iso) =>
  new Date(iso).toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", hour12: false });

let campusMap = null;
let nodeNames = new Map();

const nodeName = (id) => nodeNames.get(id) ?? id;

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

  const agent = snapshot.agent ?? {};
  if (!agent.available) {
    host.appendChild(badge(`Agent: 確定性規劃（${agent.reason ?? "模型不可用"}）`, "unavailable"));
  } else if (agent.cassette_stale) {
    host.appendChild(badge(`Agent: 錄音檔已過期，需重錄`, "stale"));
  } else if (agent.replaying) {
    host.appendChild(badge(`Agent: ${agent.model}（錄音重播）`, "fixture"));
  } else {
    host.appendChild(badge(`Agent: ${agent.model}（即時呼叫）`, "live"));
  }

  for (const [name, mode] of Object.entries(snapshot.provider_modes ?? {})) {
    host.appendChild(badge(`${SIGNAL_LABEL[name] ?? name}: ${FRESHNESS_LABEL[mode] ?? mode}`, mode));
  }
}

function renderClock(snapshot) {
  $("clock").textContent = hhmm(snapshot.now);
}

function renderEvents(snapshot) {
  const host = $("events");
  host.replaceChildren();
  const now = new Date(snapshot.now);
  for (const event of snapshot.scenario_events ?? []) {
    const li = document.createElement("li");
    const when = new Date(event.at);
    li.className = event.processed
      ? "event event--done"
      : when <= now
        ? "event event--now"
        : "event";
    li.innerHTML =
      `<span class="event-time">${hhmm(event.at)}</span>` +
      `<span class="event-body"><strong>${EVENT_LABEL[event.type] ?? event.type}</strong>` +
      `${event.expect ? `<span class="meta">${event.expect}</span>` : ""}</span>`;
    host.appendChild(li);
  }
}

function renderCommitments(snapshot) {
  const host = $("commitments");
  host.replaceChildren();
  for (const c of snapshot.commitments ?? []) {
    const li = document.createElement("li");
    li.innerHTML =
      `<strong>${hhmm(c.start)} ${c.title}</strong>` +
      `<div class="meta">${c.room}・${IMPORTANCE_LABEL[c.importance] ?? c.importance}` +
      `${c.user_confirmed ? "" : "・未經使用者確認"}</div>`;
    host.appendChild(li);
  }
  if (!host.children.length) host.innerHTML = '<li class="meta">今日沒有行程</li>';
}

function describeSignal(kind, signal) {
  const v = signal.value ?? {};
  if (signal.error) return signal.error;
  switch (kind) {
    case "rain":
      return v.mm_per_hr
        ? `${v.mm_per_hr} mm/hr${v.rain_from ? `，${v.rain_from} 起` : ""}`
        : "無降雨";
    case "flood":
      return v.level === "none" ? "無積水" : `${v.level}${v.reason ? `・${v.reason}` : ""}`;
    case "bike_availability":
      return `可借 ${v.bikes ?? "?"} 台`;
    case "bus_eta":
      return `${v.route ?? "公車"} ${v.eta_minutes ?? "?"} 分後到${v.low_floor ? "・低地板" : ""}`;
    case "air_quality":
      return `AQI ${v.aqi ?? "?"}`;
    case "departure_state":
      return v.departed ? "已出門" : "尚未出門";
    default:
      return JSON.stringify(v);
  }
}

function renderSignals(snapshot) {
  const host = $("signals");
  host.replaceChildren();
  for (const [kind, signal] of Object.entries(snapshot.signals ?? {})) {
    const li = document.createElement("li");
    li.innerHTML =
      `<strong>${SIGNAL_LABEL[kind] ?? kind}</strong> ${describeSignal(kind, signal)}` +
      `<div class="meta">${FRESHNESS_LABEL[signal.freshness] ?? signal.freshness}` +
      `・觀測於 ${hhmm(signal.observed_at)}</div>`;
    host.appendChild(li);
  }
}

function renderPlan(snapshot) {
  const host = $("plan");
  const commitment = snapshot.next_commitment;
  const plan = commitment ? snapshot.plans?.[commitment.id] : null;
  const decision = snapshot.latest_decision;

  host.replaceChildren();
  if (!plan || plan.status === "infeasible") {
    const p = document.createElement("p");
    p.className = "status--infeasible";
    p.textContent = decision?.rationale ?? "尚未產生計畫";
    host.appendChild(p);
  } else {
    const sel = plan.selected;
    const head = document.createElement("div");
    head.className = "plan-head";
    head.innerHTML =
      `<span class="plan-mode">${MODE_LABEL[sel.mode] ?? sel.mode}</span>` +
      `<span class="plan-times"><strong>${hhmm(sel.depart_at)}</strong> 出發` +
      ` → <strong>${hhmm(sel.conservative_eta)}</strong> 抵達</span>` +
      `<span class="meta">需在 ${hhmm(commitment.start)} 的` +
      `${IMPORTANCE_LABEL[commitment.importance] ?? commitment.importance}前到</span>`;
    host.appendChild(head);

    const legs = document.createElement("ul");
    legs.className = "legs";
    for (const leg of sel.legs) {
      const li = document.createElement("li");
      li.innerHTML =
        `<span class="dur">${MODE_LABEL[leg.mode] ?? leg.mode} ` +
        `${Math.round(leg.seconds / 60)}分</span>` +
        `<span class="meta">${[`${nodeName(leg.from_node)} → ${nodeName(leg.to_node)}`, ...(leg.notes ?? [])].join("・")}</span>`;
      legs.appendChild(li);
    }
    host.appendChild(legs);

    if (sel.risk_flags?.length) {
      const flags = document.createElement("ul");
      flags.className = "flags";
      for (const flag of sel.risk_flags) {
        const li = document.createElement("li");
        li.textContent = `⚠ ${flag}`;
        flags.appendChild(li);
      }
      host.appendChild(flags);
    }
    if (decision?.next_check_at) {
      const next = document.createElement("p");
      next.className = "meta";
      next.textContent = `下次檢查 ${hhmm(decision.next_check_at)}`;
      host.appendChild(next);
    }
  }

  if (decision?.rationale) {
    const why = document.createElement("p");
    why.className = "rationale";
    why.textContent = decision.rationale;
    host.appendChild(why);
  }

  const rejected = $("rejected");
  rejected.replaceChildren();
  for (const item of decision?.rejected ?? []) {
    const li = document.createElement("li");
    li.innerHTML =
      `<strong>${MODE_LABEL[item.mode] ?? item.mode}</strong>` +
      `<div class="meta">${item.reason}</div>`;
    rejected.appendChild(li);
  }
  if (!rejected.children.length) rejected.innerHTML = '<li class="meta">—</li>';

  const assumptions = $("assumptions");
  assumptions.replaceChildren();
  for (const item of decision?.assumptions ?? []) {
    const li = document.createElement("li");
    li.className = "meta";
    li.textContent = item;
    assumptions.appendChild(li);
  }
}

function appendLog(entry) {
  const host = $("agent-log");
  const li = document.createElement("li");
  li.className = `log-${entry.phase}`;
  li.innerHTML =
    `<span class="phase">${PHASE_LABEL[entry.phase] ?? entry.phase}</span> ${entry.summary}` +
    `<div class="meta">${hhmm(entry.at)}` +
    `${entry.tool ? `・工具 <code>${entry.tool}</code>` : ""}` +
    `・${entry.model_id ?? "程式"}</div>` +
    (entry.tool_result_digest
      ? `<div class="digest">${entry.tool_result_digest.replace(/</g, "&lt;")}</div>`
      : "");
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
  // Names first: renderPlan reads them to label the legs.
  nodeNames = new Map(graph.nodes.map((n) => [n.id, n.name]));

  renderBadges(snapshot);
  renderClock(snapshot);
  renderEvents(snapshot);
  renderCommitments(snapshot);
  renderSignals(snapshot);
  renderPlan(snapshot);
  renderLog(snapshot);

  campusMap.drawGraph(graph);
  const commitment = snapshot.next_commitment;
  const plan = commitment ? snapshot.plans?.[commitment.id] : null;
  const { changed } = campusMap.drawRoute(plan && plan.status !== "infeasible" ? plan : null);

  const notes = [];
  if (graph.draft) notes.push("圖資為草稿，座標未校正");
  if (changed) notes.push("路線已改變");
  const tiles = campusMap.note();
  if (tiles) notes.push(tiles);
  $("map-note").textContent = notes.join("・");
}

// One /replay/next publishes a dozen SSE frames. Refreshing on each one
// redraws the whole map a dozen times and jams the main thread, so they are
// coalesced into a single refresh.
let refreshTimer = null;
let refreshing = false;

function scheduleRefresh(delay = 120) {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    if (refreshing) {
      scheduleRefresh(delay);
      return;
    }
    refreshing = true;
    try {
      await refresh();
    } finally {
      refreshing = false;
    }
  }, delay);
}

function connectStream() {
  const source = new EventSource("/events");
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "agent_log") appendLog(event.data);
    if (["clock", "plan", "trigger", "decision"].includes(event.type)) scheduleRefresh();
  };
  source.onerror = () => {
    $("mode-badge")?.replaceWith(badge("連線中斷，重試中", "unavailable"));
  };
}

async function post(path, body) {
  const buttons = [...document.querySelectorAll(".controls button")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    await fetch(path, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
  scheduleRefresh(0);
}

function setFocus(focus) {
  campusMap.setFocus(focus);
  $("btn-focus-campus").classList.toggle("chip--on", focus === "campus");
  $("btn-focus-journey").classList.toggle("chip--on", focus === "journey");
}

function wireControls() {
  $("btn-focus-campus").addEventListener("click", () => setFocus("campus"));
  $("btn-focus-journey").addEventListener("click", () => setFocus("journey"));
  $("btn-next").addEventListener("click", () => post("/replay/next"));
  $("btn-advance").addEventListener("click", () => post("/replay/advance", { seconds: 300 }));
  // /replay/start, not /replay/reset: start also runs the agent at the
  // scenario's opening time, which is what the cassette was recorded from.
  // Resetting without that first run changes every later question, so every
  // recorded turn would miss and the demo would degrade.
  $("btn-reset").addEventListener("click", () => post("/replay/start", {}));
  $("btn-report").addEventListener("click", () =>
    post("/replay/inject", {
      type: "report_submitted",
      payload: { node_hint: "RAMP_07", confidence_hint: "low", source: "現場回報" },
    }),
  );
}

campusMap = new CampusMap("map");
buildLegend($("legend"));
wireControls();
refresh().then(connectStream);
