#!/usr/bin/env python3
"""
local-diff: View your uncommitted changes as a GitHub-style PR diff in the browser.

Usage:
    local-diff                  # diff of all uncommitted changes (staged + unstaged)
    local-diff --staged         # only staged changes
    local-diff --branch main    # diff current branch against main
"""

import argparse
import html
import os
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

STATUS_COLORS = {
    "added": ("#1a7f37", "#dafbe1"),
    "deleted": ("#cf222e", "#ffebe9"),
    "renamed": ("#9a6700", "#fff8c5"),
    "modified": ("#0969da", "#ddf4ff"),
}

HEADER_PREFIXES = {
    "new file": "added",
    "deleted file": "deleted",
    "rename from": "renamed",
}

METADATA_PREFIXES = ("similarity index", "index ", "old mode", "new mode", "rename to")


# --- Data structures ---

@dataclass
class DiffHunk:
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    header_lines: list[str] = field(default_factory=list)
    old_file: str = ""
    new_file: str = ""
    status: str = "modified"
    hunks: list[DiffHunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def display_name(self) -> str:
        return self.new_file or self.old_file

    @property
    def additions(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.startswith("+"))

    @property
    def deletions(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.startswith("-"))


# --- Git helpers ---

def run_git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def get_diff(staged_only: bool, branch: str | None) -> str:
    if branch:
        return run_git("diff", f"{branch}...HEAD")
    if staged_only:
        return run_git("diff", "--cached")
    return run_git("diff", "HEAD")


def get_untracked_diff() -> str:
    """Generate a unified diff for each untracked file, as if added from /dev/null."""
    untracked = [f for f in run_git("ls-files", "--others", "--exclude-standard").splitlines() if f.strip()]
    return "".join(_diff_for_untracked(fp) for fp in untracked)


def _diff_for_untracked(filepath: str) -> str:
    try:
        raw = open(filepath, "rb").read(8192)
    except (OSError, PermissionError):
        return ""

    header = f"diff --git a/{filepath} b/{filepath}\nnew file mode 100644\n--- /dev/null\n+++ b/{filepath}\n"

    if b"\x00" in raw:
        return header + f"Binary files /dev/null and b/{filepath} differ\n"

    try:
        lines = open(filepath, "r", errors="replace").readlines()
    except (OSError, PermissionError):
        return ""

    if not lines:
        return ""

    body = f"@@ -0,0 +1,{len(lines)} @@\n"
    body += "".join(f"+{line.rstrip(chr(10))}\n" for line in lines)
    return header + body


def get_repo_name() -> str:
    try:
        remote = run_git("remote", "get-url", "origin").strip()
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        return name.removesuffix(".git")
    except SystemExit:
        return os.path.basename(os.getcwd())


def get_branch_name() -> str:
    return run_git("rev-parse", "--abbrev-ref", "HEAD").strip()


def get_short_sha() -> str:
    return run_git("rev-parse", "--short", "HEAD").strip()


# --- Diff parsing ---

def parse_diff(raw: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: DiffHunk | None = None

    for line in raw.splitlines():
        if line.startswith("diff --git"):
            current = _flush_and_start(files, current, current_hunk, line)
            current_hunk = None
            continue

        if current is None:
            continue

        current_hunk = _process_line(line, current, current_hunk)

    _flush_file(files, current, current_hunk)
    return files


def _flush_and_start(files: list[FileDiff], current: FileDiff | None, hunk: DiffHunk | None, line: str) -> FileDiff:
    _flush_file(files, current, hunk)
    new_file = FileDiff(header_lines=[line])
    parts = line.split(" b/", 1)
    if len(parts) == 2:
        new_file.new_file = parts[1]
        new_file.old_file = line.split(" a/", 1)[1].split(" b/")[0]
    return new_file


def _flush_file(files: list[FileDiff], current: FileDiff | None, hunk: DiffHunk | None) -> None:
    if current is None:
        return
    if hunk:
        current.hunks.append(hunk)
    files.append(current)


def _process_line(line: str, current: FileDiff, current_hunk: DiffHunk | None) -> DiffHunk | None:
    if line.startswith("--- ") and line[4:] == "/dev/null":
        current.status = "added"
        current.header_lines.append(line)
        return current_hunk

    if line.startswith("+++ ") and line[4:] == "/dev/null":
        current.status = "deleted"
        current.header_lines.append(line)
        return current_hunk

    if line.startswith(("--- ", "+++ ")):
        current.header_lines.append(line)
        return current_hunk

    if line.startswith("Binary files"):
        current.is_binary = True
        current.header_lines.append(line)
        return current_hunk

    if line.startswith("@@"):
        if current_hunk:
            current.hunks.append(current_hunk)
        return DiffHunk(header=line)

    for prefix, status in HEADER_PREFIXES.items():
        if line.startswith(prefix):
            current.status = status
            current.header_lines.append(line)
            return current_hunk

    if line.startswith(METADATA_PREFIXES):
        current.header_lines.append(line)
        return current_hunk

    if current_hunk is not None:
        current_hunk.lines.append(line)

    return current_hunk


# --- HTML rendering ---

def status_badge(status: str) -> str:
    fg, bg = STATUS_COLORS.get(status, ("#656d76", "#f6f8fa"))
    return f'<span class="status-badge" style="background:{bg};color:{fg}">{status[0].upper()}</span>'


def _row(num_cls: str, num: str, code_cls: str, content: str) -> str:
    return f'<tr><td class="line-num {num_cls}">{num}</td><td class="code {code_cls}">{content}</td></tr>'


def _parse_hunk_start(header: str) -> tuple[int, int]:
    try:
        parts = header.split("@@")[1].strip()
        old_part, new_part = parts.split(" +")
        old = int(old_part.lstrip("-").split(",")[0]) - 1
        new = int(new_part.split(",")[0]) - 1
        return old, new
    except (IndexError, ValueError):
        return 0, 0


def render_split_rows(hunks: list[DiffHunk]) -> tuple[str, str]:
    """Render hunks as two separate sets of table rows for left/right split view."""
    left: list[str] = []
    right: list[str] = []

    for hunk in hunks:
        old_num, new_num = _parse_hunk_start(hunk.header)
        escaped_header = html.escape(hunk.header)

        left.append(_row("", "", "hunk-header-content", escaped_header))
        right.append(_row("", "", "hunk-header-content", escaped_header))
        # Add hunk-header class to the last appended rows
        left[-1] = left[-1].replace("<tr>", '<tr class="hunk-header">')
        right[-1] = right[-1].replace("<tr>", '<tr class="hunk-header">')

        i = 0
        lines = hunk.lines
        while i < len(lines):
            line = lines[i]

            # Context line (including empty lines from diff)
            if not line or not line[0] in "+-\\":
                old_num += 1
                new_num += 1
                content = html.escape(line) if line else " "
                left.append(_row("", str(old_num), "context", content))
                right.append(_row("", str(new_num), "context", content))
                i += 1
                continue

            # No-newline marker
            if line.startswith("\\"):
                left.append(_row("", "", "no-newline", html.escape(line)))
                right.append(_row("", "", "no-newline", ""))
                i += 1
                continue

            # Collect consecutive deletions then additions and pair them
            if line.startswith("-"):
                del_lines, add_lines, i, old_num, new_num = _collect_change_pair(lines, i, old_num, new_num)
                _append_paired_rows(left, right, del_lines, add_lines)
                continue

            # Standalone addition (no preceding deletion)
            new_num += 1
            left.append(_row("", "", "empty-side", ""))
            right.append(_row("add-num", str(new_num), "addition", html.escape(line)))
            i += 1

    return "\n".join(left), "\n".join(right)


def _collect_change_pair(
    lines: list[str], i: int, old_num: int, new_num: int
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], int, int, int]:
    del_lines: list[tuple[int, str]] = []
    while i < len(lines) and lines[i].startswith("-"):
        old_num += 1
        del_lines.append((old_num, lines[i]))
        i += 1

    add_lines: list[tuple[int, str]] = []
    while i < len(lines) and lines[i].startswith("+"):
        new_num += 1
        add_lines.append((new_num, lines[i]))
        i += 1

    return del_lines, add_lines, i, old_num, new_num


def _append_paired_rows(
    left: list[str],
    right: list[str],
    del_lines: list[tuple[int, str]],
    add_lines: list[tuple[int, str]],
) -> None:
    for j in range(max(len(del_lines), len(add_lines))):
        if j < len(del_lines):
            left.append(_row("del-num", str(del_lines[j][0]), "deletion", html.escape(del_lines[j][1])))
        else:
            left.append(_row("", "", "empty-side", ""))

        if j < len(add_lines):
            right.append(_row("add-num", str(add_lines[j][0]), "addition", html.escape(add_lines[j][1])))
        else:
            right.append(_row("", "", "empty-side", ""))


def render_file_list_item(index: int, f: FileDiff) -> str:
    return (
        f'<a class="file-item" href="#file-{index}">'
        f'{status_badge(f.status)} '
        f'<span class="file-name">{html.escape(f.display_name)}</span>'
        f'<span class="file-stat">'
        f'<span class="stat-add">+{f.additions}</span> '
        f'<span class="stat-del">-{f.deletions}</span>'
        f'</span></a>'
    )


def render_file_diff(index: int, f: FileDiff) -> str:
    body = _render_file_body(f)
    return (
        f'<div class="file-diff" id="file-{index}">'
        f'<div class="file-header">'
        f'{status_badge(f.status)} '
        f'<span class="file-path">{html.escape(f.display_name)}</span>'
        f'<span class="file-stat">'
        f'<span class="stat-add">+{f.additions}</span> '
        f'<span class="stat-del">-{f.deletions}</span>'
        f'</span></div>'
        f'{body}</div>'
    )


def _render_file_body(f: FileDiff) -> str:
    if f.is_binary:
        return '<div class="binary-notice">Binary file changed</div>'
    if not f.hunks:
        return '<div class="binary-notice">File metadata changed</div>'

    left_rows, right_rows = render_split_rows(f.hunks)
    return (
        f'<div class="split-container">'
        f'<div class="split-left"><table class="diff-table">{left_rows}</table></div>'
        f'<div class="split-handle" title="Drag to resize"></div>'
        f'<div class="split-right"><table class="diff-table">{right_rows}</table></div>'
        f'</div>'
    )


def render_change_bar(additions: int, deletions: int) -> str:
    total = additions + deletions
    if total == 0:
        return ""
    add_pct = int(additions / total * 100)
    return (
        f'<span class="bar-add" style="width:{add_pct}%"></span>'
        f'<span class="bar-del" style="width:{100 - add_pct}%"></span>'
    )


def load_template() -> Template:
    template_path = TEMPLATE_DIR / "diff.html"
    return Template(template_path.read_text())


def render_html(files: list[FileDiff], repo: str, branch: str, sha: str, diff_label: str) -> str:
    additions = sum(f.additions for f in files)
    deletions = sum(f.deletions for f in files)
    file_count = f'{len(files)} file{"s" if len(files) != 1 else ""}'

    file_list = "".join(render_file_list_item(i, f) for i, f in enumerate(files))
    diff_sections = "".join(render_file_diff(i, f) for i, f in enumerate(files))

    template = load_template()
    return template.safe_substitute(
        repo=html.escape(repo),
        branch=html.escape(branch),
        sha=html.escape(sha),
        diff_label=html.escape(diff_label),
        file_count=file_count,
        additions=additions,
        deletions=deletions,
        change_bar=render_change_bar(additions, deletions),
        file_list=file_list or '<div class="empty">No files</div>',
        diff_sections=diff_sections or '<div class="empty">No changes to display.</div>',
    )


# --- CLI ---

def get_diff_label(args: argparse.Namespace) -> str:
    if args.staged:
        return "staged changes"
    if args.branch:
        return f"branch diff vs {args.branch}"
    return "uncommitted changes"


def write_output(content: str, output_path: str | None) -> str:
    if output_path:
        path = os.path.abspath(output_path)
        with open(path, "w") as f:
            f.write(content)
        return path

    tmp = tempfile.NamedTemporaryFile(suffix=".html", prefix="local-diff-", delete=False)
    tmp.write(content.encode())
    tmp.close()
    return tmp.name


def main():
    parser = argparse.ArgumentParser(description="View local git diff as a PR-style HTML page")
    parser.add_argument("--staged", action="store_true", help="Show only staged changes")
    parser.add_argument("--branch", type=str, default=None, help="Diff against a branch (e.g. main)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    parser.add_argument("--no-untracked", action="store_true", help="Exclude untracked files from the diff")
    parser.add_argument("-o", "--output", type=str, default=None, help="Save HTML to this path instead of a temp file")
    args = parser.parse_args()

    check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if check.returncode != 0:
        print("Error: not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    raw_diff = get_diff(args.staged, args.branch)
    if not args.no_untracked and not args.staged and not args.branch:
        raw_diff += get_untracked_diff()

    if not raw_diff.strip():
        print("No changes found.")
        sys.exit(0)

    files = parse_diff(raw_diff)
    html_content = render_html(
        files,
        repo=get_repo_name(),
        branch=get_branch_name(),
        sha=get_short_sha(),
        diff_label=get_diff_label(args),
    )

    out_path = write_output(html_content, args.output)
    print(f"Diff written to: {out_path}")

    if not args.no_open:
        webbrowser.open(f"file://{out_path}")


if __name__ == "__main__":
    main()
