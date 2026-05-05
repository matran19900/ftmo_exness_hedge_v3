#!/usr/bin/env bash
# Wrapper for Claude Code that sends Telegram notifications when:
#   1. A permission-approval prompt appears on stdout (regex match).
#   2. Claude produces no stdout for >90 seconds while the process is alive.
#
# The 3rd notify type (commit done) is handled by .git/hooks/post-commit, not here.
#
# Throttle: max 1 notification per type per 180 seconds. State lives in
# /tmp/claude_notify_throttle/<type> (timestamp of last delivery).
#
# Exit code: claude's exit code is propagated unchanged.
#
# Bash subshell note: `cmd | while read line` runs the loop in a subshell, so
# a parent-shell variable cannot be updated from inside it. The watchdog also
# runs in its own subshell. Therefore a file (LAST_OUTPUT_FILE) is used as the
# shared timestamp that both the read-loop and the watchdog can see.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
NOTIFY_SH="$REPO_ROOT/scripts/notify_telegram.sh"
THROTTLE_DIR="/tmp/claude_notify_throttle"
LAST_OUTPUT_FILE="/tmp/claude_last_output_$$"
WATCHDOG_PID=""

mkdir -p "$THROTTLE_DIR"
date +%s >"$LAST_OUTPUT_FILE"

cleanup() {
  if [[ -n "${WATCHDOG_PID:-}" ]]; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
  fi
  rm -f "$LAST_OUTPUT_FILE"
}
trap cleanup EXIT

# Returns 0 if a notify of the given type is allowed (and records the timestamp);
# returns 1 if the last notify of that type was within the throttle window.
should_notify() {
  local type="$1"
  local file="$THROTTLE_DIR/$type"
  local now last
  now=$(date +%s)
  if [[ -f "$file" ]]; then
    last=$(cat "$file" 2>/dev/null || echo 0)
    if (( now - last < 180 )); then
      return 1
    fi
  fi
  echo "$now" >"$file"
  return 0
}

notify() {
  local message="$1"
  bash "$NOTIFY_SH" "$message" >/dev/null 2>&1 || true
}

# Watchdog: every 30s, check whether the gap between now and the last stdout
# line exceeds 90s. If so (and not throttled), fire a "stuck" notification.
watchdog() {
  while true; do
    sleep 30
    local now last elapsed
    now=$(date +%s)
    last=$(cat "$LAST_OUTPUT_FILE" 2>/dev/null || echo "$now")
    elapsed=$((now - last))
    if (( elapsed > 90 )); then
      if should_notify "stuck"; then
        notify "💤 [CLAUDE CODE] Possibly stuck
No stdout for ${elapsed}s
→ Check devcontainer"
      fi
    fi
  done
}

watchdog &
WATCHDOG_PID=$!

# Run claude, route both stdout/stderr through the monitor pipeline. Each line
# is echoed through to the user's terminal AND scanned for permission patterns.
# `${PIPESTATUS[0]}` recovers claude's own exit code regardless of the loop's.
claude --dangerously-skip-permissions "$@" 2>&1 | while IFS= read -r line; do
  printf '%s\n' "$line"
  date +%s >"$LAST_OUTPUT_FILE"
  if echo "$line" | grep -qiE '(allow.*\?|approve|permission|\(y/n\)|\[y/n\]|confirm)'; then
    if should_notify "approve"; then
      notify "⚠️ [CLAUDE CODE] Permission approval needed
Last line: $line
→ Open devcontainer to review"
    fi
  fi
done

exit "${PIPESTATUS[0]}"
