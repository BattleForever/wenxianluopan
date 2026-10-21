/* 文献罗盘 前端（历史内部兼容标识 papernet 保留于文件名/下载名等技术位置） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || `HTTP ${resp.status}`);
  }
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

const state = {
  view: "graph",
  cy: null,
  graphNodes: [],
  collectionFilter: null, // 收藏集过滤
  buildRunning: false,
  detailTab: "similar",
  detailSort: "cited",
  clusterPalette: [
    "#4f8cff", "#37d0a0", "#ffb347", "#ff6b81", "#b088ff",
    "#4ecdc4", "#f9c74f", "#90be6d", "#f94144", "#577590",
    "#c77dff", "#43aa8b",
  ],
};

/* ================= 面板拖拽调宽 ================= */
function dragWidth(handle, apply, opts) {
  let startX = 0, startW = 0;
  handle.addEventListener("mousedown", (e) => {
    startX = e.clientX;
    startW = opts.getStart();
    document.body.classList.add("dragging");
    e.preventDefault();
    const move = (ev) => {
      const w = Math.max(opts.min, Math.min(opts.max, startW + (ev.clientX - startX) * opts.dir));
      apply(w);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove("dragging");
      if (opts.onDone) opts.onDone();
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

function initResizers() {
  const sidenav = $("#sidenav");
  const savedSide = parseInt(localStorage.getItem("pn_sidenav_w") || "0", 10);
  if (savedSide) sidenav.style.width = savedSide + "px";
  dragWidth($("#sidenav-resizer"),
    (w) => { sidenav.style.width = w + "px"; if (state.cy) cyResize(); },
    { getStart: () => sidenav.offsetWidth, min: 60, max: 280, dir: 1,
      onDone: () => localStorage.setItem("pn_sidenav_w", sidenav.offsetWidth) });

  const detail = $("#detail");
  const savedDetail = parseInt(localStorage.getItem("pn_detail_w") || "0", 10);
  if (savedDetail) detail.style.width = savedDetail + "px";
  dragWidth($("#detail-resizer"),
    (w) => { detail.style.width = w + "px"; cyResize(); },
    { getStart: () => detail.offsetWidth, min: 300, max: 760, dir: -1,
      onDone: () => localStorage.setItem("pn_detail_w", detail.offsetWidth) });
}

/* ================= 表格列宽拖拽 ================= */
function attachColResize(tableEl, storageKey) {
  const ths = tableEl.querySelectorAll("thead th");
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch (e) { saved = {}; }
  ths.forEach((th, i) => {
    if (saved[i]) th.style.width = saved[i] + "px";
    let handle = th.querySelector(".col-resizer");
    if (!handle) {
      handle = document.createElement("div");
      handle.className = "col-resizer";
      th.appendChild(handle);
    }
    handle.onmousedown = (e) => {
      e.preventDefault(); e.stopPropagation();
      const startX = e.clientX, startW = th.offsetWidth;
      document.body.classList.add("dragging");
      const move = (ev) => {
        th.style.width = Math.max(40, Math.min(900, startW + ev.clientX - startX)) + "px";
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        document.body.classList.remove("dragging");
        saved[i] = th.offsetWidth;
        localStorage.setItem(storageKey, JSON.stringify(saved));
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    };
  });
}

/* ================= 视图切换 ================= */
$$(".nav-btn").forEach((btn) =>
  btn.addEventListener("click", () => showView(btn.dataset.view))
);
function showView(view) {
  state.view = view;
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "graph") { loadGraph(); }
  if (view === "library") { loadLibrary(); loadCollectionOptions(); }
  if (view === "collections") { loadCollections(); }
  if (view === "import") { /* 静态卡片 */ }
  if (view === "database") { initDbView(); }
  if (view === "settings") { loadSettings(); }
}

/* ================= 健康状态 ================= */
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("#health").innerHTML =
      `<span class="dot ${h.online ? "" : "off"}">●</span> ` +
      `${h.online ? "在线" : "离线"} · 库内 ${h.library} · 候选 ${h.candidates} · 引用边 ${h.edges}`;
    window._dataDir = h.data_dir;
  } catch (e) {
    $("#health").textContent = "后端未连接";
  }
}

/* ================= 图谱 ================= */
function initCy() {
  state.cy = cytoscape({
    container: $("#cy"),
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(color)",
          label: "data(short)",
          color: "#c7d2df",
          "font-size": 9,
          "text-valign": "bottom",
          "text-margin-y": 4,
          "text-wrap": "ellipsis",
          "text-max-width": 110,
          width: "data(sz)",
          height: "data(sz)",
          "border-width": (n) => (n.data("status") === "library" ? 1.5 : 0.8),
          "border-style": (n) => (n.data("status") === "library" ? "solid" : "dashed"),
          "border-color": (n) => (n.data("status") === "library" ? "#9cc0ff" : "#5a6675"),
        },
      },
      {
        selector: "edge",
        style: {
          width: 0.8,
          "line-color": "#33415a",
          "target-arrow-color": "#33415a",
          "target-arrow-shape": "triangle",
          "arrow-scale": 0.7,
          "curve-style": "bezier",
        },
      },
      {
        selector: "node:selected",
        style: { "border-color": "#ffb347", "border-width": 3, color: "#fff" },
      },
    ],
    layout: { name: "grid" },
    wheelSensitivity: 0.25,
  });
  state.cy.on("tap", "node", (evt) => showDetail(evt.target.data("id")));
  state.cy.on("tap", (evt) => {
    if (evt.target === state.cy) hideDetail();
  });
}

function nodeColor(n, mode, clusterMax, yearMin, yearMax) {
  if (mode === "status") return n.status === "library" ? "#4f8cff" : "#6b7684";
  if (mode === "year" && n.year) {
    const lo = yearMin || 1990, hi = yearMax || new Date().getFullYear();
    const t = Math.max(0, Math.min(1, (n.year - lo) / Math.max(1, hi - lo)));
    const hue = 215 - 175 * t; // 蓝 → 绿 → 黄
    return `hsl(${hue}, 70%, 58%)`;
  }
  return state.clusterPalette[(n.cluster || 0) % state.clusterPalette.length];
}

async function loadGraph() {
  if (!state.cy) initCy();
  const incCand = $("#chk-candidates").checked ? 1 : 0;
  const ymin = $("#year-min").value, ymax = $("#year-max").value;
  let url = `/api/graph?include_candidates=${incCand}`;
  if (ymin) url += `&year_min=${ymin}`;
  if (ymax) url += `&year_max=${ymax}`;
  if (state.collectionFilter) url += `&collection_id=${state.collectionFilter}`;
  try {
    const g = await api(url);
    renderGraph(g);
    $("#graph-stats").textContent =
      `库内 ${g.stats.library} · 候选 ${g.stats.candidates} · 引用边 ${g.stats.edges}` +
      (state.collectionFilter ? "（收藏集过滤中）" : "");
    refreshHealth();
  } catch (e) {
    toast(`加载图谱失败：${e.message}`, true);
  }
}

function renderGraph(g) {
  const maxCited = Math.max(1, ...g.nodes.map((n) => n.cited_by_count || 0));
  const mode = $("#sel-color").value;
  const years = g.nodes.map((n) => n.year).filter(Boolean);
  state.graphNodes = g.nodes;
  const els = g.nodes.map((n) => ({
    group: "nodes",
    data: {
      id: String(n.id),
      short: (n.title || "").slice(0, 42),
      sz: 16 + Math.min(26, 9 * Math.sqrt((n.cited_by_count || 0) / maxCited) + 5),
      color: nodeColor(n, mode, 0, Math.min(...years, 1990), Math.max(...years, 2026)),
      ...n,
    },
  }));
  for (const e of g.edges) {
    els.push({ group: "edges", data: { id: `e${e.from}_${e.to}`, source: String(e.from), target: String(e.to) } });
  }
  state.cy.elements().remove();
  state.cy.add(els);
  runLayout();
}

function runLayout() {
  if (state.cy.elements().length === 0) return;
  state.cy.layout({
    name: "cose",
    animate: true,
    animationDuration: 400,
    nodeOverlap: 24,
    idealEdgeLength: 90,
    gravity: 0.6,
    numIter: 1200,
    randomize: true,
  }).run();
}

/* 构建网络：为库内文献逐篇抓取引用边 */
async function buildNetwork() {
  if (state.buildRunning) return;
  state.buildRunning = true;
  $("#btn-build").disabled = true;
  $("#build-progress").classList.remove("hidden");
  try {
    let ids;
    if (state.collectionFilter) {
      const g = await api(`/api/graph?collection_id=${state.collectionFilter}`);
      ids = g.nodes.filter((n) => n.status === "library").map((n) => n.id);
    } else {
      const lib = await api("/api/papers?status=library&limit=1000");
      // 优先抓从未抓过的，其次抓最久没抓的——避免每次都重复同一批高被引文献
      ids = lib.papers
        .slice()
        .sort((a, b) => (a.fetched_at || "").localeCompare(b.fetched_at || ""))
        .map((p) => p.id);
    }
    const batch = await api("/api/settings");
    const limitN = parseInt(batch.build_batch_size || "25", 10);
    ids = ids.slice(0, limitN);
    let done = 0, errs = 0;
    for (const id of ids) {
      try {
        await api(`/api/citations/fetch/${id}`, { method: "POST" });
      } catch (e) {
        errs++;
        console.warn("fetch fail", id, e.message);
      }
      done++;
      const pct = Math.round((done / ids.length) * 100);
      $("#build-bar").style.width = pct + "%";
      $("#build-text").textContent = `${done}/${ids.length}` + (errs ? ` · 失败 ${errs}` : "");
    }
    toast(`构建完成：处理 ${done} 篇` + (errs ? `，失败 ${errs}` : ""));
  } catch (e) {
    toast(`构建失败：${e.message}`, true);
  } finally {
    state.buildRunning = false;
    $("#btn-build").disabled = false;
    setTimeout(() => $("#build-progress").classList.add("hidden"), 1200);
    loadGraph();
  }
}

/* ---------- 详情面板 ---------- */
function cyResize() {
  if (state.cy) setTimeout(() => state.cy.resize(), 60);
}

function venueBadges(vi) {
  if (!vi) return [];
  const badges = [];
  if (vi.impact_factor) badges.push(`IF ${esc(vi.impact_factor)}`);
  if (vi.jcr_zone) badges.push(`JCR ${esc(vi.jcr_zone)}`);
  if (vi.cas_zone) badges.push(`中科院 ${esc(vi.cas_zone)}${vi.cas_top === "是" || vi.cas_top === "TOP" ? " · TOP" : ""}`);
  if (vi.cas_sub) badges.push(esc(vi.cas_sub));
  if (vi.ccf_tier) badges.push(esc(vi.ccf_tier));
  if (vi.kind) badges.push(esc(vi.kind === "conference" ? "会议" : "期刊"));
  return badges;
}

async function showDetail(id) {
  const panel = $("#detail");
  panel.classList.remove("hidden");
  cyResize();
  $("#detail-body").innerHTML = `<p class="muted">加载中…</p>`;
  let p;
  try {
    p = await api(`/api/papers/${id}`);
  } catch (e) {
    $("#detail-body").innerHTML = `<p class="muted">加载失败：${e.message}</p>`;
    return;
  }
  const authors = (p.authors || []).slice(0, 6).join(", ");
  const isLib = p.status === "library";
  $("#detail-body").innerHTML = `
    <div class="badges">
      <span class="badge ${isLib ? "lib" : ""}">${isLib ? "库内文献" : "候选文献"}</span>
      ${p.year ? `<span class="badge">${p.year}</span>` : ""}
      <span class="badge">被引 ${p.cited_by_count ?? 0}</span>
      ${p.edges_in ? `<span class="badge">图中被引 ${p.edges_in}</span>` : ""}
      ${p.edges_out ? `<span class="badge">图中引用 ${p.edges_out}</span>` : ""}
      ${p.doi ? `<span class="badge">DOI</span>` : ""}
    </div>
    <h2>${esc(p.title || "(无标题)")}</h2>
    ${p.title_zh ? `<h2 class="title-zh">${esc(p.title_zh)}</h2>` : ""}
    <div class="authors">${esc(authors)}</div>
    <div class="venue">${esc(p.venue || "")}</div>
    ${(function () {
      const badges = venueBadges(p.venue_info);
      return badges.length
        ? `<div class="badges" style="margin-top:6px">${badges.map((b) => `<span class="badge rank">${b}</span>`).join("")}</div>`
        : "";
    })()}
    <div class="abstract">${p.abstract ? esc(p.abstract.slice(0, 900)) : "（无英文摘要）"}</div>
    ${p.abstract_zh ? `<div class="abstract zh">${esc(p.abstract_zh.slice(0, 1200))}</div>` : ""}
    <div class="actions">
      ${p.local_pdf_path ? `<button onclick="openPdf(${p.id})">📄 打开 PDF</button>` : ""}
      ${!p.local_pdf_path && p.oa_pdf_url ? `<button onclick="downloadPdf(${p.id})">⬇ 下载 OA PDF</button>` : ""}
      ${p.doi ? `<button onclick="window.open('https://doi.org/${esc(p.doi)}','_blank')">🔗 DOI 页面</button>` : ""}
      <button onclick="toggleBilingualEdit(${p.id})">✏️ 补充双语信息</button>
      ${!isLib ? `<button class="primary" onclick="promote([${p.id}])">⬆ 加入文献库</button>` : ""}
      ${state.collectionFilter ? `<button onclick="addToCollection(${p.id})">🗂 加入收藏集</button>` : ""}
    </div>
    <div id="bilingual-edit" class="bilingual-edit hidden">
      <label>中文标题<input id="edit-title-zh" type="text"></label>
      <label>中文摘要<textarea id="edit-abstract-zh"></textarea></label>
      <label>英文标题<input id="edit-title" type="text"></label>
      <label>英文摘要<textarea id="edit-abstract"></textarea></label>
      <button class="primary" onclick="saveBilingualEdit(${p.id})">保存双语信息</button>
    </div>
    <div id="detail-tabs">
      <button data-tab="citing">引用它的人</button>
      <button data-tab="references">它的参考文献</button>
      <button data-tab="similar">相似推荐</button>
    </div>
    <div id="tab-body" class="paperlist"><p class="muted">…</p></div>
  `;
  $$("#detail-tabs button").forEach((b) =>
    b.addEventListener("click", () => loadDetailTab(p.id, b.dataset.tab))
  );
  loadDetailTab(p.id, state.detailTab);
}

async function loadDetailTab(id, tab) {
  state.detailTab = tab;
  $$("#detail-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  const body = $("#tab-body");
  body.innerHTML = `<p class="muted">加载中…</p>`;
  try {
    if (tab === "similar") {
      const r = await api(`/api/papers/${id}/similar?limit=25`);
      body.innerHTML = r.results.length
        ? r.results.map(renderListItem).join("")
        : `<p class="muted">暂无推荐——先「构建网络」抓取引用边，推荐基于共参考文献与共被引计算。</p>`;
    } else {
      const direction = tab === "citing" ? "citing" : "references";
      const r = await api(`/api/papers/${id}/linked?direction=${direction}&sort=${state.detailSort}&limit=300`);
      const options = [["cited","被引次数"],["year_desc","发表时间 新到旧"],["year_asc","发表时间 旧到新"],["impact_factor","影响因子 高到低"],["cas_zone","中科院分区 1到4区"]];
      body.innerHTML = `<label class="muted">排序 <select id="detail-sort">${options.map(([v,t])=>`<option value="${v}" ${state.detailSort===v?"selected":""}>${t}</option>`).join("")}</select></label><p class="muted">${esc(r.metric_note)} 共 ${r.total} 条</p>` +
        (r.papers.length ? r.papers.map(renderRefItem).join("") : `<p class="muted">暂无本地引用关系；可联网抓取或导入标准数据表。</p>`);
      $("#detail-sort").addEventListener("change",e=>{state.detailSort=e.target.value;loadDetailTab(id,tab);});
    }
    $$("#tab-body .item").forEach((el) =>
      el.addEventListener("click", () => {
        showDetail(el.dataset.id);
        focusInGraph(el.dataset.id);
      })
    );
  } catch (e) {
    body.innerHTML = `<p class="muted">加载失败：${e.message}</p>`;
  }
}

function renderRefItem(x) {
  const isLib = x.status === "library";
  const badges = venueBadges(x.venue_info)
    .map((b) => `<span class="badge rank" style="font-size:10px;padding:1px 7px">${b}</span>`)
    .join("");
  const zh = x.title_zh ? `<div class="ref-zh">${esc(x.title_zh)}</div>` : "";
  const vi = x.venue_info || {};
  const meta = [x.year, esc(x.venue || ""), vi.source ? `指标来源 ${esc(vi.source)}` : "", vi.updated_at ? `缓存更新 ${esc(vi.updated_at.slice(0,10))}` : ""].filter(Boolean).join(" · ");
  return `<div class="item ref-item" data-id="${x.id}">
    <span class="st ${isLib ? "lib" : ""}">${isLib ? "库内" : "候选"}</span>
    <div class="ref-body">
      <div class="t">${esc(x.title || "(无标题)")} <span class="y">${x.cited_by_count != null ? `· 被引${x.cited_by_count}` : ""}</span></div>
      ${zh}
      <div class="ref-meta">${meta}</div>
      ${badges ? `<div class="ref-badges">${badges}</div>` : ""}
    </div>
  </div>`;
}

function renderListItem(x) {
  const reason = x.shared_refs != null
    ? `<div class="y">共同参考文献 ${x.shared_refs} · 共同被引 ${x.shared_citers}${x.related_signal ? " · OpenAlex 相关" : ""}</div>`
    : "";
  return `<div class="item" data-id="${x.id}">
    <span class="st ${x.in_library || x.status === "library" ? "lib" : ""}">${x.in_library || x.status === "library" ? "库内" : "候选"}</span>
    <span class="t">${esc(x.title || "(无标题)")}<span class="y"> ${x.year || ""}${x.cited_by_count != null ? ` · 被引${x.cited_by_count}` : ""}</span>${reason}</span>
  </div>`;
}

function focusInGraph(id) {
  const node = state.cy.getElementById(String(id));
  if (node && node.nonempty()) {
    state.cy.elements().unselect();
    node.select();
    state.cy.animate({ center: { eles: node }, zoom: 1.2 }, { duration: 300 });
  }
}

function hideDetail() {
  $("#detail").classList.add("hidden");
  cyResize();
}
$("#detail-close").addEventListener("click", hideDetail);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideDetail();
});

window.openPdf = (id) => window.open(`/api/papers/${id}/pdf`, "_blank");

window.toggleBilingualEdit = (id) => {
  const box = $("#bilingual-edit");
  if (box.classList.contains("hidden")) {
    // 用当前详情数据预填（showDetail 缓存于面板数据集）
    api(`/api/papers/${id}`).then((p) => {
      $("#edit-title-zh").value = p.title_zh || "";
      $("#edit-abstract-zh").value = p.abstract_zh || "";
      $("#edit-title").value = p.title || "";
      $("#edit-abstract").value = p.abstract || "";
    });
    box.classList.remove("hidden");
  } else {
    box.classList.add("hidden");
  }
};

window.saveBilingualEdit = async (id) => {
  try {
    await api(`/api/papers/${id}/edit`, {
      method: "POST",
      body: JSON.stringify({
        title_zh: $("#edit-title-zh").value,
        abstract_zh: $("#edit-abstract-zh").value,
        title: $("#edit-title").value,
        abstract: $("#edit-abstract").value,
      }),
    });
    toast("双语信息已保存");
    showDetail(id);
  } catch (e) { toast(e.message, true); }
};
window.downloadPdf = async (id) => {
  try {
    const r = await api(`/api/papers/${id}/download-pdf`, { method: "POST" });
    toast(r.downloaded ? "PDF 已下载到本地 data/pdfs/" : "已有本地 PDF");
    showDetail(id);
  } catch (e) {
    toast(e.message, true);
  }
};
window.promote = async (ids) => {
  await api("/api/papers/promote", { method: "POST", body: JSON.stringify({ ids }) });
  toast(`已将 ${ids.length} 篇转入文献库`);
  showDetail(ids[0]);
  refreshHealth();
};
window.addToCollection = async (paperId) => {
  const cols = await api("/api/collections");
  if (!cols.collections.length) {
    toast("还没有收藏集，先到「收藏集」页创建一个", true);
    return;
  }
  const name = prompt("加入哪个收藏集？\n" + cols.collections.map((c) => `${c.id}. ${c.name} (${c.n})`).join("\n"));
  const col = cols.collections.find((c) => String(c.id) === name.trim() || c.name === name.trim());
  if (!col) return;
  await api(`/api/collections/${col.id}/items`, {
    method: "POST", body: JSON.stringify({ paper_ids: [paperId] }),
  });
  toast(`已加入「${col.name}」`);
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 图谱工具栏 ---------- */
$("#btn-build").addEventListener("click", buildNetwork);
$("#btn-relayout").addEventListener("click", runLayout);
$("#btn-fit").addEventListener("click", () => state.cy.fit(undefined, 40));
$("#chk-candidates").addEventListener("change", loadGraph);
$("#sel-color").addEventListener("change", () => loadGraph());
$("#year-min").addEventListener("change", loadGraph);
$("#year-max").addEventListener("change", loadGraph);
$("#btn-png").addEventListener("click", () => {
  const data = state.cy.png({ full: true, bg: "#0d1117", scale: 2 });
  const a = document.createElement("a");
  a.href = data;
  a.download = "papernet.png";
  a.click();
});
$("#btn-graphml").addEventListener("click", () =>
  window.open(`/api/export/graphml?include_candidates=${$("#chk-candidates").checked ? 1 : 0}`, "_blank")
);
$("#graph-search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const q = e.target.value.trim().toLowerCase();
  if (!q) return;
  const hit = state.graphNodes.find(
    (n) =>
      (n.title || "").toLowerCase().includes(q) ||
      (n.doi || "").toLowerCase().includes(q)
  );
  if (hit) { focusInGraph(hit.id); showDetail(hit.id); }
  else toast("图中没有匹配的节点", true);
});

/* ================= 文献库 ================= */
const libState = { limit: 200, offset: 0 };

async function loadLibrary() {
  const q = $("#lib-search").value.trim();
  const st = $("#lib-status").value;
  const data = await api(
    `/api/papers?limit=${libState.limit}&offset=${libState.offset}` +
    `${q ? `&query=${encodeURIComponent(q)}` : ""}${st ? `&status=${st}` : ""}`
  );
  // 删除等操作后当前页可能超出新总数，回退到最后一页重取
  if (data.total > 0 && libState.offset >= data.total) {
    libState.offset = Math.floor((data.total - 1) / libState.limit) * libState.limit;
    return loadLibrary();
  }
  const page = Math.floor(libState.offset / libState.limit) + 1;
  const pages = Math.max(1, Math.ceil(data.total / libState.limit));
  $("#lib-count").textContent = `第 ${page} / ${pages} 页 · 共 ${data.total} 条`;
  $("#lib-prev").disabled = libState.offset <= 0;
  $("#lib-next").disabled = libState.offset + libState.limit >= data.total;
  const tbody = $("#lib-table tbody");
  tbody.innerHTML = data.papers
    .map(
      (p) => `<tr data-id="${p.id}">
      <td><input type="checkbox" class="rowchk" data-id="${p.id}"></td>
      <td class="title-cell">${esc(p.title || "(无标题)")}</td>
      <td class="muted-cell">${esc((p.authors || [])[0] || "")}</td>
      <td>${p.year || ""}</td>
      <td class="muted-cell">${esc(p.venue || "")}</td>
      <td>${p.cited_by_count ?? 0}</td>
      <td><span class="st ${p.status === "library" ? "lib" : ""}">${p.status === "library" ? "库内" : "候选"}</span></td>
      <td>${p.local_pdf_path ? "📄" : p.oa_pdf_url ? "☁️" : ""}</td>
    </tr>`
    )
    .join("");
  attachColResize($("#lib-table"), "pn_cols_lib");
  $$("#lib-table tbody tr").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.classList.contains("rowchk")) return;
      showView("graph");
      showDetail(tr.dataset.id);
    })
  );
}
let libSearchTimer = null;
$("#lib-search").addEventListener("input", () => {
  clearTimeout(libSearchTimer);
  libSearchTimer = setTimeout(() => { libState.offset = 0; loadLibrary(); }, 300);
});
$("#lib-status").addEventListener("change", () => { libState.offset = 0; loadLibrary(); });
$("#lib-prev").addEventListener("click", () => {
  libState.offset = Math.max(0, libState.offset - libState.limit);
  loadLibrary();
});
$("#lib-next").addEventListener("click", () => {
  libState.offset += libState.limit;
  loadLibrary();
});
$("#lib-checkall").addEventListener("change", (e) =>
  $$(".rowchk").forEach((c) => (c.checked = e.target.checked))
);
function selectedIds() {
  return $$(".rowchk").filter((c) => c.checked).map((c) => parseInt(c.dataset.id, 10));
}
$("#btn-lib-fetch").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length) return toast("先勾选文献", true);
  let ok = 0, fail = 0;
  for (const id of ids) {
    try {
      await api(`/api/citations/fetch/${id}`, { method: "POST" });
      ok++;
    } catch { fail++; }
  }
  toast(`引用抓取完成：成功 ${ok}${fail ? `，失败 ${fail}` : ""}`);
  loadLibrary();
});
$("#btn-lib-enrich").addEventListener("click", async () => {
  const scope = $("#lib-status").value === "library" ? "library" : "candidates";
  toast("联网补全中…（每轮最多 100 篇）");
  try {
    const r = await api("/api/enrich/missing", {
      method: "POST",
      body: JSON.stringify({ scope, limit: 100 }),
    });
    toast(`补全完成：${r.enriched}/${r.targets} 篇。剩余可补 ${r.remaining} 篇` +
          (r.remaining ? "，再点一次继续" : ""));
    loadLibrary();
  } catch (e) { toast(e.message, true); }
});
$("#btn-promote").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length) return toast("先勾选候选文献", true);
  await api("/api/papers/promote", { method: "POST", body: JSON.stringify({ ids }) });
  toast("已转入库内");
  loadLibrary();
});
$("#btn-lib-del").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length) return toast("先勾选文献", true);
  if (!confirm(`从文献罗盘移除 ${ids.length} 条记录？\n（只删除本软件数据库里的记录，你的 PDF 和 Zotero 文件不受任何影响）`)) return;
  for (const id of ids) {
    await api(`/api/papers/${id}`, { method: "DELETE" });
  }
  toast("已删除记录（原文件未动）");
  loadLibrary();
});

async function loadCollectionOptions() {
  const data = await api("/api/collections");
  const sel = $("#lib-collect");
  sel.innerHTML =
    `<option value="">加入收藏集…</option>` +
    data.collections.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.n})</option>`).join("");
}
$("#lib-collect").addEventListener("change", async (e) => {
  const cid = e.target.value;
  if (!cid) return;
  const ids = selectedIds();
  if (!ids.length) { e.target.value = ""; return toast("先勾选文献", true); }
  await api(`/api/collections/${cid}/items`, { method: "POST", body: JSON.stringify({ paper_ids: ids }) });
  toast(`已把 ${ids.length} 篇加入收藏集`);
  e.target.value = "";
});

/* ================= 收藏集 ================= */
async function loadCollections() {
  const data = await api("/api/collections");
  $("#col-list").innerHTML = data.collections
    .map(
      (c) => `<div class="col-item">
        <span class="name">${esc(c.name)}</span><span class="n">${c.n} 篇</span>
        <span class="spacer"></span>
        <button onclick="openCollection(${c.id})">查看图谱</button>
        <button class="danger" onclick="delCollection(${c.id})">删除</button>
      </div>`
    )
    .join("") || `<p class="muted">还没有收藏集。</p>`;
}
$("#btn-col-create").addEventListener("click", async () => {
  const name = $("#col-name").value.trim();
  if (!name) return;
  await api("/api/collections", { method: "POST", body: JSON.stringify({ name }) });
  $("#col-name").value = "";
  loadCollections();
});
window.openCollection = (id) => {
  state.collectionFilter = id;
  showView("graph");
};
window.delCollection = async (id) => {
  if (!confirm("删除该收藏集？（不影响文献本身）")) return;
  await api(`/api/collections/${id}`, { method: "DELETE" });
  if (state.collectionFilter === id) state.collectionFilter = null;
  loadCollections();
};

/* ================= 导入 ================= */
function logResult(label, r) {
  const logEl = $("#import-log");
  logEl.classList.remove("hidden");
  logEl.textContent = `[${label}]\n` + JSON.stringify(r, null, 2) + "\n\n" + logEl.textContent;
}
$("#btn-imp-doi").addEventListener("click", async () => {
  try {
    const r = await api("/api/import/doi", { method: "POST", body: JSON.stringify({ text: $("#imp-doi").value }) });
    logResult("DOI 导入", r);
    toast(`DOI 导入完成：新增 ${r.added}，已存在 ${r.updated}，未匹配 ${r.not_found.length}`);
  } catch (e) { toast(e.message, true); }
});
$("#btn-imp-bib").addEventListener("click", async () => {
  try {
    const r = await api("/api/import/bibtex", { method: "POST", body: JSON.stringify({ text: $("#imp-bib").value }) });
    logResult("BibTeX 导入", r);
    toast(`BibTeX 导入完成：新增 ${r.added}，已存在 ${r.updated}`);
  } catch (e) { toast(e.message, true); }
});
$("#btn-imp-ris").addEventListener("click", async () => {
  try {
    const r = await api("/api/import/ris", { method: "POST", body: JSON.stringify({ text: $("#imp-ris").value }) });
    logResult("RIS 导入", r);
    toast(`RIS 导入完成：新增 ${r.added}，已存在 ${r.updated}`);
  } catch (e) { toast(e.message, true); }
});
$("#imp-bib-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) $("#imp-bib").value = await f.text();
});
$("#imp-ris-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) $("#imp-ris").value = await f.text();
});
$("#btn-imp-pdf").addEventListener("click", async () => {
  const path = $("#imp-pdf").value.trim();
  if (!path) return toast("输入 PDF 文件夹路径", true);
  toast("扫描中，请稍候…");
  try {
    const r = await api("/api/import/pdf-folder", { method: "POST", body: JSON.stringify({ path }) });
    logResult("PDF 扫描", r);
    toast(r.error || `扫描完成：匹配 ${r.matched}，仅标题 ${r.title_only}，失败 ${r.failed}`);
  } catch (e) { toast(e.message, true); }
});
$("#btn-zot-load").addEventListener("click", async () => {
  try {
    const r = await api("/api/zotero/collections");
    const sel = $("#zot-collection");
    sel.innerHTML =
      `<option value="">整个文库</option>` +
      r.collections
        .sort((a, b) => b.numItems - a.numItems)
        .map((c) => `<option value="${c.key}">${esc(c.name)} (${c.numItems})</option>`)
        .join("");
    toast(`已载入 ${r.collections.length} 个分类`);
  } catch (e) { toast(e.message, true); }
});
$("#btn-imp-zotero").addEventListener("click", async () => {
  toast("Zotero 同步中…");
  try {
    const ckey = $("#zot-collection").value || null;
    const r = await api("/api/zotero/sync", {
      method: "POST",
      body: JSON.stringify(ckey ? { collection_key: ckey } : {}),
    });
    logResult("Zotero 同步", r);
    const scope = ckey ? "（所选分类含子分类）" : "";
    toast(r.error || `同步完成${scope}：条目 ${r.total}，新增 ${r.added}，含 PDF ${r.with_pdf}，联网补全 ${r.enriched}`);
  } catch (e) { toast(e.message, true); }
});

$("#imp-std-papers-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) $("#imp-std-papers").value = await f.text();
});
$("#imp-std-citations-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) $("#imp-std-citations").value = await f.text();
});
$("#imp-std-venues-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) $("#imp-std-venues").value = await f.text();
});
$("#btn-imp-std").addEventListener("click", async () => {
  const papers = $("#imp-std-papers").value.trim();
  const cits = $("#imp-std-citations").value.trim();
  const venues = $("#imp-std-venues").value.trim();
  if (!papers && !cits && !venues) return toast("请先粘贴或选择 papers / citations / venues 文件内容", true);
  try {
    const r = await api("/api/import/standard", {
      method: "POST",
      body: JSON.stringify({ papers_text: papers, citations_text: cits, venues_text: venues }),
    });
    logResult("标准数据表导入", r);
    const vInfo = (r.venues_total != null)
      ? `，期刊/会议等级 ${r.venues_added} 新增 / ${r.venues_updated} 更新` : "";
    const unresolvedN = r.unresolved_total ?? r.unresolved.length;
    toast(`导入完成：新增 ${r.added}，已存在 ${r.updated}，新增引用边 ${r.edges_added}${vInfo}` +
          (unresolvedN ? `，未解析引用 ${unresolvedN} 条（详见日志）` : ""));
  } catch (e) { toast(e.message, true); }
});

/* ================= 数据库浏览 ================= */
const dbState = { table: "papers", limit: 50, offset: 0, columns: [], total: 0 };

async function initDbView() {
  const r = await api("/api/db/tables");
  $("#db-table").innerHTML = r.tables
    .map((t) => `<option value="${t.name}">${t.label || t.name}（${t.rows} 行）</option>`)
    .join("");
  $("#db-path").textContent = r.db_path;
  $("#db-info").textContent =
    `数据库文件共 ${r.tables.reduce((s, t) => s + t.rows, 0)} 行记录 · 数据目录：${r.data_dir}`;
  if (!r.tables.some((t) => t.name === dbState.table)) dbState.table = "papers";
  $("#db-table").value = dbState.table;
  loadDbRows();
}

async function loadDbRows() {
  const q = $("#db-search").value.trim();
  const r = await api(`/api/db/rows?table=${dbState.table}&limit=${dbState.limit}` +
    `&offset=${dbState.offset}${q ? `&q=${encodeURIComponent(q)}` : ""}`);
  dbState.columns = r.columns;
  dbState.total = r.total;
  $("#db-grid-head").innerHTML =
    "<th>#</th>" + r.columns.map((c) => `<th>${esc(c)}</th>`).join("");
  $("#db-grid-body").innerHTML = r.rows.map((row, i) =>
    `<tr><td class="muted">${dbState.offset + i + 1}</td>` +
    row.map((v) => {
      const s = v == null ? "" : String(v);
      const short = s.length > 60 ? s.slice(0, 60) + "…" : s;
      return `<td title="${esc(s.slice(0, 500))}">${esc(short) || "&nbsp;"}</td>`;
    }).join("") + "</tr>"
  ).join("") || `<tr><td colspan="${r.columns.length + 1}" class="muted">（无数据）</td></tr>`;
  const page = Math.floor(dbState.offset / dbState.limit) + 1;
  const pages = Math.max(1, Math.ceil(r.total / dbState.limit));
  $("#db-page").textContent = `第 ${page} / ${pages} 页 · 共 ${r.total} 行`;
  $("#db-prev").disabled = dbState.offset <= 0;
  $("#db-next").disabled = dbState.offset + dbState.limit >= r.total;
  attachColResize($("#db-grid"), "pn_cols_db_" + dbState.table);
}

$("#db-table").addEventListener("change", (e) => {
  dbState.table = e.target.value;
  dbState.offset = 0;
  loadDbRows();
});
let dbSearchTimer = null;
$("#db-search").addEventListener("input", () => {
  clearTimeout(dbSearchTimer);
  dbSearchTimer = setTimeout(() => { dbState.offset = 0; loadDbRows(); }, 300);
});
$("#btn-db-refresh").addEventListener("click", initDbView);
$("#db-prev").addEventListener("click", () => {
  dbState.offset = Math.max(0, dbState.offset - dbState.limit);
  loadDbRows();
});
$("#db-next").addEventListener("click", () => {
  dbState.offset += dbState.limit;
  loadDbRows();
});
$("#btn-db-exp-papers").addEventListener("click",
  () => window.open("/api/db/export/papers", "_blank"));
$("#btn-db-exp-citations").addEventListener("click",
  () => window.open("/api/db/export/citations", "_blank"));
$("#btn-db-exp-collections").addEventListener("click",
  () => window.open("/api/db/export/collections", "_blank"));
$("#btn-db-exp-venues").addEventListener("click",
  () => window.open("/api/db/export/venues", "_blank"));
$("#btn-db-clear-cache").addEventListener("click", async () => {
  if (!confirm("清空联网抓取缓存（fetch_cache）？\n清空后离线模式下之前查过的接口要重新联网获取；文献、引用边、期刊等级均不受影响。")) return;
  try {
    const r = await api("/api/db/clear-cache", { method: "POST" });
    toast(`已清空 ${r.cleared_rows} 条缓存，释放约 ${r.freed_mb} MB 空间`);
    initDbView();
  } catch (e) { toast(e.message, true); }
});
let venRefreshRunning = false;
$("#btn-ven-refresh").addEventListener("click", async () => {
  if (venRefreshRunning) return toast("上一轮更新还在进行中…", true);
  venRefreshRunning = true;
  try {
    const r = await api("/api/venues/refresh", {
      method: "POST",
      body: JSON.stringify({ limit: 100, stale_days: 180 }),
    });
    toast(`期刊等级更新：查到 ${r.updated}，无数据 ${r.not_found}，错误 ${r.errors}。` +
          `剩余待更新可再点继续（共 ${r.total_venues} 个期刊/会议）`);
    loadDbRows();
  } catch (e) { toast(e.message, true); }
  finally { venRefreshRunning = false; }
});

/* ================= 设置 ================= */
async function loadSettings() {
  const s = await api("/api/settings");
  $("#set-offline").checked = s.offline_mode === "1";
  $("#set-mailto").value = s.mailto || "";
  $("#set-cap").value = s.citedby_cap || 300;
  $("#set-candcap").value = s.candidate_cap || 300;
  $("#set-batch").value = s.build_batch_size || 25;
  $("#set-oc").checked = s.opencitations_enabled === "1";
  $("#set-zot").checked = s.zotero_enabled === "1";
  $("#set-zotdir").value = s.zotero_data_dir || "";
  $("#set-datainfo").textContent =
    `全部数据（数据库 + 下载的 PDF）保存在软件目录内：${window._dataDir || "papernet/data/"}。删除文献条目只影响本软件数据库，绝不动你的原文件。`;
}
$("#btn-settings-save").addEventListener("click", async () => {
  const values = {
    offline_mode: $("#set-offline").checked ? "1" : "0",
    mailto: $("#set-mailto").value.trim(),
    citedby_cap: $("#set-cap").value,
    candidate_cap: $("#set-candcap").value,
    build_batch_size: $("#set-batch").value,
    opencitations_enabled: $("#set-oc").checked ? "1" : "0",
    zotero_enabled: $("#set-zot").checked ? "1" : "0",
    zotero_data_dir: $("#set-zotdir").value.trim(),
  };
  await api("/api/settings", { method: "POST", body: JSON.stringify({ values }) });
  toast("设置已保存");
  refreshHealth();
});

/* ================= 启动 ================= */
initResizers();
refreshHealth();
loadGraph();
setInterval(refreshHealth, 30000);
