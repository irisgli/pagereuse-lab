/* PageReuse Lab viewer. Vanilla JS, no build step. */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const SERIES = ["#0e6f6b", "#b4482f", "#3b5b8c", "#c48a1d", "#6a4c93", "#2e8540", "#8c3b5b", "#4d6a75"];

  const state = { runs: [], current: null, step: 0, metric: "exact_free", compare: new Set(), predictions: {} };
  const AMBER = [196, 138, 29];

  // ---------- colour scales ----------
  const lerp = (a, b, t) => a + (b - a) * t;
  const hex = (c) => `#${c.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("")}`;
  const mix = (c1, c2, t) => hex([0, 1, 2].map((i) => lerp(c1[i], c2[i], t)));
  const FULL = [28, 61, 90], EMPTY = [230, 238, 244], WHITE = [255, 255, 255], RUST = [180, 72, 47], TEAL = [14, 111, 107];

  const METRICS = {
    exact_free: {
      title: "Free bytes per page, measured (pd_upper minus pd_lower)",
      value: (p) => p.exact_free_bytes, max: (bs) => bs,
      color: (v, max) => mix(FULL, EMPTY, v / max), low: "page full", high: "page empty",
    },
    fsm: {
      title: "Free bytes per page as recorded in the free space map",
      value: (p) => p.fsm_avail_bytes, max: (bs) => bs,
      color: (v, max) => mix(FULL, EMPTY, v / max), low: "0 recorded", high: "page empty",
    },
    fsm_gap: {
      title: "FSM record minus measured free bytes (rust overstates, teal understates)",
      value: (p) => p.fsm_avail_bytes - p.exact_free_bytes,
      max: (bs, pages) => Math.max(64, ...pages.map((p) => Math.abs(p.fsm_avail_bytes - p.exact_free_bytes))),
      color: (v, max) => (v >= 0 ? mix(WHITE, RUST, Math.min(1, v / max)) : mix(WHITE, TEAL, Math.min(1, -v / max))),
      diverging: true, low: "FSM understates", high: "FSM overstates",
    },
    lp_dead: {
      title: "Dead line pointers per page (pruned, awaiting VACUUM)",
      value: (p) => p.lp_dead, max: (bs, pages) => Math.max(1, ...pages.map((p) => p.lp_total)),
      color: (v, max) => mix(WHITE, RUST, v / max), low: "none", high: "all slots",
    },
    lp_redirect: {
      title: "Redirect line pointers per page (left by HOT chains)",
      value: (p) => p.lp_redirect, max: (bs, pages) => Math.max(1, ...pages.map((p) => p.lp_total)),
      color: (v, max) => mix(WHITE, TEAL, v / max), low: "none", high: "all slots",
    },
    normal_xmax_set: {
      title: "Tuples whose header carries a deleting or updating transaction id",
      value: (p) => p.normal_xmax_set, max: (bs, pages) => Math.max(1, ...pages.map((p) => p.lp_total)),
      color: (v, max) => mix(WHITE, RUST, v / max), low: "none", high: "all tuples",
    },
    all_visible: {
      title: "Pages flagged all-visible (VACUUM found no dead or in-progress tuples)",
      value: (p) => (p.all_visible ? 1 : 0), max: () => 1,
      color: (v) => (v ? hex(TEAL) : hex(EMPTY)), low: "not set", high: "set",
    },
    live: {
      title: "Live tuples per page (normal line pointers without xmax)",
      value: (p) => p.lp_normal - p.normal_xmax_set, max: (bs, pages) => Math.max(1, ...pages.map((p) => p.lp_total)),
      color: (v, max) => mix(EMPTY, FULL, v / max), low: "none", high: "page full",
    },
    delta_free: {
      title: "Change in measured free bytes since the previous step (amber: page is new)",
      value: (p, prev) => (prev ? p.exact_free_bytes - prev.exact_free_bytes : null),
      max: (bs, pages, prevPages) => Math.max(64, ...pages.map((p, i) => (prevPages[i] ? Math.abs(p.exact_free_bytes - prevPages[i].exact_free_bytes) : 0))),
      color: (v, max) => (v === null ? hex(AMBER) : v >= 0 ? mix(WHITE, TEAL, Math.min(1, v / max)) : mix(WHITE, RUST, Math.min(1, -v / max))),
      diverging: true, low: "filled", high: "freed", needsPrev: true,
    },
  };

  // ---------- data ----------
  async function loadIndex() {
    state.runs = await (await fetch("/api/runs")).json();
    renderRunList();
    $("runs-empty").hidden = state.runs.length > 0;
    $("run-count").textContent = state.runs.length ? `${state.runs.length}` : "";
    if (state.runs.length && !state.current) selectRun(state.runs[0].run_id);
  }

  async function predictionsFor(id) {
    if (!state.predictions[id]) {
      const rows = await (await fetch(`/api/runs/${id}/predictions`)).json();
      state.predictions[id] = rows.filter((r) => r.predictor === "capacity");
    }
    return state.predictions[id];
  }

  function predictionAt(id, step) {
    return (state.predictions[id] || []).find((r) => r.step === step) || null;
  }

  async function selectRun(id) {
    const [run] = await Promise.all([(await fetch(`/api/runs/${id}`)).json(), predictionsFor(id)]);
    state.current = run;
    state.step = state.current.checkpoints.length - 1;
    document.querySelectorAll(".run-item").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
    renderRun();
  }

  // ---------- run list ----------
  function groupOf(r) {
    const t = r.table;
    if (t.startsWith("prl_ff")) return "fillfactor sweep";
    if (t.startsWith("prl_upd")) return "update patterns";
    if (t.startsWith("prl_cyc")) return "delete cycles";
    if (t.startsWith("prl_snap") || t.startsWith("prl_ovl")) return "long snapshots";
    if (t.startsWith("prl_conc")) return "concurrent sessions";
    if (t.startsWith("prl_av")) return "autovacuum";
    if (t.startsWith("prl_sto")) return "indexes and toast";
    return "single runs";
  }

  function renderRunList() {
    const ul = $("run-list");
    ul.innerHTML = "";
    const groups = new Map();
    for (const r of state.runs) {
      const g = groupOf(r);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(r);
    }
    for (const [g, runs] of groups) {
      const h = document.createElement("li");
      h.className = "group";
      h.textContent = g;
      ul.appendChild(h);
      for (const r of runs) {
        const li = document.createElement("li");
        li.className = "run-item";
        li.dataset.id = r.run_id;
        const first = r.main_pages[0], last = r.main_pages[r.main_pages.length - 1];
        li.innerHTML = `<input type="checkbox" aria-label="compare ${r.label}">
          <span class="label"></span>
          <span class="pages num">${fmt(first)} <b>→ ${fmt(last)}</b></span>`;
        li.querySelector(".label").textContent = r.label;
        const cb = li.querySelector("input");
        cb.checked = state.compare.has(r.run_id);
        cb.addEventListener("click", (e) => e.stopPropagation());
        cb.addEventListener("change", () => { cb.checked ? state.compare.add(r.run_id) : state.compare.delete(r.run_id); renderCompare(); });
        li.addEventListener("click", () => selectRun(r.run_id));
        ul.appendChild(li);
      }
    }
  }

  // ---------- run detail ----------
  function renderRun() {
    const r = state.current;
    $("run-panel").hidden = false;
    $("run-title").textContent = r.config.label || r.config.table;
    const c = r.config;
    $("run-config").textContent = `${c.table}, ${fmt(c.rows)} rows of ${c.payload_bytes} bytes, fillfactor ${c.fillfactor}, seed ${c.seed}, PostgreSQL ${r.environment.server_version}`;
    renderSteps();
    renderHeat();
    renderFacts();
  }

  function stepLabel(cp) {
    const o = cp.operation;
    if (o.op === "update") return `update ${o.column}${o.grow_bytes ? ` +${o.grow_bytes}B` : ""}`;
    if (o.op === "delete") return `delete ${o.pattern}`;
    if (o.op === "insert" && o.sessions > 1) return `insert ×${o.sessions}`;
    if (o.op === "vacuum" && o.snapshot_held) return `vacuum (${(o.snapshots_held || ["held"]).join(",")} held)`;
    if (o.op === "wait_autovacuum") return o.ran ? `autovacuum ran (${o.seconds}s)` : "no autovacuum";
    if (o.op === "hold_snapshot" || o.op === "release_snapshot") return `${o.op.split("_")[0]} ${o.name || ""}`;
    return o.op.replace("_", " ");
  }

  function renderSteps() {
    const wrap = $("steps");
    wrap.innerHTML = "";
    const cps = state.current.checkpoints;
    const max = Math.max(...cps.map((c) => c.relation.exact.main_pages));
    cps.forEach((cp, i) => {
      const b = document.createElement("button");
      b.className = "step";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(i === state.step));
      const pages = cp.relation.exact.main_pages;
      const pred = predictionAt(state.current.run_id, i);
      const err = pred ? pred.error : 0;
      const errText = pred && err ? `<em class="${err > 0 ? "over" : "under"}">${err > 0 ? "+" : ""}${err}</em>` : "";
      const marker = pred ? `<i class="pred" style="bottom:${Math.max(3, (pred.predicted / max) * 56)}px" title="model predicted ${fmt(pred.predicted)}"></i>` : "";
      b.innerHTML = `<span class="n">${fmt(pages)}${errText}</span><span class="barwrap"><span class="bar" style="height:${Math.max(3, (pages / max) * 56)}px"></span>${marker}</span><span></span>`;
      b.lastChild.textContent = stepLabel(cp);
      b.title = pred ? `${cp.name}: ${fmt(pages)} pages, model predicted ${fmt(pred.predicted)}` : `${cp.name}: ${fmt(pages)} pages`;
      b.addEventListener("click", () => { state.step = i; renderSteps(); renderHeat(); renderFacts(); });
      wrap.appendChild(b);
    });
  }

  let layout = null;
  function renderHeat() {
    const cp = state.current.checkpoints[state.step];
    const pages = cp.pages;
    const prevPages = state.step > 0 ? state.current.checkpoints[state.step - 1].pages : [];
    const bs = state.current.environment.block_size;
    const m = METRICS[state.metric];
    const max = m.max(bs, pages, prevPages);
    $("heat-title").textContent = `${stepLabel(cp)}: ${fmt(pages.length)} pages`;

    const canvas = $("heat");
    const width = canvas.parentElement.clientWidth - 28;
    const gap = 1;
    let cell = Math.max(3, Math.min(14, Math.floor(Math.sqrt((width * 420) / pages.length)) - gap));
    const cols = Math.max(1, Math.floor(width / (cell + gap)));
    const rows = Math.ceil(pages.length / cols);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = rows * (cell + gap) * dpr;
    canvas.style.height = `${rows * (cell + gap)}px`;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, rows * (cell + gap));
    pages.forEach((p, i) => {
      ctx.fillStyle = m.color(m.value(p, prevPages[i]), max);
      ctx.fillRect((i % cols) * (cell + gap), Math.floor(i / cols) * (cell + gap), cell, cell);
    });
    layout = { cols, cell, gap, pages };

    const ramp = m.diverging
      ? `linear-gradient(90deg, ${hex(TEAL)}, #fff, ${hex(RUST)})`
      : `linear-gradient(90deg, ${m.color(0, max)}, ${m.color(max, max)})`;
    let range = m.diverging ? `±${fmt(max)} B` : "";
    if (m.needsPrev) {
      const added = pages.length - prevPages.length;
      range += added > 0 ? `, <span class="swatch" style="background:${hex(AMBER)}"></span>${fmt(added)} new pages` : added < 0 ? `, ${fmt(-added)} pages truncated` : "";
    }
    $("legend").innerHTML = `<span>${m.low}</span><span class="ramp" style="background:${ramp}"></span><span>${m.high} ${range}</span><span class="muted" style="margin-left:auto">${m.title}</span>`;
  }

  function renderFacts() {
    const cp = state.current.checkpoints[state.step];
    const e = cp.relation.exact, f = cp.relation.fsm, s = cp.relation.estimated;
    const bytes = e.main_pages * state.current.environment.block_size;
    const facts = [
      ["Relation size", `${fmt(e.main_pages)} <small>pages, ${(bytes / 1048576).toFixed(2)} MB</small>`],
      ["Live tuples", `${fmt(e.pgstattuple_tuple_count)} <small>measured</small>`],
      ["Dead tuples", `${fmt(e.pgstattuple_dead_tuple_count)} <small>measured</small>`],
      ["Free space, measured", `${(e.page_free_bytes_total / 1024).toFixed(0)} <small>kB</small>`],
      ["Free space, FSM record", `${(f.fsm_total_avail_bytes / 1024).toFixed(0)} <small>kB on ${fmt(f.pages_with_fsm_space)} pages</small>`],
      ["All-visible pages", `${fmt(e.pages_all_visible)} <small>of ${fmt(e.main_pages)}</small>`],
      ["Line pointers", `${fmt(e.lp_dead_total)} <small>dead</small> ${fmt(e.lp_redirect_total)} <small>redirect</small> ${fmt(e.lp_unused_total)} <small>unused</small>`],
      ["Updates so far", `${fmt(s.n_tup_upd)} <small>${fmt(s.n_tup_hot_upd)} HOT, cumulative stats</small>`],
    ];
    const pred = predictionAt(state.current.run_id, state.step);
    if (pred) {
      const verdict = pred.error === 0 ? "exact" : `${pred.error > 0 ? "over" : "under"} by ${fmt(Math.abs(pred.error))}`;
      facts.push(["Model prediction", `${fmt(pred.predicted)} <small>pages, ${verdict}</small>`]);
    }
    const idx = cp.relation.indexes || [];
    if (idx.length) {
      const bytes = idx.reduce((a, i) => a + i.main_bytes, 0);
      const leaf = idx.reduce((a, i) => a + i.leaf_pages, 0);
      const dens = idx.map((i) => `${i.name.replace(/^.*_(\w+)$/, "$1")} ${Number(i.avg_leaf_density).toFixed(0)}%`).join(", ");
      facts.push(["Indexes", `${(bytes / 1024).toFixed(0)} <small>kB, ${fmt(leaf)} leaf pages, density ${dens}</small>`]);
    }
    if (cp.relation.toast) {
      const t = cp.relation.toast;
      facts.push(["TOAST", `${fmt(t.main_pages)} <small>pages, ${fmt(t.pgstattuple_tuple_count)} chunks live, ${fmt(t.pgstattuple_dead_tuple_count)} dead</small>`]);
    }
    if (e.total_bytes) facts.push(["Total on disk", `${(e.total_bytes / 1048576).toFixed(2)} <small>MB, all forks, indexes and TOAST</small>`]);
    const n = cp.observer_effect.pages_changed_by_pgstattuple.length;
    if (n) facts.push(["Observer effect", `${fmt(n)} <small>pages changed by pgstattuple</small>`]);
    $("facts").innerHTML = facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  }

  // ---------- tooltip ----------
  const tip = $("tip");
  $("heat").addEventListener("mousemove", (ev) => {
    if (!layout) return;
    const rect = ev.target.getBoundingClientRect();
    const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
    const col = Math.floor(x / (layout.cell + layout.gap)), row = Math.floor(y / (layout.cell + layout.gap));
    const p = col < layout.cols ? layout.pages[row * layout.cols + col] : null;
    if (!p) { tip.hidden = true; return; }
    tip.hidden = false;
    tip.innerHTML = `<b>page ${p.blkno}</b><br>free ${fmt(p.exact_free_bytes)} B measured, ${fmt(p.fsm_avail_bytes)} B in FSM<br>` +
      `${p.lp_normal} tuples (${fmt(p.normal_tuple_bytes)} B), ${p.normal_xmax_set} with xmax<br>` +
      `line pointers: ${p.lp_dead} dead, ${p.lp_redirect} redirect, ${p.lp_unused} unused<br>` +
      `${p.all_visible ? "all-visible" : "not all-visible"}`;
    const tx = Math.min(ev.clientX + 14, window.innerWidth - 280), ty = ev.clientY + 14;
    tip.style.transform = `translate(${tx}px, ${ty}px)`;
  });
  $("heat").addEventListener("mouseleave", () => { tip.hidden = true; });
  $("metric").addEventListener("change", (e) => { state.metric = e.target.value; renderHeat(); });
  window.addEventListener("resize", () => { if (state.current) renderHeat(); });

  // ---------- comparison ----------
  async function renderCompare() {
    const svg = $("compare"), legend = $("compare-legend");
    const runs = state.runs.filter((r) => state.compare.has(r.run_id));
    await Promise.all(runs.map((r) => predictionsFor(r.run_id)));
    legend.innerHTML = "";
    if (!runs.length) {
      svg.innerHTML = `<text x="360" y="135" text-anchor="middle">Tick two or more runs to compare how their page counts move step by step. Dashed lines are the capacity model's one-step-ahead predictions.</text>`;
      return;
    }
    const W = 720, H = 260, L = 52, R = 16, T = 16, B = 34;
    const nSteps = Math.max(...runs.map((r) => r.main_pages.length));
    const maxY = Math.max(...runs.flatMap((r) => r.main_pages));
    const x = (i) => L + (i / Math.max(1, nSteps - 1)) * (W - L - R);
    const y = (v) => T + (1 - v / maxY) * (H - T - B);
    let out = "";
    const ticks = 4;
    for (let t = 0; t <= ticks; t++) {
      const v = Math.round((maxY * t) / ticks);
      out += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`;
    }
    for (let i = 0; i < nSteps; i++) out += `<text x="${x(i)}" y="${H - 12}" text-anchor="middle">step ${i}</text>`;
    runs.forEach((r, k) => {
      const col = SERIES[k % SERIES.length];
      const d = r.main_pages.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" ");
      out += `<path class="series" d="${d}" stroke="${col}"/>`;
      const preds = state.predictions[r.run_id] || [];
      if (preds.length) {
        const pd = preds.map((p, j) => `${j ? "L" : "M"}${x(p.step)},${y(p.predicted)}`).join(" ");
        out += `<path class="predicted" d="${pd}" stroke="${col}"><title>${r.label}: capacity model</title></path>`;
      }
      out += r.main_pages.map((v, i) => `<circle class="pt" cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${col}"><title>${r.label}, ${r.steps[i]}: ${fmt(v)} pages</title></circle>`).join("");
      const li = document.createElement("li");
      li.innerHTML = `<span class="swatch" style="background:${col}"></span>`;
      const errs = (state.predictions[r.run_id] || []).map((p) => Math.abs(p.error));
      const mae = errs.length ? (errs.reduce((a, b) => a + b, 0) / errs.length).toFixed(1) : null;
      li.appendChild(document.createTextNode(`${r.label}: ${fmt(r.main_pages[0])} → ${fmt(r.main_pages[r.main_pages.length - 1])} pages${mae !== null ? `, model MAE ${mae}` : ""}`));
      legend.appendChild(li);
    });
    svg.innerHTML = out;
  }

  renderCompare();
  loadIndex();
})();
