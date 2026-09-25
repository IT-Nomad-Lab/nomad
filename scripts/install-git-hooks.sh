#!/usr/bin/env bash
# Point this clone at the versioned hooks in .githooks/ (roadmap T0.3).
# Idempotent. Run once per clone; .git/hooks is not tracked, so nothing else activates them.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks
echo "core.hooksPath = $(git config core.hooksPath)"

if command -v gitleaks >/dev/null 2>&1 || [ -x "$HOME/bin/gitleaks" ]; then
  GL="$(command -v gitleaks || echo "$HOME/bin/gitleaks")"
  echo "gitleaks: $GL ($("$GL" version))"
else
  echo "gitleaks: NOT FOUND. The hook still runs on its built-in pattern set, which"
  echo "  catches fewer shapes. Install the binary for full coverage:"
  echo "  curl -sSL -o /tmp/gl.tgz https://github.com/gitleaks/gitleaks/releases/download/v8.28.0/gitleaks_8.28.0_linux_x64.tar.gz"
  echo "  tar xzf /tmp/gl.tgz -C /tmp && install -m755 /tmp/gitleaks \"$HOME/bin/gitleaks\""
fi
echo "done. Verify with: git config core.hooksPath && ls -l .githooks/pre-commit"
