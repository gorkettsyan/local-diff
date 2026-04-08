# local-diff

View your local git changes as a GitHub-style PR diff in the browser — without pushing.

Generates an HTML page with a side-by-side split diff view, file list, line numbers, and change stats. Opens it in your default browser.

## Requirements

- Python 3.13+
- Git

## Installation

```bash
git clone <repo-url>
cd git_local_diff
bash install.sh
```

This copies `local-diff` to `~/.local/bin` (or `/usr/local/bin` if writable). If the install directory isn't on your PATH, the script will tell you how to add it.

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
local-diff --branch main    # diff current branch against main
local-diff -o diff.html     # save to a specific file
local-diff --no-open        # generate HTML without opening the browser
```

## Features

- Side-by-side split diff with a draggable resizable divider
- GitHub dark theme styling
- Clickable file list with status badges (Added, Modified, Deleted, Renamed)
- Per-file and total addition/deletion stats
- Sticky file headers
- Synced vertical scrolling between left and right panes
- No dependencies — Python standard library only

## License

MIT
