/* ============================================================
   REPORT HEAT — dashboard logic
   Fetches the denormalized feed written by the Python pipeline
   (site/data/dashboard.json) and renders a filterable,
   thermal-scored monitoring board.
   ============================================================ */

const DATA_URL = "data/dashboard.json";

const state = {
  data: null,
  items: [],
  rankWindow: "7d",
  filters: { kind: "all", source: "all", query: "", onlyHeat: false },
};

/* ---------- thermal scale ----------
   Map a heat score to a color + fill fraction. Government docs
   mostly read zero, so the scale is tuned to make the rare real
   signal unmistakable. One exact-URL mention scores 6. */
function thermal(score) {
  if (!score || score <= 0) {
    return { color: "var(--cold)", glow: "none", frac: 0.07, level: "dormant" };
  }
  // log-ish normalization: 6 -> ~0.5, 18 -> ~0.85, 30+ -> ~1
  const frac = Math.min(1, 0.3 + Math.log10(score + 1) * 0.46);
  let color, glow, level;
  if (score < 6) {
    color = "var(--gold)";
    glow = "none";
    level = "warm";
  } else if (score < 14) {
    color = "var(--orange)";
    glow = "0 0 11px rgba(255,94,26,0.5)";
    level = "warm-high";
  } else {
    color = "var(--pink)";
    glow = "0 0 15px rgba(232,30,99,0.6)";
    level = "hot";
  }
  return { color, glow, frac, level };
}

/* ---------- helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtNum(n) {
  return new Intl.NumberFormat("en-US").format(n);
}

function fmtDate(iso) {
  if (!iso) return "undated";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}

function relativeTime(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diff = Date.now() - then;
  const mins = Math.round(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr${hrs === 1 ? "" : "s"} ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function windowLabel(key) {
  return key === "today" ? "Today" : key.toUpperCase();
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return url; }
}

/* ---------- load ---------- */
async function load() {
  try {
    const res = await fetch(DATA_URL, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.data = data;
    state.rankWindow = data.rank_window || "7d";
    state.items = data.items || [];
    render();
  } catch (err) {
    const status = $("#board-status");
    status.classList.add("is-error");
    status.textContent = `Could not load data feed (${err.message}). Run the pipeline to generate site/data/dashboard.json, and serve over http (not file://).`;
  }
}

/* ---------- top-level render ---------- */
function render() {
  const d = state.data;
  hydrateHeader(d);
  hydrateControls(d);
  applyFilters();
  renderUntracked(d.untracked || []);
}

/* ---------- bubbling under: shared gov links not in the inventory ---------- */
function renderUntracked(rows) {
  const section = $("#bubbling-section");
  if (!section) return;
  if (!rows.length) { section.hidden = true; return; }
  section.hidden = false;
  $("#bubbling-list").innerHTML = rows.slice(0, 12).map((r) => `
    <div class="bubbling-row">
      <a class="bubbling-link" href="${escapeHtml(r.target_url)}" target="_blank" rel="noopener">${escapeHtml(r.target_url)}</a>
      <span class="bubbling-meta mono">${fmtNum(r.mentions)} mention${r.mentions === 1 ? "" : "s"}${r.engagement ? ` · ${fmtNum(r.engagement)} engagement` : ""} · ${escapeHtml((r.providers || []).join(", "))}</span>
    </div>`).join("");
}

function hydrateHeader(d) {
  $("#meta-generated").textContent = relativeTime(d.generated_at);
  $("#meta-generated").title = d.generated_at;
  $("#footer-generated").textContent =
    `Generated ${new Date(d.generated_at).toLocaleString("en-US")} · ${(d.providers || []).join(" · ")}`;
  $("#footer-sources").innerHTML = (d.stats.by_source || [])
    .map((s) => `${escapeHtml(s.name)} <span style="color:var(--ink-faint)">(${s.count})</span>`)
    .join("<br>");
}

function hydrateControls(d) {
  // source dropdown
  const sourceSel = $("#source-select");
  for (const s of d.stats.by_source || []) {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = `${s.name} (${s.count})`;
    sourceSel.appendChild(opt);
  }
  // window dropdown
  const winSel = $("#window-select");
  for (const key of d.windows || ["today", "7d", "30d"]) {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = windowLabel(key);
    if (key === state.rankWindow) opt.selected = true;
    winSel.appendChild(opt);
  }
}

/* ---------- score for the active ranking window ---------- */
function scoreFor(item) {
  const w = item.windows?.[state.rankWindow];
  return w ? w.score : item.heat_score || 0;
}

/* ---------- filtering + board render ---------- */
function applyFilters() {
  const { kind, source, query, onlyHeat } = state.filters;
  const q = query.trim().toLowerCase();

  let rows = state.items.filter((it) => {
    if (kind !== "all" && it.kind !== kind) return false;
    if (source !== "all" && it.source !== source) return false;
    if (onlyHeat && scoreFor(it) <= 0) return false;
    if (q) {
      const hay = `${it.title} ${it.source} ${it.agency || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  rows.sort((a, b) => scoreFor(b) - scoreFor(a) || (b.published_date || "").localeCompare(a.published_date || ""));

  renderBoard(rows);

  const total = state.items.length;
  const withHeat = rows.filter((it) => scoreFor(it) > 0).length;
  const count = $("#result-count");
  if (count) {
    const shown = rows.length === total ? `${total} documents` : `${rows.length} of ${total} documents`;
    count.textContent = withHeat
      ? `${shown} · ${withHeat} drawing heat in the ${windowLabel(state.rankWindow)} window`
      : shown;
  }
}

function renderBoard(rows) {
  const list = $("#board-list");
  const empty = $("#board-empty");
  const status = $("#board-status");
  status.hidden = true;
  list.innerHTML = "";

  if (rows.length === 0) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const tpl = $("#row-template");
  const windows = state.data.windows || ["today", "7d", "30d"];

  rows.forEach((it, idx) => {
    const node = tpl.content.firstElementChild.cloneNode(true);

    const charting = scoreFor(it) > 0;
    $(".rank-num", node).textContent = String(idx + 1);
    if (charting) {
      $(".rank-bullet", node).classList.add("is-hot");
      $(".rank-bullet", node).title = "Drawing public heat";
    }

    // tags
    const tags = $(".row-tags", node);
    tags.innerHTML =
      `<span class="tag tag--${it.kind}">${it.kind}</span>` +
      `<span class="tag tag--format">${escapeHtml(it.format)}</span>` +
      (it.is_new ? `<span class="tag tag--new">new</span>` : "");

    $(".row-title", node).textContent = it.title;

    const meta = $(".row-meta", node);
    meta.innerHTML = [
      escapeHtml(it.source),
      it.agency ? escapeHtml(it.agency) : null,
      fmtDate(it.published_date),
    ].filter(Boolean).join(' <span class="sep">/</span> ');

    // thermal meter — one cell per window
    const meter = $(".row-meter", node);
    meter.appendChild(buildMeter(it, windows));

    // score (active window)
    const score = scoreFor(it);
    const sv = $(".row-score-value", node);
    sv.textContent = score.toFixed(score % 1 === 0 ? 0 : 1);
    if (score <= 0) sv.classList.add("is-zero");

    // detail (lazy on first open)
    let built = false;
    const detail = $(".row-detail", node);
    const toggle = () => {
      const open = node.classList.toggle("is-open");
      node.setAttribute("aria-expanded", String(open));
      if (open && !built) {
        detail.innerHTML = buildDetail(it);
        built = true;
      }
      detail.hidden = !open;
    };
    node.addEventListener("click", (e) => {
      if (e.target.closest("a")) return; // let links work
      toggle();
    });
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });

    list.appendChild(node);
  });
}

function buildMeter(it, windows) {
  const wrap = document.createElement("div");
  wrap.className = "meter";
  for (const key of windows) {
    const w = it.windows[key] || { score: 0 };
    const t = thermal(w.score);
    const cell = document.createElement("div");
    cell.className = "meter-cell" + (key === state.rankWindow ? " is-rank" : "");
    cell.title = `${windowLabel(key)}: heat ${w.score}`;
    cell.innerHTML = `
      <div class="meter-bar-track">
        <div class="meter-bar-fill" style="height:${(t.frac * 100).toFixed(0)}%;background:${t.color};box-shadow:${t.glow}"></div>
      </div>
      <div class="meter-cell-label">${windowLabel(key)}</div>`;
    wrap.appendChild(cell);
  }
  return wrap;
}

function buildDetail(it) {
  const links = [
    ["page", it.url],
    it.document_url && it.document_url !== it.url ? ["document", it.document_url] : null,
    it.source_page ? ["source", it.source_page] : null,
  ].filter(Boolean);

  const linkHtml = links.map(([label, url]) =>
    `<a class="detail-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">
       <span class="lk-label">${label}</span><span>${escapeHtml(hostOf(url))} ↗</span>
     </a>`).join("");

  const rationale = (it.rationale || [])
    .map((r) => `<li>${escapeHtml(r)}</li>`).join("");

  const confLabel = (c) => ({
    exact_url: "exact link",
    filename: "filename",
  }[c] || (c || "").replace(/_/g, " "));

  let evidence;
  if (it.mentions && it.mentions.length) {
    evidence = `<div class="evidence-list">` + it.mentions.map((m) =>
      `<a class="evidence-item" href="${escapeHtml(m.url || it.heat_url)}" target="_blank" rel="noopener">
        <span class="evidence-provider">${escapeHtml(m.provider)}</span>
        <span class="evidence-title">${escapeHtml(m.title || m.url || "—")}</span>
        <span class="evidence-conf">${escapeHtml(confLabel(m.confidence))}</span>
      </a>`).join("") + `</div>`;
  } else {
    evidence = `<p class="evidence-empty">No public link pickups captured in any window.</p>`;
  }

  return `
    <div class="detail-grid">
      <div class="detail-block">
        <h4>Why this score</h4>
        <ul class="detail-rationale">${rationale}</ul>
      </div>
      <div class="detail-block">
        <h4>Canonical links</h4>
        <div class="detail-links">${linkHtml}</div>
      </div>
      <div class="detail-block evidence">
        <h4>Evidence · ${it.mentions ? it.mentions.length : 0} mention${(it.mentions && it.mentions.length === 1) ? "" : "s"}</h4>
        ${evidence}
      </div>
    </div>`;
}

/* ---------- events ---------- */
function wireEvents() {
  $("#search").addEventListener("input", (e) => {
    state.filters.query = e.target.value;
    applyFilters();
  });

  $$(".chip[data-filter='kind']").forEach((chip) => {
    chip.addEventListener("click", () => {
      $$(".chip[data-filter='kind']").forEach((c) => c.classList.remove("is-active"));
      chip.classList.add("is-active");
      state.filters.kind = chip.dataset.value;
      applyFilters();
    });
  });

  $("#only-heat").addEventListener("change", (e) => {
    state.filters.onlyHeat = e.target.checked;
    applyFilters();
  });

  $("#source-select").addEventListener("change", (e) => {
    state.filters.source = e.target.value;
    applyFilters();
  });

  $("#window-select").addEventListener("change", (e) => {
    state.rankWindow = e.target.value;
    applyFilters();
  });

  // methodology dialog
  const dialog = $("#methodology-dialog");
  if (dialog) {
    $("#methodology-open").addEventListener("click", () => dialog.showModal());
    $("#methodology-close").addEventListener("click", () => dialog.close());
    // click on the backdrop (outside the inner panel) closes
    dialog.addEventListener("click", (e) => {
      if (e.target === dialog) dialog.close();
    });
  }
}

wireEvents();
load();
