// Frontend for the PDF Tree QA app. Vanilla JS, no build step.
//
// Three-pane layout: PDF viewer (PDF.js) │ question tree (SVG) │ conversation + notes.
// All UI state lives in the single top-level `state` object below; the rest of the file
// is functions that read/mutate that object and render directly to the DOM.
//
// Navigation is hash-routed through three views: home (workspace cards) → workspace
// (PDF list) → reader (the three-pane view for one PDF). See parseRoute()/applyRoute().

const NODE_W = 238;
const NODE_H = 112;
const NODE_OVERFLOW = 18;
const X_GAP = 330;
const Y_GAP = 150;
const PALETTE = ["#287c74", "#b6542f", "#3a6ea5", "#6f7d51", "#9b4d68", "#7a6840", "#2f7d51", "#8c5b2e"];
const SIDE_WIDTH_KEY = "sidePanelWidth";
const DEFAULT_SIDE_WIDTH = 420;
const MIN_SIDE_WIDTH = 240;
const MAX_SIDE_WIDTH = 920;

const PANE_KEYS = ["pdf", "tree", "chat"];
function loadPaneState() {
  const out = { pdf: true, tree: true, chat: true };
  try {
    const raw = JSON.parse(localStorage.getItem("paneOpen") || "{}");
    for (const key of PANE_KEYS) if (typeof raw[key] === "boolean") out[key] = raw[key];
  } catch {}
  return out;
}

const state = {
  allNodes: [],
  nodes: [],
  files: [],
  workDir: "",
  // ── Workspaces (a user project: a group of PDFs + shared files) ──
  view: "home", // "home" | "workspace" | "reader"
  workspaces: [],
  currentWorkspaceId: localStorage.getItem("currentWorkspaceId") || null,
  wsDetail: { documents: [], files: [] }, // workspace-detail page data
  allFiles: [], // all files from /api/tree (state.files is the subset for the current workspace)
  // ── PDF / documents ──
  documents: [],
  currentDocId: localStorage.getItem("currentDocId") || null,
  pdfDoc: null,
  pdfScale: Number(localStorage.getItem("pdfScale")) || 1.3,
  pdfRenderToken: 0,
  pdfPageViewports: new Map(),
  highlights: [],
  pendingSelection: null, // {quote, page, rects, color}
  activeHighlightId: null,
  paneOpen: loadPaneState(),
  selectedId: null,
  parentId: null,
  fileIds: new Set(),
  workspaceRefs: [],
  selectedBackend: localStorage.getItem("askBackend") === "codex" ? "codex" : "claude",
  enableWebSearch: localStorage.getItem("askWebSearch") === "on",
  layout: new Map(),
  nodeById: new Map(),
  childrenByParent: new Map(),
  branchColors: new Map(),
  selectedPath: new Set(),
  transform: { x: 90, y: 72, k: 1 },
  search: "",
  treeDirection: localStorage.getItem("treeDirection") === "right" ? "right" : "down",
  dragging: false,
  dragStart: null,
  resizingSidePanel: false,
  sidePanelWidth: DEFAULT_SIDE_WIDTH,
  sending: false,
  activeSideTab: "conversation",
  noteDraftNodeId: null,
  noteDraft: "",
  noteDirty: false,
  noteSaving: false,
  noteEditing: false,
  projectNotes: [],
  projectNotesProjectId: null,
  noteContextText: "",
  scrollConversationToLatest: false,
  // Root node id whose subtree the "project notes" summary aggregates (null = none yet).
  selectedProjectId: null,
  // Files the AI touched under the workspace scratch dir, for the current node's side tab.
  workspaceFiles: [],
};

const els = {
  searchInput: document.getElementById("searchInput"),
  directionBtns: document.querySelectorAll("[data-direction]"),
  backendBtns: document.querySelectorAll("[data-backend]"),
  webSearchBtns: document.querySelectorAll("[data-web-search]"),
  focusBtn: document.getElementById("focusBtn"),
  refreshBtn: document.getElementById("refreshBtn"),
  zoomInBtn: document.getElementById("zoomInBtn"),
  zoomOutBtn: document.getElementById("zoomOutBtn"),
  canvasHost: document.getElementById("canvasHost"),
  treeSvg: document.getElementById("treeSvg"),
  viewport: document.getElementById("viewport"),
  edgeLayer: document.getElementById("edgeLayer"),
  nodeLayer: document.getElementById("nodeLayer"),
  miniMap: document.getElementById("miniMap"),
  emptyState: document.getElementById("emptyState"),
  sideResizeHandle: document.getElementById("sideResizeHandle"),
  sidePanel: document.getElementById("sidePanel"),
  composer: document.getElementById("composer"),
  promptInput: document.getElementById("promptInput"),
  sendBtn: document.getElementById("sendBtn"),
  selectedFiles: document.getElementById("selectedFiles"),
  threadTarget: document.getElementById("threadTarget"),
  detailTitle: document.getElementById("detailTitle"),
  detailMeta: document.getElementById("detailMeta"),
  sideTabBtns: document.querySelectorAll("[data-side-tab]"),
  conversationPane: document.getElementById("conversationPane"),
  conversationList: document.getElementById("conversationList"),
  notePane: document.getElementById("notePane"),
  noteStatus: document.getElementById("noteStatus"),
  noteProjectBtn: document.getElementById("noteProjectBtn"),
  noteEditBtn: document.getElementById("noteEditBtn"),
  noteSaveBtn: document.getElementById("noteSaveBtn"),
  noteBox: document.getElementById("noteBox"),
  noteEditor: document.getElementById("noteEditor"),
  noteRendered: document.getElementById("noteRendered"),
  noteContextMenu: document.getElementById("noteContextMenu"),
  noteContextAppendBtn: document.getElementById("noteContextAppendBtn"),
  nodeNotePreview: document.getElementById("nodeNotePreview"),
  nodePromptPreview: document.getElementById("nodePromptPreview"),
  deleteBtn: document.getElementById("deleteBtn"),
  exportBtn: document.getElementById("exportBtn"),
  // ── documents / PDF ──
  homeBtn: document.getElementById("homeBtn"),
  breadcrumb: document.getElementById("breadcrumb"),
  appContent: document.getElementById("appContent"),
  homeScreen: document.getElementById("homeScreen"),
  homeGrid: document.getElementById("homeGrid"),
  homeNewBtn: document.getElementById("homeNewBtn"),
  workspaceScreen: document.getElementById("workspaceScreen"),
  wsTitle: document.getElementById("wsTitle"),
  wsDesc: document.getElementById("wsDesc"),
  wsRenameBtn: document.getElementById("wsRenameBtn"),
  wsAddPdfBtn: document.getElementById("wsAddPdfBtn"),
  wsDocGrid: document.getElementById("wsDocGrid"),
  wsFileList: document.getElementById("wsFileList"),
  documentSelect: document.getElementById("documentSelect"),
  docUploadInput: document.getElementById("docUploadInput"),
  docDeleteBtn: document.getElementById("docDeleteBtn"),
  pdfPane: document.getElementById("pdfPane"),
  pdfResizeHandle: document.getElementById("pdfResizeHandle"),
  pdfViewport: document.getElementById("pdfViewport"),
  pdfPages: document.getElementById("pdfPages"),
  pdfEmpty: document.getElementById("pdfEmpty"),
  pdfEmptyUploadBtn: document.getElementById("pdfEmptyUploadBtn"),
  pdfPageInfo: document.getElementById("pdfPageInfo"),
  pdfZoomInBtn: document.getElementById("pdfZoomInBtn"),
  pdfZoomOutBtn: document.getElementById("pdfZoomOutBtn"),
  treePane: document.getElementById("treePane"),
  selectionToolbar: document.getElementById("selectionToolbar"),
  selHighlightBtn: document.getElementById("selHighlightBtn"),
  selAskBtn: document.getElementById("selAskBtn"),
  highlightMenu: document.getElementById("highlightMenu"),
  hlGotoNodeBtn: document.getElementById("hlGotoNodeBtn"),
  hlDeleteBtn: document.getElementById("hlDeleteBtn"),
  togglePdfBtn: document.getElementById("togglePdfBtn"),
  toggleTreeBtn: document.getElementById("toggleTreeBtn"),
  toggleChatBtn: document.getElementById("toggleChatBtn"),
};

function svgEl(name) {
  return document.createElementNS("http://www.w3.org/2000/svg", name);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTokens(value) {
  const n = Number(value || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}m`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

function compact(text, limit = 140) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, limit - 1)}…`;
}

function compactFileName(name, head = 10, tail = 12) {
  const value = String(name || "");
  if (value.length <= head + tail + 3) return value;
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
}

function fileKindForName(name) {
  const lower = String(name || "").toLowerCase();
  if (lower.endsWith(".md") || lower.endsWith(".markdown") || lower.endsWith(".mdown")) return "markdown";
  if (lower.endsWith(".pdf")) return "pdf";
  if (/\.(txt|csv|json|jsonl|log|py|js|ts|css|html|xml|yaml|yml|toml|rst)$/i.test(lower)) return "text";
  return "file";
}

function workspaceFileUrl(fileOrPath, download = false) {
  const relPath = typeof fileOrPath === "string" ? fileOrPath : fileOrPath.path;
  const suffix = download ? "&download=1" : "";
  return `/api/workspace/file?path=${encodeURIComponent(relPath)}${suffix}`;
}

function workspaceContentUrl(fileOrPath) {
  const relPath = typeof fileOrPath === "string" ? fileOrPath : fileOrPath.path;
  return `/api/workspace/content?path=${encodeURIComponent(relPath)}`;
}

function workspaceRelPathFromHref(href) {
  const root = String(state.workDir || "").replace(/\/+$/, "");
  if (!root || !href) return "";
  const candidates = [];
  try {
    candidates.push(decodeURIComponent(String(href)));
  } catch {
    candidates.push(String(href));
  }
  try {
    const url = new URL(href, window.location.origin);
    candidates.push(decodeURIComponent(url.pathname));
  } catch {
    // Non-URL workspace paths are handled by the raw candidate.
  }
  for (const candidate of candidates) {
    const normalized = String(candidate || "").replace(/^file:\/+/, "/");
    if (normalized === root) return "";
    if (normalized.startsWith(`${root}/`)) return normalized.slice(root.length + 1);
  }
  return "";
}

function workspaceFileForPath(relPath) {
  const existing = state.workspaceFiles.find((file) => file.path === relPath);
  if (existing) return existing;
  const name = relPath.split("/").pop() || relPath;
  return {
    path: relPath,
    name,
    dir: relPath.includes("/") ? relPath.slice(0, relPath.lastIndexOf("/")) : "",
    size: 0,
    modified_at: "",
    mime: "",
    kind: fileKindForName(name),
  };
}

function workspaceRefFromFile(file) {
  return {
    path: file.path,
    absolute_path: file.absolute_path || (state.workDir ? `${state.workDir.replace(/\/+$/, "")}/${file.path}` : file.path),
    name: file.name,
    kind: file.kind || fileKindForName(file.name),
    mime: file.mime || "",
    size: Number(file.size || 0),
  };
}

function appendInlineMarkdown(parent, text) {
  const source = String(text || "");
  const pattern = /(\[[^\]]+\]\([^)]+\)|`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
  let lastIndex = 0;
  let match;

  function appendText(value) {
    if (value) parent.appendChild(document.createTextNode(value));
  }

  while ((match = pattern.exec(source))) {
    appendText(source.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.appendChild(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      parent.appendChild(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      parent.appendChild(emphasis);
    } else {
      const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (!linkMatch) {
        appendText(token);
      } else {
        const [, label, href] = linkMatch;
        let safeUrl = null;
        const workspaceRelPath = workspaceRelPathFromHref(href);
        try {
          const url = new URL(href, window.location.origin);
          if (["http:", "https:", "mailto:"].includes(url.protocol)) safeUrl = url.href;
        } catch {
          safeUrl = null;
        }
        if (workspaceRelPath) {
          const link = document.createElement("a");
          const file = workspaceFileForPath(workspaceRelPath);
          link.href = workspaceFileUrl(file);
          link.textContent = label;
          link.addEventListener("click", (event) => {
            event.preventDefault();
            openWorkspaceFile(file).catch((err) => window.alert(err.message));
          });
          parent.appendChild(link);
        } else if (!safeUrl) {
          appendText(label);
        } else {
          const link = document.createElement("a");
          link.href = safeUrl;
          link.textContent = label;
          if (!safeUrl.startsWith(window.location.origin)) {
            link.target = "_blank";
            link.rel = "noopener noreferrer";
          }
          parent.appendChild(link);
        }
      }
    }
    lastIndex = pattern.lastIndex;
  }
  appendText(source.slice(lastIndex));
}

function renderMarkdown(text) {
  const root = document.createElement("div");
  root.className = "markdown-body";
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let paragraph = [];
  let list = null;
  let listType = "";

  function isTableSeparator(value) {
    const cells = splitTableRow(value);
    if (cells.length < 2) return false;
    return cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
  }

  function splitTableRow(value) {
    let row = String(value || "").trim();
    if (!row.includes("|")) return [];
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((cell) => cell.trim());
  }

  function flushParagraph() {
    const content = paragraph.join(" ").trim();
    paragraph = [];
    if (!content) return;
    const p = document.createElement("p");
    appendInlineMarkdown(p, content);
    root.appendChild(p);
  }

  function resetList() {
    list = null;
    listType = "";
  }

  function ensureList(type) {
    if (list && listType === type) return list;
    flushParagraph();
    list = document.createElement(type);
    listType = type;
    root.appendChild(list);
    return list;
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    const fence = trimmed.match(/^```(.*)$/);
    if (fence) {
      flushParagraph();
      resetList();
      const language = fence[1].trim();
      const codeLines = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i += 1;
      }
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (language) code.dataset.language = language;
      code.textContent = codeLines.join("\n");
      pre.appendChild(code);
      root.appendChild(pre);
      continue;
    }

    if (!trimmed) {
      flushParagraph();
      resetList();
      continue;
    }

    if (/^---+$/.test(trimmed)) {
      flushParagraph();
      resetList();
      root.appendChild(document.createElement("hr"));
      continue;
    }

    if (trimmed.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const headers = splitTableRow(trimmed);
      const alignments = splitTableRow(lines[i + 1]).map((cell) => {
        const value = cell.trim();
        if (value.startsWith(":") && value.endsWith(":")) return "center";
        if (value.endsWith(":")) return "right";
        return "left";
      });
      if (headers.length) {
        flushParagraph();
        resetList();
        const wrap = document.createElement("div");
        wrap.className = "markdown-table-wrap";
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        for (const [index, header] of headers.entries()) {
          const th = document.createElement("th");
          th.style.textAlign = alignments[index] || "left";
          appendInlineMarkdown(th, header);
          headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        i += 2;
        while (i < lines.length && lines[i].trim().includes("|") && lines[i].trim()) {
          const cells = splitTableRow(lines[i]);
          const tr = document.createElement("tr");
          for (let index = 0; index < headers.length; index += 1) {
            const td = document.createElement("td");
            td.style.textAlign = alignments[index] || "left";
            appendInlineMarkdown(td, cells[index] || "");
            tr.appendChild(td);
          }
          tbody.appendChild(tr);
          i += 1;
        }
        i -= 1;
        table.appendChild(tbody);
        wrap.appendChild(table);
        root.appendChild(wrap);
        continue;
      }
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      resetList();
      const level = Math.min(heading[1].length, 6);
      const h = document.createElement(`h${level}`);
      appendInlineMarkdown(h, heading[2]);
      root.appendChild(h);
      continue;
    }

    const quote = trimmed.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      resetList();
      const blockquote = document.createElement("blockquote");
      const quoted = [];
      while (i < lines.length) {
        const quoteLine = lines[i].trim().match(/^>\s?(.*)$/);
        if (!quoteLine) break;
        quoted.push(quoteLine[1]);
        i += 1;
      }
      i -= 1;
      appendInlineMarkdown(blockquote, quoted.join(" "));
      root.appendChild(blockquote);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      const ul = ensureList("ul");
      const li = document.createElement("li");
      appendInlineMarkdown(li, unordered[1]);
      ul.appendChild(li);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const ol = ensureList("ol");
      const li = document.createElement("li");
      appendInlineMarkdown(li, ordered[1]);
      ol.appendChild(li);
      continue;
    }

    resetList();
    paragraph.push(line);
  }

  flushParagraph();
  if (!root.childElementCount && text) {
    const p = document.createElement("p");
    p.textContent = String(text);
    root.appendChild(p);
  }
  return root;
}

function statusText(status) {
  return {
    queued: "Queued",
    running: "Running",
    done: "Done",
    failed: "Failed",
  }[status] || status || "-";
}

function isNodeActive(node) {
  return node && ["queued", "running"].includes(node.status);
}

function selectedNode() {
  return state.nodeById.get(state.selectedId) || null;
}

function currentWorkspace() {
  return state.workspaces.find((w) => w.id === state.currentWorkspaceId) || null;
}

async function fetchWorkspaces() {
  try {
    const res = await fetch("/api/workspaces");
    const data = await res.json();
    state.workspaces = data.workspaces || [];
  } catch (err) {
    console.error(err);
    state.workspaces = [];
  }
  // Validate the current workspace: fall back to the first one, or create a default if none exist.
  if (state.currentWorkspaceId && !state.workspaces.some((w) => w.id === state.currentWorkspaceId)) {
    state.currentWorkspaceId = null;
  }
  if (!state.currentWorkspaceId && state.workspaces.length) {
    state.currentWorkspaceId = state.workspaces[0].id;
  }
  if (!state.workspaces.length) {
    const created = await createWorkspace("My workspace", { silent: true });
    if (created) state.currentWorkspaceId = created.id;
  }
  if (state.currentWorkspaceId) localStorage.setItem("currentWorkspaceId", state.currentWorkspaceId);
  else localStorage.removeItem("currentWorkspaceId");
}

async function createWorkspace(title, { silent = false } = {}) {
  const name = String(title || "").trim();
  if (!name) return null;
  try {
    const res = await fetch("/api/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: name }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || "Create failed");
    // Refresh the local list so counts/order stay consistent.
    const list = await fetch("/api/workspaces").then((r) => r.json()).catch(() => ({}));
    state.workspaces = list.workspaces || state.workspaces;
    return data.workspace;
  } catch (err) {
    if (!silent) window.alert(err.message);
    return null;
  }
}

// ── Routing / views (home → workspace → reader) ─────────────────────────

function parseRoute() {
  const raw = (location.hash || "").replace(/^#/, "");
  const parts = raw.split("/").filter(Boolean); // ["w","<id>","d","<docId>"]
  if (parts[0] === "w" && parts[1]) {
    if (parts[2] === "d" && parts[3]) return { view: "reader", wsId: parts[1], docId: parts[3] };
    return { view: "workspace", wsId: parts[1] };
  }
  return { view: "home" };
}

function routeHash(view, { wsId, docId } = {}) {
  if (view === "workspace" && wsId) return `#/w/${wsId}`;
  if (view === "reader" && wsId && docId) return `#/w/${wsId}/d/${docId}`;
  return "#/";
}

function navigate(view, opts = {}) {
  const hash = routeHash(view, opts);
  if (location.hash === hash || (!location.hash && hash === "#/")) {
    applyRoute().catch(console.error); // same hash → no hashchange event, apply manually
  } else {
    location.hash = hash;
  }
}

async function applyRoute() {
  const route = parseRoute();
  state.view = route.view;
  applyView();

  if (route.view === "home") {
    await fetchWorkspaces();
    renderHome();
  } else if (route.view === "workspace") {
    await fetchWorkspaces();
    if (!state.workspaces.some((w) => w.id === route.wsId)) return navigate("home");
    setCurrentWorkspace(route.wsId);
    await fetchWorkspaceDetail(route.wsId);
    renderWorkspaceDetail();
  } else if (route.view === "reader") {
    await fetchWorkspaces();
    if (!state.workspaces.some((w) => w.id === route.wsId)) return navigate("home");
    setCurrentWorkspace(route.wsId);
    await fetchDocuments();
    if (!state.documents.some((d) => d.id === route.docId)) {
      return navigate("workspace", { wsId: route.wsId });
    }
    await selectDocument(route.docId, { keepNode: false });
  }
  renderBreadcrumb();
  try {
    localStorage.setItem("lastRoute", location.hash || "#/");
  } catch {}
}

function setCurrentWorkspace(wsId) {
  state.currentWorkspaceId = wsId;
  try {
    localStorage.setItem("currentWorkspaceId", wsId);
  } catch {}
}

function applyView() {
  if (els.appContent) els.appContent.className = "app-content view-" + state.view;
  document.body.classList.toggle("hide-reader-controls", state.view !== "reader");
}

function refreshCurrentScreen() {
  renderBreadcrumb();
  if (state.view === "home") renderHome();
  else if (state.view === "workspace") renderWorkspaceDetail();
}

function renderBreadcrumb() {
  if (!els.breadcrumb) return;
  const crumbs = [];
  const add = (label, { onClick = null, current = false } = {}) => {
    if (crumbs.length) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "▸";
      crumbs.push(sep);
    }
    const el = document.createElement(onClick ? "button" : "span");
    el.className = "crumb" + (current ? " current" : "");
    el.textContent = label;
    if (onClick) {
      el.type = "button";
      el.addEventListener("click", onClick);
    }
    crumbs.push(el);
  };

  if (state.view === "home") {
    add("Workspaces", { current: true });
  } else if (state.view === "workspace") {
    const ws = currentWorkspace() || state.wsDetail.workspace;
    add("Workspaces", { onClick: () => navigate("home") });
    add(ws ? ws.title || "Untitled" : "…", { current: true });
  } else if (state.view === "reader") {
    const ws = currentWorkspace() || state.wsDetail.workspace;
    const doc = currentDocument();
    add("Workspaces", { onClick: () => navigate("home") });
    add(ws ? ws.title || "Untitled" : "…", {
      onClick: () => navigate("workspace", { wsId: state.currentWorkspaceId }),
    });
    add(doc ? compact(doc.title || doc.original_name || "Untitled", 42) : "…", { current: true });
  }
  els.breadcrumb.replaceChildren(...crumbs);
}

// ── Home: wall of workspace cards ────────────────────────────────────────

function renderHome() {
  if (!els.homeGrid) return;
  if (!state.workspaces.length) {
    const empty = document.createElement("div");
    empty.className = "screen-empty";
    empty.textContent = "No workspaces yet. Create one to start adding PDFs.";
    els.homeGrid.replaceChildren(empty);
    return;
  }
  const cards = state.workspaces.map((ws) => {
    const card = document.createElement("div");
    card.className = "ws-card";

    const body = document.createElement("button");
    body.type = "button";
    body.className = "ws-card-body";
    body.addEventListener("click", () => navigate("workspace", { wsId: ws.id }));
    const title = document.createElement("div");
    title.className = "ws-card-title";
    title.textContent = ws.title || "Untitled";
    const meta = document.createElement("div");
    meta.className = "ws-card-meta";
    const when = (ws.last_opened_at || "").slice(0, 10);
    meta.textContent = `${ws.document_count || 0} PDF` + (when ? `  ·  ${when}` : "");
    body.append(title, meta);
    if (ws.description) {
      const desc = document.createElement("div");
      desc.className = "ws-card-desc";
      desc.textContent = ws.description;
      body.append(desc);
    }

    const actions = document.createElement("div");
    actions.className = "ws-card-actions";
    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "ws-btn";
    renameBtn.textContent = "Rename";
    renameBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      renameWorkspace(ws).catch(console.error);
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "ws-btn danger";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeWorkspace(ws).catch(console.error);
    });
    actions.append(renameBtn, delBtn);

    card.append(body, actions);
    return card;
  });
  els.homeGrid.replaceChildren(...cards);
}

// ── Workspace detail: PDF list + shared file drawer ──────────────────────

async function fetchWorkspaceDetail(wsId) {
  try {
    const res = await fetch(`/api/workspaces/${wsId}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || "Load failed");
    state.wsDetail = {
      workspace: data.workspace || null,
      documents: data.documents || [],
      files: data.files || [],
    };
  } catch (err) {
    console.error(err);
    state.wsDetail = { workspace: null, documents: [], files: [] };
  }
}

function renderWorkspaceDetail() {
  const ws = currentWorkspace() || state.wsDetail.workspace;
  if (els.wsTitle) els.wsTitle.textContent = ws ? ws.title || "Untitled" : "";
  if (els.wsDesc) {
    els.wsDesc.textContent = ws && ws.description ? ws.description : "";
    els.wsDesc.hidden = !(ws && ws.description);
  }

  if (els.wsDocGrid) {
    const docs = state.wsDetail.documents;
    if (!docs.length) {
      const empty = document.createElement("div");
      empty.className = "screen-empty";
      empty.textContent = "No PDFs yet. Click “+ PDF” to add one.";
      els.wsDocGrid.replaceChildren(empty);
    } else {
      const cards = docs.map((doc) => {
        const card = document.createElement("div");
        card.className = "doc-card";
        const body = document.createElement("button");
        body.type = "button";
        body.className = "doc-card-body";
        body.addEventListener("click", () =>
          navigate("reader", { wsId: state.currentWorkspaceId, docId: doc.id })
        );
        const t = document.createElement("div");
        t.className = "doc-card-title";
        t.textContent = doc.title || doc.original_name || "Untitled";
        const m = document.createElement("div");
        m.className = "doc-card-meta";
        m.textContent = doc.num_pages ? `${doc.num_pages} pages` : "PDF";
        body.append(t, m);
        const del = document.createElement("button");
        del.type = "button";
        del.className = "doc-card-del";
        del.title = "Delete PDF";
        del.textContent = "⌫";
        del.addEventListener("click", (e) => {
          e.stopPropagation();
          deleteDocument(doc).catch(console.error);
        });
        card.append(body, del);
        return card;
      });
      els.wsDocGrid.replaceChildren(...cards);
    }
  }

  if (els.wsFileList) {
    const files = state.wsDetail.files;
    if (!files.length) {
      const empty = document.createElement("div");
      empty.className = "screen-empty";
      empty.textContent = "No shared files in this workspace.";
      els.wsFileList.replaceChildren(empty);
    } else {
      const rows = files.map((f) => {
        const row = document.createElement("a");
        row.className = "ws-file-row";
        row.href = `/api/files/${f.id}/download`;
        const name = document.createElement("span");
        name.className = "ws-file-name";
        name.textContent = f.original_name;
        const sub = document.createElement("span");
        sub.className = "ws-file-sub";
        sub.textContent = formatBytes(f.size);
        row.append(name, sub);
        return row;
      });
      els.wsFileList.replaceChildren(...rows);
    }
  }
}

async function renameWorkspace(ws) {
  const next = window.prompt("Rename workspace", ws.title || "");
  if (next == null) return;
  const title = next.trim();
  if (!title || title === ws.title) return;
  try {
    const res = await fetch(`/api/workspaces/${ws.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "Rename failed");
    }
    await fetchWorkspaces();
    if (state.view === "workspace" && state.currentWorkspaceId === ws.id) {
      await fetchWorkspaceDetail(ws.id);
    }
    refreshCurrentScreen();
  } catch (err) {
    window.alert(err.message);
  }
}

async function removeWorkspace(ws) {
  const warn = `Delete workspace "${ws.title}"?\nIts ${ws.document_count || 0} PDF(s), with all highlights and question trees, will be permanently deleted.`;
  if (!window.confirm(warn)) return;
  try {
    const res = await fetch(`/api/workspaces/${ws.id}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "Delete failed");
    }
    if (state.currentWorkspaceId === ws.id) {
      state.currentWorkspaceId = null;
      try {
        localStorage.removeItem("currentWorkspaceId");
      } catch {}
    }
    await fetchWorkspaces();
    // If we deleted the workspace we're viewing → go home; otherwise refresh the current page.
    if (state.view !== "home" && parseRoute().wsId === ws.id) {
      navigate("home");
    } else {
      refreshCurrentScreen();
    }
  } catch (err) {
    window.alert(err.message);
  }
}

async function deleteDocument(doc) {
  if (!doc) return;
  if (
    !window.confirm(
      `Delete "${doc.title || doc.original_name}"?\nAll of its highlights and question nodes will be deleted too, and this can't be undone.`
    )
  )
    return;
  try {
    const res = await fetch(`/api/documents/${doc.id}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "Delete failed");
    }
    await fetchWorkspaces();
    if (state.currentWorkspaceId) await fetchWorkspaceDetail(state.currentWorkspaceId);
    renderWorkspaceDetail();
  } catch (err) {
    window.alert(err.message);
  }
}

async function fetchDocuments() {
  const wsId = state.currentWorkspaceId;
  try {
    const url = wsId ? `/api/documents?workspace_id=${encodeURIComponent(wsId)}` : "/api/documents";
    const res = await fetch(url);
    const data = await res.json();
    state.documents = data.documents || [];
  } catch (err) {
    console.error(err);
    state.documents = [];
  }
  // Validate the current document: fall back to the first one (or null).
  if (state.currentDocId && !state.documents.some((d) => d.id === state.currentDocId)) {
    state.currentDocId = null;
  }
  if (!state.currentDocId && state.documents.length) {
    state.currentDocId = state.documents[0].id;
  }
  if (state.currentDocId) localStorage.setItem("currentDocId", state.currentDocId);
  else localStorage.removeItem("currentDocId");
}

function nodesForDocument(docId) {
  if (!docId) return [];
  return state.allNodes
    .filter((node) => node.document_id === docId)
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
}

async function fetchTree({ keepSelection = true } = {}) {
  const res = await fetch("/api/tree");
  const data = await res.json();
  state.allNodes = data.nodes || [];
  // Filter files by the current workspace: state.files holds only this workspace's uploads,
  // while state.allFiles keeps the full set. Unowned legacy files (null workspace_id) join none.
  state.allFiles = data.files || [];
  state.files = state.currentWorkspaceId
    ? state.allFiles.filter((f) => f.workspace_id === state.currentWorkspaceId)
    : state.allFiles;
  state.workDir = data.work_dir || "";
  state.nodes = nodesForDocument(state.currentDocId);
  rebuildIndexes();

  if (!keepSelection || (state.selectedId && !state.nodeById.has(state.selectedId))) {
    state.selectedId = null;
  }
  if (state.parentId && !state.nodeById.has(state.parentId)) {
    state.parentId = state.selectedId;
  }
  if (!state.selectedId && state.nodes.length) {
    state.selectedId = state.nodes[0].id;
    state.parentId = state.selectedId;
  }
  render();
}

async function fetchHighlights() {
  if (!state.currentDocId) {
    state.highlights = [];
    renderHighlights();
    return;
  }
  try {
    const res = await fetch(`/api/documents/${state.currentDocId}/highlights`);
    const data = await res.json();
    state.highlights = data.highlights || [];
  } catch (err) {
    console.error(err);
    state.highlights = [];
  }
  renderHighlights();
}

async function selectDocument(docId, { keepNode = false } = {}) {
  state.currentDocId = docId || null;
  if (docId) localStorage.setItem("currentDocId", docId);
  else localStorage.removeItem("currentDocId");
  if (!keepNode) {
    state.selectedId = null;
    state.parentId = null;
  }
  state.pendingSelection = null;
  state.activeHighlightId = null;
  await Promise.all([fetchTree({ keepSelection: keepNode }), fetchHighlights(), loadPdf()]);
  renderDocumentSelect();
  renderComposer();
}

function rebuildIndexes() {
  state.nodeById = new Map(state.nodes.map((node) => [node.id, node]));
  state.childrenByParent = new Map();
  for (const node of state.nodes) {
    const key = node.parent_id || "__root__";
    if (!state.childrenByParent.has(key)) state.childrenByParent.set(key, []);
    state.childrenByParent.get(key).push(node);
  }
  for (const children of state.childrenByParent.values()) {
    children.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  }
}

function render() {
  layoutTree();
  computeSelectedPath();
  assignBranchColors();
  applyTransform();
  renderTree();
  renderMiniMap();
  renderConversation();
  renderSideTabs();
  renderNotePanel();
  renderComposer();
  renderBreadcrumb();
  renderDocumentSelect();
  renderDirectionControls();
  renderBackendControls();
  renderWebSearchControls();
  renderHighlights();
  els.emptyState.classList.toggle("visible", state.nodes.length === 0);
}

function currentDocument() {
  return state.documents.find((d) => d.id === state.currentDocId) || null;
}

function renderDocumentSelect() {
  if (!els.documentSelect) return;
  const options = [];
  if (!state.documents.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Upload PDF →";
    options.push(empty);
  }
  for (const doc of state.documents) {
    const option = document.createElement("option");
    option.value = doc.id;
    option.textContent = compact(doc.title || doc.original_name || "Untitled", 36);
    options.push(option);
  }
  els.documentSelect.replaceChildren(...options);
  els.documentSelect.value = state.currentDocId || "";
  if (els.docDeleteBtn) els.docDeleteBtn.disabled = !state.currentDocId;
}

function layoutTree() {
  state.layout = new Map();
  const roots = state.nodes.filter((node) => !node.parent_id || !state.nodeById.has(node.parent_id));
  let leaf = 0;
  const visited = new Set();

  function walk(node, depth) {
    const breadthGap = state.treeDirection === "right" ? Y_GAP : X_GAP;
    if (!node || visited.has(node.id)) return leaf * breadthGap;
    visited.add(node.id);
    const children = state.childrenByParent.get(node.id) || [];
    let breadth;
    if (!children.length) {
      breadth = leaf * breadthGap;
      leaf += 1;
    } else {
      const positions = children.map((child) => walk(child, depth + 1));
      breadth = positions.reduce((sum, item) => sum + item, 0) / positions.length;
    }
    const pos = state.treeDirection === "right"
      ? { x: depth * X_GAP, y: breadth, depth }
      : { x: breadth, y: depth * Y_GAP, depth };
    state.layout.set(node.id, pos);
    return breadth;
  }

  for (const root of roots) {
    walk(root, 0);
    leaf += 0.6;
  }
}

function computeSelectedPath() {
  state.selectedPath = new Set();
  let current = selectedNode();
  while (current) {
    state.selectedPath.add(current.id);
    current = state.nodeById.get(current.parent_id);
  }
}

function branchKey(node) {
  let current = node;
  let parent = state.nodeById.get(current.parent_id);
  while (parent && parent.parent_id) {
    current = parent;
    parent = state.nodeById.get(parent.parent_id);
  }
  return current.id;
}

function assignBranchColors() {
  state.branchColors = new Map();
  let index = 0;
  const ordered = [...state.nodes].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  for (const node of ordered) {
    const key = branchKey(node);
    if (!state.branchColors.has(key)) {
      state.branchColors.set(key, PALETTE[index % PALETTE.length]);
      index += 1;
    }
  }
}

function colorFor(node) {
  return state.branchColors.get(branchKey(node)) || PALETTE[0];
}

function matchesSearch(node) {
  const query = state.search.trim().toLowerCase();
  if (!query) return true;
  const text = `${node.title || ""} ${node.prompt || ""} ${node.answer || ""}`.toLowerCase();
  return text.includes(query);
}

function shouldDimNode(node) {
  if (state.search && !matchesSearch(node)) return true;
  if (isNodeActive(node)) return false;
  if (state.selectedId && state.selectedPath.size && !state.selectedPath.has(node.id)) {
    const selected = selectedNode();
    return selected && node.parent_id !== selected.id && selected.parent_id !== node.id;
  }
  return false;
}

function renderTree() {
  els.edgeLayer.replaceChildren();
  els.nodeLayer.replaceChildren();

  for (const node of state.nodes) {
    if (!node.parent_id) continue;
    const parent = state.nodeById.get(node.parent_id);
    if (!parent) continue;
    const from = state.layout.get(parent.id);
    const to = state.layout.get(node.id);
    if (!from || !to) continue;
    const path = svgEl("path");
    if (state.treeDirection === "right") {
      const sx = from.x + NODE_W;
      const sy = from.y + NODE_H / 2;
      const tx = to.x;
      const ty = to.y + NODE_H / 2;
      const midX = sx + (tx - sx) / 2;
      path.setAttribute("d", `M${sx},${sy} C${midX},${sy} ${midX},${ty} ${tx},${ty}`);
    } else {
      const sx = from.x + NODE_W / 2;
      const sy = from.y + NODE_H;
      const tx = to.x + NODE_W / 2;
      const ty = to.y;
      const midY = sy + (ty - sy) / 2;
      path.setAttribute("d", `M${sx},${sy} C${sx},${midY} ${tx},${midY} ${tx},${ty}`);
    }
    path.setAttribute("class", edgeClass(parent, node));
    path.style.setProperty("--edge-color", colorFor(node));
    els.edgeLayer.appendChild(path);
  }

  for (const node of state.nodes) {
    const pos = state.layout.get(node.id);
    if (!pos) continue;
    const foreign = svgEl("foreignObject");
    foreign.setAttribute("x", pos.x - NODE_OVERFLOW);
    foreign.setAttribute("y", pos.y - NODE_OVERFLOW);
    foreign.setAttribute("width", NODE_W + NODE_OVERFLOW * 2);
    foreign.setAttribute("height", NODE_H + NODE_OVERFLOW * 2);
    const card = makeNodeCard(node);
    card.style.margin = `${NODE_OVERFLOW}px`;
    foreign.appendChild(card);
    els.nodeLayer.appendChild(foreign);
  }
}

function edgeClass(parent, node) {
  const active = state.selectedPath.has(parent.id) && state.selectedPath.has(node.id);
  const dimmed = state.selectedId && !active;
  return ["edge", active ? "active" : "", dimmed ? "dimmed" : ""].filter(Boolean).join(" ");
}

function positionNodeNotePreview(target) {
  const rect = target.getBoundingClientRect();
  const preview = els.nodeNotePreview;
  const margin = 10;
  const width = preview.offsetWidth || 260;
  const height = preview.offsetHeight || 160;
  const left = clamp(rect.left, margin, window.innerWidth - width - margin);
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - margin) {
    top = rect.top - height - 8;
  }
  preview.style.left = `${left}px`;
  preview.style.top = `${clamp(top, margin, window.innerHeight - height - margin)}px`;
}

let nodeNotePreviewHideTimer = null;

function cancelNodeNotePreviewHide() {
  if (nodeNotePreviewHideTimer) {
    clearTimeout(nodeNotePreviewHideTimer);
    nodeNotePreviewHideTimer = null;
  }
}

function showNodeNotePreview(node, target) {
  const note = String(noteTextForNode(node) || "").trim();
  if (!note) return;
  cancelNodeNotePreviewHide();
  els.nodeNotePreview.replaceChildren(renderMarkdown(note));
  els.nodeNotePreview.hidden = false;
  positionNodeNotePreview(target);
}

// Short grace delay just bridges the ~8px visual gap between the node card and the
// preview so the cursor can transit onto the preview to scroll. Effectively instant.
function hideNodeNotePreview() {
  cancelNodeNotePreviewHide();
  nodeNotePreviewHideTimer = setTimeout(() => {
    els.nodeNotePreview.hidden = true;
    nodeNotePreviewHideTimer = null;
  }, 180);
}

function hideNodeNotePreviewImmediate() {
  cancelNodeNotePreviewHide();
  els.nodeNotePreview.hidden = true;
}

function noteTextForNode(node) {
  if (!node) return "";
  if (state.noteDraftNodeId === node.id) return state.noteDraft || "";
  return node.note_md || "";
}

function positionNodePromptPreview(target) {
  const rect = target.getBoundingClientRect();
  const preview = els.nodePromptPreview;
  const margin = 10;
  const width = preview.offsetWidth || 360;
  const height = preview.offsetHeight || 220;
  const left = clamp(rect.left, margin, window.innerWidth - width - margin);
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - margin) {
    top = rect.top - height - 8;
  }
  preview.style.left = `${left}px`;
  preview.style.top = `${clamp(top, margin, window.innerHeight - height - margin)}px`;
}

function showNodePromptPreview(node, target) {
  const prompt = String(node.prompt || "").trim();
  if (!prompt) return;
  const pre = document.createElement("pre");
  pre.textContent = prompt;
  els.nodePromptPreview.replaceChildren(pre);
  els.nodePromptPreview.hidden = false;
  positionNodePromptPreview(target);
}

function hideNodePromptPreview() {
  els.nodePromptPreview.hidden = true;
}

function openNodeNote(nodeId) {
  state.activeSideTab = "notes";
  hideNodePromptPreview();
  selectNode(nodeId);
}

function makeNodeCard(node) {
  const card = document.createElement("div");
  card.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
  card.className = [
    "node-card",
    node.id === state.selectedId ? "selected" : "",
    shouldDimNode(node) ? "dimmed" : "",
    isNodeActive(node) ? "running" : "",
  ]
    .filter(Boolean)
    .join(" ");
  card.style.setProperty("--node-color", colorFor(node));
  card.dataset.nodeId = node.id;
  const noteText = String(noteTextForNode(node)).trim();

  card.addEventListener("mouseenter", (event) => {
    if (event.target.closest && event.target.closest(".node-note-button")) return;
    hideNodeNotePreviewImmediate();
    showNodePromptPreview(node, card);
  });
  card.addEventListener("mousemove", (event) => {
    if (event.target.closest && event.target.closest(".node-note-button")) {
      hideNodePromptPreview();
      return;
    }
    positionNodePromptPreview(card);
  });
  card.addEventListener("mouseleave", () => {
    hideNodePromptPreview();
    if (noteText) hideNodeNotePreview();
  });

  if (noteText) {
    const noteButton = document.createElement("button");
    noteButton.type = "button";
    noteButton.className = "node-note-button";
    noteButton.setAttribute("aria-label", "Notes");
    noteButton.setAttribute("title", "");
    noteButton.textContent = "N";
    noteButton.addEventListener("mouseenter", (event) => {
      hideNodePromptPreview();
      showNodeNotePreview(node, event.currentTarget);
    });
    noteButton.addEventListener("mousemove", (event) => positionNodeNotePreview(event.currentTarget));
    noteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      hideNodeNotePreviewImmediate();
      openNodeNote(node.id);
    });
    card.appendChild(noteButton);
  }

  const top = document.createElement("div");
  top.className = "node-top";
  const title = document.createElement("div");
  title.className = "node-title";
  title.textContent = node.title || "Node";
  const markers = document.createElement("div");
  markers.className = "node-markers";
  const dot = document.createElement("span");
  dot.className = `status-dot status-${node.status || "queued"}`;
  markers.appendChild(dot);
  top.append(title, markers);

  const snippet = document.createElement("div");
  snippet.className = "node-snippet";
  snippet.textContent = compact(node.answer || node.error || node.prompt, 120);

  const bottom = document.createElement("div");
  bottom.className = "node-bottom";
  const badges = document.createElement("div");
  badges.className = "node-badges";
  const children = state.childrenByParent.get(node.id) || [];
  for (const value of [`${formatTokens(node.total_tokens)} tok`, `${node.tools_count || 0} tool`, `${children.length} fork`]) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = value;
    badges.appendChild(badge);
  }
  const status = document.createElement("span");
  status.className = "node-status-label";
  status.textContent = statusText(node.status);
  bottom.append(badges, status);
  const content = document.createElement("div");
  content.className = "node-content";
  content.append(top, snippet, bottom);
  card.appendChild(content);

  card.addEventListener("click", (event) => {
    event.stopPropagation();
    selectNode(node.id);
  });
  card.addEventListener("dblclick", (event) => {
    event.stopPropagation();
    setParent(node.id);
  });
  return card;
}

function selectNode(id) {
  hideNodePromptPreview();
  hideNodeNotePreviewImmediate();
  state.selectedId = id;
  state.parentId = id;
  render();
  fetchWorkspaceFiles().catch(console.error);
  revealNodeSourceHighlight(state.nodeById.get(id));
}

function setParent(id) {
  state.parentId = id || null;
  state.selectedId = id || state.selectedId;
  renderComposer();
  els.promptInput.focus();
}

function applyTransform() {
  els.viewport.setAttribute("transform", `translate(${state.transform.x} ${state.transform.y}) scale(${state.transform.k})`);
}

const MIN_K = 0.05;
const MAX_K = 2.4;

function zoomAt(factor, clientX, clientY) {
  const rect = els.treeSvg.getBoundingClientRect();
  const px = clientX - rect.left;
  const py = clientY - rect.top;
  const beforeX = (px - state.transform.x) / state.transform.k;
  const beforeY = (py - state.transform.y) / state.transform.k;
  const nextK = clamp(state.transform.k * factor, MIN_K, MAX_K);
  state.transform.x = px - beforeX * nextK;
  state.transform.y = py - beforeY * nextK;
  state.transform.k = nextK;
  applyTransform();
  renderMiniMap();
}

function fitTreeToView({ margin = 60, allowZoomIn = false } = {}) {
  if (!state.layout.size) return;
  const rect = els.canvasHost.getBoundingClientRect();
  const bounds = treeBounds();
  const fitK = Math.min(
    (rect.width - margin * 2) / Math.max(bounds.w, 1),
    (rect.height - margin * 2) / Math.max(bounds.h, 1),
  );
  const targetK = allowZoomIn ? fitK : Math.min(fitK, 1);
  state.transform.k = clamp(targetK, MIN_K, MAX_K);
  state.transform.x = rect.width / 2 - (bounds.x + bounds.w / 2) * state.transform.k;
  state.transform.y = rect.height / 2 - (bounds.y + bounds.h / 2) * state.transform.k;
  applyTransform();
  renderMiniMap();
}

function focusSelected() {
  fitTreeToView();
}

function treeBounds() {
  if (!state.layout.size) return { x: 0, y: 0, w: NODE_W, h: NODE_H };
  const xs = [];
  const ys = [];
  for (const pos of state.layout.values()) {
    xs.push(pos.x, pos.x + NODE_W);
    ys.push(pos.y, pos.y + NODE_H);
  }
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function miniMapGeometry() {
  const canvas = els.miniMap;
  const cssW = canvas.clientWidth || 180;
  const cssH = canvas.clientHeight || 116;
  if (!state.layout.size) return { cssW, cssH, scale: 0, tx: 0, ty: 0 };
  const bounds = treeBounds();
  const pad = 12;
  const scale = Math.min(
    (cssW - pad * 2) / Math.max(bounds.w, 1),
    (cssH - pad * 2) / Math.max(bounds.h, 1),
  );
  return {
    cssW,
    cssH,
    scale,
    tx: pad - bounds.x * scale,
    ty: pad - bounds.y * scale,
  };
}

function renderMiniMap() {
  const canvas = els.miniMap;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const geo = miniMapGeometry();
  const { cssW, cssH, scale, tx, ty } = geo;
  if (canvas.width !== cssW * dpr || canvas.height !== cssH * dpr) {
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "rgba(255, 253, 250, 0.92)";
  ctx.fillRect(0, 0, cssW, cssH);
  if (!state.layout.size) return;

  ctx.lineWidth = 1;
  for (const node of state.nodes) {
    const pos = state.layout.get(node.id);
    if (!pos) continue;
    ctx.fillStyle = node.id === state.selectedId ? colorFor(node) : "rgba(32, 33, 36, 0.34)";
    ctx.fillRect(tx + pos.x * scale, ty + pos.y * scale, Math.max(3, NODE_W * scale), Math.max(3, NODE_H * scale));
  }

  const host = els.canvasHost.getBoundingClientRect();
  const vx = (-state.transform.x / state.transform.k) * scale + tx;
  const vy = (-state.transform.y / state.transform.k) * scale + ty;
  const vw = (host.width / state.transform.k) * scale;
  const vh = (host.height / state.transform.k) * scale;
  ctx.strokeStyle = "#202124";
  ctx.strokeRect(vx, vy, vw, vh);
}

function selectedConversationPath() {
  const path = [];
  let current = selectedNode();
  const seen = new Set();
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    path.push(current);
    current = state.nodeById.get(current.parent_id);
  }
  return path.reverse();
}

function describeTool(item) {
  const type = item.type || "tool";
  if (type === "command_execution") {
    return {
      name: "Bash",
      detail: item.command || "Preparing to run a command",
      output: item.aggregated_output || "",
      status: item.status || "in_progress",
    };
  }
  if (type === "web_search") {
    const action = item.action || {};
    const query = item.query || action.query || (Array.isArray(action.queries) ? action.queries.join(", ") : "");
    return {
      name: "Search",
      detail: query || "Preparing to search",
      output: "",
      status: item.status || "in_progress",
    };
  }
  if (type === "summary") {
    const text = item.text || item.summary || item.content || item.message || "";
    return {
      name: "Context compaction",
      detail: "Codex automatically compacted the history to save context",
      output: text,
      status: item.status || "completed",
      kind: "summary",
    };
  }
  return {
    name: type.replaceAll("_", " "),
    detail: item.command || item.name || item.query || "",
    output: item.aggregated_output || "",
    status: item.status || "in_progress",
  };
}

function toolCallsForNode(node) {
  const calls = new Map();
  for (const event of node.raw_events || []) {
    const item = event.item;
    if (!item || typeof item !== "object") continue;
    const type = item.type || "";
    if (!["command_execution", "web_search", "summary"].includes(type) && !type.includes("tool")) continue;
    const key = item.id || `${type}-${calls.size}`;
    const described = describeTool(item);
    const eventType = event.type || "";
    calls.set(key, {
      ...described,
      status: eventType === "item.completed" ? "completed" : described.status,
    });
  }
  return [...calls.values()];
}

function renderToolCalls(tools, active = false) {
  if (!tools.length) return null;
  const list = document.createElement("div");
  list.className = "tool-list";
  for (const tool of tools) {
    const row = document.createElement("div");
    const classes = ["tool-call", tool.status === "completed" ? "completed" : "running"];
    if (tool.kind) classes.push(tool.kind);
    if (tool.kind === "summary") classes.push("collapsed");
    row.className = classes.join(" ");
    const spinner = document.createElement("span");
    spinner.className = "tool-spinner";
    const text = document.createElement("div");
    text.className = "tool-text";
    const title = document.createElement("div");
    title.className = "tool-title";
    if (tool.kind === "summary") {
      title.textContent = tool.name;
    } else {
      title.textContent = tool.status === "completed" ? `${tool.name} done` : `Calling ${tool.name}`;
    }
    const detail = document.createElement("div");
    detail.className = "tool-detail";
    detail.textContent = tool.detail || "-";
    text.append(title, detail);
    if (tool.output) {
      const output = document.createElement("pre");
      output.className = "tool-output";
      output.textContent = tool.kind === "summary" ? tool.output : compact(tool.output, 260);
      text.appendChild(output);
    }
    if (tool.kind === "summary") {
      row.addEventListener("click", () => row.classList.toggle("collapsed"));
    }
    row.append(spinner, text);
    list.appendChild(row);
  }
  // Fold the tool/reasoning activity into a disclosure so the final answer
  // stays the prominent message. Keep it open while the node is still running
  // (so live tool activity is visible), collapsed once it has finished.
  const details = document.createElement("details");
  details.className = "tool-disclosure";
  if (active) details.open = true;
  const summary = document.createElement("summary");
  summary.className = "tool-disclosure-summary";
  const running = tools.filter((t) => t.status !== "completed").length;
  const label = running
    ? `Tools & reasoning · ${running} running`
    : `Tools & reasoning · ${tools.length}`;
  summary.innerHTML = `<span class="tool-disclosure-caret" aria-hidden="true"></span><span class="tool-disclosure-label">${label}</span>`;
  details.append(summary, list);
  return details;
}

function makeTypingIndicator() {
  const dots = document.createElement("div");
  dots.className = "typing-dots";
  dots.setAttribute("aria-label", "Agent is running");
  for (let i = 0; i < 3; i += 1) {
    const dot = document.createElement("span");
    dots.appendChild(dot);
  }
  return dots;
}

function renderReferenceChips(attachments = [], workspaceRefs = []) {
  const refs = [
    ...attachments.map((item) => ({
      name: item.name || item.original_name || "",
      kind: fileKindForName(item.name || item.original_name || ""),
    })),
    ...workspaceRefs,
  ];
  if (!refs.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "bubble-ref-list";
  for (const ref of refs) {
    wrap.appendChild(makeCompactFileChip(ref, false));
  }
  return wrap;
}

function makeBubble(role, text, meta = "", tools = [], active = false, attachments = [], workspaceRefs = []) {
  const row = document.createElement("div");
  row.className = ["bubble-row", role, active && role === "agent" ? "running" : ""].filter(Boolean).join(" ");
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  const label = document.createElement("div");
  label.className = "bubble-label";
  label.textContent = role === "user" ? "User" : "Agent";
  const toolList = role === "agent" ? renderToolCalls(tools, active) : null;
  bubble.appendChild(label);
  if (toolList) bubble.appendChild(toolList);
  if (role === "agent") {
    bubble.appendChild(renderMarkdown(text || ""));
  } else {
    const body = document.createElement("pre");
    body.textContent = text || "";
    bubble.appendChild(body);
    const refs = renderReferenceChips(attachments, workspaceRefs);
    if (refs) bubble.appendChild(refs);
  }
  if (active && role === "agent") bubble.appendChild(makeTypingIndicator());
  if (meta) {
    const foot = document.createElement("div");
    foot.className = "bubble-meta";
    foot.textContent = meta;
    bubble.appendChild(foot);
  }
  row.appendChild(bubble);
  return row;
}

function setSideTab(tab) {
  state.activeSideTab = tab === "notes" ? "notes" : "conversation";
  hideNoteContextMenu();
  renderSideTabs();
  renderNotePanel();
}

function renderSideTabs() {
  for (const button of els.sideTabBtns) {
    const active = button.dataset.sideTab === state.activeSideTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  els.conversationPane.hidden = state.activeSideTab !== "conversation";
  els.notePane.hidden = state.activeSideTab !== "notes";
}

function ensureNoteDraft(node) {
  if (!node) {
    state.noteDraftNodeId = null;
    state.noteDraft = "";
    state.noteDirty = false;
    state.noteEditing = false;
    return;
  }
  if (state.noteDraftNodeId !== node.id) {
    state.noteDraftNodeId = node.id;
    state.noteDraft = node.note_md || "";
    state.noteDirty = false;
    state.noteEditing = false;
    state.projectNotes = [];
    state.projectNotesProjectId = null;
  }
}

function renderProjectNotesSummary() {
  if (!state.projectNotesProjectId) return null;
  const wrap = document.createElement("section");
  wrap.className = "project-notes-summary";
  const title = document.createElement("h3");
  title.textContent = "Project notes";
  wrap.appendChild(title);
  if (!state.projectNotes.length) {
    const empty = document.createElement("div");
    empty.className = "soft-label";
    empty.textContent = "No notes yet";
    wrap.appendChild(empty);
    return wrap;
  }
  for (const item of state.projectNotes) {
    const article = document.createElement("article");
    article.className = "project-note-item";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "project-note-title";
    button.textContent = item.title || "Node";
    button.addEventListener("click", () => {
      selectNode(item.id);
      setSideTab("notes");
    });
    article.appendChild(button);
    article.appendChild(renderMarkdown(item.note_md || ""));
    wrap.appendChild(article);
  }
  return wrap;
}

function renderNotePanel() {
  const node = selectedNode();
  ensureNoteDraft(node);
  const disabled = !node;
  els.noteEditor.disabled = disabled || state.noteSaving;
  els.noteEditBtn.disabled = disabled || state.noteSaving;
  els.noteSaveBtn.disabled = disabled || state.noteSaving || !state.noteEditing;
  els.noteEditBtn.hidden = disabled || state.noteEditing;
  els.noteSaveBtn.hidden = disabled || !state.noteEditing;
  els.noteProjectBtn.disabled = !state.selectedProjectId || state.selectedProjectId === "__new__";
  els.noteStatus.textContent = disabled
    ? "say something"
    : state.noteSaving
      ? "Saving"
      : state.noteDirty
        ? "Node notes unsaved"
        : state.noteEditing
          ? "Editing notes"
          : "Node notes";
  if (els.noteEditor.value !== state.noteDraft) {
    els.noteEditor.value = state.noteDraft;
  }
  els.noteEditor.hidden = !state.noteEditing;
  els.noteRendered.hidden = state.noteEditing;

  const rendered = document.createElement("div");
  rendered.className = "note-preview-section";
  const noteText = state.noteDraft.trim();
  if (noteText) {
    rendered.appendChild(renderMarkdown(noteText));
  } else {
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "say something";
    rendered.appendChild(empty);
  }
  const projectNotes = renderProjectNotesSummary();
  if (projectNotes) rendered.appendChild(projectNotes);
  els.noteRendered.replaceChildren(rendered);
}

function renderConversation() {
  const node = selectedNode();
  const disabled = !node;
  els.deleteBtn.disabled = disabled;
  els.exportBtn.disabled = disabled;

  if (!node) {
    els.detailTitle.textContent = "say something";
    els.detailMeta.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "say something";
    els.conversationList.replaceChildren(empty);
    return;
  }

  els.detailTitle.textContent = node.title || "Node";

  const meta = [
    statusText(node.status),
    `${formatTokens(node.total_tokens)} tokens`,
    `${node.tools_count || 0} tools`,
    node.completed_at || node.created_at || "",
  ].filter(Boolean);
  const metaEls = meta.map((item) => {
    const span = document.createElement("span");
    span.className = "meta-pill";
    span.textContent = item;
    return span;
  });

  if (node.backend) {
    const backendPill = document.createElement("span");
    backendPill.className = `meta-pill backend-${node.backend}`;
    backendPill.textContent = node.backend === "claude" ? "Claude" : "Codex";
    metaEls.push(backendPill);
  }

  els.detailMeta.replaceChildren(...metaEls);

  const bubbles = [];
  for (const item of selectedConversationPath()) {
    bubbles.push(makeBubble("user", item.prompt, item.created_at || "", [], false, item.attachments || [], item.workspace_refs || []));
    const active = isNodeActive(item);
    const agentText = item.answer || item.error || (active ? "Working…" : item.status === "done" ? "" : statusText(item.status));
    bubbles.push(makeBubble("agent", agentText, item.completed_at || item.updated_at || "", toolCallsForNode(item), active));
  }
  els.conversationList.replaceChildren(...bubbles);
  if (state.scrollConversationToLatest) {
    els.conversationList.scrollTop = els.conversationList.scrollHeight;
    state.scrollConversationToLatest = false;
  }
}

// Show whether the next question starts a new tree (root) or continues an
// existing node, and offer a one-click break-out to a fresh thread.
function renderThreadTarget() {
  if (!els.threadTarget) return;
  els.threadTarget.replaceChildren();
  const parent = state.parentId ? state.nodeById.get(state.parentId) : null;

  if (!parent) {
    const label = document.createElement("span");
    label.className = "thread-chip thread-chip-root";
    label.textContent = "🌱 New thread";
    label.title = "Your next question will start a separate tree for this PDF";
    els.threadTarget.appendChild(label);
    return;
  }

  const chip = document.createElement("span");
  chip.className = "thread-chip thread-chip-continue";
  const arrow = document.createElement("span");
  arrow.className = "thread-chip-arrow";
  arrow.textContent = "↳";
  const text = document.createElement("span");
  text.className = "thread-chip-text";
  text.textContent = compact(parent.title || parent.prompt || "question", 48);
  text.title = parent.prompt || parent.title || "";
  chip.append(arrow, document.createTextNode(" Continuing: "), text);

  const newThread = document.createElement("button");
  newThread.type = "button";
  newThread.className = "thread-new-btn";
  newThread.textContent = "+ New thread";
  newThread.title = "Ask an unrelated question as a separate tree instead";
  newThread.addEventListener("click", () => {
    state.parentId = null;
    renderComposer();
    els.promptInput.focus();
  });

  els.threadTarget.append(chip, newThread);
}

function renderComposer() {
  renderThreadTarget();
  els.selectedFiles.replaceChildren();
  const sel = state.pendingSelection;
  if (sel && sel.quote) {
    const cite = document.createElement("div");
    cite.className = "pending-quote";
    const label = document.createElement("span");
    label.className = "pending-quote-label";
    label.textContent = `Source · p.${(sel.page || 0) + 1}`;
    const text = document.createElement("span");
    text.className = "pending-quote-text";
    text.textContent = compact(sel.quote, 160);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "pending-quote-remove";
    remove.textContent = "×";
    remove.title = "Remove citation";
    remove.addEventListener("click", () => {
      state.pendingSelection = null;
      renderComposer();
    });
    cite.append(label, text, remove);
    els.selectedFiles.appendChild(cite);
  }
  els.sendBtn.disabled = state.sending;
}

function workspaceIcon(file) {
  if (file.kind === "markdown") return "MD";
  if (file.kind === "pdf") return "PDF";
  if (file.kind === "text") return "TXT";
  return "FILE";
}

function compactFileIcon(file) {
  const kind = file.kind || fileKindForName(file.original_name || file.name);
  if (kind === "markdown") return "M";
  if (kind === "pdf") return "P";
  if (kind === "text") return "T";
  return "F";
}

function makeCompactFileChip(file, removable = false, onRemove = null) {
  const chip = document.createElement("span");
  chip.className = "file-chip compact-file-chip";
  chip.title = file.name || file.original_name || "";
  const icon = document.createElement("span");
  icon.className = "file-icon";
  icon.textContent = compactFileIcon(file);
  const name = document.createElement("span");
  name.textContent = compactFileName(file.name || file.original_name || "");
  chip.append(icon, name);
  if (removable) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.addEventListener("click", onRemove);
    chip.appendChild(remove);
  }
  return chip;
}

function renderWorkspaceFiles() {
  if (!els.workspaceFileList) return;
  if (state.workspaceLoading) {
    const loading = document.createElement("div");
    loading.className = "soft-label";
    loading.textContent = "Loading Workspace";
    els.workspaceFileList.replaceChildren(loading);
    return;
  }
  if (state.workspaceError) {
    const error = document.createElement("div");
    error.className = "soft-label error-label";
    error.textContent = state.workspaceError;
    els.workspaceFileList.replaceChildren(error);
    return;
  }
  if (!state.workspaceFiles.length) {
    const empty = document.createElement("div");
    empty.className = "soft-label";
    empty.textContent = "No Workspace files yet";
    els.workspaceFileList.replaceChildren(empty);
    return;
  }

  const rows = state.workspaceFiles.map((file) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "workspace-file-row";
    button.draggable = true;
    const icon = document.createElement("span");
    icon.className = `workspace-file-icon kind-${file.kind || "file"}`;
    icon.textContent = workspaceIcon(file);
    const main = document.createElement("span");
    main.className = "workspace-file-main";
    const name = document.createElement("span");
    name.className = "workspace-file-name";
    name.textContent = file.name;
    const sub = document.createElement("span");
    sub.className = "workspace-file-sub";
    sub.textContent = [
      file.source_title ? `from ${compact(file.source_title, 22)}` : "",
      file.dir || "workspace",
      formatBytes(file.size),
      file.modified_at || "",
    ].filter(Boolean).join(" · ");
    main.append(name, sub);
    button.append(icon, main);
    button.addEventListener("dragstart", (event) => {
      const reference = workspaceRefFromFile(file);
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-workspace-ref", JSON.stringify(reference));
      event.dataTransfer.setData("text/plain", file.name);
      event.dataTransfer.setData("text/uri-list", file.absolute_path || file.path);
    });
    button.addEventListener("click", () => {
      openWorkspaceFile(file).catch((err) => window.alert(err.message));
    });
    return button;
  });
  els.workspaceFileList.replaceChildren(...rows);
}

function renderWorkspacePreviewShell(file) {
  els.workspacePreview.hidden = false;
  els.workspacePreviewTitle.textContent = file.name;
  els.workspacePreviewMeta.textContent = [file.path, formatBytes(file.size), file.mime || ""].filter(Boolean).join(" · ");
  els.workspaceOpenBtn.href = workspaceFileUrl(file);
  els.workspaceDownloadBtn.href = workspaceFileUrl(file, true);
  const loading = document.createElement("div");
  loading.className = "workspace-preview-loading";
  loading.textContent = "Loading";
  els.workspacePreviewBody.replaceChildren(loading);
}

async function openWorkspaceFile(file) {
  renderWorkspacePreviewShell(file);

  if (file.kind === "markdown" || file.kind === "text") {
    const res = await fetch(workspaceContentUrl(file));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to read file");
    const body = document.createElement("div");
    if (file.kind === "markdown") {
      body.className = "workspace-markdown-preview";
      body.appendChild(renderMarkdown(data.content || ""));
    } else {
      const pre = document.createElement("pre");
      pre.className = "workspace-text-preview";
      pre.textContent = data.content || "";
      body.appendChild(pre);
    }
    if (data.truncated) {
      const notice = document.createElement("div");
      notice.className = "preview-notice";
      notice.textContent = "Large file — showing only the first portion";
      body.appendChild(notice);
    }
    els.workspacePreviewBody.replaceChildren(body);
    return;
  }

  if (file.kind === "pdf") {
    const frame = document.createElement("iframe");
    frame.className = "workspace-pdf-preview";
    frame.src = workspaceFileUrl(file);
    frame.title = file.name;
    els.workspacePreviewBody.replaceChildren(frame);
    return;
  }

  const fallback = document.createElement("div");
  fallback.className = "workspace-file-fallback";
  fallback.textContent = "This file type can't be previewed inline — open it in a new window or download it.";
  els.workspacePreviewBody.replaceChildren(fallback);
}

async function fetchWorkspaceFiles() {
  if (!els.workspaceFileList) return;
  const selectedId = state.selectedId;
  if (!selectedId) {
    state.workspaceFiles = [];
    state.workspaceLoading = false;
    state.workspaceError = "";
    renderWorkspaceFiles();
    return;
  }
  state.workspaceLoading = true;
  state.workspaceError = "";
  renderWorkspaceFiles();
  try {
    const res = await fetch(`/api/workspace/files?node_id=${encodeURIComponent(selectedId)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to load Workspace");
    if (selectedId !== state.selectedId) return;
    state.workspaceFiles = data.files || [];
  } catch (err) {
    state.workspaceError = err.message;
  } finally {
    state.workspaceLoading = false;
    renderWorkspaceFiles();
  }
}

function handlePromptDragOver(event) {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes("text/plain") && !types.includes("Files")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  els.promptInput.classList.add("drop-target");
}

function handlePromptDrop(event) {
  els.promptInput.classList.remove("drop-target");
  // Handle external file drops from OS
  if (event.dataTransfer.files && event.dataTransfer.files.length > 0) {
    event.preventDefault();
    event.stopPropagation();
    _dropOverlayCounter = 0;
    hideDropOverlay();
    uploadDroppedFiles(event.dataTransfer.files);
    return;
  }
  // Handle workspace ref drops
  const rawRef = event.dataTransfer.getData("application/x-workspace-ref");
  if (!rawRef) return;
  event.preventDefault();
  event.stopPropagation();
  try {
    const ref = JSON.parse(rawRef);
    if (!ref.path || state.workspaceRefs.some((item) => item.path === ref.path)) return;
    state.workspaceRefs.push(ref);
    renderComposer();
  } catch (err) {
    window.alert(err.message);
  }
}

/* ── Conversation pane drop zone for external files ── */
let _dropOverlayCounter = 0;

function showDropOverlay() {
  let overlay = els.conversationPane.querySelector(".drop-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.className = "drop-overlay";
    overlay.innerHTML = '<div class="drop-overlay-label">Drop to upload files</div>';
    els.conversationPane.appendChild(overlay);
  }
  overlay.hidden = false;
}

function hideDropOverlay() {
  const overlay = els.conversationPane.querySelector(".drop-overlay");
  if (overlay) overlay.hidden = true;
}

function handlePaneDragEnter(event) {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes("Files")) return;
  event.preventDefault();
  _dropOverlayCounter++;
  if (_dropOverlayCounter === 1) showDropOverlay();
}

function handlePaneDragOver(event) {
  const types = Array.from(event.dataTransfer.types || []);
  if (!types.includes("Files")) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
}

function handlePaneDragLeave(event) {
  _dropOverlayCounter--;
  if (_dropOverlayCounter <= 0) {
    _dropOverlayCounter = 0;
    hideDropOverlay();
  }
}

function handlePaneDrop(event) {
  _dropOverlayCounter = 0;
  hideDropOverlay();
  if (!event.dataTransfer.files || event.dataTransfer.files.length === 0) return;
  event.preventDefault();
  uploadDroppedFiles(event.dataTransfer.files);
}

async function uploadDroppedFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  if (state.currentWorkspaceId) form.append("workspace_id", state.currentWorkspaceId);
  try {
    const res = await fetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) {
      window.alert(data.error || "Upload failed");
      return;
    }
    for (const file of data.files || []) state.fileIds.add(file.id);
    await fetchTree();
    renderComposer();
  } catch (err) {
    window.alert(err.message);
  }
}

function renderDirectionControls() {
  for (const button of els.directionBtns) {
    button.classList.toggle("active", button.dataset.direction === state.treeDirection);
  }
}

function renderBackendControls() {
  for (const button of els.backendBtns) {
    button.classList.toggle("active", button.dataset.backend === state.selectedBackend);
  }
}

function renderWebSearchControls() {
  const current = state.enableWebSearch ? "on" : "off";
  for (const button of els.webSearchBtns) {
    button.classList.toggle("active", button.dataset.webSearch === current);
  }
}

async function sendPrompt(event) {
  event.preventDefault();
  const prompt = els.promptInput.value.trim();
  if (!prompt || state.sending) return;
  if (!state.currentDocId) {
    window.alert("Please upload and select a PDF first");
    return;
  }
  state.sending = true;
  renderComposer();
  try {
    // If an unsaved selection is cited, persist it as a highlight first so we can link it to the question node.
    let highlightId = state.pendingSelection ? state.pendingSelection.highlightId || null : null;
    if (state.pendingSelection && !highlightId) {
      highlightId = await persistHighlight(state.pendingSelection);
    }

    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        parent_id: state.parentId,
        prompt,
        backend: state.selectedBackend,
        enable_web_search: state.enableWebSearch,
        document_id: state.currentDocId,
        source_highlight_id: highlightId,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || "Send failed");
    state.selectedId = data.node.id;
    state.parentId = data.node.id;
    state.pendingSelection = null;
    els.promptInput.value = "";
    state.scrollConversationToLatest = true;
    await Promise.all([fetchTree(), fetchHighlights()]);
    focusSelected();
  } catch (err) {
    window.alert(err.message);
  } finally {
    state.sending = false;
    renderComposer();
  }
}

async function saveNote() {
  const node = selectedNode();
  if (!node || state.noteSaving) return;
  state.noteSaving = true;
  renderNotePanel();
  try {
    const res = await fetch(`/api/nodes/${node.id}/note`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note_md: els.noteEditor.value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Save failed");
    state.noteDirty = false;
    state.noteEditing = false;
    state.noteDraftNodeId = data.node.id;
    state.noteDraft = data.node.note_md || "";
    await fetchTree();
  } catch (err) {
    window.alert(err.message);
  } finally {
    state.noteSaving = false;
    renderNotePanel();
  }
}

async function readProjectNotes() {
  const projectId = state.selectedProjectId;
  if (!projectId || projectId === "__new__") return;
  const res = await fetch(`/api/projects/${projectId}/notes`);
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "Load failed");
    return;
  }
  state.projectNotes = data.notes || [];
  state.projectNotesProjectId = projectId;
  state.noteEditing = false;
  state.activeSideTab = "notes";
  renderSideTabs();
  renderNotePanel();
}

function cleanMarkdown(value) {
  return String(value || "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function markdownChildren(node) {
  return Array.from(node.childNodes || []).map((child) => markdownFromNode(child)).join("");
}

function markdownFromNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
  if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) return markdownChildren(node);
  if (node.nodeType !== Node.ELEMENT_NODE) return "";

  const el = node;
  const tag = el.tagName.toLowerCase();
  if (el.classList.contains("bubble-label") || el.classList.contains("bubble-meta")) return "";
  if (tag === "br") return "\n";
  if (tag === "hr") return "\n---\n";
  if (tag === "strong" || tag === "b") return `**${markdownChildren(el).trim()}**`;
  if (tag === "em" || tag === "i") return `*${markdownChildren(el).trim()}*`;
  if (tag === "code" && el.parentElement && el.parentElement.tagName.toLowerCase() !== "pre") {
    return `\`${el.textContent || ""}\``;
  }
  if (tag === "a") {
    const label = markdownChildren(el).trim() || el.textContent || "";
    const href = el.getAttribute("href") || "";
    return href ? `[${label}](${href})` : label;
  }
  if (tag === "pre") {
    const code = el.querySelector("code");
    const language = code && code.dataset.language ? code.dataset.language : "";
    const text = code ? code.textContent || "" : el.textContent || "";
    return code ? `\n\`\`\`${language}\n${text.replace(/\n$/, "")}\n\`\`\`\n` : `\n${text}\n`;
  }
  if (/^h[1-6]$/.test(tag)) {
    const level = Number(tag.slice(1));
    return `\n${"#".repeat(level)} ${markdownChildren(el).trim()}\n`;
  }
  if (tag === "p") return `\n${markdownChildren(el).trim()}\n`;
  if (tag === "blockquote") {
    const body = cleanMarkdown(markdownChildren(el));
    return `\n${body.split("\n").map((line) => `> ${line}`).join("\n")}\n`;
  }
  if (tag === "ul" || tag === "ol") {
    const items = Array.from(el.children).filter((child) => child.tagName.toLowerCase() === "li");
    return `\n${items.map((item, index) => {
      const marker = tag === "ol" ? `${index + 1}.` : "-";
      const body = cleanMarkdown(markdownChildren(item)).replace(/\n/g, "\n  ");
      return `${marker} ${body}`;
    }).join("\n")}\n`;
  }
  if (tag === "li") return markdownChildren(el);
  if (tag === "table") return tableToMarkdown(el);
  return markdownChildren(el);
}

function tableToMarkdown(table) {
  const rows = Array.from(table.querySelectorAll("tr")).filter((tr) => tr.cells && tr.cells.length);
  if (!rows.length) return "";

  const cellMarkdown = (cell) =>
    cleanMarkdown(markdownChildren(cell))
      .replace(/\|/g, "\\|")
      .replace(/\s*\n+\s*/g, " ")
      .trim() || " ";

  const rowCells = (tr) => Array.from(tr.cells).map(cellMarkdown);
  const headerCells = rowCells(rows[0]);
  const bodyRows = rows.slice(1).map(rowCells);
  const colCount = Math.max(headerCells.length, ...bodyRows.map((r) => r.length), 1);

  const pad = (cells) => {
    const out = cells.slice();
    while (out.length < colCount) out.push(" ");
    return out;
  };

  const lines = [];
  lines.push(`| ${pad(headerCells).join(" | ")} |`);
  lines.push(`| ${new Array(colCount).fill("---").join(" | ")} |`);
  for (const row of bodyRows) {
    lines.push(`| ${pad(row).join(" | ")} |`);
  }
  return `\n\n${lines.join("\n")}\n\n`;
}

function applyAncestorMarkdown(markdown, range) {
  let result = cleanMarkdown(markdown);
  if (!result) return "";
  let el = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentElement;
  while (el && el !== els.conversationList) {
    const tag = el.tagName.toLowerCase();
    if ((tag === "strong" || tag === "b") && !/^\*\*[\s\S]+\*\*$/.test(result)) {
      result = `**${result}**`;
    } else if ((tag === "em" || tag === "i") && !/^\*[\s\S]+\*$/.test(result)) {
      result = `*${result}*`;
    } else if (tag === "code" && el.parentElement && el.parentElement.tagName.toLowerCase() === "pre") {
      const language = el.dataset.language || "";
      result = `\`\`\`${language}\n${result}\n\`\`\``;
    } else if (tag === "code" && !/^`[\s\S]+`$/.test(result)) {
      result = `\`${result}\``;
    } else if (tag === "a" && !/^\[[\s\S]+\]\([^)]+\)$/.test(result)) {
      const href = el.getAttribute("href") || "";
      if (href) result = `[${result}](${href})`;
    } else if (/^h[1-6]$/.test(tag) && !/^#{1,6}\s/.test(result)) {
      result = `${"#".repeat(Number(tag.slice(1)))} ${result}`;
    } else if (tag === "li" && !/^(-|\d+\.)\s/.test(result)) {
      const parent = el.parentElement;
      const ordered = parent && parent.tagName.toLowerCase() === "ol";
      const siblings = parent ? Array.from(parent.children).filter((child) => child.tagName.toLowerCase() === "li") : [];
      const marker = ordered ? `${Math.max(1, siblings.indexOf(el) + 1)}.` : "-";
      result = `${marker} ${result.replace(/\n/g, "\n  ")}`;
    } else if (tag === "blockquote" && !/^>\s/m.test(result)) {
      result = result.split("\n").map((line) => `> ${line}`).join("\n");
    }
    el = el.parentElement;
  }
  return cleanMarkdown(result);
}

function selectedConversationMarkdown() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return "";
  const anchor = selection.anchorNode && (selection.anchorNode.nodeType === Node.ELEMENT_NODE
    ? selection.anchorNode
    : selection.anchorNode.parentElement);
  const focus = selection.focusNode && (selection.focusNode.nodeType === Node.ELEMENT_NODE
    ? selection.focusNode
    : selection.focusNode.parentElement);
  if (!anchor || !focus) return "";
  if (!els.conversationList.contains(anchor) || !els.conversationList.contains(focus)) return "";
  const parts = [];
  for (let i = 0; i < selection.rangeCount; i += 1) {
    const range = selection.getRangeAt(i);
    const ancestor = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
      ? range.commonAncestorContainer
      : range.commonAncestorContainer.parentElement;
    if (!ancestor || !els.conversationList.contains(ancestor)) continue;
    const markdown = applyAncestorMarkdown(markdownFromNode(range.cloneContents()), range);
    if (markdown) parts.push(markdown);
  }
  return cleanMarkdown(parts.join("\n\n")) || selection.toString().trim();
}

function showNoteContextMenu(event, text) {
  state.noteContextText = text;
  const menu = els.noteContextMenu;
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  const left = clamp(event.clientX, 8, window.innerWidth - rect.width - 8);
  const top = clamp(event.clientY, 8, window.innerHeight - rect.height - 8);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

function hideNoteContextMenu() {
  state.noteContextText = "";
  els.noteContextMenu.hidden = true;
}

async function appendSelectionToNote() {
  const node = selectedNode();
  const text = state.noteContextText.trim();
  hideNoteContextMenu();
  if (!node || !text) return;
  const res = await fetch(`/api/nodes/${node.id}/note/append`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "Append failed");
    return;
  }
  state.activeSideTab = "notes";
  state.noteDirty = false;
  state.noteDraftNodeId = data.node.id;
  state.noteDraft = data.node.note_md || "";
  await fetchTree();
}

async function deleteSelected() {
  const node = selectedNode();
  if (!node) return;
  const children = state.childrenByParent.get(node.id) || [];
  const confirmed = window.confirm(
    children.length
      ? "Delete this node and all nodes after it?"
      : "Delete this node?"
  );
  if (!confirmed) return;
  const fallbackParentId = node.parent_id || null;
  const res = await fetch(`/api/nodes/${node.id}`, { method: "DELETE" });
  const data = await res.json();
  if (!res.ok) {
    window.alert(data.error || "Delete failed");
    return;
  }
  if (fallbackParentId && state.nodeById.has(fallbackParentId)) {
    state.selectedId = fallbackParentId;
    state.parentId = fallbackParentId;
    state.selectedProjectId = rootIdForNode(fallbackParentId) || fallbackParentId;
  } else {
    state.selectedProjectId = "__new__";
    state.selectedId = null;
    state.parentId = null;
  }
  localStorage.setItem("selectedProjectId", state.selectedProjectId);
  await fetchTree();
  focusSelected();
  await fetchWorkspaceFiles();
}

function rootIdForNode(nodeId) {
  let current = state.nodeById.get(nodeId);
  const seen = new Set();
  while (current && current.parent_id && !seen.has(current.id)) {
    seen.add(current.id);
    const parent = state.nodeById.get(current.parent_id);
    if (!parent) break;
    current = parent;
  }
  return current ? current.id : null;
}

function exportSelected() {
  const node = selectedNode();
  if (!node) return;
  window.location.href = `/api/export/${node.id}`;
}

function maxSidePanelWidth() {
  return Math.max(MIN_SIDE_WIDTH, Math.min(MAX_SIDE_WIDTH, window.innerWidth - 280));
}

function applySidePanelWidth(width) {
  const safeWidth = clamp(Number(width) || DEFAULT_SIDE_WIDTH, MIN_SIDE_WIDTH, maxSidePanelWidth());
  state.sidePanelWidth = safeWidth;
  document.documentElement.style.setProperty("--side-panel-width", `${safeWidth}px`);
  renderMiniMap();
}

function initSidePanelWidth() {
  applySidePanelWidth(Number(localStorage.getItem(SIDE_WIDTH_KEY)) || DEFAULT_SIDE_WIDTH);
}

function startSidePanelResize(event) {
  if (!els.sideResizeHandle) return;
  event.preventDefault();
  state.resizingSidePanel = true;
  document.body.classList.add("resizing-side-panel");
}

function resizeSidePanel(event) {
  if (!state.resizingSidePanel) return;
  const nextWidth = window.innerWidth - event.clientX;
  applySidePanelWidth(nextWidth);
}

function stopSidePanelResize() {
  if (!state.resizingSidePanel) return;
  state.resizingSidePanel = false;
  document.body.classList.remove("resizing-side-panel");
  localStorage.setItem(SIDE_WIDTH_KEY, String(Math.round(state.sidePanelWidth)));
}

function pollIfNeeded() {
  const active = state.allNodes.some((node) => node.status === "running" || node.status === "queued");
  if (active) {
    fetchTree().catch(console.error);
    fetchWorkspaceFiles().catch(console.error);
  }
}

// ══════════════════════════════════════════════════════════════════════
//  PDF rendering + text selection + highlights
// ══════════════════════════════════════════════════════════════════════

const HL_COLORS = ["yellow", "green", "blue", "pink"];
let pdfSelColor = localStorage.getItem("hlColor") || "yellow";

if (window.pdfjsLib) {
  window.pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.js";
}

async function loadPdf() {
  const token = ++state.pdfRenderToken;
  state.pdfPageViewports = new Map();
  if (els.pdfPages) els.pdfPages.replaceChildren();
  const docId = state.currentDocId;
  if (!docId || !window.pdfjsLib) {
    state.pdfDoc = null;
    if (els.pdfEmpty) els.pdfEmpty.hidden = Boolean(docId);
    updatePdfPageInfo();
    return;
  }
  if (els.pdfEmpty) els.pdfEmpty.hidden = true;
  try {
    const task = window.pdfjsLib.getDocument({ url: `/api/documents/${docId}/file` });
    const pdf = await task.promise;
    if (token !== state.pdfRenderToken) return; // the document changed while rendering
    state.pdfDoc = pdf;
    for (let n = 1; n <= pdf.numPages; n += 1) {
      const pageEl = document.createElement("div");
      pageEl.className = "pdf-page";
      pageEl.dataset.page = String(n - 1); // 0-based
      els.pdfPages.appendChild(pageEl);
      await renderPdfPage(pdf, n, pageEl, token);
      if (token !== state.pdfRenderToken) return;
    }
    updatePdfPageInfo();
    renderHighlights();
  } catch (err) {
    console.error("PDF failed to load", err);
    if (els.pdfPages) {
      const msg = document.createElement("div");
      msg.className = "pdf-error";
      msg.textContent = `PDF failed to load: ${err.message || err}`;
      els.pdfPages.replaceChildren(msg);
    }
  }
}

async function renderPdfPage(pdf, pageNum, pageEl, token) {
  const page = await pdf.getPage(pageNum);
  if (token !== state.pdfRenderToken) return;
  const viewport = page.getViewport({ scale: state.pdfScale });
  state.pdfPageViewports.set(pageNum - 1, { width: viewport.width, height: viewport.height });
  pageEl.style.width = `${viewport.width}px`;
  pageEl.style.height = `${viewport.height}px`;
  // PDF.js 3.x positions text-layer divs with calc(var(--scale-factor) * …);
  // without this variable those calcs collapse to 0 and text selection /
  // highlight anchoring break. Keep it equal to the viewport scale.
  pageEl.style.setProperty("--scale-factor", String(viewport.scale));

  const canvas = document.createElement("canvas");
  canvas.className = "pdf-canvas";
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  pageEl.appendChild(canvas);

  const textLayer = document.createElement("div");
  textLayer.className = "textLayer";
  pageEl.appendChild(textLayer);

  const hlLayer = document.createElement("div");
  hlLayer.className = "pdf-hl-layer";
  pageEl.appendChild(hlLayer);

  await page.render({ canvasContext: ctx, viewport }).promise;
  if (token !== state.pdfRenderToken) return;
  try {
    const textContent = await page.getTextContent();
    if (token !== state.pdfRenderToken) return;
    await window.pdfjsLib.renderTextLayer({
      textContentSource: textContent,
      container: textLayer,
      viewport,
      textDivs: [],
    }).promise;
  } catch (err) {
    console.warn("Text layer failed to render", err);
  }
  drawHighlightsForPage(pageNum - 1);
}

function updatePdfPageInfo() {
  if (!els.pdfPageInfo) return;
  const n = state.pdfDoc ? state.pdfDoc.numPages : 0;
  els.pdfPageInfo.textContent = n ? `${n} pages · ${Math.round(state.pdfScale * 100)}%` : "";
}

function setPdfScale(scale) {
  state.pdfScale = clamp(scale, 0.5, 3);
  localStorage.setItem("pdfScale", String(state.pdfScale));
  loadPdf();
}

// ── Text selection → floating toolbar ────────────────────────────────────

function pageElForNode(node) {
  const el = node && node.nodeType === 1 ? node : node && node.parentElement;
  return el ? el.closest(".pdf-page") : null;
}

function handlePdfMouseUp() {
  // Defer until the selection has settled before reading it.
  setTimeout(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) {
      if (!state.pendingSelection) hideSelectionToolbar();
      return;
    }
    const range = sel.getRangeAt(0);
    const quote = sel.toString().trim();
    if (!quote) {
      hideSelectionToolbar();
      return;
    }
    const pageEl = pageElForNode(range.startContainer) || pageElForNode(range.endContainer);
    if (!pageEl) {
      hideSelectionToolbar();
      return;
    }
    const page = Number(pageEl.dataset.page);
    const pageRect = pageEl.getBoundingClientRect();
    const rects = [];
    for (const r of range.getClientRects()) {
      // Keep only the rects that fall within this page.
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      if (cx < pageRect.left || cx > pageRect.right || cy < pageRect.top || cy > pageRect.bottom) continue;
      if (r.width < 1 || r.height < 1) continue;
      rects.push({
        x: (r.left - pageRect.left) / pageRect.width,
        y: (r.top - pageRect.top) / pageRect.height,
        w: r.width / pageRect.width,
        h: r.height / pageRect.height,
      });
    }
    if (!rects.length) {
      hideSelectionToolbar();
      return;
    }
    state.pendingSelection = { quote, page, rects, color: pdfSelColor };
    showSelectionToolbar(range);
  }, 0);
}

function showSelectionToolbar(range) {
  const bar = els.selectionToolbar;
  if (!bar) return;
  const rects = range.getClientRects();
  const last = rects[rects.length - 1] || range.getBoundingClientRect();
  bar.hidden = false;
  syncSelColorButtons(bar);
  const barRect = bar.getBoundingClientRect();
  let left = last.left + last.width / 2 - barRect.width / 2;
  let top = last.bottom + 8;
  left = clamp(left, 8, window.innerWidth - barRect.width - 8);
  if (top + barRect.height > window.innerHeight - 8) top = last.top - barRect.height - 8;
  bar.style.left = `${left}px`;
  bar.style.top = `${top}px`;
}

function hideSelectionToolbar() {
  if (els.selectionToolbar) els.selectionToolbar.hidden = true;
}

function syncSelColorButtons(container) {
  container.querySelectorAll(".hl-color").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.color === pdfSelColor);
  });
}

async function persistHighlight(sel) {
  const res = await fetch(`/api/documents/${state.currentDocId}/highlights`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page: sel.page, rects: sel.rects, quote: sel.quote, color: sel.color || pdfSelColor }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.error || "Failed to save highlight");
  state.highlights.push(data.highlight);
  drawHighlightsForPage(sel.page);
  return data.highlight.id;
}

async function highlightSelection() {
  if (!state.pendingSelection) return;
  try {
    await persistHighlight(state.pendingSelection);
  } catch (err) {
    window.alert(err.message);
    return;
  }
  state.pendingSelection = null;
  hideSelectionToolbar();
  window.getSelection()?.removeAllRanges();
}

function askFromSelection() {
  if (!state.pendingSelection) return;
  hideSelectionToolbar();
  window.getSelection()?.removeAllRanges();
  renderComposer();
  if (!state.paneOpen.chat) setPaneOpen("chat", true);
  els.promptInput.focus();
}

// ── Highlight overlay rendering ──────────────────────────────────────────

function renderHighlights() {
  if (!state.pdfDoc) return;
  const pages = new Set(state.highlights.map((h) => h.page));
  for (const page of pages) drawHighlightsForPage(page);
  // Also clear pages that no longer have any highlights.
  if (els.pdfPages) {
    els.pdfPages.querySelectorAll(".pdf-page").forEach((pageEl) => {
      const page = Number(pageEl.dataset.page);
      if (!pages.has(page)) {
        const layer = pageEl.querySelector(".pdf-hl-layer");
        if (layer) layer.replaceChildren();
      }
    });
  }
}

function drawHighlightsForPage(page) {
  if (!els.pdfPages) return;
  const pageEl = els.pdfPages.querySelector(`.pdf-page[data-page="${page}"]`);
  if (!pageEl) return;
  const layer = pageEl.querySelector(".pdf-hl-layer");
  if (!layer) return;
  const w = pageEl.clientWidth;
  const h = pageEl.clientHeight;
  const boxes = [];
  for (const hl of state.highlights) {
    if (hl.page !== page) continue;
    for (const rect of hl.rects || []) {
      const box = document.createElement("div");
      box.className = `pdf-hl hl-${hl.color || "yellow"}`;
      if (hl.node_id) box.classList.add("has-node");
      if (hl.id === state.activeHighlightId) box.classList.add("active");
      box.style.left = `${rect.x * w}px`;
      box.style.top = `${rect.y * h}px`;
      box.style.width = `${rect.w * w}px`;
      box.style.height = `${rect.h * h}px`;
      box.dataset.highlightId = hl.id;
      box.title = hl.node_id ? "Click: go to question node · Right-click: menu" : "Right-click: menu";
      boxes.push(box);
    }
  }
  layer.replaceChildren(...boxes);
}

function highlightById(id) {
  return state.highlights.find((h) => h.id === id) || null;
}

// Scroll the PDF viewer so a highlight's first rect is centered, mark it active,
// and briefly flash it. Used when selecting a node that was asked from a quote.
function scrollToHighlight(hl) {
  if (!hl || !els.pdfViewport || !els.pdfPages) return;
  const pageEl = els.pdfPages.querySelector(`.pdf-page[data-page="${hl.page}"]`);
  if (!pageEl) return;
  state.activeHighlightId = hl.id;
  drawHighlightsForPage(hl.page);
  const rect = (hl.rects || [])[0];
  const viewport = els.pdfViewport;
  // pageEl.offsetTop is relative to #pdfPages, which sits inside #pdfViewport.
  const rectTop = pageEl.offsetTop + (rect ? rect.y * pageEl.clientHeight : 0);
  const target = rectTop - viewport.clientHeight / 2 + (rect ? (rect.h * pageEl.clientHeight) / 2 : 0);
  viewport.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
}

// When a node is linked to a source highlight, reveal and jump to that quote.
function revealNodeSourceHighlight(node) {
  if (!node || !node.source_highlight_id) return;
  const hl = highlightById(node.source_highlight_id);
  if (!hl) return;
  if (!state.paneOpen.pdf) setPaneOpen("pdf", true);
  // Defer so a just-opened PDF pane has laid out before we measure/scroll.
  requestAnimationFrame(() => scrollToHighlight(hl));
}

function onHighlightClick(event) {
  const box = event.target.closest(".pdf-hl");
  if (!box) return;
  const hl = highlightById(box.dataset.highlightId);
  if (!hl) return;
  event.stopPropagation();
  if (hl.node_id && state.nodeById.has(hl.node_id)) {
    if (!state.paneOpen.tree) setPaneOpen("tree", true);
    selectNode(hl.node_id);
    focusSelected();
  }
}

function onHighlightContextMenu(event) {
  const box = event.target.closest(".pdf-hl");
  if (!box) return;
  event.preventDefault();
  event.stopPropagation();
  const hl = highlightById(box.dataset.highlightId);
  if (!hl) return;
  state.activeHighlightId = hl.id;
  const menu = els.highlightMenu;
  menu.hidden = false;
  syncSelColorButtons(menu);
  if (els.hlGotoNodeBtn) els.hlGotoNodeBtn.hidden = !(hl.node_id && state.nodeById.has(hl.node_id));
  const rect = menu.getBoundingClientRect();
  let left = clamp(event.clientX, 8, window.innerWidth - rect.width - 8);
  let top = clamp(event.clientY, 8, window.innerHeight - rect.height - 8);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

function hideHighlightMenu() {
  if (els.highlightMenu) els.highlightMenu.hidden = true;
}

async function recolorActiveHighlight(color) {
  const hl = highlightById(state.activeHighlightId);
  if (!hl) return;
  try {
    const res = await fetch(`/api/highlights/${hl.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ color }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || "Update failed");
    hl.color = color;
    drawHighlightsForPage(hl.page);
  } catch (err) {
    window.alert(err.message);
  }
}

async function deleteActiveHighlight() {
  const hl = highlightById(state.activeHighlightId);
  if (!hl) return;
  try {
    const res = await fetch(`/api/highlights/${hl.id}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "Delete failed");
    }
    const page = hl.page;
    state.highlights = state.highlights.filter((h) => h.id !== hl.id);
    state.activeHighlightId = null;
    drawHighlightsForPage(page);
  } catch (err) {
    window.alert(err.message);
  }
}

// ── Pane show/hide and width ─────────────────────────────────────────────

function setPaneOpen(pane, open) {
  state.paneOpen[pane] = open;
  try {
    localStorage.setItem("paneOpen", JSON.stringify(state.paneOpen));
  } catch {}
  renderPanes();
  setTimeout(renderMiniMap, 180);
}

function renderPanes() {
  const map = { pdf: els.pdfPane, tree: els.treePane, chat: els.sidePanel };
  const toggles = { pdf: els.togglePdfBtn, tree: els.toggleTreeBtn, chat: els.toggleChatBtn };
  for (const key of PANE_KEYS) {
    const open = state.paneOpen[key];
    if (map[key]) map[key].classList.toggle("collapsed", !open);
    if (toggles[key]) toggles[key].classList.toggle("active", open);
  }
  // The side resize handles only make sense when both adjacent panes are open.
  if (els.pdfResizeHandle) els.pdfResizeHandle.style.display = state.paneOpen.pdf && state.paneOpen.tree ? "" : "none";
  if (els.sideResizeHandle) els.sideResizeHandle.style.display = state.paneOpen.chat && (state.paneOpen.tree || state.paneOpen.pdf) ? "" : "none";
}

function initPdfPaneWidth() {
  const stored = Number(localStorage.getItem("pdfPaneWidth"));
  if (stored && els.pdfPane) els.pdfPane.style.flexBasis = `${clamp(stored, 320, 1200)}px`;
}

let resizingPdf = false;
function startPdfResize(event) {
  resizingPdf = true;
  event.preventDefault();
}
function movePdfResize(event) {
  if (!resizingPdf || !els.pdfPane) return;
  const left = els.pdfPane.getBoundingClientRect().left;
  const width = clamp(event.clientX - left, 320, 1200);
  els.pdfPane.style.flexBasis = `${width}px`;
}
function stopPdfResize() {
  if (!resizingPdf) return;
  resizingPdf = false;
  if (els.pdfPane) {
    const width = els.pdfPane.getBoundingClientRect().width;
    localStorage.setItem("pdfPaneWidth", String(Math.round(width)));
  }
  renderMiniMap();
}

async function uploadPdf(file) {
  if (!file || !state.currentWorkspaceId) return;
  const wsId = state.currentWorkspaceId;
  const form = new FormData();
  form.append("file", file);
  form.append("workspace_id", wsId);
  try {
    const res = await fetch("/api/documents", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || data.error || "Upload failed");
    await fetchWorkspaces();
    if (state.view === "reader") {
      // Adding a PDF from inside the reader → open it immediately.
      navigate("reader", { wsId, docId: data.document.id });
    } else {
      // Adding a PDF from the workspace page → stay on the list and refresh.
      await fetchWorkspaceDetail(wsId);
      refreshCurrentScreen();
    }
  } catch (err) {
    window.alert(err.message);
  }
}

function wireEvents() {
  els.composer.addEventListener("submit", sendPrompt);
  for (const button of els.sideTabBtns) {
    button.addEventListener("click", () => setSideTab(button.dataset.sideTab));
  }
  els.noteEditor.addEventListener("input", () => {
    const node = selectedNode();
    state.noteDraftNodeId = node ? node.id : null;
    state.noteDraft = els.noteEditor.value;
    state.noteDirty = Boolean(node);
    renderTree();
    renderNotePanel();
  });
  els.noteEditBtn.addEventListener("click", () => {
    if (!selectedNode()) return;
    state.noteEditing = true;
    renderNotePanel();
    els.noteEditor.focus();
  });
  els.noteSaveBtn.addEventListener("click", saveNote);
  els.noteProjectBtn.addEventListener("click", readProjectNotes);
  els.conversationList.addEventListener("contextmenu", (event) => {
    const text = selectedConversationMarkdown();
    if (!text || !selectedNode()) return;
    event.preventDefault();
    showNoteContextMenu(event, text);
  });
  els.noteContextAppendBtn.addEventListener("click", appendSelectionToNote);
  document.addEventListener("click", (event) => {
    if (!els.noteContextMenu.hidden && !els.noteContextMenu.contains(event.target)) {
      hideNoteContextMenu();
    }
  });
  els.sideResizeHandle.addEventListener("pointerdown", startSidePanelResize);
  window.addEventListener("pointermove", resizeSidePanel);
  window.addEventListener("pointerup", stopSidePanelResize);
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hideSelectionToolbar();
      hideHighlightMenu();
      hideNoteContextMenu();
      hideNodeNotePreviewImmediate();
      hideNodePromptPreview();
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s" && state.activeSideTab === "notes") {
      event.preventDefault();
      saveNote();
    }
  });
  els.promptInput.addEventListener("dragover", handlePromptDragOver);
  els.promptInput.addEventListener("dragleave", () => els.promptInput.classList.remove("drop-target"));
  els.promptInput.addEventListener("drop", handlePromptDrop);
  els.conversationPane.addEventListener("dragenter", handlePaneDragEnter);
  els.conversationPane.addEventListener("dragover", handlePaneDragOver);
  els.conversationPane.addEventListener("dragleave", handlePaneDragLeave);
  els.conversationPane.addEventListener("drop", handlePaneDrop);
  els.promptInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (!state.sending && els.promptInput.value.trim()) {
      els.composer.requestSubmit();
    }
  });
  els.searchInput.addEventListener("input", () => {
    state.search = els.searchInput.value;
    renderTree();
  });
  for (const button of els.directionBtns) {
    button.addEventListener("click", () => {
      const direction = button.dataset.direction === "right" ? "right" : "down";
      if (direction === state.treeDirection) return;
      state.treeDirection = direction;
      localStorage.setItem("treeDirection", direction);
      render();
      focusSelected();
    });
  }
  for (const button of els.backendBtns) {
    button.addEventListener("click", () => {
      const backend = button.dataset.backend === "claude" ? "claude" : "codex";
      if (backend === state.selectedBackend) return;
      state.selectedBackend = backend;
      localStorage.setItem("askBackend", backend);
      renderBackendControls();
    });
  }
  for (const button of els.webSearchBtns) {
    button.addEventListener("click", () => {
      const enabled = button.dataset.webSearch === "on";
      if (enabled === state.enableWebSearch) return;
      state.enableWebSearch = enabled;
      localStorage.setItem("askWebSearch", enabled ? "on" : "off");
      renderWebSearchControls();
    });
  }
  // ── Navigation / home / workspace detail ──
  window.addEventListener("hashchange", () => applyRoute().catch(console.error));
  if (els.homeBtn) els.homeBtn.addEventListener("click", () => navigate("home"));
  if (els.homeNewBtn) {
    els.homeNewBtn.addEventListener("click", async () => {
      const title = window.prompt("New workspace name");
      if (title == null) return;
      const created = await createWorkspace(title);
      if (created) navigate("workspace", { wsId: created.id });
    });
  }
  if (els.wsRenameBtn) {
    els.wsRenameBtn.addEventListener("click", () => {
      const ws = currentWorkspace() || state.wsDetail.workspace;
      if (ws) renameWorkspace(ws).catch(console.error);
    });
  }
  if (els.wsAddPdfBtn) els.wsAddPdfBtn.addEventListener("click", () => els.docUploadInput.click());

  // ── Document upload (one hidden input, shared by the workspace page and the reader's empty state) ──
  if (els.documentSelect) {
    els.documentSelect.addEventListener("change", () => {
      const value = els.documentSelect.value;
      if (value) navigate("reader", { wsId: state.currentWorkspaceId, docId: value });
    });
  }
  els.docUploadInput.addEventListener("change", () => {
    const file = els.docUploadInput.files && els.docUploadInput.files[0];
    els.docUploadInput.value = "";
    if (file) uploadPdf(file).catch(console.error);
  });
  if (els.pdfEmptyUploadBtn) els.pdfEmptyUploadBtn.addEventListener("click", () => els.docUploadInput.click());

  // ── PDF zoom / selection / highlights ──
  els.pdfZoomInBtn.addEventListener("click", () => setPdfScale(state.pdfScale * 1.15));
  els.pdfZoomOutBtn.addEventListener("click", () => setPdfScale(state.pdfScale / 1.15));
  els.pdfPages.addEventListener("mouseup", handlePdfMouseUp);
  els.pdfPages.addEventListener("click", onHighlightClick);
  els.pdfPages.addEventListener("contextmenu", onHighlightContextMenu);
  els.selHighlightBtn.addEventListener("click", () => highlightSelection().catch(console.error));
  els.selAskBtn.addEventListener("click", askFromSelection);
  els.selectionToolbar.querySelectorAll(".hl-color").forEach((btn) => {
    btn.addEventListener("mousedown", (event) => {
      event.preventDefault(); // keep the current text selection alive
      pdfSelColor = btn.dataset.color;
      localStorage.setItem("hlColor", pdfSelColor);
      if (state.pendingSelection) state.pendingSelection.color = pdfSelColor;
      syncSelColorButtons(els.selectionToolbar);
      // Clicking a color swatch highlights the selection immediately in that color.
      highlightSelection().catch(console.error);
    });
  });
  els.highlightMenu.querySelectorAll(".hl-color").forEach((btn) => {
    btn.addEventListener("click", () => {
      recolorActiveHighlight(btn.dataset.color).catch(console.error);
      hideHighlightMenu();
    });
  });
  if (els.hlGotoNodeBtn) {
    els.hlGotoNodeBtn.addEventListener("click", () => {
      const hl = highlightById(state.activeHighlightId);
      hideHighlightMenu();
      if (hl && hl.node_id && state.nodeById.has(hl.node_id)) {
        if (!state.paneOpen.tree) setPaneOpen("tree", true);
        selectNode(hl.node_id);
        focusSelected();
      }
    });
  }
  els.hlDeleteBtn.addEventListener("click", () => {
    deleteActiveHighlight().catch(console.error);
    hideHighlightMenu();
  });
  document.addEventListener("click", (event) => {
    if (!els.highlightMenu.hidden && !els.highlightMenu.contains(event.target) && !event.target.closest(".pdf-hl")) {
      hideHighlightMenu();
    }
    if (!els.selectionToolbar.hidden && !els.selectionToolbar.contains(event.target)) {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) hideSelectionToolbar();
    }
  });

  // ── Pane show/hide and PDF-width drag ──
  els.togglePdfBtn.addEventListener("click", () => setPaneOpen("pdf", !state.paneOpen.pdf));
  els.toggleTreeBtn.addEventListener("click", () => setPaneOpen("tree", !state.paneOpen.tree));
  els.toggleChatBtn.addEventListener("click", () => setPaneOpen("chat", !state.paneOpen.chat));
  document.querySelectorAll(".pane-collapse").forEach((btn) => {
    btn.addEventListener("click", () => setPaneOpen(btn.dataset.pane, false));
  });
  document.querySelectorAll(".pane-rail").forEach((rail) => {
    rail.addEventListener("click", () => setPaneOpen(rail.dataset.pane, true));
  });
  els.pdfResizeHandle.addEventListener("pointerdown", (event) => {
    startPdfResize(event);
    els.pdfResizeHandle.setPointerCapture(event.pointerId);
  });
  window.addEventListener("pointermove", movePdfResize);
  window.addEventListener("pointerup", stopPdfResize);

  els.emptyState.addEventListener("click", () => {
    els.promptInput.focus();
  });
  els.refreshBtn.addEventListener("click", () => {
    applyRoute().catch(console.error);
  });

  if (els.miniMap) {
    let dragging = false;
    const moveViewportTo = (clientX, clientY) => {
      const geo = miniMapGeometry();
      if (!geo.scale) return;
      const rect = els.miniMap.getBoundingClientRect();
      const mx = clientX - rect.left;
      const my = clientY - rect.top;
      const treeX = (mx - geo.tx) / geo.scale;
      const treeY = (my - geo.ty) / geo.scale;
      const host = els.canvasHost.getBoundingClientRect();
      state.transform.x = host.width / 2 - treeX * state.transform.k;
      state.transform.y = host.height / 2 - treeY * state.transform.k;
      applyTransform();
      renderMiniMap();
    };
    els.miniMap.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      dragging = true;
      els.miniMap.setPointerCapture(event.pointerId);
      els.miniMap.classList.add("dragging");
      moveViewportTo(event.clientX, event.clientY);
    });
    els.miniMap.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      moveViewportTo(event.clientX, event.clientY);
    });
    const endDrag = (event) => {
      if (!dragging) return;
      dragging = false;
      els.miniMap.classList.remove("dragging");
      try { els.miniMap.releasePointerCapture(event.pointerId); } catch {}
    };
    els.miniMap.addEventListener("pointerup", endDrag);
    els.miniMap.addEventListener("pointercancel", endDrag);
  }
  els.focusBtn.addEventListener("click", focusSelected);
  els.zoomInBtn.addEventListener("click", () => {
    const rect = els.canvasHost.getBoundingClientRect();
    zoomAt(1.16, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  els.zoomOutBtn.addEventListener("click", () => {
    const rect = els.canvasHost.getBoundingClientRect();
    zoomAt(0.86, rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  els.deleteBtn.addEventListener("click", deleteSelected);
  els.exportBtn.addEventListener("click", exportSelected);

  els.treeSvg.addEventListener("wheel", (event) => {
    event.preventDefault();
    // Trackpads fire many wheel events per gesture with small deltaY each;
    // map magnitude → factor so the gesture feels continuous instead of stepping.
    const dy = Math.max(-80, Math.min(80, event.deltaY));
    const factor = Math.exp(-dy * 0.0035);
    zoomAt(factor, event.clientX, event.clientY);
  }, { passive: false });

  if (els.nodeNotePreview) {
    els.nodeNotePreview.addEventListener("mouseenter", cancelNodeNotePreviewHide);
    els.nodeNotePreview.addEventListener("mouseleave", hideNodeNotePreviewImmediate);
  }
  els.treeSvg.addEventListener("pointerdown", (event) => {
    if (event.target.closest && event.target.closest(".node-card")) return;
    state.dragging = true;
    state.dragStart = {
      x: event.clientX,
      y: event.clientY,
      tx: state.transform.x,
      ty: state.transform.y,
    };
    els.treeSvg.setPointerCapture(event.pointerId);
  });
  els.treeSvg.addEventListener("pointermove", (event) => {
    if (!state.dragging || !state.dragStart) return;
    state.transform.x = state.dragStart.tx + event.clientX - state.dragStart.x;
    state.transform.y = state.dragStart.ty + event.clientY - state.dragStart.y;
    applyTransform();
    renderMiniMap();
  });
  els.treeSvg.addEventListener("pointerup", (event) => {
    state.dragging = false;
    state.dragStart = null;
    try {
      els.treeSvg.releasePointerCapture(event.pointerId);
    } catch {
      // Pointer capture may already be released by the browser.
    }
  });
  window.addEventListener("resize", () => {
    applySidePanelWidth(state.sidePanelWidth);
    renderMiniMap();
  });
}

initSidePanelWidth();
initPdfPaneWidth();
wireEvents();
renderPanes();

(async () => {
  try {
    // With no hash, restore the last location you were at; otherwise default to home.
    if (!location.hash) {
      let last = "#/";
      try {
        last = localStorage.getItem("lastRoute") || "#/";
      } catch {}
      if (last && last !== "#/") {
        location.hash = last; // triggers hashchange → applyRoute
        return;
      }
    }
    await applyRoute();
    if (state.view === "reader" && state.nodes.length) focusSelected();
  } catch (err) {
    console.error(err);
    navigate("home");
  }
})();

setInterval(pollIfNeeded, 2500);
