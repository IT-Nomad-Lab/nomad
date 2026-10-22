#!/usr/bin/env bash
# ntfy push for Claude Code, one script for both hosts. Runs in whatever bash the session uses
# (Git Bash on Windows, bash in WSL), with no WSL call, so pushes still go out when WSL is down.
#
#   as a hook:  Notification and Stop events, payload JSON on stdin
#   by hand:    ntfy.sh "<title>" "<message>"
#
# The topic is read from $HOME/.config/ntfy/topic, a different file on each host. It is a secret:
# never printed, never logged. No topic file, no push. Always exits 0.
set -u

topic_file="$HOME/.config/ntfy/topic"
[ -s "$topic_file" ] || exit 0
topic="$(tr -d '[:space:]' < "$topic_file")"
[ -n "$topic" ] || exit 0

case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) host=windows ;;
  *) host=wsl ;;
esac

# One string field from flat hook JSON. Good enough for the few fields used here; escaped quotes
# in a message come out as \".
field() {
  printf '%s' "$payload" | tr '\n' ' ' |
    sed -nE "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"(([^\"\\\\]|\\\\.)*)\".*/\\1/p"
}

title="${1:-}"
message="${2:-}"
if [ -z "$title" ] && [ -z "$message" ]; then
  payload="$(cat)"
  case "$(field hook_event_name)" in
    Notification)
      title="Claude needs you"; message="$(field message)" ;;
    Stop|SubagentStop)
      printf '%s' "$payload" | grep -qE '"stop_hook_active"[[:space:]]*:[[:space:]]*true' && exit 0
      title="Claude finished"; message="Turn done" ;;
    *) exit 0 ;;
  esac
  cwd="$(field cwd)"
  if [ -n "$cwd" ]; then
    leaf="${cwd//\\\\//}"; leaf="${leaf%/}"; leaf="${leaf##*/}"
    message="$message - $leaf"
  fi
fi
[ -n "$title" ] || title="Claude"
[ -n "$message" ] || message="$title"

curl -s -m 10 -o /dev/null -H "Title: $title ($host)" -H "Tags: robot,$host" \
  --data-binary "$message" "${NTFY_SERVER:-https://ntfy.sh}/$topic" >/dev/null 2>&1
exit 0
