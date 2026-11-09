// Page script: renders each file's diff with @pierre/diffs and drives the page chrome
// (tabs/views, resizable sidebar, hunk navigation, relative dates).

import { FileDiff, type FileDiffMetadata } from "@pierre/diffs";
import { FileTree } from "@pierre/trees";

const diffData: (FileDiffMetadata | null)[] = JSON.parse(document.getElementById("diff-data")?.textContent || "[]");

// --- Diff rendering ---
// Files render lazily as they approach the viewport; a hunk jump renders its target on demand.

const instances = new Map<number, FileDiff>();

// Diffs render inside a shadow root, so styles for rows we target must be injected into it.
const DIFF_CSS = `
[data-line] { scroll-margin-top: 50px; }
.row-flash { animation: hunk-flash-anim 1.1s ease; }
@keyframes hunk-flash-anim { 0% { background: rgba(88,166,255,0.45); } 100% { background: transparent; } }
`;

function renderFile(body: HTMLElement): FileDiff | undefined {
  const index = Number(body.dataset.fileIndex);
  const existing = instances.get(index);
  if (existing) return existing;
  const fileDiff = diffData[index];
  if (!fileDiff) return undefined;

  const container = document.createElement("diffs-container");
  body.appendChild(container);
  const instance = new FileDiff({
    theme: { dark: "github-dark", light: "github-light" },
    themeType: "dark",
    diffStyle: "split",
    disableFileHeader: true,
    hunkSeparators: "line-info",
    lineDiffType: "word",
    overflow: "wrap",
    unsafeCSS: DIFF_CSS,
  });
  instance.render({ fileDiff, fileContainer: container });
  instances.set(index, instance);
  body.style.minHeight = "";
  return instance;
}

const lazyRenderer = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      lazyRenderer.unobserve(entry.target);
      renderFile(entry.target as HTMLElement);
    }
  },
  { rootMargin: "1500px 0px" },
);
document.querySelectorAll<HTMLElement>(".diff-body[data-file-index]").forEach((el) => lazyRenderer.observe(el));

// --- Sidebar file trees (@pierre/trees), one per files panel ---
// Clicking a file scrolls to its diff; scrolling the diffs selects the file in view.

interface FileInfo {
  path: string;
  status: "added" | "deleted" | "renamed" | "modified";
  additions: number;
  deletions: number;
}

const fileInfo: FileInfo[] = JSON.parse(document.getElementById("diff-files")?.textContent || "[]");

interface PanelTree {
  tree: FileTree;
  indexByPath: Map<string, number>;
  selected: string | null;
}

/** Tree for each file index, so the scroll observer can reach the panel's tree. */
const treeByFile = new Map<number, PanelTree>();

function selectInTree(panel: PanelTree, path: string): void {
  if (panel.selected === path) return;
  if (panel.selected) panel.tree.getItem(panel.selected)?.deselect();
  panel.selected = path;
  panel.tree.getItem(path)?.select();
  panel.tree.scrollToPath(path, { focus: false, offset: "nearest" });
}

document.querySelectorAll<HTMLElement>(".file-tree").forEach((mount) => {
  const first = Number(mount.dataset.first);
  const count = Number(mount.dataset.count);
  const indexByPath = new Map<string, number>();
  const infoByPath = new Map<string, FileInfo>();
  for (let i = first; i < first + count; i++) {
    indexByPath.set(fileInfo[i].path, i);
    infoByPath.set(fileInfo[i].path, fileInfo[i]);
  }

  const panel: PanelTree = { tree: undefined!, indexByPath, selected: null };
  panel.tree = new FileTree({
    paths: [...indexByPath.keys()],
    initialExpansion: "open",
    density: "compact",
    flattenEmptyDirectories: true,
    search: true,
    gitStatus: [...infoByPath.values()].map((f) => ({ path: f.path, status: f.status })),
    renderRowDecoration: ({ item }) => {
      const info = item.kind === "file" ? infoByPath.get(item.path) : undefined;
      if (!info) return null;
      return {
        text: `+${info.additions} -${info.deletions}`,
        parts: [
          { text: `+${info.additions}`, color: "#3fb950" },
          { text: ` -${info.deletions}`, color: "#f85149" },
        ],
      };
    },
    onSelectionChange: (paths) => {
      const path = paths[paths.length - 1];
      if (!path || path === panel.selected) return;
      const index = indexByPath.get(path);
      if (index === undefined) return; // a directory
      panel.selected = path;
      document.getElementById(`file-${index}`)?.scrollIntoView({ block: "start" });
      history.replaceState(null, "", `#file-${index}`);
    },
  });
  panel.tree.render({ containerWrapper: mount });
  for (const index of indexByPath.values()) treeByFile.set(index, panel);
});

// Select the tree entry for whichever file is currently in view (files in hidden views never intersect).
(() => {
  const fileDiffs = document.querySelectorAll(".file-diff");
  if (!fileDiffs.length) return;
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const index = Number(entry.target.id.replace("file-", ""));
        const panel = treeByFile.get(index);
        if (panel) selectInTree(panel, fileInfo[index].path);
      }
    },
    { rootMargin: "-10% 0px -70% 0px", threshold: 0 },
  );
  fileDiffs.forEach((el) => observer.observe(el));
})();

// --- Resizable / collapsible file sidebar (width and collapsed state persist across runs) ---
// Every files panel (the full diff and each commit) has its own sidebar; they share one width and state.
const toggleSidebar = (() => {
  const sidebars = Array.from(document.querySelectorAll<HTMLElement>(".sidebar"));
  if (!sidebars.length) return () => {};
  let collapsed = false;

  const DEFAULT_W = 260;
  const MIN_W = 180;
  const MAX_W = 640;
  const WIDTH_KEY = "localDiff.sidebarWidth";
  const COLLAPSED_KEY = "localDiff.sidebarCollapsed";

  // localStorage is unavailable on file:// in some browsers; fall back to defaults silently.
  const store = (key: string, value: string) => {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* ignore */
    }
  };
  const load = (key: string) => {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  };

  function setWidth(px: number): number {
    const w = Math.min(MAX_W, Math.max(MIN_W, Math.round(px)));
    document.documentElement.style.setProperty("--sidebar-w", w + "px");
    return w;
  }

  function setCollapsed(value: boolean): void {
    collapsed = value;
    for (const sidebar of sidebars) {
      sidebar.classList.toggle("collapsed", collapsed);
      const toggle = sidebar.querySelector<HTMLElement>(".sidebar-toggle")!;
      const verb = collapsed ? "Expand" : "Collapse";
      toggle.innerHTML = collapsed ? "&laquo;" : "&raquo;";
      toggle.title = `${verb} file list (f)`;
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.setAttribute("aria-label", `${verb} file list`);
    }
    store(COLLAPSED_KEY, collapsed ? "1" : "0");
  }

  function onMouseDown(e: MouseEvent): void {
    e.preventDefault();
    const handle = e.currentTarget as HTMLElement;
    const sidebar = handle.nextElementSibling as HTMLElement;
    const startX = e.clientX;
    const startW = sidebar.getBoundingClientRect().width;
    let width = startW;
    handle.classList.add("active");
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";

    const onMouseMove = (ev: MouseEvent) => {
      width = setWidth(startW - (ev.clientX - startX)); // sidebar is on the right
    };
    const onMouseUp = () => {
      handle.classList.remove("active");
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      store(WIDTH_KEY, String(width));
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }

  const savedWidth = parseInt(load(WIDTH_KEY) ?? "", 10);
  if (savedWidth) setWidth(savedWidth);
  setCollapsed(load(COLLAPSED_KEY) === "1");

  document.querySelectorAll<HTMLElement>(".sidebar-handle").forEach((handle) => {
    handle.addEventListener("mousedown", onMouseDown);
    handle.addEventListener("dblclick", () => store(WIDTH_KEY, String(setWidth(DEFAULT_W))));
  });
  document.querySelectorAll(".sidebar-toggle").forEach((toggle) => {
    toggle.addEventListener("click", () => setCollapsed(!collapsed));
  });

  return () => setCollapsed(!collapsed);
})();

// --- Views: "Files changed", the commit list and one per commit. The URL hash picks the view, so
// tabs, commit links and the browser Back button all switch views; a hash naming an element inside
// a view (e.g. #file-3 or #hunk-3-0) opens that view and scrolls to it. ---

function activeView(): ParentNode {
  return document.querySelector(".view:not([hidden])") || document;
}

function showViewForHash(): void {
  const views = Array.from(document.querySelectorAll<HTMLElement>(".view"));
  if (!views.length) return;
  const hashTarget = parseHunkHash(location.hash);
  const target = hashTarget
    ? document.getElementById(`file-${hashTarget[0]}`)
    : location.hash
      ? document.getElementById(decodeURIComponent(location.hash.slice(1)))
      : null;
  const view = (target?.closest<HTMLElement>(".view")) || views[0];
  const changed = view.hidden;
  views.forEach((v) => {
    v.hidden = v !== view;
  });
  document.querySelectorAll<HTMLElement>(".tab").forEach((tab) => {
    tab.classList.toggle("selected", tab.dataset.view === view.dataset.tab);
  });
  if (target === view) {
    if (changed) window.scrollTo(0, 0);
  } else if (hashTarget && changed) {
    scrollToHunk({ file: hashTarget[0], hunk: hashTarget[1] }, false);
  } else if (target && changed) {
    target.scrollIntoView({ block: "start" });
  }
  updateHunkCounter(-1, getHunkAnchors().length);
}

// Show "3 days ago" for recent commit dates; older ones keep their "on Sep 15, 2026" text.
document.querySelectorAll<HTMLTimeElement>("time[datetime]").forEach((el) => {
  const seconds = (Date.now() - new Date(el.dateTime).getTime()) / 1000;
  if (!(seconds >= 0) || seconds > 30 * 86400) return;
  const units: [number, string][] = [
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
  ];
  const unit = units.find(([size]) => seconds >= size);
  if (!unit) {
    el.textContent = "just now";
    return;
  }
  const n = Math.floor(seconds / unit[0]);
  el.textContent = `${n} ${unit[1]}${n === 1 ? "" : "s"} ago`;
});

// --- Jump between hunks ---
// A hunk anchor is (file index, hunk index); its position is the first row the hunk renders.

interface HunkAnchor {
  file: number;
  hunk: number;
}

function parseHunkHash(hash: string): [number, number] | null {
  const m = /^#hunk-(\d+)-(\d+)$/.exec(hash);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

function getHunkAnchors(): HunkAnchor[] {
  const anchors: HunkAnchor[] = [];
  activeView()
    .querySelectorAll<HTMLElement>(".diff-body[data-file-index]")
    .forEach((body) => {
      const file = Number(body.dataset.fileIndex);
      diffData[file]?.hunks.forEach((_, hunk) => anchors.push({ file, hunk }));
    });
  return anchors;
}

/** The rendered row where a hunk starts (rendering its file first if needed). */
function hunkRow(anchor: HunkAnchor): HTMLElement | null {
  const body = document.querySelector<HTMLElement>(`.diff-body[data-file-index="${anchor.file}"]`);
  const hunk = diffData[anchor.file]?.hunks[anchor.hunk];
  if (!body || !hunk) return null;
  renderFile(body);
  const root = body.querySelector("diffs-container")?.shadowRoot;
  if (!root) return body;
  // Prefer the new side; a hunk that only deletes lines has nothing there.
  const side = hunk.additionCount > 0 ? "additions" : "deletions";
  const line = side === "additions" ? hunk.additionStart : hunk.deletionStart;
  return (
    root.querySelector<HTMLElement>(`[data-${side}] [data-line="${line}"]`) ??
    root.querySelector<HTMLElement>(`[data-line="${line}"]`) ??
    body
  );
}

/** Vertical position of a hunk relative to the viewport; unrendered files fall back to their section. */
function hunkTop(anchor: HunkAnchor): number {
  const body = document.querySelector<HTMLElement>(`.diff-body[data-file-index="${anchor.file}"]`);
  if (!body) return Infinity;
  if (!instances.has(anchor.file)) return body.getBoundingClientRect().top + anchor.hunk;
  return (hunkRow(anchor) ?? body).getBoundingClientRect().top;
}

function flash(el: HTMLElement): void {
  el.classList.remove("row-flash");
  // Force reflow so the animation restarts if the same row is jumped to again.
  void el.offsetWidth;
  el.classList.add("row-flash");
  setTimeout(() => el.classList.remove("row-flash"), 1100);
}

function scrollToHunk(anchor: HunkAnchor, smooth = true): void {
  const row = hunkRow(anchor);
  if (!row) return;
  // Freshly created rows may not be laid out (or highlighted) yet; scroll on the next frame.
  requestAnimationFrame(() => {
    row.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
    if (row.matches("[data-line]")) flash(row);
  });
  history.replaceState(null, "", `#hunk-${anchor.file}-${anchor.hunk}`);
  const anchors = getHunkAnchors();
  updateHunkCounter(
    anchors.findIndex((a) => a.file === anchor.file && a.hunk === anchor.hunk),
    anchors.length,
  );
}

function updateHunkCounter(idx: number, total: number): void {
  const el = document.getElementById("hunkCounter");
  if (!el) return;
  el.textContent = total ? `${idx >= 0 ? idx + 1 : "–"} / ${total}` : "";
}

function jumpHunk(direction: number): void {
  const anchors = getHunkAnchors();
  if (!anchors.length) return;
  const threshold = 70;
  let currentIdx = -1;
  for (let i = 0; i < anchors.length; i++) {
    if (hunkTop(anchors[i]) <= threshold) currentIdx = i;
    else break;
  }
  const target = Math.min(anchors.length - 1, Math.max(0, currentIdx + direction));
  scrollToHunk(anchors[target]);
}

document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  const target = e.composedPath()[0] as HTMLElement | undefined;
  const tag = (target?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea" || target?.isContentEditable) return;
  if (e.key === "n") {
    e.preventDefault();
    jumpHunk(1);
  } else if (e.key === "p") {
    e.preventDefault();
    jumpHunk(-1);
  } else if (e.key === "f") {
    e.preventDefault();
    toggleSidebar();
  }
});

document.getElementById("prevHunkBtn")?.addEventListener("click", () => jumpHunk(-1));
document.getElementById("nextHunkBtn")?.addEventListener("click", () => jumpHunk(1));
window.addEventListener("hashchange", showViewForHash);
showViewForHash();
