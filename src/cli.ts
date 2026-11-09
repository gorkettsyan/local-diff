/**
 * local-diff: View your uncommitted changes as a GitHub-style PR diff in the browser.
 *
 * Usage:
 *     local-diff                  # diff of all uncommitted changes (staged + unstaged)
 *     local-diff --staged         # only staged changes
 *     local-diff --branch main    # diff current branch against main, with its commits (like a PR)
 *     local-diff --branch main --head feature   # PR-style view of branch feature against main
 *     local-diff src/ tests/      # only changes under the given directories/files
 *     local-diff --compare old.py new.py   # any two files or directories, no repository needed
 */

import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, openSync, readFileSync, readSync, closeSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { getFiletypeFromFileName, getSingularPatch, type FileDiffMetadata } from "@pierre/diffs";
import { bundledLanguages } from "shiki/langs";

// The bundle lives in dist/ next to the page template and the browser script.
const DIST_DIR = dirname(fileURLToPath(import.meta.url));

type Status = "added" | "deleted" | "renamed" | "modified";

// (foreground, background) per status, using GitHub's dark-theme label colors.
const STATUS_COLORS: Record<Status, [string, string]> = {
  added: ["#3fb950", "rgba(46,160,67,0.15)"],
  deleted: ["#f85149", "rgba(248,81,73,0.15)"],
  renamed: ["#d29922", "rgba(187,128,9,0.15)"],
  modified: ["#4493f8", "rgba(56,139,253,0.15)"],
};

// --- Data structures ---

interface FileEntry {
  diff: FileDiffMetadata;
  status: Status;
  isBinary: boolean;
  additions: number;
  deletions: number;
}

interface Commit {
  sha: string;
  shortSha: string;
  author: string;
  date: Date;
  dateIso: string;
  subject: string;
  body: string;
  files: FileEntry[];
}

interface Args {
  paths: string[];
  staged: boolean;
  branch?: string;
  head: string;
  noOpen: boolean;
  noUntracked: boolean;
  compare?: [string, string];
  output?: string;
}

// --- Process helpers ---

function fail(message: string): never {
  process.stderr.write(message + "\n");
  process.exit(1);
}

function git(args: string[]): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync("git", args, { encoding: "utf8", maxBuffer: 1024 * 1024 * 1024 });
  if (result.error) fail(`Error: could not run git: ${result.error.message}`);
  return result;
}

function runGit(...args: string[]): string {
  const result = git(args);
  if (result.status !== 0) fail(`git error: ${result.stderr.trim()}`);
  return result.stdout;
}

/** Trailing pathspec args limiting a git command to the given paths (empty when unrestricted). */
function pathspec(paths: string[]): string[] {
  return paths.length ? ["--", ...paths] : [];
}

// --- Git helpers ---

function checkRef(ref: string): void {
  if (git(["rev-parse", "--verify", "--quiet", `${ref}^{commit}`]).status !== 0) {
    fail(`Error: unknown branch or commit: ${ref}`);
  }
}

/**
 * Fail fast on a pathspec that matches nothing, so a typo doesn't look like 'no changes'.
 *
 * A path missing from the working tree is still valid if git knows it (e.g. a deleted directory,
 * or one that only exists on one of the branches being compared).
 */
function checkPaths(paths: string[], refs: string[]): void {
  for (const path of paths) {
    if (existsSync(path)) continue;
    const known = refs.length
      ? refs.map((ref) => runGit("ls-tree", "-r", "--name-only", ref, "--", path)).join("")
      : runGit("ls-files", "--", path);
    if (!known.trim()) fail(`Error: path not found in repository: ${path}`);
  }
}

function getDiff(stagedOnly: boolean, branch: string | undefined, head: string, paths: string[]): string {
  const spec = pathspec(paths);
  // Three dots: changes on head since it diverged from branch, as a pull request shows them.
  if (branch) return runGit("diff", `${branch}...${head}`, ...spec);
  if (stagedOnly) return runGit("diff", "--cached", ...spec);
  return runGit("diff", "HEAD", ...spec);
}

/** Generate a unified diff for each untracked file, as if added from /dev/null. */
function getUntrackedDiff(paths: string[]): string {
  const listing = runGit("ls-files", "--others", "--exclude-standard", ...pathspec(paths));
  return listing
    .split("\n")
    .filter((f) => f.trim())
    .map(diffForUntracked)
    .join("");
}

function diffForUntracked(filepath: string): string {
  let head: Buffer;
  try {
    const fd = openSync(filepath, "r");
    head = Buffer.alloc(8192);
    const n = readSync(fd, head, 0, head.length, 0);
    closeSync(fd);
    head = head.subarray(0, n);
  } catch {
    return "";
  }

  const header = `diff --git a/${filepath} b/${filepath}\nnew file mode 100644\n--- /dev/null\n+++ b/${filepath}\n`;
  if (head.includes(0)) return header + `Binary files /dev/null and b/${filepath} differ\n`;

  let text: string;
  try {
    text = readFileSync(filepath, "utf8");
  } catch {
    return "";
  }
  if (!text) return "";

  const lines = text.split("\n");
  const endsWithNewline = lines[lines.length - 1] === "";
  if (endsWithNewline) lines.pop();
  let body = `@@ -0,0 +1,${lines.length} @@\n` + lines.map((l) => `+${l}\n`).join("");
  if (!endsWithNewline) body += "\\ No newline at end of file\n";
  return header + body;
}

/** Diff two files or directories directly, whether or not they're in a repository (git diff --no-index). */
function getCompareDiff(oldPath: string, newPath: string): string {
  const result = git(["diff", "--no-index", "--no-color", "--", oldPath, newPath]);
  // Exit status 1 just means the inputs differ.
  if (result.status !== 0 && result.status !== 1) fail(`git error: ${result.stderr.trim()}`);
  return result.stdout;
}

/**
 * When comparing directories, show file paths relative to the compared directories.
 *
 * git prints each side's path as given, minus any leading slash, so strip that root back off.
 */
function stripCompareRoots(files: FileEntry[], oldPath: string, newPath: string): void {
  if (!(isDir(oldPath) && isDir(newPath))) return;
  const trim = (p: string) => p.replace(/^\/+|\/+$/g, "") + "/";
  const oldRoot = trim(oldPath);
  const newRoot = trim(newPath);
  const strip = (p: string, first: string, second: string) => removePrefix(removePrefix(p, first), second);
  for (const f of files) {
    // A file only on one side has both paths under that side's root.
    f.diff.name = strip(f.diff.name, newRoot, oldRoot);
    if (f.diff.prevName !== undefined) f.diff.prevName = strip(f.diff.prevName, oldRoot, newRoot);
  }
}

function removePrefix(s: string, prefix: string): string {
  return s.startsWith(prefix) ? s.slice(prefix.length) : s;
}

function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

function getRepoName(): string {
  const result = git(["remote", "get-url", "origin"]);
  if (result.status === 0) {
    const name = result.stdout.trim().replace(/\/+$/, "").split("/").pop() ?? "";
    if (name) return name.replace(/\.git$/, "");
  }
  return basename(process.cwd());
}

function getBranchName(): string {
  return runGit("rev-parse", "--abbrev-ref", "HEAD").trim();
}

function getShortSha(ref = "HEAD"): string {
  return runGit("rev-parse", "--short", ref).trim();
}

/** Commits on head that aren't on base, oldest first (the commit list of a pull request). */
function getCommits(base: string, head: string, paths: string[]): Commit[] {
  const fields = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%b%x1e";
  const out = runGit("log", "--reverse", `--format=${fields}`, `${base}..${head}`, ...pathspec(paths));
  const commits: Commit[] = [];
  for (const raw of out.split("\x1e")) {
    const record = raw.replace(/^\n+|\n+$/g, "");
    if (!record) continue;
    const [sha, shortSha, author, dateIso, subject, body] = record.split("\x1f");
    commits.push({ sha, shortSha, author, date: new Date(dateIso), dateIso, subject, body: body.trim(), files: [] });
  }
  return commits;
}

/** A single commit's changes; merge commits are shown against their first parent. */
function getCommitDiff(sha: string, paths: string[]): string {
  return runGit("show", "--format=", "--diff-merges=first-parent", sha, ...pathspec(paths));
}

// --- Diff parsing ---

/** Split a multi-file git diff into per-file chunks and parse each with @pierre/diffs. */
function parseDiff(raw: string): FileEntry[] {
  const chunks = raw.split(/^(?=diff --git )/m).filter((c) => c.startsWith("diff --git "));
  return chunks.map((chunk) => {
    const diff = getSingularPatch(chunk);
    const isBinary = /^Binary files .* differ$/m.test(chunk) || /^GIT binary patch$/m.test(chunk);
    let additions = 0;
    let deletions = 0;
    for (const h of diff.hunks) {
      additions += h.additionLines;
      deletions += h.deletionLines;
    }
    return { diff, status: statusOf(diff), isBinary, additions, deletions };
  });
}

function statusOf(diff: FileDiffMetadata): Status {
  switch (diff.type) {
    case "new":
      return "added";
    case "deleted":
      return "deleted";
    case "rename-pure":
    case "rename-changed":
      return "renamed";
    default:
      return "modified";
  }
}

// --- HTML rendering (page chrome; the diffs themselves are rendered in the browser by @pierre/diffs) ---

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function statusBadge(status: Status): string {
  const [fg, bg] = STATUS_COLORS[status];
  return `<span class="status-badge" style="background:${bg};color:${fg}">${status[0].toUpperCase()}</span>`;
}

function fileStat(additions: number, deletions: number): string {
  return `<span class="file-stat"><span class="stat-add">+${additions}</span> <span class="stat-del">-${deletions}</span></span>`;
}

function displayName(f: FileEntry): string {
  return f.diff.prevName && f.diff.prevName !== f.diff.name ? `${f.diff.prevName} → ${f.diff.name}` : f.diff.name;
}

/**
 * One file's section. The diff body is a placeholder the page script fills in with a
 * @pierre/diffs split view; its parsed diff travels in the page's JSON data under the same index.
 */
function renderFileDiff(index: number, f: FileEntry): string {
  let body: string;
  if (f.isBinary) body = '<div class="binary-notice">Binary file changed</div>';
  else if (!f.diff.hunks.length) body = '<div class="binary-notice">File metadata changed</div>';
  else body = `<div class="diff-body" data-file-index="${index}" style="min-height:${Math.min(f.diff.splitLineCount, 400) * 18}px"></div>`;
  return (
    `<div class="file-diff" id="file-${index}">` +
    `<div class="file-header">${statusBadge(f.status)} <span class="file-path">${esc(displayName(f))}</span>` +
    `${fileStat(f.additions, f.deletions)}</div>${body}</div>`
  );
}

function renderChangeBar(additions: number, deletions: number): string {
  const total = additions + deletions;
  if (!total) return "";
  const addPct = Math.floor((additions / total) * 100);
  return `<span class="bar-add" style="width:${addPct}%"></span><span class="bar-del" style="width:${100 - addPct}%"></span>`;
}

/** Floating prev/next buttons for jumping between changes across the whole diff. */
function renderHunkNav(files: FileEntry[]): string {
  if (!files.some((f) => f.diff.hunks.length)) return "";
  return (
    '<div class="hunk-nav" title="Jump between changes (n / p)">' +
    '<button type="button" id="prevHunkBtn" aria-label="Previous change">&uarr;</button>' +
    '<span id="hunkCounter" class="hunk-counter"></span>' +
    '<button type="button" id="nextHunkBtn" aria-label="Next change">&darr;</button>' +
    "</div>"
  );
}

/**
 * Summary bar, file sidebar and diff sections for one set of files. Indexes start at firstIndex
 * so element ids stay unique when a page holds several panels (the full diff plus one per commit).
 */
function renderFilesPanel(files: FileEntry[], firstIndex: number, emptyMessage: string): string {
  if (!files.length) return `<div class="empty">${esc(emptyMessage)}</div>`;
  const additions = files.reduce((n, f) => n + f.additions, 0);
  const deletions = files.reduce((n, f) => n + f.deletions, 0);
  const sections = files.map((f, i) => renderFileDiff(firstIndex + i, f)).join("");
  return (
    `<div class="summary"><span class="stat-add">+${additions}</span><span class="stat-del">&minus;${deletions}</span>` +
    `<span>across <strong>${plural(files.length, "file")}</strong> changed</span>` +
    `<span class="change-bar">${renderChangeBar(additions, deletions)}</span></div>` +
    `<div class="layout"><div class="main">${sections}</div>` +
    `<div class="sidebar-handle" title="Drag to resize (double-click to reset)"></div>` +
    `<aside class="sidebar"><div class="file-list"><div class="file-list-title">` +
    `<span class="file-list-label">Changed files</span>` +
    `<button type="button" class="sidebar-toggle" aria-expanded="true">&raquo;</button></div>` +
    // The page script mounts a @pierre/trees file tree here for files firstIndex..firstIndex+count-1.
    `<div class="file-list-body file-tree" data-first="${firstIndex}" data-count="${files.length}"></div></div></aside></div>`
  );
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Sep 15, 2026", in the commit's own timezone (the date part of its ISO timestamp). */
function formatDate(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

function avatar(author: string): string {
  let sum = 0;
  for (const ch of author) sum += ch.codePointAt(0)!;
  const hue = (sum * 37) % 360;
  const initial = esc(author.slice(0, 1).toUpperCase() || "?");
  return `<span class="avatar" style="background:hsl(${hue},45%,38%)" title="${esc(author)}">${initial}</span>`;
}

/** '<author> committed <date>'; the page script rewrites recent dates as relative times. */
function committed(c: Commit): string {
  const title = `${c.dateIso.slice(0, 10)} ${c.dateIso.slice(11, 16)} ${c.dateIso.slice(19).replace(":", "") || "+0000"}`;
  return (
    `<strong>${esc(c.author)}</strong> committed ` +
    `<time datetime="${esc(c.dateIso)}" title="${esc(title)}">on ${formatDate(c.dateIso)}</time>`
  );
}

/** Commits grouped by day, oldest first, each linking to its own diff view. */
function renderCommitList(commits: Commit[]): string {
  const groups = new Map<string, [number, Commit][]>();
  commits.forEach((c, i) => {
    const day = formatDate(c.dateIso);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day)!.push([i, c]);
  });

  const sections = [...groups].map(([day, entries]) => {
    const rows = entries
      .map(
        ([i, c]) =>
          `<a class="commit-row" href="#view-commit-${i}">${avatar(c.author)}` +
          `<span class="commit-row-main"><span class="commit-subject">${esc(c.subject)}</span>` +
          `<span class="commit-row-meta">${committed(c)}</span></span>` +
          fileStat(
            c.files.reduce((n, f) => n + f.additions, 0),
            c.files.reduce((n, f) => n + f.deletions, 0),
          ) +
          `<code class="commit-sha">${esc(c.shortSha)}</code></a>`,
      )
      .join("");
    return (
      `<div class="commit-group"><h3 class="commit-group-title">Commits on ${day}</h3>` +
      `<div class="commit-list">${rows}</div></div>`
    );
  });
  return sections.join("") || '<div class="empty">No commits.</div>';
}

function renderCommitHeader(commits: Commit[], index: number): string {
  const c = commits[index];
  const nav = (target: number, label: string) =>
    target >= 0 && target < commits.length
      ? `<a class="btn" href="#view-commit-${target}">${label}</a>`
      : `<span class="btn disabled">${label}</span>`;
  const body = c.body ? `<pre class="commit-body">${esc(c.body)}</pre>` : "";
  return (
    `<div class="commit-header"><div class="commit-nav">` +
    `<a class="back-link" href="#view-commits">&lsaquo; All commits</a>` +
    `<span class="commit-position">Commit ${index + 1} of ${commits.length}</span>` +
    `<span class="btn-group">${nav(index - 1, "&lsaquo; Prev")}${nav(index + 1, "Next &rsaquo;")}</span></div>` +
    `<h2 class="commit-title">${esc(c.subject)}</h2>${body}` +
    `<div class="commit-meta">${avatar(c.author)}<span>${committed(c)}</span>` +
    `<span class="commit-full-sha">commit <code>${esc(c.sha)}</code></span></div></div>`
  );
}

function renderTabs(commitCount: number, fileCount: number): string {
  return (
    '<nav class="tabs">' +
    `<a class="tab" href="#view-commits" data-view="view-commits">Commits <span class="counter">${commitCount}</span></a>` +
    `<a class="tab selected" href="#view-files" data-view="view-files">Files changed <span class="counter">${fileCount}</span></a>` +
    "</nav>"
  );
}

/** Embed JSON in a <script> element safely (no "</script>" or HTML comment openers can leak through). */
function jsonForScript(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c");
}

/**
 * Syntax grammars for the languages in these files. The page's script bundle leaves Shiki's
 * grammars out (they total ~8 MB), so each page carries just the ones it uses.
 */
async function loadGrammars(diffs: FileDiffMetadata[]): Promise<Record<string, unknown>> {
  const langs = new Set(diffs.map((d) => d.lang ?? getFiletypeFromFileName(d.name)));
  const grammars: Record<string, unknown> = {};
  for (const lang of langs) {
    const loader = (bundledLanguages as Record<string, () => Promise<{ default: unknown }>>)[lang];
    if (loader) grammars[lang] = (await loader()).default;
  }
  return grammars;
}

/**
 * Render the page. With commits (branch comparisons) it gets PR-style tabs: the combined
 * "Files changed" view, a commit list, and a diff view per commit.
 */
async function renderHtml(files: FileEntry[], repo: string, meta: string, commits?: Commit[]): Promise<string> {
  const views = [
    `<section class="view" id="view-files" data-tab="view-files">${renderFilesPanel(files, 0, "No changes to display.")}</section>`,
  ];
  let tabs = "";
  const allFiles = [...files];
  if (commits) {
    tabs = renderTabs(commits.length, files.length);
    views.push(`<section class="view" id="view-commits" data-tab="view-commits" hidden>${renderCommitList(commits)}</section>`);
    commits.forEach((c, i) => {
      const panel = renderFilesPanel(c.files, allFiles.length, "No file changes in this commit.");
      allFiles.push(...c.files);
      views.push(
        `<section class="view" id="view-commit-${i}" data-tab="view-commits" hidden>${renderCommitHeader(commits, i)}${panel}</section>`,
      );
    });
  }

  // Only files with something to draw need their parsed diff in the page.
  const data = allFiles.map((f) => (f.isBinary || !f.diff.hunks.length ? null : f.diff));
  // What the sidebar file trees show for each file.
  const fileInfo = allFiles.map((f) => ({ path: f.diff.name, status: f.status, additions: f.additions, deletions: f.deletions }));
  const values: Record<string, string> = {
    repo: esc(repo),
    meta,
    tabs,
    views: views.join(""),
    hunk_nav: renderHunkNav(allFiles),
    diff_data: jsonForScript(data),
    diff_files: jsonForScript(fileInfo),
    diff_grammars: jsonForScript(await loadGrammars(data.filter((d) => d !== null))),
    client_js: readFileSync(join(DIST_DIR, "client.js"), "utf8").replace(/<\/script/gi, "<\\/script"),
  };
  const template = readFileSync(join(DIST_DIR, "diff.html"), "utf8");
  // Single pass, so placeholder-like text inside the substituted values is left alone.
  return template.replace(/\$\{(\w+)\}/g, (match, key: string) => values[key] ?? match);
}

// --- CLI ---

/** The HTML subtitle under the page title, e.g. "main ← feature @ abc1234 · 3 commits · 5 files changed". */
function getPageMeta(args: Args, files: FileEntry[], commits?: Commit[]): string {
  const parts: string[] = [];
  if (args.compare) {
    parts.push(`${esc(args.compare[0])} &rarr; ${esc(args.compare[1])}`);
  } else if (args.branch) {
    parts.push(`${esc(args.branch)} &larr; ${esc(args.head)} @ ${esc(getShortSha(args.head))}`);
    parts.push(plural(commits?.length ?? 0, "commit"));
  } else {
    parts.push(`${esc(getBranchName())} @ ${esc(getShortSha())}`);
    parts.push(args.staged ? "staged changes" : "uncommitted changes");
  }
  if (args.paths.length) parts.push("in " + esc(args.paths.join(", ")));
  parts.push(`${plural(files.length, "file")} changed`);
  return parts.join(" &middot; ");
}

function writeOutput(content: string, outputPath?: string): string {
  const path = outputPath ? resolve(outputPath) : join(mkdtempSync(join(tmpdir(), "local-diff-")), "diff.html");
  writeFileSync(path, content);
  return path;
}

function openInBrowser(path: string): void {
  const url = "file://" + path;
  const [cmd, args] =
    process.platform === "darwin"
      ? ["open", [url]]
      : process.platform === "win32"
        ? ["cmd", ["/c", "start", "", url]]
        : ["xdg-open", [url]];
  const result = spawnSync(cmd, args, { stdio: "ignore" });
  if (result.error || result.status !== 0) process.stderr.write(`Could not open a browser; open ${url} manually.\n`);
}

function publish(html: string, args: Args): void {
  const outPath = writeOutput(html, args.output);
  console.log(`Diff written to: ${outPath}`);
  if (!args.noOpen) openInBrowser(outPath);
}

async function runCompare(args: Args): Promise<void> {
  const [oldPath, newPath] = args.compare!;
  for (const path of [oldPath, newPath]) {
    if (!existsSync(path)) fail(`Error: path not found: ${path}`);
  }
  if (isDir(oldPath) !== isDir(newPath)) fail("Error: --compare needs two files or two directories.");

  const rawDiff = getCompareDiff(oldPath, newPath);
  if (!rawDiff.trim()) {
    console.log("No differences found.");
    process.exit(0);
  }

  const files = parseDiff(rawDiff);
  stripCompareRoots(files, oldPath, newPath);
  publish(await renderHtml(files, basename(resolve(newPath)), getPageMeta(args, files)), args);
}

const USAGE = `usage: local-diff [-h] [--staged] [--branch BRANCH] [--head HEAD] [--no-open]
                  [--no-untracked] [--compare OLD NEW] [-o OUTPUT] [PATH ...]

View local git diff as a PR-style HTML page

positional arguments:
  PATH               Limit the diff to these directories or files (default: whole repository)

options:
  -h, --help         show this help message and exit
  --staged           Show only staged changes
  --branch BRANCH    Base branch to compare against, like a PR's target (e.g. main)
  --head HEAD        Branch or commit to review against --branch (default: HEAD)
  --no-open          Don't auto-open the browser
  --no-untracked     Exclude untracked files from the diff
  --compare OLD NEW  Compare two files or directories directly, inside or outside a git repository
  -o, --output OUTPUT
                     Save HTML to this path instead of a temp file`;

function usageError(message: string): never {
  fail(`${USAGE.split("\n\n")[0]}\nlocal-diff: error: ${message}`);
}

function parseCli(argv: string[]): Args {
  // --compare takes two values; pull it out before handing the rest to parseArgs.
  let compare: [string, string] | undefined;
  const rest: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--compare") {
      if (argv.length - i - 1 < 2) usageError("argument --compare: expected 2 arguments");
      compare = [argv[i + 1], argv[i + 2]];
      i += 2;
    } else {
      rest.push(argv[i]);
    }
  }

  let parsed;
  try {
    parsed = parseArgs({
      args: rest,
      allowPositionals: true,
      options: {
        help: { type: "boolean", short: "h" },
        staged: { type: "boolean" },
        branch: { type: "string" },
        head: { type: "string" },
        "no-open": { type: "boolean" },
        "no-untracked": { type: "boolean" },
        output: { type: "string", short: "o" },
      },
    });
  } catch (e) {
    usageError((e as Error).message);
  }
  const { values, positionals } = parsed;
  if (values.help) {
    console.log(USAGE);
    process.exit(0);
  }

  const args: Args = {
    paths: positionals,
    staged: !!values.staged,
    branch: values.branch,
    head: values.head ?? "HEAD",
    noOpen: !!values["no-open"],
    noUntracked: !!values["no-untracked"],
    compare,
    output: values.output,
  };
  if (values.head && !args.branch) usageError("--head requires --branch (the base to compare against)");
  if (args.staged && args.branch) usageError("--staged can't be combined with --branch");
  if (args.compare && (args.staged || args.branch || args.paths.length)) {
    usageError("--compare can't be combined with --staged, --branch or paths");
  }
  return args;
}

async function main(): Promise<void> {
  const args = parseCli(process.argv.slice(2));

  if (args.compare) {
    await runCompare(args);
    return;
  }

  if (git(["rev-parse", "--is-inside-work-tree"]).status !== 0) fail("Error: not inside a git repository.");

  const refs = args.branch ? [args.branch, args.head] : [];
  refs.forEach(checkRef);
  checkPaths(args.paths, refs);

  let rawDiff = getDiff(args.staged, args.branch, args.head, args.paths);
  if (!args.noUntracked && !args.staged && !args.branch) rawDiff += getUntrackedDiff(args.paths);

  let commits: Commit[] | undefined;
  if (args.branch) {
    commits = getCommits(args.branch, args.head, args.paths);
    for (const commit of commits) commit.files = parseDiff(getCommitDiff(commit.sha, args.paths));
  }

  if (!rawDiff.trim() && !commits?.length) {
    const scope = args.paths.length ? ` in ${args.paths.join(", ")}` : "";
    console.log(`No changes found${scope}.`);
    process.exit(0);
  }

  const files = parseDiff(rawDiff);
  publish(await renderHtml(files, getRepoName(), getPageMeta(args, files, commits), commits), args);
}

await main();
