#!/usr/bin/env bash
set -e

COMMAND_NAME="local-diff"
DATA_DIR="$HOME/.local/share/$COMMAND_NAME"

LOCATION=$(command -v "$COMMAND_NAME" 2>/dev/null || true)

if [ -z "$LOCATION" ] && [ ! -d "$DATA_DIR" ]; then
    echo "$COMMAND_NAME is not installed."
    exit 0
fi

if [ -n "$LOCATION" ]; then
    echo "Removing $LOCATION..."
    rm "$LOCATION"
fi

if [ -d "$DATA_DIR" ]; then
    echo "Removing $DATA_DIR/..."
    rm -rf "$DATA_DIR"
fi

echo "Done! $COMMAND_NAME has been uninstalled."
