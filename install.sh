#!/usr/bin/env bash
set -e

COMMAND_NAME="local-diff"
SCRIPT_NAME="local_diff.py"

# Resolve the directory this installer lives in
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/$SCRIPT_NAME"
TEMPLATES_DIR="$SCRIPT_DIR/templates"

if [ ! -f "$SOURCE" ]; then
    echo "Error: $SCRIPT_NAME not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -d "$TEMPLATES_DIR" ]; then
    echo "Error: templates/ directory not found in $SCRIPT_DIR"
    exit 1
fi

# Find a suitable Python 3.13+ interpreter
PYTHON=""
for candidate in python3.13 python3.14 python3; do
    if command -v "$candidate" &>/dev/null; then
        PY_VERSION=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 13 ]; then
            PYTHON=$(command -v "$candidate")
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.13+ is required but not found."
    echo "Install it from https://www.python.org/downloads/"
    exit 1
fi

echo "  Python: $PYTHON ($PY_VERSION)"

# Check for git
if ! command -v git &>/dev/null; then
    echo "Error: git is required but not found."
    exit 1
fi

# Install to ~/.local/share/local-diff/ with a wrapper in bin
DATA_DIR="$HOME/.local/share/$COMMAND_NAME"
mkdir -p "$DATA_DIR"

# Determine bin directory
if [ -n "$LOCAL_DIFF_INSTALL_DIR" ]; then
    BIN_DIR="$LOCAL_DIFF_INSTALL_DIR"
elif [ -w "/usr/local/bin" ]; then
    BIN_DIR="/usr/local/bin"
else
    mkdir -p "$HOME/.local/bin"
    BIN_DIR="$HOME/.local/bin"
fi

echo "Installing $COMMAND_NAME..."
echo "  App:     $DATA_DIR/"
echo "  Command: $BIN_DIR/$COMMAND_NAME"

# Copy script and templates
sed "1s|.*|#!${PYTHON}|" "$SOURCE" > "$DATA_DIR/$SCRIPT_NAME"
chmod +x "$DATA_DIR/$SCRIPT_NAME"
cp -r "$TEMPLATES_DIR" "$DATA_DIR/"

# Create wrapper script in bin
cat > "$BIN_DIR/$COMMAND_NAME" <<WRAPPER
#!${PYTHON}
import sys, runpy
sys.argv[0] = "$DATA_DIR/$SCRIPT_NAME"
runpy.run_path("$DATA_DIR/$SCRIPT_NAME", run_name="__main__")
WRAPPER
chmod +x "$BIN_DIR/$COMMAND_NAME"

# Verify it's on PATH
if command -v "$COMMAND_NAME" &>/dev/null; then
    echo ""
    echo "Done! Run '$COMMAND_NAME' inside any git repo."
else
    echo ""
    echo "Installed to $BIN_DIR/$COMMAND_NAME, but $BIN_DIR is not on your PATH."
    echo "Add it with:"
    echo ""
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    echo ""
    echo "Add that line to your ~/.bashrc or ~/.zshrc to make it permanent."
fi
