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


@dataclass
class DiffHunk:
    header: str
    lines: list = field(default_factory=list)


@dataclass
class FileDiff:
    header_lines: list = field(default_factory=list)
    old_file: str = ""
    new_file: str = ""
    status: str = "modified"  # modified, added, deleted, renamed
    hunks: list = field(default_factory=list)
    is_binary: bool = False


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"git error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def get_diff(staged_only: bool, branch: str | None) -> str:
    if branch:
        return run_git("diff", branch + "...HEAD")
    if staged_only:
        return run_git("diff", "--cached")
    return run_git("diff", "HEAD")


def parse_diff(raw: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: DiffHunk | None = None

    for line in raw.splitlines():
        if line.startswith("diff --git"):
            if current is not None:
                if current_hunk:
                    current.hunks.append(current_hunk)
                files.append(current)
            current = FileDiff(header_lines=[line])
            current_hunk = None
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                current.new_file = parts[1]
                current.old_file = line.split(" a/", 1)[1].split(" b/")[0]
            continue

        if current is None:
            continue

        if line.startswith("--- "):
            path = line[4:]
            if path == "/dev/null":
                current.status = "added"
            current.header_lines.append(line)
        elif line.startswith("+++ "):
            path = line[4:]
            if path == "/dev/null":
                current.status = "deleted"
            current.header_lines.append(line)
        elif line.startswith("Binary files"):
            current.is_binary = True
            current.header_lines.append(line)
        elif line.startswith("@@"):
            if current_hunk:
                current.hunks.append(current_hunk)
            current_hunk = DiffHunk(header=line)
        elif line.startswith("new file"):
            current.status = "added"
            current.header_lines.append(line)
        elif line.startswith("deleted file"):
            current.status = "deleted"
            current.header_lines.append(line)
        elif line.startswith("rename from"):
            current.status = "renamed"
            current.header_lines.append(line)
        elif line.startswith("rename to"):
            current.header_lines.append(line)
        elif line.startswith(("similarity index", "index ", "old mode", "new mode")):
            current.header_lines.append(line)
        elif current_hunk is not None:
            current_hunk.lines.append(line)

    if current is not None:
        if current_hunk:
            current.hunks.append(current_hunk)
        files.append(current)

    return files


def get_repo_name() -> str:
    try:
        remote = run_git("remote", "get-url", "origin").strip()
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name
    except SystemExit:
        return os.path.basename(os.getcwd())


def get_branch_name() -> str:
    return run_git("rev-parse", "--abbrev-ref", "HEAD").strip()


def get_short_sha() -> str:
    return run_git("rev-parse", "--short", "HEAD").strip()


def count_changes(files: list[FileDiff]) -> tuple[int, int]:
    additions = deletions = 0
    for f in files:
        for hunk in f.hunks:
            for line in hunk.lines:
                if line.startswith("+"):
                    additions += 1
                elif line.startswith("-"):
                    deletions += 1
    return additions, deletions


def status_badge(status: str) -> str:
    colors = {
        "added": ("#1a7f37", "#dafbe1"),
        "deleted": ("#cf222e", "#ffebe9"),
        "renamed": ("#9a6700", "#fff8c5"),
        "modified": ("#0969da", "#ddf4ff"),
    }
    fg, bg = colors.get(status, ("#656d76", "#f6f8fa"))
    label = status[0].upper()
    return f'<span class="status-badge" style="background:{bg};color:{fg}">{label}</span>'


def render_hunk_lines_split(hunks: list[DiffHunk]) -> tuple[str, str]:
    """Render hunks as two separate sets of table rows (left/right) for split view."""
    left_rows = []
    right_rows = []

    for hunk in hunks:
        old_num = new_num = 0
        header = hunk.header
        try:
            parts = header.split("@@")[1].strip()
            old_part, new_part = parts.split(" +")
            old_num = int(old_part.lstrip("-").split(",")[0]) - 1
            new_num = int(new_part.split(",")[0]) - 1
        except (IndexError, ValueError):
            pass

        left_rows.append(
            f'<tr class="hunk-header"><td class="line-num"></td>'
            f'<td class="code hunk-header-content">{html.escape(header)}</td></tr>'
        )
        right_rows.append(
            f'<tr class="hunk-header"><td class="line-num"></td>'
            f'<td class="code hunk-header-content">{html.escape(header)}</td></tr>'
        )

        i = 0
        lines = hunk.lines
        while i < len(lines):
            line = lines[i]

            if not line:
                old_num += 1
                new_num += 1
                left_rows.append(
                    f'<tr><td class="line-num">{old_num}</td><td class="code context"> </td></tr>'
                )
                right_rows.append(
                    f'<tr><td class="line-num">{new_num}</td><td class="code context"> </td></tr>'
                )
                i += 1
            elif line.startswith("-"):
                del_lines = []
                while i < len(lines) and lines[i].startswith("-"):
                    old_num += 1
                    del_lines.append((old_num, lines[i]))
                    i += 1
                add_lines = []
                while i < len(lines) and lines[i].startswith("+"):
                    new_num += 1
                    add_lines.append((new_num, lines[i]))
                    i += 1
                max_len = max(len(del_lines), len(add_lines))
                for j in range(max_len):
                    if j < len(del_lines):
                        left_rows.append(
                            f'<tr><td class="line-num del-num">{del_lines[j][0]}</td>'
                            f'<td class="code deletion">{html.escape(del_lines[j][1])}</td></tr>'
                        )
                    else:
                        left_rows.append(
                            '<tr><td class="line-num"></td><td class="code empty-side"></td></tr>'
                        )
                    if j < len(add_lines):
                        right_rows.append(
                            f'<tr><td class="line-num add-num">{add_lines[j][0]}</td>'
                            f'<td class="code addition">{html.escape(add_lines[j][1])}</td></tr>'
                        )
                    else:
                        right_rows.append(
                            '<tr><td class="line-num"></td><td class="code empty-side"></td></tr>'
                        )
            elif line.startswith("+"):
                new_num += 1
                left_rows.append(
                    '<tr><td class="line-num"></td><td class="code empty-side"></td></tr>'
                )
                right_rows.append(
                    f'<tr><td class="line-num add-num">{new_num}</td>'
                    f'<td class="code addition">{html.escape(line)}</td></tr>'
                )
                i += 1
            elif line.startswith("\\"):
                left_rows.append(
                    f'<tr><td class="line-num"></td><td class="code no-newline">{html.escape(line)}</td></tr>'
                )
                right_rows.append(
                    '<tr><td class="line-num"></td><td class="code no-newline"></td></tr>'
                )
                i += 1
            else:
                old_num += 1
                new_num += 1
                left_rows.append(
                    f'<tr><td class="line-num">{old_num}</td><td class="code context">{html.escape(line)}</td></tr>'
                )
                right_rows.append(
                    f'<tr><td class="line-num">{new_num}</td><td class="code context">{html.escape(line)}</td></tr>'
                )
                i += 1

    return "\n".join(left_rows), "\n".join(right_rows)


def render_html(files: list[FileDiff], repo: str, branch: str, sha: str, args) -> str:
    additions, deletions = count_changes(files)
    total = additions + deletions

    file_list_items = []
    diff_sections = []

    for i, f in enumerate(files):
        display_name = f.new_file or f.old_file
        file_additions = sum(1 for h in f.hunks for l in h.lines if l.startswith("+"))
        file_deletions = sum(1 for h in f.hunks for l in h.lines if l.startswith("-"))

        file_list_items.append(
            f'<a class="file-item" href="#file-{i}">'
            f'{status_badge(f.status)} '
            f'<span class="file-name">{html.escape(display_name)}</span>'
            f'<span class="file-stat">'
            f'<span class="stat-add">+{file_additions}</span> '
            f'<span class="stat-del">-{file_deletions}</span>'
            f'</span></a>'
        )

        if f.is_binary:
            body = '<div class="binary-notice">Binary file changed</div>'
        elif not f.hunks:
            body = '<div class="binary-notice">File metadata changed</div>'
        else:
            left_rows, right_rows = render_hunk_lines_split(f.hunks)
            body = (
                f'<div class="split-container">'
                f'<div class="split-left"><table class="diff-table">{left_rows}</table></div>'
                f'<div class="split-handle" title="Drag to resize"></div>'
                f'<div class="split-right"><table class="diff-table">{right_rows}</table></div>'
                f'</div>'
            )

        diff_sections.append(
            f'<div class="file-diff" id="file-{i}">'
            f'<div class="file-header">'
            f'{status_badge(f.status)} '
            f'<span class="file-path">{html.escape(display_name)}</span>'
            f'<span class="file-stat">'
            f'<span class="stat-add">+{file_additions}</span> '
            f'<span class="stat-del">-{file_deletions}</span>'
            f'</span>'
            f'</div>'
            f'{body}'
            f'</div>'
        )

    diff_label = "staged changes" if args.staged else f"branch diff vs {args.branch}" if args.branch else "uncommitted changes"

    if total > 0:
        add_pct = int(additions / total * 100)
        bar = f'<span class="bar-add" style="width:{add_pct}%"></span><span class="bar-del" style="width:{100 - add_pct}%"></span>'
    else:
        bar = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(repo)} - Local Diff</title>
<style>
:root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --add-bg: rgba(46,160,67,0.15);
    --add-line: rgba(46,160,67,0.4);
    --add-text: #3fb950;
    --del-bg: rgba(248,81,73,0.15);
    --del-line: rgba(248,81,73,0.4);
    --del-text: #f85149;
    --hunk-bg: rgba(56,139,253,0.1);
    --hunk-text: #79c0ff;
    --link: #58a6ff;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
.container {{ max-width: 1600px; margin: 0 auto; padding: 24px 16px; }}
.header {{ padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; }}
.header h1 {{ font-size: 20px; font-weight: 600; }}
.header h1 .repo {{ color: var(--link); }}
.header .meta {{ color: var(--text-muted); font-size: 13px; margin-top: 4px; }}
.summary {{ display: flex; align-items: center; gap: 16px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 16px; font-size: 14px; flex-wrap: wrap; }}
.summary .stat-add {{ color: var(--add-text); font-weight: 600; }}
.summary .stat-del {{ color: var(--del-text); font-weight: 600; }}
.summary .change-bar {{ display: flex; width: 120px; height: 8px; border-radius: 4px; overflow: hidden; }}
.bar-add {{ background: var(--add-text); }}
.bar-del {{ background: var(--del-text); }}
.file-list {{ background: var(--surface); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 24px; overflow: hidden; }}
.file-list-title {{ padding: 12px 16px; font-size: 14px; font-weight: 600; border-bottom: 1px solid var(--border); }}
.file-item {{ display: flex; align-items: center; gap: 8px; padding: 6px 16px; text-decoration: none; color: var(--text); font-size: 13px; border-bottom: 1px solid var(--border); }}
.file-item:last-child {{ border-bottom: none; }}
.file-item:hover {{ background: rgba(56,139,253,0.1); }}
.file-item .file-name {{ flex: 1; font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace; }}
.file-stat {{ font-size: 12px; white-space: nowrap; }}
.status-badge {{ display: inline-block; width: 20px; height: 20px; text-align: center; line-height: 20px; border-radius: 3px; font-size: 11px; font-weight: 700; flex-shrink: 0; }}
.file-diff {{ border: 1px solid var(--border); border-radius: 6px; margin-bottom: 16px; overflow: hidden; }}
.file-header {{ display: flex; align-items: center; gap: 8px; padding: 10px 16px; background: var(--surface); border-bottom: 1px solid var(--border); font-size: 13px; position: sticky; top: 0; z-index: 10; }}
.file-header .file-path {{ flex: 1; font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace; font-weight: 600; }}
.split-container {{ display: flex; width: 100%; overflow: hidden; }}
.split-left, .split-right {{ overflow-x: auto; overflow-y: hidden; flex: 1 1 50%; min-width: 0; }}
.split-handle {{ width: 6px; min-width: 6px; background: var(--border); cursor: col-resize; position: relative; z-index: 5; transition: background 0.15s; }}
.split-handle:hover, .split-handle.active {{ background: var(--link); }}
.diff-table {{ width: 100%; border-collapse: collapse; font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace; font-size: 12px; tab-size: 4; table-layout: fixed; }}
.diff-table td {{ padding: 0 10px; white-space: pre-wrap; word-break: break-all; vertical-align: top; }}
.line-num {{ width: 50px; min-width: 50px; max-width: 50px; text-align: right; color: var(--text-muted); user-select: none; opacity: 0.5; padding-right: 10px !important; border-right: 1px solid var(--border); }}
.code {{ padding-left: 10px !important; }}
.code.context {{ background: var(--bg); }}
.code.addition {{ background: var(--add-bg); }}
.code.deletion {{ background: var(--del-bg); }}
.code.empty-side {{ background: var(--surface); }}
.del-num {{ background: var(--del-bg); }}
.add-num {{ background: var(--add-bg); }}
.hunk-header td {{ background: var(--hunk-bg); }}
.hunk-header-content {{ color: var(--hunk-text); font-style: italic; padding: 4px 10px !important; }}
.hunk-header .line-num {{ background: var(--hunk-bg); }}
.no-newline {{ color: var(--text-muted); font-style: italic; }}
.binary-notice {{ padding: 24px; text-align: center; color: var(--text-muted); font-style: italic; background: var(--bg); }}
.empty {{ text-align: center; padding: 48px; color: var(--text-muted); }}
.footer {{ text-align: center; color: var(--text-muted); font-size: 12px; padding: 24px 0; border-top: 1px solid var(--border); margin-top: 24px; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1><span class="repo">{html.escape(repo)}</span> &mdash; Local Diff</h1>
        <div class="meta">{html.escape(branch)} @ {html.escape(sha)} &middot; {html.escape(diff_label)} &middot; {len(files)} file{"s" if len(files) != 1 else ""} changed</div>
    </div>
    <div class="summary">
        <span class="stat-add">+{additions}</span>
        <span class="stat-del">&minus;{deletions}</span>
        <span>across <strong>{len(files)}</strong> file{"s" if len(files) != 1 else ""}</span>
        <span class="change-bar">{bar}</span>
    </div>
    <div class="file-list">
        <div class="file-list-title">Changed files</div>
        {"".join(file_list_items) if file_list_items else '<div class="empty">No files</div>'}
    </div>
    {"".join(diff_sections) if diff_sections else '<div class="empty">No changes to display.</div>'}
    <div class="footer">Generated by local-diff</div>
</div>
<script>
document.querySelectorAll('.split-handle').forEach(handle => {{
    let startX, startLeftW, startRightW, leftPane, rightPane;

    function onMouseDown(e) {{
        e.preventDefault();
        leftPane = handle.previousElementSibling;
        rightPane = handle.nextElementSibling;
        const container = handle.parentElement;
        startX = e.clientX;
        startLeftW = leftPane.getBoundingClientRect().width;
        startRightW = rightPane.getBoundingClientRect().width;
        handle.classList.add('active');
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
        document.body.style.userSelect = 'none';
        document.body.style.cursor = 'col-resize';
    }}

    function onMouseMove(e) {{
        const dx = e.clientX - startX;
        const totalW = startLeftW + startRightW;
        let newLeft = startLeftW + dx;
        let newRight = startRightW - dx;
        // Enforce minimum 15% each side
        const minW = totalW * 0.15;
        if (newLeft < minW) {{ newLeft = minW; newRight = totalW - minW; }}
        if (newRight < minW) {{ newRight = minW; newLeft = totalW - minW; }}
        leftPane.style.flex = '0 0 ' + newLeft + 'px';
        rightPane.style.flex = '0 0 ' + newRight + 'px';
    }}

    function onMouseUp() {{
        handle.classList.remove('active');
        document.removeEventListener('mousemove', onMouseMove);
        document.removeEventListener('mouseup', onMouseUp);
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
    }}

    handle.addEventListener('mousedown', onMouseDown);
}});

// Sync vertical scroll between left and right panes
document.querySelectorAll('.split-container').forEach(container => {{
    const left = container.querySelector('.split-left');
    const right = container.querySelector('.split-right');
    let syncing = false;
    left.addEventListener('scroll', () => {{
        if (syncing) return;
        syncing = true;
        right.scrollTop = left.scrollTop;
        syncing = false;
    }});
    right.addEventListener('scroll', () => {{
        if (syncing) return;
        syncing = true;
        left.scrollTop = right.scrollTop;
        syncing = false;
    }});
}});
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="View local git diff as a PR-style HTML page")
    parser.add_argument("--staged", action="store_true", help="Show only staged changes")
    parser.add_argument("--branch", type=str, default=None, help="Diff against a branch (e.g. main)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    parser.add_argument("-o", "--output", type=str, default=None, help="Save HTML to this path instead of a temp file")
    args = parser.parse_args()

    check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if check.returncode != 0:
        print("Error: not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    raw_diff = get_diff(args.staged, args.branch)
    if not raw_diff.strip():
        print("No changes found.")
        sys.exit(0)

    files = parse_diff(raw_diff)
    repo = get_repo_name()
    branch = get_branch_name()
    sha = get_short_sha()

    html_content = render_html(files, repo, branch, sha, args)

    if args.output:
        out_path = os.path.abspath(args.output)
        with open(out_path, "w") as f:
            f.write(html_content)
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".html", prefix="local-diff-", delete=False)
        tmp.write(html_content.encode())
        tmp.close()
        out_path = tmp.name

    print(f"Diff written to: {out_path}")
    if not args.no_open:
        webbrowser.open(f"file://{out_path}")


if __name__ == "__main__":
    main()
