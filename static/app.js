// 全局错误捕获：任何未捕获的错误显示在页面顶部
window.addEventListener("error", (e) => {
  const d = document.getElementById("globalErr") || (() => {
    const el = document.createElement("div");
    el.id = "globalErr";
    el.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#a45045;color:#fff;padding:8px 16px;font-size:12px;font-family:sans-serif;white-space:pre-wrap";
    document.body ? document.body.prepend(el) : document.documentElement.prepend(el);
    return el;
  })();
  d.textContent += (d.textContent ? "\n" : "") + (e.message || "未知错误") + (e.filename ? " @ " + e.filename.split("/").pop() + ":" + e.lineno : "");
});
window.addEventListener("unhandledrejection", (e) => {
  const d = document.getElementById("globalErr") || (() => {
    const el = document.createElement("div");
    el.id = "globalErr";
    el.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#a45045;color:#fff;padding:8px 16px;font-size:12px;font-family:sans-serif;white-space:pre-wrap";
    document.body ? document.body.prepend(el) : document.documentElement.prepend(el);
    return el;
  })();
  d.textContent += (d.textContent ? "\n" : "") + "Promise: " + (e.reason?.message || e.reason || "未知");
});

const SAVED_VIEWS_KEY = "conversation-hub-v6-saved-views";
const DETAIL_WIDTH_KEY = "conversation-hub-detail-width";
const SOURCE_DETAILS_KEY = "conversation-hub-source-details-open";
const SOURCE_ORDER_KEY = "conversation-hub-source-order";
const SIDEBAR_COLLAPSED_KEY = "conversation-hub-sidebar-collapsed";
const THEME_KEY = "ai-hub-theme";
const READER_THEME_KEY = "conversation-hub-reader-theme";
const READER_TOC_KEY = "conversation-hub-reader-toc";
const MESSAGE_ORDER_KEY = "conversation-hub-message-order";
const READER_PRESETS = {
  inherit: { name: "跟随皮肤", bg: "", ink: "", font: "" },
  paper: { name: "米纸", bg: "#f6f1e6", ink: "#2a241c", font: "Georgia, 'Songti SC', 'SimSun', serif" },
  night: { name: "夜间", bg: "#16181d", ink: "#d8dde6", font: "'Segoe UI', 'Microsoft YaHei UI', sans-serif" },
  sepia: { name: "旧书", bg: "#ead7b4", ink: "#3d2a16", font: "Georgia, 'Songti SC', 'SimSun', serif" },
  green: { name: "护眼", bg: "#e4eed6", ink: "#243022", font: "'Segoe UI', 'Microsoft YaHei UI', sans-serif" },
  contrast: { name: "高对比", bg: "#ffffff", ink: "#111111", font: "'Segoe UI', 'Microsoft YaHei UI', sans-serif" },
};
const THEMES = {
  "dream-glass": { name: "梦境流光", mode: "dark" },
  "violet-night": { name: "紫夜星云", mode: "dark" },
  "warm-paper": { name: "温暖纸张", mode: "light" },
  terminal: { name: "终端黑客", mode: "dark" },
  archive: { name: "档案纸张", mode: "light" },
  qingci: { name: "青花瓷", mode: "light" },
  shuimo: { name: "水墨黑白", mode: "light" },
  senlin: { name: "松烟森林", mode: "dark" },
  luoxia: { name: "落霞暖橙", mode: "dark" },
};
const SOURCE_LABELS = {
  hermes: "Hermes",
  codex: "Codex",
  workbuddy: "WorkBuddy",
  claude: "Claude Code",
  cursor: "Cursor",
  qclaw: "QClaw",
  qoderwork: "QoderWork",
  zcode: "ZCode",
  codepilot: "CodePilot",
  marvis: "Marvis",
  qoder: "Qoder",
  qodercn: "QoderCN",
  qwenworkcn: "千问办公",
  grok: "Grok Build",
};
const EXTRA_SOURCES = ["claude", "cursor", "qclaw", "qoderwork", "zcode", "codepilot", "marvis", "qoder", "qodercn", "qwenworkcn", "grok"];
const VALID_SOURCES = new Set(["all", ...Object.keys(SOURCE_LABELS)]);
const VALID_RANGES = new Set(["all", "today", "3d", "7d", "30d"]);
const VALID_STATUSES = new Set(["all", "todo", "done", "reference", "archive_candidate"]);
const VALID_VIEWS = new Set(["find", "daily", "projects", "assets", "settings"]);
const customSourceIds = new Set();

function registerCustomSources(sources = {}) {
  const currentIds = new Set(
    Object.entries(sources).filter(([, item]) => item.custom).map(([source]) => source)
  );
  customSourceIds.forEach((source) => {
    if (currentIds.has(source)) return;
    customSourceIds.delete(source);
    VALID_SOURCES.delete(source);
    delete SOURCE_LABELS[source];
    delete state.filters[source];
  });
  currentIds.forEach((source) => {
    const item = sources[source];
    customSourceIds.add(source);
    VALID_SOURCES.add(source);
    SOURCE_LABELS[source] = item.label || "自定义 Agent";
    state.filters[source] ||= defaultFilters();
  });
  $("#customSourceRows").innerHTML = [...currentIds].map((source) => {
    const item = sources[source];
    return `<button class="source-row" data-source="${escapeHtml(source)}" type="button">
      <span class="source-dot ${escapeHtml(source)}"></span>
      <span>${escapeHtml(item.label || source)}</span>
      <b id="${escapeHtml(source)}Count">${item.conversations || 0}</b>
    </button>`;
  }).join("");
  const search = $("#searchAgentFilter");
  search.querySelectorAll("[data-custom-source-option]").forEach((node) => node.remove());
  [...currentIds].forEach((source) => {
    const option = document.createElement("option");
    option.value = source;
    option.dataset.customSourceOption = "1";
    option.textContent = SOURCE_LABELS[source];
    search.append(option);
  });
  if (!VALID_SOURCES.has(state.source)) {
    state.source = "all";
    Object.assign(state, state.filters.all);
  }
}

function syncSourceControls(sources = {}) {
  state.enabledSources = new Set(
    Object.entries(sources)
      .filter(([, item]) => item.enabled !== false)
      .map(([source]) => source)
  );
  document.querySelectorAll("#agentSwitcher .source-row[data-source]").forEach((row) => {
    const source = row.dataset.source;
    if (source === "all") return;
    const enabled = source in sources && sources[source]?.enabled !== false;
    // The switchboard lists active sources only; enable/disable belongs in Settings.
    if (!enabled) {
      row.style.display = "none";
      row.querySelector("[data-source-enabled]")?.remove();
      return;
    }
    row.style.display = "";
    row.querySelector("[data-source-enabled]")?.remove();
    row.classList.remove("source-disabled");
  });
  document.querySelectorAll("#searchAgentFilter option").forEach((option) => {
    if (option.value === "all") return;
    // 后端没有的源：隐藏筛选项
    if (!(option.value in sources)) { option.style.display = "none"; option.disabled = true; }
    else { option.style.display = ""; option.disabled = !state.enabledSources.has(option.value); }
  });
  if (state.source !== "all" && !state.enabledSources.has(state.source)) {
    state.source = "all";
    Object.assign(state, state.filters.all);
  }
}

function conversationSourceLabel(item) {
  const base = SOURCE_LABELS[item.source] || item.source;
  if (item.source === "workbuddy" && item.source_kind === "assistant") {
    return `${base} · 助理`;
  }
  if (item.source === "claude" && item.source_kind?.includes("metadata-only")) {
    return `${base} · 历史索引`;
  }
  return base;
}

function conversationKindLabel(item) {
  if (item.source === "workbuddy" && item.source_kind === "assistant") return "助理 / Claw";
  if (item.source === "claude" && item.source_kind?.includes("metadata-only")) return "正文不完整";
  if (item.source === "claude" && item.source_kind?.includes("partial")) return "部分正文";
  return "";
}

function localDateIso(value = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function shiftDate(day, amount) {
  const [year, month, value] = day.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, value + amount));
  return shifted.toISOString().slice(0, 10);
}

const defaultFilters = () => ({
  range: "all",
  status: "all",
  workspace: "all",
  nativeProject: "all",
  favorites: false,
  query: "",
  tag: "",
});

const state = {
  view: "find",
  source: "all",
  ...defaultFilters(),
  offset: 0,
  limit: 120,
  total: 0,
  selected: null,
  token: "",
  launchers: [],
  items: [],
  queryTerms: [],
  summary: null,
  enabledSources: new Set(),
  checked: new Map(),
  exportResult: null,
  backupImport: null,
  updateCandidate: null,
  dailyDate: localDateIso(),
  daily: null,
  dailyReportOpen: false,
  projects: [],
  openProjectId: null,
  projectForm: { mode: "create", id: null, addAfter: false },
  filters: {
    all: defaultFilters(),
    hermes: defaultFilters(),
    codex: defaultFilters(),
    workbuddy: defaultFilters(),
    claude: defaultFilters(),
    qoderwork: defaultFilters(),
    zcode: defaultFilters(),
  },
};

const $ = (selector) => document.querySelector(selector);
const list = $("#conversationList");
const detailPane = $("#detailPane");
let searchTimer = null;
let toastTimer = null;

function currentTheme() {
  const value = document.documentElement.dataset.theme || "archive";
  return THEMES[value] ? value : "archive";
}

function applyTheme(themeId, { persist = true } = {}) {
  const selected = THEMES[themeId] ? themeId : "archive";
  document.documentElement.dataset.theme = selected;
  document.documentElement.style.colorScheme = THEMES[selected].mode;
  if (persist) {
    try { localStorage.setItem(THEME_KEY, selected); } catch {}
  }
  const trigger = $("#themeButton");
  if (trigger) {
    trigger.title = `当前皮肤：${THEMES[selected].name}`;
    trigger.setAttribute("aria-label", `切换皮肤，当前为${THEMES[selected].name}`);
  }
  document.querySelectorAll("[data-theme-id]").forEach((button) => {
    const active = button.dataset.themeId === selected;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const stateRoot = $("#themeSelectionState");
  if (stateRoot) stateRoot.textContent = `当前：${THEMES[selected].name} · 已保存在本机`;
}

function setSidebarCollapsed(collapsed, { persist = true } = {}) {
  const value = Boolean(collapsed);
  document.body.classList.toggle("sidebar-collapsed", value);
  const button = $("#sidebarCollapseButton");
  if (button) {
    button.setAttribute("aria-expanded", String(!value));
    button.setAttribute("aria-label", value ? "展开侧边栏" : "收起侧边栏");
    button.title = value ? "展开侧边栏" : "收起侧边栏";
    button.textContent = value ? "›" : "‹";
  }
  if (persist) {
    try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, value ? "1" : "0"); } catch {}
  }
}

function initSidebarCollapse() {
  let saved = false;
  try { saved = localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1"; } catch {}
  if (window.matchMedia("(max-width: 840px)").matches) saved = false;
  setSidebarCollapsed(saved, { persist: false });
  $("#sidebarCollapseButton")?.addEventListener("click", () => {
    setSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
  });
  window.addEventListener("resize", () => {
    if (window.matchMedia("(max-width: 840px)").matches) {
      setSidebarCollapsed(false, { persist: false });
    }
  });
}

function openThemeDialog() {
  applyTheme(currentTheme(), { persist: false });
  $("#themeDialog").showModal();
}

function detailWidthBounds() {
  const layout = $(".find-layout");
  const max = Math.max(300, Math.min(720, (layout?.clientWidth || window.innerWidth) - 420));
  return { min: 300, max };
}

function setDetailWidth(value, { persist = false } = {}) {
  const bounds = detailWidthBounds();
  const width = Math.round(Math.max(bounds.min, Math.min(bounds.max, Number(value) || 400)));
  document.documentElement.style.setProperty("--detail", `${width}px`);
  $("#detailResizer").setAttribute("aria-valuenow", String(width));
  $("#detailResizer").setAttribute("aria-valuemax", String(bounds.max));
  if (persist) {
    try {
      localStorage.setItem(DETAIL_WIDTH_KEY, String(width));
    } catch {
      // Browser storage is optional; resizing still works for this session.
    }
  }
  return width;
}

function readerLookState() {
  try {
    const raw = JSON.parse(localStorage.getItem(READER_THEME_KEY) || "null");
    if (raw && typeof raw === "object") return raw;
  } catch {
    // localStorage may be blocked
  }
  return { preset: "inherit", bg: "#f6f1e6", ink: "#2a241c", font: "" };
}

function saveReaderLook(look) {
  try { localStorage.setItem(READER_THEME_KEY, JSON.stringify(look)); } catch {
    // Appearance still applies for this session.
  }
}

function applyReaderLook(look = readerLookState()) {
  const preset = READER_PRESETS[look.preset] || READER_PRESETS.inherit;
  const bg = look.preset === "custom" ? look.bg : (preset.bg || look.bg);
  const ink = look.preset === "custom" ? look.ink : (preset.ink || look.ink);
  const font = look.font || preset.font || "";
  const root = document.documentElement;
  if (look.preset === "inherit" || !bg || !ink) {
    root.style.removeProperty("--reader-bg");
    root.style.removeProperty("--reader-ink");
  } else {
    root.style.setProperty("--reader-bg", bg);
    root.style.setProperty("--reader-ink", ink);
  }
  if (font) root.style.setProperty("--reader-font", font);
  else root.style.removeProperty("--reader-font");
  root.dataset.readerLook = look.preset || "inherit";
  document.querySelectorAll("[data-reader-preset]").forEach((button) => {
    button.classList.toggle("active", button.dataset.readerPreset === look.preset);
  });
  const bgInput = document.querySelector(".reader-bg");
  const inkInput = document.querySelector(".reader-ink");
  const fontInput = document.querySelector(".reader-font");
  if (bgInput && (bg || look.bg)) bgInput.value = bg || look.bg || "#f6f1e6";
  if (inkInput && (ink || look.ink)) inkInput.value = ink || look.ink || "#2a241c";
  if (fontInput) fontInput.value = look.font || preset.font || "";
}

let readerOpenHook = null;
let readerJumpHandler = null;

function readerTocTip() {
  let tip = document.getElementById("readerTocTip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "readerTocTip";
    tip.className = "reader-toc-tip";
    tip.hidden = true;
    document.body.append(tip);
  }
  return tip;
}

function hideReaderTocTip() {
  const tip = document.getElementById("readerTocTip");
  if (tip) tip.hidden = true;
}

function showReaderTocTip(anchor, text) {
  const value = String(text || "").trim();
  if (!value) {
    hideReaderTocTip();
    return;
  }
  const tip = readerTocTip();
  tip.textContent = value;
  tip.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 24);
  let left = rect.right + 10;
  if (left + width > window.innerWidth - 12) left = Math.max(12, rect.left - width - 10);
  let top = rect.top;
  const height = Math.min(tip.scrollHeight + 8, 220);
  if (top + height > window.innerHeight - 12) top = Math.max(12, window.innerHeight - height - 12);
  tip.style.width = `${width}px`;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function readerTocOpen() {
  try { return localStorage.getItem(READER_TOC_KEY) !== "0"; } catch { return true; }
}

function messageOrderPreference() {
  try { return localStorage.getItem(MESSAGE_ORDER_KEY) === "newest" ? "newest" : "oldest"; } catch { return "oldest"; }
}

function placeReaderToc() {
  const toc = document.querySelector(".reader-toc");
  if (!toc) return;
  if (document.body.classList.contains("reader-open")) {
    document.body.append(toc);
    return;
  }
  const host = document.querySelector(".detail-inner");
  if (host && toc.parentElement !== host) host.prepend(toc);
}

function setReaderTocOpen(open) {
  const on = Boolean(open) && document.body.classList.contains("reader-open");
  document.body.classList.toggle("reader-toc-open", on);
  placeReaderToc();
  const toc = document.querySelector(".reader-toc");
  if (toc) toc.hidden = !document.body.classList.contains("reader-open");
  document.querySelectorAll(".reader-toc-toggle").forEach((button) => {
    button.setAttribute("aria-pressed", String(on));
    const count = button.dataset.count;
    button.textContent = on ? "收起目录" : (count ? `目录 · ${count}` : "目录");
  });
  try { localStorage.setItem(READER_TOC_KEY, on ? "1" : "0"); } catch { /* optional */ }
  if (!on) hideReaderTocTip();
}

function setReaderOpen(open) {
  const allowed = Boolean(open) && $(".find-layout")?.classList.contains("detail-open");
  document.body.classList.toggle("reader-open", allowed);
  document.querySelectorAll(".reader-toggle").forEach((button) => {
    button.setAttribute("aria-pressed", String(allowed));
    button.textContent = allowed ? "退出整页" : "整页阅读";
    button.title = allowed ? "回到侧栏阅读" : "整页阅读对话";
  });
  if (!allowed) {
    hideReaderTocTip();
    document.body.classList.remove("reader-toc-open");
    placeReaderToc();
    const parked = document.querySelector(".reader-toc");
    if (parked) parked.hidden = true;
  }
  if (allowed) {
    applyReaderLook();
    setReaderTocOpen(readerTocOpen());
    readerOpenHook?.();
  }
}

function setDetailOpen(open, { focusToggle = false } = {}) {
  const layout = $(".find-layout");
  const toggle = $("#detailToggleButton");
  const expanded = Boolean(open);
  layout.classList.toggle("detail-open", expanded);
  detailPane.hidden = !expanded;
  detailPane.setAttribute("aria-hidden", String(!expanded));
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.textContent = expanded ? "收起对话内容" : "打开对话内容";
  if (!expanded) setReaderOpen(false);
  if (focusToggle) toggle.focus({ preventScroll: true });
}

async function toggleDetailDrawer() {
  const isOpen = $(".find-layout").classList.contains("detail-open");
  if (isOpen) {
    setDetailOpen(false);
    return;
  }
  if (state.selected && (
    detailPane.dataset.source !== state.selected.source
    || detailPane.dataset.conversationId !== state.selected.id
  )) {
    await openDetail(state.selected.source, state.selected.id);
    return;
  }
  setDetailOpen(true);
}

function initDetailResizer() {
  const handle = $("#detailResizer");
  let saved = 400;
  let startX = 0;
  let startWidth = 400;
  let previewWidth = 400;
  let dragBounds = { min: 300, max: 720 };
  try {
    saved = Number(localStorage.getItem(DETAIL_WIDTH_KEY)) || 400;
  } catch {
    saved = 400;
  }
  setDetailWidth(saved);
  handle.addEventListener("pointerdown", (event) => {
    if (window.matchMedia("(max-width: 840px)").matches) return;
    startX = event.clientX;
    startWidth = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--detail")) || 400;
    previewWidth = startWidth;
    dragBounds = detailWidthBounds();
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing-detail");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    const requested = startWidth - (event.clientX - startX);
    previewWidth = Math.round(
      Math.max(dragBounds.min, Math.min(dragBounds.max, requested)) / 4
    ) * 4;
    document.documentElement.style.setProperty("--detail", `${previewWidth}px`);
    handle.setAttribute("aria-valuenow", String(previewWidth));
  });
  const finish = (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove("resizing-detail");
    setDetailWidth(previewWidth, { persist: true });
  };
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
  handle.addEventListener("dblclick", () => setDetailWidth(400, { persist: true }));
  handle.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
    event.preventDefault();
    const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--detail")) || 400;
    const next = event.key === "Home" ? 400 : current + (event.key === "ArrowLeft" ? 24 : -24);
    setDetailWidth(next, { persist: true });
  });
  window.addEventListener("resize", () => {
    if (!window.matchMedia("(max-width: 840px)").matches) {
      const current = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--detail")) || 400;
      setDetailWidth(current);
    }
  });
}

function initSourceDetails() {
  const details = $("#sourceDetails");
  try {
    const saved = localStorage.getItem(SOURCE_DETAILS_KEY);
    if (saved !== null) details.open = saved === "1";
  } catch {
    // Browser storage is optional; the section remains open by default.
  }
  details.addEventListener("toggle", () => {
    try {
      localStorage.setItem(SOURCE_DETAILS_KEY, details.open ? "1" : "0");
    } catch {
      // Keep the interaction available even when storage is disabled.
    }
  });
}

function builtinSourceRows() {
  return [...document.querySelectorAll("#agentSwitcher .source-row[data-source]")]
    .filter((row) => row.dataset.source !== "all");
}

function applySourceOrder(order) {
  if (!Array.isArray(order) || !order.length) return;
  const switcher = $("#agentSwitcher");
  const custom = $("#customSourceRows");
  const rows = builtinSourceRows();
  const rowById = Object.fromEntries(rows.map((row) => [row.dataset.source, row]));
  for (const id of order) if (rowById[id]) switcher.insertBefore(rowById[id], custom);
  for (const row of rows) if (!order.includes(row.dataset.source)) switcher.insertBefore(row, custom);

  const select = $("#searchAgentFilter");
  if (!select) return;
  const options = [...select.options].filter((opt) => opt.value !== "all");
  const optById = Object.fromEntries(options.map((opt) => [opt.value, opt]));
  select.innerHTML = "";
  const allOpt = document.createElement("option");
  allOpt.value = "all";
  allOpt.textContent = "全部 Agent";
  select.appendChild(allOpt);
  for (const id of order) if (optById[id]) select.appendChild(optById[id]);
  for (const opt of options) if (!order.includes(opt.value)) select.appendChild(opt);
}

function persistSourceOrder() {
  try {
    localStorage.setItem(SOURCE_ORDER_KEY, JSON.stringify(builtinSourceRows().map((r) => r.dataset.source)));
  } catch {
    // Ordering is a convenience; ignore storage failures.
  }
}

function initSourceDrag() {
  const switcher = $("#agentSwitcher");
  let dragged = null;
  for (const row of builtinSourceRows()) {
    row.draggable = true;
    row.title = "拖动可调整来源排序";
  }
  switcher.addEventListener("dragstart", (event) => {
    const row = event.target.closest(".source-row[data-source]");
    if (!row || row.dataset.source === "all") return;
    dragged = row;
    row.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    try { event.dataTransfer.setData("text/plain", row.dataset.source); } catch {}
  });
  switcher.addEventListener("dragover", (event) => {
    if (!dragged) return;
    const row = event.target.closest(".source-row[data-source]:not([data-source='all'])");
    if (!row || row === dragged) return;
    event.preventDefault();
    const rect = row.getBoundingClientRect();
    const after = event.clientY > rect.top + rect.height / 2;
    switcher.insertBefore(dragged, after ? row.nextSibling : row);
  });
  const settle = () => {
    if (dragged) { dragged.classList.remove("dragging"); persistSourceOrder(); }
    dragged = null;
  };
  switcher.addEventListener("drop", (event) => { if (dragged) { event.preventDefault(); settle(); } });
  switcher.addEventListener("dragend", settle);
  applySourceOrder(JSON.parse(localStorage.getItem(SOURCE_ORDER_KEY) || "null") || []);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const THINKING_TAG_RE = /<(thinking|think|thought|reasoning|redacted_thinking)>([\s\S]*?)<\/\1>/gi;
const SYSTEM_TAG_RE = /<system-reminder>([\s\S]*?)<\/system-reminder>/gi;
const USER_QUERY_RE = /<user_query>([\s\S]*?)<\/user_query>/gi;
const LONG_FENCE_RE = /```[^\n]*\n([\s\S]*?)```/g;
const PROCESS_PARA_RE = /^(?:the user\b|i(?:'m|'ll| am| will| need| should| can| think| see| have| want to)\b|let me\b|looking at\b|this (?:is|looks|seems)\b|we (?:need|should|can)\b|okay[,.]|alright[,.]|based on\b|from the (?:code|file|output|diff)\b|i'll\b|the count is\b|wait[,.]|hmm[,.]|我(?:需要|先|来|会|将|觉得|看)|让我|接下来我|首先(?:，|,)|根据.{0,16}(?:代码|文件|输出|结果))/i;

const PROGRESS_MARKERS = [
  "已成功杀掉", "没杀掉", "先杀进程", "等几秒", "换姿势", "换正确姿势",
  "换个更稳", "换终极", "启动命令已执行", "脚本写好了", "重新测试",
  "用管道喂", "先确认桌面路径", "开工！", "BOM 加上了", "路径短名没变化",
];

function isInternalNoiseMessage(message) {
  const text = String(message?.text || "").trim();
  const role = String(message?.role || "");
  const low = text.toLocaleLowerCase();
  if (!text) return true;
  if (role === "user") {
    return (
      text.startsWith("[System:")
      || text.startsWith("[CONTEXT COMPACTION")
      || text.startsWith("<system-reminder")
      || low.includes("your previous response was truncated")
    );
  }
  if (role !== "assistant") return false;
  if (text.length < 280 && (
    text.startsWith("内存满了")
    || text.includes("批量精简")
    || low.includes("memory consolidation failed")
    || low.includes("stop retrying memory calls")
    || low.includes("memory would be at")
  )) return true;
  return text.length < 140 && (text.includes("匹配到了两条") || text.includes("也有歧义") || text.includes("唯一匹配的压缩替换"));
}

function isProgressMonologue(message) {
  const text = String(message?.text || "").trim();
  if (String(message?.role || "") !== "assistant" || !text || text.length > 280) return false;
  if (text.includes("达令菁") || text.includes("🎉") || text.includes("\n##")) return false;
  if (PROGRESS_MARKERS.some((marker) => text.includes(marker))) return true;
  return /[：:]$/.test(text) && /验证|杀掉|启动|进程|窗口|脚本|测试|换/.test(text);
}

function messageVisibility(message) {
  if (message?.visibility === "visible" || message?.visibility === "system" || message?.visibility === "progress") {
    return message.visibility;
  }
  if (isInternalNoiseMessage(message)) return "system";
  if (isProgressMonologue(message)) return "progress";
  return "visible";
}

function messageRoleLabel(message) {
  const visibility = messageVisibility(message);
  if (visibility === "system") return "系统记账";
  if (visibility === "progress") return "过程独白";
  return message.role === "user" ? "你" : "助手";
}

function messageTurnKey(message) {
  const ts = String(Number(message.timestamp || 0)).replace(".", "_");
  const role = String(message.role || "x");
  const size = String(message.text || "").length;
  return `${role}-${ts}-${size}`;
}

function splitMessageBody(text, role) {
  const folded = [];
  let main = String(text || "");
  if (role === "user") {
    const queries = [...main.matchAll(USER_QUERY_RE)].map((match) => match[1].trim()).filter(Boolean);
    if (queries.length) {
      const remainder = main.replace(USER_QUERY_RE, "\n").replace(SYSTEM_TAG_RE, "\n").trim();
      if (remainder) folded.push({ kind: "system", title: "附加上下文", text: remainder });
      main = queries[queries.length - 1];
    }
  }
  main = main.replace(THINKING_TAG_RE, (_, __, body) => {
    if (body.trim()) folded.push({ kind: "thinking", title: "思考过程", text: body.trim() });
    return "\n";
  });
  main = main.replace(SYSTEM_TAG_RE, (_, body) => {
    if (body.trim()) folded.push({ kind: "system", title: "系统注入", text: body.trim() });
    return "\n";
  });
  main = main.replace(LONG_FENCE_RE, (block) => {
    const lines = block.split("\n").length;
    if (lines < 18) return block;
    folded.push({ kind: "dump", title: `长输出 · ${lines} 行`, text: block });
    return "\n";
  });
  main = main.replace(/\n{3,}/g, "\n\n").trim();
  if (role === "assistant" && main) {
    const parts = main.split(/\n{2,}/);
    let count = 0;
    while (count < parts.length - 1 && PROCESS_PARA_RE.test(parts[count].trim())) count += 1;
    if (count > 0) {
      folded.unshift({ kind: "thinking", title: "思考 / 过程", text: parts.slice(0, count).join("\n\n") });
      main = parts.slice(count).join("\n\n").trim();
    }
  }
  if (!main && folded.length) {
    const first = folded.shift();
    main = first.text;
  }
  return { main, folded };
}

function clampText(text, limit = 220) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  if (value.length <= limit) return { short: value, rest: "" };
  return { short: value.slice(0, limit).trimEnd() + "…", rest: String(text || "").trim() };
}

function isMarkdownTableSeparator(line) {
  return /^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(String(line || ""));
}

function isMarkdownTableRow(line) {
  const text = String(line || "").trim();
  if (!text.includes("|")) return false;
  return text.split("|").filter((cell) => cell.trim() !== "").length >= 2;
}

function splitMarkdownTableRow(line) {
  let text = String(line || "").trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|")) text = text.slice(0, -1);
  return text.split("|").map((cell) => cell.trim());
}

function splitFlattenedTableLine(line) {
  if (String(line || "").split("|").length < 6) return line;
  const start = line.indexOf("|");
  if (start < 0) return line;
  const prefix = line.slice(0, start).trimEnd();
  const pipeChunk = line.slice(start).trim();
  const rows = pipeChunk.split(/\|\s+(?=\|)/).map((row) => {
    let text = row.trim();
    if (!text.startsWith("|")) text = `|${text}`;
    if (!text.endsWith("|")) text = `${text}|`;
    return text;
  });
  if (rows.length < 3 || !rows.some(isMarkdownTableSeparator)) return line;
  return [prefix, rows.join("\n")].filter(Boolean).join("\n\n");
}

function restorePipeTables(text) {
  return String(text || "").replace(/\r\n/g, "\n").split(/(```[\s\S]*?```)/g).map((chunk) => {
    if (chunk.startsWith("```")) return chunk;
    return chunk.split("\n").map(splitFlattenedTableLine).join("\n");
  }).join("");
}

function parseMarkdownTable(lines, start) {
  if (!isMarkdownTableRow(lines[start]) || !isMarkdownTableSeparator(lines[start + 1] || "")) return null;
  const header = splitMarkdownTableRow(lines[start]);
  if (header.length < 2) return null;
  const rows = [];
  let index = start + 2;
  while (index < lines.length && isMarkdownTableRow(lines[index]) && !isMarkdownTableSeparator(lines[index])) {
    const cells = splitMarkdownTableRow(lines[index]);
    while (cells.length < header.length) cells.push("");
    rows.push(cells.slice(0, Math.max(header.length, cells.length)));
    index += 1;
  }
  if (!rows.length) return null;
  return { header, rows, end: index };
}

function inlineMarkdown(text, query) {
  const slots = [];
  const slot = (html) => {
    slots.push(html);
    return `\u0002${slots.length - 1}\u0003`;
  };
  let source = String(text ?? "");
  source = source.replace(/`{1,2}([^`]+)`{1,2}/g, (_, code) => slot(`<code>${highlightHtml(code, query)}</code>`));
  source = source.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, (_, label, href) => (
    slot(`<a class="md-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${highlightHtml(label, query)}</a>`)
  ));
  source = source.replace(/~~([^~]+)~~/g, (_, body) => slot(`<del>${highlightHtml(body, query)}</del>`));
  source = source.replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, (_, starred, underscored) => (
    slot(`<strong>${highlightHtml(starred || underscored, query)}</strong>`)
  ));
  source = source.replace(/\bhttps?:\/\/[^\s)<]+/g, (url) => {
    const clean = url.replace(/[.,;:!?，。；：！？]+$/, "");
    return slot(`<a class="md-link" href="${escapeHtml(clean)}" target="_blank" rel="noopener noreferrer">${highlightHtml(clean, query)}</a>`) + url.slice(clean.length);
  });
  return highlightHtml(source, query).replace(/\u0002(\d+)\u0003/g, (_, index) => slots[Number(index)] || "");
}

function renderRichText(text, query = "") {
  const restored = restorePipeTables(text);
  const lines = restored.split("\n");
  const blocks = [];
  const paragraph = [];
  let index = 0;

  const flushParagraph = () => {
    const body = paragraph.join(" ").replace(/\s+/g, " ").trim();
    paragraph.length = 0;
    if (body) blocks.push(`<p class="md-p">${inlineMarkdown(body, query)}</p>`);
  };

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      flushParagraph();
      const fence = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        fence.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(`<pre class="md-pre"><code>${highlightHtml(fence.join("\n"), query)}</code></pre>`);
      continue;
    }
    const table = parseMarkdownTable(lines, index);
    if (table) {
      flushParagraph();
      const head = table.header.map((cell) => `<th>${inlineMarkdown(cell, query)}</th>`).join("");
      const body = table.rows.map((row) => {
        const cells = table.header.map((_, cellIndex) => `<td>${inlineMarkdown(row[cellIndex] || "", query)}</td>`);
        return `<tr>${cells.join("")}</tr>`;
      }).join("");
      blocks.push(`<div class="md-table-wrap"><table class="md-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      index = table.end;
      continue;
    }
    if (!trimmed) {
      flushParagraph();
      index += 1;
      continue;
    }
    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      blocks.push(`<h${level + 2} class="md-h md-h${level}">${inlineMarkdown(heading[2], query)}</h${level + 2}>`);
      index += 1;
      continue;
    }
    if (/^(?:-{3,}|\*{3,})$/.test(trimmed)) {
      flushParagraph();
      blocks.push('<hr class="md-hr">');
      index += 1;
      continue;
    }
    if (/^>\s?/.test(trimmed)) {
      flushParagraph();
      const quotes = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) {
        quotes.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(`<blockquote class="md-quote">${inlineMarkdown(quotes.join("\n"), query)}</blockquote>`);
      continue;
    }
    if (/^[-*]\s+\S/.test(trimmed) || /^\d+\.\s+\S/.test(trimmed)) {
      flushParagraph();
      const ordered = /^\d+\.\s+/.test(trimmed);
      const items = [];
      while (index < lines.length) {
        const current = lines[index].trim();
        const task = !ordered && current.match(/^[-*]\s+\[([ xX])\]\s+(.+)$/);
        const item = ordered
          ? current.match(/^\d+\.\s+(.+)$/)
          : current.match(/^[-*]\s+(.+)$/);
        if (task) {
          const checked = task[1].toLowerCase() === "x";
          items.push(`<li class="md-task"><span class="md-check" aria-hidden="true">${checked ? "☑" : "☐"}</span>${inlineMarkdown(task[2], query)}</li>`);
        } else if (item) {
          items.push(`<li>${inlineMarkdown(item[1], query)}</li>`);
        } else {
          break;
        }
        index += 1;
      }
      const tag = ordered ? "ol" : "ul";
      blocks.push(`<${tag} class="md-list">${items.join("")}</${tag}>`);
      continue;
    }
    paragraph.push(line);
    index += 1;
  }
  flushParagraph();
  return blocks.join("") || `<p class="md-p">${inlineMarkdown(restored, query)}</p>`;
}

function summaryLead(text, limit = 110) {
  const original = String(text || "").trim();
  if (!original) return { short: "未提取到", rest: "" };
  const keep = [];
  for (const line of restorePipeTables(original).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("```")) {
      if (keep.length) break;
      continue;
    }
    if (isMarkdownTableRow(trimmed) || isMarkdownTableSeparator(trimmed)) {
      if (keep.length) break;
      continue;
    }
    keep.push(trimmed.replace(/^#{1,3}\s+/, ""));
    if (keep.join(" ").length >= limit || /[。！？!?：:]$/.test(keep[keep.length - 1])) break;
  }
  const short = (keep.join(" ").replace(/\s+/g, " ").trim() || clampText(original, limit).short);
  const clipped = short.length > limit ? `${short.slice(0, limit).trimEnd()}…` : short;
  const hasStructure = /\|.+\|/.test(original) || /^#{1,3}\s/m.test(original) || original.includes("```") || original.length > clipped.length + 8;
  return { short: clipped, rest: hasStructure ? original : "" };
}

function compactMarkdown(text, query = "", limit = 160) {
  const restored = restorePipeTables(String(text || ""));
  const keep = [];
  let hasTable = false;
  for (const line of restored.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("```")) continue;
    if (isMarkdownTableRow(trimmed) || isMarkdownTableSeparator(trimmed)) {
      hasTable = true;
      continue;
    }
    keep.push(trimmed.replace(/^#{1,3}\s+/, ""));
  }
  let joined = keep.join(" ").replace(/\s+/g, " ").trim() || String(text || "").replace(/\s+/g, " ").trim();
  joined = joined.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, "$1");
  joined = joined.replace(/\bhttps?:\/\/[^\s)<]+/g, "").replace(/\s+/g, " ").trim();
  if (joined.length > limit) joined = `${joined.slice(0, limit).trimEnd()}…`;
  if (hasTable) joined = joined ? `${joined} · 含表格` : "含对照表";
  return inlineMarkdown(joined, query);
}

function highlightHtml(value, query) {
  const raw = String(value ?? "");
  const needles = (Array.isArray(query) ? query : [query])
    .map((item) => String(item ?? "").trim())
    .filter(Boolean)
    .sort((left, right) => right.length - left.length);
  if (!needles.length) return escapeHtml(raw);
  const pattern = needles.map(escapeRegExp).join("|");
  const marked = raw.replace(new RegExp(pattern, "gi"), (match) => `\u0000${match}\u0001`);
  return escapeHtml(marked).replaceAll("\u0000", "<mark>").replaceAll("\u0001", "</mark>");
}

function dateTime(value) {
  if (!value) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value * 1000));
}

function dayLabel(day) {
  const today = localDateIso();
  if (day === today) return "今天";
  if (day === shiftDate(today, -1)) return "昨天";
  const [year, month, value] = day.split("-").map(Number);
  return `${month}月${value}日${year !== new Date().getFullYear() ? ` · ${year}` : ""}`;
}

function relativeTime(value) {
  const seconds = Math.max(0, Date.now() / 1000 - value);
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))} 分钟`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天`;
  return dateTime(value);
}

function statusLabel(value) {
  return {
    active: "活跃",
    week: "近七天",
    recent: "近期",
    archive: "可归档",
    history: "历史",
    todo: "待继续",
    done: "已完成",
    reference: "重要参考",
    archive_candidate: "归档候选",
  }[value] || value || "";
}

function rangeLabel(value) {
  return {
    today: "今天",
    "3d": "近 3 天",
    "7d": "近 7 天",
    "30d": "近 30 天",
    all: "全部",
  }[value] || value;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
}

async function api(path, options = {}) {
  const send = async () => fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json", "X-Hub-Token": state.token } : {}),
      ...(options.headers || {}),
    },
  });
  let response = await send();
  if (response.status === 403 && options.body) {
    // 令牌可能因服务重启而失效：刷新后重试一次
    try {
      const refreshed = await fetch("/api/token");
      if (refreshed.ok) state.token = (await refreshed.json()).token;
    } catch {}
    response = await send();
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}

function downloadText(filename, content, mime = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function sourceValues(group, fallback) {
  if (state.source === "all") return group;
  return fallback[state.source];
}

let lastRefreshedAt = 0;

async function loadSummary() {
  const data = await api("/api/summary");
  state.summary = data;
  lastRefreshedAt = data.refreshed_at || 0;
  $("#allCount").textContent = data.total;
  Object.keys(SOURCE_LABELS).forEach((source) => {
    const node = $(`#${source}Count`);
    if (node) node.textContent = data.by_source[source] || 0;
  });
  renderWorkspaceSummary();
}

function renderWorkspaceSummary() {
  const data = state.summary;
  if (!data) return;
  const sourceTotal = state.source === "all" ? data.total : data.by_source[state.source];
  const ranges = sourceValues(data.by_range, data.by_source_range);
  const favoriteTotal = state.source === "all" ? data.favorites : data.favorites_by_source[state.source];
  const stats = [
    {
      label: state.source === "all"
        ? "全部对话"
        : `${SOURCE_LABELS[state.source] || state.source} 对话`,
      value: sourceTotal,
      range: "all",
    },
    { label: "今天", value: ranges.today, range: "today" },
    { label: "近 3 天", value: ranges["3d"], range: "3d" },
    { label: "近 7 天", value: ranges["7d"], range: "7d" },
    { label: "近 30 天", value: ranges["30d"], range: "30d" },
    { label: "收藏", value: favoriteTotal, favorite: true },
  ];
  $("#summary").innerHTML = stats.map((stat) => {
    const active = stat.favorite ? state.favorites : (!state.favorites && state.range === stat.range);
    const action = stat.favorite ? `data-favorite="1"` : `data-range="${stat.range}"`;
    return `<button class="stat${active ? " active" : ""}" ${action} type="button">
      <strong>${stat.value}</strong><span>${stat.label}</span>
    </button>`;
  }).join("");

  $("#refreshedAt").textContent = `更新于 ${dateTime(data.refreshed_at)}`;
  const select = $("#workspaceFilter");
  const workspaceRows = state.source === "all" ? data.workspaces : data.workspaces_by_source[state.source];
  select.innerHTML = `<option value="all">全部工作区</option>` +
    workspaceRows.map(([name, count]) =>
      `<option value="${escapeHtml(name)}">${escapeHtml(name)} · ${count}</option>`
    ).join("");
  select.value = [...select.options].some((option) => option.value === state.workspace)
    ? state.workspace
    : "all";
  if (select.value !== state.workspace) {
    state.workspace = "all";
    state.filters[state.source].workspace = "all";
  }
  const nativeSelect = $("#nativeProjectFilter");
  const nativeRows = state.source === "all"
    ? (data.native_projects || [])
    : (data.native_projects_by_source?.[state.source] || []);
  nativeSelect.innerHTML = `<option value="all">全部原生项目</option>` +
    nativeRows.map(([name, count]) =>
      `<option value="${escapeHtml(name)}">${escapeHtml(name)} · ${count}</option>`
    ).join("");
  nativeSelect.value = [...nativeSelect.options].some(
    (option) => option.value === state.nativeProject
  ) ? state.nativeProject : "all";
  if (nativeSelect.value !== state.nativeProject) {
    state.nativeProject = "all";
    state.filters[state.source].nativeProject = "all";
  }
  const tagSelect = $("#tagFilter");
  if (tagSelect) {
    const tagRows = data.tags || [];
    tagSelect.innerHTML = `<option value="">全部标签</option>` +
      tagRows.map(([name, count]) =>
        `<option value="${escapeHtml(name)}">${escapeHtml(name)} · ${count}</option>`
      ).join("");
    tagSelect.value = [...tagSelect.options].some((option) => option.value === state.tag)
      ? state.tag
      : "";
    if (tagSelect.value !== state.tag) state.tag = "";
  }
  renderTagChips();

  document.querySelectorAll("#quickRanges [data-range]").forEach((button) => {
    const value = button.dataset.range;
    const count = value === "all" ? sourceTotal : ranges[value];
    const active = state.range === value;
    button.innerHTML = `<span>${escapeHtml(rangeLabel(value))}</span><b>${count}</b>`;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function renderTagChips() {
  const box = $("#tagChips");
  if (!box) return;
  const tagRows = (state.summary && state.summary.tags) || [];
  if (state.tag && !tagRows.some(([name]) => name === state.tag)) state.tag = "";
  const top = tagRows.slice(0, 8);
  if (state.tag && !top.some(([name]) => name === state.tag)) top.unshift([state.tag, null]);
  box.innerHTML = top.map(([name, count]) =>
    `<button class="tag-chip${state.tag === name ? " active" : ""}" type="button" data-tag="${escapeHtml(name)}">${escapeHtml(name)}${count != null ? `<b>${count}</b>` : ""}</button>`
  ).join("");
}

function queryString() {
  const params = new URLSearchParams({
    source: state.source,
    range: state.range,
    status: state.status,
    workspace: state.workspace,
    native_project: state.nativeProject,
    favorites: state.favorites ? "1" : "0",
    tag: state.tag,
    q: state.query,
    offset: String(state.offset),
    limit: String(state.limit),
  });
  return params.toString();
}

function syncUrl() {
  const params = new URLSearchParams();
  if (state.view !== "find") params.set("view", state.view);
  if (state.source !== "all") params.set("source", state.source);
  if (state.range !== "all") params.set("range", state.range);
  if (state.status !== "all") params.set("status", state.status);
  if (state.workspace !== "all") params.set("workspace", state.workspace);
  if (state.nativeProject !== "all") params.set("nativeProject", state.nativeProject);
  if (state.favorites) params.set("favorites", "1");
  if (state.tag) params.set("tag", state.tag);
  if (state.query) params.set("q", state.query);
  if (state.dailyDate !== localDateIso()) params.set("reviewDate", state.dailyDate);
  if (state.selectedProjectId) params.set("project", state.selectedProjectId);
  if (state.selected) {
    params.set("conversationSource", state.selected.source);
    params.set("conversation", state.selected.id);
  }
  const query = params.toString();
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}`);
}

function readUrlState() {
  const params = new URLSearchParams(location.search);
  const view = params.get("view");
  const source = params.get("source");
  const range = params.get("range");
  const status = params.get("status");
  if (VALID_VIEWS.has(view)) state.view = view;
  if (VALID_SOURCES.has(source)) state.source = source;
  if (VALID_RANGES.has(range)) state.range = range;
  if (VALID_STATUSES.has(status)) state.status = status;
  state.workspace = params.get("workspace") || "all";
  state.nativeProject = params.get("nativeProject") || "all";
  state.favorites = params.get("favorites") === "1";
  state.tag = params.get("tag") || "";
  state.query = (params.get("q") || "").trim();
  const reviewDate = params.get("reviewDate");
  if (/^\d{4}-\d{2}-\d{2}$/.test(reviewDate || "")) state.dailyDate = reviewDate;
  state.selectedProjectId = params.get("project") || "";
  const conversation = params.get("conversation");
  const conversationSource = params.get("conversationSource");
  if (conversation && VALID_SOURCES.has(conversationSource) && conversationSource !== "all") {
    state.selected = { source: conversationSource, id: conversation };
  }
  state.filters[state.source] = currentFilters();
}

function renderDailyDateStrip() {
  const today = localDateIso();
  const days = Array.from({ length: 7 }, (_, index) => shiftDate(today, index - 6));
  if (!days.includes(state.dailyDate)) days.unshift(state.dailyDate);
  $("#dailyDateStrip").innerHTML = days.map((day) => `
    <button class="daily-date${day === state.dailyDate ? " active" : ""}" data-day="${day}" type="button">
      <strong>${escapeHtml(dayLabel(day))}</strong>
      <span>${escapeHtml(day.slice(5))}</span>
    </button>
  `).join("");
}

function dailyItemHtml(item, tone = "") {
  const linked = item.source && item.conversation_id;
  const tag = linked ? "button" : "div";
  const attrs = linked
    ? `type="button" data-source="${escapeHtml(item.source)}" data-id="${escapeHtml(item.conversation_id)}"`
    : "";
  return `<${tag} class="daily-item ${tone}${linked ? " linked" : ""}" ${attrs}>
    ${linked ? `<span class="source-dot ${escapeHtml(item.source)}"></span>` : `<span class="daily-bullet">•</span>`}
    <span>
      <strong>${inlineMarkdown(item.text)}</strong>
      ${item.reason ? `<small class="daily-item-detail"><b>原因：</b>${inlineMarkdown(item.reason)}</small>` : ""}
      ${item.next_action ? `<small class="daily-item-detail"><b>后续：</b>${inlineMarkdown(item.next_action)}</small>` : ""}
    </span>
    ${linked ? `<small>查看原对话 ↗</small>` : ""}
  </${tag}>`;
}

function dailySectionHtml(title, items, tone = "", empty = "当天没有识别到相关事项") {
  return `<section class="daily-section ${tone}">
    <div class="daily-section-head"><h3>${title}</h3><span>${items.length}</span></div>
    <div class="daily-items">
      ${items.length ? items.map((item) => dailyItemHtml(item, tone)).join("") : `<p class="daily-empty">${empty}</p>`}
    </div>
  </section>`;
}

function readableParagraphsHtml(value) {
  return String(value || "")
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean)
    .map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`)
    .join("");
}

function joinedSummary(items, fallback) {
  const sentences = (items || []).map((item) => String(item.text || "").trim()).filter(Boolean);
  return sentences.length ? sentences.join(" ") : fallback;
}

function summaryEvidenceButton(item) {
  if (!item?.source || !item?.conversation_id) return "";
  return `<button class="summary-evidence-link" type="button"
    data-source="${escapeHtml(item.source)}" data-id="${escapeHtml(item.conversation_id)}">
    <span class="source-dot ${escapeHtml(item.source)}"></span>证据
  </button>`;
}

function summaryItemParts(item, tone) {
  const text = String(item?.text || "").trim();
  // 优先用对话真实标题（agent 自己起的），没有才从摘要句子解析
  const itemTitle = String(item?.title || "").trim();
  let title = itemTitle;
  let detail = "";
  let match;
  if (tone === "achievement") {
    match = text.match(/^围绕“(.+?)”.*?[：:](.+)$/);
    if (match) { if (!title) title = match[1]; detail = match[2]; }
    else detail = text;
  } else if (tone === "unfinished") {
    match = text.match(/^“(.+?)”目前还没有完成/);
    if (match && !title) title = match[1];
    detail = item.reason || "";
  } else if (tone === "decision") {
    match = text.match(/^关于“(.+?)”.*?[：:](.+)$/);
    if (match) { if (!title) title = match[1]; detail = match[2]; }
  } else if (tone === "next") {
    if (!title) title = "优先动作";
    detail = text;
  }
  if (!title) title = text;
  return { title, detail };
}

function summaryTreeItem(item, tone, project = false) {
  const { title, detail } = summaryItemParts(item, tone);
  const showNext = tone === "unfinished" && item.next_action;
  return `<li class="summary-tree-item ${tone}">
    <span class="summary-tree-branch" aria-hidden="true"></span>
    <div class="summary-tree-content">
      <div class="summary-tree-title">
        <strong>${inlineMarkdown(title)}</strong>
        ${summaryEvidenceButton(item, project)}
      </div>
      ${detail ? `<p>${inlineMarkdown(detail)}</p>` : ""}
      ${showNext ? `<div class="summary-child-node"><b>下一步</b><span>${inlineMarkdown(item.next_action)}</span></div>` : ""}
    </div>
  </li>`;
}

function summaryTreeGroup(title, items, tone, empty, project = false) {
  const values = items || [];
  return `<section class="summary-tree-group ${tone}">
    <header>
      <span class="summary-parent-node"></span>
      <h3>${escapeHtml(title)}</h3>
      <b>${values.length}</b>
    </header>
    ${values.length
      ? `<ul>${values.map((item) => summaryTreeItem(item, tone, project)).join("")}</ul>`
      : `<p class="summary-tree-empty">${escapeHtml(empty)}</p>`}
  </section>`;
}

function summaryOtherItems(summary) {
  const used = new Set(
    [
      ...(summary.main_focus || []),
      ...(summary.achievements || []),
      ...(summary.unfinished || []),
      ...(summary.decisions || []),
      ...(summary.first_step || []),
    ].map((item) => `${item.source}:${item.conversation_id}`)
  );
  return (summary.activities || []).filter(
    (item) => !used.has(`${item.source}:${item.conversation_id}`)
  ).slice(0, 8);
}

function summaryHierarchyHtml(summary, { project = false } = {}) {
  const focus = summary.main_focus?.[0];
  const firstStep = summary.first_step?.[0] || summary.next_actions?.[0];
  const lead = String(summary.narrative || summary.overview_sentence || summary.overview || "")
    .split(/\n\s*\n/)[0]
    .trim();
  const others = summaryOtherItems(summary);
  return `
    <article class="summary-priority">
      <div class="summary-priority-main">
        <span class="summary-priority-label">最重要</span>
        <h2>${inlineMarkdown(focus?.text || "今天没有识别到唯一主线")}</h2>
        ${lead && lead !== focus?.text ? `<p>${inlineMarkdown(lead)}</p>` : ""}
        ${summaryEvidenceButton(focus, project)}
      </div>
      <div class="summary-priority-next">
        <span>接下来先做</span>
        <strong>${inlineMarkdown(firstStep?.text || summary.next_step_summary || "核对今天的结果并确定下一步")}</strong>
        ${summaryEvidenceButton(firstStep, project)}
      </div>
    </article>
    <div class="summary-tree">
      ${summaryTreeGroup("已经完成", summary.achievements, "achievement", "今天没有识别到可以核验的完成成果。", project)}
      ${summaryTreeGroup("尚未完成", summary.unfinished || summary.ongoing, "unfinished", "目前没有明确遗留事项。", project)}
      ${summaryTreeGroup("关键决定", summary.decisions || [], "decision", "今天没有需要单独记录的关键决定。", project)}
    </div>
    <details class="summary-other">
      <summary><span>其他记录</span><b>${others.length}</b><small>展开查看次要事项</small></summary>
      ${others.length
        ? `<ul>${others.map((item) => summaryTreeItem(item, "other", project)).join("")}</ul>`
        : `<p>没有额外的次要记录。</p>`}
    </details>`;
}

// ---- 每日回顾：摘要卡（结构化摘要模板）+ 完整日报（日报模板） ----

function dailyStatusBadge(c) {
  if (c.user_status === "done") return `<span class="daily-status done">已完成</span>`;
  if (c.user_status === "todo") return `<span class="daily-status todo">待继续</span>`;
  if (c.user_status === "reference") return `<span class="daily-status ref">重要参考</span>`;
  return "";
}

function dailyConversationRow(c) {
  const latest = String(c.latest_user || "").trim();
  const latestLine = latest
    ? `<small class="daily-latest">你最近说：${compactMarkdown(latest, "", 90)}</small>`
    : "";
  const badge = dailyStatusBadge(c);
  return `<button class="daily-conversation" type="button" data-source="${escapeHtml(c.source)}" data-id="${escapeHtml(c.id)}">
    <span class="source-dot ${escapeHtml(c.source)}"></span>
    <span>
      <strong>${escapeHtml(c.title)}</strong>
      ${latestLine}
      <small>${escapeHtml(conversationSourceLabel(c))} · ${c.message_count} 条消息${badge ? " · " : ""}${badge}</small>
    </span>
    <time>${dateTime(c.updated_at)}</time>
  </button>`;
}

function dailyOverviewHtml(data) {
  const convs = data.conversations || [];
  const s = data.stats || {};
  if (!convs.length) {
    return `<section class="daily-card"><div class="daily-card-head">
      <span class="daily-card-label">今日概览</span>
      <h2>当天没有可回顾的对话</h2></div></section>`;
  }
  const focal = [...convs].sort((a, b) => b.message_count - a.message_count)[0];
  const agents = [...new Set(convs.map((c) => conversationSourceLabel(c)))];
  const perSource = Object.entries(s.by_source || {}).filter(([, n]) => n > 0)
    .map(([src, n]) => `<span><b>${n}</b> ${escapeHtml(conversationSourceLabel({ source: src }))}</span>`).join("");
  return `
    <section class="daily-card">
      <div class="daily-card-head">
        <span class="daily-card-label">今日概览</span>
        <h2>${convs.length} 个对话 · ${s.messages || 0} 条有效消息</h2>
        <p class="daily-card-overview">涉及 Agent：${agents.map(escapeHtml).join("、")}</p>
      </div>
      <div class="daily-card-next">
        <span>当天投入最多</span>
        <strong>${escapeHtml(focal.title)}</strong>
        ${summaryEvidenceButton({ source: focal.source, conversation_id: focal.id })}
      </div>
      <div class="daily-card-metrics">
        <span><b>${s.conversations || convs.length}</b> 对话</span>
        <span><b>${s.messages || 0}</b> 有效消息</span>
        ${perSource}
      </div>
    </section>
  `;
}

function dailyProgressHtml(data) {
  const convs = data.conversations || [];
  if (!convs.length) return "";
  const groups = new Map();
  for (const c of convs) {
    const key = c.workspace && c.workspace !== "无工作区" ? c.workspace : "未分组对话";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  return `
    <section class="daily-section daily-progress">
      <div class="daily-section-head"><h3>项目进展（按工作区分组）</h3><span>${groups.size} 组</span></div>
      ${[...groups.entries()].map(([name, items]) => `
        <div class="daily-group">
          <div class="daily-group-title">${escapeHtml(name)} <span class="muted">· ${items.length} 个对话</span></div>
          <div class="daily-conversation-list">${items.map(dailyConversationRow).join("")}</div>
        </div>`).join("")}
    </section>
  `;
}

function dailyMarksHtml(data) {
  const convs = data.conversations || [];
  const todo = convs.filter((c) => c.user_status === "todo");
  const done = convs.filter((c) => c.user_status === "done");
  const block = (title, items, empty) => `
    <div class="daily-mark-block">
      <div class="daily-section-head"><h3>${title}</h3><span>${items.length}</span></div>
      ${items.length
        ? `<div class="daily-conversation-list">${items.map(dailyConversationRow).join("")}</div>`
        : `<p class="daily-empty">${empty}</p>`}
    </div>`;
  return `
    <section class="daily-section daily-marks">
      <div class="daily-section-head"><h3>我的标记</h3><span>以你在对话详情里的状态为准</span></div>
      ${block("待继续", todo, "当天没有标记为「待继续」的对话。")}
      ${block("已完成", done, "当天没有标记为「已完成」的对话。")}
      <p class="muted daily-marks-tip">打开对话详情，在「我的管理信息」里设置状态后，会出现在这里。</p>
    </section>
  `;
}

function dailyManualNoteHtml(data) {
  return `
    <div class="daily-manual-note">
      <div>
        <strong>人工补充与修订</strong>
        <p class="muted">记录没有捕捉到的成果、状态和下一步。</p>
      </div>
      <textarea id="dailyManualNote" rows="3" placeholder="例如：项目已人工验收；下周继续处理数据迁移…">${escapeHtml(data.manual_note || "")}</textarea>
      <button id="saveDailyNoteButton" class="button secondary" type="button">保存补充</button>
    </div>
  `;
}

function renderDaily(data) {
  state.daily = data;
  const summary = data.summary;
  const html = `<div class="daily-head">
      <span class="muted">生成于 ${dateTime(data.generated_at)}${data.is_stale ? " · 对话有更新，可点右上角「刷新数据」重建" : ""}</span>
    </div>
    ${dailyOverviewHtml(data)}
    ${dailyProgressHtml(data)}
    ${dailyMarksHtml(data)}
    ${dailyManualNoteHtml(data)}
  `;
  const brief = $("#findDailyBrief");
  if (brief) {
    const focusEntry = summary.main_focus?.[0] || {};
    const achievements = summary.achievements || [];
    const unfinishedList = summary.unfinished || summary.ongoing || [];
    const focusKey = (focusEntry.source || "") + "/" + (focusEntry.conversation_id || focusEntry.id || "");
    // 所有事项平等并列：焦点 + 完成 + 待继续，去重同源（统一圆点，不区分符号/状态）
    const seenKeys = new Set();
    const items = [];
    const add = (entry, fallbackTitle) => {
      const key = (entry.source || "") + "/" + (entry.conversation_id || entry.id || "");
      if (seenKeys.has(key)) return;
      const parts = summaryItemParts(entry, "unfinished");
      const title = (parts.title && parts.title !== entry.text ? parts.title : fallbackTitle) || "（无标题）";
      items.push({
        title,
        source: entry.source,
        conversation_id: entry.conversation_id || entry.id,
        last_user: entry.last_user || "",
        last_reply: entry.last_reply || "",
      });
      seenKeys.add(key);
    };
    if (focusEntry.text) add(focusEntry, focusEntry.text);
    unfinishedList.slice(0, 3).forEach((it) => add(it, summaryItemParts(it, "unfinished").title || "待继续"));
    achievements.slice(0, 2).forEach((it) => add(it, summaryItemParts(it, "achievement").title || "已完成"));
    const totalItems = achievements.length + unfinishedList.length;
    const itemLi = (it) => {
      const hasMsg = !!(it.last_user || it.last_reply);
      const sourceLabel = SOURCE_LABELS[it.source] || it.source;
      return `<li class="brief-item"${hasMsg ? ' tabindex="0"' : ""}>
        <span class="brief-row">
          <span class="brief-dot" aria-hidden="true"></span>
          <span class="brief-title">${escapeHtml(it.title)}</span>
          <span class="brief-source src-${escapeHtml(it.source)}">${escapeHtml(sourceLabel)}</span>
          ${hasMsg ? '<span class="brief-toggle" aria-hidden="true">▾</span>' : ""}
          <button class="brief-jump" type="button" data-source="${escapeHtml(it.source)}" data-id="${escapeHtml(it.conversation_id)}" title="打开该对话" aria-label="打开该对话">↗</button>
        </span>
        ${hasMsg ? `<div class="brief-detail" hidden>${[
          it.last_user ? `<div class="brief-msg user"><b>你最近说</b><span>${escapeHtml(it.last_user)}</span></div>` : "",
          it.last_reply ? `<div class="brief-msg assistant"><b>最近回复</b><span>${escapeHtml(it.last_reply)}</span></div>` : "",
        ].join("")}</div>` : ""}
      </li>`;
    };
    const today = localDateIso();
    const canPrev = data.day > "2026-01-01";
    const canNext = data.day < today;
    // 本地日期加减（避免 toISOString 的时区坑）
    const shiftDay = (dayStr, delta) => {
      const [y, m, d] = dayStr.split("-").map(Number);
      const dt = new Date(y, m - 1, d + delta);
      return localDateIso(dt);
    };
    brief.innerHTML = `
      <div class="brief-label">
        <div class="brief-date-nav">
          <button class="brief-date-btn" type="button" data-brief-day-prev ${canPrev ? "" : "disabled"} aria-label="前一天">‹</button>
          <button class="brief-date-pick" type="button" data-brief-day-pick title="选择日期">${escapeHtml(dayLabel(data.day))}</button>
          <input class="brief-date-input" type="date" max="${today}" value="${data.day}" hidden>
          <button class="brief-date-btn" type="button" data-brief-day-next ${canNext ? "" : "disabled"} aria-label="后一天">›</button>
        </div>
        <strong>${data.day === today ? "今日要点" : "当日要点"}</strong>
      </div>
      <div class="brief-copy">
        <ul class="brief-points-list">${items.map(itemLi).join("")}</ul>
      </div>
      <div class="brief-actions">
        <span><b>${totalItems}</b> 件事</span>
        <button class="button secondary" type="button" data-open-daily>完整回顾</button>
      </div>
    `;
    // 日期切换：‹ 前一天 / › 后一天 / 点中间开日历
    brief.querySelector("[data-brief-day-prev]")?.addEventListener("click", () => setDailyDate(shiftDay(data.day, -1)));
    brief.querySelector("[data-brief-day-next]")?.addEventListener("click", () => setDailyDate(shiftDay(data.day, 1)));
    const pickBtn = brief.querySelector("[data-brief-day-pick]");
    const pickInput = brief.querySelector(".brief-date-input");
    pickBtn?.addEventListener("click", () => pickInput?.showPicker?.() || pickInput?.click());
    pickInput?.addEventListener("change", () => { if (pickInput.value && pickInput.value <= today) setDailyDate(pickInput.value); });
    // 展开/收起：直接用注入的最近消息，无需请求
    brief.querySelectorAll(".brief-item").forEach((li) => {
      const box = li.querySelector(".brief-detail");
      if (!box) return;
      const toggle = () => {
        const open = li.classList.toggle("expanded");
        box.hidden = !open;
      };
      li.addEventListener("click", toggle);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    });
    // 跳转按钮：切到找对话视图并打开该对话（阻止冒泡，不触发展开/收起）
    brief.querySelectorAll(".brief-jump").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        setView("find");
        await openDetail(btn.dataset.source, btn.dataset.id);
      });
    });
  }
  if (data.warning) showToast(data.warning);
  return html;
}

async function loadDaily() {
  const _t = (m) => { const el = document.getElementById("bootDebug"); if (el) el.textContent = `[daily] ${m}`; };
  _t("start");
  $("#reviewDate").value = state.dailyDate;
  $("#reviewDate").max = localDateIso();
  $("#nextDayButton").disabled = state.dailyDate >= localDateIso();
  renderDailyDateStrip();
  _t("before api");
  const data = await api(`/api/daily?date=${encodeURIComponent(state.dailyDate)}`);
  _t("api done");
  $("#dailyBody").innerHTML = renderDaily(data);
  _t("render done");
  syncUrl();
}

async function setDailyDate(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || day > localDateIso()) return;
  state.dailyDate = day;
  state.dailyReportOpen = false;
  await loadDaily();
}

function currentFilters() {
  return {
    range: state.range,
    status: state.status,
    workspace: state.workspace,
    nativeProject: state.nativeProject,
    favorites: state.favorites,
    query: state.query,
    tag: state.tag,
  };
}

function rememberCurrentFilters() {
  state.filters[state.source] = currentFilters();
}

function syncControls() {
  document.querySelectorAll("#agentSwitcher [data-source]").forEach((node) =>
    node.classList.toggle("active", node.dataset.source === state.source));
  $("#searchAgentFilter").value = state.source;
  document.body.dataset.agent = state.source;
  $("#statusFilter").value = state.status;
  $("#favoriteFilter").setAttribute("aria-pressed", String(state.favorites));
  $("#favoriteFilter").textContent = state.favorites ? "★ 只看收藏" : "☆ 只看收藏";
  $("#searchInput").value = state.query;
  $("#workspaceFilter").value = state.workspace;
  $("#nativeProjectFilter").value = state.nativeProject;
  const tagSelectEl = $("#tagFilter");
  if (tagSelectEl) {
    tagSelectEl.value = [...tagSelectEl.options].some((o) => o.value === state.tag) ? state.tag : "";
  }
  const moreFilters = $("#moreFilters");
  if (moreFilters) {
    const activeCount = (state.workspace !== "all" ? 1 : 0) + (state.nativeProject !== "all" ? 1 : 0);
    moreFilters.classList.toggle("has-value", activeCount > 0);
    moreFilters.querySelector("summary").textContent = activeCount ? `筛选 · ${activeCount}` : "筛选";
  }
  renderTagChips();
  renderWorkspaceHeading();
  renderWorkspaceSummary();
  renderSourceStarters();
}

function setView(view, { sync = true } = {}) {
  state.view = VALID_VIEWS.has(view) ? view : "find";
  document.querySelectorAll(".app-view").forEach((node) => {
    node.classList.toggle("active", node.id === `${state.view}View`);
  });
  document.querySelectorAll("#primaryNav [data-view], #sidebarUtility [data-view]").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === state.view);
  });
  document.body.dataset.view = state.view;
  const workspace = $(".app-workspace");
  workspace?.classList.toggle("page-scroll", state.view !== "find");
  syncSearchFloat();
  if (state.view === "daily" && !state.daily) {
    loadDaily().catch((error) => showToast(error.message));
  }
  if (state.view === "settings") {
    loadSourceHealth().catch((error) => showToast(error.message));
    loadVersionInfo().catch((error) => showToast(error.message));
  }
  if (state.view === "assets") {
    loadAssets().catch((error) => showToast(error.message));
  }
  if (state.view === "projects") {
    state.openProjectId = null;
    loadProjects().catch((error) => showToast(error.message));
  }
  if (sync) syncUrl();
}

async function loadSourceHealth() {
  const data = await api("/api/sources");
  const values = Object.values(data.sources);
  const healthy = values.filter((item) => item.status === "healthy").length;
  const warnings = values.filter((item) =>
    ["partial", "metadata_only"].includes(item.completeness) || ["error", "schema_changed"].includes(item.status)
  ).length;
  $("#sourceQualitySummary").textContent =
    `${healthy} 个适配器健康 · ${warnings} 个需关注 · 核心来源优先验收`;
  $("#sourceHealth").innerHTML = Object.entries(data.sources).map(([source, item]) => `
    <div class="health-row">
      <label class="health-toggle" title="停用后不再索引与显示该来源，可随时重新启用">
        <input type="checkbox" data-source-enabled="${source}" ${item.enabled ? "checked" : ""}>
        <span>启用</span>
      </label>
      <strong>${escapeHtml(item.label || SOURCE_LABELS[source] || source)}</strong>
      <span class="health-state ${item.status === "healthy" ? "ok" : "missing"}">${
        !item.enabled ? "未启用" : ({
          healthy: "兼容",
          schema_changed: "结构有变化",
          error: "读取异常",
          missing: "路径缺失",
        }[item.status] || (item.exists ? "待检查" : "路径缺失"))
      }</span>
      <code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code>
      <b>${item.conversations} 个对话${
        item.subsources?.assistant ? `（桌面 ${item.subsources.desktop || 0} · 助理 ${item.subsources.assistant}）` : ""
      } · ${item.message_count || 0} 条正文 · ${
        { full: "正文完整", partial: "部分正文", metadata_only: "仅元数据", waiting: "等待数据", disabled: "未启用" }[item.completeness] || "待评估"
      }${item.schema_fingerprint_short ? ` · 结构 ${escapeHtml(item.schema_fingerprint_short)}` : ""}${
        item.excluded ? ` · 已排除 ${item.excluded} 个子 Agent/后台线程` : ""}${
        item.error ? ` · ${escapeHtml(item.error)}` : ""
      }</b>
    </div>
  `).join("");
}

async function loadVersionInfo() {
  try {
    const health = await api("/api/health");
    const el = $("#updateState");
    if (el) el.textContent = `当前版本 ${health.app_version}`;
  } catch {}
}

async function previewBackupFile(file) {
  const text = await file.text();
  if (text.length > 10_000_000) throw new Error("备份文件超过 10 MB");
  const backup = JSON.parse(text);
  const preview = await api("/api/backup/preview", {
    method: "POST",
    body: JSON.stringify({ backup }),
  });
  state.backupImport = backup;
  $("#backupState").innerHTML = `
    <strong>${preview.rows} 行</strong> · 新增 ${preview.new} · 冲突 ${preview.conflicts}
    <div class="settings-card-actions">
      <button id="restoreBackupKeepButton" class="button ghost" type="button">保留本机并补充缺失</button>
      <button id="restoreBackupNewerButton" class="button secondary" type="button">以较新记录合并</button>
    </div>`;
  const restore = async (mode) => {
    const result = await api("/api/backup/restore", {
      method: "POST",
      body: JSON.stringify({ backup: state.backupImport, mode }),
    });
    $("#backupState").textContent = `恢复完成：新增 ${result.inserted}，更新 ${result.updated}`;
    await Promise.all([loadDaily()]);
  };
  $("#restoreBackupKeepButton").addEventListener("click", () =>
    restore("keep_existing").catch((error) => showToast(error.message))
  );
  $("#restoreBackupNewerButton").addEventListener("click", () =>
    restore("merge_newer").catch((error) => showToast(error.message))
  );
}

function renderSetupStatus(data, { fill = true } = {}) {
  registerCustomSources(data.sources || {});
  syncSourceControls(data.sources || {});
  const mapping = {
    hermes: ["#setupHermesPath", "Hermes"],
    codex: ["#setupCodexPath", "Codex"],
    workbuddy: ["#setupWorkbuddyPath", "WorkBuddy"],
  };
  if (fill) {
    Object.entries(mapping).forEach(([key, [selector]]) => {
      const node = $(selector);
      const value = data.sources?.[key]?.path || "";
      if (node && value) node.value = value;
    });
  }
  $("#setupExtraSources").innerHTML = EXTRA_SOURCES.map((source) => {
    const item = data.sources?.[source] || {};
    return `<label class="setup-extra-source${item.enabled ? " enabled" : ""}" data-extra-source="${source}">
      <input type="checkbox" data-extra-enabled="${source}" ${item.enabled ? "checked" : ""}>
      <span><strong>${escapeHtml(item.label || SOURCE_LABELS[source])}</strong>
        <small>${item.valid ? `${item.conversations || 0} 个候选 · ${escapeHtml(item.detail || "结构验证通过")}` : "未发现或结构不匹配"}</small>
      </span>
      <input type="text" data-extra-path="${source}" value="${escapeHtml(item.path || "")}"
        placeholder="自动发现或粘贴数据路径">
    </label>`;
  }).join("");
  const customEntries = Object.entries(data.sources || {}).filter(([, item]) => item.custom);
  $("#setupCustomSources").innerHTML = customEntries.map(([source, item]) =>
    customSourceRow({
      id: source,
      label: item.label,
      format: item.format,
      path: item.path,
      enabled: item.enabled,
      valid: item.valid,
      detail: item.detail,
      conversations: item.conversations,
    })
  ).join("");
  $("#setupSourceState").innerHTML = Object.entries(mapping).map(([key, [, label]]) => {
    const item = data.sources?.[key] || {};
    return `<div class="setup-source-row ${item.valid ? "ok" : "missing"}">
      <strong>${label}</strong>
      <span>${item.valid ? `有效 · ${item.conversations || 0} 个对话` : "未找到或结构不匹配"}</span>
    </div>`;
  }).join("");
  $("#setupDataDir").textContent = data.data_dir ? `管理信息将保存在：${data.data_dir}` : "";
  $("#setupDialog").dataset.required = String(Boolean(data.required));
  $("#closeSetupButton").hidden = Boolean(data.required);
}

function customSourceRow(item = {}) {
  const id = item.id || `custom_${Date.now().toString(36)}`;
  const format = item.format || "jsonl";
  const stateText = item.valid
    ? `结构验证通过 · ${item.conversations || 0} 个候选 · ${item.detail || ""}`
    : (item.detail || "填写名称、格式和路径后保存验证");
  return `<div class="setup-custom-source${item.enabled ? " enabled" : ""}"
      data-custom-source="${escapeHtml(id)}">
    <input type="checkbox" data-custom-enabled ${item.enabled ? "checked" : ""} aria-label="启用">
    <input type="text" data-custom-label value="${escapeHtml(item.label || "")}" placeholder="Agent 名称">
    <select data-custom-format aria-label="数据格式">
      <option value="jsonl" ${format === "jsonl" ? "selected" : ""}>JSONL 自动识别</option>
      <option value="markdown" ${format === "markdown" ? "selected" : ""}>Markdown 目录</option>
      <option value="sqlite" ${format === "sqlite" ? "selected" : ""}>SQLite 自动识别</option>
    </select>
    <input class="custom-path" type="text" data-custom-path value="${escapeHtml(item.path || "")}"
      placeholder="会话文件、数据库或目录路径">
    <button class="button ghost custom-remove" data-remove-custom type="button" title="移除">×</button>
    <small>${escapeHtml(stateText)}</small>
  </div>`;
}

async function loadSetupStatus({ openIfRequired = false, open = false } = {}) {
  const data = await api("/api/setup/status");
  renderSetupStatus(data);
  if ((open || (openIfRequired && data.required)) && !$("#setupDialog").open) {
    $("#setupDialog").showModal();
  }
  return data;
}

async function loadAssets() {
  const today = localDateIso();
  const exportDate = $("#exportDate");
  if (exportDate && !exportDate.value) exportDate.value = today;
  if (exportDate) exportDate.max = today;
}

function exportPayload() {
  const scope = $("#exportScope")?.value || "day";
  const payload = {
    format: $("#exportFormat").value,
    include_messages: $("#exportMessages").checked,
    include_notes: $("#exportNotes").checked,
    anonymize_paths: $("#exportAnonPaths")?.checked !== false,
  };
  if (scope === "selected") {
    const selected = [...state.checked.values()];
    if (!selected.length) throw new Error("请先在「找对话」里勾选要导出的对话（点击对话左侧方框）");
    payload.scope = "selected";
    payload.conversations = selected.map((it) => ({ source: it.source, id: it.id }));
  } else {
    payload.scope = "day";
    payload.day = $("#exportDate").value;
  }
  return payload;
}

// 导出范围切换：选"已选对话"时隐藏日期，选"按日期"时显示
$("#exportScope")?.addEventListener("change", (e) => {
  const dateLabel = $("#exportDateLabel");
  if (dateLabel) dateLabel.style.display = e.target.value === "selected" ? "none" : "";
  const count = state.checked.size;
  const hint = $("#exportState");
  if (e.target.value === "selected") {
    if (count) {
      hint.textContent = `已勾选 ${count} 个对话`;
    } else {
      hint.textContent = "尚未勾选，正在跳转到「找对话」…";
      setView("find");
      showToast("请勾选要导出的对话，然后点选择栏的「导出所选」");
    }
  } else {
    hint.textContent = "";
  }
});

async function previewExport() {
  const button = $("#previewExportButton");
  button.disabled = true;
  $("#exportState").textContent = "正在整理安全导出…";
  try {
    const result = await api("/api/export", {
      method: "POST",
      body: JSON.stringify(exportPayload()),
    });
    state.exportResult = result;
    $("#exportPreview").value = result.preview;
    $("#downloadExportButton").disabled = false;
    $("#exportState").textContent = `${result.conversation_count} 个对话 · ${(result.bytes / 1024).toFixed(1)} KB`;
  } finally {
    button.disabled = false;
  }
}

function fileCategoryLabel(value) {
  return {
    code: "代码", document: "文档", image: "图片", data: "数据",
    archive: "压缩包", other: "其他",
  }[value] || value;
}

function classificationQueryMatches(item) {
  const query = ($("#classificationSearch").value || "").trim().toLocaleLowerCase();
  return !query || `${item.title} ${item.workspace} ${item.preview} ${item.reason}`
    .toLocaleLowerCase().includes(query);
}

function renderClassificationList() {
  const visible = state.classificationItems.filter(classificationQueryMatches);
  $("#classificationList").innerHTML = visible.length ? visible.map((item) => `
    <label class="classification-row">
      <input type="checkbox" data-source="${escapeHtml(item.source)}" data-id="${escapeHtml(item.id)}">
      <span>
        <strong>${escapeHtml(item.title)}</strong>
        <small>${escapeHtml(item.workspace || "未命名工作区")} · ${escapeHtml(item.source)} · ${dateTime(item.updated_at)}</small>
        <em>${inlineMarkdown(item.reason)}${item.project_name ? ` · 当前：${escapeHtml(item.project_name)}` : ""}</em>
      </span>
      <b>${Math.round((item.confidence || 0) * 100)}%</b>
    </label>
  `).join("") : `<div class="asset-empty"><strong>没有匹配的对话</strong><p>可以切换“未归属”或“待确认”范围。</p></div>`;
  $("#classificationState").textContent = `共 ${state.classificationItems.length} 条，当前显示 ${visible.length} 条`;
}

function linesValue(values) {
  return (values || []).join("\n");
}

function renderWorkspaceHeading() {
  const labels = {
    all: ["UNIFIED OVERVIEW", "统一总览", "同时查看所有已启用 Agent，需要专注时切换到独立来源。"],
    hermes: ["HERMES WORKSPACE", "Hermes 工作区", "专门管理 Hermes 会话、续接链、上下文和历史记录。"],
    codex: ["CODEX WORKSPACE", "Codex 工作区", "专门管理 Codex 任务、项目执行记录和长期工作线程。"],
    workbuddy: ["WORKBUDDY WORKSPACE", "WorkBuddy 工作区", "集中查找 WorkBuddy 的本地任务、问答与整理记录。"],
  }[state.source] || [
    `${state.source.toUpperCase()} WORKSPACE`,
    `${SOURCE_LABELS[state.source] || state.source} 工作区`,
    `集中查找 ${SOURCE_LABELS[state.source] || state.source} 的本地主对话。`,
  ];
  $("#workspaceEyebrow").textContent = labels[0];
  $("#workspaceTitle").textContent = labels[1];
  $("#workspaceDescription").textContent = labels[2];
}

async function loadConversations({ append = false } = {}) {
  list.setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/conversations?${queryString()}`);
    state.total = data.total;
    state.queryTerms = data.query_terms || [];
    state.items = append ? [...state.items, ...data.items] : data.items;
    $("#searchInput").removeAttribute("aria-invalid");
    renderList();
    syncUrl();
  } finally {
    list.removeAttribute("aria-busy");
  }
}

function renderList() {
  $("#resultCount").textContent = `${state.total} 个结果`;
  if (!state.items.length) {
    list.innerHTML = `<div class="empty-detail"><div><h2>没有匹配结果</h2><p>换一个关键词或放宽时间范围。</p></div></div>`;
  } else {
    const groups = new Map();
    state.items.forEach((item) => {
      const day = localDateIso(new Date(item.updated_at * 1000));
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(item);
    });
    list.innerHTML = [...groups.entries()].map(([day, items]) => {
      const rows = items.map((item) => {
      const selected = state.selected?.source === item.source && state.selected?.id === item.id;
      const checked = state.checked.has(`${item.source}:${item.id}`);
      const tags = [
        item.favorite ? "★ 收藏" : "",
        item.user_status ? statusLabel(item.user_status) : statusLabel(item.status),
        conversationKindLabel(item),
        ...(item.tags || []).slice(0, 3),
      ].filter(Boolean);
      const match = item.match_snippet
        ? `<span class="conversation-match">${compactMarkdown(item.match_snippet, state.queryTerms, 180)}</span>`
        : "";
      return `
        <button class="conversation${selected ? " selected" : ""}${checked ? " checked" : ""}" type="button"
          data-source="${item.source}" data-id="${escapeHtml(item.id)}">
          <span class="check-mark" role="checkbox" aria-checked="${checked}" data-check="1">${checked ? "✓" : ""}</span>
          <span class="source-dot ${item.source}"></span>
          <span class="conversation-main">
            <span class="conversation-title">${inlineMarkdown(item.title, state.queryTerms)}</span>
            <span class="conversation-preview">${compactMarkdown(item.preview || "暂无预览", state.queryTerms, 180)}</span>
            ${match}
            <span class="chips">
              ${item.native_project
                ? `<span class="chip native-project">原生 · ${escapeHtml(item.native_project)}</span>`
                : `<span class="chip neutral">${escapeHtml(item.workspace)}</span>`}
              ${tags.map((tag) => `<span class="chip">${escapeHtml(tag)}</span>`).join("")}
            </span>
          </span>
          <span class="conversation-source">${escapeHtml(conversationSourceLabel(item))}</span>
          <span class="conversation-workspace" title="${escapeHtml(item.workspace)}">${escapeHtml(item.workspace)}</span>
          <span class="conversation-status">${escapeHtml(tags[0] || statusLabel(item.status))}</span>
          <span class="conversation-time">${relativeTime(item.updated_at)}</span>
        </button>`;
      }).join("");
      return `<section class="timeline-group">
        <header class="timeline-head"><h2>${escapeHtml(dayLabel(day))}</h2><span>${items.length} 个对话</span></header>
        ${rows}
      </section>`;
    }).join("");
  }
  $("#loadMoreButton").hidden = state.items.length >= state.total;
  updateGlobalSearchNav();
}

function syncSearchFloat() {
  const workspace = $(".app-workspace");
  if (!workspace) return;
  workspace.classList.toggle(
    "search-float",
    workspace.classList.contains("page-scroll") && workspace.scrollTop > 40
  );
}

function scrollChildInto(scroller, node, { offset = 24, align = "start" } = {}) {
  if (!node) return false;
  if (!scroller) {
    node.scrollIntoView({ block: align === "center" ? "center" : "start", behavior: "auto" });
    return true;
  }
  const extra = align === "center" ? scroller.clientHeight / 3 : offset;
  const top = node.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - extra;
  scroller.scrollTo({ top: Math.max(0, top), behavior: "auto" });
  return true;
}

function listedConversations() {
  return [...document.querySelectorAll("#conversationList .conversation")];
}

function updateGlobalSearchNav() {
  const buttons = listedConversations();
  const searching = Boolean(state.query.trim());
  const index = buttons.findIndex((button) => button.classList.contains("selected"));
  const stateNode = $("#searchNavState");
  const prev = $("#searchPrev");
  const next = $("#searchNext");
  const nav = document.querySelector(".global-search-nav");
  if (nav) nav.hidden = !searching;
  if (stateNode) {
    stateNode.hidden = !searching;
    stateNode.textContent = searching
      ? (buttons.length ? `${Math.max(1, index + 1)} / ${buttons.length}` : "没有匹配")
      : "";
  }
  if (prev) prev.disabled = !searching || buttons.length < 2;
  if (next) next.disabled = !searching || buttons.length < 2;
}

function listedConversationNode(source, id) {
  return document.querySelector(
    `#conversationList .conversation[data-source="${CSS.escape(String(source))}"][data-id="${CSS.escape(String(id))}"]`
  );
}

function revealListedConversation(source, id) {
  const next = listedConversationNode(source, id);
  if (!next) return null;
  scrollChildInto(document.querySelector(".workstream"), next, { align: "center" });
  next.classList.add("search-current");
  window.setTimeout(() => next.classList.remove("search-current"), 900);
  return next;
}

async function focusListedConversation(direction = 1, { keepSearchFocus = false } = {}) {
  if (!state.query.trim()) return;
  const buttons = listedConversations();
  if (!buttons.length) return;
  let index = buttons.findIndex((button) => button.classList.contains("selected"));
  if (index < 0) index = direction > 0 ? -1 : 0;
  index = (index + direction + buttons.length) % buttons.length;
  const button = buttons[index];
  const source = button.dataset.source;
  const id = button.dataset.id;
  setView("find");
  state.selected = { source, id };
  setDetailOpen(true);
  renderList();
  const current = revealListedConversation(source, id);
  if (current && !keepSearchFocus) current.focus({ preventScroll: true });
  updateGlobalSearchNav();
  await openDetail(source, id);
  const next = revealListedConversation(source, id);
  if (next && !keepSearchFocus) next.focus({ preventScroll: true });
  updateGlobalSearchNav();
}

function editorTags(root) {
  return [...root.querySelectorAll(".tags-chips .tag-edit-chip")].map((c) => c.dataset.tag);
}

function renderTagEditor(root, tags) {
  const box = root.querySelector(".tags-chips");
  if (!box) return;
  box.innerHTML = tags.map((t) =>
    `<span class="tag-edit-chip" data-tag="${escapeHtml(t)}">${escapeHtml(t)}<button type="button" class="tag-chip-x" data-remove-tag="${escapeHtml(t)}" title="删除标签">×</button></span>`
  ).join("");
}

function hideTagSuggest(root) {
  const box = root.querySelector(".tags-suggest");
  if (box) { box.hidden = true; box.innerHTML = ""; }
}

function showTagSuggest(root) {
  const input = root.querySelector(".tags-input");
  const box = root.querySelector(".tags-suggest");
  if (!input || !box) return;
  const q = input.value.trim().toLowerCase();
  const have = new Set(editorTags(root));
  const all = (state.summary && state.summary.tags) || [];
  const matches = all
    .filter(([name]) => !have.has(name) && (!q || name.toLowerCase().includes(q)))
    .slice(0, 8);
  if (!matches.length) { hideTagSuggest(root); return; }
  box.innerHTML = matches.map(([name, count]) =>
    `<button type="button" class="tags-suggest-item" data-suggest-tag="${escapeHtml(name)}">${escapeHtml(name)}<span class="muted">· ${count}</span></button>`
  ).join("");
  box.hidden = false;
}

async function openDetail(source, id) {
  state.selected = { source, id };
  setDetailOpen(true);
  renderList();
  syncUrl();
  detailPane.scrollTop = 0;
  detailPane.innerHTML = `<div class="empty-detail"><div><h2>读取上下文…</h2></div></div>`;
  try {
    const data = await api(`/api/conversation/${encodeURIComponent(source)}/${encodeURIComponent(id)}`);
    renderDetail(data);
  } catch (error) {
    detailPane.innerHTML = `<div class="empty-detail"><div><h2>读取失败</h2><p>${escapeHtml(error.message)}</p></div></div>`;
  }
}

function renderDetail(data) {
  const item = data.conversation;
  detailPane.dataset.source = item.source;
  detailPane.dataset.conversationId = item.id;
  const fragment = $("#detailTemplate").content.cloneNode(true);
  // replaceChildren 会清空 fragment，先拿到元素节点引用供后续保存使用
  const detailRoot = fragment.querySelector(".detail-inner");
  fragment.querySelector(".source-line").textContent = [
    conversationSourceLabel(item),
    item.native_project ? `原生项目：${item.native_project}` : item.workspace,
  ].filter(Boolean).join(" · ");
  const titleNode = fragment.querySelector(".detail-title");
  titleNode.textContent = item.title;
  titleNode.title = item.title;
  const statusLabels = {
    todo: "待继续",
    done: "已完成",
    reference: "重要参考",
    archive_candidate: "可归档",
  };
  fragment.querySelector(".detail-meta").textContent = [
    dateTime(item.updated_at),
    item.model,
    statusLabels[item.user_status] || "",
  ].filter(Boolean).join(" · ");
  const favoriteButton = fragment.querySelector(".favorite-button");
  favoriteButton.textContent = item.favorite ? "★" : "☆";
  favoriteButton.classList.toggle("active", item.favorite);
  fragment.querySelector(".detail-close-button").addEventListener("click", () => {
    setDetailOpen(false, { focusToggle: true });
  });
  const readerToggle = fragment.querySelector(".reader-toggle");
  if (readerToggle) {
    readerToggle.addEventListener("click", () => {
      setReaderOpen(!document.body.classList.contains("reader-open"));
    });
    setReaderOpen(document.body.classList.contains("reader-open"));
  }
  readerOpenHook = () => {
    loadFullConversationMessages().catch((error) => showToast(error.message));
  };
  const tocToggle = fragment.querySelector(".reader-toc-toggle");
  const tocList = fragment.querySelector(".reader-toc-list");
  if (tocToggle) {
    tocToggle.addEventListener("click", () => setReaderTocOpen(!document.body.classList.contains("reader-toc-open")));
  }
  if (tocList) {
    tocList.addEventListener("mouseover", (event) => {
      const button = event.target.closest(".reader-toc-item");
      if (!button) return;
      showReaderTocTip(button, tocPreviews.get(button.dataset.turnKey) || "");
    });
    tocList.addEventListener("mouseleave", hideReaderTocTip);
  }
  const lookRoot = fragment.querySelector(".reader-look");
  if (lookRoot) {
    applyReaderLook();
    lookRoot.querySelectorAll("[data-reader-preset]").forEach((button) => {
      button.addEventListener("click", () => {
        const preset = button.dataset.readerPreset;
        const next = { ...readerLookState(), preset };
        if (READER_PRESETS[preset]?.bg) {
          next.bg = READER_PRESETS[preset].bg;
          next.ink = READER_PRESETS[preset].ink;
        }
        if (READER_PRESETS[preset]?.font) next.font = READER_PRESETS[preset].font;
        if (preset === "inherit") next.font = "";
        saveReaderLook(next);
        applyReaderLook(next);
      });
    });
    lookRoot.querySelector(".reader-bg")?.addEventListener("input", (event) => {
      const next = { ...readerLookState(), preset: "custom", bg: event.target.value };
      saveReaderLook(next);
      applyReaderLook(next);
    });
    lookRoot.querySelector(".reader-ink")?.addEventListener("input", (event) => {
      const next = { ...readerLookState(), preset: "custom", ink: event.target.value };
      saveReaderLook(next);
      applyReaderLook(next);
    });
    lookRoot.querySelector(".reader-font")?.addEventListener("change", (event) => {
      const next = { ...readerLookState(), font: event.target.value };
      saveReaderLook(next);
      applyReaderLook(next);
    });
  }

  const launchTargetsRoot = fragment.querySelector(".launch-targets");
  const launchNote = fragment.querySelector(".launch-note");
  const launchTargets = data.launch_targets || [];
  if (!launchTargets.length) {
    launchTargetsRoot.hidden = true;
    launchNote.textContent = "该来源暂未提供安全、可验证的续接方式";
  } else {
    launchTargets.forEach((target, index) => {
      const isLink = ["deep_link", "app_link"].includes(target.kind);
      const control = document.createElement(isLink ? "a" : "button");
      control.className = `button ${index === 0 ? "primary" : "secondary"}`;
      control.textContent = target.label;
      control.title = target.note || target.label;
      if (isLink) {
        control.href = target.href;
      } else {
        control.type = "button";
      }
      if (target.kind === "copy_command") {
        control.addEventListener("click", async () => {
          await navigator.clipboard.writeText(target.value);
          showToast("恢复命令已复制");
        });
      }
      if (target.kind === "server_launch") {
        control.addEventListener("click", async () => {
          control.disabled = true;
          try {
            await api("/api/launch", {
              method: "POST",
              body: JSON.stringify({
                source: item.source,
                conversation_id: item.id,
                target_id: target.target_id,
              }),
            });
            showToast(target.exact ? `已打开：${target.label}` : `已交给本机打开：${target.label}`);
          } catch (error) {
            showToast(error.message);
          } finally {
            control.disabled = false;
          }
        });
      }
      launchTargetsRoot.append(control);
    });
    const target = launchTargets[0];
    const capabilityLabels = {
      session: "精确到会话",
      command: "精确恢复命令",
      workspace: "仅工作区",
      client: "仅打开客户端",
      none: "无法续接",
    };
    const capability = capabilityLabels[target.capability]
      || (target.exact ? "精确到会话" : target.kind === "server_launch" ? "仅工作区" : "仅打开客户端");
    launchNote.textContent = target.capability === "none" && target.note
      ? target.note
      : `${capability}${target.note ? ` · ${target.note}` : ""}`;
    launchNote.classList.toggle("exact", !!target.exact);
  }

  const generateReview = fragment.querySelector(".generate-review");
  const reviewState = fragment.querySelector(".review-state");
  const reviewPreview = fragment.querySelector(".review-preview");
  const reviewOutput = fragment.querySelector(".review-output");
  const copyReview = fragment.querySelector(".copy-review");
  const downloadReview = fragment.querySelector(".download-review");
  let reviewResult = null;
  generateReview.addEventListener("click", async () => {
    generateReview.disabled = true;
    reviewState.textContent = "生成中…";
    try {
      reviewResult = await api(
        `/api/review/${encodeURIComponent(item.source)}/${encodeURIComponent(item.id)}`
      );
      reviewOutput.value = reviewResult.markdown || "";
      reviewPreview.hidden = false;
      copyReview.hidden = false;
      downloadReview.hidden = false;
      const fingerprint = reviewResult.review?.content_sha256?.slice(0, 10) || "";
      reviewState.textContent = `已生成 · ${fingerprint}`;
    } catch (error) {
      reviewState.textContent = error.message;
    } finally {
      generateReview.disabled = false;
    }
  });
  copyReview.addEventListener("click", async () => {
    if (!reviewResult) return;
    await navigator.clipboard.writeText(reviewResult.markdown || "");
    showToast("回顾卡 Markdown 已复制");
  });
  downloadReview.addEventListener("click", () => {
    if (!reviewResult) return;
    const filename = `${item.source}-${item.id}-review.json`.replace(/[<>:"/\\|?*]/g, "_");
    downloadText(filename, JSON.stringify(reviewResult.review, null, 2), "application/json;charset=utf-8");
  });

  const memoryInput = fragment.querySelector(".memory-card-input");
  const includeMemory = fragment.querySelector(".include-memory");
  const memoryState = fragment.querySelector(".memory-save-state");
  const continuationState = fragment.querySelector(".continuation-state");
  const continuationPreview = fragment.querySelector(".continuation-preview");
  const continuationOutput = fragment.querySelector(".continuation-output");
  const copyContinuation = fragment.querySelector(".copy-continuation");
  const downloadContinuation = fragment.querySelector(".download-continuation");
  const saveMemoryButton = fragment.querySelector(".save-memory-card");
  const generateContinuation = fragment.querySelector(".generate-continuation");
  const savedMemory = data.continuation_memory || { body: "", updated_at: 0 };
  let memorySavedBody = savedMemory.body || "";
  let memoryUpdatedAt = Number(savedMemory.updated_at || 0);
  let continuationResult = null;
  memoryInput.value = memorySavedBody;
  memoryState.textContent = memorySavedBody ? "已保存在本机" : "尚未保存记忆卡";
  memoryInput.addEventListener("input", () => {
    memoryState.textContent = memoryInput.value.trim() === memorySavedBody
      ? (memorySavedBody ? "已保存在本机" : "尚未保存记忆卡")
      : "有未保存修改";
  });
  saveMemoryButton.addEventListener("click", async () => {
    saveMemoryButton.disabled = true;
    memoryState.textContent = "保存中…";
    try {
      const result = await api("/api/continuation-memory", {
        method: "POST",
        body: JSON.stringify({
          source: item.source,
          conversation_id: item.id,
          body: memoryInput.value,
          expected_updated_at: memoryUpdatedAt,
        }),
      });
      memorySavedBody = result.body || "";
      memoryUpdatedAt = Number(result.updated_at || 0);
      memoryInput.value = memorySavedBody;
      memoryState.textContent = memorySavedBody ? "已保存在本机" : "记忆卡已清空";
      showToast(memorySavedBody ? "记忆卡已保存" : "记忆卡已清空");
    } catch (error) {
      memoryState.textContent = error.message;
    } finally {
      saveMemoryButton.disabled = false;
    }
  });
  generateContinuation.addEventListener("click", async () => {
    if (includeMemory.checked && memoryInput.value.trim() !== memorySavedBody) {
      showToast("请先保存记忆卡，再选择附带");
      return;
    }
    generateContinuation.disabled = true;
    continuationState.textContent = "生成中…";
    try {
      continuationResult = await api(
        `/api/continuation/${encodeURIComponent(item.source)}/${encodeURIComponent(item.id)}`
        + `?memory=${includeMemory.checked ? "1" : "0"}`
      );
      continuationOutput.value = continuationResult.markdown || "";
      continuationPreview.hidden = false;
      copyContinuation.hidden = false;
      downloadContinuation.hidden = false;
      const fingerprint = continuationResult.packet?.content_sha256?.slice(0, 10) || "";
      continuationState.textContent = `已生成 · ${fingerprint}`;
    } catch (error) {
      continuationState.textContent = error.message;
    } finally {
      generateContinuation.disabled = false;
    }
  });
  copyContinuation.addEventListener("click", async () => {
    if (!continuationResult) return;
    await navigator.clipboard.writeText(continuationResult.markdown || "");
    showToast("接续包 Markdown 已复制");
  });
  downloadContinuation.addEventListener("click", () => {
    if (!continuationResult) return;
    const filename = `${item.source}-${item.id}-continuation.json`.replace(/[<>:"/\\|?*]/g, "_");
    downloadText(
      filename,
      JSON.stringify(continuationResult.packet, null, 2),
      "application/json;charset=utf-8",
    );
  });

  const overview = data.overview || {};
  const overviewRows = [];
  if (!overview.opening_is_latest && String(overview.goal || "").trim()) {
    overviewRows.push(["开场", "goal", overview.goal]);
  }
  overviewRows.push(["最近在问", "request", overview.latest_request || "还没有用户发言"]);
  overviewRows.push(["最近回应", "response", overview.latest_response || "还没有助手回复"]);
  fragment.querySelector(".overview").innerHTML = overviewRows.map(([term, kind, text]) => `
    <div class="overview-row ${kind}"><dt>${term}</dt><dd>${inlineMarkdown(text)}</dd></div>
  `).join("");

  const status = fragment.querySelector(".user-status");
  status.value = item.user_status || "";
  const projectSelect = fragment.querySelector(".project-assignment");
  if (projectSelect) projectSelect.parentElement.style.display = "none";
  renderTagEditor(detailRoot, item.tags || []);
  fragment.querySelector(".note-input").value = item.note || "";
  const relatedBlock = fragment.querySelector(".related-block");
  const relatedItems = data.related_conversations || [];
  if (relatedItems.length) {
    relatedBlock.hidden = false;
    fragment.querySelector(".related-count").textContent = `${relatedItems.length} 条`;
    fragment.querySelector(".related-conversations").innerHTML = relatedItems.map((related) => `
      <button type="button" class="related-conversation" data-source="${escapeHtml(related.source)}"
        data-id="${escapeHtml(related.id)}">
        <span class="source-badge ${escapeHtml(related.source)}">${escapeHtml(SOURCE_LABELS[related.source] || related.source)}</span>
        <strong>${escapeHtml(related.title)}</strong>
        <small>${Math.round((related.confidence || 0) * 100)}% · ${dateTime(related.updated_at)}</small>
      </button>
    `).join("");
    fragment.querySelector(".related-conversations").addEventListener("click", (event) => {
      const button = event.target.closest(".related-conversation");
      if (button) openDetail(button.dataset.source, button.dataset.id);
    });
  }

  let messageRole = "all";
  let messageQuery = "";
  let messageOrder = messageOrderPreference();
  let activeMessageMatch = 0;
  let conversationMessages = data.messages;
  let fullMessagesLoaded = false;
  let fullMessagesLoading = false;
  let transcriptView = "clean";
  const messagesRoot = fragment.querySelector(".messages");
  const messageCount = fragment.querySelector(".message-count");
  const viewButtons = [...fragment.querySelectorAll(".message-view-mode [data-view]")];
  const roleButtons = [...fragment.querySelectorAll(".message-role-filter [data-role]")];
  const orderButtons = [...fragment.querySelectorAll(".message-order [data-order]")];
  const messageSearch = fragment.querySelector(".conversation-search-input");
  const messageSearchState = fragment.querySelector(".conversation-search-state");
  const previousMatchButton = fragment.querySelector(".conversation-search-previous");
  const nextMatchButton = fragment.querySelector(".conversation-search-next");
  const clearMessageSearchButton = fragment.querySelector(".conversation-search-clear");

  const hiddenMessageCount = () => conversationMessages.filter((message) => messageVisibility(message) !== "visible").length;

  const renderMessages = () => {
    const needle = messageQuery.trim().toLocaleLowerCase();
    const hiddenCount = hiddenMessageCount();
    const showFullTranscript = transcriptView === "full";
    const filtered = conversationMessages.filter((message) => {
      if (!showFullTranscript && messageVisibility(message) !== "visible") return false;
      const roleMatch = messageRole === "all" || message.role === messageRole;
      const queryMatch = !needle || message.text.toLocaleLowerCase().includes(needle);
      return roleMatch && queryMatch;
    });
    if (activeMessageMatch >= filtered.length) activeMessageMatch = Math.max(0, filtered.length - 1);
    if (needle) {
      messageCount.textContent = `命中 ${filtered.length} 条 · 已读取 ${conversationMessages.length} 条`;
    } else if (showFullTranscript) {
      messageCount.textContent = `${filtered.length} 条 · 含过程 / 系统`;
    } else if (hiddenCount) {
      messageCount.textContent = `${filtered.length} 条正文 · ${hiddenCount} 条过程已隐藏`;
    } else {
      messageCount.textContent = `${filtered.length} 条`;
    }
    const searchNav = fragment.querySelector(".conversation-search-nav") || document.querySelector(".detail-pane .conversation-search-nav");
    if (searchNav) searchNav.hidden = !needle;
    messageSearchState.hidden = !needle && !fullMessagesLoading;
    messageSearchState.textContent = fullMessagesLoading
      ? "正在读取…"
      : (needle ? (filtered.length ? `${activeMessageMatch + 1} / ${filtered.length}` : "没有匹配") : "");
    previousMatchButton.disabled = !needle || filtered.length < 2;
    nextMatchButton.disabled = !needle || filtered.length < 2;
    clearMessageSearchButton.disabled = !needle;
    viewButtons.forEach((button) => button.classList.toggle("active", button.dataset.view === transcriptView));
    orderButtons.forEach((button) => button.classList.toggle("active", button.dataset.order === messageOrder));
    const foldProcess = !showFullTranscript;
    const ordered = messageOrder === "newest" ? filtered.slice().reverse() : filtered;
    messagesRoot.innerHTML = ordered.length ? ordered.map((message, index) => {
      const visibility = messageVisibility(message);
      const turnKey = messageTurnKey(message);
      const parts = splitMessageBody(message.text, message.role);
      const foldedHtml = parts.folded.map((block) => {
        const hit = needle && block.text.toLocaleLowerCase().includes(needle);
        const open = !foldProcess || hit ? " open" : "";
        return `<details class="message-fold ${escapeHtml(block.kind)}"${open}>
          <summary>${escapeHtml(block.title)}</summary>
          <div class="message-fold-body md-body">${renderRichText(block.text, messageQuery)}</div>
        </details>`;
      }).join("");
      return `
      <article class="message ${message.role} visibility-${visibility}${needle && index === activeMessageMatch ? " active-match" : ""}"
        data-turn-key="${escapeHtml(turnKey)}"${needle ? ` data-message-match="${index}"` : ""}>
        <div class="message-head"><strong>${messageRoleLabel(message)}</strong><span>${dateTime(message.timestamp)}</span></div>
        <div class="message-bubble">
          ${parts.main ? `<div class="message-text md-body">${renderRichText(parts.main, messageQuery)}</div>` : ""}
          ${foldedHtml}
        </div>
      </article>`;
    }).join("") : `<p class="muted">当前条件下没有消息。</p>`;
    renderReaderToc();
  };

  const tocPreviews = new Map();
  const renderReaderToc = () => {
    const nav = document.querySelector(".reader-toc-list") || fragment.querySelector(".reader-toc-list");
    if (!nav) return;
    tocPreviews.clear();
    const users = conversationMessages
      .map((message, index) => ({ message, index }))
      .filter(({ message }) => message.role === "user" && messageVisibility(message) === "visible");
    const tocUsers = messageOrder === "newest" ? users.slice().reverse() : users;
    const tocToggleButton = document.querySelector(".reader-toc-toggle") || fragment.querySelector(".reader-toc-toggle");
    if (tocToggleButton) {
      tocToggleButton.dataset.count = String(tocUsers.length);
      if (!document.body.classList.contains("reader-toc-open")) {
        tocToggleButton.textContent = tocUsers.length ? `目录 · ${tocUsers.length}` : "目录";
      }
    }
    if (!tocUsers.length) {
      nav.innerHTML = `<p class="muted">没有可跳转的提问</p>`;
      return;
    }
    nav.innerHTML = tocUsers.map(({ message }, order) => {
      const key = messageTurnKey(message);
      const full = (splitMessageBody(message.text, "user").main || message.text || "").trim();
      const lead = summaryLead(full, 22).short.replace(/\*\*|`|#/g, "").trim() || `提问 ${order + 1}`;
      tocPreviews.set(key, full);
      return `<button type="button" class="reader-toc-item" data-turn-key="${escapeHtml(key)}">
        <span class="reader-toc-index">${order + 1}</span>
        <span class="reader-toc-lead">${escapeHtml(lead)}</span>
      </button>`;
    }).join("");
  };

  const scrollToTurnKey = (key) => {
    const safe = typeof CSS !== "undefined" && CSS.escape ? CSS.escape(key) : key;
    const node = detailPane.querySelector(`.message[data-turn-key="${safe}"]`);
    if (!node) return false;
    scrollChildInto(detailPane, node, { offset: 24 });
    node.classList.add("toc-flash");
    window.setTimeout(() => node.classList.remove("toc-flash"), 900);
    document.querySelectorAll(".reader-toc-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.turnKey === key);
    });
    return true;
  };

  const jumpToTurn = (key) => {
    hideReaderTocTip();
    if (!key) return;
    messageRole = "all";
    messageQuery = "";
    if (messageSearch) messageSearch.value = "";
    roleButtons.forEach((button) => button.classList.toggle("active", button.dataset.role === "all"));
    const go = () => {
      renderMessages();
      if (scrollToTurnKey(key)) return;
      if (transcriptView !== "full") {
        transcriptView = "full";
        loadFullConversationMessages().then(() => {
          renderMessages();
          scrollToTurnKey(key);
        }).catch((error) => showToast(error.message));
        return;
      }
      showToast("没有找到这条提问");
    };
    if (!fullMessagesLoaded) {
      loadFullConversationMessages().then(go).catch((error) => showToast(error.message));
      return;
    }
    go();
  };
  readerJumpHandler = jumpToTurn;

  const focusMessageMatch = (direction = 0) => {
    if (!messageQuery.trim()) return;
    const matches = [...messagesRoot.querySelectorAll("[data-message-match]")];
    if (!matches.length) return;
    activeMessageMatch = (activeMessageMatch + direction + matches.length) % matches.length;
    matches.forEach((node, index) => node.classList.toggle("active-match", index === activeMessageMatch));
    messageSearchState.textContent = `${activeMessageMatch + 1} / ${matches.length}`;
    scrollChildInto(detailPane, matches[activeMessageMatch], { align: "center" });
  };

  let fullLoadPromise = null;
  const loadFullConversationMessages = () => {
    if (fullMessagesLoaded) return Promise.resolve();
    if (fullLoadPromise) return fullLoadPromise;
    fullMessagesLoading = true;
    renderMessages();
    fullLoadPromise = (async () => {
      try {
        const result = await api(
          `/api/conversation-messages/${encodeURIComponent(item.source)}/${encodeURIComponent(item.id)}?limit=200`
        );
        conversationMessages = result.messages || conversationMessages;
        fullMessagesLoaded = true;
      } catch (error) {
        fullLoadPromise = null;
        showToast(`读取完整对话失败：${error.message}`);
        throw error;
      } finally {
        fullMessagesLoading = false;
        activeMessageMatch = 0;
        renderMessages();
        if (messageQuery.trim()) focusMessageMatch(0);
      }
    })();
    return fullLoadPromise;
  };

  viewButtons.forEach((button) => button.addEventListener("click", () => {
    transcriptView = button.dataset.view === "full" ? "full" : "clean";
    activeMessageMatch = 0;
    if (transcriptView === "full") {
      loadFullConversationMessages().catch((error) => showToast(error.message));
    }
    renderMessages();
  }));
  orderButtons.forEach((button) => button.addEventListener("click", () => {
    messageOrder = button.dataset.order === "newest" ? "newest" : "oldest";
    try { localStorage.setItem(MESSAGE_ORDER_KEY, messageOrder); } catch { /* ignore quota / private mode */ }
    activeMessageMatch = 0;
    renderMessages();
  }));

  renderMessages();

  roleButtons.forEach((button) => button.addEventListener("click", () => {
    messageRole = button.dataset.role;
    roleButtons.forEach((candidate) => candidate.classList.toggle("active", candidate === button));
    activeMessageMatch = 0;
    renderMessages();
  }));
  let messageSearchTimer = 0;
  messageSearch.addEventListener("input", (event) => {
    messageQuery = event.target.value;
    activeMessageMatch = 0;
    renderMessages();
    window.clearTimeout(messageSearchTimer);
    if (messageQuery.trim()) {
      messageSearchTimer = window.setTimeout(() => {
        loadFullConversationMessages().catch((error) => showToast(error.message));
      }, 180);
    }
  });
  messageSearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      if (!messageQuery.trim()) return;
      event.preventDefault();
      focusMessageMatch(event.shiftKey ? -1 : 1);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!messageQuery.trim()) return;
      event.preventDefault();
      focusMessageMatch(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Escape") {
      messageSearch.value = "";
      messageQuery = "";
      activeMessageMatch = 0;
      renderMessages();
    }
  });
  previousMatchButton.addEventListener("click", () => focusMessageMatch(-1));
  nextMatchButton.addEventListener("click", () => focusMessageMatch(1));
  clearMessageSearchButton.addEventListener("click", () => {
    messageSearch.value = "";
    messageQuery = "";
    activeMessageMatch = 0;
    renderMessages();
    messageSearch.focus();
  });

  favoriteButton.addEventListener("click", async () => {
    item.favorite = !item.favorite;
    favoriteButton.textContent = item.favorite ? "★" : "☆";
    favoriteButton.classList.toggle("active", item.favorite);
    await saveDetail(detailRoot, item, true);
  });
  fragment.querySelector(".copy-id").addEventListener("click", async () => {
    await navigator.clipboard.writeText(item.id);
    showToast("已复制对话 ID");
  });
  fragment.querySelector(".export-conversation").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/export", {
        method: "POST",
        body: JSON.stringify({
          scope: "conversation",
          source: item.source,
          conversation_id: item.id,
          format: "markdown",
          include_messages: true,
          include_notes: true,
          include_knowledge: false,
        }),
      });
      downloadText(result.filename, result.content, result.mime);
      showToast("对话 Markdown 已导出");
    } catch (error) {
      showToast(error.message);
    } finally {
      button.disabled = false;
    }
  });
  fragment.querySelector(".save-note")?.addEventListener("click", () => saveDetail(detailRoot, item, false));

  // 标签编辑器：输入/回车/逗号确认，下拉候选，点 × 删除；改动即自动保存
  const persistTags = () => saveDetail(detailRoot, item, true, true);
  const addEditorTag = (value) => {
    const tag = String(value || "").trim().replace(/[,，]/g, "").slice(0, 60);
    const input = detailRoot.querySelector(".tags-input");
    if (!tag) { if (input) input.value = ""; return; }
    const tags = editorTags(detailRoot);
    if (tags.includes(tag)) {
      if (input) input.value = "";
      hideTagSuggest(detailRoot);
      return;
    }
    if (tags.length >= 20) { showToast("最多 20 个标签"); return; }
    tags.push(tag);
    renderTagEditor(detailRoot, tags);
    if (input) { input.value = ""; input.focus(); }
    hideTagSuggest(detailRoot);
    persistTags();
  };
  const tagsInput = fragment.querySelector(".tags-input");
  if (tagsInput) {
    tagsInput.addEventListener("input", () => {
      const raw = tagsInput.value;
      if (/[,，]/.test(raw)) {
        const parts = raw.split(/[,，]/);
        tagsInput.value = parts.pop() || "";
        parts.forEach((p) => addEditorTag(p));
      }
      showTagSuggest(detailRoot);
    });
    tagsInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const first = detailRoot.querySelector(".tags-suggest-item");
        if (tagsInput.value.trim()) addEditorTag(tagsInput.value);
        else if (first) addEditorTag(first.dataset.suggestTag);
      } else if (event.key === "Escape") {
        hideTagSuggest(detailRoot);
      } else if (event.key === "Backspace" && !tagsInput.value) {
        const tags = editorTags(detailRoot);
        if (tags.length) {
          renderTagEditor(detailRoot, tags.slice(0, -1));
          persistTags();
        }
      }
    });
    tagsInput.addEventListener("blur", () => {
      setTimeout(() => hideTagSuggest(detailRoot), 150);
    });
  }
  fragment.querySelector(".tags-editor")?.addEventListener("mousedown", (event) => {
    const pick = event.target.closest("[data-suggest-tag]");
    if (pick) { event.preventDefault(); addEditorTag(pick.dataset.suggestTag); return; }
    const del = event.target.closest("[data-remove-tag]");
    if (del) {
      event.preventDefault();
      renderTagEditor(detailRoot, editorTags(detailRoot).filter((t) => t !== del.dataset.removeTag));
      persistTags();
    }
  });

  // 备注自动保存：只读取不回写，不打断输入框原生的 Ctrl+Z 撤销栈
  const noteInputEl = fragment.querySelector(".note-input");
  let noteAutoTimer = 0;
  if (noteInputEl) {
    noteInputEl.addEventListener("input", () => {
      clearTimeout(noteAutoTimer);
      const ss = detailRoot.querySelector(".save-state");
      if (ss) ss.textContent = "编辑中…";
      noteAutoTimer = setTimeout(() => saveDetail(detailRoot, item, true, true), 900);
    });
  }
  // 状态切换也自动保存
  fragment.querySelector(".user-status")?.addEventListener("change", () => {
    item.user_status = status.value;
    const meta = detailRoot.querySelector(".detail-meta");
    if (meta) {
      meta.textContent = [
        dateTime(item.updated_at),
        item.model,
        statusLabels[item.user_status] || "",
      ].filter(Boolean).join(" · ");
    }
    saveDetail(detailRoot, item, true, true);
  });

  detailPane.replaceChildren(fragment);
  if (document.body.classList.contains("reader-open")) {
    setReaderTocOpen(readerTocOpen());
  }
}

async function saveDetail(root, item, quiet, auto = false) {
  const noteInput = root.querySelector(".note-input");
  const chipsBox = root.querySelector(".tags-chips");
  const statusSelect = root.querySelector(".user-status");
  const payload = {
    source: item.source,
    id: item.id,
    note: noteInput ? noteInput.value : (item.note || ""),
    tags: chipsBox
      ? [...chipsBox.querySelectorAll(".tag-edit-chip")].map((c) => c.dataset.tag)
      : (item.tags || []),
    user_status: statusSelect ? statusSelect.value : (item.user_status || ""),
    favorite: item.favorite,
  };
  const saveState = root.querySelector(".save-state");
  if (saveState) saveState.textContent = auto ? "自动保存中…" : "保存中…";
  try {
    await api("/api/note", { method: "POST", body: JSON.stringify(payload) });
    saveState.textContent = auto ? "已自动保存" : "已保存";
    Object.assign(item, payload);
    const cached = state.items.find((candidate) => candidate.source === item.source && candidate.id === item.id);
    if (cached) Object.assign(cached, payload);
    renderList();
    loadSummary();
    if (!quiet) showToast("备注和状态已保存");
  } catch (error) {
    if (saveState) saveState.textContent = "保存失败";
    showToast(`保存失败：${error.message}（可刷新页面后重试）`);
  }
}

function resetAndLoad() {
  state.offset = 0;
  state.items = [];
  rememberCurrentFilters();
  syncControls();
  syncUrl();
  loadConversations().catch((error) => {
    if (error.message.startsWith("搜索语法：")) {
      $("#searchInput").setAttribute("aria-invalid", "true");
      $("#resultCount").textContent = "搜索条件需要调整";
      list.innerHTML = `<div class="empty-detail"><div><h2>搜索语法未完成</h2><p>${escapeHtml(error.message)}。点击搜索框右侧“语法”查看示例。</p></div></div>`;
    } else {
      showToast(error.message);
    }
  });
}

function setRange(value) {
  if (!VALID_RANGES.has(value)) return;
  state.range = value;
  resetAndLoad();
}

function readSavedViews() {
  try {
    const value = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

function writeSavedViews(views) {
  localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views));
}

function renderSavedViews(selectedId = "") {
  const select = $("#savedViewSelect");
  const views = readSavedViews();
  select.innerHTML = `<option value="">选择视图…</option>` + views.map((view) =>
    `<option value="${escapeHtml(view.id)}">${escapeHtml(view.name)}</option>`
  ).join("");
  select.value = views.some((view) => view.id === selectedId) ? selectedId : "";
  $("#deleteViewButton").disabled = !select.value;
}

function saveCurrentView() {
  const nameInput = $("#savedViewName");
  const name = nameInput.value.trim();
  if (!name) {
    nameInput.focus();
    showToast("先给当前视图起个名字");
    return;
  }
  const views = readSavedViews();
  const existing = views.find((view) => view.name === name);
  const saved = {
    id: existing?.id || `${Date.now()}`,
    name,
    filters: { source: state.source, ...currentFilters() },
  };
  const next = existing
    ? views.map((view) => view.id === existing.id ? saved : view)
    : [...views, saved];
  writeSavedViews(next);
  renderSavedViews(saved.id);
  nameInput.value = "";
  showToast(existing ? "已更新保存的视图" : "当前视图已保存");
}

function applySavedView(id) {
  const view = readSavedViews().find((candidate) => candidate.id === id);
  if (!view) return;
  const filters = view.filters || {};
  state.source = VALID_SOURCES.has(filters.source) ? filters.source : "all";
  state.range = VALID_RANGES.has(filters.range) ? filters.range : "all";
  state.status = VALID_STATUSES.has(filters.status) ? filters.status : "all";
  state.workspace = filters.workspace || "all";
  state.nativeProject = filters.nativeProject || "all";
  state.favorites = Boolean(filters.favorites);
  state.query = filters.query || "";
  state.filters[state.source] = currentFilters();
  state.selected = null;
  syncControls();
  resetAndLoad();
}

$("#primaryNav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  setView(button.dataset.view);
});

$("#sidebarUtility").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  setView(button.dataset.view);
});

$("#closeSetupButton").addEventListener("click", () => $("#setupDialog").close());
$("#setupDialog").addEventListener("cancel", (event) => {
  if (event.currentTarget.dataset.required === "true") event.preventDefault();
});
$("#setupExtraSources").addEventListener("change", (event) => {
  const source = event.target.dataset.extraEnabled;
  if (!source) return;
  event.target.closest(".setup-extra-source")?.classList.toggle("enabled", event.target.checked);
});
$("#addCustomSourceButton").addEventListener("click", () => {
  const root = $("#setupCustomSources");
  root.insertAdjacentHTML("beforeend", customSourceRow());
  root.lastElementChild?.querySelector("[data-custom-label]")?.focus();
});
$("#setupCustomSources").addEventListener("change", (event) => {
  if (event.target.matches("[data-custom-enabled]")) {
    event.target.closest(".setup-custom-source")?.classList.toggle("enabled", event.target.checked);
  }
});
$("#setupCustomSources").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-custom]");
  if (button) button.closest(".setup-custom-source")?.remove();
});
$("#discoverSourcesButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "发现中…";
  try {
    const extraRoot = $("#setupExtraRoot").value.trim();
    const data = await api("/api/setup/discover", {
      method: "POST",
      body: JSON.stringify({ roots: extraRoot ? [extraRoot] : [] }),
    });
    renderSetupStatus(data);
    showToast("自动发现完成，请确认后保存");
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "自动发现";
  }
});
$("#saveSourcesButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "验证中…";
  try {
    const data = await api("/api/setup/save", {
      method: "POST",
      body: JSON.stringify({
        hermes_db: $("#setupHermesPath").value.trim(),
        codex_db: $("#setupCodexPath").value.trim(),
        workbuddy_home: $("#setupWorkbuddyPath").value.trim(),
        extra_sources: Object.fromEntries(EXTRA_SOURCES.map((source) => [
          source,
          {
            enabled: Boolean($(`[data-extra-enabled="${source}"]`)?.checked),
            path: $(`[data-extra-path="${source}"]`)?.value.trim() || "",
          },
        ])),
        custom_sources: [...document.querySelectorAll("[data-custom-source]")].map((row) => ({
          id: row.dataset.customSource,
          label: row.querySelector("[data-custom-label]").value.trim(),
          format: row.querySelector("[data-custom-format]").value,
          path: row.querySelector("[data-custom-path]").value.trim(),
          enabled: row.querySelector("[data-custom-enabled]").checked,
        })),
      }),
    });
    renderSetupStatus(data);
    $("#setupDialog").close();
    await Promise.all([loadDaily(), loadConversations(), loadSourceHealth()]);
    showToast("数据源已验证并建立索引");
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "验证并开始使用";
  }
});

$("#previewExportButton").addEventListener("click", () => {
  previewExport().catch((error) => {
    $("#exportState").textContent = error.message;
    showToast(error.message);
  });
});
$("#downloadExportButton").addEventListener("click", () => {
  if (!state.exportResult) return;
  let content = state.exportResult.content;
  // Markdown 加 UTF-8 BOM，避免 Windows 记事本/部分知识库工具按 ANSI 误判乱码
  if (state.exportResult.filename.endsWith(".md") && !content.startsWith("\ufeff")) {
    content = "\ufeff" + content;
  }
  downloadText(state.exportResult.filename, content, state.exportResult.mime);
  showToast("导出文件已下载");
});

$("#findDailyBrief").addEventListener("click", (event) => {
  if (event.target.closest("[data-open-daily]")) setView("daily");
});

async function setSourceEnabled(checkbox) {
  const source = checkbox.dataset.sourceEnabled;
  const enabled = checkbox.checked;
  checkbox.disabled = true;
  const row = checkbox.closest(".source-row");
  row?.classList.add("source-updating");
  try {
    const data = await api("/api/sources/enabled", {
      method: "POST",
      body: JSON.stringify({ source, enabled }),
    });
    syncSourceControls(data.sources || {});
    await Promise.all([
      loadConversations(),
      loadDaily(),
    ]);
    syncControls();
    showToast(`${SOURCE_LABELS[source] || source} 已${enabled ? "启用" : "停用"}`);
  } catch (error) {
    checkbox.checked = !enabled;
    showToast(error.message);
  } finally {
    checkbox.disabled = false;
    row?.classList.remove("source-updating");
  }
}

function switchSource(source, { preserveQuery = false } = {}) {
  if (
    !VALID_SOURCES.has(source)
    || source === state.source
    || (source !== "all" && !state.enabledSources.has(source))
  ) return;
  setView("find");
  const currentQuery = state.query;
  rememberCurrentFilters();
  state.source = source;
  Object.assign(state, state.filters[state.source]);
  if (preserveQuery) state.query = currentQuery;
  if (state.selected && state.selected.source !== state.source && state.source !== "all") {
    state.selected = null;
  }
  syncControls();
  resetAndLoad();
}

async function launchNewSession(source, targetId, href, kind) {
  if (kind === "app_link" && href) {
    window.location.href = href;
    showToast(`已打开 ${SOURCE_LABELS[source] || source}`);
    return;
  }
  await api("/api/launch", {
    method: "POST",
    body: JSON.stringify({ source, target_id: targetId || `${source}-new` }),
  });
  showToast(source === "grok" ? "已新开 Grok Build" : `已打开 ${SOURCE_LABELS[source] || source}`);
}

function renderSourceStarters() {
  const host = $("#sourceStarters");
  if (!host) return;
  host.innerHTML = "";
  const items = (state.launchers || []).filter((item) => item.source === state.source);
  if (state.source === "all" || !items.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button secondary";
    button.textContent = item.label;
    button.title = item.note || item.label;
    button.addEventListener("click", () => {
      launchNewSession(item.source, item.target_id, item.href, item.kind).catch((error) => {
        showToast(error.message);
      });
    });
    host.append(button);
  });
}

async function loadSourceLaunchers() {
  try {
    const health = await api("/api/health");
    state.launchers = health.launchers || [];
  } catch {
    state.launchers = [];
  }
  renderSourceStarters();
}

$("#agentSwitcher").addEventListener("click", (event) => {
  const checkbox = event.target.closest("[data-source-enabled]");
  if (checkbox) {
    setSourceEnabled(checkbox).catch((error) => showToast(error.message));
    return;
  }
  const starter = event.target.closest("[data-new-source]");
  if (starter) {
    event.preventDefault();
    event.stopPropagation();
    launchNewSession(
      starter.dataset.newSource,
      starter.dataset.targetId,
      starter.dataset.href,
      starter.dataset.kind,
    ).catch((error) => {
      showToast(error.message);
    });
    return;
  }
  const button = event.target.closest("[data-source]");
  if (!button) return;
  switchSource(button.dataset.source);
});

// 设置页「数据源质量报告」里的启用开关：复用侧栏同一套开关逻辑
$("#sourceHealth").addEventListener("click", (event) => {
  const checkbox = event.target.closest("[data-source-enabled]");
  if (!checkbox) return;
  setSourceEnabled(checkbox)
    .then(() => loadSourceHealth().catch(() => {}))
    .catch((error) => showToast(error.message));
});

// 弹窗点击遮罩（窗口外）自动关闭；首次运行必需配置除外
document.querySelectorAll("dialog.settings-dialog").forEach((dlg) => {
  dlg.addEventListener("click", (event) => {
    if (event.target !== dlg) return;
    if (dlg.id === "setupDialog" && dlg.dataset.required === "true") return;
    dlg.close();
  });
});

// ---- 无感自动刷新：标签页回到前台或每 60 秒静默比对，数据有变才重载，不弹窗不跳滚动 ----
async function silentAutoRefresh() {
  try {
    const data = await api("/api/summary");
    if (!data || data.refreshed_at === lastRefreshedAt) return;
    const scroller = document.querySelector(".app-workspace");
    const top = scroller ? scroller.scrollTop : 0;
    await Promise.all([
      loadSummary(),
      loadConversations(),
      loadDaily().catch(() => {}),
    ]);
    if (scroller) scroller.scrollTop = top;
  } catch {
    // 静默失败：自动刷新只是尽力而为的增量增强
  }
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) silentAutoRefresh();
});
setInterval(() => {
  if (!document.hidden) silentAutoRefresh();
}, 60_000);

$("#searchAgentFilter").addEventListener("change", (event) => {
  switchSource(event.target.value, { preserveQuery: true });
});

$("#dailyDateStrip").addEventListener("click", (event) => {
  const button = event.target.closest("[data-day]");
  if (button) setDailyDate(button.dataset.day).catch((error) => showToast(error.message));
});

$("#previousDayButton").addEventListener("click", () => {
  setDailyDate(shiftDate(state.dailyDate, -1)).catch((error) => showToast(error.message));
});

$("#nextDayButton").addEventListener("click", () => {
  setDailyDate(shiftDate(state.dailyDate, 1)).catch((error) => showToast(error.message));
});

$("#reviewDate").addEventListener("change", (event) => {
  setDailyDate(event.target.value).catch((error) => showToast(error.message));
});

$("#refreshDataButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  showToast("正在重新读取数据源…");
  try {
    const result = await api("/api/refresh", { method: "POST", body: "{}" });
    await Promise.all([loadSummary(), loadConversations(), loadDaily()]);
    showToast(`已刷新 · 共 ${result.total ?? state.total} 个对话`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});

// ------------------------------------------------------------------ 我的项目
async function loadProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects || [];
  if (state.openProjectId) {
    await openProject(state.openProjectId);
  } else {
    renderProjectList();
  }
}

function renderProjectList() {
  const list = $("#projectList");
  const detail = $("#projectDetail");
  detail.hidden = true;
  list.hidden = false;
  $("#backToProjectsButton").hidden = true;
  if (!state.projects.length) {
    list.innerHTML = `<div class="empty-detail"><div><h2>还没有项目</h2><p>在「找对话」里勾选几个对话，点「归入项目」；或点「新建项目」。</p></div></div>`;
    return;
  }
  const stLabels = { active: "进行中", done: "已完成", paused: "暂停" };
  const stClass = { active: "st-active", done: "st-done", paused: "st-paused" };
  list.innerHTML = state.projects.map((p) => {
    const st = p.status || "active";
    return `
    <button class="project-card" type="button" data-project="${escapeHtml(p.id)}">
      <span class="project-card-head">
        <strong>${escapeHtml(p.name)}</strong>
        <span class="proj-status-pill ${stClass[st]}">${stLabels[st]}</span>
      </span>
      <span class="project-desc">${escapeHtml(p.description || "暂无说明")}</span>
      <span class="muted">${p.count} 个对话 · 更新于 ${relativeTime(p.updated_at)}</span>
    </button>`;
  }).join("");
}

async function openProject(id) {
  const data = await api(`/api/projects/${encodeURIComponent(id)}`);
  state.openProjectId = id;
  renderProjectDetail(data);
}

function renderProjectDetail(p) {
  const list = $("#projectList");
  const detail = $("#projectDetail");
  list.hidden = true;
  detail.hidden = false;
  $("#backToProjectsButton").hidden = false;
  state.openProjectId = p.id;
  const items = p.items || [];
  const tasks = p.tasks || [];
  const statusLabels = { active: "进行中", done: "已完成", paused: "暂停" };
  const statusClass = { active: "st-active", done: "st-done", paused: "st-paused" };
  const st = p.status || "active";
  const nextSt = st === "active" ? "done" : st === "done" ? "paused" : "active";

  detail.innerHTML = `
    <div class="project-detail-head">
      <div>
        <h3>${escapeHtml(p.name)}</h3>
        <p class="muted">${escapeHtml(p.description || "暂无说明")}</p>
      </div>
      <span class="project-detail-actions">
        <button class="proj-status-pill ${statusClass[st]}" type="button" data-cycle-status="${escapeHtml(p.id)}" title="点击切换状态">${statusLabels[st]}</button>
        <button class="button ghost" type="button" data-edit-project="${escapeHtml(p.id)}">编辑</button>
        <button class="button ghost" type="button" data-delete-project="${escapeHtml(p.id)}">删除</button>
      </span>
    </div>

    <section class="proj-section">
      <h4 class="proj-section-title">📝 项目笔记</h4>
      <textarea id="projectNoteArea" class="proj-note-area" rows="5" placeholder="记录关键结论、决策、反思…">${escapeHtml(p.note || "")}</textarea>
      <button class="button secondary proj-note-save" type="button" data-save-note="${escapeHtml(p.id)}">保存笔记</button>
    </section>

    <section class="proj-section">
      <h4 class="proj-section-title">✓ 任务清单 <small class="muted">(${tasks.filter(t => !t.done).length} 待办)</small></h4>
      <div class="proj-tasks">
        ${tasks.length ? tasks.map((t) => `
          <div class="proj-task${t.done ? " done" : ""}">
            <label><input type="checkbox" data-toggle-task="${escapeHtml(t.id)}" ${t.done ? "checked" : ""}><span>${escapeHtml(t.title)}</span></label>
            <button class="proj-task-del" type="button" data-del-task="${escapeHtml(t.id)}" title="删除">×</button>
          </div>`).join("") : `<p class="muted proj-empty-hint">暂无任务</p>`}
      </div>
      <div class="proj-task-add">
        <input id="projTaskInput" type="text" placeholder="添加任务后回车…" maxlength="200">
        <button class="button ghost" type="button" data-add-task="${escapeHtml(p.id)}">＋</button>
      </div>
    </section>

    <section class="proj-section">
      <h4 class="proj-section-title">💬 对话 (${items.length})</h4>
      ${items.length ? items.map((it) => `
        <div class="project-item${it.present ? "" : " missing"}">
          <span class="source-dot ${escapeHtml(it.source)}"></span>
          <div class="project-item-body">
            <div class="project-item-row">
              <button class="project-item-main" type="button" ${it.present ? `data-open-conv="${escapeHtml(it.source)}|${escapeHtml(it.id)}"` : "disabled"}>
                <strong>${escapeHtml(it.title)}</strong>
                <small class="muted">${escapeHtml(conversationSourceLabel({ source: it.source }))} · ${it.message_count} 条 · ${it.updated_at ? relativeTime(it.updated_at) : ""}</small>
              </button>
              <button class="button ghost" type="button" data-remove-conv="${escapeHtml(it.source)}|${escapeHtml(it.id)}">移除</button>
            </div>
            <input class="proj-item-note" type="text" placeholder="${escapeHtml(it.note ? "" : "加标注：为什么重要…")}"
              value="${escapeHtml(it.note || "")}"
              data-annotate="${escapeHtml(it.source)}|${escapeHtml(it.id)}">
          </div>
        </div>`).join("") : `<div class="empty-detail"><div><h2>这个项目还是空的</h2><p>回「找对话」勾选对话后点「归入项目」。</p></div></div>`}
    </section>`;

  // 笔记保存按钮
  detail.querySelector("[data-save-note]")?.addEventListener("click", async () => {
    const body = $("#projectNoteArea").value;
    await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "save_note", id: p.id, body }) });
    showToast("笔记已保存");
  });
  // 状态切换
  detail.querySelector("[data-cycle-status]")?.addEventListener("click", async () => {
    await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "set_status", id: p.id, status: nextSt }) });
    openProject(p.id);
  });
  // 添加任务
  const addTask = async () => {
    const title = $("#projTaskInput").value.trim();
    if (!title) return;
    await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "add_task", id: p.id, title }) });
    openProject(p.id);
  };
  detail.querySelector("[data-add-task]")?.addEventListener("click", addTask);
  $("#projTaskInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addTask(); } });
  // 任务勾选/删除
  detail.querySelectorAll("[data-toggle-task]").forEach((cb) => {
    cb.addEventListener("change", async () => {
      await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "toggle_task", id: p.id, task_id: cb.dataset.toggleTask }) });
      openProject(p.id);
    });
  });
  detail.querySelectorAll("[data-del-task]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "delete_task", id: p.id, task_id: btn.dataset.delTask }) });
      openProject(p.id);
    });
  });
  // 对话标注（失焦时保存）
  detail.querySelectorAll("[data-annotate]").forEach((inp) => {
    inp.addEventListener("change", async () => {
      const [source, cid] = inp.dataset.annotate.split("|");
      await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "annotate_item", id: p.id, source, conversation_id: cid, note: inp.value }) });
      showToast("标注已保存");
    });
  });
}

function checkedConversations() {
  return [...state.checked.values()].map((it) => ({ source: it.source, id: it.id }));
}

async function assignToProject(projectId) {
  const conversations = checkedConversations();
  if (!conversations.length) { showToast("请先勾选对话"); return; }
  await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "add", id: projectId, conversations }) });
  state.checked.clear();
  renderList();
  updateSelectionBar();
  $("#projectAssignDialog").close();
  showToast(`已归入 ${conversations.length} 个对话`);
  if (state.view === "projects") loadProjects().catch(() => {});
}

function renderAssignList() {
  const box = $("#assignProjectList");
  const stLabels = { active: "进行中", done: "已完成", paused: "暂停" };
  const stClass = { active: "st-active", done: "st-done", paused: "st-paused" };
  box.innerHTML = state.projects.length
    ? state.projects.map((p) => {
        const st = p.status || "active";
        return `
        <button class="assign-project-row" type="button" data-assign="${escapeHtml(p.id)}">
          <span class="assign-row-main">
            <strong>${escapeHtml(p.name)}</strong>
            <span class="muted">${p.count} 个对话</span>
          </span>
          <span class="assign-row-side">
            <span class="proj-status-pill ${stClass[st]}">${stLabels[st]}</span>
            <span class="assign-row-go">归入 →</span>
          </span>
        </button>`;
      }).join("")
    : `<p class="muted">还没有项目，先在下方新建一个。</p>`;
}

$("#addToProjectButton").addEventListener("click", async () => {
  if (!state.checked.size) { showToast("请先勾选要归入的对话"); return; }
  try {
    const data = await api("/api/projects");
    state.projects = data.projects || [];
    renderAssignList();
    const hint = $("#assignDialogHint");
    if (hint) hint.textContent = `已选 ${state.checked.size} 个对话。点击一个项目立即归入；或在下方新建项目并归入。`;
    $("#projectAssignDialog").showModal();
  } catch (error) { showToast(error.message); }
});

$("#assignProjectList").addEventListener("click", (event) => {
  const row = event.target.closest("[data-assign]");
  if (row) assignToProject(row.dataset.assign).catch((error) => showToast(error.message));
});

$("#assignCreateButton").addEventListener("click", async () => {
  const name = $("#assignNewName").value.trim();
  if (!name) { showToast("先给新项目起个名字"); return; }
  try {
    const created = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ action: "create", name, description: $("#assignNewDesc").value.trim() }),
    });
    $("#assignNewName").value = "";
    $("#assignNewDesc").value = "";
    await assignToProject(created.id);
  } catch (error) { showToast(error.message); }
});

$("#closeAssignButton").addEventListener("click", () => $("#projectAssignDialog").close());

function openProjectForm(mode, id = null, addAfter = false) {
  state.projectForm = { mode, id, addAfter };
  const existing = mode === "edit" ? state.projects.find((p) => p.id === id) : null;
  $("#projectFormTitle").textContent = mode === "edit" ? "编辑项目" : "新建项目";
  $("#projectNameInput").value = existing?.name || "";
  $("#projectDescInput").value = existing?.description || "";
  $("#projectFormDialog").showModal();
}

$("#newProjectButton").addEventListener("click", () => openProjectForm("create"));
$("#projectFormCancel").addEventListener("click", () => $("#projectFormDialog").close());

$("#projectFormSave").addEventListener("click", async () => {
  const name = $("#projectNameInput").value.trim();
  if (!name) { showToast("项目需要名字"); return; }
  const { mode, id, addAfter } = state.projectForm;
  try {
    const result = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        action: mode === "edit" ? "update" : "create",
        id: mode === "edit" ? id : undefined,
        name,
        description: $("#projectDescInput").value.trim(),
      }),
    });
    $("#projectFormDialog").close();
    if (mode === "create" && addAfter) {
      await assignToProject(result.id);
    } else {
      showToast("已保存");
      loadProjects().catch(() => {});
    }
  } catch (error) { showToast(error.message); }
});

$("#projectList").addEventListener("click", (event) => {
  const card = event.target.closest("[data-project]");
  if (card) openProject(card.dataset.project).catch((error) => showToast(error.message));
});

$("#backToProjectsButton").addEventListener("click", () => {
  state.openProjectId = null;
  renderProjectList();
});

$("#projectDetail").addEventListener("click", async (event) => {
  const open = event.target.closest("[data-open-conv]");
  if (open) {
    const [source, id] = open.dataset.openConv.split("|");
    setView("find");
    await openDetail(source, id);
    detailPane.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const remove = event.target.closest("[data-remove-conv]");
  if (remove) {
    const [source, id] = remove.dataset.removeConv.split("|");
    await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ action: "remove", id: state.openProjectId, conversations: [{ source, id }] }),
    });
    openProject(state.openProjectId).catch(() => {});
    return;
  }
  const edit = event.target.closest("[data-edit-project]");
  if (edit) { openProjectForm("edit", edit.dataset.editProject); return; }
  const del = event.target.closest("[data-delete-project]");
  if (del) {
    const ok = await new Promise((resolve) => {
      wxConfirm(resolve);
    });
    if (!ok) return;
    await api("/api/projects", { method: "POST", body: JSON.stringify({ action: "delete", id: del.dataset.deleteProject }) });
    state.openProjectId = null;
    loadProjects().catch(() => {});
  }
});

function wxConfirm(resolve) {
  // 桌面环境用原生 confirm
  resolve(window.confirm("删除该项目？（不会删除对话本身）"));
}

$("#dailyBody").addEventListener("click", async (event) => {
  const toggle = event.target.closest("#toggleDailyReportButton");
  if (toggle) {
    state.dailyReportOpen = !state.dailyReportOpen;
    const report = $("#dailyReport");
    if (report) report.hidden = !state.dailyReportOpen;
    toggle.textContent = state.dailyReportOpen ? "收起完整日报" : "查看完整日报";
    if (state.dailyReportOpen) report?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const conversation = event.target.closest("[data-source][data-id]");
  if (conversation) {
    setView("find");
    // 从回顾跳回找对话时归零滚动，避免残留滚动位置造成大片空白
    document.querySelector(".app-workspace")?.scrollTo({ top: 0 });
    await openDetail(conversation.dataset.source, conversation.dataset.id);
    return;
  }
  const saveButton = event.target.closest("#saveDailyNoteButton");
  if (!saveButton) return;
  saveButton.disabled = true;
  saveButton.textContent = "保存中…";
  try {
    const result = await api("/api/daily/note", {
      method: "POST",
      body: JSON.stringify({
        day: state.dailyDate,
        manual_note: $("#dailyManualNote").value,
      }),
    });
    if (state.daily) state.daily.manual_note = result.manual_note;
    showToast("当日补充已保存");
  } catch (error) {
    showToast(error.message);
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = "保存补充";
  }
});

$("#summary").addEventListener("click", (event) => {
  const favorite = event.target.closest("[data-favorite]");
  if (favorite) {
    state.favorites = !state.favorites;
    resetAndLoad();
    return;
  }
  const range = event.target.closest("[data-range]");
  if (range) setRange(range.dataset.range);
});

$("#quickRanges").addEventListener("click", (event) => {
  const button = event.target.closest("[data-range]");
  if (button) setRange(button.dataset.range);
});

$("#clearFiltersButton").addEventListener("click", () => {
  Object.assign(state, defaultFilters());
  state.selected = null;
  resetAndLoad();
});

$("#statusFilter").addEventListener("change", (event) => {
  state.status = event.target.value;
  resetAndLoad();
});

$("#workspaceFilter").addEventListener("change", (event) => {
  state.workspace = event.target.value;
  resetAndLoad();
});

$("#nativeProjectFilter").addEventListener("change", (event) => {
  state.nativeProject = event.target.value;
  resetAndLoad();
});

$("#tagFilter").addEventListener("change", (event) => {
  state.tag = event.target.value;
  renderTagChips();
  resetAndLoad();
});

$("#tagChips").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-tag]");
  if (!chip) return;
  state.tag = state.tag === chip.dataset.tag ? "" : chip.dataset.tag;
  const tagSelectSync = $("#tagFilter");
  if (tagSelectSync) tagSelectSync.value = state.tag;
  renderTagChips();
  resetAndLoad();
});

document.addEventListener("click", (event) => {
  const more = $("#moreFilters");
  if (more && more.open && !event.target.closest("#moreFilters")) more.open = false;
});

$("#favoriteFilter").addEventListener("click", () => {
  state.favorites = !state.favorites;
  resetAndLoad();
});

// ---- 全局搜索 ----
function applySearch(rawValue) {
  state.query = rawValue.trim();
  resetAndLoad();
}

$("#searchInput").addEventListener("input", (event) => {
  event.target.removeAttribute("aria-invalid");
  clearTimeout(searchTimer);
  const value = event.target.value;
  searchTimer = setTimeout(() => applySearch(value), 380);
});
$("#searchInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    clearTimeout(searchTimer);
    applySearch(event.currentTarget.value);
  } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && state.query.trim()) {
    event.preventDefault();
    focusListedConversation(event.key === "ArrowDown" ? 1 : -1, { keepSearchFocus: true });
  } else if (event.key === "Escape" && event.currentTarget.value) {
    clearTimeout(searchTimer);
    event.currentTarget.value = "";
    state.query = "";
    resetAndLoad();
  }
});
$("#searchPrev")?.addEventListener("click", () => focusListedConversation(-1));
$("#searchNext")?.addEventListener("click", () => focusListedConversation(1));

document.addEventListener("click", (event) => {
  if (event.target.closest(".reader-toc-close")) {
    event.preventDefault();
    setReaderTocOpen(false);
    return;
  }
  if (event.target.closest(".reader-toc-head") && !document.body.classList.contains("reader-toc-open")) {
    setReaderTocOpen(true);
    return;
  }
  const item = event.target.closest(".reader-toc-item[data-turn-key]");
  if (item && readerJumpHandler) {
    event.preventDefault();
    readerJumpHandler(item.dataset.turnKey);
  }
});

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    setView("find");
    $("#searchInput").focus();
    return;
  }
  if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey) {
    if (event.target.closest("input, textarea, select, [contenteditable]")) return;
    event.preventDefault();
    setView("find");
    $("#searchInput").focus();
  }
});

$("#savedViewSelect").addEventListener("change", (event) => {
  $("#deleteViewButton").disabled = !event.target.value;
  if (event.target.value) applySavedView(event.target.value);
});

$("#saveViewButton").addEventListener("click", saveCurrentView);
$("#savedViewName").addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveCurrentView();
});

$("#deleteViewButton").addEventListener("click", () => {
  const id = $("#savedViewSelect").value;
  if (!id) return;
  writeSavedViews(readSavedViews().filter((view) => view.id !== id));
  renderSavedViews();
  showToast("保存的视图已删除");
});

list.addEventListener("click", (event) => {
  const button = event.target.closest(".conversation");
  if (!button) return;
  if (event.target.closest(".check-mark")) {
    toggleConversationCheck(button.dataset.source, button.dataset.id);
    return;
  }
  openDetail(button.dataset.source, button.dataset.id);
});

function toggleConversationCheck(source, id) {
  const key = `${source}:${id}`;
  if (state.checked.has(key)) {
    state.checked.delete(key);
  } else {
    const item = state.items.find((it) => it.source === source && it.id === id);
    state.checked.set(key, { source, id, title: item?.title || "" });
  }
  renderList();
  updateSelectionBar();
}

function updateSelectionBar() {
  const bar = $("#selectionBar");
  if (!bar) return;
  const count = state.checked.size;
  bar.hidden = count === 0;
  $("#selectionCount").textContent = `已选 ${count} 个对话`;
}

// ---- 对话总结 / 内容分析 ----

$("#selectAllVisibleButton")?.addEventListener("click", () => {
  state.items.forEach((item) => {
    const key = `${item.source}:${item.id}`;
    if (!state.checked.has(key)) {
      state.checked.set(key, { source: item.source, id: item.id, title: item.title });
    }
  });
  renderList();
  updateSelectionBar();
});

$("#clearSelectionButton")?.addEventListener("click", () => {
  state.checked.clear();
  renderList();
  updateSelectionBar();
});

// 导出所选：切到工具页，自动选"已勾选的对话"并预览
$("#exportSelectedButton")?.addEventListener("click", () => {
  if (!state.checked.size) { showToast("请先勾选要导出的对话"); return; }
  setView("assets");
  const scopeSelect = $("#exportScope");
  if (scopeSelect) scopeSelect.value = "selected";
  const dateLabel = $("#exportDateLabel");
  if (dateLabel) dateLabel.style.display = "none";
  $("#exportState").textContent = `已勾选 ${state.checked.size} 个对话`;
  previewExport().catch((error) => showToast(error.message));
});

$("#loadMoreButton").addEventListener("click", async () => {
  state.offset = state.items.length;
  await loadConversations({ append: true });
});

$("#refreshButton")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    await api("/api/refresh", { method: "POST", body: "{}" });
    await Promise.all([loadConversations(), loadDaily()]);
    showToast("已从所有启用的数据来源重新读取");
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "刷新数据";
  }
});

$("#openSetupButton")?.addEventListener("click", () => loadSetupStatus({ open: true }));

$("#diagnoseSourcesButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "检查中…";
  try {
    await api("/api/sources/diagnose", { method: "POST", body: "{}" });
    await loadSourceHealth();
    showToast("适配器、结构和正文索引检查完成");
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "运行兼容性检查";
  }
});

$("#exportBackupButton").addEventListener("click", async () => {
  try {
    const backup = await api("/api/backup/export", { method: "POST", body: "{}" });
    downloadText(
      `AIConversationHub-backup-${localDateIso()}.json`,
      JSON.stringify(backup, null, 2),
      "application/json;charset=utf-8"
    );
    $("#backupState").textContent = "备份已导出；不含密钥与原始对话。";
  } catch (error) {
    showToast(error.message);
  }
});

$("#backupFileInput").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  if (file) previewBackupFile(file).catch((error) => showToast(error.message));
  event.target.value = "";
});




function startHubWatchdog() {
  const banner = $("#hubOfflineBanner");
  const button = $("#hubReconnectButton");
  let offline = false;
  const check = async () => {
    try {
      const data = await api("/api/health");
      if (!data || data.ok === false) throw new Error("offline");
      if (offline) {
        offline = false;
        if (banner) banner.hidden = true;
        showToast("对话中心已重新连上");
      }
    } catch {
      offline = true;
      if (banner) banner.hidden = false;
    }
  };
  button?.addEventListener("click", () => {
    check().catch(() => {});
  });
  window.setInterval(check, 15000);
}

$("#themeButton").addEventListener("click", openThemeDialog);
$("#detailToggleButton").addEventListener("click", () => {
  toggleDetailDrawer().catch((error) => showToast(error.message));
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !$(".find-layout").classList.contains("detail-open")) return;
  if (event.target.closest("dialog")) return;
  if (document.body.classList.contains("reader-open")) {
    event.preventDefault();
    setReaderOpen(false);
    return;
  }
  setDetailOpen(false, { focusToggle: true });
});
$("#openThemeSettingsButton").addEventListener("click", openThemeDialog);
$("#closeThemeButton").addEventListener("click", () => $("#themeDialog").close());
$("#themeGallery").addEventListener("click", (event) => {
  const button = event.target.closest("[data-theme-id]");
  if (!button) return;
  applyTheme(button.dataset.themeId);
  showToast(`已切换为${THEMES[button.dataset.themeId].name}`);
});
$("#resetThemeButton").addEventListener("click", () => {
  applyTheme("archive");
  showToast("已恢复经典主题");
});

async function boot() {
  const DEBUG = /[?&]debug=1/.test(location.search);
  const _log = (msg) => {
    if (!DEBUG) return;
    const el = document.getElementById("bootDebug") || (() => {
      const d = document.createElement("div");
      d.id = "bootDebug";
      d.style.cssText = "position:fixed;top:0;left:50%;transform:translateX(-50%);z-index:9999;background:#173f3b;color:#fff;padding:6px 16px;border-radius:0 0 8px 8px;font-size:12px;font-family:sans-serif";
      document.body.prepend(d);
      return d;
    })();
    el.textContent = msg;
  };
  try {
    _log("启动中…");
    let savedTheme = "";
    try { savedTheme = localStorage.getItem(THEME_KEY) || ""; } catch {}
    applyTheme(THEMES[savedTheme] ? savedTheme : currentTheme(), { persist: false });
    applyReaderLook();
    detailPane.addEventListener("scroll", hideReaderTocTip, { passive: true });
    initSidebarCollapse();
    startHubWatchdog();
    $(".app-workspace")?.addEventListener("scroll", syncSearchFloat, { passive: true });
    setDetailOpen(false);
    initDetailResizer();
    initSourceDetails();
    initSourceDrag();
    _log("初始化完成…");
    $("#todayDate").textContent = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      month: "long",
      day: "numeric",
      weekday: "short",
    }).format(new Date());
    renderSavedViews();
    state.token = (await api("/api/token")).token;
    _log("获取令牌…");
    await loadSetupStatus({ openIfRequired: true });
    _log("检查数据源…");
    await waitForIndexReady();
    loadSourceLaunchers().catch(() => {});
    readUrlState();
    setView(state.view, { sync: false });
    _log("加载对话…");
    loadSummary();
    syncControls();
    await loadConversations();
    _log("完成");
    // 深链（?conversationSource=&conversation=）直接展开详情，
    // 让外部跳转（handoff 包、收藏链接）一步落到续接按钮
    if (state.selected) {
      setView("find", { sync: false });
      await openDetail(state.selected.source, state.selected.id);
    }
    if (state.view === "daily") {
      await loadDaily();
    } else {
      loadDaily().catch((error) => showToast(error.message));
    }
  } catch (error) {
    showToast(error.message);
  }
}

async function waitForIndexReady() {
  const deadline = Date.now() + 60000;
  let lastState = null;
  while (Date.now() < deadline) {
    const health = await api("/api/health");
    lastState = health.index || {};
    if (lastState.status === "ready") return health;
    if (lastState.status === "error") {
      throw new Error(`本地索引初始化失败：${lastState.error || "未知错误"}`);
    }
    $("#resultCount").textContent = "正在建立本地索引…";
    list.innerHTML = `<div class="index-loading"><span></span><strong>正在读取本地对话</strong><small>页面已经就绪，索引完成后会自动显示结果</small></div>`;
    await new Promise((resolve) => setTimeout(resolve, 120));
  }
  throw new Error(`本地索引等待超时（当前：${lastState?.status || "未知"}）`);
}

boot();
