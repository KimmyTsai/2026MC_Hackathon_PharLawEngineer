// The map. Vector-first on purpose: OSM tiles need the internet, the graph does
// not, so the route stays readable when the tiles fail to load.

const NODE_STYLE = {
  elevator: { color: "#1d4ed8", radius: 7, label: "電梯" },
  entrance: { color: "#0f8a4c", radius: 6, label: "入口" },
  ramp: { color: "#7a5af8", radius: 6, label: "無障礙斜坡" },
  junction: { color: "#9aa1ac", radius: 4, label: "路口" },
  dorm: { color: "#b8791a", radius: 7, label: "宿舍" },
  stop: { color: "#0e7490", radius: 7, label: "公車站" },
  bike_station: { color: "#c2410c", radius: 7, label: "YouBike 站" },
  offcampus: { color: "#64748b", radius: 7, label: "校外起點" },
};

const BLOCKED_COLOR = "#c0392b";
const UNCONFIRMED_COLOR = "#e08a1e";
const ROUTE_COLOR = "#1d4ed8";

function edgeStyle(edge, inRoute) {
  if (inRoute) return null; // the route layer draws these itself
  if (edge.mode === "bus") return { color: "#0e7490", weight: 3, dashArray: "1 7", opacity: 0.7 };
  if (edge.mode === "youbike") {
    return { color: "#c2410c", weight: 3, dashArray: "1 7", opacity: 0.7 };
  }
  if (edge.stairs) return { color: BLOCKED_COLOR, weight: 2.5, dashArray: "4 4", opacity: 0.75 };
  if (edge.slope === "steep") return { color: "#b8791a", weight: 2.5, dashArray: "8 4", opacity: 0.8 };
  if (edge.covered) return { color: "#5d636e", weight: 3.5, opacity: 0.85 };
  return { color: "#b6bcc6", weight: 2.5, opacity: 0.9 };
}

function edgeTitle(edge) {
  const bits = [`${edge.id}・${Math.round(edge.length_m)} m`];
  if (edge.mode !== "walk") bits.push(edge.mode);
  if (edge.stairs) bits.push("階梯");
  if (edge.slope !== "flat") bits.push(edge.slope === "steep" ? "陡坡" : "緩坡");
  if (edge.covered) bits.push("有頂蓋");
  bits.push(`來源 ${edge.source}・${edge.updated_at}`);
  return bits.join("・");
}

class CampusMap {
  constructor(elementId) {
    this.map = L.map(elementId, { zoomControl: true, attributionControl: true });
    this.baseLayer = L.layerGroup().addTo(this.map);
    this.routeLayer = L.layerGroup().addTo(this.map);
    this.markerLayer = L.layerGroup().addTo(this.map);
    this.nodes = new Map();
    this.lastRouteKey = null;
    this.tilesFailed = false;
    // The accessibility story lives in the last walking leg. Fitting the whole
    // 2.2 km commute shrinks it to a few pixels, so campus is the default view.
    this.focus = "campus";
    this.lastBounds = { campus: null, journey: null };
    // fitBounds animates; running it on every refresh queues animations and
    // stalls the page. Only re-fit when the view actually needs to move.
    this.lastFitKey = null;

    this.map.setView([22.9969, 120.2201], 16);
    this.tileLayer = null;
    this.provider = null;
    this.tileErrors = 0;
  }

  /** Install the basemap. `config` comes from /map/config.
   *
   * Google tiles arrive through our own server so the API key stays there. If
   * they stop working mid-demo we switch to OSM rather than showing a blank
   * map — the vectors alone are still readable, but a basemap helps.
   */
  useBasemap(config, onFallback) {
    if (this.tileLayer) this.map.removeLayer(this.tileLayer);
    this.provider = config.provider;
    this.tileErrors = 0;
    this.tilesFailed = false;

    this.tileLayer = L.tileLayer(config.tile_url, {
      maxZoom: config.max_zoom ?? 19,
      attribution: config.attribution ?? "",
    });
    this.tileLayer.on("tileerror", () => {
      this.tileErrors += 1;
      this.tilesFailed = true;
      // A handful of misses is normal at the edges; a wall of them is not.
      if (config.fallback && this.tileErrors === 5) {
        this.useBasemap(config.fallback, onFallback);
        if (onFallback) onFallback(config.fallback);
      }
    });
    this.tileLayer.addTo(this.map);
  }

  /** Draw the graph. Called whenever facility status changes. */
  drawGraph(graph) {
    this.nodes = new Map(graph.nodes.map((n) => [n.id, n]));
    const blocked = new Set(graph.blocked_ids ?? []);
    const unconfirmed = new Set(graph.unconfirmed_ids ?? []);

    this.baseLayer.clearLayers();
    this.markerLayer.clearLayers();

    for (const edge of graph.edges) {
      const from = this.nodes.get(edge.from);
      const to = this.nodes.get(edge.to);
      if (!from || !to) continue;
      const style = edgeStyle(edge, false);
      const line = L.polyline(
        [
          [from.lat, from.lng],
          [to.lat, to.lng],
        ],
        blocked.has(edge.id) ? { ...style, color: BLOCKED_COLOR, weight: 4, dashArray: "2 6" } : style,
      );
      line.bindTooltip(
        blocked.has(edge.id) ? `${edgeTitle(edge)}・<b>停用</b>` : edgeTitle(edge),
      );
      line.addTo(this.baseLayer);
    }

    for (const node of graph.nodes) {
      const style = NODE_STYLE[node.type] ?? NODE_STYLE.junction;
      const isBlocked = blocked.has(node.id);
      const isUnconfirmed = unconfirmed.has(node.id);
      const marker = L.circleMarker([node.lat, node.lng], {
        radius: isBlocked || isUnconfirmed ? style.radius + 2 : style.radius,
        color: isBlocked ? BLOCKED_COLOR : isUnconfirmed ? UNCONFIRMED_COLOR : "#ffffff",
        weight: isBlocked || isUnconfirmed ? 3 : 1.5,
        fillColor: style.color,
        fillOpacity: node.step_free ? 0.95 : 0.35,
        className: isBlocked ? "marker-blocked" : isUnconfirmed ? "marker-unconfirmed" : "",
      });
      const state = isBlocked ? "・<b>停用</b>" : isUnconfirmed ? "・<b>回報待確認</b>" : "";
      marker.bindTooltip(
        `<b>${node.name}</b><br>${style.label}${node.floors ? `（${node.floors.join("/")}F）` : ""}` +
          `${node.step_free ? "" : "・非無障礙"}${state}<br>` +
          `<span class="tip-meta">${node.id}・來源 ${node.source}・${node.updated_at}</span>`,
      );
      marker.addTo(this.markerLayer);
    }

    if (!this.fitted) {
      const campus = graph.nodes.filter((n) => n.building || n.type === "ramp" || n.type === "dorm");
      const bounds = L.latLngBounds((campus.length ? campus : graph.nodes).map((n) => [n.lat, n.lng]));
      if (bounds.isValid()) this.map.fitBounds(bounds, { padding: [30, 30] });
      this.fitted = true;
    }
  }

  /** Draw the selected plan's route. Flashes when the route actually changed. */
  drawRoute(plan) {
    this.routeLayer.clearLayers();
    if (!plan) {
      this.lastRouteKey = null;
      return { changed: false };
    }

    const legs = plan.selected.legs ?? [];
    const key = legs.map((leg) => leg.nodes.join(">")).join("|");
    const changed = this.lastRouteKey !== null && this.lastRouteKey !== key;
    this.lastRouteKey = key;

    const allPoints = [];
    for (const leg of legs) {
      const points = leg.nodes
        .map((id) => this.nodes.get(id))
        .filter(Boolean)
        .map((n) => [n.lat, n.lng]);
      if (points.length < 2) continue;
      allPoints.push(...points);

      const transit = leg.mode !== "walk";
      L.polyline(points, {
        color: ROUTE_COLOR,
        weight: transit ? 7 : 6,
        opacity: 0.35,
        className: changed ? "route-halo route-changed" : "route-halo",
      }).addTo(this.routeLayer);
      L.polyline(points, {
        color: ROUTE_COLOR,
        weight: transit ? 4 : 3.5,
        opacity: 0.95,
        dashArray: transit ? "10 6" : null,
        className: "route-line",
      })
        .bindTooltip(
          `${leg.mode}・${Math.round(leg.seconds / 60)} 分` +
            (leg.notes?.length ? `<br>${leg.notes.join("<br>")}` : ""),
        )
        .addTo(this.routeLayer);

      for (const facility of leg.uses_facilities ?? []) {
        const node = this.nodes.get(facility);
        if (!node) continue;
        L.circleMarker([node.lat, node.lng], {
          radius: 11,
          color: ROUTE_COLOR,
          weight: 2.5,
          fill: false,
          className: "route-facility",
        }).addTo(this.routeLayer);
      }
    }

    const campusLeg = [...legs].reverse().find((leg) => leg.mode === "walk" && leg.nodes.length > 1);
    const campusPoints = (campusLeg?.nodes ?? [])
      .map((id) => this.nodes.get(id))
      .filter(Boolean)
      .map((n) => [n.lat, n.lng]);

    this.lastBounds.journey = allPoints.length > 1 ? L.latLngBounds(allPoints) : null;
    this.lastBounds.campus = campusPoints.length > 1 ? L.latLngBounds(campusPoints) : null;
    this.applyFocus();
    return { changed };
  }

  applyFocus(force = false) {
    const bounds = this.lastBounds[this.focus] ?? this.lastBounds.journey;
    if (!bounds || !bounds.isValid()) return;
    const key = `${this.focus}:${bounds.toBBoxString()}`;
    if (!force && key === this.lastFitKey) return;
    this.lastFitKey = key;
    this.map.fitBounds(bounds, {
      padding: this.focus === "campus" ? [40, 40] : [30, 30],
      animate: false,
    });
  }

  setFocus(focus) {
    this.focus = focus;
    this.applyFocus(true);
  }

  note() {
    if (!this.tilesFailed) return "";
    return this.provider === "osm"
      ? "圖磚離線，只顯示圖資向量"
      : "Google 圖磚載入異常";
  }
}

function buildLegend(host) {
  const rows = [
    ["電梯", NODE_STYLE.elevator.color],
    ["入口", NODE_STYLE.entrance.color],
    ["斜坡", NODE_STYLE.ramp.color],
    ["公車站", NODE_STYLE.stop.color],
    ["YouBike", NODE_STYLE.bike_station.color],
    ["停用設施", BLOCKED_COLOR],
    ["待確認回報", UNCONFIRMED_COLOR],
    ["目前路線", ROUTE_COLOR],
  ];
  host.replaceChildren();
  for (const [label, color] of rows) {
    const li = document.createElement("li");
    li.innerHTML = `<i style="background:${color}"></i>${label}`;
    host.appendChild(li);
  }
}
