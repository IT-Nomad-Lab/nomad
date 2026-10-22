#!/usr/bin/env bash
# SessionStart: put the short SDLC rules in the session's context. Native bash on both hosts, no
# WSL call, so the rules arrive even when WSL is down.
cat "$(dirname "${BASH_SOURCE[0]}")/sdlc-context.md" 2>/dev/null
exit 0
