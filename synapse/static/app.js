const $ = (id) => document.getElementById(id);
const colors = [
  "#d5d5d5",
  "#9aafbd",
  "#bdac99",
  "#b4a8be",
  "#c3968f",
  "#879fb8",
  "#bcb5a1",
];
const kindColors = {
  presence: "#b9c7d2",
  ban: "#d29991",
  note: "#b9adcd",
  warning: "#c9b68d",
  kick: "#96b2c4",
  blacklist: "#c8a1b7",
  damage: "#d19b80",
  death: "#c17f7f",
  radio: "#a6b899",
  pm: "#b7a3cc",
  communication: "#91b7c6",
  other: "#ababab",
};
const ns = "http://www.w3.org/2000/svg";
const logPlayers = new Map();
let logsOpened = false,
  logCursor = null,
  logRequest = 0,
  playerSearchRequest = 0,
  playerSearchTimer;
let lastLogQuery = null;
let data,
  selected,
  requestNumber = 0,
  detailNumber = 0,
  frame = 0;
let transform = { x: 0, y: 0, k: 1 },
  world,
  activeRange;
const date = (n) => (n ? new Date(n).toLocaleString() : "Never");
const hours = (n) => `${(n / 60).toFixed(1)}h`;
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function svgEl(tag, attributes = {}) {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attributes))
    node.setAttribute(key, value);
  return node;
}
function localInput(time) {
  const d = new Date(time);
  return new Date(time - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
function preset(days) {
  const end = Date.now();
  $("start").value = localInput(end - days * 86400000);
  $("end").value = localInput(end);
  document
    .querySelectorAll("[data-days]")
    .forEach((b) => b.classList.toggle("active", +b.dataset.days === days));
}
function params() {
  return new URLSearchParams({
    start: new Date($("start").value).getTime(),
    end: new Date($("end").value).getTime(),
    kinds: [...document.querySelectorAll("[name=kind]:checked")]
      .map((x) => x.value)
      .join(","),
    minimum: $("minimum").value,
    ...(selected ? { focus: selected } : {}),
  });
}
async function read(url) {
  const r = await fetch(url);
  if (r.status === 401) window.location.assign("/");
  const result = await r.json();
  if (!r.ok) throw new Error(result.error || "The request failed");
  return result;
}
async function load() {
  const number = ++requestNumber;
  $("loading").hidden = false;
  $("refresh").disabled = true;
  $("notice").classList.remove("error");
  try {
    const query = params();
    const result = await read("/api/graph?" + query);
    if (number !== requestNumber) return;
    data = result;
    activeRange = { start: result.start, end: result.end };
    $("log-range").textContent = `${date(result.start)} – ${date(result.end)}`;
    $("players-stat").textContent = data.nodes.length;
    $("edges-stat").textContent = data.edges.length.toLocaleString();
    $("clusters-stat").textContent = data.stats.communities;
    $("coverage-stat").textContent =
      `${(data.stats.coverage * 100).toFixed(1)}%`;
    const last = data.stats.latest_poll,
      age = Date.now() - (last.observed_at || 0);
    $("live-status").textContent = data.demo
      ? "Synthetic observations for exploring the viewer"
      : !last.observed_at
        ? "No samples collected yet"
        : `${age > data.stats.interval_seconds * 3000 ? "Collector stale" : "Latest poll"} · ${date(last.observed_at)}${last.ok ? "" : " · failed"}`;
    const warnings = [];
    if (data.demo)
      warnings.push("DEMO DATA — these players and histories are fictional.");
    if (data.stats.coverage < 0.8)
      warnings.push("Low coverage: gaps are excluded, not treated as absence.");
    if (data.stats.players > data.nodes.length)
      warnings.push(
        `Showing ${data.nodes.length} of ${data.stats.players} players, ranked by observed time. Narrow the time range to explore others.`,
      );
    if (data.stats.hidden_edges)
      warnings.push(
        `${data.stats.hidden_edges} weaker edges hidden by the 5,000-edge display limit.`,
      );
    if (data.jobs.some((j) => j.name === "kicks" && j.error))
      warnings.push(
        "New kick history is unavailable with this read-only key. Previously collected kicks remain visible.",
      );
    if (data.jobs.some((j) => j.name !== "kicks" && j.error))
      warnings.push(
        "Some history jobs are failing. See collection status below.",
      );
    if (data.log_status.truncated)
      warnings.push(
        `${data.log_status.truncated} log windows are capped and incomplete.`,
      );
    $("notice").textContent = warnings.join(" ");
    $("health-detail").replaceChildren(
      el(
        "div",
        `${data.stats.successful_polls} successful polls · ${data.stats.failed_polls} recorded failures · ${data.stats.covered_hours.toFixed(1)} hours covered in this range`,
      ),
    );
    for (const job of data.jobs)
      $("health-detail").append(
        el(
          "div",
          `${job.name} · ${job.name === "logs" ? "last successful request" : "last completed"} ${date(job.last_success)}${job.offset ? " · page offset " + job.offset : ""}${job.error ? " · " + job.error : ""}`,
        ),
      );
    $("health-detail").append(el("div", logCoverage(data.log_status)));
    if (logsOpened) loadLogs();
    if (!data.nodes.some((n) => n.id === selected)) selected = null;
    renderGraph();
    directory();
    if (selected) showPlayer(selected);
    else clearSelection();
  } catch (error) {
    if (number === requestNumber) {
      $("notice").textContent =
        error.message + " Previous results, if any, remain visible.";
      $("notice").classList.add("error");
    }
  } finally {
    if (number === requestNumber) {
      $("loading").hidden = true;
      $("refresh").disabled = false;
    }
  }
}
function directory() {
  const query = $("search").value.toLowerCase();
  $("directory").replaceChildren();
  const nodes = [...data.nodes]
    .sort((a, b) => b.minutes - a.minutes || a.name.localeCompare(b.name))
    .filter((n) =>
      `${n.name} ${n.steam_id || ""}`.toLowerCase().includes(query),
    );
  for (const node of nodes) {
    const button = el("button", undefined, "person");
    button.type = "button";
    button.append(el("span", node.name.slice(0, 2).toUpperCase(), "avatar"));
    const label = el("span");
    label.append(
      el("span", node.name, "name"),
      el(
        "small",
        `${hours(node.minutes)} observed${node.events ? " · " + node.events + " records" : ""}`,
      ),
    );
    button.append(label);
    button.addEventListener("click", () => showPlayer(node.id));
    $("directory").append(button);
  }
  if (!nodes.length)
    $("directory").append(el("p", "No players match this view.", "hint"));
}
function emphasis(edge) {
  return edge.kind === "presence"
    ? Math.max(0.001, edge[$("emphasis").value])
    : edge.weight;
}
function strongestEdges(edges) {
  const buckets = new Map();
  for (const edge of edges) {
    for (const id of [edge.source, edge.target]) {
      const key = `${id}:${edge.kind}`;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(edge);
    }
  }
  const keep = new Set();
  for (const bucket of buckets.values())
    bucket
      .sort((a, b) => emphasis(b) - emphasis(a))
      .slice(0, 3)
      .forEach((edge) => keep.add(edge));
  return keep;
}
let hovered = null,
  viewTouched = false;
function renderGraph() {
  cancelAnimationFrame(frame);
  const svg = $("graph");
  svg.replaceChildren();
  const defs = svgEl("defs");
  for (const [kind, color] of Object.entries(kindColors)) {
    const marker = svgEl("marker", {
      id: "arrow-" + kind,
      viewBox: "0 0 10 10",
      refX: 20,
      refY: 5,
      markerWidth: 5,
      markerHeight: 5,
      orient: "auto",
    });
    marker.append(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: color }));
    defs.append(marker);
  }
  svg.append(defs);
  world = svgEl("g");
  viewTouched = false;
  svg.append(world);
  updateTransform();
  $("empty").hidden = data.nodes.length > 0;
  const communities = [...new Set(data.nodes.map((n) => n.community))];
  const positions = new Map();
  data.nodes.forEach((n, i) => {
    const c = Math.max(0, communities.indexOf(n.community));
    const angle = (c / Math.max(communities.length, 1)) * Math.PI * 2;
    const spin = i * 2.39996;
    positions.set(n.id, {
      ...n,
      x: 480 + Math.cos(angle) * 190 + Math.cos(spin) * 70,
      y: 320 + Math.sin(angle) * 155 + Math.sin(spin) * 70,
      r: 5 + Math.min(15, Math.sqrt(n.minutes) / 7),
      vx: 0,
      vy: 0,
    });
  });
  const edges = data.edges.map((e) => ({
    ...e,
    a: positions.get(e.source),
    b: positions.get(e.target),
  }));
  const sparse = strongestEdges(edges);
  for (const edge of edges) edge.strong = sparse.has(edge);
  const springs = new Map();
  for (const edge of sparse) {
    const key = [edge.source, edge.target].sort().join(":");
    if (edge.source !== edge.target) springs.set(key, edge);
  }
  const degrees = new Map();
  for (const edge of springs.values())
    for (const id of [edge.source, edge.target])
      degrees.set(id, (degrees.get(id) || 0) + 1);
  const max = Math.max(1, ...edges.map(emphasis));
  for (const e of edges) {
    const width = 0.5 + 3 * Math.sqrt(emphasis(e) / max);
    const line = svgEl("line", {
      stroke: kindColors[e.kind],
      "stroke-width": width,
      "stroke-opacity": e.kind === "presence" ? 0.24 : 0.68,
      class: "edge",
      ...(e.directed
        ? {
            "stroke-dasharray": "4 4",
            "marker-end": "url(#arrow-" + e.kind + ")",
          }
        : e.logs
          ? { "stroke-dasharray": "8 3" }
          : {}),
    });
    const description =
      e.kind === "presence"
        ? `${e.a.name} ↔ ${e.b.name}: ${e.minutes.toFixed(1)} shared minutes, ${e.weight.toFixed(1)} crowd-adjusted minutes, ${(e.jaccard * 100).toFixed(1)}% overlap, ${e.days} UTC days`
        : `${e.a.name} ${e.directed ? "→" : "↔"} ${e.b.name}: ${e.count} ${e.kind} records${e.logs ? " · listed together; direction unverified" : ""}`;
    line.append(svgEl("title"));
    line.firstChild.textContent = description;
    line.addEventListener("click", () => {
      if (dragMoved) return;
      $("edge-summary").textContent = description;
      if (!e.directed) openLogs([e.a, e.b], e.logs ? e.kind : "");
    });
    world.append(line);
    e.element = line;
  }
  for (const n of positions.values()) {
    const group = svgEl("g", {
      class: "node",
      tabindex: 0,
      role: "button",
      "aria-label": `${n.name}, ${hours(n.minutes)} observed`,
    });
    const circle = svgEl("circle", {
      r: n.r,
      fill:
        n.community === null ? "#7e7e7e" : colors[n.community % colors.length],
      stroke: "#161616",
      "stroke-width": 2,
    });
    group.append(circle);
    const text = svgEl("text", { x: n.r + 5, y: 4 });
    text.textContent = n.name.length > 60 ? n.name.slice(0, 59) + "…" : n.name;
    group.append(text);
    n.label = text;
    const title = svgEl("title");
    title.textContent = n.name;
    group.append(title);
    group.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        showPlayer(n.id);
      }
    });
    group.addEventListener("pointerdown", (event) => beginDrag(event, n));
    group.addEventListener("pointerenter", () => {
      hovered = n.id;
      updateLabels();
    });
    group.addEventListener("pointerleave", () => {
      hovered = null;
      updateLabels();
    });
    group.addEventListener("focus", () => {
      hovered = n.id;
      updateLabels();
    });
    group.addEventListener("blur", () => {
      hovered = null;
      updateLabels();
    });
    world.append(group);
    n.element = group;
  }
  let step = 0;
  function tick() {
    if (step++ < 150) {
      const nodes = [...positions.values()];
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        if (a.fixed) continue;
        a.vx += (480 - a.x) * 0.0015;
        a.vy += (320 - a.y) * 0.0015;
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          let dx = a.x - b.x,
            dy = a.y - b.y;
          const d = Math.max(50, dx * dx + dy * dy);
          const f =
            Math.min(5, 2200 / d) + Math.max(0, 65 - Math.sqrt(d)) * 0.04;
          a.vx += (dx * f) / Math.sqrt(d);
          a.vy += (dy * f) / Math.sqrt(d);
          if (!b.fixed) {
            b.vx -= (dx * f) / Math.sqrt(d);
            b.vy -= (dy * f) / Math.sqrt(d);
          }
        }
      }
      for (const e of springs.values()) {
        const dx = e.b.x - e.a.x,
          dy = e.b.y - e.a.y,
          d = Math.hypot(dx, dy) || 1;
        const force =
          ((d - 170) * 0.004) /
          Math.sqrt(Math.max(degrees.get(e.source), degrees.get(e.target)));
        if (!e.a.fixed) {
          e.a.vx += (dx / d) * force;
          e.a.vy += (dy / d) * force;
        }
        if (!e.b.fixed) {
          e.b.vx -= (dx / d) * force;
          e.b.vy -= (dy / d) * force;
        }
      }
      for (const n of positions.values()) {
        if (!n.fixed) {
          n.x += n.vx;
          n.y += n.vy;
          n.vx *= 0.8;
          n.vy *= 0.8;
        }
      }
      frame = requestAnimationFrame(tick);
    } else if (step === 151 && !drag && !viewTouched) {
      fitGraph();
    }
    draw();
  }
  function draw() {
    for (const e of edges) {
      e.element.setAttribute("x1", e.a.x);
      e.element.setAttribute("y1", e.a.y);
      e.element.setAttribute("x2", e.b.x);
      e.element.setAttribute("y2", e.b.y);
    }
    for (const n of positions.values())
      n.element.setAttribute("transform", `translate(${n.x},${n.y})`);
    updateLabels();
  }
  world._positions = positions;
  world._edges = edges;
  world._draw = draw;
  tick();
  highlight();
}
function updateTransform() {
  if (world)
    world.setAttribute(
      "transform",
      `translate(${transform.x},${transform.y}) scale(${transform.k})`,
    );
  updateLabels();
}
function updateLabels() {
  if (!world?._positions) return;
  const occupied = [];
  const matrix = $("graph").getScreenCTM();
  const viewportScale = Math.hypot(matrix.a, matrix.b) || 1;
  const size =
    (12 * Math.sqrt(Math.max(1, transform.k))) / (transform.k * viewportScale);
  const nodes = [...world._positions.values()].sort(
    (a, b) =>
      Number(b.id === hovered || b.id === selected) -
        Number(a.id === hovered || a.id === selected) || b.minutes - a.minutes,
  );
  for (const node of nodes) {
    node.label.style.fontSize = `${size}px`;
    node.label.style.strokeWidth = `${3 / (transform.k * viewportScale)}px`;
    const x = transform.x + (node.x + node.r + 5) * transform.k;
    const y = transform.y + node.y * transform.k;
    const box = [
      x,
      y - size * transform.k,
      x + node.label.textContent.length * size * transform.k * 0.59,
      y + 6,
    ];
    const priority = node.id === hovered || node.id === selected;
    const overlaps = occupied.some(
      (other) =>
        box[0] < other[2] + 6 &&
        box[2] > other[0] - 6 &&
        box[1] < other[3] + 4 &&
        box[3] > other[1] - 4,
    );
    const visible =
      priority ||
      (!node.element.classList.contains("dim") &&
        !overlaps &&
        box[0] < 960 &&
        box[2] > 0 &&
        box[1] < 640 &&
        box[3] > 0);
    node.label.style.visibility = visible ? "visible" : "hidden";
    if (visible) occupied.push(box);
  }
}
function fitGraph() {
  if (!world || !world._positions.size) return;
  const nodes = [...world._positions.values()];
  const left = Math.min(...nodes.map((n) => n.x - n.r)) - 30;
  const right = Math.max(...nodes.map((n) => n.x + n.r + 90)) + 30;
  const top = Math.min(...nodes.map((n) => n.y - n.r)) - 30;
  const bottom = Math.max(...nodes.map((n) => n.y + n.r)) + 30;
  const k = Math.min(2.5, 900 / (right - left), 580 / (bottom - top));
  transform = {
    k,
    x: 480 - ((left + right) * k) / 2,
    y: 320 - ((top + bottom) * k) / 2,
  };
  updateTransform();
}
function point(event) {
  const p = new DOMPoint(event.clientX, event.clientY);
  return p.matrixTransform($("graph").getScreenCTM().inverse());
}
let drag = null,
  dragMoved = false;
function beginDrag(event, node) {
  if (event.button !== 0) return;
  event.stopPropagation();
  const p = point(event);
  drag = {
    node,
    p,
    x: node ? node.x : transform.x,
    y: node ? node.y : transform.y,
  };
  dragMoved = false;
  viewTouched = true;
  $("graph").setPointerCapture(event.pointerId);
}
$("graph").addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".node")) beginDrag(event, null);
});
$("graph").addEventListener("pointermove", (event) => {
  if (!drag) return;
  const p = point(event),
    dx = p.x - drag.p.x,
    dy = p.y - drag.p.y;
  if (Math.hypot(dx, dy) > 3) dragMoved = true;
  if (drag.node) {
    drag.node.fixed = true;
    drag.node.x = drag.x + dx / transform.k;
    drag.node.y = drag.y + dy / transform.k;
    world._draw();
  } else {
    transform.x = drag.x + dx;
    transform.y = drag.y + dy;
    updateTransform();
  }
});
$("graph").addEventListener("pointerup", () => {
  if (drag && drag.node && !dragMoved) showPlayer(drag.node.id);
  drag = null;
});
$("graph").addEventListener("pointercancel", () => {
  drag = null;
});
$("graph").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const p = point(event),
      old = transform.k;
    viewTouched = true;
    transform.k = Math.max(
      0.35,
      Math.min(12, old * Math.exp(-event.deltaY * 0.001)),
    );
    transform.x = p.x - ((p.x - transform.x) * transform.k) / old;
    transform.y = p.y - ((p.y - transform.y) * transform.k) / old;
    updateTransform();
  },
  { passive: false },
);
function highlight() {
  if (!world) return;
  const neighbors = new Set([selected]);
  for (const e of world._edges)
    if (e.source === selected || e.target === selected) {
      neighbors.add(e.source);
      neighbors.add(e.target);
    }
  for (const n of world._positions.values()) {
    n.element.classList.toggle("selected", n.id === selected);
    n.element.classList.toggle("dim", !!selected && !neighbors.has(n.id));
  }
  for (const e of world._edges)
    e.element.classList.toggle(
      "dim",
      !!selected && e.source !== selected && e.target !== selected,
    );
  let visible = 0;
  for (const edge of world._edges) {
    const show =
      $("network-density").value === "all" ||
      edge.strong ||
      edge.source === selected ||
      edge.target === selected;
    edge.element.style.display = show ? "" : "none";
    if (show) visible++;
  }
  $("network-count").textContent =
    `${visible.toLocaleString()} of ${data.edges.length.toLocaleString()} available links · comparison totals use all collected records`;
  updateLabels();
}
$("network-density").addEventListener("change", highlight);
new ResizeObserver(updateLabels).observe($("graph"));
async function showPlayer(id) {
  selected = id;
  highlight();
  const number = ++detailNumber;
  $("directory").hidden = true;
  $("detail").hidden = false;
  const host = $("player-detail");
  host.replaceChildren(el("p", "Loading player history…"));
  try {
    const result = await read(
      "/api/player?" + new URLSearchParams({ id, ...activeRange }),
    );
    if (number !== detailNumber) return;
    const n = data.nodes.find((x) => x.id === id);
    host.replaceChildren(
      el("h2", result.name),
      el("p", result.steam_id || result.id),
      el(
        "div",
        `${hours(n?.minutes || 0)} observed in this window`,
        "detail-stat",
      ),
    );
    host.append(
      el(
        "p",
        `First sampled ${date(result.first_seen)}. Last sampled ${date(result.last_seen)}.`,
      ),
    );
    const showLogsButton = el(
      "button",
      "Explore this player’s logs",
      "primary",
    );
    showLogsButton.addEventListener("click", () => openLogs([result]));
    const compareButton = el("button", "Add to log comparison", "person");
    compareButton.addEventListener("click", () => {
      addLogPlayer(result);
      $("log-explorer").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    host.append(showLogsButton, compareButton);
    const edges = data.edges
      .filter(
        (e) => e.kind === "presence" && (e.source === id || e.target === id),
      )
      .sort((a, b) => b.minutes - a.minutes);
    host.append(el("h3", `Shared time · ${edges.length} visible connections`));
    for (const e of edges.slice(0, 12)) {
      const other = data.nodes.find(
        (n) => n.id === (e.source === id ? e.target : e.source),
      );
      const b = el("button", `${other.name} · ${hours(e.minutes)}`, "person");
      b.addEventListener("click", () => showPlayer(other.id));
      host.append(b);
    }
    if (!edges.length)
      host.append(el("p", "No co-presence edges pass the current filters."));
    host.append(el("h3", "Administrative history"));
    host.append(
      el(
        "p",
        "All history kinds in the selected time window, including records this account authored.",
      ),
    );
    host.append(
      el(
        "p",
        result.history
          ? `Player history checked ${date(result.history.last_success)}${result.history.error ? " · " + result.history.error : ""}`
          : "Player history has not been fetched yet.",
      ),
    );
    if (!result.events.length)
      host.append(
        el(
          "p",
          "No collected records in this window. This does not establish a clean history.",
        ),
      );
    for (const e of result.events) {
      const card = el("article", undefined, "event");
      card.append(
        el("strong", `${e.kind} · ${e.scope}`),
        el("p", e.body),
        el(
          "div",
          `${date(e.created_at)} · ${e.actor_name || "Unknown staff"} → ${e.subject_name}`,
          "meta",
        ),
      );
      for (const [key, value] of Object.entries(e.details)) {
        const label = [
          "expire",
          "unbannedAt",
          "editedAt",
          "updatedAt",
        ].includes(key)
          ? date(value)
          : String(value);
        card.append(el("p", `${key}: ${label}`));
      }
      card.append(el("div", `Last retrieved ${date(e.seen_at)}`, "meta"));
      host.append(card);
    }
    if (result.history_truncated)
      host.append(
        el(
          "p",
          "Only the latest 1,000 records are shown. Narrow the time range.",
        ),
      );
  } catch (error) {
    if (number === detailNumber)
      host.replaceChildren(el("p", error.message, "error"));
  }
}
function logCoverage(status) {
  return `${status.total.toLocaleString()} collected gameplay records · ${(status.coverage * 100).toFixed(1)}% of this time range fetched without a reported cap · ${status.pending} windows pending · ${status.truncated} capped · ${status.failures} failing`;
}
function renderLogPlayers() {
  $("log-selected").replaceChildren();
  for (const player of logPlayers.values()) {
    const button = el("button", player.name + " ×", "player-chip");
    button.type = "button";
    button.setAttribute(
      "aria-label",
      "Remove " + player.name + " from log comparison",
    );
    button.addEventListener("click", () => {
      logPlayers.delete(player.id);
      renderLogPlayers();
      if (logsOpened) loadLogs();
    });
    $("log-selected").append(button);
  }
  if (!logPlayers.size)
    $("log-selected").append(el("span", "All collected players", "hint"));
}
function addLogPlayer(player) {
  if (logPlayers.size >= 20 && !logPlayers.has(player.id)) {
    $("log-status").textContent = "Choose up to 20 players.";
    return;
  }
  logPlayers.set(player.id, player);
  renderLogPlayers();
  $("log-player-options").replaceChildren();
  $("log-person-search").value = "";
  ++playerSearchRequest;
  if (logsOpened || logPlayers.size >= 2) loadLogs();
}
function renderComparison(result) {
  const host = $("comparison-summary");
  host.replaceChildren();
  if (!result.summary) return;
  const summary = result.summary;
  host.append(
    el("h3", `${result.total.toLocaleString()} matching log records`),
  );
  const types = el("div", undefined, "log-participants");
  for (const item of summary.by_kind)
    types.append(
      el(
        "span",
        `${item.kind === "death" ? "Kills / deaths" : item.kind} · ${item.count.toLocaleString()}`,
        "comparison-kind",
      ),
    );
  host.append(types);
  if (!summary.pairs.length) {
    host.append(
      el(
        "p",
        "Select two or more players to compare shared time and linked records.",
        "hint",
      ),
    );
    return;
  }
  host.append(
    el(
      "p",
      `${summary.together_minutes.toFixed(1)} minutes with all ${logPlayers.size} selected players online together · ${(summary.presence_coverage * 100).toFixed(1)}% presence coverage. Shared time uses the full time range; record counts follow the log filters.`,
      "hint",
    ),
  );
  const table = el("table", undefined, "comparison-table");
  const head = el("thead"),
    headings = el("tr");
  for (const label of [
    "Player pair",
    "Shared minutes",
    "Linked records",
    "By type",
    "",
  ])
    headings.append(el("th", label));
  head.append(headings);
  table.append(head);
  const body = el("tbody");
  for (const pair of summary.pairs) {
    const a = logPlayers.get(pair.source),
      b = logPlayers.get(pair.target);
    const row = el("tr");
    row.append(
      el("td", `${a?.name || pair.source} ↔ ${b?.name || pair.target}`),
      el("td", pair.shared_minutes.toFixed(1)),
      el("td", pair.records.toLocaleString()),
      el(
        "td",
        Object.entries(pair.by_kind)
          .map(([kind, count]) => `${kind}: ${count}`)
          .join(" · ") || "No matching records",
      ),
    );
    const action = el("td"),
      button = el("button", "View records");
    button.type = "button";
    button.disabled = !pair.records;
    button.addEventListener("click", () => {
      logPlayers.clear();
      logPlayers.set(a.id, a);
      logPlayers.set(b.id, b);
      $("log-match").value = "between";
      renderLogPlayers();
      loadLogs();
    });
    action.append(button);
    row.append(action);
    body.append(row);
  }
  table.append(body);
  host.append(table);
  host.append(
    el(
      "p",
      "Counts are log records, not unique actions. A record involving three people appears in three pair totals. Shared time alone does not establish an interaction; missing records do not establish that none occurred.",
      "hint",
    ),
  );
}
function openLogs(players, kind = "") {
  logPlayers.clear();
  for (const player of players) logPlayers.set(player.id, player);
  $("log-kind").value = kind;
  $("log-match").value = "between";
  $("log-text").value = "";
  renderLogPlayers();
  loadLogs();
  $("log-explorer").scrollIntoView({ behavior: "smooth", block: "start" });
}
function renderLog(row) {
  const card = el("article", undefined, "log-record");
  card.dataset.logId = row.id;
  const heading = el("div", undefined, "log-record-heading");
  heading.append(
    el(
      "strong",
      row.kind === "death" ? "KILLS / DEATHS" : row.kind.toUpperCase(),
    ),
    el("time", date(row.created_at)),
  );
  card.append(heading, el("p", row.message, "log-message"));
  const people = el("div", undefined, "log-participants");
  for (const player of row.participants) {
    const button = el("button", player.name, "player-chip");
    button.type = "button";
    button.title = "Add to comparison: " + (player.steam_id || player.id);
    button.addEventListener("click", () => addLogPlayer(player));
    people.append(button);
  }
  card.append(people);
  if (row.participants.length < 2)
    card.append(
      el(
        "p",
        row.participant_status === "missing"
          ? "The API did not supply participant data."
          : row.participants.length === 1
            ? "Only one linked player. Recipients or bystanders are not identified."
            : "No linked players supplied.",
        "hint",
      ),
    );
  const source = el("details", undefined, "log-source");
  source.append(
    el("summary", "Source details"),
    el(
      "p",
      `Category: ${row.category} · Log ID: ${row.id} · Raw timestamp: ${row.timestamp_s} seconds · Retrieved: ${date(row.seen_at)}`,
    ),
  );
  card.append(source);
  return card;
}
async function loadLogs(more = false) {
  if (!activeRange) return;
  logsOpened = true;
  const number = ++logRequest;
  $("log-more").hidden = true;
  $("log-status").classList.remove("error");
  let query;
  if (more && lastLogQuery && logCursor) {
    query = new URLSearchParams(lastLogQuery);
    query.set("before", JSON.stringify(logCursor));
  } else {
    query = new URLSearchParams({
      ...activeRange,
      players: [...logPlayers.keys()].join(","),
      match: $("log-match").value,
      text: $("log-text").value,
      limit: 50,
    });
    if ($("log-kind").value) query.set("kinds", $("log-kind").value);
    lastLogQuery = query.toString();
    logCursor = null;
    $("log-results").replaceChildren();
    $("comparison-summary").replaceChildren();
  }
  $("log-status").textContent = "Reading collected logs…";
  try {
    const result = await read("/api/logs?" + query);
    if (number !== logRequest) return;
    if (!more) renderComparison(result);
    for (const row of result.logs) $("log-results").append(renderLog(row));
    logCursor = result.next;
    $("log-more").hidden = !logCursor;
    const shown = $("log-results").children.length;
    $("log-status").textContent =
      `${shown.toLocaleString()} of ${result.total.toLocaleString()} matching records. ${logCoverage(result.status)}. ${result.total ? "" : "No collected records match; this does not prove that no interaction occurred."}`;
  } catch (error) {
    if (number === logRequest) {
      $("log-status").textContent = error.message;
      $("log-status").classList.add("error");
    }
  }
}
function invalidateLogs() {
  ++logRequest;
  logCursor = null;
  $("log-more").hidden = true;
  $("log-results").replaceChildren();
  $("comparison-summary").replaceChildren();
  $("log-status").textContent = "Filters changed. Select Find logs to search.";
}
$("log-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  loadLogs();
});
$("log-more").addEventListener("click", () => loadLogs(true));
$("log-clear").addEventListener("click", () => {
  logPlayers.clear();
  renderLogPlayers();
  if (logsOpened) loadLogs();
});
for (const id of ["log-match", "log-kind", "log-text"])
  $(id).addEventListener("input", invalidateLogs);
$("log-person-search").addEventListener("input", () => {
  clearTimeout(playerSearchTimer);
  const number = ++playerSearchRequest;
  const value = $("log-person-search").value.trim();
  $("log-player-options").replaceChildren();
  if (!value) return;
  playerSearchTimer = setTimeout(async () => {
    try {
      const result = await read(
        "/api/players?" + new URLSearchParams({ q: value }),
      );
      if (number !== playerSearchRequest) return;
      for (const player of result.players) {
        const button = el(
          "button",
          `${player.name} · ${player.steam_id || player.id}`,
          "person",
        );
        button.type = "button";
        button.addEventListener("click", () => addLogPlayer(player));
        $("log-player-options").append(button);
      }
      if (!result.players.length)
        $("log-player-options").append(
          el("p", "No collected player matches.", "hint"),
        );
    } catch (error) {
      if (number === playerSearchRequest)
        $("log-player-options").append(el("p", error.message, "error"));
    }
  }, 200);
});
renderLogPlayers();

function clearSelection() {
  selected = null;
  ++detailNumber;
  $("directory").hidden = false;
  $("detail").hidden = true;
  highlight();
}
$("filters").addEventListener("submit", (e) => {
  e.preventDefault();
  load();
});
$("refresh").addEventListener("click", load);
$("search").addEventListener("input", () => {
  if (data) {
    clearSelection();
    directory();
  }
});
$("clear-selection").addEventListener("click", clearSelection);
$("reset-view").addEventListener("click", () => {
  fitGraph();
});
$("emphasis").addEventListener("change", () => {
  if (data) renderGraph();
});
document.querySelectorAll("[data-days]").forEach((b) =>
  b.addEventListener("click", () => {
    preset(+b.dataset.days);
    load();
  }),
);
preset(7);
read("/auth/session")
  .then((session) => {
    if (!session.enabled) return;
    $("manage-access").hidden = !session.owner;
    const button = $("sign-out");
    button.hidden = false;
    button.title = `Signed in as ${session.name}`;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await fetch("/auth/logout", {
          method: "POST",
          headers: { "X-CSRF-Token": session.csrf },
        });
        if (!response.ok) throw new Error("Sign-out failed");
        window.location.assign("/");
      } catch {
        button.textContent = "Retry sign out";
        button.disabled = false;
      }
    });
  })
  .catch(() => {});
load();
