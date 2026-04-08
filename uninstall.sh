#!/usr/bin/env bash
set -e

COMMAND_NAME="local-diff"

LOCATION=$(command -v "$COMMAND_NAME" 2>/dev/null || true)

if [ -z "$LOCATION" ]; then
    echo "$COMMAND_NAME is not installed (not found on PATH)."
    exit 0
fi

echo "Removing $LOCATION..."
rm "$LOCATION"
echo "Done! $COMMAND_NAME has been uninstalled."
