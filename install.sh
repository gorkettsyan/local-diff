#!/usr/bin/env bash
set -e

COMMAND_NAME="local-diff"

# Resolve the directory this installer lives in
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check for Node.js 20+
if ! command -v node &>/dev/null; then
    echo "Error: Node.js 20+ is required but not found."
    echo "Install it from https://nodejs.org/"
    exit 1
fi
NODE=$(command -v node)
NODE_MAJOR=$("$NODE" -p 'process.versions.node.split(".")[0]')
if [ "$NODE_MAJOR" -lt 20 ]; then
    echo "Error: Node.js 20+ is required (found $("$NODE" --version))."
    exit 1
fi
echo "  Node:    $NODE ($("$NODE" --version))"

# Check for git
if ! command -v git &>/dev/null; then
    echo "Error: git is required but not found."
    exit 1
fi

# Build the CLI and page script into dist/
echo "Building..."
(cd "$SCRIPT_DIR" && npm ci --no-audit --no-fund --loglevel=error && npm run --silent build)

# Install to ~/.local/share/local-diff/ with a wrapper in bin
DATA_DIR="$HOME/.local/share/$COMMAND_NAME"
rm -rf "$DATA_DIR"
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

# The bundle is self-contained: no node_modules needed at runtime.
cp "$SCRIPT_DIR"/dist/* "$DATA_DIR/"

# Create wrapper script in bin
cat > "$BIN_DIR/$COMMAND_NAME" <<WRAPPER
#!/usr/bin/env bash
exec "$NODE" "$DATA_DIR/local-diff.js" "\$@"
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
