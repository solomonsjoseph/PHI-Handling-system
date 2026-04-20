#!/usr/bin/env bash
# Quick-start script for resuming PHI-Handling-IRB-approval-ready in Claude Code.
#
# Usage:
#   bash setup-claude-code.sh [target-directory]
#
# If target-directory is not provided, uses ~/dev/PHI-Handling-IRB-approval-ready
#
# After this runs successfully, change into the target directory and run:
#   claude
# Then give Claude Code this prompt:
#   "Read CLAUDE.md in full before doing anything else. Then read
#    authorities/AUTHORITY_MATRIX.md to understand scope. Then execute
#    the build plan in CLAUDE.md Phase 1 through Phase 8 in order.
#    Stop after each phase and report status. Use the Truth Protocol."

set -euo pipefail

TARGET="${1:-$HOME/dev/PHI-Handling-IRB-approval-ready}"
TARBALL_NAME="phi-handling-handoff-v0.1.tar.gz"

echo "=== PHI-Handling-IRB-approval-ready setup ==="
echo "Target: $TARGET"

# Find the tarball
if [ -f "$TARBALL_NAME" ]; then
    TARBALL="$(pwd)/$TARBALL_NAME"
elif [ -f "$HOME/Downloads/$TARBALL_NAME" ]; then
    TARBALL="$HOME/Downloads/$TARBALL_NAME"
else
    echo "ERROR: Cannot find $TARBALL_NAME"
    echo "Place it in the current directory or $HOME/Downloads/ and retry."
    exit 1
fi

echo "Using tarball: $TARBALL"

# Verify hash if available
EXPECTED_HASH="cbcd6f38c662141db26f3f0992ca8072a70ea561afd45cdec0547e13a193f049"
if command -v sha256sum > /dev/null; then
    ACTUAL_HASH=$(sha256sum "$TARBALL" | awk '{print $1}')
    if [ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]; then
        echo "WARNING: Tarball hash mismatch."
        echo "  expected: $EXPECTED_HASH"
        echo "  actual:   $ACTUAL_HASH"
        echo "  If the tarball was rebuilt this is fine. Proceeding..."
    else
        echo "Tarball hash verified: $EXPECTED_HASH"
    fi
fi

# Create target parent if needed
mkdir -p "$(dirname "$TARGET")"

# Check if target already exists
if [ -d "$TARGET" ]; then
    echo "Target directory already exists: $TARGET"
    read -rp "Overwrite? (y/N): " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        echo "Aborted."
        exit 1
    fi
    rm -rf "$TARGET"
fi

# Extract
cd "$(dirname "$TARGET")"
tar -xzf "$TARBALL"
EXTRACTED="$(dirname "$TARGET")/PHI-Handling-IRB-approval-ready"
if [ "$EXTRACTED" != "$TARGET" ]; then
    mv "$EXTRACTED" "$TARGET"
fi

cd "$TARGET"

echo ""
echo "=== Extracted to: $TARGET ==="
echo ""

# Initialize git repo if not already (target repo is brucebanner010198-commits/PHI-Handling-IRB-approval-ready)
if [ ! -d .git ]; then
    echo "Initializing git repository..."
    git init -b main > /dev/null
    git add .
    git commit -m "chore: initial commit from claude.ai research phase handoff" > /dev/null
    echo "Git initialized. Add remote with:"
    echo "  git remote add origin https://github.com/brucebanner010198-commits/PHI-Handling-IRB-approval-ready.git"
fi

# Show status
echo ""
echo "=== Project status ==="
cat .phi-build-status
echo ""
echo "=== Next steps ==="
echo "1. Create and activate a Python virtual environment:"
echo "   python3 -m venv venv && source venv/bin/activate"
echo ""
echo "2. Install dependencies:"
echo "   pip install -r requirements.txt"
echo ""
echo "3. Start Claude Code in this directory:"
echo "   claude"
echo ""
echo "4. Paste this prompt into Claude Code:"
echo '   "Read CLAUDE.md in full before doing anything else. Then read'
echo '    authorities/AUTHORITY_MATRIX.md to understand scope. Then execute'
echo '    the build plan in CLAUDE.md Phase 1 through Phase 8 in order.'
echo '    Stop after each phase and report status. Use the Truth Protocol."'
echo ""
echo "Done. Working directory: $TARGET"
