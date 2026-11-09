# local-diff

View your local git changes as a GitHub-style PR diff in the browser — without pushing.

Generates an HTML page with a side-by-side split diff view, file list, line numbers, and change stats. Opens it in your default browser.

## Requirements

- Node.js 20+
- Git

## Installation

```bash
git clone <repo-url>
cd git_local_diff
bash install.sh
```

This builds the TypeScript sources with esbuild and installs `local-diff` to `~/.local/bin` (or `/usr/local/bin` if writable). If the install directory isn't on your PATH, the script will tell you how to add it.

To specify a custom install directory:

```bash
LOCAL_DIFF_INSTALL_DIR=/usr/local/bin bash install.sh
```

## Uninstallation

```bash
bash uninstall.sh
```

## Usage

```bash
local-diff                  # all uncommitted changes (staged + unstaged) vs HEAD
local-diff --staged         # only staged changes
local-diff --branch main    # current branch vs main, PR-style with its commits
local-diff --branch main --head feature   # review branch feature against main, like a PR
local-diff src/             # only changes under src/
local-diff src/ tests/      # limit to several directories or files
local-diff --staged src/    # combine with any other mode
local-diff --compare old.py new.py   # compare any two files, no repository needed
local-diff --compare v1/ v2/         # or two directories (added/deleted files included)
local-diff -o diff.html     # save to a specific file
local-diff --no-open        # generate HTML without opening the browser
```

Paths are relative to your current directory, so running `local-diff .` inside a subdirectory scopes the diff to that subdirectory.

## Features

- PR-style branch comparison (`--branch`/`--head`): "Files changed" and "Commits" tabs, commits grouped by day, and a diff view per commit with previous/next navigation
- Compare any two files or directories with `--compare OLD NEW`, inside or outside a git repository
- Diffs rendered by [@pierre/diffs](https://diffs.com): split view with word-level change highlighting
- GitHub dark theme styling (Primer colors)
- Syntax highlighting via Shiki (only the grammars a diff needs are embedded in the page)
- Resizable, collapsible file sidebar — drag its edge to widen (double-click to reset), or press `f` to collapse it; the width and collapsed state are remembered
- File tree sidebar ([@pierre/trees](https://www.npmjs.com/package/@pierre/trees)) with git-status colors, per-file +/- counts and search; follows your scroll position
- Per-file and total addition/deletion stats
- Sticky file headers
- Self-contained HTML output (no server, works offline)

## Development

```bash
npm install
npm run build       # bundles src/ into dist/
npm run typecheck
node dist/local-diff.js --no-open -o /tmp/diff.html
```

## License

MIT
